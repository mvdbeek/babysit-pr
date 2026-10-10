"""Workspace and agent actions hold locks only for what they change; history is pruned.

Fake herdr, git and checkouts only; nothing here reads live workspaces or agents.
"""

import json
import os
import threading
import time
from types import SimpleNamespace

import agent_messages
import browser_extension
import pr_workspaces as pw
import pytest

SHA = "a" * 40


class Listed:
    """An overview that serves fixed items and never refreshes."""

    def __init__(self, key, items):
        self.kind = type("Kind", (), {"key": key})
        self.items = items

    def snapshot(self):
        return {self.kind.key: self.items, "synced_at": time.time()}


def in_thread(function, *args):
    """Run ``function`` in a thread; its outcome is ``box[0]`` once ``thread`` ends."""
    box: list = []

    def run():
        try:
            box.append(function(*args))
        except Exception as exc:  # Reported to the test through the box.
            box.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, box


@pytest.fixture
def two(tmp_path, monkeypatch):
    """Two PRs, each with a verified checkout open in its own herdr workspace."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    prs, items, spaces = [], [], []
    for name, number in (("a", 1), ("b", 2)):
        path = tmp_path / "worktrees" / name
        path.mkdir(parents=True)
        prs.append(
            {
                "id": f"PR_{name}",
                "repo": "base/repo",
                "number": number,
                "url": f"https://github.com/base/repo/pull/{number}",
                "head_repo": "fork/repo",
                "head_branch": f"feature-{name}",
                "head_sha": SHA,
            }
        )
        items.append(
            {
                "path": str(path.resolve()),
                "common": str(tmp_path / "clone/.git"),
                "branch": f"feature-{name}",
                "sha": SHA,
                "remotes": ["base/repo", "fork/repo"],
                "upstream": ["fork/repo", f"feature-{name}"],
            }
        )
        spaces.append(
            {
                "workspace_id": f"w{name}",
                "label": name,
                "agent_status": "idle",
                "worktree": {"checkout_path": items[-1]["path"], "repo_root": "/clone"},
            }
        )

    def herdr(*args):
        if args[:2] == ("workspace", "list"):
            return {"workspaces": spaces}
        if args[:2] == ("agent", "list"):
            return {"agents": []}
        raise AssertionError(f"herdr {args}")

    focused = []
    gates = {"wa": threading.Event(), "wb": threading.Event()}
    gates["wb"].set()
    entered = threading.Event()

    def run(*args, **kwargs):
        if args[:3] == ("herdr", "workspace", "focus"):
            entered.set()
            assert gates[args[3]].wait(10)
            focused.append(args[3])
            return ""
        raise AssertionError(f"run {args}")

    monkeypatch.setattr(pw, "herdr", herdr)
    monkeypatch.setattr(pw, "run", run)
    monkeypatch.setattr(pw, "git", lambda *args: "")  # Every head is contained.
    monkeypatch.setattr(
        pw, "checkout", lambda path: next(i for i in items if i["path"] == str(path))
    )
    (tmp_path / "src").mkdir()
    manager = pw.Workspaces(tmp_path / "state", Listed("prs", prs), lambda: [], tmp_path / "src")
    manager.next_poll = float("inf")  # No background scan.
    manager.inventory = {
        "checkouts": items,
        "workspaces": spaces,
        "clones": [{"path": str(tmp_path / "clone"), "remotes": ["base/repo", "fork/repo"]}],
        "error": None,
        "synced_at": time.time(),
    }
    return SimpleNamespace(
        manager=manager, prs=prs, items=items, focused=focused, gates=gates, entered=entered
    )


def focus(state, name):
    item = state.items[0 if name == "a" else 1]
    return {"id": f"PR_{name}", "action": "focus", "path": item["path"], "workspace_id": f"w{name}"}


def test_a_slow_focus_blocks_neither_snapshots_nor_other_workspaces(two):
    manager = two.manager
    scans = []
    manager.scan = lambda: scans.append(1) or pytest.fail("focus rescanned every clone")
    slow, outcome = in_thread(manager.action, focus(two, "a"))
    assert two.entered.wait(5)
    # Before: the action held the manager lock through herdr, so these waited for it.
    snapshot, value = in_thread(manager.snapshot)
    snapshot.join(5)
    assert not snapshot.is_alive() and value[0]["prs"]["PR_a"]["matches"]
    other, result = in_thread(manager.action, focus(two, "b"))
    other.join(5)
    assert not other.is_alive() and result == [{"result": result[0]["result"]}]
    assert two.focused == ["wb"] and slow.is_alive()
    two.gates["wa"].set()
    slow.join(5)
    assert outcome[0]["result"]["workspace_id"] == "wa" and two.focused == ["wb", "wa"]
    assert not scans  # The cached inventory listed both checkouts.


def test_a_checkout_missing_from_the_cache_is_rescanned(two):
    manager = two.manager
    cached = manager.inventory
    manager.inventory = {**cached, "checkouts": [], "synced_at": None}
    scans = []

    def scan():
        scans.append(1)
        manager.inventory = cached
        return cached

    manager.scan = scan
    two.gates["wa"].set()
    assert manager.action({**focus(two, "a"), "action": "copy"}) == {
        "command": "herdr workspace focus wa"
    }
    assert scans == [1]
    manager.action({**focus(two, "a"), "action": "copy"})
    assert scans == [1]
    with pytest.raises(ValueError, match="Workspace changed"):
        manager.action({**focus(two, "a"), "workspace_id": "elsewhere"})
    assert scans == [1, 1]


def test_creation_scans_without_the_lock_and_still_reserves_once(two):
    manager = two.manager
    release = threading.Event()
    waiting = threading.Semaphore(0)
    inventory = {**manager.inventory, "checkouts": [], "workspaces": []}

    def scan():
        waiting.release()
        assert release.wait(10)
        return inventory

    started = []
    manager.scan = scan
    manager.perform = lambda pr, op, task: started.append(op["id"])
    request = {"id": "PR_a", "action": "create", "task": "Fix it"}
    first, one = in_thread(manager.action, request)
    second, other = in_thread(manager.action, request)
    assert waiting.acquire(timeout=5) and waiting.acquire(timeout=5)
    # Both scans run at once; neither holds the lock that snapshots and focus need.
    snapshot, value = in_thread(manager.snapshot)
    snapshot.join(5)
    assert not snapshot.is_alive() and "PR_a" in value[0]["prs"]
    focused, result = in_thread(manager.action, focus(two, "b"))
    focused.join(5)
    assert not focused.is_alive() and result[0]["result"]["workspace_id"] == "wb"
    release.set()
    first.join(5)
    second.join(5)
    manager.workers["PR_a"].join(5)
    results = one + other
    assert [r.get("started") for r in results].count(True) == 1
    assert len({r["operation"]["id"] for r in results}) == 1 == len(started)


# -- agent messages --


@pytest.fixture
def panes(tmp_path, monkeypatch):
    """One working agent per workspace; Docker changes wait for ``gate``."""
    gate, entered = threading.Event(), threading.Event()
    keys = []
    monkeypatch.setattr(
        agent_messages.workspace_viewer, "workspace_checkout", lambda w: str(tmp_path / w)
    )
    monkeypatch.setattr(
        agent_messages,
        "agents",
        lambda workspace, **kwargs: [
            {
                "pane": f"{workspace}:p1",
                "agent": "claude",
                "status": "working",
                "session": f"session-{workspace}",
            }
        ],
    )

    def change_docker(target, enabled, home):
        entered.set()
        assert gate.wait(10)
        return {"pane": target["pane"], "docker": enabled, "warning": None}

    def herdr(*args):
        keys.append(args)
        return {}

    monkeypatch.setattr(agent_messages, "change_docker", change_docker)
    monkeypatch.setattr(agent_messages, "herdr", herdr)
    return gate, entered, keys


def stop(workspace):
    return {"workspace": workspace, "pane": f"{workspace}:p1", "session": f"session-{workspace}"}


def test_a_docker_restart_holds_up_only_its_own_pane(panes):
    gate, entered, keys = panes
    docker = {**stop("wa"), "enabled": True}
    toggling, toggled = in_thread(agent_messages.set_docker, docker)
    assert entered.wait(5)
    # Before: one lock for every workspace, so Stop elsewhere waited up to 40 seconds.
    assert agent_messages.interrupt(stop("wb")) == {
        "stopped": True,
        "pane": "wb:p1",
        "warning": None,
    }
    assert agent_messages.send({"workspace": "wb", "text": "Go on"})["sent"]
    assert ("agent", "send-keys", "wb:p1", "esc") in keys
    # A stop of the agent being restarted waits for the restart.
    same, stopped = in_thread(agent_messages.interrupt, stop("wa"))
    same.join(0.3)
    assert same.is_alive() and ("agent", "send-keys", "wa:p1", "esc") not in keys
    gate.set()
    toggling.join(5)
    same.join(5)
    assert toggled == [{"pane": "wa:p1", "docker": True, "warning": None}]
    assert stopped[0]["stopped"] is True
    assert agent_messages._locks == {}  # Locks nobody holds are forgotten.


def test_locks_taken_in_any_argument_order_never_deadlock():
    def take(*keys):
        for _ in range(300):
            with agent_messages.locked(*keys), agent_messages.locked(keys[-1]):
                pass

    threads = [
        in_thread(take, ("pane", "p"), ("session", "s"), ("checkout", "c"))[0],
        in_thread(take, ("checkout", "c"), ("session", "s"), ("pane", "p"))[0],
        in_thread(take, ("session", "s"), ("pane", "p"))[0],
    ]
    for thread in threads:
        thread.join(10)
        assert not thread.is_alive()
    assert agent_messages._locks == {}


# -- operation history --


@pytest.fixture
def manager(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(pw, "herdr", lambda *args: pytest.fail(f"herdr {args}"))
    manager = pw.Workspaces(tmp_path / "state", Listed("prs", []), lambda: [], tmp_path / "src")
    manager.next_poll = float("inf")
    return manager


def put(manager, table, key, value):
    with manager.db() as db:
        db.execute(f"INSERT OR REPLACE INTO {table} VALUES (?,?)", (key, json.dumps(value)))


def keys(manager, table, column="id"):
    with manager.db() as db:
        return sorted(key for (key,) in db.execute(f"SELECT {column} FROM {table}"))


def test_finished_history_and_missing_checkouts_are_pruned_after_a_month(manager, tmp_path):
    now = time.time()
    old, recent = now - 31 * 86400, now - 86400
    here = tmp_path / "checkout"
    here.mkdir()
    gone = str(tmp_path / "removed")
    for key, status, at in [
        ("old-complete", "complete", old),
        ("old-failed", "failed", old),
        ("old-uncertain", "uncertain", old),
        ("old-running", "running", old),
        ("old-queued", "queued", old),
        ("old-scheduled", "complete", old),
        ("recent", "complete", recent),
    ]:
        put(manager, "operation_history", key, {"id": key, "status": status, "updated_at": at})
    put(
        manager,
        "scheduled",
        "task",
        {"id": "task", "status": "started", "operation_id": "old-scheduled"},
    )
    for key, status, at, path in [
        ("PR_gone", "complete", old, gone),
        ("PR_failed", "failed", old, None),
        ("PR_here", "complete", old, str(here)),
        ("PR_uncertain", "uncertain", old, gone),
        ("PR_recent", "failed", recent, gone),
    ]:
        put(
            manager,
            "operations",
            key,
            {"id": key, "status": status, "updated_at": at, "path": path},
        )
    for key, path in (("PR_here", str(here)), ("PR_gone", gone)):
        manager.association({"id": key}, {"path": path, "branch": "x"})
    manager.prune_history(now)
    assert keys(manager, "operation_history") == [
        "old-queued",
        "old-running",
        "old-scheduled",
        "old-uncertain",
        "recent",
    ]
    assert keys(manager, "operations", "pr") == ["PR_here", "PR_recent", "PR_uncertain"]
    state = manager.state()["associations"]
    # A missing checkout is marked, not forgotten yet; an existing one is unchanged.
    assert state["PR_gone"][gone]["missing_at"] == now
    assert "missing_at" not in state["PR_here"][str(here)]
    # A checkout back in its place is no longer missing; at most hourly.
    os.rename(here, gone)
    manager.prune_history(now + 60)
    assert manager.state()["associations"] == state
    manager.prune_history(now + 3600)
    state = manager.state()["associations"]
    assert "missing_at" not in state["PR_gone"][gone]
    assert state["PR_here"][str(here)]["missing_at"] == now + 3600
    # A month after it went missing, it is forgotten.
    manager.prune_history(now + 3600 + 30 * 86400 - 1)
    assert set(manager.state()["associations"]) == {"PR_gone", "PR_here"}
    manager.prune_history(now + 2 * 3600 + 30 * 86400)
    assert set(manager.state()["associations"]) == {"PR_gone"}


def test_the_scheduler_prunes_history(manager, monkeypatch):
    pruned = threading.Event()
    monkeypatch.setattr(manager, "prune_history", pruned.set)
    manager.start()
    try:
        assert pruned.wait(5)
    finally:
        manager.close()


# -- extension status --


def test_operation_status_reads_one_record_and_marks_interrupted_launches(manager):
    put(manager, "operations", "new:one", {"id": "op-new", "pr": "new:one", "status": "running"})
    put(manager, "operations", "PR_one", {"id": "op-pr", "pr": "PR_one", "status": "queued"})
    put(manager, "operations", "PR_two", {"id": "op-live", "pr": "PR_two", "status": "running"})
    put(manager, "operations", "PR_old", {"id": "op-done", "pr": "PR_old", "status": "complete"})
    manager.workers["PR_two"] = threading.Thread(target=lambda: None)
    snapshots = []

    def snapshot():
        # As describe() does when the launch's agent came up after a restart.
        snapshots.append(True)
        op = manager.operation("PR_one")
        manager.save_operation(op, status="complete", message="Recovered existing workspace")

    manager.snapshot = snapshot
    server = SimpleNamespace(workspaces=manager)
    extension = browser_extension.Extension(manager.home)

    def status(key):
        return extension.dispatch(server, "client", {"action": "status", "id": key})

    # A launch whose worker is gone takes the snapshot path once: recovered if its agent
    # came up, otherwise marked uncertain, which ends the extension's polling.
    recovered = status("op-pr")["operation"]
    assert recovered["status"] == "complete" and len(snapshots) == 1
    manager.snapshot = lambda: snapshots.append(True)  # Recovers nothing.
    new = status("op-new")["operation"]
    assert new["status"] == "uncertain" and new["message"] == pw.NEW_INTERRUPTED
    assert len(snapshots) == 2
    put(manager, "operations", "PR_one", {"id": "op-pr2", "pr": "PR_one", "status": "queued"})
    assert status("op-pr2")["operation"]["message"] == pw.INTERRUPTED
    assert manager.operation("PR_one")["status"] == "uncertain"
    # Polls of a healthy or finished launch read one record, never a whole snapshot.
    manager.snapshot = lambda: pytest.fail("status built a whole snapshot")
    assert status("op-live")["operation"]["status"] == "running"
    assert status("op-done")["operation"]["status"] == "complete"
    assert status("op-pr2")["operation"]["status"] == "uncertain"
    for key in ("missing", 7, None):
        with pytest.raises(ValueError, match="Operation not found"):
            status(key)
    # A record a newer launch replaced is no longer the item's operation.
    put(manager, "operations", "PR_old", {"id": "op-newer", "pr": "PR_old", "status": "failed"})
    assert manager.operation_status("op-done") is None
