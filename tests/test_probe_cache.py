"""The probe a worker runs: its loader, bytecode cache and thread helper."""

import base64
import hashlib
import hmac
import json
import marshal
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
import remote_probe


def run(home):
    """One probe run as ``call`` sends it, of an op it does not know."""
    request = base64.b64encode(json.dumps({"op": "none", "args": {}}).encode()).decode()
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-", remote_probe.PROTOCOL, request],
        input=remote_probe.probe_loader(home),
        capture_output=True,
        check=True,
    )
    return json.loads(result.stdout)


def cached():
    return sorted(Path(os.environ["BABYSIT_PROBE_CACHE"]).iterdir())


def signed(home, path, program):
    """``program`` compiled and signed as the controller of ``home`` would sign it."""
    body = marshal.dumps(compile(program, "planted", "exec"))
    key = (Path(home) / remote_probe.PROBE_KEY).read_bytes()
    label = (path.name + "\n").encode()
    return hmac.new(key, label + body, "sha256").digest() + body


ANSWER = {"protocol": remote_probe.PROTOCOL, "error": "ValueError: Unknown probe op"}
PLANTED = "import sys; sys.stdout.write('{\"planted\": true}')"


def test_a_probe_is_compiled_once_then_runs_from_its_signed_cache(tmp_path):
    assert run(tmp_path) == ANSWER
    [path] = cached()
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    # What the cache holds is what runs, once the controller's key signed it.
    path.write_bytes(signed(tmp_path, path, PLANTED))
    assert run(tmp_path) == {"planted": True}


def test_bytecode_the_key_did_not_sign_never_runs_and_is_replaced(tmp_path):
    run(tmp_path)
    [path] = cached()
    marker = tmp_path / "PWNED"
    program = f"open({str(marker)!r}, 'w').close()"
    other = tmp_path / "other-home"
    run(other)  # Another controller's key, so its signature does not count here.
    for planted in (
        b"\0" * 32 + marshal.dumps(compile(program, "planted", "exec")),
        signed(other, path, program),
        b"short",
    ):
        path.write_bytes(planted)
        assert run(tmp_path) == ANSWER and not marker.exists()
        assert path.read_bytes() != planted
    assert run(tmp_path) == ANSWER


def test_a_cache_folder_that_is_not_the_users_own_is_left_alone(tmp_path, monkeypatch):
    target = tmp_path / "elsewhere"
    target.mkdir()
    link = tmp_path / "linked"
    link.symlink_to(target)
    monkeypatch.setenv("BABYSIT_PROBE_CACHE", str(link))
    assert run(tmp_path) == ANSWER and list(target.iterdir()) == []


def test_a_new_probe_source_replaces_this_controllers_older_bytecode(tmp_path, monkeypatch):
    run(tmp_path)
    run(tmp_path / "other-home")
    [mine, theirs] = sorted(cached(), key=lambda p: p.stat().st_mtime_ns)
    source = remote_probe.probe_source() + b"\n# A new deploy.\n"
    monkeypatch.setattr(remote_probe, "probe_source", lambda: source)
    assert run(tmp_path) == ANSWER
    names = {p.name for p in cached()}
    assert mine.name not in names and theirs.name in names and len(names) == 2
    assert any(n.startswith(hashlib.sha256(source).hexdigest()[:32]) for n in names)


def test_the_controller_key_is_made_once_privately_and_a_broken_one_replaced(tmp_path):
    key = remote_probe.probe_key(tmp_path)
    path = tmp_path / remote_probe.PROBE_KEY
    assert len(key) == 32 and path.stat().st_mode & 0o777 == 0o600
    assert remote_probe.probe_key(tmp_path) == key
    path.write_bytes(b"short")
    assert remote_probe.probe_key(tmp_path) not in (key, b"short")
    assert list(tmp_path.iterdir()) == [path]


def test_parallel_map_keeps_order_and_raises_the_first_failure_in_order():
    assert remote_probe.parallel_map(lambda n: n * n, range(20), workers=4) == [
        n * n for n in range(20)
    ]
    assert remote_probe.parallel_map(str, []) == []
    started = []
    lock = threading.Lock()

    def work(n):
        with lock:
            started.append(n)
        if n in (3, 1):
            time.sleep(0.05 if n == 1 else 0)
            raise ValueError(f"item {n}")
        return n

    with pytest.raises(ValueError, match="item 1"):
        remote_probe.parallel_map(work, range(50), workers=2)
    # None start once a failure is seen.
    assert len(started) < 50
