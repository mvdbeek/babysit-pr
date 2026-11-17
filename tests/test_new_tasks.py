"""Tasks started from scratch: temporary clones, fake gh/herdr/agents, no live workspaces."""

import json

import pr_workspaces as pw
import pytest
from test_pr_workspaces import (  # noqa: F401 (fixture)
    finish,
    helper_calls,
    local,
    make_checkout,
    space,
)


def request(manager, **changes):
    return {
        "repo": "base/repo",
        "clone": str((manager.src / "repo").resolve()),
        "agent": "codex",
        "task": "Explore the parser",
        **changes,
    }


def test_local_repositories_list_recent_clones_first_and_upstream_first(local):  # noqa: F811
    manager, _, git, _, _ = local
    clone = (manager.src / "repo").resolve()
    listed = manager.local_repositories()
    assert listed == {
        "repos": [
            {
                "repo": "base/repo",
                "clone": str(clone),
                "remote": "origin",
                "active": (clone / ".git/logs/HEAD").stat().st_mtime,
            }
        ],
        "idle": 0,
    }
    git("remote", "add", "upstream", "git@github.com:Up/Repo.git")
    git("config", "remote.notgithub.url", "https://example.org/x/y.git")
    git("remote", "add", "contributor", "https://github.com/someone/repo.git")
    assert [r["repo"] for r in manager.local_repositories()["repos"]] == ["up/repo", "base/repo"]
    # A clone nobody committed or checked out in for months is left out unless asked for,
    # however recently a tool fetched it or read its status.
    old = manager.src / "old"
    git("clone", "-q", str(clone), str(old))
    git("remote", "rename", "origin", "mine", cwd=old)
    git("remote", "set-url", "mine", "https://github.com/old/repo.git", cwd=old)
    git("status", cwd=old)
    stale = pw.time.time() - pw.RECENT_CLONE_SECONDS - 86400
    for log in [old / ".git/logs/HEAD", *(old / ".git/logs/refs").rglob("*")]:
        pw.os.utime(log, (stale, stale))
    listed = manager.local_repositories()
    assert [r["repo"] for r in listed["repos"]] == ["up/repo", "base/repo"]
    assert listed["idle"] == 1
    everything = manager.local_repositories(everything=True)["repos"]
    assert [r["repo"] for r in everything] == ["up/repo", "base/repo", "old/repo"]
    # A worktree's checkout counts as activity in its clone.
    git("worktree", "add", "-q", str(manager.src / "worktrees/fresh"), "-b", "fresh", cwd=old)
    assert manager.local_repositories()["idle"] == 0
    # So does an earlier launch from it, however long ago.
    for log in (old / ".git").rglob("logs/HEAD"):
        pw.os.utime(log, (stale, stale))
    assert manager.local_repositories()["idle"] == 1
    with manager.db() as db:
        db.execute("INSERT INTO clones VALUES (?,?)", ("old/repo", str(old.resolve())))
    assert [r["repo"] for r in manager.local_repositories()["repos"]][-1] == "old/repo"


def test_scratch_task_branches_from_its_base_and_starts_the_agent(local):  # noqa: F811
    manager, _, git, state, _ = local
    calls = helper_calls(manager)
    value = manager.new_task(request(manager, name="try-parser", base="feature"))
    key = value["operation"]["pr"]
    assert key.startswith(pw.NEW_PREFIX)
    op = finish(manager, key)
    assert op["status"] == "complete", op
    assert op["branch"] == "try-parser"
    assert op["subject"] == {
        "kind": "scratch",
        "repo": "base/repo",
        "branch": "try-parser",
        "base": "feature",
    }
    assert git("rev-parse", "HEAD", cwd=op["path"]) == git("rev-parse", "feature")
    prompt = manager.home / "workspace-prompts" / op["id"]
    clone = str((manager.src / "repo").resolve())
    assert calls == [
        [
            "wt",
            "--codex",
            "--no-focus",
            "--name",
            "try-parser",
            "--repo-path",
            clone,
            "--github-repo",
            "base/repo",
            "--worktree-root",
            str(manager.src / "worktrees/repo"),
            "--prompt-file",
            str(prompt),
            "feature",
        ]
    ]
    assert prompt.read_text() == "Explore the parser\n"
    assert json.loads(state.read_text())["agents"][0]["task"] == "Explore the parser"
    listed = manager.snapshot()["new"][key]["operation"]
    assert listed["status"] == "complete" and listed["result"]["path"] == op["path"]
    assert "Explore the parser" in [p["text"] for p in manager.prompts()["prompts"]]
    # The same name again gets its own branch rather than reusing the first one.
    second = finish(
        manager, manager.new_task(request(manager, name="try-parser"))["operation"]["pr"]
    )
    assert second["status"] == "complete", second
    assert second["branch"] == "try-parser-2"
    assert calls[-1][-2:] == [
        "--prompt-file",
        str(manager.home / "workspace-prompts" / second["id"]),
    ]


def test_new_task_files_its_issue_then_works_on_it(local):  # noqa: F811
    manager, _, git, state, _ = local
    calls = helper_calls(manager)
    value = manager.new_task(request(manager, issue_title="Crash on start!", task="It crashes"))
    key = value["operation"]["pr"]
    op = finish(manager, key)
    assert op["status"] == "complete", op
    url = "https://github.com/base/repo/issues/12"
    assert op["subject"] == {
        "kind": "issue",
        "repo": "base/repo",
        "title": "Crash on start!",
        "number": 12,
        "url": url,
    }
    gh = json.loads(state.read_text())["gh"]
    assert gh[0] == [
        "issue",
        "create",
        "--repo",
        "base/repo",
        "--title",
        "Crash on start!",
        "--body",
        "It crashes",
    ]
    assert op["branch"] == "issue-12-crash-on-start"
    assert calls[0][0] == "wti" and calls[0][-1] == url
    task = json.loads(state.read_text())["agents"][0]["task"]
    assert task == f"It crashes\n\nIssue: {url}"
    # Once the issue list carries it, the issue row finds the new checkout.
    manager.scan()
    assert [m["path"] for m in manager.snapshot()["issues"]["I_one"]["matches"]] == [op["path"]]


def test_a_failed_issue_starts_nothing(local, monkeypatch):  # noqa: F811
    manager, *_ = local
    calls = helper_calls(manager)
    monkeypatch.setenv("FAKE_GH_FAIL", "HTTP 403: Resource not accessible")
    op = finish(manager, manager.new_task(request(manager, issue_title="Crash"))["operation"]["pr"])
    assert op["status"] == "failed"
    assert "Resource not accessible" in op["message"]
    assert calls == [] and op["path"] is None


def test_attached_files_reach_the_agent_but_not_the_filed_issue(local):  # noqa: F811
    manager, _, _, state, _ = local
    files = "Attached files (read them from these paths):\n- /inbox/2026-10-05-0123abcd-a.png"
    value = manager.new_task(
        request(manager, issue_title="Crash", task="It crashes", task_files=files)
    )
    assert finish(manager, value["operation"]["pr"])["status"] == "complete"
    data = json.loads(state.read_text())
    assert data["gh"][0][-2:] == ["--body", "It crashes"]
    assert data["agents"][0]["task"].startswith(f"It crashes\n\n{files}\n\nIssue: ")


@pytest.mark.parametrize(
    ("changes", "error"),
    [
        ({"repo": "base"}, "Choose a repository"),
        ({"repo": "other/repo"}, "local clone of other/repo"),
        ({"clone": "/elsewhere"}, "local clone of base/repo"),
        ({"name": ""}, "Name the new branch"),
        ({"name": "-x"}, "Name the new branch"),
        ({"name": "two words"}, "Name the new branch"),
        ({"name": "a..b"}, "Name the new branch"),
        ({"name": "ok", "base": "-x"}, "base branch"),
        ({"name": "ok", "base": "bad..ref"}, "base branch"),
        ({"name": "ok", "base": "@{-1}"}, "base branch"),
        ({"name": "ok", "task": " "}, "Supply a task"),
        ({"name": "ok", "agent": "other"}, "Select Codex or Claude"),
        ({"issue_title": "x" * 257}, "issue title"),
        ({"issue_title": "line\nbreak"}, "issue title"),
        ({"name": "ok", "extra": "x"}, "Invalid new task parameters"),
        ({"name": 1}, "Invalid new task parameters"),
    ],
)
def test_new_task_validation(local, changes, error):  # noqa: F811
    manager, *_ = local
    with pytest.raises(ValueError, match=error):
        manager.new_task(request(manager, **changes))
    assert manager.snapshot()["new"] == {}


def test_an_interrupted_new_task_is_reported_uncertain(local):  # noqa: F811
    manager, *_ = local
    op = {
        "id": "op",
        "pr": "new:gone",
        "action": "new",
        "status": "running",
        "created_at": pw.time.time(),
        "log": "",
    }
    with manager.db() as db:
        db.execute("INSERT INTO operations VALUES (?,?)", (op["pr"], json.dumps(op)))
    listed = manager.snapshot()["new"]["new:gone"]["operation"]
    assert listed["status"] == "uncertain"
    assert "Check for its issue and workspace" in listed["message"]


def test_a_filed_issue_is_never_filed_again_after_a_failure(local, monkeypatch):  # noqa: F811
    manager, _, _, state, _ = local
    monkeypatch.setenv("FAKE_GH_ISSUE", "{}")  # wti cannot read the issue back.
    op = finish(manager, manager.new_task(request(manager, issue_title="Crash"))["operation"]["pr"])
    assert op["status"] == "failed"
    assert op["subject"]["url"] == "https://github.com/base/repo/issues/12"
    assert op["message"].endswith(
        "The issue https://github.com/base/repo/issues/12 was filed; use Handle on it from the Issues tab."
    )
    # A link GitHub reports elsewhere still means the issue exists: nothing more starts.
    calls = helper_calls(manager)
    monkeypatch.setenv("FAKE_GH_CREATED", "https://github.com/moved/repo/issues/3")
    op = finish(manager, manager.new_task(request(manager, issue_title="Crash"))["operation"]["pr"])
    assert op["status"] == "uncertain" and "was filed" in op["message"]
    assert calls == [] and "url" not in op["subject"]
    assert [a[:2] for a in json.loads(state.read_text())["gh"]].count(["issue", "create"]) == 2


def test_a_finished_task_is_not_reported_uncertain_from_a_stale_read(local):  # noqa: F811
    manager, *_ = local
    now = pw.time.time()
    done = {"id": "op", "pr": "new:done", "status": "complete", "created_at": now, "log": ""}
    expired = {**done, "pr": "new:old", "created_at": now - pw.NEW_LISTED - 1}
    with manager.db() as db:
        for op in (done, expired):
            db.execute("INSERT INTO operations VALUES (?,?)", (op["pr"], json.dumps(op)))
    # The snapshot's state was read while the worker was still running.
    stale = {"operations": {"new:done": {**done, "status": "running"}, "new:old": expired}}
    assert manager.new_operations(stale)["new:done"]["operation"]["status"] == "complete"
    assert manager.operation("new:done")["status"] == "complete"
    assert manager.operation("new:old") is None


def test_new_task_can_let_the_agent_use_docker(local):  # noqa: F811
    manager, _, _, state, _ = local
    calls = helper_calls(manager)
    value = manager.new_task(request(manager, name="containers", docker=True))
    op = finish(manager, value["operation"]["pr"])
    assert op["status"] == "complete", op
    assert op["docker"] is True
    assert calls[0][:3] == ["wt", "--codex", "--docker"]
    assert json.loads(state.read_text())["agents"][0]["safe_enable"] == "docker"
    off = finish(manager, manager.new_task(request(manager, name="plain"))["operation"]["pr"])
    assert off["docker"] is False and "--docker" not in calls[1]


def test_new_task_passes_claudes_auto_permission_mode_on(local):  # noqa: F811
    manager, _, _, _, _ = local
    calls = helper_calls(manager)
    value = manager.new_task(
        request(manager, name="careful", agent="claude", permission_mode="auto")
    )
    # The fake tools have no `safe` function for the typed line to call, so only the
    # launch is checked here.
    op = finish(manager, value["operation"]["pr"])
    assert op["permission_mode"] == "auto"
    assert calls[0][:4] == ["wt", "--claude", "--permission-mode", "auto"]
    plain = finish(manager, manager.new_task(request(manager, name="plain"))["operation"]["pr"])
    assert plain["permission_mode"] is None and "--permission-mode" not in calls[1]
    for change in ({"permission_mode": "auto"}, {"agent": "claude", "permission_mode": "plan"}):
        with pytest.raises(ValueError, match="Only Claude takes a permission mode, and only auto"):
            manager.new_task(request(manager, name="nope", **change))


@pytest.mark.parametrize("flag", ["docker", "exit_when_done"])
@pytest.mark.parametrize("value", ["true", 1, None])
def test_new_task_options_must_be_flags(local, flag, value):  # noqa: F811
    manager, _, _, _, _ = local
    with pytest.raises(ValueError, match="Invalid new task"):
        manager.new_task(request(manager, name="containers", **{flag: value}))


def test_new_task_can_quit_its_agent_when_done(local):  # noqa: F811
    manager, _, _, _, _ = local
    calls = helper_calls(manager)
    value = manager.new_task(request(manager, name="quits", exit_when_done=True))
    op = finish(manager, value["operation"]["pr"])
    assert op["status"] == "complete", op
    assert op["exit_when_done"] is True and "--exit-when-done" in calls[0]
    with manager.db() as db:
        assert db.execute("SELECT count(*) FROM exit_watches").fetchone() == (1,)
    off = finish(manager, manager.new_task(request(manager, name="stays"))["operation"]["pr"])
    assert off["exit_when_done"] is False and "--exit-when-done" not in calls[1]


def other_clone(manager, git, name="other"):
    """A second recent clone; with no origin remote, wt branches it from its local main."""
    other = manager.src / name
    other.mkdir()
    git("init", "-b", "main", cwd=other)
    git(
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.test",
        "commit",
        "--allow-empty",
        "-m",
        "other",
        cwd=other,
    )
    git("remote", "add", "upstream", f"https://github.com/base/{name}.git", cwd=other)
    return other.resolve()


def test_new_task_can_also_work_in_other_clones(local, monkeypatch):  # noqa: F811
    manager, _, git, state, _ = local
    calls = helper_calls(manager)
    other = other_clone(manager, git)
    # The name is taken in the other clone, so both checkouts get the next free one.
    git("branch", "shared", cwd=other)
    monkeypatch.setenv("SAFEHOUSE_ADD_DIRS", "/granted")
    value = manager.new_task(request(manager, name="shared", also=[str(other)]))
    op = finish(manager, value["operation"]["pr"])
    assert op["status"] == "complete", op
    assert op["branch"] == "shared-2" and op["also"] == [str(other)]
    also = manager.src / "worktrees/other/shared-2"
    assert op["also_paths"] == [str(also)]
    assert calls[0][:4] == ["wt", "--codex", "--with", str(other)]
    assert git("symbolic-ref", "--short", "HEAD", cwd=also) == "shared-2"
    agent = json.loads(state.read_text())["agents"][0]
    assert agent["safehouse_add_dirs"] == f"/granted:{also.resolve()}:{other}/.git"
    assert f"you can write and commit there:\n- {also.resolve()}" in agent["task"]


def test_new_task_names_a_base_for_another_clone(local):  # noqa: F811
    manager, _, git, _, _ = local
    calls = helper_calls(manager)
    other = other_clone(manager, git)
    git("branch", "dev", cwd=other)
    git(
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.test",
        "commit",
        "--allow-empty",
        "-m",
        "main only",
        cwd=other,
    )
    value = manager.new_task(
        request(manager, name="based", also=[str(other)], also_bases={str(other): " dev "})
    )
    op = finish(manager, value["operation"]["pr"])
    assert op["status"] == "complete", op
    assert op["also_bases"] == {str(other): "dev"}
    assert calls[0][:4] == ["wt", "--codex", "--with", f"{other}:dev"]
    also = manager.src / "worktrees/other/based"
    assert git("rev-parse", "HEAD", cwd=also) == git("rev-parse", "dev", cwd=other)
    for bases, error in [
        ({str(other): "a..b"}, "base branch for other is not a valid"),
        ({str(other): "@{-1}"}, "base branch for other is not a valid"),
        ({"/elsewhere": "dev"}, "only for a chosen other repository"),
        ({str(other): 1}, "Invalid new task"),
        ([], "Invalid new task"),
    ]:
        with pytest.raises(ValueError, match=error):
            manager.new_task(request(manager, name="x", also=[str(other)], also_bases=bases))


@pytest.mark.parametrize(
    "also",
    ["/x", [1], ["main"], ["/elsewhere"], ["other", "other"], ["named-repo"], ["other"] * 5],
)
def test_new_task_other_clones_must_be_distinct_listed_clones(local, also):  # noqa: F811
    manager, _, git, _, _ = local
    clones = {"main": str((manager.src / "repo").resolve())}
    clones["other"] = str(other_clone(manager, git))
    # Checkouts of base/repo go in worktrees/repo, so a clone named "Repo" cannot join.
    (manager.src / "repo").rename(manager.src / "main-clone")
    clones["main"] = str((manager.src / "main-clone").resolve())
    clones["named-repo"] = str(other_clone(manager, git, "Repo"))
    if isinstance(also, list):
        also = [clones.get(a, a) if isinstance(a, str) else a for a in also]
    with pytest.raises(ValueError, match="Invalid new task|other repositor|share the task"):
        manager.new_task(request(manager, name="x", clone=clones["main"], also=also))


def uncloned(manager, git):
    """``base/repo`` with no clone here: the fixture's clone tracks another repository,
    so the first destination is taken and the second one is free."""
    git("remote", "set-url", "origin", "https://github.com/unrelated/repo.git")
    return str(manager.src / "base--repo")


def cloning(manager, destination, **changes):
    asked = request(manager, **{"destination": destination, "name": "try-parser", **changes})
    del asked["clone"]
    return asked


def clones_made(state):
    return [
        call
        for call in json.loads(state.read_text()).get("gh", [])
        if call[:2] == ["repo", "clone"]
    ]


def test_a_repository_named_for_a_new_task_lists_its_clones_or_its_destination(local):  # noqa: F811
    manager, _, git, _, _ = local
    clone = str((manager.src / "repo").resolve())
    found = manager.repository("Base/Repo")
    assert found["destination"] is None
    assert [(r["repo"], r["clone"]) for r in found["clones"]] == [("base/repo", clone)]
    destination = uncloned(manager, git)
    assert manager.repository("base/repo") == {
        "repo": "base/repo",
        "clones": [],
        "destination": destination,
    }
    # Nothing under src is in the way of a repository by another name.
    assert manager.repository("new/thing")["destination"] == str(manager.src / "thing")
    (manager.src / "base--repo").mkdir()
    assert manager.repository("base/repo")["destination"] is None
    for name in ("nope", "a/..", "-a/b", "a/b/c"):
        with pytest.raises(ValueError, match="Name a GitHub repository as owner/name"):
            manager.repository(name)


def test_new_task_clones_a_repository_that_has_no_clone_here(local):  # noqa: F811
    manager, _, git, state, _ = local
    calls = helper_calls(manager)
    destination = uncloned(manager, git)
    with pytest.raises(ValueError, match="Clone destination changed"):
        manager.new_task(cloning(manager, str(manager.src / "repo")))
    with pytest.raises(ValueError, match="Invalid new task parameters"):
        manager.new_task({**cloning(manager, destination), "clone": destination})
    assert not clones_made(state)
    value = manager.new_task(cloning(manager, destination, base="feature"))
    assert value["operation"]["fresh_clone"] is True
    op = finish(manager, value["operation"]["pr"])
    assert op["status"] == "complete", op
    assert op["clone"] == destination and op["branch"] == "try-parser"
    assert clones_made(state) == [["repo", "clone", "base/repo", destination]]
    assert calls[0][calls[0].index("--repo-path") + 1] == destination
    assert calls[0][calls[0].index("--github-repo") + 1] == "base/repo"
    # Its worktrees go where every other task's for that repository do.
    assert op["path"] == str(manager.src / "worktrees/repo/try-parser")
    assert git("rev-parse", "HEAD", cwd=op["path"]) == git("rev-parse", "feature")
    # A dialog opened before the clone existed starts in it rather than cloning again.
    second = manager.new_task(cloning(manager, destination))["operation"]
    assert "fresh_clone" not in second
    second = finish(manager, second["pr"])
    assert second["status"] == "complete", second
    assert second["clone"] == destination and second["branch"] == "try-parser-2"
    assert len(clones_made(state)) == 1
    # Asked for anywhere else, the clone that now exists is named instead: also by
    # another spelling of its own path.
    for other in (str(manager.src / "elsewhere"), str(manager.src / "worktrees/../base--repo")):
        with pytest.raises(ValueError, match=f"already has a local clone at {destination}"):
            manager.new_task(cloning(manager, other))


def test_new_tasks_started_together_share_one_clone(local):  # noqa: F811
    manager, _, git, state, _ = local
    destination = uncloned(manager, git)
    first = manager.new_task(cloning(manager, destination))["operation"]
    # While the first clones: the same destination, never a second one beside it.
    assert manager.repository("base/repo")["destination"] in (None, destination)
    second = manager.new_task(cloning(manager, destination, name="other"))["operation"]
    done = [finish(manager, op["pr"]) for op in (first, second)]
    assert [op["status"] for op in done] == ["complete", "complete"], done
    assert {op["clone"] for op in done} == {destination}
    assert [op["branch"] for op in done] == ["try-parser", "other"]
    assert len(clones_made(state)) == 1


def test_a_failed_clone_fails_the_new_task_and_leaves_nothing(local, monkeypatch):  # noqa: F811
    manager, _, git, state, _ = local
    destination = uncloned(manager, git)
    monkeypatch.setenv("FAKE_GH_FAIL", "GraphQL: Could not resolve to a Repository")
    op = finish(manager, manager.new_task(cloning(manager, destination))["operation"]["pr"])
    assert op["status"] == "failed" and "Could not resolve to a Repository" in op["message"]
    assert not pw.os.path.lexists(destination)
    assert not json.loads(state.read_text())["agents"]


def in_directory(path, **changes):
    return {"directory": str(path), "agent": "codex", "task": "Tidy the notes", **changes}


def herdr_calls(state, *verb):
    return [c for c in json.loads(state.read_text())["calls"] if c[: len(verb)] == list(verb)]


def test_a_task_in_a_directory_starts_its_agent_there_as_it_is(local):  # noqa: F811
    manager, _, git, state, _ = local
    calls = helper_calls(manager)
    clone = (manager.src / "repo").resolve()
    head = git("rev-parse", "HEAD")
    value = manager.new_task(in_directory(clone))
    key = value["operation"]["pr"]
    assert key.startswith(pw.NEW_PREFIX)
    op = finish(manager, key)
    assert op["status"] == "complete", op
    assert op["subject"] == {"kind": "directory", "path": str(clone), "repo": "base/repo"}
    assert (op["path"], op["clone"], op["branch"], op["machine"]) == (
        str(clone),
        str(clone),
        "main",
        None,
    )
    prompt = manager.home / "workspace-prompts" / op["id"]
    assert calls == [
        ["wt", "--codex", "--no-focus", "--dir", str(clone), "--prompt-file", str(prompt)]
    ]
    assert prompt.read_text() == "Tidy the notes\n"
    # Nothing was checked out, branched or switched: the agent works in the clone itself.
    assert not (manager.src / "worktrees").exists()
    assert git("branch", "--format=%(refname:short)").split() == ["feature", "main"]
    assert (git("symbolic-ref", "--short", "HEAD"), git("rev-parse", "HEAD")) == ("main", head)
    agent = json.loads(state.read_text())["agents"][0]
    assert (agent["task"], agent["cwd"]) == ("Tidy the notes", str(clone))
    # A clone's own checkout is its workspace, which herdr creates rather than opens.
    assert herdr_calls(state, "workspace", "create") == [
        ["workspace", "create", "--cwd", str(clone), "--label", "repo", "--no-focus"]
    ]
    assert not herdr_calls(state, "worktree", "open")
    result = op["result"]
    assert (result["path"], result["branch"], result["workspace_id"]) == (str(clone), "main", "w1")
    assert result["url"] == pw.collie_url(None, "w1")
    listed = manager.snapshot()["new"][key]["operation"]
    assert listed["status"] == "complete" and listed["result"]["path"] == str(clone)


def test_a_task_in_a_linked_worktree_opens_it_under_its_clone(local):  # noqa: F811
    manager, _, _, state, _ = local
    clone = (manager.src / "repo").resolve()
    path = make_checkout(local).resolve()
    op = finish(manager, manager.new_task(in_directory(path))["operation"]["pr"])
    assert op["status"] == "complete", op
    # Its clone is what a launch there locks, as a cleanup of the worktree does.
    assert (op["path"], op["clone"], op["branch"]) == (str(path), str(clone), "feature")
    opened = herdr_calls(state, "worktree", "open")
    assert opened == [
        [
            "worktree",
            "open",
            "--cwd",
            str(clone),
            "--path",
            str(path),
            "--label",
            "checkout",
            "--no-focus",
        ]
    ]
    assert json.loads(state.read_text())["agents"][0]["cwd"] == str(path)


def test_a_directory_needs_no_github_remote_and_may_be_detached(local, tmp_path):  # noqa: F811
    manager, _, git, state, _ = local
    notes = tmp_path / "elsewhere" / "notes"
    notes.mkdir(parents=True)
    git("init", "-q", "-b", "main", cwd=notes)
    git(
        "-c",
        "user.name=F",
        "-c",
        "user.email=f@example.test",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "x",
        cwd=notes,
    )
    git("checkout", "-q", "--detach", cwd=notes)
    op = finish(manager, manager.new_task(in_directory(notes))["operation"]["pr"])
    assert op["status"] == "complete", op
    assert op["subject"] == {"kind": "directory", "path": str(notes.resolve())}
    assert op["branch"] is None and op["result"]["branch"] is None
    assert json.loads(state.read_text())["agents"][0]["cwd"] == str(notes.resolve())


def test_a_directory_with_an_open_workspace_gets_its_agent_in_a_new_pane(local):  # noqa: F811
    manager, _, _, state, _ = local
    clone = (manager.src / "repo").resolve()
    space(state, clone, clone, wid="w7")
    data = json.loads(state.read_text())
    data["panes"] = [{"pane_id": "w7:p1", "workspace_id": "w7"}]
    state.write_text(json.dumps(data))
    op = finish(manager, manager.new_task(in_directory(clone))["operation"]["pr"])
    assert op["status"] == "complete", op
    data = json.loads(state.read_text())
    # The workspace's own pane may hold someone's shell: the agent gets a split of it.
    assert data["splits"] == [["w7:p1", "--direction", "right", "--cwd", str(clone), "--no-focus"]]
    assert data["runs"][0][0] == "w7:p11" and data["agents"][0]["pane_id"] == "w7:p11"
    assert not herdr_calls(state, "workspace", "create")
    assert op["result"]["workspace_id"] == "w7"


def test_a_directory_whose_workspace_has_an_agent_takes_no_second_one(local):  # noqa: F811
    manager, _, _, state, _ = local
    clone = (manager.src / "repo").resolve()
    space(state, clone, clone, wid="w7")
    data = json.loads(state.read_text())
    data["agents"] = [{"workspace_id": "w7", "agent": "claude", "cwd": str(clone)}]
    state.write_text(json.dumps(data))
    op = finish(manager, manager.new_task(in_directory(clone))["operation"]["pr"])
    # Refused before anything is typed, so it failed rather than may have started.
    assert op["status"] == "failed", op
    assert "an agent is already open in the herdr workspace" in op["message"]
    data = json.loads(state.read_text())
    assert len(data["agents"]) == 1 and not data.get("runs") and not data.get("splits")


@pytest.mark.parametrize(
    ("changes", "error"),
    [
        ({"directory": "src/repo"}, "absolute path"),
        ({"directory": "{src}/missing"}, "not the top of a Git checkout"),
        ({"directory": "{src}"}, "not the top of a Git checkout"),
        ({"directory": "{src}/repo/.git"}, "not the top of a Git checkout"),
        ({"machine": "auto"}, "Choose the machine the directory is on"),
        ({"machine": "mini"}, "Unknown machine mini"),
        ({"repo": "base/repo"}, "takes no repository"),
        ({"name": "try-parser"}, "takes no repository"),
        ({"issue_title": "Crash"}, "takes no repository"),
        ({"also": []}, "takes no repository"),
        ({"task": " "}, "Supply a task"),
        ({"agent": "gemini"}, "Select Codex or Claude"),
        ({"permission_mode": "auto"}, "Only Claude takes a permission mode"),
        ({"docker": "yes"}, "Invalid new task parameters"),
    ],
)
def test_directory_task_validation(local, changes, error):  # noqa: F811
    manager, _, _, state, _ = local
    request = in_directory((manager.src / "repo").resolve(), **changes)
    request["directory"] = request["directory"].format(src=manager.src.resolve())
    with pytest.raises(ValueError, match=error):
        manager.new_task(request)
    assert manager.snapshot()["new"] == {} and not json.loads(state.read_text())["calls"]


def test_a_directory_inside_a_checkout_is_refused(local):  # noqa: F811
    manager, _, _, _, _ = local
    inner = manager.src / "repo" / "docs"
    inner.mkdir()
    with pytest.raises(ValueError, match="not the top of a Git checkout on this machine"):
        manager.new_task(in_directory(inner.resolve()))


def test_a_directory_the_sandbox_could_not_be_given_is_refused_when_asked(
    local,  # noqa: F811
    tmp_path,
):
    manager, _, git, state, _ = local
    # A home directory that is a checkout (dotfiles) would put all of it in the sandbox.
    git("init", "-q", "-b", "main", cwd=tmp_path)
    for path in (tmp_path.resolve(), f"~/../{tmp_path.name}"):
        with pytest.raises(ValueError, match="holds the home directory"):
            manager.new_task(in_directory(path))
    # The Git directory is read from the checkout's layout, as the sandbox reads it:
    # never from a commondir file, which an agent in the checkout can write.
    path = make_checkout(local).resolve()
    admin = (manager.src / "repo/.git/worktrees/checkout").resolve()
    (admin / "commondir").write_text(str(tmp_path / "elsewhere/.git"))
    # Git itself, or the layout check after it, refuses the checkout.
    with pytest.raises(ValueError, match="not the top of a Git checkout|commondir names"):
        manager.new_task(in_directory(path))
    separate = tmp_path / "work" / "separate"
    separate.mkdir(parents=True)
    git("init", "-q", "--separate-git-dir", str(tmp_path / "work/kept.git"), cwd=separate)
    with pytest.raises(ValueError, match="cannot be verified"):
        manager.new_task(in_directory(separate.resolve()))
    assert manager.snapshot()["new"] == {} and not json.loads(state.read_text())["calls"]


def test_a_directory_task_passes_its_agent_settings_on(local):  # noqa: F811
    manager, _, _, state, _ = local
    calls = helper_calls(manager)
    clone = (manager.src / "repo").resolve()
    request = in_directory(
        clone,
        model="fixture-codex",
        effort="high",
        docker=True,
        exit_when_done=True,
        machine="local",
        tags=["notes"],
    )
    op = finish(manager, manager.new_task(request)["operation"]["pr"])
    assert op["status"] == "complete", op
    prompt = manager.home / "workspace-prompts" / op["id"]
    assert calls == [
        [
            "wt",
            "--codex",
            "--model",
            "fixture-codex",
            "--effort",
            "high",
            "--docker",
            "--exit-when-done",
            "--no-focus",
            "--dir",
            str(clone),
            "--prompt-file",
            str(prompt),
        ]
    ]
    assert op["tags"] == ["notes"] and op["tagged_path"] == str(clone)
    agent = json.loads(state.read_text())["agents"][0]
    assert agent["safe_enable"] == "docker" and "[babysit-done:" in agent["task"]
    # Claude's auto permission mode and account reach wt the same way.
    careful = in_directory(make_checkout(local).resolve(), agent="claude", permission_mode="auto")
    finish(manager, manager.new_task(careful)["operation"]["pr"])
    assert calls[1][:4] == ["wt", "--claude", "--permission-mode", "auto"]


def test_a_directory_that_went_away_fails_its_task(local, tmp_path):  # noqa: F811
    manager, _, git, state, _ = local
    path = make_checkout(local).resolve()
    request = in_directory(path)
    original = pw.directory_checkout
    seen = []

    def gone(target):
        # Removed between the request and the launch, as a cleanup would.
        if seen:
            git("worktree", "remove", "--force", str(path))
        seen.append(target)
        return original(target)

    pw.directory_checkout = gone
    try:
        op = finish(manager, manager.new_task(request)["operation"]["pr"])
    finally:
        pw.directory_checkout = original
    assert op["status"] == "failed" and "is not the top of a Git checkout" in op["message"]
    assert not json.loads(state.read_text())["agents"]
