"""Temporary clones, fake herdr and fake GitHub; never touches live workspaces."""

import http.client
import json
import os
import subprocess
import threading
import time
from pathlib import Path

import dashboard
import pytest
import workspace_overview as wso
from conftest import install_fakes

BASE = "base/repo"


def read(state):
    return json.loads(Path(state).read_text())


def calls(state, prefix):
    return [call for call in read(state)["calls"] if call[: len(prefix)] == prefix]


class FakeGitHub:
    """Answers only the endpoints this experiment is allowed to ask for."""

    def __init__(self):
        self.calls = []
        self.fail = set()
        self.repos = {
            "repos/fork/repo": {
                "full_name": "fork/repo",
                "fork": True,
                "parent": {"full_name": BASE},
            }
        }
        self.issues = {
            "12": {
                "number": 12,
                "title": "Crash on start",
                "html_url": f"https://github.com/{BASE}/issues/12",
                "state": "closed",
            },
            "44": {
                "number": 44,
                "title": "Still open",
                "html_url": f"https://github.com/{BASE}/issues/44",
                "state": "open",
            },
        }
        self.pulls = {
            "9": {
                "number": 9,
                "title": "Named pull request",
                "html_url": f"https://github.com/{BASE}/pull/9",
                "state": "open",
                "draft": True,
            }
        }
        self.heads = {
            "fork:merged-work": [
                {
                    "number": 7,
                    "title": "Merged work",
                    "html_url": f"https://github.com/{BASE}/pull/7",
                    "state": "closed",
                    "merged_at": "2026-09-01T00:00:00Z",
                }
            ],
            "fork:dirty-work": [
                {
                    "number": 8,
                    "title": "Dirty work",
                    "html_url": f"https://github.com/{BASE}/pull/8",
                    "state": "closed",
                }
            ],
        }

    def __call__(self, endpoint):
        self.calls.append(endpoint)
        if endpoint in self.fail:
            raise ValueError("gh: Not Found (HTTP 404)")
        if endpoint in self.repos:
            return self.repos[endpoint]
        if "/pulls?" in endpoint:
            head = endpoint.split("head=")[1]
            return self.heads.get(head, [])
        for kind, table in (("issues", self.issues), ("pulls", self.pulls)):
            prefix = f"repos/{BASE}/{kind}/"
            if endpoint.startswith(prefix):
                number = endpoint[len(prefix) :]
                if number not in table:
                    raise ValueError("gh: Not Found (HTTP 404)")
                return table[number]
        raise ValueError(f"gh: unexpected endpoint {endpoint}")


@pytest.fixture
def site(tmp_path, monkeypatch):
    """A clone with four worktrees covering clean, dirty, unpushed and issue checkouts."""
    src = tmp_path / "src"
    clone = src / "repo"
    clone.mkdir(parents=True)

    def git(*args, cwd=clone):
        return subprocess.check_output(["git", "-C", str(cwd), *args], text=True).strip()

    git("init", "-b", "main")
    git("config", "user.name", "Fixture")
    git("config", "user.email", "fixture@example.test")
    git("commit", "--allow-empty", "-m", "base")
    git("remote", "add", "origin", "https://github.com/fork/repo.git")
    git("remote", "add", "upstream", f"https://github.com/{BASE}.git")
    git("update-ref", "refs/remotes/origin/main", "HEAD")
    worktrees = src / "worktrees" / "repo"
    for branch in ("merged-work", "dirty-work", "unpushed-work", "issue-12-crash"):
        git("worktree", "add", "-b", branch, str(worktrees / branch))
        git("update-ref", f"refs/remotes/origin/{branch}", "HEAD")
    (worktrees / "dirty-work" / "scratch.txt").write_text("left behind")
    git("commit", "--allow-empty", "-m", "local only", cwd=worktrees / "unpushed-work")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    install_fakes(bin_dir)
    state = tmp_path / "herdr.json"
    state.write_text(
        json.dumps(
            {
                "workspaces": [
                    {
                        "workspace_id": "w1",
                        "label": "merged-work",
                        "agent_status": "idle",
                        "worktree": {
                            "repo_root": str(clone),
                            "checkout_path": str(worktrees / "merged-work"),
                        },
                    },
                    {
                        "workspace_id": "w2",
                        "label": "gone",
                        "agent_status": "unknown",
                        "worktree": {
                            "repo_root": str(clone),
                            "checkout_path": str(src / "worktrees/repo/removed"),
                        },
                    },
                ],
                "agents": [
                    {
                        "workspace_id": "w1",
                        "agent": "codex",
                        "agent_status": "idle",
                        "pane_id": "w1:p1",
                        "cwd": str(worktrees / "merged-work"),
                    }
                ],
                "calls": [],
            }
        )
    )
    monkeypatch.setenv("FAKE_HERDR", str(state))
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("HOME", str(tmp_path))
    home = tmp_path / "state"
    directory = home / "experiments" / "workspaces"
    directory.mkdir(parents=True)
    (directory / "config.json").write_text(json.dumps({"enabled": True, "src": str(src)}))
    fake = FakeGitHub()
    plugin = wso.WorkspaceOverview(home, fake)
    return plugin, fake, clone, worktrees, state, git


def texts(row):
    return [item["text"] for item in row["blockers"]]


def rows(plugin):
    return {row["name"]: row for row in plugin.collect()["workspaces"]}


def test_lists_worktrees_with_links_agents_and_local_state(site):
    plugin, fake, clone, worktrees, _, _ = site
    found = rows(plugin)
    assert set(found) == {"merged-work", "dirty-work", "unpushed-work", "issue-12-crash", "gone"}
    merged = found["merged-work"]
    assert merged["status"] == "ready"
    assert merged["repo"] == BASE and merged["branch"] == "merged-work"
    assert merged["links"] == [
        {
            "kind": "pr",
            "repo": BASE,
            "number": 7,
            "title": "Merged work",
            "url": f"https://github.com/{BASE}/pull/7",
            "state": "merged",
            "draft": False,
        }
    ]
    assert merged["agents"] == [{"agent": "codex", "status": "idle", "pane": "w1:p1"}]
    assert merged["workspace_ids"] == ["w1"] and merged["removable"]
    assert found["dirty-work"]["status"] == "blocked"
    assert texts(found["dirty-work"]) == ["1 uncommitted change(s)"]
    assert texts(found["unpushed-work"]) == ["1 commit(s) only on this branch"]
    assert found["issue-12-crash"]["links"][0]["number"] == 12
    assert found["issue-12-crash"]["links"][0]["state"] == "closed"
    # The clone's own checkout has no herdr workspace, so it is not a listed workspace.
    assert "repo" not in found
    assert found["gone"]["missing"] and found["gone"]["removable"]
    assert f"repos/{BASE}/pulls/9" not in fake.calls


def test_base_tracking_and_foreign_names_never_become_links():
    item = {"path": "/w/topic", "branch": "topic", "upstream": ["fork/repo", "dev"]}
    assert wso.candidates(item, BASE, "fork") == [("head", "fork:topic")]
    release = {"path": "/w/release_26.1", "branch": "release_26.1", "upstream": None}
    assert wso.candidates(release, BASE, "fork") == []
    named = {"path": "/w/pr-other-4", "branch": "pr-other-4", "upstream": None}
    assert wso.candidates(named, BASE, "fork") == [("head", "fork:pr-other-4")]
    mine = {"path": "/w/pr-base-4-2", "branch": "pr-base-4-2", "upstream": None}
    assert ("pr", "4") in wso.candidates(mine, BASE, "fork")
    fork_head = {"path": "/w/fix", "branch": "fix", "upstream": ["other/repo", "fix"]}
    assert wso.candidates(fork_head, BASE, "fork")[0] == ("head", "other:fix")


def test_links_are_cached_reused_and_bounded_by_the_request_budget(site, monkeypatch):
    plugin, fake, *_ = site
    plugin.collect()
    first = len(fake.calls)
    assert first and rows(plugin)["merged-work"]["links"]
    assert len(fake.calls) == first  # Fresh cache entries are reused, not refetched.
    monkeypatch.setattr(wso, "CALL_BUDGET", 0)
    plugin.links.clear()
    value = plugin.collect()
    assert value["calls"] == 0
    assert any("budget exhausted" in warning for warning in value["warnings"])
    worktrees = [row for row in value["workspaces"] if row["branch"]]
    assert worktrees and all(not row["links"] and row["stale_links"] for row in worktrees)


def test_base_repository_resolves_through_the_fork_parent_and_is_cached(site):
    plugin, fake, clone, *_ = site
    subprocess.check_call(["git", "-C", str(clone), "remote", "remove", "upstream"])
    assert rows(plugin)["merged-work"]["repo"] == BASE
    assert "repos/fork/repo" in fake.calls
    fake.calls.clear()
    assert rows(plugin)["merged-work"]["repo"] == BASE
    assert "repos/fork/repo" not in fake.calls


def test_a_failed_repository_lookup_is_not_cached_as_an_answer(site):
    plugin, fake, clone, *_ = site
    subprocess.check_call(["git", "-C", str(clone), "remote", "remove", "upstream"])
    fake.fail.add("repos/fork/repo")
    value = plugin.collect()
    assert any("repository lookup failed" in warning for warning in value["warnings"])
    fake.fail.clear()
    assert rows(plugin)["merged-work"]["repo"] == BASE


def finish(plugin, started=None):
    assert started is None or started["cleanup"]["status"] in {"running", "complete"}
    deadline = time.monotonic() + 30
    while plugin.job["status"] == "running" and time.monotonic() < deadline:
        time.sleep(0.02)
    assert plugin.job["status"] == "complete"
    return {result["key"]: result for result in plugin.job["results"]}


def test_cleanup_exits_agents_closes_the_workspace_and_removes_worktree_and_branch(site):
    plugin, _, clone, worktrees, state, git = site
    target = str(worktrees / "merged-work")
    results = finish(plugin, plugin.cleanup({"targets": [{"key": target}]}))
    assert results[target]["status"] == "done"
    assert calls(state, ["agent", "send-keys", "w1:p1"])
    assert calls(state, ["workspace", "close", "w1"])
    assert not Path(target).exists()
    assert "merged-work" not in git("branch", "--format=%(refname:short)").split()
    assert "worktree" in " ".join(results[target]["steps"]).lower()


def test_cleanup_skips_local_only_work_until_it_is_explicitly_forced(site):
    plugin, _, clone, worktrees, state, git = site
    dirty, unpushed = str(worktrees / "dirty-work"), str(worktrees / "unpushed-work")
    results = finish(plugin, plugin.cleanup({"targets": [{"key": dirty}, {"key": unpushed}]}))
    assert results[dirty]["status"] == "skipped"
    assert "uncommitted" in results[dirty]["message"]
    assert results[unpushed]["status"] == "skipped"
    assert Path(dirty).exists() and Path(unpushed).exists()
    results = finish(
        plugin,
        plugin.cleanup(
            {"targets": [{"key": dirty, "approve": ["dirty", "unpushed"]}, {"key": unpushed}]}
        ),
    )
    assert results[dirty]["status"] == "done"
    assert not Path(dirty).exists()
    assert "dirty-work" not in git("branch", "--format=%(refname:short)").split()
    assert results[unpushed]["status"] == "skipped" and Path(unpushed).exists()


def test_a_branch_git_refuses_to_delete_is_kept_and_reported(site, monkeypatch):
    plugin, _, clone, worktrees, _, git = site
    target = str(worktrees / "unpushed-work")
    # Local state cannot be verified, so only Git's own safe delete decides — and it
    # refuses, leaving the branch behind with its worktree already gone.
    monkeypatch.setattr(
        wso,
        "local_state",
        lambda path, branch=None: {
            "changes": None,
            "unpushed": None,
            "error": "Local state unavailable: fixture",
        },
    )
    results = finish(plugin, plugin.cleanup({"targets": [{"key": target, "approve": ["unknown"]}]}))
    assert results[target]["status"] == "done", results[target]["message"]
    assert not Path(target).exists()
    assert "Kept local branch unpushed-work" in results[target]["message"]
    assert "not fully merged" in results[target]["message"]
    assert "unpushed-work" in git("branch", "--format=%(refname:short)").split()


def test_a_branch_no_commit_can_be_lost_from_is_deleted_even_when_unmerged(site):
    plugin, _, clone, worktrees, _, git = site
    target = str(worktrees / "unpushed-work")
    git("update-ref", "refs/remotes/origin/unpushed-work", "unpushed-work")
    results = finish(plugin, plugin.cleanup({"targets": [{"key": target}]}))
    assert results[target]["status"] == "done" and results[target]["message"] == "Cleaned up"
    assert "unpushed-work" not in git("branch", "--format=%(refname:short)").split()


def test_a_blocker_that_appears_after_selection_stops_the_cleanup(site, monkeypatch):
    """An override approves the blockers the user saw, never one that arrived later."""
    plugin, _, _, worktrees, _, _ = site
    target = str(worktrees / "unpushed-work")
    original = wso.WorkspaceOverview.resolve

    def resolve(self, key):
        row = original(self, key)
        if row:
            row["blockers"].append(wso.blocker("dirty", "9 uncommitted change(s)"))
        return row

    monkeypatch.setattr(wso.WorkspaceOverview, "resolve", resolve)
    results = finish(
        plugin, plugin.cleanup({"targets": [{"key": target, "approve": ["unpushed"]}]})
    )
    assert results[target]["status"] == "skipped"
    assert results[target]["message"] == "Skipped: 9 uncommitted change(s)"
    assert Path(target).exists()


def test_a_checkout_outside_the_scanned_clones_is_never_removed(tmp_path, site):
    plugin, _, _, _, _, _ = site
    outside = tmp_path / "elsewhere"
    outside.mkdir()

    def git(*args, cwd=outside):
        return subprocess.check_output(["git", "-C", str(cwd), *args], text=True).strip()

    git("init", "-b", "main")
    git("config", "user.name", "Fixture")
    git("config", "user.email", "fixture@example.test")
    git("commit", "--allow-empty", "-m", "base")
    victim = tmp_path / "victim"
    git("worktree", "add", "-b", "secret", str(victim))
    key = str(victim)
    results = finish(
        plugin, plugin.cleanup({"targets": [{"key": key, "approve": sorted(wso.OVERRIDABLE)}]})
    )
    assert results[key]["status"] == "failed"
    assert "outside the scanned clones" in results[key]["message"]
    assert victim.exists()


def test_a_main_checkout_with_a_workspace_only_loses_that_workspace(site):
    plugin, _, clone, _, state, git = site
    data = read(state)
    data["workspaces"].append(
        {
            "workspace_id": "w3",
            "label": "repo",
            "agent_status": "idle",
            "worktree": {"repo_root": str(clone), "checkout_path": str(clone)},
        }
    )
    Path(state).write_text(json.dumps(data))
    key = str(clone)
    results = finish(plugin, plugin.cleanup({"targets": [{"key": key, "approve": ["main"]}]}))
    assert results[key]["status"] == "done"
    assert calls(state, ["workspace", "close", "w3"])
    assert Path(clone, ".git").exists()
    assert "main" in git("branch", "--format=%(refname:short)").split()


def test_an_unexpected_failure_stops_only_its_own_target(site, monkeypatch):
    plugin, _, _, worktrees, _, _ = site
    first, second = str(worktrees / "merged-work"), str(worktrees / "issue-12-crash")
    original = wso.WorkspaceOverview.remove

    def remove(self, job, index, target):
        if target["key"] == first:
            raise RuntimeError("herdr returned nonsense")
        return original(self, job, index, target)

    monkeypatch.setattr(wso.WorkspaceOverview, "remove", remove)
    results = finish(plugin, plugin.cleanup({"targets": [{"key": first}, {"key": second}]}))
    assert results[first]["status"] == "failed"
    assert "nonsense" in results[first]["message"]
    assert results[second]["status"] == "done" and not Path(second).exists()


def test_cleanup_never_removes_a_main_checkout_or_a_watched_checkout(site):
    plugin, _, clone, worktrees, state, _ = site
    results = finish(
        plugin, plugin.cleanup({"targets": [{"key": str(clone), "approve": ["main"]}]})
    )
    assert results[str(clone)]["status"] == "skipped"
    assert Path(clone, ".git").exists()
    target = str(worktrees / "merged-work")
    plugin.jobs = lambda: [{"cwd": target}]
    results = finish(
        plugin, plugin.cleanup({"targets": [{"key": target, "approve": sorted(wso.OVERRIDABLE)}]})
    )
    assert "watch" in results[target]["message"]
    assert Path(target).exists()


def test_a_missing_checkout_only_closes_its_workspace(site):
    plugin, _, _, _, state, _ = site
    results = finish(plugin, plugin.cleanup({"targets": [{"key": "workspace:w2"}]}))
    assert results["workspace:w2"]["status"] == "done"
    assert calls(state, ["workspace", "close", "w2"])
    assert not read(state)["workspaces"] or all(
        space["workspace_id"] != "w2" for space in read(state)["workspaces"]
    )


def test_an_unknown_target_reports_that_nothing_remains(site):
    plugin, _, _, worktrees, _, _ = site
    key = str(worktrees / "never-existed")
    assert finish(plugin, plugin.cleanup({"targets": [{"key": key}]}))[key]["status"] == "done"


def test_a_running_batch_is_returned_instead_of_starting_a_second(site, monkeypatch):
    plugin, _, _, worktrees, _, _ = site
    gate = threading.Event()
    monkeypatch.setattr(
        wso.WorkspaceOverview, "remove", lambda *args, **kwargs: gate.wait(timeout=10)
    )
    started = plugin.cleanup({"targets": [{"key": str(worktrees / "merged-work")}]})
    refused = plugin.cleanup({"targets": [{"key": str(worktrees / "dirty-work")}]})
    assert started["accepted"] and not refused["accepted"]
    assert refused["cleanup"]["id"] == started["cleanup"]["id"]
    gate.set()
    finish(plugin)


@pytest.mark.parametrize(
    "request_body",
    [
        {},
        {"targets": []},
        {"targets": [{"key": ""}]},
        {"targets": [{"key": "/a"}, {"key": "/a"}]},
        {"targets": [{"key": "/a", "approve": "dirty"}]},
        {"targets": [{"key": "/a", "other": 1}]},
        {"targets": "/a"},
        {"targets": [{"key": "/a"}], "unexpected": 1},
        {"targets": [{"key": f"/{'a' * 2000}"}]},
    ],
)
def test_cleanup_rejects_invalid_requests(site, request_body):
    plugin, *_ = site
    with pytest.raises(ValueError):
        plugin.cleanup(request_body)
    assert plugin.job is None


def test_too_many_targets_are_refused(site):
    plugin, *_ = site
    with pytest.raises(ValueError):
        plugin.cleanup({"targets": [{"key": f"/w/{n}"} for n in range(wso.MAX_TARGETS + 1)]})


def test_disabled_and_missing_configuration_do_no_work(tmp_path):
    plugin = wso.WorkspaceOverview(tmp_path, lambda endpoint: pytest.fail("no GitHub call"))
    assert plugin.snapshot() == {
        "workspaces": [],
        "warnings": [],
        "synced_at": None,
        "enabled": False,
        "loading": False,
        "error": None,
        "cleanup": None,
        "stale": False,
    }
    with pytest.raises(ValueError):
        plugin.cleanup({"targets": [{"key": "/w/one"}]})
    directory = tmp_path / "experiments" / "workspaces"
    directory.mkdir(parents=True)
    (directory / "config.json").write_text(json.dumps({"enabled": False}))
    assert not wso.WorkspaceOverview(tmp_path).enabled


def test_a_broken_configuration_is_reported_without_raising(tmp_path):
    directory = tmp_path / "experiments" / "workspaces"
    directory.mkdir(parents=True)
    (directory / "config.json").write_text("{not json")
    plugin = wso.WorkspaceOverview(tmp_path)
    assert not plugin.enabled and "unavailable" in plugin.error


def test_the_cache_survives_a_restart_and_an_old_format_is_discarded(site):
    plugin, fake, *_ = site
    plugin._refresh()
    assert plugin.value["workspaces"] and (plugin.directory / "cache.json").exists()
    assert (plugin.directory / "cache.json").stat().st_mode & 0o777 == 0o600
    restarted = wso.WorkspaceOverview(plugin.home, fake)
    assert restarted.links == plugin.links and restarted.bases == plugin.bases
    saved = json.loads((plugin.directory / "cache.json").read_text())
    (plugin.directory / "cache.json").write_text(json.dumps({**saved, "version": 0}))
    assert not wso.WorkspaceOverview(plugin.home, fake).links


def test_a_refresh_failure_keeps_the_previous_inventory_and_reports_it(site, monkeypatch):
    plugin, *_ = site
    plugin._refresh()
    kept = plugin.value["workspaces"]
    monkeypatch.setattr(
        wso.WorkspaceOverview, "inventory", lambda *args: (_ for _ in ()).throw(OSError("no herdr"))
    )
    plugin._refresh()
    assert plugin.value["workspaces"] == kept
    assert "no herdr" in plugin.error
    assert plugin.snapshot()["stale"]


def test_an_unavailable_herdr_still_lists_worktrees(site, monkeypatch):
    plugin, *_ = site
    monkeypatch.setenv("PATH", "/usr/bin:/bin")  # Git remains; herdr does not.
    value = plugin.collect()
    assert any("herdr" in warning for warning in value["warnings"])
    assert {row["name"] for row in value["workspaces"]} >= {"merged-work", "dirty-work"}
    assert all(not row["workspace_ids"] for row in value["workspaces"])


def test_requested_refresh_respects_the_floor(site, monkeypatch):
    plugin, *_ = site
    plugin.value["synced_at"] = time.time()
    plugin.next_poll = time.time() + wso.INTERVAL
    plugin.request_refresh()
    assert plugin.next_poll > time.time()
    plugin.value["synced_at"] = time.time() - wso.MIN_REFRESH - 1
    plugin.request_refresh()
    assert plugin.next_poll == 0.0


def request(port, path, body=None, action="workspace-cleanup"):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        if body is None:
            connection.request("GET", path, headers={"Host": f"127.0.0.1:{port}"})
        else:
            connection.request(
                "POST",
                path,
                json.dumps(body),
                {
                    "Host": f"127.0.0.1:{port}",
                    "Content-Type": "application/json",
                    "X-Babysit-Action": action,
                },
            )
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


@pytest.fixture
def server(site):
    plugin = site[0]
    plugin.next_poll = float("inf")
    with dashboard.DashboardServer(plugin.home, 0, workspace_overview=plugin) as httpd:
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        yield httpd.server_port, plugin
        httpd.shutdown()
        thread.join(5)


def test_the_endpoint_serves_the_snapshot_and_starts_a_cleanup(server, site):
    port, plugin = server
    worktrees = site[3]
    status, value = request(port, "/api/workspace-overview")
    assert status == 200 and value["enabled"] and value["cleanup"] is None
    status, value = request(
        port, "/api/workspace-cleanup", {"targets": [{"key": str(worktrees / "merged-work")}]}
    )
    assert status == 200 and value["cleanup"]["status"] in {"running", "complete"}
    finish(plugin)
    status, value = request(port, "/api/workspace-overview")
    assert value["cleanup"]["results"][0]["status"] == "done"


def test_the_endpoint_rejects_an_invalid_cleanup_and_a_disabled_experiment(server, tmp_path):
    port, _ = server
    status, value = request(port, "/api/workspace-cleanup", {"targets": []})
    assert status == 400 and "Select between" in value["error"]
    with dashboard.DashboardServer(tmp_path, 0) as httpd:
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            assert request(httpd.server_port, "/api/workspace-overview")[1] == {"enabled": False}
            status, value = request(
                httpd.server_port, "/api/workspace-cleanup", {"targets": [{"key": "/w/one"}]}
            )
            assert status == 400 and "disabled" in value["error"]
        finally:
            httpd.shutdown()
            thread.join(5)


def test_a_failing_snapshot_stays_inside_the_experiment(server, monkeypatch):
    port, plugin = server
    monkeypatch.setattr(
        type(plugin), "snapshot", lambda self: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    assert request(port, "/api/workspace-overview") == (
        200,
        {"enabled": True, "error": "Workspace experiment unavailable"},
    )


@pytest.mark.parametrize("name,workspace_id", [("merged-work", "w1"), ("dirty-work", "w3")])
def test_open_workspace_reuses_or_creates_without_touching_agents(server, site, name, workspace_id):
    port, plugin = server
    _, _, _, worktrees, state, _ = site
    plugin.value = plugin.collect()
    before = read(state)["agents"]
    for _ in range(2):
        status, value = request(
            port, "/api/workspace-open", {"key": str(worktrees / name)}, action="workspace-open"
        )
        assert status == 200 and value == {"url": f"{wso.COLLIE_URL}/space/{workspace_id}"}
    opened = calls(state, ["worktree", "open"])
    assert len(opened) == (0 if name == "merged-work" else 1)
    if opened:
        assert "--no-focus" in opened[0]
    assert read(state)["agents"] == before
    assert not calls(state, ["workspace", "focus"])
    assert (worktrees / "dirty-work" / "scratch.txt").read_text() == "left behind"


def test_open_workspace_rejects_unknown_removed_and_disabled(site):
    plugin, _, _, worktrees, _, git = site
    plugin.value = plugin.collect()
    with pytest.raises(ValueError, match="Unknown workspace"):
        plugin.open_workspace({"key": "/not-listed"})
    key = str(worktrees / "issue-12-crash")
    git("worktree", "remove", key)
    with pytest.raises(ValueError, match="changed"):
        plugin.open_workspace({"key": key})
    plugin.enabled = False
    with pytest.raises(ValueError, match="disabled"):
        plugin.open_workspace({"key": str(worktrees / "merged-work")})
