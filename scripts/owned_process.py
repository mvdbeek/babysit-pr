"""Process groups owned by bounded, non-interactive commands.

Do not use this for detached services or terminal sessions intended to outlive a
command. Children that deliberately create another session/group are outside
this ownership boundary.
"""

import contextlib
import os
import signal
import subprocess


def stop_group(proc, grace=0):
    """Reap the child and stop its group even if the group leader already exited."""
    try:
        if grace:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                pass
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        # ESRCH once the leader is reaped; macOS answers EPERM while it is still a zombie.
        pass
    finally:
        proc.wait()


@contextlib.contextmanager
def command(args, **kwargs):
    # Cleanup must run before Popen.__exit__, which otherwise waits indefinitely
    # on errors, and must target only a group created by this invocation.
    with subprocess.Popen(args, start_new_session=True, **kwargs) as proc:
        try:
            yield proc
        finally:
            stop_group(proc)


def run(args, *, timeout, input=None, **kwargs):
    if input is not None:
        if "stdin" in kwargs:
            raise ValueError("stdin and input arguments may not both be used")
        kwargs["stdin"] = subprocess.PIPE
    with command(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs) as proc:
        stdout, stderr = proc.communicate(input=input, timeout=timeout)
        return subprocess.CompletedProcess(args, proc.returncode, stdout, stderr)
