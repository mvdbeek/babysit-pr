"""What ``wt`` shares with the dashboard's exit watches; stdlib only and cheap to import.

A brief asks the agent to end its final response with a marker line once it is done,
and ``workspace_exit`` follows the session and exits the agent when it does. Launches
the dashboard makes keep that watch with the launch. ``wt --exit-when-done`` records
its own in the ``exit_watches`` table of the dashboard's workspace database, and the
dashboard follows it from its scheduler loop. No dashboard running on that state home
means nothing exits: the watch waits until one runs.
"""

import contextlib
import json
import os
import time
import uuid
from pathlib import Path

BRIEF = (
    "\n\nThis task was scheduled to run unattended. When it is finished and nothing more "
    "is needed from the user, end your final response with this line:\n{marker}\n"
    "Leave the line out if you stop to ask a question or cannot finish; the session then "
    "stays open for the user.\n"
)
DATABASE = "pr-workspaces.sqlite"
TABLE = "CREATE TABLE IF NOT EXISTS exit_watches (id TEXT PRIMARY KEY, data TEXT)"


def new_marker():
    return f"[babysit-done:{uuid.uuid4().hex[:16]}]"


def brief(marker):
    return BRIEF.format(marker=marker)


def watching(now=None):
    return {
        "state": "watching",
        "since": time.time() if now is None else now,
        "message": "Waiting for the agent to finish",
    }


@contextlib.contextmanager
def connect(home):
    """The workspace database under a dashboard state home, made private if missing.

    sqlite errors come out as ``OSError``, so callers need not import sqlite3.
    """
    import sqlite3

    try:
        Path(home).mkdir(parents=True, exist_ok=True)
        path = Path(home) / DATABASE
        os.close(os.open(path, os.O_CREAT | os.O_RDWR, 0o600))
        db = sqlite3.connect(path, timeout=10)
    except sqlite3.Error as exc:
        raise OSError(str(exc)) from exc
    try:
        with db:
            db.execute(TABLE)
            yield db
    except sqlite3.Error as exc:
        raise OSError(str(exc)) from exc
    finally:
        db.close()


def register(home, watch):
    with connect(home) as db:
        db.execute("INSERT INTO exit_watches VALUES (?,?)", (watch["id"], json.dumps(watch)))


def forget(home, key):
    with connect(home) as db:
        db.execute("DELETE FROM exit_watches WHERE id=?", (key,))
