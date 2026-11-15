"""Tell a worker's herdr the session of each Codex started by hand in one of its panes.

herdr lists an agent's session only once something reports it. A sandboxed launch's
broker does (``worker_broker``), and herdr's own hook does for Claude. herdr's Codex
hook cannot do it here: Codex runs hooks in its shared background server, with the
environment of whichever pane started that server first, so a second Codex would be
reported in the first one's pane, and Codex runs a hook only once it is trusted in a
``config.toml`` that sandboxes share. Without a report the dashboard cannot tell
whether the Codex in a pane runs the transcript it shows ("Status unknown").

This runs as the worker's user, outside every sandbox (a systemd user service that
``provision_worker.sh --user-step`` installs), and reads ``/proc`` every ``POLL``
seconds. A Codex is the pane's when it runs in the worker's own PID namespace (a
sandboxed one never does, and is its broker's to report), carries the pane's
``HERDR_PANE_ID``, is not a background server and was not started by another Codex;
a pane with two of them is left alone.

Started with ``--no-daemon``, Codex holds its conversation's rollout open itself, and
that is its session, as the broker reads a sandboxed one's. Otherwise the shared
background server holds the rollouts of every Codex of its home, and nothing says
which pane each belongs to. The pane's Codex is then given the rollout that server
last wrote among those recorded in the directory the Codex runs in, and only while no
other such Codex runs in that directory. That is a guess from how Codex happens to
work: after a ``/new`` or ``/resume`` the name follows at the conversation's first
write, and two Codex in one directory are not named at all. A Codex may also run a
conversation recorded in another directory (``-C``, or one it resumed from elsewhere),
which would pass for that directory's; while one started that way has no rollout
recorded in its own directory, no Codex of that server is named.

Only a rollout in the Codex's own home is reported, never one a link leads to.
Sandboxed agents write that home too: what they put in ``/proc`` or in a rollout is
read as bytes of bounded length and can at most leave a pane unnamed, or, by writing
to a rollout the server holds for a directory, make it pass for that directory's
latest. A session's name grants nothing: every action on a pane checks the pane.
With ``--print`` it prints what one look finds, as JSON, and tells herdr nothing.
"""

import contextlib
import json
import os
import sys
import time
from pathlib import Path
from typing import NamedTuple

# Its own deployed directory, which no sandbox can write; -I leaves it off sys.path.
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

import worker_broker  # noqa: E402

POLL = 5  # Seconds between looks at /proc.
MAX_ANCESTORS = 64
MAX_ENVIRON = 1 << 20
MAX_STATUS = 1 << 16
MAX_PATH = 4096  # Longest directory a rollout may record and still be compared.
FUTURE = 60  # Seconds ahead of this clock a rollout's last write may lie.
# Arguments with which Codex may run a conversation recorded in another directory.
ELSEWHERE = {"resume", "fork", "-C", "--cd"}


class Codex(NamedTuple):
    pid: int
    argv: list
    pane: str | None
    cwd: str | None
    home: str
    held: dict  # The rollouts it writes, as worker_broker.held_rollouts gives them.

    @property
    def server(self):
        """Codex's background server, which runs the conversations of others."""
        return "app-server" in self.argv[1:]

    @property
    def elsewhere(self):
        return any(word in ELSEWHERE or word.startswith("--cd=") for word in self.argv[1:])


def _status(proc, pid):
    """The fields of a process's status, each as its list of words; {} once it is gone.
    A process names itself with any bytes it likes."""
    try:
        with open(proc / str(pid) / "status", "rb") as handle:
            lines = handle.read(MAX_STATUS).decode(errors="surrogateescape").splitlines()
    except OSError:
        return {}
    return {words[0].rstrip(":"): words[1:] for words in map(str.split, lines) if words}


def _words(proc, pid, name):
    """The NUL-separated strings of a process's ``environ`` or ``cmdline``."""
    try:
        with open(proc / str(pid) / name, "rb") as handle:
            data = handle.read(MAX_ENVIRON)
    except OSError:
        return []
    return [word.decode(errors="surrogateescape") for word in data.split(b"\0") if word]


def _directory(proc, pid):
    try:
        return os.path.realpath(os.readlink(proc / str(pid) / "cwd"))
    except OSError:
        return None


def codexes(proc, default_home):
    """Every Codex process in this PID namespace that this user may read."""
    found: list = []
    try:
        pids = sorted(int(entry) for entry in os.listdir(proc) if entry.isdigit())
    except OSError:
        return found
    for pid in pids:
        status = _status(proc, pid)
        # npm's codex runs the native one, which is named codex. One PID means this
        # namespace: a sandboxed Codex has a second one, and its broker reports it.
        if status.get("Name") != ["codex"] or len(status.get("NSpid", [])) != 1:
            continue
        variables = dict(word.partition("=")[::2] for word in _words(proc, pid, "environ"))
        if not variables:
            continue  # Another user's, or gone.
        try:
            home = os.path.realpath(variables.get("CODEX_HOME") or default_home)
            found.append(
                Codex(
                    pid,
                    _words(proc, pid, "cmdline"),
                    variables.get("HERDR_PANE_ID") if variables.get("HERDR_ENV") == "1" else None,
                    _directory(proc, pid),
                    home,
                    worker_broker.held_rollouts(proc, pid, home),
                )
            )
        except Exception:  # noqa: BLE001, S112 - one unreadable Codex hides only itself.
            continue
    return found


def _nested(proc, codex, pids):
    """Whether another Codex started this one: a tool it runs, not what the pane shows."""
    pid = codex.pid
    for _ in range(MAX_ANCESTORS):
        parent = _status(proc, pid).get("PPid", [])
        if not parent or not parent[0].isdigit() or int(parent[0]) <= 1:
            return False
        pid = int(parent[0])
        if pid in pids:
            return True
    return True  # Too deep to tell.


def _recorded(listed):
    """The directory each rollout a background server writes was recorded in, by the
    server's home: {home: {(ID, transcript): directory}}. A directory the rollout does
    not plainly name is None."""
    found: dict = {}
    for server in listed:
        if not server.server:
            continue
        for key, meta in server.held.items():
            cwd = meta.get("cwd")
            directory = None
            if isinstance(cwd, str) and cwd.startswith("/") and len(cwd) <= MAX_PATH:
                with contextlib.suppress(OSError, ValueError):
                    directory = os.path.realpath(cwd)
            found.setdefault(server.home, {})[key] = directory
    return found


def _latest(rollouts, now):
    """Of several rollouts, the one written last; none when that cannot be told."""
    written = []
    for session, path in rollouts:
        try:
            modified = os.stat(path).st_mtime
        except OSError:
            return None
        if modified > now + FUTURE:
            return None  # A time someone set: which was written last is not known.
        written.append((modified, session, path))
    written.sort()
    if len(written) > 1 and written[-1][0] == written[-2][0]:
        return None
    return written[-1][1:] if written else None


def sessions(proc=Path("/proc"), default_home=None, now=None):
    """The session each pane's Codex runs, as {pane: (its PID, session ID, transcript)}."""
    now = time.time() if now is None else now
    listed = codexes(proc, default_home or Path.home() / ".codex")
    pids = {codex.pid for codex in listed}
    own = [codex for codex in listed if not codex.server and not _nested(proc, codex, pids)]
    recorded = _recorded(listed)
    # Homes where a Codex may run a conversation recorded in another directory: started
    # to resume one or with a directory of its choice, and none recorded where it runs.
    astray = {
        codex.home
        for codex in own
        if not codex.held
        and codex.elsewhere
        and (codex.cwd is None or codex.cwd not in recorded.get(codex.home, {}).values())
    }
    found = {}
    for codex in own:
        if not codex.pane or sum(other.pane == codex.pane for other in own) != 1:
            continue
        if codex.held:
            # It writes its conversation itself; two at once are not told apart.
            chosen = next(iter(codex.held)) if len(codex.held) == 1 else None
        elif (
            codex.cwd is None
            or codex.home in astray
            or any(
                other is not codex
                and not other.held
                and other.cwd == codex.cwd
                and other.home == codex.home
                for other in own
            )
        ):
            chosen = None  # Which of the server's rollouts is this one's is not known.
        else:
            here = [
                key
                for key, directory in recorded.get(codex.home, {}).items()
                if directory == codex.cwd
            ]
            chosen = _latest(here, now)
        if chosen:
            found[codex.pane] = (codex.pid, *chosen)
    return found


def report(proc=Path("/proc"), running=lambda: True, poll=POLL):
    """Tell herdr each pane's session, and again when it changes or the pane's Codex is
    another process. A report herdr refuses is tried again less and less often."""
    told: dict = {}
    refused: dict = {}
    while running():
        try:
            found = sessions(proc)
        except Exception:  # noqa: BLE001 - what agents write must not end the reports.
            found = {}
        now = time.monotonic()
        for pane, value in found.items():
            count, wait = refused.get(pane, (0, 0.0))
            if told.get(pane) == value or now < wait:
                continue
            if worker_broker.tell_herdr("codex", pane, *value[1:]):
                told[pane] = value
                refused.pop(pane, None)
            else:
                backoff = min(poll * 2 ** min(count, 5), worker_broker.MAX_BACKOFF)
                refused[pane] = (count + 1, now + backoff)
        told = {pane: value for pane, value in told.items() if pane in found}
        refused = {pane: value for pane, value in refused.items() if pane in found}
        time.sleep(poll)


def main(argv):
    if argv == ["--print"]:
        found = sessions()
        listing = {
            pane: {"pid": pid, "session": session, "transcript": path}
            for pane, (pid, session, path) in found.items()
        }
        print(json.dumps(listing, indent=2, sort_keys=True))
        return 0
    if argv:
        print("usage: worker_sessions.py [--print]", file=sys.stderr)
        return 2
    with contextlib.suppress(KeyboardInterrupt):
        report()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
