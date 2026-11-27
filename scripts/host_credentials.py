"""Temporary credential files for a Safehouse agent on this Mac.

Seatbelt fixes what a sandbox may read when it starts. A grant therefore restarts the
idle agent with one private directory added read-only (``agent_messages.grant_credential``).
The directories live in the dashboard's auth home, which no sandbox may otherwise read.
Each grant's metadata sits beside its directory, outside what the agent is granted.
"""

import contextlib
import json
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
from pathlib import Path

import worker_credentials

DIRECTORY = "credential-grants"
NAME = "credential"
# A restart's read-only grant, which agent_docker drops from a launcher it reuses.
GRANTED = re.compile(rf"--add-dirs-ro=/.*/{DIRECTORY}/[0-9a-f]{{32}}")
SWEEP = 15  # Seconds between expiry checks while any grant remains.
_lock = threading.Lock()
_sweeper: threading.Thread | None = None


def root(auth_home):
    return Path(auth_home) / DIRECTORY


def stage(auth_home, data, minutes, pane, session, clock=time.time):
    """Write a read-only credential file in a new grant directory and return its path."""
    base = root(auth_home)
    worker_credentials.private_root(base)
    grant = uuid.uuid4().hex
    meta = base / f"{grant}.json"
    directory = base / grant
    # Metadata first, so that a sweep never finds a directory it cannot expire. It is
    # published whole, so that a sweep never reads half of it as invalid.
    with tempfile.NamedTemporaryFile("w", dir=base, delete=False) as handle:
        json.dump({"expires": clock() + minutes * 60, "pane": pane, "session": session}, handle)
    os.replace(handle.name, meta)
    try:
        directory.mkdir(mode=0o700)
        path = directory / NAME
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
    except BaseException:
        remove(auth_home, grant)
        raise
    return path


def remove(auth_home, grant):
    base = root(auth_home)
    shutil.rmtree(base / grant, ignore_errors=True)
    (base / f"{grant}.json").unlink(missing_ok=True)


def sweep(auth_home, session_of=None, clock=time.time):
    """Remove expired grants and grants whose pane now runs another conversation.

    ``session_of(pane)`` names the pane's current agent session. None or "" means it is
    unknown or no agent runs there, as during a restart, and the grant stays until it
    expires. Returns how many grants remain.
    """
    base = root(auth_home)
    try:
        entries = list(base.iterdir())
    except FileNotFoundError:
        return 0
    now = clock()
    remaining = 0
    for entry in entries:
        grant = entry.name.removesuffix(".json")
        if not worker_credentials.GRANT_ID.fullmatch(grant):
            continue
        if entry.suffix != ".json":
            if not entry.with_name(f"{grant}.json").exists():
                remove(auth_home, grant)
            continue
        try:
            meta = json.loads(entry.read_text())
            expired = not meta["expires"] > now
            pane, session = meta["pane"], meta["session"]
        except (OSError, ValueError, KeyError, TypeError):
            expired = True
        if not expired and session_of is not None:
            current = session_of(pane)
            expired = bool(current) and current != session
        if expired:
            remove(auth_home, grant)
        else:
            remaining += 1
    return remaining


def watch(auth_home, session_of, interval=SWEEP):
    """Sweep in the background while any grant remains."""
    global _sweeper
    with _lock:
        if _sweeper is not None and _sweeper.is_alive():
            return
        _sweeper = threading.Thread(
            target=_run,
            args=(auth_home, session_of, interval),
            daemon=True,
            name="credential-grants",
        )
        _sweeper.start()


def _run(auth_home, session_of, interval):
    global _sweeper
    while True:
        # The decision to stop is taken under the lock, so a grant staged meanwhile
        # either is seen here or starts a new sweeper in watch().
        with _lock:
            remaining = 1
            with contextlib.suppress(OSError):
                remaining = sweep(auth_home, session_of)
            if not remaining:
                _sweeper = None
                return
        time.sleep(interval)
