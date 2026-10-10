"""What one Workspaces snapshot costs and writes: indexes, caches and durable state.

Temporary repositories and fake herdr only; nothing here reads live workspaces.
"""

import json
import subprocess
import threading
import time
from unittest.mock import Mock

import pr_workspaces as pw
import pytest

SHA = "a" * 40


def test_ancestry_cache_overflow_keeps_the_new_answer(monkeypatch):
    monkeypatch.setattr(pw, "ANCESTRY", {("old", str(n), "x"): True for n in range(10000)})
    calls = []
    monkeypatch.setattr(pw, "git", lambda *args: calls.append(args))
    pr = {"repo": "base/repo", "head_repo": "fork/repo", "head_branch": "feature", "head_sha": SHA}
    item = {
        "path": "/work/repo",
        "common": "/work/repo/.git",
        "branch": "feature",
        "sha": "b" * 40,
        "remotes": ["base/repo", "fork/repo"],
        "upstream": ["fork/repo", "feature"],
    }
    # Before: the insert that crossed the limit cleared the cache, then read it: KeyError.
    assert pw.verified_head(pr, item) is True
    assert pw.ANCESTRY == {("/work/repo/.git", SHA, "b" * 40): True}
    assert pw.verified_head(pr, item) is True and len(calls) == 1


class Listed:
    """An overview that serves fixed items and never refreshes."""

    def __init__(self, key, items):
        self.kind = type("Kind", (), {"key": key})
        self.items = items

    def snapshot(self):
        return {self.kind.key: self.items, "synced_at": time.time()}


@pytest.fixture
def manager(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setattr(pw, "herdr", lambda *args: pytest.fail(f"herdr {args}"))
    pr = {
        "id": "PR_one",
        "repo": "base/repo",
        "number": 7,
        "url": "https://github.com/base/repo/pull/7",
        "head_repo": "fork/repo",
        "head_branch": "feature",
        "head_sha": SHA,
    }
    (tmp_path / "src").mkdir()
    manager = pw.Workspaces(tmp_path / "state", Listed("prs", [pr]), lambda: [], tmp_path / "src")
    manager.next_poll = float("inf")  # No background scan.
    return manager, pr


def put(manager, op):
    with manager.db() as db:
        db.execute("INSERT OR REPLACE INTO operations VALUES (?,?)", (op["pr"], json.dumps(op)))


def test_an_uncertain_launch_keeps_its_cause_and_is_not_rewritten(manager):
    manager, pr = manager
    inventory = {"checkouts": [], "clones": [], "workspaces": []}
    cause = "wt.py timed out after delivering the prompt"
    put(
        manager,
        {"id": "op1", "pr": pr["id"], "status": "uncertain", "message": cause, "updated_at": 5},
    )
    for _ in range(3):
        info = manager.describe(pr, inventory)
        assert info["operation"]["message"] == cause
    # Before: every poll replaced the cause with "Delivery was interrupted" and a new time.
    assert manager.operation(pr["id"]) == {
        "id": "op1",
        "pr": pr["id"],
        "status": "uncertain",
        "message": cause,
        "updated_at": 5,
    }


def test_an_orphaned_launch_becomes_uncertain_once(manager):
    manager, pr = manager
    inventory = {"checkouts": [], "clones": [], "workspaces": []}
    put(manager, {"id": "op1", "pr": pr["id"], "status": "running", "message": "Cloning"})
    first = manager.describe(pr, inventory)["operation"]
    assert first["status"] == "uncertain" and "interrupted" in first["message"]
    stored = manager.operation(pr["id"])
    manager.describe(pr, inventory)
    assert manager.operation(pr["id"]) == stored


def test_a_launch_that_finished_after_the_state_was_read_is_not_overwritten(manager):
    manager, pr = manager
    inventory = {"checkouts": [], "clones": [], "workspaces": []}
    put(manager, {"id": "op1", "pr": pr["id"], "status": "running", "message": "Starting"})
    state = manager.state()  # Read while the worker still ran.
    put(manager, {"id": "op1", "pr": pr["id"], "status": "failed", "message": "wt failed"})
    assert manager.describe(pr, inventory, state)["operation"]["status"] == "failed"
    assert manager.operation(pr["id"])["message"] == "wt failed"


def test_an_unchanged_association_is_not_written_again(manager):
    manager, pr = manager
    item = {"path": "/w", "common": "/c", "branch": "feature", "remotes": [], "upstream": None}
    data = manager.association(pr, item)
    with manager.db() as db:
        db.execute("UPDATE associations SET data='sentinel'")
    assert manager.association(pr, item, data) == data
    with manager.db() as db:
        assert db.execute("SELECT data FROM associations").fetchone() == ("sentinel",)
    manager.association(pr, {**item, "branch": "other"}, data)
    with manager.db() as db:
        assert json.loads(db.execute("SELECT data FROM associations").fetchone()[0])["branch"] == (
            "other"
        )


def test_expired_new_tasks_are_pruned_at_most_once_a_minute(manager):
    manager, _ = manager
    old = {"id": "x", "pr": "new:old", "status": "complete", "created_at": 1.0}

    def count():
        with manager.db() as db:
            return db.execute("SELECT count(*) FROM operations").fetchone()[0]

    put(manager, old)
    manager.new_operations(manager.state())
    assert count() == 0
    put(manager, old)
    manager.new_operations(manager.state())
    assert count() == 1  # Hidden from the list, deleted on the next pass a minute on.
    manager.pruned_at -= pw.PRUNE_SECONDS
    assert manager.new_operations(manager.state()) == {}
    assert count() == 0


class Turn:
    """The compute lock, recording each request that reached it."""

    def __init__(self):
        self.lock = threading.Lock()
        self.arrived = []

    def __enter__(self):
        self.arrived.append(threading.current_thread().name)
        self.lock.acquire()

    def __exit__(self, *exc):
        self.lock.release()


def test_concurrent_snapshots_share_the_computation_after_their_arrival(manager, monkeypatch):
    manager, _ = manager
    started, release = threading.Event(), threading.Event()
    runs = []

    def compute():
        runs.append(1)
        if len(runs) == 1:
            started.set()
            assert release.wait(5)
        return {"run": len(runs)}

    turn = Turn()
    monkeypatch.setattr(manager, "compute_snapshot", compute)
    monkeypatch.setattr(manager, "compute_lock", turn)
    results = {}
    threads = {
        name: threading.Thread(target=lambda name=name: results.update({name: manager.snapshot()}))
        for name in ("first", "second", "third")
    }
    threads["first"].start()
    assert started.wait(5)
    threads["second"].start()
    threads["third"].start()
    deadline = time.monotonic() + 5
    while len(turn.arrived) < 3:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    release.set()
    for thread in threads.values():
        thread.join(5)
        assert not thread.is_alive()
    # Before: each request computed its own, every one running git for a cold cache.
    # Requests that came during the first share the one after it, which they did not
    # predate.
    assert results["first"] == {"run": 1}
    assert results["second"] == {"run": 2} and results["third"] is results["second"]
    assert manager.snapshot() == {"run": 3}
    # A failure reaches its callers and leaves the next request free to compute again.
    monkeypatch.setattr(manager, "compute_snapshot", Mock(side_effect=ValueError("broken")))
    with pytest.raises(ValueError, match="broken"):
        manager.snapshot()
    monkeypatch.setattr(manager, "compute_snapshot", lambda: {"run": "again"})
    assert manager.snapshot() == {"run": "again"}


class Everything(dict):
    """An index entry that lists every checkout: the full scan matches() used to do."""

    def __init__(self, size):
        super().__init__()
        self.size = size

    def get(self, key, default=None):
        return range(self.size)


def checkout(path, remotes, branch, sha="c" * 40, upstream=None):
    return {
        "path": path,
        "common": f"{path}/.git" if "/worktrees/" not in path else "/src/base/.git",
        "branch": branch,
        "sha": sha,
        "remotes": remotes,
        "upstream": upstream,
    }


def test_indexed_matching_equals_the_full_scan(manager, monkeypatch):
    manager, _ = manager
    # merge-base answers by commit, without a repository.
    monkeypatch.setattr(pw, "ANCESTRY", {})

    def git(path, *args):
        if args[0] == "merge-base" and args[2].startswith("b"):
            raise ValueError("not an ancestor")

    monkeypatch.setattr(pw, "git", git)
    checkouts = [
        checkout("/src/base", ["base/repo", "fork/repo"], "main"),
        checkout(
            "/src/worktrees/repo/pr-fork-7",
            ["base/repo", "fork/repo"],
            "feature",
            upstream=["fork/repo", "feature"],
        ),
        checkout(
            "/src/worktrees/repo/pr-fork-8",
            ["base/repo", "fork/repo"],
            "other",
            upstream=["fork/repo", "other"],
        ),
        checkout("/src/worktrees/repo/issue-12-crash", ["base/repo"], "issue-12-crash"),
        checkout("/src/worktrees/repo/issue-12-copy", ["elsewhere/repo"], "issue-12-copy"),
        checkout("/src/worktrees/repo/issue-13", ["base/repo"], "issue-13"),
        checkout("/src/worktrees/repo/sentry-proj-1a", ["base/repo"], "sentry-proj-1a"),
        checkout("/src/worktrees/repo/sentry-proj-1a-2", ["base/repo"], "sentry-proj-1a-2"),
        checkout("/src/worktrees/repo/sentry-proj-1a-x", ["other/app"], "sentry-proj-1a"),
        checkout("/src/app", ["other/app"], "feature", upstream=["fork/repo", "feature"]),
        checkout("/src/worktrees/app/issue-7", ["other/app"], "issue-7"),
        checkout("/headless/watch", ["base/repo"], "feature"),
        checkout("/src/worktrees/repo/scratch", ["base/repo"], "my-branch"),
        checkout("/src/norepo", [], "issue-12"),
    ]
    inventory = {
        "checkouts": checkouts,
        "clones": [],
        "workspaces": [
            {
                "workspace_id": "w1",
                "label": "pr-repo-feature",
                "agent_status": "idle",
                "worktree": {"repo_root": "/src/base", "checkout_path": checkouts[1]["path"]},
            },
            {
                "workspace_id": "w2",
                "label": "issue",
                "worktree": {"repo_root": "/src/base", "checkout_path": checkouts[3]["path"]},
            },
            {
                "workspace_id": "w3",
                "label": "second",
                "worktree": {"repo_root": "/src/base", "checkout_path": checkouts[1]["path"]},
            },
        ],
    }
    pr = {"repo": "base/repo", "head_repo": "fork/repo", "head_branch": "feature"}
    targets = [
        {**pr, "id": "PR_7", "number": 7, "head_sha": SHA},
        {**pr, "id": "PR_7b", "number": 7, "head_sha": "b" * 40},
        {**pr, "id": "PR_8", "number": 8, "head_sha": SHA, "head_branch": "other"},
        {**pr, "id": "PR_x", "number": 9, "head_sha": SHA, "head_repo": "base/repo"},
        {
            "id": "PR_app",
            "repo": "other/app",
            "number": 7,
            "head_repo": "fork/repo",
            "head_branch": "feature",
            "head_sha": SHA,
        },
        {"id": "I_12", "kind": "issue", "repo": "base/repo", "number": 12, "linked_prs": []},
        {
            "id": "I_13",
            "kind": "issue",
            "repo": "base/repo",
            "number": 13,
            "linked_prs": [{**pr, "number": 7, "head_sha": SHA}],
        },
        {
            "id": "I_7",
            "kind": "issue",
            "repo": "base/repo",
            "number": 7,
            "linked_prs": [{**pr, "number": 7, "head_sha": SHA}],
        },
        {"id": "I_none", "kind": "issue", "repo": "third/repo", "number": 99, "linked_prs": []},
        {
            "id": "watch:1",
            "kind": "watch",
            "repo": "base/repo",
            "cwd": "/headless/watch",
            "head_repo": "fork/repo",
        },
        {"id": "watch:2", "kind": "watch", "repo": "base/repo", "cwd": "/nowhere"},
        {"id": "sentry:1", "kind": "sentry", "repo": "base/repo", "short_id": "PROJ-1A"},
        {"id": "new:1", "kind": "scratch", "repo": "base/repo", "branch": "my-branch"},
    ]
    associations = {
        "PR_8": {
            checkouts[2]["path"]: {**checkouts[2], "head_repo": "fork/repo", "head_branch": "other"}
        },
        "new:1": {checkouts[12]["path"]: {**checkouts[12], "head_repo": None, "head_branch": None}},
    }
    bound = {("https://github.com/base/repo/pull/9", checkouts[0]["path"], "main")}
    common = {
        "associations": associations,
        "bound": bound,
        "spaces": pw.workspaces_by_path(inventory),
    }
    indexed = {**common, "index": pw.checkout_index(inventory)}
    full = {
        **common,
        "index": {key: Everything(len(checkouts)) for key in ("path", "remote", "number")},
    }
    found = 0
    for target in targets:
        expected = manager.matches(target, inventory, full)
        assert manager.matches(target, inventory, indexed) == expected, target["id"]
        found += len(expected[0]) + len(expected[1])
    assert found >= 10  # The fixture exercises matches, suggestions and workspaces.


def make_clone(src, name, remote=None):
    path = src / name
    path.mkdir()
    steps = [
        ["init", "-b", "main"],
        ["-c", "user.name=F", "-c", "user.email=f@x", "commit", "--allow-empty", "-m", "x"],
    ]
    if remote:
        steps.append(["remote", "add", "origin", f"https://github.com/{remote}.git"])
    for args in steps:
        subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)
    return path.resolve()


@pytest.fixture
def clones(tmp_path, monkeypatch):
    config = tmp_path / "gitconfig"
    config.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(pw, "herdr", lambda *args: {"workspaces": []})
    src = tmp_path / "src"
    src.mkdir()
    wanted = make_clone(src, "repo", "base/repo")
    unwanted = [make_clone(src, f"other{n}", f"other/repo{n}") for n in range(5)]
    return src, wanted, unwanted


@pytest.fixture
def git_calls(monkeypatch):
    calls = []
    original = pw.run

    def run(*args, **kwargs):
        calls.append([str(arg) for arg in args])
        return original(*args, **kwargs)

    monkeypatch.setattr(pw, "run", run)
    return calls


TRACKED = {"id": "PR_one", "repo": "base/repo", "number": 7, "head_repo": "fork/repo"}


def test_scan_starts_no_git_for_untracked_clones_or_removed_checkouts(clones, git_calls, tmp_path):
    src, wanted, unwanted = clones
    jobs = [
        {"id": "a", "repo": "base/repo", "cwd": str(tmp_path / "removed"), "status": "watching"},
        {"id": "b", "repo": "base/repo", "cwd": str(tmp_path / "removed"), "status": "closed"},
    ]
    manager = pw.Workspaces(tmp_path / "state", Listed("prs", [TRACKED]), lambda: jobs, src)
    inventory = manager.scan()
    assert [clone["path"] for clone in inventory["clones"]] == [str(wanted)]
    assert [item["path"] for item in inventory["checkouts"]] == [str(wanted)]
    paths = {call[2] for call in git_calls if call[0] == "git"}
    assert paths == {str(wanted)}  # Three calls for the tracked clone, none elsewhere.
    assert len(git_calls) == 3
    # A watch's checkout outside the clones is still resolved, an ended one's included:
    # its detail offers the workspace for cleanup.
    for status in ("watching", "closed"):
        jobs[:] = [{"id": "d", "repo": "base/repo", "cwd": str(unwanted[0]), "status": status}]
        git_calls.clear()
        assert str(unwanted[0]) in {item["path"] for item in manager.scan()["checkouts"]}
        assert {call[2] for call in git_calls} == {str(wanted), str(unwanted[0])}


def test_scan_leaves_remotes_its_own_config_reading_misses_to_git(clones, git_calls, tmp_path):
    src, wanted, unwanted = clones
    # A remote from an included file, and one under a section header with a comment.
    included = make_clone(src, "included")
    shared = tmp_path / "remotes.cfg"
    shared.write_text('[remote "origin"]\n\turl = https://github.com/base/repo.git\n')
    subprocess.run(
        ["git", "-C", str(included), "config", "include.path", str(shared)],
        check=True,
        capture_output=True,
    )
    commented = make_clone(src, "commented")
    with (commented / ".git" / "config").open("a") as config:
        config.write('[remote "origin"] # the fork\n\turl = https://github.com/base/repo.git\n')
    assert pw.file_remotes(included / ".git") == {} == pw.file_remotes(commented / ".git")
    manager = pw.Workspaces(tmp_path / "state", Listed("prs", [TRACKED]), lambda: [], src)
    # Before: both were skipped as tracking nothing wanted, so their PRs found no checkout.
    found = [clone["path"] for clone in manager.scan()["clones"]]
    assert sorted(found) == sorted(map(str, (wanted, included, commented)))
    # Clones whose config names only other remotes still start no git.
    assert {call[2] for call in git_calls} == set(found)
