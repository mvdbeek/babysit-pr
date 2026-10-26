"""Waiting for a test's child processes to end, whoever reaps them.

A process killed with its group, or left by an exited parent, is a zombie until its new
parent reaps it, and ``os.kill(pid, 0)`` still finds a zombie. On a loaded CI runner that
can take a while, so a zombie counts as gone.
"""

import os
import subprocess
import time


def gone(pid):
    """Whether ``pid`` has exited: no such process, or a zombie not reaped yet."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False  # Not this test's to see; never taken for gone.
    try:
        with open(f"/proc/{pid}/stat") as stream:
            return stream.read().rpartition(")")[2].split()[0] == "Z"
    except FileNotFoundError:
        if os.path.isdir("/proc/self"):
            return True
    except OSError:
        return False
    shown = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, timeout=10
    )
    return shown.returncode != 0 or shown.stdout.strip().startswith("Z")


def wait_gone(pid, seconds=5):
    """Whether ``pid`` exits within ``seconds``."""
    deadline = time.monotonic() + seconds
    while not gone(pid):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)
    return True
