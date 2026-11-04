"""Shared tag behavior across processes, HTTP, repositories and linked work."""

import concurrent.futures
import json

import pytest
import work_tags
import wt
from test_dashboard_http import get, serve  # noqa: F401
from test_wt import repo, run_tool  # noqa: F401


def test_cli_and_dashboard_share_tags_without_starting_agents(repo, serve, monkeypatch):  # noqa: F811
    home, _, state = repo
    shared = home / "dashboard-state"
    monkeypatch.setenv("BABYSIT_HOME", str(shared))
    path = home / "src/repo"
    run_tool("tag", "add", "release", str(path), "https://github.com/Owner/Other/issues/12")
    run_tool("tag", "add", "release", "https://github.com/owner/third/pull/12")
    run_tool("tag", "add", "needs review", cwd=path)
    server = serve()
    server.tags = work_tags.Tags(shared)
    status, _, body = get(server, "/api/tags")
    assert status == 200
    assert json.loads(body)["assignments"] == {
        f"workspace:{path}": ["needs review", "release"],
        "issue:owner/other#12": ["release"],
        "pr:owner/third#12": ["release"],
    }
    status, _, _ = get(
        server,
        "/api/tags",
        {"Content-Type": "application/json", "X-Babysit-Action": "tags"},
        method="POST",
        body=json.dumps({"target": "pr:OWNER/third#12", "remove": ["release"], "add": ["ship"]}),
    )
    assert status == 200
    listing = run_tool("tag", "list", "release").stdout
    assert "issue:owner/other#12" in listing and "pr:owner/third#12" not in listing
    assert "ship" in run_tool("tag", "list").stdout
    assert json.loads(state.read_text())["calls"] == []


def test_linked_tags_are_bidirectional_without_transitive_spread(tmp_path):
    tags = work_tags.Tags(tmp_path)
    tags.change({"target": "workspace:/src/a", "add": ["workspace work"]})
    tags.change({"target": "issue:owner/a#7", "add": ["issue work"]})
    tags.link("workspace:/src/a", "issue:owner/a#7")
    tags.link("workspace:/src/a", "pr:owner/b#8")
    tags.link("workspace:m:mini:/src/a", "pr:owner/c#8")
    snapshot = tags.snapshot()
    assert work_tags.effective(snapshot, "workspace:/src/a") == ["issue work", "workspace work"]
    assert work_tags.effective(snapshot, "issue:owner/a#7") == ["issue work", "workspace work"]
    assert work_tags.effective(snapshot, "pr:owner/b#8") == ["workspace work"]
    assert work_tags.effective(snapshot, "workspace:m:mini:/src/a") == []
    tags.change({"target": "workspace:/src/a", "remove": ["workspace work"]})
    assert work_tags.effective(tags.snapshot(), "pr:owner/b#8") == []
    assert work_tags.effective(tags.snapshot(), "workspace:/src/a") == ["issue work"]


def test_concurrent_writers_preserve_independent_changes_and_deduplicate(tmp_path):
    def write(number):
        work_tags.Tags(tmp_path).change(
            {"target": "pr:owner/a#7", "add": [f"tag {number}", "shared"]}
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(write, range(12)))
    assert set(work_tags.Tags(tmp_path).snapshot()["assignments"]["pr:owner/a#7"]) == {
        "shared",
        *(f"tag {n}" for n in range(12)),
    }


@pytest.mark.parametrize("bad", ["", " \t", "x\ny", "x" * 65, 123, "hidden\x7f"])
def test_invalid_tag_requests_leave_all_assignments_intact(tmp_path, bad):
    tags = work_tags.Tags(tmp_path)
    tags.change({"target": "pr:owner/a#7", "add": ["keep"]})
    with pytest.raises(ValueError):
        tags.change({"target": "pr:owner/a#7", "remove": ["keep"], "add": ["new", bad]})
    assert tags.snapshot()["assignments"] == {"pr:owner/a#7": ["keep"]}


@pytest.mark.parametrize(
    "command,args,item",
    [
        ("wt", ["main", "tagged"], None),
        ("wti", ["12"], "issue:base/repo#12"),
        ("wtpr", ["7"], "pr:base/repo#7"),
    ],
)
def test_tag_option_records_workspace_and_known_github_link(repo, command, args, item):  # noqa: F811
    home, _, _ = repo
    run_tool(command, "--tag", "focus", "--tag", "another", "-r", "repo", *args)
    tags = work_tags.Tags(home / ".local/state/babysit-pr")
    snapshot = tags.snapshot()
    [(target, names)] = snapshot["assignments"].items()
    assert target.startswith(f"workspace:{home}/src/worktrees/repo/")
    assert names == ["another", "focus"]
    if item:
        assert snapshot["links"][target] == [item]
        assert item in run_tool("tag", "list", "focus").stdout


def test_invalid_tag_option_fails_before_creating_work(repo):  # noqa: F811
    home, _, state = repo
    result = run_tool("--tag", "bad\nname", "main", "tagged", check=False)
    assert result.returncode == 1 and "control characters" in result.stderr
    assert not (home / "src/worktrees").exists()
    assert json.loads(state.read_text())["calls"] == []


def test_tag_http_retains_csrf_guard_and_rejects_invalid_targets(serve):  # noqa: F811
    server = serve()
    body = json.dumps({"target": "pr:owner/a#7", "add": ["focus"]})
    assert get(server, "/api/tags", method="POST", body=body)[0] == 403
    headers = {"Content-Type": "application/json", "X-Babysit-Action": "tags"}
    assert (
        get(
            server,
            "/api/tags",
            {**headers, "Origin": "https://other.test"},
            method="POST",
            body=body,
        )[0]
        == 403
    )
    assert (
        get(
            server,
            "/api/tags",
            headers,
            method="POST",
            body=json.dumps({"target": "pr:owner/a#0", "add": ["focus"]}),
        )[0]
        == 400
    )
    assert json.loads(get(server, "/api/tags")[2])["assignments"] == {}


def test_launch_request_carries_tags_to_controller():
    options = wt.parse_args("wt", ["--tag", "release", "-p", "Fix this", "main", "feature"])
    assert wt.launch_request("wt", options, "owner/repo", "mini")["tags"] == ["release"]


def test_tagging_from_a_subdirectory_uses_the_workspace_root(repo):  # noqa: F811
    home, _, _ = repo
    nested = home / "src/repo/src/deep"
    nested.mkdir(parents=True)
    run_tool("tag", "add", "focus", cwd=nested)
    tags = work_tags.Tags(home / ".local/state/babysit-pr")
    assert tags.snapshot()["assignments"] == {f"workspace:{home}/src/repo": ["focus"]}


def test_inventory_links_are_read_without_scanning_and_failure_keeps_tags_available(serve):  # noqa: F811
    server = serve()
    server.tags.change({"target": "workspace:/src/a", "add": ["focus"]})

    class Inventory:
        def snapshot(self, start=True):
            assert start is False
            return {
                "workspaces": [
                    {"key": "/src/a", "links": [{"kind": "pr", "repo": "owner/other", "number": 8}]}
                ]
            }

    server.workspace_overview = Inventory()
    value = json.loads(get(server, "/api/tags")[2])
    assert work_tags.effective(value, "pr:owner/other#8") == ["focus"]

    class Broken:
        def snapshot(self, start=True):
            raise ValueError("Inventory unavailable")

    server.workspace_overview = Broken()
    status, _, body = get(server, "/api/tags")
    value = json.loads(body)
    assert status == 200 and value["assignments"] == {"workspace:/src/a": ["focus"]}
    assert "warning" in value
