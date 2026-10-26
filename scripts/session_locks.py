"""Per-session leases: the one place that names, takes and probes a session's lock.

A repair holds its session's flock for as long as its agent runs. There is one lease
per (machine, session), owned by the controller: every supervisor home on it contends
for the same lock. A local session (``machine`` None) keeps its lock beside the agent's
own store, under ``CLAUDE_CONFIG_DIR`` or ``CODEX_HOME``. A session on a worker machine
is locked on the controller instead, so its guardian is still local and the flock is
released when the guardian dies. Locks are keyed by machine, so one session id recorded
on two machines takes two leases; nothing here stops a conversation copied to another
host from being resumed there.

A remote repair that loses its worker cannot tell whether the agent still runs; it
leaves a ``<session>.orphan`` tombstone beside the lock, and the session stays held
until the tombstone is cleared.
"""

import contextlib
import fcntl
import json
import os
import uuid
from pathlib import Path

import machines


def _state_home():
    # Fixed rather than per --home: every supervisor home must see the same leases.
    return Path.home() / ".local/state/babysit-pr"


def path(agent, session_id, config_dir=None, machine=None):
    """Where this session's lock lives; unchanged from before for local sessions."""
    # Session ids are UUIDs everywhere; anything else must not shape a path.
    session_id = str(uuid.UUID(session_id))
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
            # An orphaned agent may still run on its worker; never start a second one.
            if machine is not None and _orphan(agent, session_id, config_dir, machine).exists():
                raise BlockingIOError(f"Session {session_id} is orphaned on {machine}")
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def held(agent, session_id, config_dir=None, machine=None):
    """Whether a babysit repair holds this session right now, from any home."""
    try:
        uuid.UUID(session_id)
    except (TypeError, ValueError):
        return False  # acquire takes only UUIDs, so nothing holds a sidechain or other id.
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
    partial = tombstone.with_suffix(".orphan.tmp")
    partial.write_text(json.dumps({"attempt": attempt, "machine": machine}))
    partial.replace(tombstone)


def clear_orphan(agent, session_id, config_dir, machine):
    _orphan(agent, session_id, config_dir, machine).unlink(missing_ok=True)


def orphan_attempt(session_id, machine):
    """The attempt a remote session's tombstone names; None without one."""
    try:
        data = json.loads(_orphan(None, session_id, None, machine).read_text())
    except (OSError, ValueError):
        return None
    return data.get("attempt") if isinstance(data, dict) else None


def orphans():
    """Every tombstoned remote session, as (machine, session id, attempt)."""
    for tombstone in sorted((_state_home() / "session-locks").glob("*/*.orphan")):
        machine, session_id = tombstone.parent.name, tombstone.name.removesuffix(".orphan")
        try:
            path(None, session_id, None, machine)
        except ValueError:
            continue  # Not a lease this module names.
        yield machine, session_id, orphan_attempt(session_id, machine)
