"""Tasks started from scratch: temporary clones, fake gh/herdr/agents, no live workspaces."""

import json

import pr_workspaces as pw
import pytest
from test_pr_workspaces import finish, helper_calls, local  # noqa: F401 (fixture)


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


@pytest.mark.parametrize("docker", ["true", 1, None])
def test_new_task_docker_must_be_a_flag(local, docker):  # noqa: F811
    manager, _, _, _, _ = local
    with pytest.raises(ValueError, match="Invalid new task"):
        manager.new_task(request(manager, name="containers", docker=docker))


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
