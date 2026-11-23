"""The model and reasoning effort a running Claude or Codex agent uses.

Its command line says what it was started with, as `wt` passes it. What that leaves
out, the agent's configured default, its transcript's newest turn says: a Codex turn
context names both, a Claude reply only its model. Transcripts are read from their end,
and only again once they change.
"""

import contextlib
import json
import os
import threading
from pathlib import Path

import claude_accounts
import workspace_viewer
import wt

# A transcript's last bytes read for its newest turn; a turn writing more tool output
# than this since its context leaves the setting to the command line.
TAIL = 512 * 1024
CACHED = 64

_lock = threading.Lock()
_files: dict = {}  # session ID -> transcript file
_read: dict = {}  # file -> (size, mtime_ns, settings)


def from_argv(argv, kind):
    """``{"model", "effort"}`` the command line names, "" for each it leaves out."""
    model = effort = ""
    words = iter(argv[1:])
    for word in words:
        name, joined, value = word.partition("=")
        if not joined and name in {"--model", "-m", "--effort", "-c", "--config"}:
            value = next(words, "")
        if name == "--model" or (name == "-m" and kind == "codex"):
            model = value
        elif name == "--effort" and kind == "claude":
            effort = value
        elif name in {"-c", "--config"} and kind == "codex":
            key, _, setting = value.partition("=")
            if key.strip() == "model_reasoning_effort":
                effort = setting.strip().strip("\"'")
            elif key.strip() == "model":
                model = setting.strip().strip("\"'")
    return checked(model, effort, kind)


def checked(model, effort, kind):
    efforts = wt.EFFORTS | (wt.CODEX_EFFORTS if kind == "codex" else set())
    return {
        "model": model if isinstance(model, str) and wt.MODEL.fullmatch(model) else "",
        "effort": effort if effort in efforts else "",
    }


def from_text(text, kind):
    """The settings of the newest turn in transcript ``text`` (JSON lines)."""
    for line in reversed(text.splitlines()):
        if kind == "codex" and '"turn_context"' in line:
            with contextlib.suppress(ValueError):
                item = json.loads(line)
                payload = item.get("payload")
                if item.get("type") == "turn_context" and isinstance(payload, dict):
                    return checked(payload.get("model") or "", payload.get("effort") or "", kind)
        elif kind == "claude" and '"assistant"' in line:
            with contextlib.suppress(ValueError):
                item = json.loads(line)
                message = item.get("message")
                model = message.get("model") if isinstance(message, dict) else None
                # Claude Code's own notices are recorded as replies of "<synthetic>".
                if item.get("type") == "assistant" and model and not model.startswith("<"):
                    return checked(model, "", kind)
    return checked("", "", kind)


def tail(path):
    """The last ``TAIL`` bytes of a file, from its first whole line."""
    with open(path, "rb") as stream:
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        stream.seek(max(0, size - TAIL))
        data = stream.read(TAIL)
    if size > TAIL:
        data = data.partition(b"\n")[2]
    return data.decode(errors="replace")


def transcript(session, kind):
    """This machine's transcript file of ``session``, or None."""
    with _lock:
        file = _files.get(session)
    if file is None or not file.exists():
        row = workspace_viewer.session_anywhere(session)
        if row is None or row["agent"] != kind:
            return None
        file = Path(row["_file"])
        with _lock:
            remember(_files, session, file)
    return file


def account(session):
    """The Claude account whose store holds ``session``, as ``claude_accounts`` names it;
    None for a configuration that is not one of them, or no transcript here."""
    file = transcript(session, "claude")
    home = file and claude_accounts.transcript_home(file)
    return claude_accounts.account_name(home) if home else None


def from_transcript(session, kind):
    """The newest turn's settings in this machine's transcript of ``session``."""
    file = transcript(session, kind)
    if file is None:
        return checked("", "", kind)
    try:
        stat = file.stat()
        with _lock:
            cached = _read.get(file)
        if cached and cached[:2] == (stat.st_size, stat.st_mtime_ns):
            return cached[2]
        found = from_text(tail(file), kind)
    except OSError:
        return checked("", "", kind)
    with _lock:
        remember(_read, file, (stat.st_size, stat.st_mtime_ns, found))
    return found


def remember(cache, key, value):
    cache.pop(key, None)
    cache[key] = value
    while len(cache) > CACHED:
        cache.pop(next(iter(cache)))


def observed(argv, kind, session=None):
    """What an agent runs with: its command line's settings, else its transcript's.

    ``session`` is read from this machine's transcripts; a worker's agent goes by its
    command line alone.
    """
    found = from_argv(argv, kind)
    if session and not (found["model"] and found["effort"]):
        recorded = from_transcript(session, kind)
        found = {key: found[key] or recorded[key] for key in found}
    return found
