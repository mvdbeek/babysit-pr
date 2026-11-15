"""Waiting for a test's child processes to end, whoever reaps them.

A process killed with its group, or left by an exited parent, is a zombie until its new
parent reaps it, and ``os.kill(pid, 0)`` still finds a zombie. On a loaded CI runner that
can take a while, so a zombie counts as gone.
"""

import contextlib
import ctypes
import ctypes.util
import errno
import os
import subprocess
import sys
import time

PROC_PIDT_SHORTBSDINFO = 13
SZOMB = 5
_libproc = None
if sys.platform == "darwin":
    with contextlib.suppress(OSError):
        _libproc = ctypes.CDLL(ctypes.util.find_library("proc") or "libproc.dylib", use_errno=True)


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
    if _libproc is not None:
        return _zombie(pid)
    shown = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, timeout=10
    )
    return shown.returncode != 0 or shown.stdout.strip().startswith("Z")


def _zombie(pid):
    """macOS has no /proc, and an agent's sandbox may not run ps (it is setuid): the
    kernel is asked through libproc, which answers a zombie with no such process."""
    short = (ctypes.c_uint32 * 16)()  # struct proc_bsdshortinfo: pid, ppid, pgid, status.
    ctypes.set_errno(0)
    size = _libproc.proc_pidinfo(
        pid, PROC_PIDT_SHORTBSDINFO, ctypes.c_uint64(0), short, ctypes.sizeof(short)
    )
    if size <= 0:
        return ctypes.get_errno() == errno.ESRCH
    return short[3] == SZOMB


def wait_gone(pid, seconds=5):
    """Whether ``pid`` exits within ``seconds``."""
    deadline = time.monotonic() + seconds
    while not gone(pid):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)
    return True
