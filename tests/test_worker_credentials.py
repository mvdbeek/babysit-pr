"""The worker's private credential control socket and file lifecycle."""

import stat
import tempfile
import threading
import time
import uuid
from pathlib import Path

import worker_credentials


def test_a_grant_targets_one_live_launch_and_is_removed_on_expiry_or_session_change():
    with tempfile.TemporaryDirectory(prefix="bs-cred-", dir="/tmp") as tmp:
        root = Path(tmp)
        chosen = str(uuid.UUID(int=1))
        session = chosen
        clock = 0.0
        minutes = 47
        credential = b"fixture\ncredential"
        grant = uuid.uuid4().hex
        stop = threading.Event()
        target = worker_credentials.Vault(
            root, "launch0001", ["w1:p1"], lambda: session, lambda: clock
        )
        sibling = worker_credentials.Vault(root, "launch0002", ["w1:p2"], lambda: chosen)
        threads = [
            threading.Thread(target=vault.serve, args=(stop, lambda: True))
            for vault in (target, sibling)
        ]
        for thread in threads:
            thread.start()
        try:

            def deliver(action="grant", owner=chosen):
                return worker_credentials.deliver(
                    root, "w1:p1", owner, action, grant, minutes, credential
                )

            def wait_removed(file):
                for _ in range(50):
                    if not file.exists():
                        return
                    time.sleep(0.02)
                assert not file.exists()

            assert deliver(owner=str(uuid.UUID(int=2))) == {"error": worker_credentials.UNAVAILABLE}
            assert deliver() == {"ok": True}
            file = target.files / grant
            assert file.read_bytes() == credential
            assert stat.S_IMODE(file.stat().st_mode) == 0o400
            assert not list(sibling.files.iterdir())
            assert (target.directory / "control").exists()
            assert not (target.files / "control").exists()

            clock = minutes * 60 - 1
            for _ in range(2):
                assert worker_credentials.exchange(
                    target.directory / "control", {"action": "check", "session": chosen}
                ) == {"matches": True}
            assert file.exists()
            clock = minutes * 60
            wait_removed(file)
            assert deliver() == {"ok": True}
            session = str(uuid.UUID(int=2))
            wait_removed(file)
            assert deliver() == {"error": worker_credentials.UNAVAILABLE}
            assert deliver(owner=session) == {"ok": True}
            assert deliver("revoke", owner=session) == {"ok": True}
            assert not file.exists()
            assert "error" in deliver(owner=session)
            grant = uuid.uuid4().hex
            assert deliver(owner=session) == {"ok": True}
        finally:
            stop.set()
            for thread in threads:
                thread.join(3)
                assert not thread.is_alive()
        assert not target.directory.exists() and not sibling.directory.exists()
