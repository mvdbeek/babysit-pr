"""Bounded helpers must not leave shells behind after exit, timeout, or errors."""

import contextlib
import os
import signal
import subprocess
import sys
import time

import owned_process
import pr_workspaces
import pytest


def wait_gone(pid):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.02)
    pytest.fail(f"Child shell {pid} survived command cleanup")


@pytest.fixture
def shell_child(tmp_path):
    """A fake CLI with a real, stubborn zsh child, and a cleanup backstop."""
    pidfile = tmp_path / "child.pid"
    helper = tmp_path / "helper.py"
    helper.write_text("""
import os, subprocess, sys, time
from pathlib import Path
child = subprocess.Popen([
    '/bin/zsh', '-dfc',
    'trap "" TERM; zmodload zsh/zselect; print -r -- $$ > "$1"; '
    'while true; do zselect -t 100; done',
    'fixture', sys.argv[1],
], stdin=subprocess.DEVNULL,
   stdout=subprocess.DEVNULL if sys.argv[2] == 'exit_closed' else None,
   stderr=subprocess.DEVNULL if sys.argv[2] == 'exit_closed' else None)
while not Path(sys.argv[1]).exists():
    time.sleep(0.01)
print('ready', flush=True)
if sys.argv[2] == 'hang':
    time.sleep(60)
""")
    yield helper, pidfile
    if pidfile.exists():
        with contextlib.suppress(ProcessLookupError):
            os.kill(int(pidfile.read_text()), signal.SIGKILL)


@pytest.mark.parametrize("mode", ["exit", "hang"])
def test_captured_command_cleans_children_inheriting_output(shell_child, mode):
    helper, pidfile = shell_child
    # Even after the leader exits, its child keeps the capture pipes open.
    with pytest.raises(subprocess.TimeoutExpired):
        pr_workspaces.run(sys.executable, helper, pidfile, mode, timeout=1)
    wait_gone(int(pidfile.read_text()))


def test_successful_captured_command_also_cleans_children(shell_child):
    helper, pidfile = shell_child
    assert pr_workspaces.run(sys.executable, helper, pidfile, "exit_closed", timeout=5) == "ready"
    wait_gone(int(pidfile.read_text()))


@pytest.mark.parametrize("mode", ["exit", "hang", "log_error"])
def test_logged_command_cleans_children_on_every_exit(tmp_path, shell_child, mode):
    helper, pidfile = shell_child
    manager = object.__new__(pr_workspaces.Workspaces)

    def save(op, **changes):
        if mode == "log_error":
            raise OSError("simulated database error")
        op.update(changes)

    manager.save_operation = save
    if mode == "hang":
        expected = pytest.raises(subprocess.TimeoutExpired)
    elif mode == "log_error":
        expected = pytest.raises(OSError, match="simulated database error")
    else:
        expected = contextlib.nullcontext()
    with expected:
        manager.run_logged(
            {"log": ""},
            sys.executable,
            helper,
            pidfile,
            "hang" if mode != "exit" else "exit",
            timeout=1,
        )
    wait_gone(int(pidfile.read_text()))


def test_group_cleanup_does_not_kill_unrelated_processes(tmp_path):
    with subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"]) as unrelated:
        try:
            result = owned_process.run(
                [sys.executable, "-c", "print('done')"],
                text=True,
                timeout=5,
            )
            assert result.stdout == "done\n" and result.returncode == 0
            assert unrelated.poll() is None
        finally:
            unrelated.kill()


def test_cleanup_tolerates_an_exited_but_unreaped_leader():
    """A caller may leave the block after EOF without waiting; the zombie is reaped quietly."""
    with owned_process.command(
        [sys.executable, "-c", "print('done')"], stdout=subprocess.PIPE, text=True
    ) as proc:
        assert proc.stdout is not None
        assert proc.stdout.read() == "done\n"
        deadline = time.monotonic() + 5
        while proc.returncode is None and time.monotonic() < deadline:
            # Poll the kernel state without reaping so cleanup meets the zombie itself.
            try:
                if os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOWAIT):
                    break
            except ChildProcessError:
                break
            time.sleep(0.01)
    assert proc.returncode == 0
