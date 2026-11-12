#!/usr/bin/env python3
"""Watch registration, handoff and sub-tasks for one sandbox: only its own worktrees and
sessions.

    worker_broker.py [--cwd=DIR] [--granted=FEATURES] LISTEN AGENT CONFIG_DIR [WORKTREE...]

On a worker, babysit-bwrap starts this outside the sandbox and binds LISTEN inside,
naming it in ``BABYSIT_REGISTER_SOCKET``; the sandbox never sees ``worker.json``, whose
token this process holds. A client sends one request (``worker_api``'s fields for its
``action``, register when it names none) and closes its side; the answer is the
controller's, or a refusal. Forwarded are only requests for the launch's ``AGENT``
whose ``cwd`` is one of the launch's granted WORKTREEs and whose session transcript
lies, resolved, exactly in its CONFIG_DIR's store (``projects/<project>/<id>.jsonl`` for
Claude, under ``sessions/`` for Codex). The controller is told to check that transcript,
and that CONFIG_DIR, which it checks again on the path it opens; no Claude account is
passed, so the account is where the transcript lies. So an agent never registers
another task's checkout, nor a session of another account or Codex home. A handoff
names the launch's own conversation only, with the transcript found for it, and the
controller acts only on a watch of that machine, worktree and session whose transcript
is exactly that one.

A launch (``action: launch``) asks for a sub-task: ``wt.LAUNCH_FIELDS`` and optional ``tags``/``review`` under
``launch``, and the conversations it may run in under ``sessions``. The broker keeps its
launch's agent's session, checked as a handoff's, and tells the controller, as the
sub-task's parent, what the sandbox cannot forge: the launch's working directory
(``--cwd``, babysit-bwrap's), its agent and CONFIG_DIR, that transcript, and whether the
launch was granted Docker (``docker`` in ``--granted``). The review flag requests approval,
never authorizes a provider/account change. The controller decides the rest; a launch without
``--cwd`` starts nothing.

A follow-up (``action: follow-up``) carries a child task's ``target`` handle and
``text`` with the same ``sessions`` as a launch. The broker supplies the same parent
identity; the controller checks that the recorded child belongs to it before
messaging its live agent. No pane, worker or session to message comes from the sandbox.

"Its own sessions" means every session in that configuration directory, which other
launches of the same Claude account or Codex home share and can write: an agent can
register such a session (one recorded in its own worktree) or plant one there, so a
repair may resume a conversation another launch of that account started. That gives
it no access beyond what the account and its worktree already have.

One request is forwarded at a time, at most one of each action per MIN_INTERVAL
seconds, so one sandbox cannot use up its worker's requests at the controller. A
launch's answer waits for the task to start, a few minutes at most.

It also tells the pane's herdr which session the sandbox runs, as herdr's own hooks
would: inside the sandbox they cannot reach herdr. Without it, herdr lists the agent
with no session, and the dashboard cannot tell that the agent runs the transcript it
shows. In its own PID namespace Claude records its session under the PID it sees there
(``sessions/2.json``, in every sandbox alike), which herdr, looking up the host's PID,
never finds. babysit-bwrap binds ``sessions`` beside LISTEN over CONFIG_DIR's, so each
launch's records are its own, and this process reads them there. Codex records none:
its session is the rollout its process holds open for writing, read from ``/proc``.
Only a session whose transcript lies in CONFIG_DIR's store is reported, the same check
a registration passes.

Prints ``ready`` once LISTEN accepts connections, then runs until its parent exits
(babysit-bwrap execs bwrap, so that is the sandbox), and removes LISTEN and its
directory.
"""

import contextlib
import json
import os
import re
import shutil
import signal
import socket
import stat as stat_module
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

# Its own deployed directory, which no sandbox can write; -I leaves it off sys.path.
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

import worker_api  # noqa: E402
import wt  # noqa: E402

READ_TIMEOUT = 30  # For a whole request, however slowly it arrives.
MIN_INTERVAL = 20
CONNECTIONS = 4
_forwarding = threading.Lock()
_last = dict.fromkeys(worker_api.ACTIONS, 0.0)
_slots = threading.BoundedSemaphore(CONNECTIONS)
SESSION_POLL = 2  # Seconds between looks at the session the sandbox's agent runs.
SESSION_DEPTH = 4  # bwrap, its init in the namespace, then the agent: two levels down.
MAX_RECORD = 65536
MAX_META = 1 << 20  # Codex's session_meta line carries its instructions.
ROLLOUT = re.compile(r"rollout-.+-([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})\.jsonl")
HERDR_TIMEOUT = 10
MAX_BACKOFF = 60  # Seconds between reports herdr keeps refusing.


def transcript(agent, config_dir, session):
    """The one transcript of ``session`` in this launch's configuration directory."""
    root = Path(os.path.realpath(config_dir))
    if agent == "claude":
        found = list((root / "projects").glob(f"*/{session}.jsonl"))
    else:
        found = list((root / "sessions").glob(f"*/*/*/rollout-*{session}.jsonl"))
    found = [Path(os.path.realpath(path)) for path in found]
    # Resolved, still exactly in the store: no link leads to another account's.
    if len(found) != 1 or not (
        found[0].parent.parent == root / "projects"
        if agent == "claude"
        else found[0].is_relative_to(root / "sessions")
    ):
        raise ValueError(f"Session {session} is not one of this sandbox's own")
    return str(found[0])


def check(request, agent, config_dir, worktrees, launch=None):
    """The action and request to forward, or ValueError: this launch's own session and
    checkout. ``launch`` is this launch's ``cwd`` and ``docker`` grant."""
    action = request.pop("action", "register") if isinstance(request, dict) else None
    if action in {"launch", "follow-up"}:
        sessions = request.get("sessions")
        fields = request.get("launch")
        if action == "follow-up":
            if set(request) != {"target", "text", "sessions"}:
                raise ValueError("Expected exactly the follow-up fields")
            fields = {"target": request["target"], "text": request["text"]}
        elif (
            set(request) != {"launch", "sessions"}
            or not isinstance(fields, dict)
            or set(fields) - {"tags", "review"} != set(wt.LAUNCH_FIELDS)
        ):
            raise ValueError("Expected exactly the launch fields")
        if not launch or not launch.get("cwd"):
            raise ValueError("This sandbox's launch cannot start tasks")
        try:
            if not isinstance(sessions, dict):
                raise ValueError("Invalid sessions")
            session = str(uuid.UUID(sessions.get(agent)))
        except (TypeError, ValueError, AttributeError) as exc:
            raise ValueError(f"Start tasks from this sandbox's own {agent} conversation") from exc
        return action, {
            **({"launch": fields} if action == "launch" else fields),
            "parent": {
                "cwd": launch["cwd"],
                "session": session,
                "agent": agent,
                "config_dir": os.path.realpath(config_dir),
                "rollout": transcript(agent, config_dir, session),
                "docker": launch["docker"],
            },
        }
    if action == "handoff":
        sessions = request.get("sessions")
        if set(request) != worker_api.HANDOFF or not isinstance(sessions, dict):
            raise ValueError("Expected exactly the handoff fields")
        asked = sessions.get(agent)
    elif action != "register" or set(request) != worker_api.REGISTER:
        raise ValueError("Expected exactly the registration fields")
    else:
        if request["agent"] != agent:
            raise ValueError(f"This sandbox runs {agent}")
        asked = request["session"]
    cwd = request["cwd"]
    if not isinstance(cwd, str) or os.path.realpath(cwd) not in worktrees:
        raise ValueError("Ask from the worktree this sandbox was given")
    try:
        session = str(uuid.UUID(asked))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("Supply the exact --session UUID") from exc
    found = transcript(agent, config_dir, session)
    checked = {**request, "cwd": os.path.realpath(cwd)}
    if action == "handoff":
        # Only this launch's own conversation, with the transcript found for it here.
        other = "codex" if agent == "claude" else "claude"
        return action, {**checked, "sessions": {agent: session, other: None}, "rollout": found}
    rollout = request["rollout"]
    if rollout is not None and (not isinstance(rollout, str) or os.path.realpath(rollout) != found):
        raise ValueError(f"Session {session} is not one of this sandbox's own")
    return action, {
        **checked,
        "session": session,
        "rollout": found,
        "claude_account": None,
        "config_dir": os.path.realpath(config_dir),
    }


def answer(client, agent, config_dir, worktrees, launch=None):
    with client:
        action = None
        try:
            deadline = time.monotonic() + READ_TIMEOUT
            data = b""
            while len(data) <= worker_api.MAX_BODY:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise ValueError("The request took too long to arrive")
                client.settimeout(left)
                chunk = client.recv(65536)
                if not chunk:
                    break
                data += chunk
            if len(data) > worker_api.MAX_BODY:
                raise ValueError("Request too large")
            action, request = check(json.loads(data), agent, config_dir, worktrees, launch)
            client.settimeout(None)
            result = forward(action, request)
        except RuntimeError as exc:
            # Sent, but unanswered: a launch may have started or a message been delivered.
            result = {
                "error": str(exc),
                **({"uncertain": True} if action in {"launch", "follow-up"} else {}),
            }
        except (ValueError, OSError) as exc:
            result = {"error": str(exc)}
        with contextlib.suppress(OSError):
            client.sendall(json.dumps(result).encode())


def forward(action, request):
    if not _forwarding.acquire(blocking=False):
        raise ValueError("A request from this sandbox is already running")
    try:
        wait = _last[action] + MIN_INTERVAL - time.monotonic()
        if wait > 0:
            raise ValueError(f"Sent a {action} moments ago; wait {wait:.0f} s")
        _last[action] = time.monotonic()
        return worker_api.post(action, request)
    finally:
        _forwarding.release()


def serve(client, agent, config_dir, worktrees, launch=None):
    try:
        answer(client, agent, config_dir, worktrees, launch)
    finally:
        _slots.release()


def _children(proc, pid):
    found = []
    for listed in (proc / str(pid) / "task").glob("*/children"):
        with contextlib.suppress(OSError, ValueError):
            found += [int(child) for child in listed.read_text().split()]
    return found


def _namespaced(proc, pid):
    """A process in a PID namespace of its own: (its PID there, its start time, its
    name), or None."""
    try:
        status = (proc / str(pid) / "status").read_text()
        stat = (proc / str(pid) / "stat").read_text()
    except OSError:
        return None
    lines = [line.split() for line in status.splitlines()]
    nspid: list[str] = next((line[1:] for line in lines if line[:1] == ["NSpid:"]), [])
    name = next((line[1] for line in lines if line[:1] == ["Name:"] and len(line) > 1), "")
    # The command name may hold spaces or parentheses; the fields follow its last ")".
    fields = stat[stat.rfind(")") + 2 :].split()
    if len(nspid) < 2 or len(fields) < 20:
        return None
    return nspid[-1], fields[19], name  # starttime is field 22 of stat


def _record(store, pid):
    """Claude's record of the process with this PID, or None. The sandbox writes the
    store: only a regular file is read, never through a link or from a pipe."""
    try:
        fd = os.open(store / f"{pid}.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    with os.fdopen(fd, "rb") as handle:
        if not stat_module.S_ISREG(os.fstat(fd).st_mode):
            return None
        try:
            record = json.loads(handle.read(MAX_RECORD))
        except (OSError, ValueError, RecursionError):
            return None
    return record if isinstance(record, dict) else None


def running_claude_session(parent, config_dir, store, proc=Path("/proc")):
    """The Claude session the sandbox under ``parent`` runs, as (ID, transcript), or None.

    Claude records it in ``store`` (``sessions`` of its configuration directory in the
    sandbox, this launch's own), as ``<pid>.json`` by the PID it has in the sandbox's
    namespace, with its start time. Only the outermost Claude is the launch's own, not
    one it runs; a session with no transcript in this launch's store is not reported.
    """
    level = _children(proc, parent)
    for _ in range(SESSION_DEPTH):
        below = []
        for pid in level:
            ids = _namespaced(proc, pid)
            if ids is None:
                below += _children(proc, pid)
                continue
            nspid, start, name = ids
            record = _record(store, nspid)
            if record is not None and str(record.get("procStart")) == start:
                try:
                    session = str(uuid.UUID(record.get("sessionId")))
                    return session, transcript("claude", config_dir, session)
                except (TypeError, ValueError, AttributeError):
                    return None
            if name != "claude":  # Not yet recorded: what it runs is not the launch's.
                below += _children(proc, pid)
        level = below
    return None


def _name(proc, pid):
    try:
        status = (proc / str(pid) / "status").read_text()
    except OSError:
        return ""
    lines = [line.split() for line in status.splitlines()]
    return next((line[1] for line in lines if line[:1] == ["Name:"] and len(line) > 1), "")


def _writing(proc, pid, fd):
    """Whether ``pid`` has its descriptor ``fd`` open for writing."""
    try:
        info = (proc / str(pid) / "fdinfo" / fd).read_text()
        flags = next(line.split()[1] for line in info.splitlines() if line.startswith("flags:"))
        return int(flags, 8) & os.O_ACCMODE != os.O_RDONLY
    except (OSError, StopIteration, IndexError, ValueError):
        return False


def _rollout_meta(path, held):
    """The ``session_meta`` a rollout opens with, read only when ``path`` is the regular
    file ``held`` is, never through a link or from a pipe; else None."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    with os.fdopen(fd, "rb") as handle:
        status = os.fstat(fd)
        if not stat_module.S_ISREG(status.st_mode) or not os.path.samestat(status, held):
            return None
        try:
            item = json.loads(handle.readline(MAX_META))
        except (OSError, ValueError, RecursionError):
            return None
    if not isinstance(item, dict) or item.get("type") != "session_meta":
        return None
    return item["payload"] if isinstance(item.get("payload"), dict) else None


def _held_rollouts(proc, pid, config_dir):
    """The conversations ``pid`` writes the rollouts of in this launch's store, as
    {(ID, transcript)}: not a subagent's, nor one Codex runs for itself."""
    try:
        fds = list((proc / str(pid) / "fd").iterdir())
    except OSError:
        return set()
    found = set()
    for fd in fds:
        try:
            match = ROLLOUT.fullmatch(os.path.basename(os.readlink(fd)))
            if match is None or not _writing(proc, pid, fd.name):
                continue
            session = match[1]
            path = transcript("codex", config_dir, session)
            # The file it has open, whatever name the sandbox gives it now.
            meta = _rollout_meta(path, os.stat(fd))
        except (OSError, ValueError):
            continue
        if meta is None or meta.get("id") != session:
            continue
        source = meta.get("source")
        if not (isinstance(source, dict) and source.keys() & {"subagent", "internal"}):
            found.add((session, path))
    return found


def running_codex_session(parent, config_dir, proc=Path("/proc")):
    """The Codex session the sandbox under ``parent`` runs, as (ID, transcript), or None.

    Codex keeps the rollout of the conversation it runs open for appending, in the
    ``sessions`` of its home, from its first write until it moves to another. Only the
    outermost Codex is the launch's own, not one it runs; a rollout outside this
    launch's store is not reported, nor are two at once.
    """
    level = _children(proc, parent)
    for _ in range(SESSION_DEPTH):
        below = []
        for pid in level:
            held = _held_rollouts(proc, pid, config_dir)
            if held:
                return held.pop() if len(held) == 1 else None
            # npm's codex runs the native one, which is named codex. One with nothing
            # written yet: what it runs is not the launch's.
            if _name(proc, pid) != "codex":
                below += _children(proc, pid)
        level = below
    return None


def tell_herdr(agent, pane, session, path):
    """Report the session to herdr as its hook for ``agent`` does; whether herdr took it."""
    # herdr takes a session report only from the source that holds the pane, and its
    # detection holds it as herdr:<agent>. Only the session is reported, as the hooks
    # do, so herdr still reads the agent's state from its screen.
    command = [
        os.environ.get("HERDR_BIN_PATH") or "herdr",
        "pane",
        "report-agent-session",
        f"--source=herdr:{agent}",
        f"--agent={agent}",
        f"--seq={time.time_ns()}",
        f"--agent-session-id={session}",
        f"--agent-session-path={path}",
        pane,
    ]
    try:
        done = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=HERDR_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def report_sessions(agent, parent, config_dir, store, proc=Path("/proc"), running=None):
    """Tell herdr the sandbox's session, and again whenever it changes (a /clear, a
    /resume, a /new), while ``running()``: by default, until the sandbox exits. Outside a
    herdr pane there is no one to tell; a refusal is tried again less and less often."""
    pane = os.environ.get("HERDR_PANE_ID")
    if os.environ.get("HERDR_ENV") != "1" or not pane:
        return
    running = running or (lambda: os.getppid() == parent)
    told = None
    refusals = 0
    while running():
        try:
            found = (
                running_claude_session(parent, config_dir, store, proc)
                if agent == "claude"
                else running_codex_session(parent, config_dir, proc)
            )
            if found and found != told:
                if tell_herdr(agent, pane, *found):
                    told, refusals = found, 0
                else:
                    refusals += 1
        except Exception:  # noqa: BLE001 - what the sandbox writes must not end the reports.
            pass
        time.sleep(min(SESSION_POLL * 2 ** min(refusals, 5), MAX_BACKOFF))


def main(argv):
    argv = list(argv)
    launch = {"cwd": None, "docker": False}
    while argv and argv[0].startswith("--"):
        name, _, value = argv.pop(0).partition("=")
        if name == "--cwd" and value.startswith("/"):
            launch["cwd"] = os.path.realpath(value)
        elif name == "--granted":
            launch["docker"] = "docker" in value.split(",")
        else:
            raise SystemExit(f"worker_broker: unknown option {name}")
    listen, agent, config_dir, *worktrees = argv
    if agent not in {"claude", "codex"}:
        raise SystemExit(f"worker_broker: unknown agent {agent}")
    granted = {os.path.realpath(path) for path in worktrees}
    # Beside LISTEN, babysit-bwrap's per-launch directory over CONFIG_DIR/sessions.
    store = Path(os.path.dirname(listen)) / "sessions"
    # Out of the terminal's session: its signals and the terminal itself are not ours.
    with contextlib.suppress(OSError):
        os.setsid()
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    parent = os.getppid()
    server = socket.socket(socket.AF_UNIX)
    try:
        server.bind(listen)
        server.listen()
        server.settimeout(1)
        print("ready", flush=True)
        os.dup2(os.open(os.devnull, os.O_WRONLY), 1)
        threading.Thread(
            target=report_sessions, args=(agent, parent, config_dir, store), daemon=True
        ).start()
        while os.getppid() == parent:
            try:
                client, _ = server.accept()
            except TimeoutError:
                continue
            except OSError:
                time.sleep(0.5)  # Out of descriptors, say: the next accept may work.
                continue
            if not _slots.acquire(blocking=False):
                client.close()  # Enough connections already.
                continue
            threading.Thread(
                target=serve, args=(client, agent, config_dir, granted, launch), daemon=True
            ).start()
    finally:
        server.close()
        with contextlib.suppress(OSError):
            os.unlink(listen)
        # The sandbox's session records: the directory is this launch's, and gone with it.
        shutil.rmtree(store, ignore_errors=True)
        with contextlib.suppress(OSError):
            os.rmdir(os.path.dirname(listen))


if __name__ == "__main__":
    main(sys.argv[1:])
