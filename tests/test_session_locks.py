"""Session lock paths, probes and orphan tombstones, against isolated homes only."""

import json
import subprocess
import sys
from pathlib import Path

import pytest
import session_locks

SID = "0b6e2f9a-1c2d-4e5f-8a9b-0c1d2e3f4a5b"
HOLD = """import fcntl,sys
handle=open(sys.argv[1],'a'); fcntl.flock(handle,fcntl.LOCK_EX)
print('held',flush=True); sys.stdin.read()
"""


@pytest.fixture(autouse=True)
def homes(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    return tmp_path


def test_local_paths_are_unchanged(homes):
    import claude_runner

    # The computation run_repair and repair_running made before the module existed.
    assert session_locks.path("claude", SID) == (
        Path(claude_runner.config_home()) / "babysit-pr-locks" / f"{SID}.lock"
    )
    assert session_locks.path("claude", SID, str(homes / "work")) == (
        homes / "work" / "babysit-pr-locks" / f"{SID}.lock"
    )
    assert session_locks.path("codex", SID, str(homes / "work")) == (
        homes / "codex" / "babysit-pr-locks" / f"{SID}.lock"
    )


def test_remote_paths_live_on_the_controller(homes):
    expected = homes / "home/.local/state/babysit-pr/session-locks/mini" / f"{SID}.lock"
    assert session_locks.path("claude", SID, str(homes / "work"), "mini") == expected
    assert session_locks.path("codex", SID, None, "mini") == expected


@pytest.mark.parametrize("machine", ["", "local", "Mini", "../x", "a/b", 3])
def test_machine_ids_are_validated(machine):
    with pytest.raises(ValueError, match="Invalid machine id"):
        session_locks.path("codex", SID, None, machine)


@pytest.mark.parametrize("machine", [None, "mini"])
def test_held_while_another_process_holds_the_lock(machine):
    lock = session_locks.path("codex", SID, None, machine)
    assert not session_locks.held("codex", SID, None, machine)
    lock.parent.mkdir(parents=True)
    holder = subprocess.Popen(
        [sys.executable, "-c", HOLD, str(lock)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout.readline() == "held\n"
        assert session_locks.held("codex", SID, None, machine)
        with pytest.raises(BlockingIOError):
            with session_locks.acquire("codex", SID, None, machine):
                pass
    finally:
        holder.communicate("")
    assert not session_locks.held("codex", SID, None, machine)
    with session_locks.acquire("codex", SID, None, machine):
        assert lock.exists()


def test_orphan_tombstone_holds_until_cleared(homes):
    session_locks.mark_orphan("claude", SID, None, "a1", "mini")
    tombstone = homes / "home/.local/state/babysit-pr/session-locks/mini" / f"{SID}.orphan"
    assert json.loads(tombstone.read_text()) == {"attempt": "a1", "machine": "mini"}
    assert not tombstone.with_suffix(".orphan.tmp").exists()
    assert session_locks.held("claude", SID, None, "mini")
    assert not session_locks.held("claude", SID, None, "other")
    with pytest.raises(BlockingIOError, match="orphaned on mini"):
        with session_locks.acquire("claude", SID, None, "mini"):
            pytest.fail("acquired an orphaned session")
    session_locks.clear_orphan("claude", SID, None, "mini")
    assert not session_locks.held("claude", SID, None, "mini")
    with session_locks.acquire("claude", SID, None, "mini"):
        pass
    session_locks.clear_orphan("claude", SID, None, "mini")


@pytest.mark.parametrize("session_id", ["../x", "a/b", "not-a-uuid", ""])
def test_session_ids_must_be_uuids(session_id):
    with pytest.raises(ValueError):
        session_locks.path("codex", session_id)


def test_only_remote_sessions_can_be_orphaned():
    with pytest.raises(ValueError, match="worker machine"):
        session_locks.mark_orphan("claude", SID, None, "a1", None)
