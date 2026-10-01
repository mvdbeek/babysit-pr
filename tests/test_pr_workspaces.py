"""Temporary repositories and fake herdr/GitHub/agents; never touch live workspaces."""

import http.client
import json
import os
import shutil
import subprocess
import sys
import threading
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
