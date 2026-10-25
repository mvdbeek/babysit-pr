"""Per-session leases: the one place that names, takes and probes a session's lock.

A repair holds its session's flock for as long as its agent runs, and every supervisor
home on the controller shares the same lock. A local session (``machine`` None) keeps
its lock beside the agent's own store, under ``CLAUDE_CONFIG_DIR`` or ``CODEX_HOME``.
A session on a worker machine is locked on the controller instead, so its guardian is
still local and the flock is released when the guardian dies.

A remote repair that loses its worker cannot tell whether the agent still runs; it
leaves a ``<session>.orphan`` tombstone beside the lock, and the session stays held
until the tombstone is cleared.
"""

import contextlib
import fcntl
import json
import os
from pathlib import Path

import machines


def _state_home():
    # Fixed rather than per --home: every supervisor home must see the same leases.
    return Path.home() / ".local/state/babysit-pr"


def path(agent, session_id, config_dir=None, machine=None):
    """Where this session's lock lives; unchanged from before for local sessions."""
    if machine is not None:
        if not isinstance(machine, str) or not machines.ID.fullmatch(machine) or machine == "local":
            raise ValueError(f"Invalid machine id: {machine!r}")
        return _state_home() / "session-locks" / machine / f"{session_id}.lock"
    if agent == "claude":
        import claude_runner

        lock_home = Path(config_dir or claude_runner.config_home()) / "babysit-pr-locks"
    else:
        lock_home = (
            Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "babysit-pr-locks"
        )
    return lock_home / f"{session_id}.lock"


def _orphan(agent, session_id, config_dir, machine):
    return path(agent, session_id, config_dir, machine).with_suffix(".orphan")


@contextlib.contextmanager
def acquire(agent, session_id, config_dir=None, machine=None):
    """Hold the session's lock; BlockingIOError if another repair already does."""
    lock = path(agent, session_id, config_dir, machine)
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def held(agent, session_id, config_dir=None, machine=None):
    """Whether a babysit repair holds this session right now, from any home."""
    if machine is not None and _orphan(agent, session_id, config_dir, machine).exists():
        return True
    lock = path(agent, session_id, config_dir, machine)
    if lock.exists():
        with lock.open("a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(handle, fcntl.LOCK_UN)
    return False


def mark_orphan(agent, session_id, config_dir, attempt, machine):
    """Keep a remote session held after its repair lost the worker mid-run."""
    if machine is None:
        raise ValueError("Only a session on a worker machine can be orphaned")
    tombstone = _orphan(agent, session_id, config_dir, machine)
    tombstone.parent.mkdir(parents=True, exist_ok=True)
    tombstone.write_text(json.dumps({"attempt": attempt, "machine": machine}))


def clear_orphan(agent, session_id, config_dir, machine):
    _orphan(agent, session_id, config_dir, machine).unlink(missing_ok=True)
