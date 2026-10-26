"""Process groups owned by bounded, non-interactive commands.

Do not use this for detached services or terminal sessions intended to outlive a
command. Children that deliberately create another session/group are outside
this ownership boundary.
"""

import contextlib
import os
import selectors
import signal
import subprocess
import threading
import time

# Bytes of stderr a bounded run keeps: the end of it, where the reason usually is.
STDERR_TAIL = 64 * 1024


class OutputLimitExceeded(subprocess.SubprocessError):
    """A bounded run's stdout grew past its cap; the process group was stopped."""

    def __init__(self, args, limit):
        super().__init__(f"Output exceeded {limit} bytes")
        self.cmd = args
        self.limit = limit


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
            # Input still buffered for the stopped group has nowhere to go; flushing it
            # in Popen.__exit__ would raise BrokenPipeError over the reason it stopped.
            if proc.stdin:
                with contextlib.suppress(OSError, ValueError):
                    proc.stdin.close()


def _feed(proc, data):
    try:
        proc.stdin.write(data)
        proc.stdin.close()
    except (OSError, ValueError):
        pass  # The group was stopped, or the child stopped reading; its exit says why.


def _bounded(proc, input, timeout, limit):
    """Read stdout up to ``limit`` bytes and the last STDERR_TAIL bytes of stderr.

    Never holds more than that in memory: past the cap it raises and ``command`` stops
    the whole group, instead of buffering whatever the child keeps writing.
    """
    deadline = None if timeout is None else time.monotonic() + timeout
    if proc.stdin:
        if input:
            threading.Thread(target=_feed, args=(proc, input), daemon=True).start()
        else:
            proc.stdin.close()
    stdout, stderr = bytearray(), bytearray()
    with selectors.DefaultSelector() as selector:
        selector.register(proc.stdout, selectors.EVENT_READ, stdout)
        selector.register(proc.stderr, selectors.EVENT_READ, stderr)
        while selector.get_map():
            left = None if deadline is None else deadline - time.monotonic()
            if left is not None and left <= 0:
                raise subprocess.TimeoutExpired(proc.args, timeout, bytes(stdout), bytes(stderr))
            for key, _ in selector.select(left):
                chunk = os.read(key.fd, 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                key.data.extend(chunk)
                if key.data is stdout and len(stdout) > limit:
                    raise OutputLimitExceeded(proc.args, limit)
                if key.data is stderr:
                    del stderr[:-STDERR_TAIL]
    left = None if deadline is None else max(0.0, deadline - time.monotonic())
    try:
        proc.wait(timeout=left)
    except subprocess.TimeoutExpired:
        raise subprocess.TimeoutExpired(proc.args, timeout, bytes(stdout), bytes(stderr)) from None
    return bytes(stdout), bytes(stderr)


def run(args, *, timeout, input=None, max_output=None, **kwargs):
    """Capture a command's output; ``max_output`` caps stdout bytes as they arrive.

    A bounded run is binary only and keeps the tail of stderr; past the cap it raises
    ``OutputLimitExceeded`` once the group is stopped.
    """
    if input is not None:
        if "stdin" in kwargs:
            raise ValueError("stdin and input arguments may not both be used")
        kwargs["stdin"] = subprocess.PIPE
    if max_output is not None and {"text", "encoding", "errors", "universal_newlines"} & set(
        kwargs
    ):
        raise ValueError("Bounded output is read as bytes")
    with command(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs) as proc:
        if max_output is None:
            stdout, stderr = proc.communicate(input=input, timeout=timeout)
        else:
            stdout, stderr = _bounded(proc, input, timeout, max_output)
        return subprocess.CompletedProcess(args, proc.returncode, stdout, stderr)
