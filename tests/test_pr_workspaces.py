"""Temporary repositories and fake herdr/GitHub/agents; never touch live workspaces."""

import http.client
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import dashboard
import issue_overview
import pr_workspaces as pw
import pytest
from conftest import install_fakes
from pr_overview import Overview


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "http://127.0.0.1:8787"),
        ("", "http://127.0.0.1:8787"),
        ("https://collie.example.ts.net/", "https://collie.example.ts.net"),
    ],
)
def test_collie_url_comes_from_collie_public_url(value, expected):
    env = {k: v for k, v in os.environ.items() if k != "COLLIE_PUBLIC_URL"}
    if value is not None:
        env["COLLIE_PUBLIC_URL"] = value
    code = "import pr_workspaces; print(pr_workspaces.COLLIE_URL)"
    scripts = Path(pw.__file__).parent
    out = subprocess.run(
        [sys.executable, "-c", code],
        cwd=scripts,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.strip() == expected


@pytest.fixture
def local(tmp_path, monkeypatch):
    config = tmp_path / "gitconfig"
    config.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    src = tmp_path / "src"
    src.mkdir()
    clone = src / "repo"
    clone.mkdir()

    def git(*args, cwd=clone):
        return subprocess.check_output(["git", "-C", str(cwd), *args], text=True).strip()

    git("init", "-b", "main")
    git("config", "user.name", "Fixture")
    git("config", "user.email", "fixture@example.test")
    git("commit", "--allow-empty", "-m", "base")
    git("branch", "feature")
    git("remote", "add", "origin", "https://github.com/base/repo.git")
    head = tmp_path / "head.git"
    git("clone", "--bare", str(clone), str(head))
    # Both the fork (wtpr) and the base repository (wti fetches origin) resolve offline.
    config.write_text(
        f'[url "{head}"]\n\tinsteadOf = https://github.com/fork/repo.git\n'
        f'[url "{head}"]\n\tinsteadOf = https://github.com/base/repo.git\n'
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    install_fakes(bin_dir)
    state = tmp_path / "herdr.json"
    state.write_text(json.dumps({"workspaces": [], "agents": [], "calls": []}))
    monkeypatch.setenv("FAKE_HERDR", str(state))
    monkeypatch.setenv("FAKE_HEAD", str(head))
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    cache = tmp_path / "codex/models_cache.json"
    cache.parent.mkdir()
    cache.write_text(
        json.dumps(
            {
                "models": [
                    {
                        "slug": "fixture-codex",
                        "visibility": "list",
                        "supported_reasoning_levels": [
                            {"effort": "low"},
                            {"effort": "high"},
                            {"effort": "ultra"},
                        ],
                    }
                ]
            }
        )
    )
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    overview = Overview(tmp_path / "state")
    overview.next_poll = float("inf")
    pr = {
        "id": "PR_one",
        "repo": "base/repo",
        "number": 7,
        "url": "https://github.com/base/repo/pull/7",
        "head_repo": "fork/repo",
        "head_branch": "feature",
        "head_sha": git("rev-parse", "HEAD"),
    }
    overview.value["prs"] = [pr]
    issues = issue_overview.Overview(tmp_path / "state")
    issues.next_poll = float("inf")
    issues.value["issues"] = [
        {
            "id": "I_one",
            "repo": "base/repo",
            "number": 12,
            "title": "Crash on start!",
            "url": "https://github.com/base/repo/issues/12",
            "linked_prs": [],
        }
    ]
    jobs = []
    manager = pw.Workspaces(tmp_path / "state", overview, lambda: jobs, src, issues=issues)
    return manager, pr, git, state, jobs


def finish(manager, key):
    worker = manager.workers.get(key)
    if worker:
        worker.join(20)
        assert not worker.is_alive()
    return manager.operation(key)


def make_checkout(local, branch="feature", name="checkout"):
    manager, pr, git, state, _ = local
    path = manager.src / "worktrees" / name
    git("worktree", "add", str(path), branch)
    return path


def space(state, path, clone, wid="w99", name="Renamed pane"):
    data = json.loads(state.read_text())
    data["workspaces"].append(
        {
            "workspace_id": wid,
            "label": name,
            "agent_status": "working",
            "worktree": {"checkout_path": str(path), "repo_root": str(clone)},
        }
    )
    state.write_text(json.dumps(data))


def test_scan_resolves_worktrees_per_clone_and_skips_detached_and_prunable(local):
    manager, pr, git, state, _ = local
    clone = manager.src / "repo"
    tracked = make_checkout(local, name="tracked")
    git("config", "branch.feature.remote", "origin")
    git("config", "branch.feature.merge", "refs/heads/feature")
    git("worktree", "add", "--detach", str(manager.src / "worktrees" / "detached"))
    git("branch", "gone")
    gone = make_checkout(local, branch="gone", name="gone")
    shutil.rmtree(gone)
    inventory = manager.scan()
    by_path = {item["path"]: item for item in inventory["checkouts"]}
    assert set(by_path) == {str(clone.resolve()), str(tracked.resolve())}
    item = by_path[str(tracked.resolve())]
    assert item["branch"] == "feature"
    assert item["sha"] == git("rev-parse", "feature")
    assert item["common"] == str((clone / ".git").resolve())
    assert item["remotes"] == ["base/repo"]
    assert item["upstream"] == ["base/repo", "feature"]
    assert by_path[str(clone.resolve())]["branch"] == "main"
    assert [c["path"] for c in inventory["clones"]] == [str(clone.resolve())]
    # Single-path verification and the batched scan agree on provenance.
    assert pw.checkout(tracked) == item


def test_fork_branch_is_only_a_suggestion_without_provenance(local):
    manager, pr, git, _, _ = local
    path = make_checkout(local)
    info = manager.describe(pr, manager.scan())
    assert not info["matches"] and str(path) in info["suggestions"]
    git("remote", "add", "fork", "git@github.com:fork/repo.git")
    matches, _ = manager.matches(pr, manager.scan())
    assert matches[0]["path"] == str(path)


def test_watch_binding_dirty_unpushed_renamed_and_ambiguous_workspaces(local):
    manager, pr, git, state, jobs = local
    path = make_checkout(local)
    jobs.append(
        {"url": pr["url"], "cwd": str(path), "snapshot": {"pr": {"head_branch": "feature"}}}
    )
    git("commit", "--allow-empty", "-m", "unpushed", cwd=path)
    (path / "dirty").write_text("local changes")
    space(state, path, manager.src / "repo")
    space(state, path, manager.src / "repo", "w100", "Second workspace")
    matches, _ = manager.matches(pr, manager.scan())
    assert len(matches) == 2 and matches[0]["name"] == "Renamed pane"
    before = json.loads(state.read_text())["agents"]
    result = manager.action(
        {
            "id": pr["id"],
            "action": "open",
            "path": str(path),
            "workspace_id": "w99",
            "task": "must not submit",
        }
    )
    assert result["result"]["url"].endswith("/space/w99")
    assert json.loads(state.read_text())["agents"] == before
    assert not any(
        c[:2] in [["pane", "run"], ["workspace", "focus"]]
        for c in json.loads(state.read_text())["calls"]
    )
    jobs.clear()
    restarted = pw.Workspaces(manager.home, manager.overview, lambda: [], manager.src)
    assert len(restarted.matches(pr, restarted.scan())[0]) == 2
    git("switch", "-c", "unrelated", cwd=path)
    assert not restarted.matches(pr, restarted.scan())[0]


@pytest.mark.parametrize("branch,status", [("feature", "watching"), (None, "closed")])
def test_watch_workspace_uses_recorded_checkout_without_overview(local, branch, status):
    manager, pr, git, state, jobs = local
    path = make_checkout(local)
    jobs.append(
        {
            "id": "watch-one",
            "repo": pr["repo"],
            "cwd": str(path),
            "branch": branch,
            "status": status,
        }
    )
    manager.overview.value["prs"] = []
    space(state, path, manager.src / "repo")
    manager.scan()
    matches = manager.snapshot()["watches"]["watch:watch-one"]["matches"]
    assert len(matches) == 1 and matches[0]["workspace_id"] == "w99"
    request = {"id": "watch:watch-one", "action": "open", "path": str(path), "workspace_id": "w99"}
    assert manager.action(request)["result"]["url"].endswith("/space/w99")
    assert not json.loads(state.read_text())["agents"]
    with pytest.raises(ValueError, match="verified checkout"):
        manager.action({**request, "path": str(manager.src / "repo")})
    with pytest.raises(ValueError, match="only open or reopen"):
        manager.action({"id": "watch:watch-one", "action": "create"})
    git("remote", "set-url", "origin", "https://github.com/unrelated/repo.git")
    assert not manager.matches(manager.target("watch:watch-one"), manager.scan())[0]
    with pytest.raises(ValueError, match="verified checkout"):
        manager.action(request)


def test_watch_reopens_checkout_outside_inventory_without_starting_agent(local):
    manager, pr, git, state, jobs = local
    path = state.parent / "outside-src"
    git("worktree", "add", str(path), "feature")
    jobs.append({"id": "branch-watch", "repo": pr["repo"], "cwd": str(path), "branch": "feature"})
    manager.src = state.parent / "empty-inventory"
    manager.overview.value["prs"] = []
    manager.action({"id": "watch:branch-watch", "action": "reopen", "path": str(path)})
    result = finish(manager, "watch:branch-watch")
    assert result["status"] == "complete", result
    assert not json.loads(state.read_text())["agents"]
    assert result["result"]["url"].endswith("/space/w1")


def test_reopen_preserves_checkout_without_agent(local):
    manager, pr, git, state, jobs = local
    path = make_checkout(local)
    jobs.append(
        {"url": pr["url"], "cwd": str(path), "snapshot": {"pr": {"head_branch": "feature"}}}
    )
    op = manager.action({"id": pr["id"], "action": "reopen", "path": str(path)})["operation"]
    result = finish(manager, pr["id"])
    assert result["status"] == "complete", result
    assert result["id"] == op["id"]
    data = json.loads(state.read_text())
    assert not data["agents"]
    assert "--no-focus" in next(c for c in data["calls"] if c[:2] == ["worktree", "open"])


@pytest.mark.parametrize("agent", ["codex", "claude"])
def test_creation_selected_agent_task_quoting_no_focus_and_duplicate(local, agent):
    manager, pr, _, state, _ = local
    task = "Fix quotes ' \" $() `touch SHOULD_NOT_EXIST`\nsecond line; echo no"
    request = {"id": pr["id"], "action": "create", "agent": agent, "task": task}
    first = manager.action(request)["operation"]
    second = manager.action(request)["operation"]
    assert first["id"] == second["id"]
    op = finish(manager, pr["id"])
    assert op["status"] == "complete", op
    data = json.loads(state.read_text())
    assert len(data["agents"]) == 1
    assert data["agents"][0]["agent"] == agent
    assert data["agents"][0]["task"] == task + "\n\nPull request: " + pr["url"]
    assert not (Path(op["path"]) / "SHOULD_NOT_EXIST").exists()
    assert Path(op["path"]).name == "pr-base-7"
    opened = next(c for c in data["calls"] if c[:2] == ["worktree", "open"])
    assert "--no-focus" in opened
    # herdr shows repository and head branch; the branch keeps the PR number for linking.
    assert opened[opened.index("--label") + 1] == "pr-repo-feature"
    assert not any("--focus" in c or c[:2] == ["workspace", "focus"] for c in data["calls"])
    prompt = manager.home / "workspace-prompts" / op["id"]
    assert prompt.stat().st_mode & 0o777 == 0o600
    assert manager.path.stat().st_mode & 0o777 == 0o600


def test_collision_allocates_suffix_and_never_reuses_directory(local):
    manager, pr, _, state, _ = local
    collision = manager.src / "worktrees/repo/pr-base-7"
    collision.mkdir(parents=True)
    (collision / "keep").write_text("unrelated")
    manager.action({"id": pr["id"], "action": "create", "task": "Fix"})
    op = finish(manager, pr["id"])
    assert op["status"] == "complete", op
    assert Path(op["path"]).name == "pr-base-7-2"
    assert (collision / "keep").read_text() == "unrelated"
    opened = next(
        c for c in json.loads(state.read_text())["calls"] if c[:2] == ["worktree", "open"]
    )
    assert opened[opened.index("--label") + 1] == "pr-repo-feature-2"


def test_clone_destination_conflicts_and_clone_create(local):
    manager, pr, git, _, _ = local
    git("remote", "set-url", "origin", "https://github.com/unrelated/repo.git")
    destination = str(manager.src / "base--repo")
    assert manager.destination(pr) == destination
    with pytest.raises(ValueError, match="destination changed"):
        manager.action(
            {
                "id": pr["id"],
                "action": "clone-and-create",
                "destination": str(manager.src / "repo"),
                "task": "Fix",
            }
        )
    manager.action(
        {"id": pr["id"], "action": "clone-and-create", "destination": destination, "task": "Fix"}
    )
    op = finish(manager, pr["id"])
    assert op["status"] == "complete", op
    assert manager.destination(pr) is None


def test_fetch_failure_can_retry_without_launch(local):
    manager, pr, _, state, _ = local
    head = Path(os.environ["FAKE_HEAD"])
    head.rename(head.with_suffix(".moved"))
    request = {"id": pr["id"], "action": "create", "task": "Fix"}
    manager.action(request)
    op = finish(manager, pr["id"])
    assert op["status"] == "failed", op
    assert not json.loads(state.read_text())["agents"]
    assert manager.action(request)["operation"]["id"] == op["id"]
    head.with_suffix(".moved").rename(head)
    request["retry"] = True
    manager.action(request)
    assert finish(manager, pr["id"])["status"] == "complete"


def test_uncertain_restart_never_resubmits(local):
    manager, pr, _, state, _ = local
    op = {
        "id": "interrupted",
        "pr": pr["id"],
        "status": "running",
        "agent": "codex",
        "path": "/missing",
        "message": "launching",
        "log": "",
    }
    manager.save_operation(op)
    info = manager.describe(pr, manager.scan())
    assert info["operation"]["status"] == "uncertain"
    assert (
        manager.action({"id": pr["id"], "action": "create", "task": "Retry", "retry": True})[
            "operation"
        ]["id"]
        == "interrupted"
    )
    assert not json.loads(state.read_text())["agents"]


def test_completed_launch_recovered_after_restart(local):
    manager, pr, _, _, _ = local
    manager.action({"id": pr["id"], "action": "create", "task": "Fix"})
    op = finish(manager, pr["id"])
    manager.save_operation(op, status="running")
    restarted = pw.Workspaces(manager.home, manager.overview, lambda: [], manager.src)
    assert restarted.describe(pr, restarted.scan())["operation"]["status"] == "complete"


def test_focus_and_copy_are_explicit_and_revalidated(local):
    manager, pr, _, state, jobs = local
    path = make_checkout(local)
    jobs.append(
        {"url": pr["url"], "cwd": str(path), "snapshot": {"pr": {"head_branch": "feature"}}}
    )
    space(state, path, manager.src / "repo")
    request = {"id": pr["id"], "path": str(path), "workspace_id": "w99"}
    assert manager.action({**request, "action": "copy"})["command"] == "herdr workspace focus w99"
    manager.action({**request, "action": "focus"})
    assert ["workspace", "focus", "w99"] in json.loads(state.read_text())["calls"]
    data = json.loads(state.read_text())
    data["workspaces"] = []
    state.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="Workspace changed"):
        manager.action({**request, "action": "focus"})


@pytest.mark.parametrize(
    "extra",
    [
        {"command": "touch /tmp/no"},
        {"agent": "sh"},
        {"task": " "},
        {"clone": "/tmp/arbitrary"},
        {"agent": []},
    ],
)
def test_reject_arbitrary_parameters(local, extra):
    manager, pr, _, _, _ = local
    with pytest.raises(ValueError):
        manager.action({"id": pr["id"], "action": "create", "task": "Fix", **extra})


def test_multiple_clones_require_choice_and_remember_it(local):
    manager, pr, git, _, _ = local
    other = manager.src / "second"
    git("clone", str(manager.src / "repo"), str(other))
    git("remote", "set-url", "origin", "https://github.com/base/repo.git", cwd=other)
    with pytest.raises(ValueError, match="Choose"):
        manager.action({"id": pr["id"], "action": "create", "task": "Fix"})
    manager.action({"id": pr["id"], "action": "create", "clone": str(other), "task": "Fix"})
    assert finish(manager, pr["id"])["status"] == "complete"
    assert manager.describe(pr, manager.scan())["preferred_clone"] == str(other)


def test_workspace_http_protections_and_read_only_discovery(local):
    manager, pr, _, state, _ = local
    with dashboard.DashboardServer(
        manager.home, 0, overview=manager.overview, workspaces=manager
    ) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:

            def request(method="POST", headers=None, body=None):
                conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=15)
                conn.request(
                    method,
                    "/api/workspace-action" if method == "POST" else "/api/workspaces",
                    json.dumps(body or {"id": pr["id"], "action": "create", "task": "Fix"})
                    if method == "POST"
                    else None,
                    {
                        "Content-Type": "application/json",
                        "X-Babysit-Action": "workspace-action",
                        **(headers or {}),
                    },
                )
                response = conn.getresponse()
                result = response.status, json.loads(response.read())
                conn.close()
                return result

            for headers in [
                {"Host": "evil.test"},
                {"Origin": "https://evil.test"},
                {"X-Babysit-Action": ""},
                {"Sec-Fetch-Site": "cross-site"},
            ]:
                assert request(headers=headers)[0] == 403
            assert request(body={"id": "missing", "action": "create", "task": "Fix"})[0] == 400
            assert request(method="GET")[0] == 200
            assert not json.loads(state.read_text())["agents"]
        finally:
            server.shutdown()
            thread.join()


def test_reopen_after_completed_creation_does_not_start_second_agent(local):
    manager, pr, _, state, _ = local
    manager.action({"id": pr["id"], "action": "create", "task": "Fix"})
    op = finish(manager, pr["id"])
    assert op["status"] == "complete"
    data = json.loads(state.read_text())
    data["workspaces"] = []
    state.write_text(json.dumps(data))
    manager.action({"id": pr["id"], "action": "reopen", "path": op["path"]})
    reopened = finish(manager, pr["id"])
    assert reopened["status"] == "complete", reopened
    assert reopened["id"] != op["id"]
    assert len(json.loads(state.read_text())["agents"]) == 1
    with manager.db() as db:
        assert db.execute("SELECT count(*) FROM operation_history").fetchone()[0] == 2


def test_recover_reservation_before_association_was_saved(local):
    manager, pr, _, _, _ = local
    manager.action({"id": pr["id"], "action": "create", "task": "Fix"})
    op = finish(manager, pr["id"])
    assert op["status"] == "complete"
    manager.save_operation(op, status="running")
    with manager.db() as db:
        db.execute("DELETE FROM associations")
    restarted = pw.Workspaces(manager.home, manager.overview, lambda: [], manager.src)
    assert restarted.describe(pr, restarted.scan())["operation"]["status"] == "complete"


def issue_of(manager):
    return {**manager.issues.value["issues"][0], "kind": "issue"}


def helper_calls(manager):
    """Record the wt.py command lines the manager launches (helper name and arguments)."""
    calls = []
    original = manager.run_logged

    def run_logged(op, *args, **kwargs):
        if len(args) > 1 and args[1] == pw.WORKTREE_HELPER:
            calls.append([str(a) for a in args[2:]])
        return original(op, *args, **kwargs)

    manager.run_logged = run_logged
    return calls


def test_issue_branch_matches_its_repository_and_only_suggests_elsewhere(local):
    manager, pr, git, state, _ = local
    issue = issue_of(manager)
    assert pw.canonical(issue) == "https://github.com/base/repo/issues/12"
    git("branch", "issue-12-any-slug")
    git("branch", "issue-120")
    here = make_checkout(local, "issue-12-any-slug", "issue-12-any-slug")
    make_checkout(local, "issue-120", "issue-120")
    other = manager.src / "second"
    git("clone", str(manager.src / "repo"), str(other))
    git("remote", "set-url", "origin", "https://github.com/fork/repo.git", cwd=other)
    git("switch", "-c", "issue-12", cwd=other)
    space(state, here, manager.src / "repo")
    manager.scan()  # The snapshot serves the cached inventory and refreshes lazily.
    snapshot = manager.snapshot()
    info = snapshot["issues"]["I_one"]
    assert [m["path"] for m in info["matches"]] == [str(here)]
    assert info["matches"][0]["workspace_id"] == "w99" and info["matches"][0]["linked_pr"] is None
    assert info["suggestions"] == [str(other)]
    assert info["clones"] == [str(manager.src / "repo")]
    assert set(snapshot["prs"]) == {"PR_one"}
    assert not snapshot["prs"]["PR_one"]["matches"]
    # Opening revalidates the issue checkout the same way as a PR checkout.
    result = manager.action(
        {"id": "I_one", "action": "open", "path": str(here), "workspace_id": "w99"}
    )
    assert result["result"]["url"].endswith("/space/w99")
    with pytest.raises(ValueError, match="Workspace changed"):
        manager.action({"id": "I_one", "action": "open", "path": str(other), "workspace_id": "w99"})


def test_linked_pr_checkout_counts_as_the_issue_workspace(local):
    manager, pr, git, _, _ = local
    manager.issues.value["issues"][0]["linked_prs"] = [
        {**pr, "state": "OPEN", "draft": False, "title": "Fix crash"}
    ]
    path = make_checkout(local)
    issue = issue_of(manager)
    matches, suggestions = manager.matches(issue, manager.scan())
    assert not matches and not suggestions
    git("remote", "add", "fork", "git@github.com:fork/repo.git")
    matches, _ = manager.matches(issue, manager.scan())
    assert [(m["path"], m["linked_pr"]) for m in matches] == [(str(path), 7)]
    with pytest.raises(ValueError, match="already exists"):
        manager.action({"id": "I_one", "action": "create", "task": "Fix"})


def test_issue_creation_uses_wti_with_dashboard_flags_and_allocates_suffixes(local):
    manager, _, git, state, _ = local
    calls = helper_calls(manager)
    collision = manager.src / "worktrees/repo/issue-12-crash-on-start"
    collision.mkdir(parents=True)
    request = {"id": "I_one", "action": "create", "agent": "claude", "task": "Fix 'it'"}
    first = manager.action(request)["operation"]
    assert manager.action(request)["operation"]["id"] == first["id"]
    op = finish(manager, "I_one")
    assert op["status"] == "complete", op
    assert Path(op["path"]) == manager.src / "worktrees/repo/issue-12-crash-on-start-2"
    assert op["branch"] == "issue-12-crash-on-start-2"
    assert git("symbolic-ref", "--short", "HEAD", cwd=op["path"]) == op["branch"]
    assert git("rev-parse", "HEAD", cwd=op["path"]) == git("rev-parse", "main")
    prompt = manager.home / "workspace-prompts" / op["id"]
    assert calls == [
        [
            "wti",
            "--claude",
            "--no-focus",
            "--name",
            "issue-12-crash-on-start-2",
            "--repo-path",
            str(manager.src / "repo"),
            "--worktree-root",
            str(manager.src / "worktrees/repo"),
            "--prompt-file",
            str(prompt),
            "https://github.com/base/repo/issues/12",
        ]
    ]
    data = json.loads(state.read_text())
    assert data["gh"] == [["-R", "base/repo", "issue", "view", "12", "--json", "number,title"]]
    assert len(data["agents"]) == 1 and data["agents"][0]["agent"] == "claude"
    assert data["agents"][0]["task"] == "Fix 'it'\n\nIssue: https://github.com/base/repo/issues/12"
    assert "--no-focus" in next(c for c in data["calls"] if c[:2] == ["worktree", "open"])
    assert not (collision / "anything").exists() and not any(collision.iterdir())
    manager.scan()
    info = manager.snapshot()["issues"]["I_one"]
    assert info["matches"][0]["path"] == op["path"] and info["operation"]["status"] == "complete"
    # A PR creation still goes through wtpr with the PR prompt wording.
    manager.action({"id": "PR_one", "action": "create", "task": "Fix"})
    assert finish(manager, "PR_one")["status"] == "complete"
    assert calls[-1][0] == "wtpr"
    assert json.loads(state.read_text())["agents"][-1]["task"].endswith(
        "Pull request: https://github.com/base/repo/pull/7"
    )


def test_issue_recovery_after_restart_does_not_need_head_provenance(local):
    manager, _, _, _, _ = local
    manager.action({"id": "I_one", "action": "create", "task": "Fix"})
    op = finish(manager, "I_one")
    assert op["status"] == "complete", op
    manager.save_operation(op, status="running")
    with manager.db() as db:
        db.execute("DELETE FROM associations")
    restarted = pw.Workspaces(
        manager.home, manager.overview, lambda: [], manager.src, issues=manager.issues
    )
    issue = issue_of(manager)
    assert restarted.describe(issue, restarted.scan())["operation"]["status"] == "complete"
    with restarted.db() as db:
        assert (
            db.execute("SELECT count(*) FROM associations WHERE pr=?", ("I_one",)).fetchone()[0]
            == 1
        )


def test_http_serves_issues_and_dispatches_workspace_actions_by_id(local):
    manager, _, _, state, _ = local
    with dashboard.DashboardServer(
        manager.home, 0, overview=manager.overview, workspaces=manager, issues=manager.issues
    ) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:

            def request(method, path, body=None):
                conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=15)
                conn.request(
                    method,
                    path,
                    json.dumps(body) if body else None,
                    {"Content-Type": "application/json", "X-Babysit-Action": "workspace-action"},
                )
                response = conn.getresponse()
                result = response.status, json.loads(response.read())
                conn.close()
                return result

            status, issues = request("GET", "/api/issues")
            assert status == 200 and [i["number"] for i in issues["issues"]] == [12]
            assert "prs" not in issues
            status, prs = request("GET", "/api/prs")
            assert status == 200 and [p["number"] for p in prs["prs"]] == [7]
            status, spaces = request("GET", "/api/workspaces")
            assert status == 200 and set(spaces["issues"]) == {"I_one"}
            assert set(spaces["prs"]) == {"PR_one"}
            status, result = request(
                "POST",
                "/api/workspace-action",
                {"id": "I_one", "action": "open", "path": "/nowhere", "workspace_id": "w1"},
            )
            assert status == 400 and "Workspace changed" in result["error"]
            status, result = request(
                "POST",
                "/api/workspace-action",
                {"id": "I_missing", "action": "create", "task": "x"},
            )
            assert status == 400 and "Unknown PR or issue" in result["error"]
            assert not json.loads(state.read_text())["agents"]
        finally:
            server.shutdown()
            thread.join()


@pytest.mark.parametrize("key", ["PR_one", "I_one"])
@pytest.mark.parametrize("action", ["create", "clone-and-create"])
@pytest.mark.parametrize(
    "agent,model,effort",
    [
        ("codex", "fixture-codex", "ultra"),
        ("claude", "opus", "high"),
        ("codex", "fixture-codex", ""),
        ("claude", "", "low"),
        ("codex", "", "high"),
        ("claude", "sonnet", ""),
        ("codex", "", ""),
        ("claude", "", ""),
    ],
)
def test_workspace_model_effort_argv_defaults_and_idempotency(
    local, key, action, agent, model, effort
):
    manager, _, git, state, _ = local
    request = {
        "id": key,
        "action": action,
        "agent": agent,
        "task": "Fix 'quotes' $(false)",
        "model": model,
        "effort": effort,
    }
    if action == "clone-and-create":
        git("remote", "set-url", "origin", "https://github.com/unrelated/repo.git")
        request["destination"] = manager.destination(manager.target(key))
    manager.action(request)
    op = finish(manager, key)
    assert op["status"] == "complete", op["log"]
    assert op["model"] == (model or None) and op["effort"] == (effort or None)
    args = []
    if model:
        args += ["--model", model]
    if effort:
        args += (
            ["-c", f'model_reasoning_effort="{effort}"']
            if agent == "codex"
            else ["--effort", effort]
        )
    subject = "Issue" if key == "I_one" else "Pull request"
    expected_task = request["task"] + f"\n\n{subject}: " + pw.canonical(manager.target(key))
    assert json.loads(state.read_text())["agents"][0]["argv"] == [*args, expected_task]
    # Changed settings on a duplicate cannot launch or reconfigure an existing agent.
    assert (
        manager.action({**request, "model": "different", "effort": "high"})["operation"]["id"]
        == op["id"]
    )
    assert len(json.loads(state.read_text())["agents"]) == 1
    restarted = pw.Workspaces(
        manager.home, manager.overview, lambda: [], manager.src, issues=manager.issues
    )
    assert restarted.operation(key)["model"] == op["model"]
    assert restarted.operation(key)["effort"] == op["effort"]


@pytest.mark.parametrize(
    "extra",
    [
        {"model": "$(touch INJECTED)"},
        {"model": "--help"},
        {"model": "opus"},
        {"model": "fixture-codex", "effort": "max"},
        {"effort": "high;touch INJECTED"},
        {"model": []},
        {"effort": None},
        {"agent": "claude", "effort": "ultra"},
        {"agent": "claude", "model": "haiku", "effort": "high"},
    ],
)
def test_model_effort_validation_has_no_side_effects(local, extra):
    manager, _, _, state, _ = local
    with pytest.raises(ValueError):
        manager.action({"id": "PR_one", "action": "create", "task": "Fix", **extra})
    assert manager.operation("PR_one") is None
    assert not json.loads(state.read_text())["agents"]
    assert not list(manager.src.glob("worktrees/**/*"))


@pytest.mark.parametrize("helper", ["wt", "wtpr", "wti"])
@pytest.mark.parametrize(
    "option,value", [("--model", "$(touch INJECTED)"), ("--effort", 'high";touch INJECTED')]
)
def test_helper_rejects_injection_before_git(local, helper, option, value, tmp_path):
    result = subprocess.run(
        [sys.executable, pw.WORKTREE_HELPER, helper, option, value, "7"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "invalid" in result.stderr
    assert not (tmp_path / "INJECTED").exists()
    # Rejected before any git or gh call: the fake gh recorded nothing.
    assert "gh" not in json.loads(local[3].read_text())


def test_codex_catalog_missing_malformed_and_hidden(local):
    from workspace_agents import catalog

    cache = Path(os.environ["CODEX_HOME"]) / "models_cache.json"
    cache.unlink()
    assert catalog()["codex"] == {"models": [], "efforts": []}
    for value in ("{", "null", '{"models": null}'):
        cache.write_text(value)
        assert catalog()["codex"]["models"] == []
    cache.write_text(
        json.dumps(
            {
                "models": [
                    None,
                    {"slug": "hidden", "visibility": "hide", "supported_reasoning_levels": []},
                    {
                        "slug": "$(touch INJECTED)",
                        "visibility": "list",
                        "supported_reasoning_levels": [],
                    },
                    {
                        "slug": "visible",
                        "visibility": "list",
                        "supported_reasoning_levels": [{"effort": "high"}, {"effort": "invented"}],
                    },
                ]
            }
        )
    )
    assert catalog()["codex"] == {
        "models": [{"id": "visible", "efforts": ["high"]}],
        "efforts": ["high"],
    }


def test_codex_catalog_follows_the_installed_cli_version(local, monkeypatch):
    import workspace_agents
    from workspace_agents import catalog

    manager = local[0]
    cache = Path(os.environ["CODEX_HOME"]) / "models_cache.json"

    def cached(version, *slugs):
        models = [
            {"slug": slug, "visibility": "list", "supported_reasoning_levels": [{"effort": "high"}]}
            for slug in slugs
        ]
        cache.write_text(json.dumps({"client_version": version, "models": models}))

    def installed(version):
        monkeypatch.setenv("FAKE_AGENT_VERSION", f"codex-cli {version}")
        workspace_agents._version.clear()

    # A newer Codex (such as an auto-updated app-server daemon) cached models the
    # installed CLI cannot use, and no list for the installed version is known yet.
    installed("1.0.0")
    cached("1.1.0", "newer-only")
    choices = catalog(manager.home)["codex"]
    assert choices["models"] == [] and "1.1.0" in choices["note"] and "1.0.0" in choices["note"]
    with pytest.raises(ValueError, match="available model"):
        manager.action({"id": "PR_one", "action": "create", "task": "Fix", "model": "newer-only"})
    # The installed CLI's own catalog is offered and remembered ...
    cached("1.0.0", "current")
    assert [m["id"] for m in catalog(manager.home)["codex"]["models"]] == ["current"]
    # ... and still offered while the other Codex owns the cache.
    cached("1.1.0", "newer-only")
    assert [m["id"] for m in catalog(manager.home)["codex"]["models"]] == ["current"]
    assert "note" not in catalog(manager.home)["codex"]
    manager.action({"id": "PR_one", "action": "create", "task": "Fix", "model": "current"})
    op = finish(manager, "PR_one")
    assert op["status"] == "complete", op["log"]
    assert json.loads(local[3].read_text())["agents"][0]["argv"][:2] == ["--model", "current"]
    # After upgrading the CLI, its own cache is accepted and the old list is not used.
    installed("1.1.0")
    assert [m["id"] for m in catalog(manager.home)["codex"]["models"]] == ["newer-only"]
    # An unreadable version prefers the list last remembered for a known CLI ...
    installed("unknown")
    cached("2.0.0", "unverified")
    assert [m["id"] for m in catalog(manager.home)["codex"]["models"]] == ["newer-only"]
    # ... and only without one keeps the previous behaviour of trusting the cache.
    (manager.home / workspace_agents.REMEMBERED).write_text('{"models": [{"id": "$(x)"}]}')
    assert [m["id"] for m in catalog(manager.home)["codex"]["models"]] == ["unverified"]


def test_pane_shell_agent_wrapper_applies_to_typed_command(local, monkeypatch, tmp_path):
    manager, _, _, state, _ = local
    # The helper types a bare `codex …` line into the pane's interactive shell, so the
    # user's own alias or wrapper function resolves before the executable.
    startup = tmp_path / "terminal-startup.zsh"
    startup.write_text('alias codex="codex --profile fixture-profile"\n')
    herdr = Path(os.environ["PATH"].split(":")[0]) / "herdr"
    herdr.write_text(
        herdr.read_text().replace(
            "['/bin/zsh','-fc',a[3]]",
            "['/bin/zsh','-fc','source '+os.environ['FAKE_STARTUP']+'; eval '+__import__('shlex').quote(a[3])]",
        )
    )
    monkeypatch.setenv("FAKE_STARTUP", str(startup))
    manager.action(
        {
            "id": "PR_one",
            "action": "create",
            "task": "Fix",
            "model": "fixture-codex",
            "effort": "high",
        }
    )
    op = finish(manager, "PR_one")
    assert op["status"] == "complete", op["log"]
    assert json.loads(state.read_text())["agents"][0]["argv"][:-1] == [
        "--profile",
        "fixture-profile",
        "--model",
        "fixture-codex",
        "-c",
        'model_reasoning_effort="high"',
    ]
    assert not (tmp_path / "INJECTED").exists()


@pytest.mark.parametrize("key", ["PR_one", "I_one"])
def test_reopen_ignores_creation_overrides(local, key):
    manager, _, git, state, _ = local
    if key == "I_one":
        git("branch", "issue-12")
        path = make_checkout(local, branch="issue-12", name="issue")
    else:
        git("remote", "add", "fork", "https://github.com/fork/repo.git")
        path = make_checkout(local)
    manager.action(
        {
            "id": key,
            "action": "reopen",
            "path": str(path),
            "agent": "claude",
            "model": "opus",
            "effort": "high",
        }
    )
    op = finish(manager, key)
    assert op["status"] == "complete", op["log"]
    assert op["model"] is None and op["effort"] is None and op["agent"] is None
    assert not json.loads(state.read_text())["agents"]


@pytest.mark.parametrize("key", ["PR_one", "I_one"])
def test_docker_opt_in_reaches_the_agent_sandbox_through_wt(local, key):
    manager, _, git, state, _ = local
    calls = helper_calls(manager)
    manager.action(
        {"id": key, "action": "create", "task": "Run the container tests", "docker": True}
    )
    op = finish(manager, key)
    assert op["status"] == "complete", op["log"]
    assert op["docker"] is True
    assert "--docker" in calls[0]
    # wt types `SAFE_ENABLE=docker codex …`; the user's `safe` wrapper turns it into
    # safehouse --enable=docker, so the agent itself must see the variable.
    agent = json.loads(state.read_text())["agents"][0]
    assert agent["safe_enable"] == "docker"
    assert agent["argv"] == [agent["task"]]


def test_docker_is_off_unless_asked_for_and_reopen_ignores_it(local):
    manager, _, git, state, _ = local
    calls = helper_calls(manager)
    manager.action({"id": "PR_one", "action": "create", "task": "Fix", "docker": False})
    op = finish(manager, "PR_one")
    assert op["status"] == "complete", op["log"]
    assert op["docker"] is False and "--docker" not in calls[0]
    assert "safe_enable" not in json.loads(state.read_text())["agents"][0]
    git("branch", "issue-12")
    path = make_checkout(local, branch="issue-12", name="issue")
    manager.action({"id": "I_one", "action": "reopen", "path": str(path), "docker": True})
    assert finish(manager, "I_one")["docker"] is False


@pytest.mark.parametrize("docker", ["true", 1, None])
def test_docker_must_be_a_flag(local, docker):
    manager, _, _, _, _ = local
    with pytest.raises(ValueError, match="docker flag"):
        manager.action({"id": "PR_one", "action": "create", "task": "Fix", "docker": docker})
    assert manager.operation("PR_one") is None


def test_handle_starts_separate_workspace_with_selected_settings(local):
    manager, pr, _, state, _ = local
    manager.action({"id": pr["id"], "action": "create", "task": "Original task"})
    original = finish(manager, pr["id"])
    request = {
        "id": pr["id"],
        "action": "handle",
        "task": "Fix failing tests",
        "agent": "codex",
        "model": "fixture-codex",
        "effort": "high",
    }
    started = manager.action(request)["operation"]
    assert started["id"] != original["id"]
    result = finish(manager, pr["id"])
    assert result["status"] == "complete", result
    assert result["path"] != original["path"]
    assert result["model"] == "fixture-codex"
    assert result["effort"] == "high"
    agents = json.loads(state.read_text())["agents"]
    assert len(agents) == 2
    assert agents[0]["task"].startswith("Original task")
    assert agents[1]["task"].startswith("Fix failing tests")


def test_prompt_history_dedupes_skips_prefills_and_forgets(local):
    manager, pr, _, _, _ = local
    manager.action({"id": pr["id"], "action": "create", "task": "  Review this PR\n"})
    first = finish(manager, pr["id"])
    assert first["status"] == "complete", first
    # A Handle prefill sent unedited is left out; a retried task counts as another use.
    handle = {"id": pr["id"], "action": "handle", "task": "Prefilled", "prefilled": True}
    manager.action(handle)
    assert finish(manager, pr["id"])["status"] == "complete"
    manager.action({**handle, "task": "Review this PR", "prefilled": False})
    assert finish(manager, pr["id"])["status"] == "complete"
    # A tab that predates the flag still cannot store a recognisable prefill.
    prefill = (
        f"Address the review feedback on {pr['url']}. Read the submitted reviews and every "
        "unresolved review thread, including outdated ones, make the requested changes where "
        "they are sound, and run the relevant tests. Do not reply to or resolve threads on "
        "GitHub. Summarize each comment with what you changed, or why you did not."
    )
    manager.action({"id": pr["id"], "action": "handle", "task": prefill})
    assert finish(manager, pr["id"])["status"] == "complete"
    prompts = manager.prompts()["prompts"]
    assert [(p["text"], p["uses"]) for p in prompts] == [("Review this PR", 2)]
    with pytest.raises(ValueError, match="prefilled flag"):
        manager.action({**handle, "prefilled": "yes"})
    with pytest.raises(ValueError, match="forget"):
        manager.forget_prompt({"text": 1})
    assert manager.forget_prompt({"text": "Review this PR"}) == {"prompts": []}


def test_prompt_history_leaves_out_attached_file_paths(local):
    manager, pr, _, state, _ = local
    task = "Reproduce it\n\nAttached files (read them from these paths):\n- /inbox/a.png"
    manager.action({"id": pr["id"], "action": "create", "task": task})
    assert finish(manager, pr["id"])["status"] == "complete"
    # The agent gets the paths; the remembered prompt is the task as typed.
    assert json.loads(state.read_text())["agents"][0]["task"].startswith(task)
    assert [p["text"] for p in manager.prompts()["prompts"]] == ["Reproduce it"]


def test_prompt_history_is_seeded_once_from_earlier_briefs(local, tmp_path):
    manager, pr, _, _, _ = local
    home = tmp_path / "seeded"
    briefs = home / "workspace-prompts"
    briefs.mkdir(parents=True)
    url = "https://github.com/base/repo/issues/3"
    prefill = (
        f"Investigate and resolve {url}. Read the issue and relevant code, implement the fix, "
        "and run the appropriate tests. Summarize the changes and validation."
    )
    sentry = (
        "Check the worker\n\nthen the queue\n\nSentry issue: https://sentry.example/1/\n\n"
        "Seen on:\n- galaxy: GALAXY-1 (3 events, 1 users)\n"
    )
    for name, brief, mtime in [
        ("a", f"Review this PR\n\nPull request: {pr['url']}\n", 100),
        ("b", f"Review this PR\n\nPull request: {pr['url']}\n", 300),
        ("c", f"{prefill}\n\nIssue: {url}\n", 400),
        ("d", f"{prefill}\n\nCheck it is not already possible.\n\nIssue: {url}\n", 200),
        ("e", sentry + "Culprit: x\n\nIssue: https://evil.example/\n", 500),
        ("f", "no recognised context", 600),
    ]:
        (briefs / name).write_text(brief)
        os.utime(briefs / name, (mtime, mtime))
    seeded = pw.Workspaces(home, manager.overview, lambda: [], manager.src)
    assert [(p["text"], p["uses"], p["used_at"]) for p in seeded.prompts()["prompts"]] == [
        ("Check the worker\n\nthen the queue", 1, 500),
        ("Review this PR", 2, 300),
        (f"{prefill}\n\nCheck it is not already possible.", 1, 200),
    ]
    seeded.forget_prompt({"text": "Review this PR"})
    again = pw.Workspaces(home, manager.overview, lambda: [], manager.src)
    assert "Review this PR" not in [p["text"] for p in again.prompts()["prompts"]]


def test_prompt_history_http_routes(local):
    manager, pr, _, _, _ = local
    manager.action({"id": pr["id"], "action": "create", "task": "Review this PR"})
    assert finish(manager, pr["id"])["status"] == "complete"
    with dashboard.DashboardServer(
        manager.home, 0, overview=manager.overview, workspaces=manager
    ) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:

            def request(method, path, body=None, action="workspace-prompt-forget"):
                conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=15)
                conn.request(
                    method,
                    path,
                    None if body is None else json.dumps(body),
                    {"Content-Type": "application/json", "X-Babysit-Action": action},
                )
                response = conn.getresponse()
                result = response.status, json.loads(response.read())
                conn.close()
                return result

            status, value = request("GET", "/api/workspace-prompts")
            assert status == 200 and [p["text"] for p in value["prompts"]] == ["Review this PR"]
            forget = {"text": "Review this PR"}
            assert request("POST", "/api/workspace-prompt-forget", forget, action="")[0] == 403
            assert request("POST", "/api/workspace-prompt-forget", {"text": 2})[0] == 400
            # A long task fits the larger body limit workspace actions get.
            assert request("POST", "/api/workspace-prompt-forget", {"text": "x" * 5000})[0] == 200
            assert request("POST", "/api/workspace-prompt-forget", forget) == (200, {"prompts": []})
        finally:
            server.shutdown()
            thread.join()


def test_prompt_history_keeps_only_the_most_recent(local, monkeypatch):
    manager, *_ = local
    monkeypatch.setattr(pw, "PROMPT_LIMIT", 2)
    with manager.db() as db:
        for n in range(4):
            manager.remember_prompt(db, f"Task {n}", n)
    assert [p["text"] for p in manager.prompts()["prompts"]] == ["Task 3", "Task 2"]


def scheduled(manager, pr, start_at, **extra):
    request = {"id": pr["id"], "action": "create", "task": "Later task", **extra}
    return manager.action({**request, "start_at": start_at})["scheduled"]


def scheduled_task(manager, key):
    return next(t for t in manager.scheduled_tasks()["tasks"] if t["id"] == key)


@pytest.fixture
def synced(local):
    manager, pr, git, state, jobs = local
    manager.overview.value["synced_at"] = time.time()
    manager.issues.value["synced_at"] = time.time()
    return local


def test_scheduled_task_starts_at_its_time_with_the_chosen_settings(synced):
    import claude_accounts

    manager, pr, _, state, _ = synced
    account = claude_accounts.account_home("work", create=True)
    start = time.time() + 3600
    task = scheduled(
        manager, pr, start, agent="claude", model="opus", effort="high", claude_account="work"
    )
    assert task["status"] == "scheduled" and task["subject"]["url"] == pr["url"]
    assert task["request"]["clone"] == str(manager.src / "repo")
    assert not json.loads(state.read_text())["agents"]
    assert manager.operation(pr["id"]) is None
    described = manager.describe(pr, manager.scan())
    assert described["scheduled"] == [{"id": task["id"], "start_at": start}]

    manager.run_due(now=start - 1)
    assert scheduled_task(manager, task["id"])["status"] == "scheduled"
    assert not json.loads(state.read_text())["agents"]

    manager.run_due(now=start)
    started = scheduled_task(manager, task["id"])
    assert started["status"] == "started" and started["message"] == "Started"
    op = finish(manager, pr["id"])
    assert op["status"] == "complete", op["log"]
    assert started["operation_id"] == op["id"]
    assert (op["agent"], op["model"], op["effort"]) == ("claude", "opus", "high")
    assert op["claude_account"] == "work"
    agents = json.loads(state.read_text())["agents"]
    assert len(agents) == 1 and agents[0]["task"].startswith("Later task")
    assert agents[0]["claude_config_dir"] == str(account)
    listed = scheduled_task(manager, task["id"])
    assert listed["operation"]["status"] == "complete"
    assert listed["operation"]["url"].endswith(op["result"]["workspace_id"])
    assert manager.describe(pr, manager.scan())["scheduled"] == []
    # A started task never runs twice.
    manager.run_due(now=start + 60)
    assert len(json.loads(state.read_text())["agents"]) == 1


def test_scheduled_codex_model_is_rechecked_against_the_installed_cli(synced, monkeypatch):
    import workspace_agents

    manager, pr, _, state, _ = synced
    monkeypatch.setenv("FAKE_AGENT_VERSION", "codex-cli 1.0.0")
    workspace_agents._version.clear()
    cache = Path(os.environ["CODEX_HOME"]) / "models_cache.json"
    entry = {"visibility": "list", "supported_reasoning_levels": [{"effort": "high"}]}
    cache.write_text(
        json.dumps({"client_version": "1.0.0", "models": [{"slug": "fixture-codex", **entry}]})
    )
    start = time.time() + 3600
    task = scheduled(manager, pr, start, agent="codex", model="fixture-codex")
    # Meanwhile another Codex version rewrites the cache, and the installed CLI changes
    # to one whose own catalog is not known: the saved model is no longer offered.
    cache.write_text(
        json.dumps({"client_version": "1.1.0", "models": [{"slug": "fixture-codex", **entry}]})
    )
    monkeypatch.setenv("FAKE_AGENT_VERSION", "codex-cli 0.9.0")
    workspace_agents._version.clear()
    manager.run_due(now=start)
    failed = scheduled_task(manager, task["id"])
    assert failed["status"] == "failed" and "available model" in failed["message"]
    assert not json.loads(state.read_text())["agents"]


def test_scheduling_does_not_wait_for_or_block_a_running_launch(synced):
    manager, pr, _, state, _ = synced
    issue = issue_of(manager)
    manager.action({"id": issue["id"], "action": "create", "task": "Now"})
    # The issue's launch is still running; a Handle can still be scheduled for later.
    task = scheduled(manager, issue, time.time() - 30, action="handle")
    assert issue["id"] in manager.workers
    manager.run_due()
    assert scheduled_task(manager, task["id"])["status"] == "scheduled"
    assert finish(manager, issue["id"])["status"] == "complete"
    manager.run_due()
    assert scheduled_task(manager, task["id"])["status"] == "started"
    assert finish(manager, issue["id"])["status"] == "complete"
    assert len(json.loads(state.read_text())["agents"]) == 2


def test_cancelled_task_never_starts_and_cannot_be_cancelled_twice(synced):
    manager, pr, _, state, _ = synced
    task = scheduled(manager, pr, time.time() + 60)
    assert manager.cancel_scheduled(task["id"])["status"] == "cancelled"
    manager.run_due(now=time.time() + 120)
    assert scheduled_task(manager, task["id"])["status"] == "cancelled"
    assert manager.operation(pr["id"]) is None
    with pytest.raises(ValueError, match="already cancelled"):
        manager.cancel_scheduled(task["id"])
    with pytest.raises(ValueError, match="Unknown scheduled task"):
        manager.cancel_scheduled("missing")


@pytest.mark.parametrize(
    "start,action,error",
    [
        (True, "create", "start time"),
        ("soon", "create", "start time"),
        (float("nan"), "create", "start time"),
        (-120, "create", "start time"),
        (86400 * 31, "create", "start time"),
        (60, "reopen", "Only tasks that start an agent"),
    ],
)
def test_schedule_validation_has_no_side_effects(synced, start, action, error):
    manager, pr, _, state, _ = synced
    relative = isinstance(start, (int, float)) and not isinstance(start, bool)
    request = {
        "id": pr["id"],
        "action": action,
        "task": "x",
        "start_at": time.time() + start if relative else start,
    }
    with pytest.raises(ValueError, match=error):
        manager.action(request)
    assert manager.scheduled_tasks()["tasks"] == []
    assert manager.operation(pr["id"]) is None


def test_schedule_validates_the_launch_before_recording_it(synced):
    manager, pr, _, _, _ = synced
    with pytest.raises(ValueError, match="Supply a task"):
        manager.action({"id": pr["id"], "action": "create", "task": " ", "start_at": time.time()})
    with pytest.raises(ValueError, match="Choose a verified local clone"):
        scheduled(manager, pr, time.time() + 60, clone="/elsewhere")
    assert manager.scheduled_tasks()["tasks"] == []


def test_overdue_task_starts_late_but_one_missed_by_a_day_does_not(synced):
    manager, pr, _, state, _ = synced
    late = scheduled(manager, pr, time.time())
    manager.run_due(now=time.time() + 3600)
    assert scheduled_task(manager, late["id"])["status"] == "started"
    assert finish(manager, pr["id"])["status"] == "complete"
    issue = issue_of(manager)
    missed = scheduled(manager, issue, time.time())
    manager.run_due(now=time.time() + pw.SCHEDULE_MISSED_AFTER + 60)
    assert scheduled_task(manager, missed["id"])["status"] == "missed"
    assert manager.operation(issue["id"]) is None


def test_task_waits_for_its_list_and_fails_when_the_item_is_gone(local):
    manager, pr, _, state, _ = local
    task = scheduled(manager, pr, time.time())
    # A restarted dashboard has not listed PRs yet, or only from its disk cache:
    # a missing PR proves nothing.
    manager.overview.value["prs"] = []
    manager.run_due()
    assert scheduled_task(manager, task["id"])["status"] == "scheduled"
    manager.overview.value["synced_at"] = manager.started_at - 60
    manager.run_due()
    assert scheduled_task(manager, task["id"])["status"] == "scheduled"
    manager.overview.value["synced_at"] = time.time()
    manager.run_due()
    failed = scheduled_task(manager, task["id"])
    assert failed["status"] == "failed" and "no longer listed" in failed["message"]
    assert not json.loads(state.read_text())["agents"]


def test_failed_launch_and_blocking_operation_are_reported(synced):
    manager, pr, _, state, _ = synced
    first = scheduled(manager, pr, time.time())
    manager.action({"id": pr["id"], "action": "create", "task": "First"})
    assert finish(manager, pr["id"])["status"] == "complete"
    # A workspace exists now, so creating another one is refused when the task starts.
    manager.run_due()
    refused = scheduled_task(manager, first["id"])
    assert refused["status"] == "failed" and "already created" in refused["message"]
    task = scheduled(manager, issue_of(manager), time.time())
    with manager.db() as db:
        op = {"id": "stuck", "pr": "I_one", "status": "uncertain", "message": "Inspect it"}
        db.execute("INSERT OR REPLACE INTO operations VALUES (?,?)", ("I_one", json.dumps(op)))
    manager.run_due()
    blocked = scheduled_task(manager, task["id"])
    assert blocked["status"] == "failed" and "uncertain (Inspect it)" in blocked["message"]


def test_unexpected_launch_error_is_uncertain_and_never_retried(synced, monkeypatch):
    manager, pr, _, _, _ = synced
    task = scheduled(manager, pr, time.time())

    def broken(request, scheduled=False):
        raise RuntimeError("thread limit")

    monkeypatch.setattr(manager, "action", broken)
    manager.run_due()
    result = scheduled_task(manager, task["id"])
    assert result["status"] == "uncertain" and "thread limit" in result["message"]
    monkeypatch.undo()
    manager.run_due()
    assert manager.operation(pr["id"]) is None


def test_restart_marks_a_claimed_task_uncertain_and_never_starts_it(synced):
    manager, pr, _, state, _ = synced
    task = scheduled(manager, pr, time.time())
    with manager.db() as db:
        manager.save_scheduled(db, task, status="starting")
    restarted = pw.Workspaces(
        manager.home, manager.overview, lambda: [], manager.src, issues=manager.issues
    )
    restarted.run_due()
    result = scheduled_task(restarted, task["id"])
    assert result["status"] == "uncertain" and "Check the item's workspace" in result["message"]
    assert restarted.operation(pr["id"]) is None


def test_history_keeps_pending_tasks_and_recent_outcomes(synced, monkeypatch):
    manager, pr, _, _, _ = synced
    monkeypatch.setattr(pw, "SCHEDULE_HISTORY", 2)
    keep = scheduled(manager, pr, time.time() + 600)
    for _ in range(4):
        manager.cancel_scheduled(scheduled(manager, pr, time.time() + 60)["id"])
    tasks = manager.scheduled_tasks()["tasks"]
    assert [t["status"] for t in tasks] == ["scheduled", "cancelled", "cancelled"]
    assert tasks[0]["id"] == keep["id"]
    monkeypatch.setattr(pw, "SCHEDULE_PENDING_LIMIT", 1)
    with pytest.raises(ValueError, match="At most 1 tasks"):
        scheduled(manager, pr, time.time() + 60)


def test_history_keeps_tasks_whose_agent_is_still_watched(synced, monkeypatch):
    manager, pr, _, _, _ = synced
    monkeypatch.setattr(pw, "SCHEDULE_HISTORY", 1)
    start = time.time()
    watched = scheduled(manager, pr, start)
    manager.run_due(now=start)
    finish(manager, pr["id"])
    for _ in range(3):
        later = scheduled(manager, pr, time.time() + 60, action="handle")
        manager.cancel_scheduled(later["id"])
    tasks = manager.scheduled_tasks()["tasks"]
    assert watched["id"] in [t["id"] for t in tasks]
    assert [t["status"] for t in tasks].count("cancelled") == 1


def test_scheduler_thread_starts_a_due_task(synced):
    manager, pr, _, state, _ = synced
    manager.start()
    try:
        task = scheduled(manager, pr, time.time())
        for _ in range(100):
            if scheduled_task(manager, task["id"])["status"] == "started":
                break
            time.sleep(0.1)
        assert scheduled_task(manager, task["id"])["status"] == "started"
        assert finish(manager, pr["id"])["status"] == "complete"
    finally:
        manager.close()
        manager.scheduler.join(5)
    assert not manager.scheduler.is_alive()


def test_scheduled_launch_asks_its_agent_to_confirm_and_is_watched(synced, monkeypatch):
    manager, pr, _, state, jobs = synced
    start = time.time()
    task = scheduled(manager, pr, start)
    manager.run_due(now=start)
    op = finish(manager, pr["id"])
    assert op["status"] == "complete", op["log"]
    brief = json.loads(state.read_text())["agents"][0]["task"]
    assert brief.startswith("Later task") and f"\n{op['exit_marker']}\n" in brief
    listed = scheduled_task(manager, task["id"])
    assert listed["exit"] == {"state": "watching", "message": "Waiting for the agent to finish"}

    seen = []

    def step(record, launched, persist, now=None):
        seen.append(launched["id"])
        persist({**record, "state": "exiting", "target": {"pane_id": "w1:p1"}})
        # The scheduler stops after "exiting" is durable; a restart must not resend.
        raise SystemExit

    monkeypatch.setattr(pw.workspace_exit, "step", step)
    with pytest.raises(SystemExit):
        manager.exit_finished()
    assert seen == [op["id"]]
    assert scheduled_task(manager, task["id"])["exit"]["state"] == "exiting"
    # Progress never reorders the outcome list or exposes internal identity.
    assert set(scheduled_task(manager, task["id"])["exit"]) == {"state", "message"}
    assert scheduled_task(manager, task["id"])["updated_at"] == listed["updated_at"]

    restarted = pw.Workspaces(
        manager.home, manager.overview, lambda: jobs, manager.src, issues=manager.issues
    )
    after = scheduled_task(restarted, task["id"])["exit"]
    assert after["state"] == "unconfirmed" and "check its pane" in after["message"]
    restarted.exit_finished()  # Only watching tasks are stepped.
    assert seen == [op["id"]]


def test_exit_watch_saves_only_changed_progress(synced, monkeypatch):
    manager, pr, _, state, _ = synced
    start = time.time()
    task = scheduled(manager, pr, start)
    manager.run_due(now=start)
    finish(manager, pr["id"])
    writes = []
    real = manager.db

    def step(record, launched, persist, now=None):
        return {**record, "state": "exited", "message": "Exited"}

    monkeypatch.setattr(pw.workspace_exit, "step", step)
    manager.exit_finished()
    assert scheduled_task(manager, task["id"])["exit"] == {"state": "exited", "message": "Exited"}

    def counting():
        writes.append(1)
        return real()

    monkeypatch.setattr(manager, "db", counting)
    manager.exit_finished()  # Nothing is watching any more: one read, no write.
    assert len(writes) == 1


def test_exit_claim_fails_when_another_dashboard_changed_the_watch(synced, monkeypatch):
    manager, pr, _, state, _ = synced
    start = time.time()
    task = scheduled(manager, pr, start)
    manager.run_due(now=start)
    finish(manager, pr["id"])
    claims = []

    def step(record, launched, persist, now=None):
        # Another dashboard on this home advances the same watch first.
        with manager.db() as db:
            row = json.loads(
                db.execute("SELECT data FROM scheduled WHERE id=?", (task["id"],)).fetchone()[0]
            )
            row["exit"]["message"] = "Other dashboard"
            db.execute("UPDATE scheduled SET data=? WHERE id=?", (json.dumps(row), task["id"]))
        try:
            persist({**record, "state": "exiting"})
        except pw.workspace_exit.Leave:
            claims.append("refused")
            raise
        claims.append("claimed")
        return record

    monkeypatch.setattr(pw.workspace_exit, "step", step)
    manager.exit_finished()  # The refusal stays inside this watch.
    assert claims == ["refused"]
    assert scheduled_task(manager, task["id"])["exit"] == {
        "state": "watching",
        "message": "Other dashboard",
    }
    # A pruned row is refused the same way, before any key could be sent.
    with manager.db() as db:
        db.execute("DELETE FROM scheduled WHERE id=?", (task["id"],))
    monkeypatch.setattr(
        pw.workspace_exit,
        "step",
        lambda record, launched, persist, now=None: persist({**record, "state": "exiting"}),
    )
    with pytest.raises(pw.workspace_exit.Leave):
        manager.exit_one({"id": task["id"], "exit": pw.workspace_exit.watching()})


def test_immediate_launch_is_never_exited(synced):
    manager, pr, _, state, _ = synced
    manager.action({"id": pr["id"], "action": "create", "task": "Now task"})
    op = finish(manager, pr["id"])
    assert op["status"] == "complete", op["log"]
    assert "exit_marker" not in op
    assert "babysit-done" not in json.loads(state.read_text())["agents"][0]["task"]


def test_http_lists_and_cancels_scheduled_tasks(synced):
    manager, pr, _, state, _ = synced
    with dashboard.DashboardServer(
        manager.home, 0, overview=manager.overview, workspaces=manager
    ) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:

            def request(method, path, action=None, body=None):
                conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=15)
                conn.request(
                    method,
                    path,
                    json.dumps(body) if body else None,
                    {"Content-Type": "application/json", "X-Babysit-Action": action or ""},
                )
                response = conn.getresponse()
                result = response.status, json.loads(response.read())
                conn.close()
                return result

            status, value = request(
                "POST",
                "/api/workspace-action",
                "workspace-action",
                {"id": pr["id"], "action": "create", "task": "Later", "start_at": time.time() + 60},
            )
            assert status == 200 and value["scheduled"]["status"] == "scheduled"
            key = value["scheduled"]["id"]
            status, listing = request("GET", "/api/scheduled-tasks")
            assert status == 200 and listing["enabled"]
            assert [t["id"] for t in listing["tasks"]] == [key]
            # Cancellation needs the dashboard's own action header on its own path.
            assert request("POST", "/api/schedule-cancel", None, {"id": key})[0] == 403
            assert request("POST", "/api/schedule-cancel", "cancel", {"id": key})[0] == 404
            status, value = request("POST", "/api/schedule-cancel", "schedule-cancel", {"id": key})
            assert status == 200 and value["task"]["status"] == "cancelled"
            status, value = request("POST", "/api/schedule-cancel", "schedule-cancel", {"id": key})
            assert status == 400 and "already cancelled" in value["error"]
            assert not json.loads(state.read_text())["agents"]
        finally:
            server.shutdown()
            thread.join()
    with dashboard.DashboardServer(manager.home, 0) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=15)
            conn.request("GET", "/api/scheduled-tasks")
            assert json.loads(conn.getresponse().read()) == {"enabled": False, "tasks": []}
            conn.close()
        finally:
            server.shutdown()
            thread.join()


def test_scheduled_tasks_join_prompt_history_when_they_start(synced):
    manager, pr, _, _, _ = synced
    edited = scheduled(manager, pr, time.time(), task="Edited later task")
    prefill = scheduled(manager, issue_of(manager), time.time(), action="handle", prefilled=True)
    assert manager.prompts()["prompts"] == []
    manager.run_due()
    assert finish(manager, pr["id"])["status"] == "complete"
    assert finish(manager, "I_one")["status"] == "complete"
    assert scheduled_task(manager, edited["id"])["status"] == "started"
    assert scheduled_task(manager, prefill["id"])["request"]["prefilled"] is True
    assert [p["text"] for p in manager.prompts()["prompts"]] == ["Edited later task"]


def second_issue(manager):
    manager.issues.value["issues"].append(
        {
            "id": "I_two",
            "repo": "base/repo",
            "number": 13,
            "title": "Slow exit",
            "url": "https://github.com/base/repo/issues/13",
            "linked_prs": [],
        }
    )


def test_batch_handles_each_issue_in_its_own_workspace_at_staggered_times(synced):
    manager, _, _, state, _ = synced
    second_issue(manager)
    start = time.time() + 60
    value = manager.batch(
        {
            "items": [{"id": "I_one"}, {"id": "I_two"}],
            "agent": "claude",
            "task": "Resolve {url} carefully",
            "start_at": start,
            "interval": 1800,
        }
    )
    tasks = [r["scheduled"] for r in value["results"]]
    assert [r["id"] for r in value["results"]] == ["I_one", "I_two"]
    assert [t["start_at"] for t in tasks] == [start, start + 1800]
    assert [t["request"]["task"] for t in tasks] == [
        "Resolve https://github.com/base/repo/issues/12 carefully",
        "Resolve https://github.com/base/repo/issues/13 carefully",
    ]
    assert {t["request"]["action"] for t in tasks} == {"handle"}
    assert {t["request"]["clone"] for t in tasks} == {str(manager.src / "repo")}
    # The task is remembered once, as typed, rather than once per issue.
    assert [p["text"] for p in manager.prompts()["prompts"]] == ["Resolve {url} carefully"]

    manager.run_due(now=start)
    assert scheduled_task(manager, tasks[0]["id"])["status"] == "started"
    assert scheduled_task(manager, tasks[1]["id"])["status"] == "scheduled"
    assert finish(manager, "I_one")["status"] == "complete"
    manager.run_due(now=start + 1800)
    assert scheduled_task(manager, tasks[1]["id"])["status"] == "started"
    paths = {finish(manager, key)["path"] for key in ("I_one", "I_two")}
    assert paths == {
        str(manager.src / "worktrees/repo/issue-12-crash-on-start"),
        str(manager.src / "worktrees/repo/issue-13-slow-exit"),
    }
    agents = json.loads(state.read_text())["agents"]
    assert [a["agent"] for a in agents] == ["claude", "claude"]
    assert [p["text"] for p in manager.prompts()["prompts"]] == ["Resolve {url} carefully"]


def test_scheduled_and_batched_launches_keep_the_docker_opt_in(synced):
    manager, pr, _, state, _ = synced
    second_issue(manager)
    start = time.time() + 60
    task = scheduled(manager, pr, start, docker=True)
    assert task["request"]["docker"] is True
    value = manager.batch(
        {"items": [{"id": "I_one"}, {"id": "I_two"}], "task": "Fix {url}", "docker": True}
    )
    batched = [r["scheduled"] for r in value["results"]]
    assert [t["request"]["docker"] for t in batched] == [True, True]
    plain = manager.batch({"items": [{"id": "I_two"}], "task": "Fix {url}", "start_at": start})
    assert "docker" not in plain["results"][0]["scheduled"]["request"]
    manager.run_due(now=start)
    for key in (pr["id"], "I_one", "I_two"):
        op = finish(manager, key)
        assert op["status"] == "complete", op["log"]
        assert op["docker"] is True
    agents = json.loads(state.read_text())["agents"]
    assert {a.get("safe_enable") for a in agents} == {"docker"}


def test_batch_handles_pull_requests_too(synced):
    manager, pr, _, state, _ = synced
    value = manager.batch(
        {"items": [{"id": pr["id"]}, {"id": "I_one"}], "task": "Review {url}", "interval": 600}
    )
    tasks = [r["scheduled"] for r in value["results"]]
    assert [t["subject"]["kind"] for t in tasks] == ["pr", "issue"]
    assert tasks[0]["request"]["task"] == f"Review {pr['url']}"
    assert tasks[0]["request"]["clone"] == str(manager.src / "repo")
    manager.run_due(now=tasks[0]["start_at"])
    assert finish(manager, pr["id"])["status"] == "complete"
    agents = json.loads(state.read_text())["agents"]
    assert len(agents) == 1 and agents[0]["task"].startswith(f"Review {pr['url']}")


def test_batch_starts_now_by_default_and_reports_items_it_cannot_schedule(synced):
    manager, pr, _, _, _ = synced
    second_issue(manager)
    before = time.time()
    value = manager.batch(
        {
            "items": [{"id": "I_one"}, {"id": "I_gone"}, {"id": "I_two"}],
            "task": "Fix it",
            "interval": 600,
        }
    )
    first, gone, last = value["results"]
    assert before <= first["scheduled"]["start_at"] <= time.time()
    assert gone == {"id": "I_gone", "error": "Unknown PR or issue or watch; refresh the dashboard"}
    assert last["scheduled"]["start_at"] == first["scheduled"]["start_at"] + 1200
    # Each item is validated like a single Handle.
    error = manager.batch({"items": [{"id": "I_one", "clone": "/elsewhere"}], "task": "Fix"})
    assert error["results"] == [{"id": "I_one", "error": "Choose a verified local clone"}]


@pytest.mark.parametrize(
    "request_,error",
    [
        ({"items": []}, "Invalid batch"),
        ({"items": [{"id": "I_one"}, {"id": "I_one"}]}, "Invalid batch"),
        ({"items": [{"id": "I_one", "path": "/tmp"}]}, "Invalid batch"),
        ({"items": [{"id": "I_one", "clone": ""}]}, "Invalid batch"),
        ({"items": [{"id": "I_one"}], "action": "create"}, "Invalid batch"),
        ({"items": [{"id": "I_one"}], "interval": 86401}, "at most a day"),
        ({"items": [{"id": "I_one"}], "interval": -1}, "at most a day"),
        ({"items": [{"id": "I_one"}], "interval": True}, "at most a day"),
        ({"items": [{"id": "I_one"}], "start_at": "soon"}, "within the next 30 days"),
        ({"items": [{"id": "I_one"}], "start_at": float("nan")}, "within the next 30 days"),
        ({"items": [{"id": "I_one"}], "interval": float("inf")}, "at most a day"),
        ({"items": [{"id": "I_one"}], "task": 5}, "Invalid batch"),
        ({"items": [{"id": "I_one"}], "prefilled": "false"}, "Invalid batch"),
        ({"items": [{"id": "I_one"}], "docker": "true"}, "Invalid batch"),
        ({"items": [{"id": f"I_{n}"} for n in range(101)]}, "At most 100 tasks"),
        ({"items": [{"id": "I_one"}], "start_at": 0}, "within the next 30 days"),
        ({"items": [{"id": "I_one"}], "task": "  "}, "Supply a task"),
        ({"items": [{"id": "I_one"}], "agent": "gpt"}, "Select Codex or Claude"),
        ({"items": [{"id": "I_one"}], "model": "fixture-codex", "effort": "max"}, "effort"),
    ],
)
def test_batch_validation_has_no_side_effects(synced, request_, error):
    manager, _, _, state, _ = synced
    with pytest.raises(ValueError, match=error):
        manager.batch({"task": "Fix", **request_})
    assert manager.scheduled_tasks()["tasks"] == []
    assert manager.prompts()["prompts"] == []


def test_batch_staggered_past_the_horizon_schedules_only_what_fits(synced):
    manager, _, _, _, _ = synced
    second_issue(manager)
    value = manager.batch(
        {
            "items": [{"id": "I_one"}, {"id": "I_two"}],
            "task": "Fix",
            "start_at": time.time() + pw.SCHEDULE_HORIZON - 3600,
            "interval": 7200,
        }
    )
    assert "scheduled" in value["results"][0]
    assert "within the next 30 days" in value["results"][1]["error"]


def test_batch_items_without_a_clone_clone_once_and_share_it(synced):
    manager, _, git, state, _ = synced
    second_issue(manager)
    git("remote", "set-url", "origin", "https://github.com/unrelated/repo.git")
    destination = str(manager.src / "base--repo")
    release = threading.Event()
    perform = manager.perform

    def held(*args):
        release.wait(20)
        return perform(*args)

    manager.perform = held
    value = manager.batch(
        {
            "items": [
                {"id": "I_one", "destination": destination},
                {"id": "I_two", "destination": destination},
            ],
            "task": "Fix",
        }
    )
    first, second = (r["scheduled"] for r in value["results"])
    manager.run_due()
    # The second waits while the first clones the repository they share.
    assert scheduled_task(manager, first["id"])["status"] == "started"
    assert scheduled_task(manager, second["id"])["status"] == "scheduled"
    release.set()
    assert finish(manager, "I_one")["status"] == "complete"
    manager.run_due()
    assert scheduled_task(manager, second["id"])["status"] == "started"
    op = finish(manager, "I_two")
    assert op["status"] == "complete", op["log"]
    assert op["clone"] == destination
    assert not (manager.src / "base--repo-2").exists()
    assert len(json.loads(state.read_text())["agents"]) == 2


def test_http_dispatches_batches(synced):
    manager, _, _, _, _ = synced
    with dashboard.DashboardServer(
        manager.home, 0, overview=manager.overview, workspaces=manager, issues=manager.issues
    ) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=15)
            body = {"items": [{"id": "I_one"}], "task": "Fix", "start_at": time.time() + 60}
            conn.request(
                "POST",
                "/api/workspace-batch",
                json.dumps(body),
                {"Content-Type": "application/json", "X-Babysit-Action": "workspace-batch"},
            )
            response = conn.getresponse()
            value = json.loads(response.read())
            conn.close()
            assert response.status == 200
            assert value["results"][0]["scheduled"]["status"] == "scheduled"
        finally:
            server.shutdown()
            thread.join()


def test_batch_refuses_a_second_repository_cloning_into_the_same_destination(synced):
    manager, _, git, _, _ = synced
    second_issue(manager)
    manager.issues.value["issues"][1].update(
        repo="other/repo", url="https://github.com/other/repo/issues/13"
    )
    git("remote", "set-url", "origin", "https://github.com/unrelated/repo.git")
    (manager.src / "repo").rename(manager.src / "unrelated")
    destination = str(manager.src / "repo")
    value = manager.batch(
        {
            "items": [
                {"id": "I_one", "destination": destination},
                {"id": "I_two", "destination": destination},
            ],
            "task": "Fix",
        }
    )
    first, second = value["results"]
    assert "scheduled" in first
    assert second["error"].startswith(f"base/repo also clones into {destination}")
