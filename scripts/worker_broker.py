#!/usr/bin/env python3
"""Watch registration for one sandbox: only its own worktrees and its own sessions.

    worker_broker.py LISTEN AGENT CONFIG_DIR [WORKTREE...]

On a worker, babysit-bwrap starts this outside the sandbox and binds LISTEN inside,
naming it in ``BABYSIT_REGISTER_SOCKET``; the sandbox never sees ``worker.json``, whose
token this process holds. A client sends one registration request (``worker_api``'s
fields) and closes its side; the answer is the controller's, or a refusal. Forwarded
are only requests for the launch's ``AGENT`` whose ``cwd`` is one of the launch's
granted WORKTREEs and whose session transcript lies in its CONFIG_DIR (``projects/``
for Claude, ``sessions/`` for Codex): the transcript found there is what the
controller is told to check, and no Claude account is passed, so the controller takes
the account from where that transcript lies. An agent can therefore register only its
own sessions in its own checkouts, not another task's.

Prints ``ready`` once LISTEN accepts connections, then runs until its parent exits
(babysit-bwrap execs bwrap, so that is the sandbox), and removes LISTEN and its
directory.
"""

import contextlib
import json
import os
import signal
import socket
import sys
import threading
import uuid
from pathlib import Path

# Its own deployed directory, which no sandbox can write; -I leaves it off sys.path.
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

import worker_api  # noqa: E402

READ_TIMEOUT = 30


def transcript(agent, config_dir, session):
    """The one transcript of ``session`` in this launch's configuration directory."""
    root = Path(os.path.realpath(config_dir))
    if agent == "claude":
        found = list((root / "projects").glob(f"*/{session}.jsonl"))
    else:
        found = list((root / "sessions").glob(f"*/*/*/rollout-*{session}.jsonl"))
    found = [Path(os.path.realpath(path)) for path in found]
    if len(found) != 1 or not found[0].is_relative_to(root):
        raise ValueError(f"Session {session} is not one of this sandbox's own")
    return str(found[0])


def check(request, agent, config_dir, worktrees):
    """The request to forward, or ValueError: this launch's own session and checkout."""
    if not isinstance(request, dict) or set(request) != worker_api.REGISTER:
        raise ValueError("Expected exactly the registration fields")
    if request["agent"] != agent:
        raise ValueError(f"This sandbox runs {agent}")
    cwd = request["cwd"]
    if not isinstance(cwd, str) or os.path.realpath(cwd) not in worktrees:
        raise ValueError("Register a watch from the worktree this sandbox was given")
    try:
        session = str(uuid.UUID(request["session"]))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("Supply the exact --session UUID") from exc
    found = transcript(agent, config_dir, session)
    rollout = request["rollout"]
    if rollout is not None and (not isinstance(rollout, str) or os.path.realpath(rollout) != found):
        raise ValueError(f"Session {session} is not one of this sandbox's own")
    return {
        **request,
        "cwd": os.path.realpath(cwd),
        "session": session,
        "rollout": found,
        "claude_account": None,
    }


def answer(client, agent, config_dir, worktrees):
    with client:
        try:
            client.settimeout(READ_TIMEOUT)
            data = b""
            while len(data) <= worker_api.MAX_BODY:
                chunk = client.recv(65536)
                if not chunk:
                    break
                data += chunk
            if len(data) > worker_api.MAX_BODY:
                raise ValueError("Request too large")
            request = check(json.loads(data), agent, config_dir, worktrees)
            client.settimeout(None)
            result = worker_api.post("register", request)
        except (ValueError, RuntimeError, OSError) as exc:
            result = {"error": str(exc)}
        with contextlib.suppress(OSError):
            client.sendall(json.dumps(result).encode())


def main(argv):
    listen, agent, config_dir, *worktrees = argv
    if agent not in {"claude", "codex"}:
        raise SystemExit(f"worker_broker: unknown agent {agent}")
    granted = {os.path.realpath(path) for path in worktrees}
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
        while os.getppid() == parent:
            try:
                client, _ = server.accept()
            except TimeoutError:
                continue
            threading.Thread(
                target=answer, args=(client, agent, config_dir, granted), daemon=True
            ).start()
    finally:
        server.close()
        with contextlib.suppress(OSError):
            os.unlink(listen)
            os.rmdir(os.path.dirname(listen))


if __name__ == "__main__":
    main(sys.argv[1:])
