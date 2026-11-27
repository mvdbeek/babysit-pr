#!/usr/bin/env python3
"""Temporary credential files for a worker launch. The control socket stays on the host."""

import base64
import contextlib
import json
import os
import re
import shutil
import socket
import stat
import sys
import tempfile
import time
import uuid
from pathlib import Path

MOUNT = "/run/babysit-credentials"
ROOT = Path("/dev/shm") / f"babysit-credentials-{os.getuid()}"
MAX_SECRET = 64 * 1024
MAX_REQUEST = 2 * MAX_SECRET
REVOKED_TTL = 60  # Keep cancelled IDs through any delayed SSH delivery.
GRANT_ID = re.compile(r"[0-9a-f]{32}")
RUN_ID = re.compile(r"[A-Za-z0-9]{10}")
UNAVAILABLE = (
    "Could not grant access to this session. Make sure the Linux worker is up to date, "
    "then quit the agent and resume the conversation in a sandbox with Docker access off. "
    "Try granting the credential again."
)


def valid_duration(minutes: object) -> bool:
    return type(minutes) is int and minutes > 0


def private_root(root):
    root.mkdir(mode=0o700, exist_ok=True)
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise OSError("The credential root must be a private directory")


class Vault:
    """The worker broker's per-launch files, available read-only inside one sandbox."""

    def __init__(self, root, run_id, panes, current_session, clock=time.monotonic):
        self.directory = root / run_id
        self.files = self.directory / "files"
        self.current_session = current_session
        self.clock = clock
        self.grants = {}
        self.revoked = {}
        if not RUN_ID.fullmatch(run_id):
            raise ValueError("Invalid sandbox run ID")
        private_root(root)
        self.directory.mkdir(mode=0o700)
        self.socket = socket.socket(socket.AF_UNIX)
        try:
            self.files.mkdir(mode=0o700)
            self.socket.bind(str(self.directory / "control"))
            self.socket.listen(4)
            self.socket.settimeout(0.5)
            (self.directory / "panes").write_text(json.dumps(panes))
        except OSError:
            self.close()
            raise

    def close(self):
        self.socket.close()
        shutil.rmtree(self.directory, ignore_errors=True)

    def expire(self):
        now = self.clock()
        for grant, deadline in list(self.revoked.items()):
            if deadline <= now:
                del self.revoked[grant]
        if self.grants:
            session = self.current_session()
            for grant, (owner, started, seconds) in list(self.grants.items()):
                if owner != session or now - started >= seconds:
                    self.remove(grant)

    def remove(self, grant):
        if grant in self.grants:
            (self.files / grant).unlink(missing_ok=True)
            del self.grants[grant]

    def handle(self, request):
        action, session = request.get("action"), request.get("session")
        if action == "check":
            return {"matches": session is not None and session == self.current_session()}
        grant = request.get("grant")
        if not isinstance(grant, str) or not GRANT_ID.fullmatch(grant):
            raise ValueError
        if action == "revoke":
            if (owner := self.grants.get(grant)) and owner[0] == session:
                self.remove(grant)
            self.revoked[grant] = self.clock() + REVOKED_TTL
            return {"ok": True}
        minutes = request.get("minutes")
        if action != "grant" or not valid_duration(minutes):
            raise ValueError
        data = base64.b64decode(request["data"], validate=True)
        if (
            not 0 < len(data) <= MAX_SECRET
            or grant in self.grants
            or grant in self.revoked
            or len(self.grants) >= 32
        ):
            raise ValueError
        if session is None or session != self.current_session():
            return {"matches": False}
        # Write outside the bind, then publish a completed read-only file in one rename.
        with tempfile.NamedTemporaryFile(dir=self.directory, delete=False) as stream:
            temporary = Path(stream.name)
            try:
                stream.write(data)
                stream.flush()
                os.fchmod(stream.fileno(), 0o400)
                os.replace(temporary, self.files / grant)
                self.grants[grant] = (session, self.clock(), minutes * 60)
            finally:
                temporary.unlink(missing_ok=True)
        return {"ok": True}

    def serve(self, stopping, running):
        try:
            while running() and not stopping.is_set():
                self.expire()
                try:
                    client, _ = self.socket.accept()
                except TimeoutError:
                    continue
                with client, client.makefile("rb") as reader:
                    client.settimeout(1)
                    try:
                        raw = reader.readline(MAX_REQUEST + 1)
                        if len(raw) > MAX_REQUEST or not raw.endswith(b"\n"):
                            raise ValueError
                        result = self.handle(json.loads(raw))
                    except (ValueError, KeyError, AttributeError, TypeError, OSError):
                        result = {"error": "The worker refused the credential operation"}
                    with contextlib.suppress(OSError):
                        client.sendall(json.dumps(result).encode() + b"\n")
        finally:
            self.close()


def exchange(path, request):
    try:
        with socket.socket(socket.AF_UNIX) as client, client.makefile("rb") as reader:
            client.settimeout(2)
            client.connect(str(path))
            client.sendall(json.dumps(request).encode() + b"\n")
            value = json.loads(reader.readline(1024))
            return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def deliver(root, pane, session, action, grant, minutes, data):
    private_root(root)
    candidates = []
    for marker in root.glob("*/panes"):
        if not RUN_ID.fullmatch(marker.parent.name):
            continue
        try:
            if pane in json.loads(marker.read_text()):
                candidates.append(marker.with_name("control"))
        except (OSError, ValueError, TypeError):
            continue
    request = {"action": action, "session": session, "grant": grant}
    if action == "revoke":
        # The session may already have changed. The old owner is still part of the request.
        for path in candidates:
            exchange(path, request)
        return {"ok": True}
    matches = [
        path
        for path in candidates
        if exchange(path, {"action": "check", "session": session}).get("matches")
    ]
    if len(matches) != 1:
        return {"error": UNAVAILABLE}
    request.update(minutes=minutes, data=base64.b64encode(data).decode("ascii"))
    return exchange(matches[0], request)


def main(argv):
    # The controller sends the credential over SSH stdin. The command line holds metadata.
    try:
        action, pane, session, grant, minutes = argv
        if action not in {"grant", "revoke"} or session != str(uuid.UUID(session)):
            raise ValueError
        data = sys.stdin.buffer.read(MAX_SECRET + 1) if action == "grant" else b""
        if action == "grant" and not 0 < len(data) <= MAX_SECRET:
            raise ValueError
        result = deliver(ROOT, pane, session, action, grant, int(minutes), data)
    except (OSError, ValueError):
        result = {"error": "The worker could not manage the credential grant"}
    # Emit only a predefined outcome. The credential never leaves this process on stdout.
    print(
        json.dumps(
            {"ok": True} if result.get("ok") else {"error": result.get("error", UNAVAILABLE)}
        )
    )


if __name__ == "__main__":
    main(sys.argv[1:])
