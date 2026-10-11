"""Restarting the watcher daemon, and the dashboard itself, from the dashboard page.

Both services live outside the page: the daemon is a detached process that records
itself in ``daemon.json``, and the dashboard is normally a launchd job that starts
again whenever it exits. A terminal used to be the only way to recover a wedged
watcher, or to put a dashboard on the checkout's current code, which is exactly what
a phone over the tailnet does not have.
"""

import functools
import json
import os
import re
import signal
import subprocess
import threading
import time
from pathlib import Path

SUPERVISOR = Path(__file__).resolve().parent / "pr_supervisor.py"
# The daemon's loop sleeps a second at a time; a stop waits past any poll it is inside.
STOP_TIMEOUT = 15
# ``start_daemon`` returns once the new process holds the lock, a moment before it
# writes the pid file the answer reports.
START_TIMEOUT = 10
DEFAULT_WORKERS = 2
# This dashboard process: the module is imported while the dashboard starts.
STARTED_AT = time.time()
PID_LINE = re.compile(r"^\s*pid = (\d+)$", re.M)


def read_state(path):
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def process_args(pid):
    """A running process's command line, or "" once it is gone or unreadable."""
    try:
        proc = subprocess.run(
            ["ps", "-p", str(pid), "-o", "args="], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout if proc.returncode == 0 else ""


def running_pid(home):
    """The pid in ``daemon.json`` when that process really is a watcher daemon.

    A pid file outlives the process it names and the number is handed out again, so
    nothing is signalled before the command line says what the pid is now.
    """
    pid = read_state(home / "daemon.json").get("pid")
    if not isinstance(pid, int) or pid <= 1 or pid == os.getpid():
        return None
    return pid if SUPERVISOR.name in process_args(pid) else None


def idle(home):
    """True while no process holds this home's supervisor lock."""
    from pr_supervisor import locked

    try:
        with locked(home / "supervisor.lock"):
            return True
    except BlockingIOError:
        return False


def stop_daemon(home):
    """Stop the watcher daemon. Repairs it started are detached and keep running."""
    pid = running_pid(home)
    if pid is None:
        if idle(home):
            return
        raise RuntimeError(
            "A watcher holds the lock but its process cannot be identified; stop it in a terminal"
        )
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass  # It exited between the check and the signal: the lock is free or freeing.
    except PermissionError:
        raise RuntimeError(f"Not allowed to stop the watcher (pid {pid})") from None
    deadline = time.monotonic() + STOP_TIMEOUT
    while not idle(home):
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"The watcher did not stop within {STOP_TIMEOUT} seconds; stop it in a terminal"
            )
        time.sleep(0.2)


def restart_daemon(home):
    """Replace the watcher daemon with a new one, keeping how many repairs it runs."""
    from pr_supervisor import start_daemon

    before = read_state(home / "daemon.json")
    workers = before.get("max_workers")
    stop_daemon(home)
    start_daemon(home, workers if isinstance(workers, int) and workers > 0 else DEFAULT_WORKERS)
    deadline = time.monotonic() + START_TIMEOUT
    while True:
        state = read_state(home / "daemon.json")
        if state.get("pid") and state.get("started_at") != before.get("started_at"):
            return state
        if time.monotonic() >= deadline:
            raise RuntimeError("The watcher started without recording itself; read the service log")
        time.sleep(0.2)


@functools.cache
def launchd_label():
    """The launchd job running this dashboard, or None when nothing would start it again.

    ``XPC_SERVICE_NAME`` names the job in a LaunchAgent's environment, but everything
    the job starts inherits it, so launchd is asked which pid holds that label: only
    the job itself may exit expecting to be started again. The answer cannot change
    while the process lives, so it is asked for once.
    """
    label = os.environ.get("XPC_SERVICE_NAME", "")
    if "." not in label:
        return None
    try:
        proc = subprocess.run(
            ["launchctl", "print", f"gui/{os.getuid()}/{label}"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    found = PID_LINE.search(proc.stdout)
    return label if found and int(found.group(1)) == os.getpid() else None


def dashboard_state():
    return {"pid": os.getpid(), "started_at": STARTED_AT, "service": launchd_label()}


def check_restartable():
    """Refuse a restart that would leave the user with no dashboard at all."""
    if launchd_label() is None:
        raise ValueError(
            "This dashboard is not a launchd job, so nothing would start it again. "
            "Restart it where you started it."
        )


def restart_dashboard(server):
    """Stop serving, so launchd starts the dashboard again on the checkout's code.

    The request has already been answered: that response goes out while the server
    stops, and the page waits for the new process to answer before reloading.
    """
    server.restart = True
    threading.Thread(target=server.shutdown, daemon=True).start()
