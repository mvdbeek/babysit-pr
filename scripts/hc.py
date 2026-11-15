"""Client for herdr-core (``hc``), the terminal runtime that replaces herdr.

herdr-core keeps *terminals*: a program in a pty that outlives its viewers. It has no
workspaces, tabs or panes. A terminal has an address (``t7``), free-form labels, the
folder it started in, what runs in its foreground now, and the status its program
reports itself (OSC 7501: idle, working, done, blocked or error). Claude Code and Codex
both report that status, so nothing here reads a screen to learn what an agent does.

Every call is one JSON-RPC request over ``hc connect``, which speaks the core's API on
stdin and stdout and starts the core when none runs. The API carries what the CLI's
listings leave out (labels, the foreground process), and a call made this way has no
caller: a terminal started here has no parent to end with, and text sent here carries
no sender tag.

This machine's core also holds the lines to the workers added with ``hc machine add
<ssh target> --name <label>``, where the label is the worker's in ``machines.json``.
Their terminals are in the same listing, addressed ``<label>/t7``. A terminal id kept
in a record is the address on its own machine (``t7``); ``machine`` here is always that
label, or None for this machine.
"""

import json
import os
import re
import subprocess

# A terminal's address on its own machine, as records keep it.
TERMINAL = re.compile(r"t[0-9]+[a-z]?")
TIMEOUT = 30
# hc holds a send while a person has an unsent draft, for at most this long.
DRAFT_WAIT = 60
# How long a send that must not be delayed may be held before it is taken back.
BRIEF_HOLD = 5
# What a send takes once nothing holds it: hc's pause after the last key, then the paste.
TYPING = 5
NAME_LIMIT = 32


class Error(RuntimeError):
    """hc refused a request; ``kind`` is its stable word for why."""

    def __init__(self, kind: str, message: str, hint: str = ""):
        super().__init__(message)
        self.kind = kind
        self.hint = hint


class Unreachable(Error):
    """The core or the line to a worker did not answer; nothing is known about it."""


def binary() -> str:
    return os.environ.get("BABYSIT_HC") or "hc"


def call(method: str, params: dict | None = None, *, timeout: float = TIMEOUT) -> dict:
    """One API request; its ``result``. Raises ``Error`` with hc's kind when refused."""
    request = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    try:
        proc = subprocess.run(
            [binary(), "connect"],
            input=json.dumps(request) + "\n",
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        raise Unreachable("unavailable", "hc is not installed on this machine") from None
    except subprocess.TimeoutExpired:
        raise Unreachable("unavailable", f"hc did not answer {method} in time") from None
    reply = None
    for line in proc.stdout.splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict) and value.get("id") == 1:
            reply = value
            break
    if reply is None:
        detail = (proc.stderr or proc.stdout).strip()[-400:]
        raise Unreachable("unavailable", detail or "hc returned no reply")
    error = reply.get("error")
    if error:
        data = error.get("data") if isinstance(error, dict) else None
        data = data if isinstance(data, dict) else {}
        kind = str(data.get("kind") or "internal")
        message = str(error.get("message") if isinstance(error, dict) else error)
        failure = Unreachable if kind in {"unavailable", "machine_unknown"} else Error
        raise failure(kind, message, str(data.get("hint") or ""))
    result = reply.get("result")
    if not isinstance(result, dict):
        raise Error("internal", f"hc returned no result for {method}")
    return result


def address(machine: str | None, terminal: str) -> str:
    """How this machine's core names ``terminal`` on ``machine`` (None: here)."""
    if not TERMINAL.fullmatch(terminal):
        raise Error("not_found", f"{terminal!r} is not a terminal address")
    return terminal if machine is None else f"{machine}/{terminal}"


def local(record: dict) -> dict:
    """``record`` with the address its own machine gives it."""
    return {**record, "address": str(record.get("address", "")).rpartition("/")[2]}


def terminals(machine: str | None = None, *, timeout: float = TIMEOUT) -> list[dict]:
    """One machine's live terminals, each addressed as on that machine.

    A worker whose line is down raises ``Unreachable``: its terminals are missing from
    the listing then, which must not read as the worker having none.
    """
    listed = call("terminal.list", {}, timeout=timeout)
    if machine is not None:
        lines = [m for m in listed.get("machines") or [] if isinstance(m, dict)]
        line = next((m for m in lines if m.get("name") == machine), None)
        if line is None:
            raise Unreachable(
                "machine_unknown",
                f"hc does not know {machine}; add it with hc machine add <ssh target> "
                f"--name {machine}",
            )
        if line.get("state") != "connected":
            raise Unreachable("unavailable", f"hc's line to {machine} is {line.get('state')}")
    prefix = "" if machine is None else f"{machine}/"
    found = []
    for record in listed.get("terminals") or []:
        if not isinstance(record, dict) or record.get("state") != "live":
            continue
        name, slash, rest = str(record.get("address", "")).rpartition("/")
        if (name + slash) == prefix and TERMINAL.fullmatch(rest):
            found.append(local(record))
    return found


def get(terminal: str, machine: str | None = None) -> dict:
    return local(call("terminal.get", {"terminal": address(machine, terminal)}))


def start(
    cwd: str,
    *,
    machine: str | None = None,
    name: str | None = None,
    labels: dict[str, str] | None = None,
) -> dict:
    """A new terminal running the machine's own shell in ``cwd``.

    It is kept: if its shell is lost (a reboot, a crash) hc gives it a new one in the
    same folder, so the terminal and its address outlive the loss. It has no parent, so
    it also outlives whatever asked for it.
    """
    params: dict = {"cwd": cwd, "no_parent": True, "keep": True, "labels": labels or {}}
    if name:
        params["name"] = name
    if machine is not None:
        params["on"] = machine
    return local(call("terminal.run", params, timeout=60))


def send(
    terminal: str,
    text: str,
    machine: str | None = None,
    *,
    force: bool = False,
    only_if: str | None = None,
    hold: float = DRAFT_WAIT,
) -> dict:
    """Paste ``text`` and press Enter, once no person has an unsent draft there.

    hc holds the text while a person is typing in the terminal: until they send or
    clear their draft, at most a minute after their last key. It decides whether to type
    only when that hold ends, not when asked: it then refuses a program that reports
    ``blocked`` (the text would answer its dialog) unless ``force``, and with
    ``only_if`` (idle or working) one whose status is anything else, an agent that left
    for its shell included. Those raise ``Error`` with kind ``blocked`` or
    ``status_mismatch``, and nothing was typed.

    ``hold`` bounds the wait. A send still held when it runs out is taken back: hc
    cancels one whose caller goes away before typing starts. That raises ``Error`` with
    kind ``held``; only a hold that ended in that very moment is typed all the same,
    which nothing here can tell apart. The same happens when whoever runs this is
    stopped first, so a caller's shorter timeout never leaves a line to arrive later.
    """
    params: dict = {"terminal": address(machine, terminal), "text": text}
    if force:
        params["force"] = True
    if only_if:
        params["if"] = only_if
    try:
        return call("terminal.send", params, timeout=hold + TYPING)
    except Unreachable as exc:
        if "did not answer" not in str(exc):
            raise
        raise Error(
            "held",
            f"hc held the text for {terminal}, where a person is typing; it was taken back "
            "unless hc had just begun to type it",
        ) from exc


def keys(terminal: str, names: list[str], machine: str | None = None) -> dict:
    """Press named keys (enter, esc, up, ctrl+d, a, ...)."""
    return call("terminal.keys", {"terminal": address(machine, terminal), "keys": names})


def read(terminal: str, machine: str | None = None, *, lines: int = 0) -> str:
    """The screen as plain text, with ``lines`` of history above it."""
    result = call("terminal.read", {"terminal": address(machine, terminal), "lines": lines})
    return str(result.get("text") or "")


def wait(terminal: str, seconds: float, machine: str | None = None) -> dict:
    """Wait until the program stops working; ``ended`` is false when it still does."""
    return call(
        "terminal.wait",
        {"terminal": address(machine, terminal), "timeout": seconds},
        timeout=seconds + TIMEOUT,
    )


def label(terminal: str, labels: dict[str, str], machine: str | None = None) -> dict:
    """Set labels; an empty value removes one."""
    return local(call("terminal.label", {"terminal": address(machine, terminal), "labels": labels}))


def close(terminal: str, machine: str | None = None) -> None:
    """End a terminal's program and forget it; one already gone is fine."""
    target = address(machine, terminal)
    for method in ("terminal.kill", "terminal.rm"):
        try:
            call(method, {"terminal": target})
        except Unreachable:
            raise
        except Error as exc:
            if exc.kind not in {"not_found", "finished", "live"}:
                raise


def execute(argv: list[str], machine: str | None = None, *, timeout: int = 20) -> str:
    """Run a short command without a terminal, here or on a worker; what it wrote."""
    params: dict = {"argv": argv, "timeout": timeout}
    if machine is not None:
        params["on"] = machine
    result = call("machine.exec", params, timeout=timeout + TIMEOUT)
    if result.get("exit_code") not in (0, None):
        raise Error("exec_failed", str(result.get("stderr") or result.get("stdout") or "")[-400:])
    return str(result.get("stdout") or "")


def terminal_name(text: str, taken: set[str]) -> str | None:
    """A name hc accepts for ``text``, unique among ``taken``; None when none fits."""
    base = re.sub(r"[^a-z0-9_-]+", "-", text.lower()).strip("-")[:NAME_LIMIT].strip("-")
    if not base:
        return None
    if base not in taken:
        return base
    for n in range(2, 100):
        suffix = f"-{n}"
        candidate = base[: NAME_LIMIT - len(suffix)].rstrip("-") + suffix
        if candidate not in taken:
            return candidate
    return None
