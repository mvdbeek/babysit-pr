"""Restarting the watcher daemon and the dashboard, against isolated state only.

Every daemon here runs in a temporary home with an empty queue, so it polls nothing
and starts no agent. Process inspection sees only children started by the test and
does not require host ``ps`` access. The launchd checks use a fake ``launchctl`` on PATH.
"""

import json
import os
import subprocess
import sys
import time

import pr_supervisor as supervisor
import pytest
import service_control

LAUNCHCTL = """#!/bin/sh
echo "$@" >> "$FAKE_LAUNCHCTL_CALLS"
printf '%s = {\\n\\tstate = running\\n\\tpid = %s\\n}\\n' "$2" "$FAKE_LAUNCHCTL_PID"
"""


@pytest.fixture(autouse=True)
def isolated_process_table(monkeypatch):
    """Keep real child lifecycles, with ps limited to this test's subprocesses."""
    children = {}
    popen = subprocess.Popen
    run = subprocess.run

    def start(argv, *args, **kwargs):
        child = popen(argv, *args, **kwargs)
        children[child.pid] = child
        return child

    def inspect(argv, *args, **kwargs):
        if argv[0] != "ps":
            return run(argv, *args, **kwargs)
        assert argv == ["ps", "-p", argv[2], "-o", "args="]
        child = children.get(int(argv[2]))
        alive = child is not None and child.poll() is None
        return subprocess.CompletedProcess(
            argv, 0 if alive else 1, " ".join(child.args) + "\n" if alive else "", ""
        )

    monkeypatch.setattr(subprocess, "Popen", start)
    monkeypatch.setattr(subprocess, "run", inspect)
    yield
    for child in children.values():
        if child.poll() is None:
            child.kill()
        child.wait()


@pytest.fixture
def daemon(tmp_path):
    """A real supervisor in a temporary home, always stopped again."""

    def start():
        supervisor.start_daemon(tmp_path, 2)
        return wait_for(lambda: recorded(tmp_path))

    return start


def recorded(home):
    """The daemon's own record of itself, once the new process has written it."""
    state = service_control.read_state(home / "daemon.json")
    return state if state.get("pid") else None


def wait_for(read, timeout=10):
    deadline = time.monotonic() + timeout
    while True:
        value = read()
        if value:
            return value
        assert time.monotonic() < deadline, "The daemon never recorded itself"
        time.sleep(0.05)


@pytest.fixture
def fake_launchd(tmp_path, monkeypatch):
    """Answer ``launchctl print`` with a pid this test chooses."""
    binary = tmp_path / "bin"
    binary.mkdir()
    (binary / "launchctl").write_text(LAUNCHCTL)
    (binary / "launchctl").chmod(0o755)
    calls = tmp_path / "launchctl-calls"
    monkeypatch.setenv("PATH", f"{binary}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_LAUNCHCTL_CALLS", str(calls))

    def configure(label, pid):
        service_control.launchd_label.cache_clear()
        monkeypatch.setenv("XPC_SERVICE_NAME", label)
        monkeypatch.setenv("FAKE_LAUNCHCTL_PID", str(pid))
        return calls

    yield configure
    service_control.launchd_label.cache_clear()


def test_a_pid_file_naming_another_program_is_never_signalled(tmp_path):
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        (tmp_path / "daemon.json").write_text(json.dumps({"pid": other.pid, "max_workers": 2}))
        assert service_control.running_pid(tmp_path) is None
        service_control.stop_daemon(tmp_path)  # Nothing holds the lock: nothing to stop.
        assert other.poll() is None
    finally:
        other.kill()
        other.wait()


def test_a_stale_pid_file_leaves_nothing_to_stop(tmp_path):
    gone = subprocess.Popen([sys.executable, "-c", ""])
    gone.wait()
    (tmp_path / "daemon.json").write_text(json.dumps({"pid": gone.pid, "max_workers": 2}))
    assert service_control.running_pid(tmp_path) is None
    service_control.stop_daemon(tmp_path)


def test_stopping_the_daemon_frees_its_lock(tmp_path, daemon):
    state = daemon()
    assert service_control.running_pid(tmp_path) == state["pid"]
    assert not service_control.idle(tmp_path)
    service_control.stop_daemon(tmp_path)
    assert service_control.idle(tmp_path)
    assert service_control.running_pid(tmp_path) is None


def test_restarting_the_daemon_replaces_it_and_keeps_its_workers(tmp_path, daemon):
    before = daemon()
    (tmp_path / "daemon.json").write_text(json.dumps({**before, "max_workers": 3}))
    after = service_control.restart_daemon(tmp_path)
    assert after["pid"] != before["pid"]
    assert after["max_workers"] == 3
    assert service_control.running_pid(tmp_path) == after["pid"]
    # The new daemon is really watching: it heartbeats within its first seconds.
    beat = wait_for(lambda: service_control.read_state(tmp_path / "heartbeat.json").get("pid"))
    assert beat == after["pid"]


def test_a_daemon_can_be_started_when_none_is_running(tmp_path, daemon):
    assert service_control.idle(tmp_path)
    state = service_control.restart_daemon(tmp_path)
    assert service_control.running_pid(tmp_path) == state["pid"]
    assert state["max_workers"] == service_control.DEFAULT_WORKERS


def test_an_unidentifiable_lock_holder_is_reported_not_killed(tmp_path, monkeypatch, daemon):
    daemon()
    monkeypatch.setattr(service_control, "process_args", lambda pid: "")
    with pytest.raises(RuntimeError, match="cannot be identified"):
        service_control.stop_daemon(tmp_path)


def test_launchd_ownership_needs_the_label_to_name_this_process(fake_launchd):
    calls = fake_launchd("net.sci-scale.babysitter-dashboard", os.getpid())
    assert service_control.launchd_label() == "net.sci-scale.babysitter-dashboard"
    assert service_control.dashboard_state()["service"] == "net.sci-scale.babysitter-dashboard"
    assert f"gui/{os.getuid()}/net.sci-scale.babysitter-dashboard" in calls.read_text()
    service_control.check_restartable()


def test_an_inherited_label_belonging_to_another_process_is_not_ownership(fake_launchd):
    fake_launchd("net.sci-scale.babysitter-dashboard", os.getpid() + 1)
    assert service_control.launchd_label() is None
    assert service_control.dashboard_state()["service"] is None
    with pytest.raises(ValueError, match="not a launchd job"):
        service_control.check_restartable()


def test_a_dashboard_started_by_hand_owns_no_job(fake_launchd, monkeypatch):
    fake_launchd("net.sci-scale.babysitter-dashboard", os.getpid())
    monkeypatch.delenv("XPC_SERVICE_NAME")
    service_control.launchd_label.cache_clear()
    assert service_control.launchd_label() is None


def test_the_launchd_answer_is_asked_for_once(fake_launchd):
    calls = fake_launchd("net.sci-scale.babysitter-dashboard", os.getpid())
    for _ in range(3):
        assert service_control.launchd_label()
    assert len(calls.read_text().splitlines()) == 1


def test_the_started_at_of_this_dashboard_is_its_own(tmp_path):
    state = service_control.dashboard_state()
    assert state["pid"] == os.getpid()
    assert 0 <= time.time() - state["started_at"] < 3600
