"""Files attached to the messages and tasks the dashboard sends to agents.

An upload is saved in the attachments directory under a new name that cannot collide
with another or leave the directory. A message or task names its attachments by those
names, and the agent gets their full paths after its text, so it reads them itself.
Nothing is ever overwritten. Saving prunes, at most hourly, the uploads saved here
long ago; no other file in the directory is ever touched.
"""

import datetime
import os
import re
import secrets
import threading
import time
from pathlib import Path

HEADING = "Attached files (read them from these paths):"
MAX_FILE = 25 * 1024 * 1024
MAX_FILES = 10
STORED = re.compile(r"^\d{4}-\d{2}-\d{2}-[0-9a-f]{8}-[A-Za-z0-9._-]{1,80}$")
# An agent reads its files when its task starts. The last of a batch can start 30 days
# out plus 99 later items a day apart, plus a day's grace for a missed start (the
# scheduling limits in pr_workspaces, which imports this module): uploads are kept 30
# days beyond that.
LATEST_START_SECONDS = (30 + 99 * 1 + 1) * 86400
KEEP_SECONDS = LATEST_START_SECONDS + 30 * 86400
PRUNE_SECONDS = 3600
_pruned: dict[Path, float] = {}
_pruned_lock = threading.Lock()
# Fields holding the text an agent receives, by dashboard action.
TEXT_FIELDS = {
    "workspace-message": "text",
    "workspace-action": "task",
    "workspace-batch": "task",
    "workspace-new": "task",
}


def save(directory: Path, filename: str, data: bytes) -> dict:
    """Store one upload; return the name a request uses to attach it."""
    if not data or len(data) > MAX_FILE:
        raise ValueError(f"Attach a file of at most {MAX_FILE // (1024 * 1024)} MB")
    # Only the base name, in characters a path or shell reads plainly; no hidden files.
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(filename.replace("\\", "/")).name)
    safe = safe.strip(".-")[-80:].lstrip(".-") or "file"
    stored = f"{datetime.date.today():%Y-%m-%d}-{secrets.token_hex(4)}-{safe}"
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / stored).open("xb") as stream:
        stream.write(data)
    prune(directory)
    return {"id": stored, "name": filename[:200], "size": len(data)}


def prune(directory: Path, now: float | None = None) -> None:
    """Delete uploads saved here more than KEEP_SECONDS ago; at most once an hour.

    Only regular files named as save() names them, whose date and modification time are
    both that old, are deleted. Anything else in the directory is left alone.
    """
    now = time.time() if now is None else now
    with _pruned_lock:
        if now - _pruned.get(directory, float("-inf")) < PRUNE_SECONDS:
            return
        _pruned[directory] = now
    cutoff = now - KEEP_SECONDS
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return
    for entry in entries:
        if not STORED.fullmatch(entry.name):
            continue
        try:
            saved = datetime.datetime.strptime(entry.name[:10], "%Y-%m-%d").timestamp()
            if (
                saved < cutoff
                and entry.is_file(follow_symlinks=False)
                and entry.stat(follow_symlinks=False).st_mtime < cutoff
            ):
                os.unlink(entry.path)
        except (OSError, ValueError):
            continue  # Unreadable, removed meanwhile, or not a real date: kept.


def note(directory: Path, ids) -> str:
    """The lines naming attached files for the agent, after checking each exists."""
    if (
        not isinstance(ids, list)
        or len(ids) > MAX_FILES
        or len(set(map(str, ids))) != len(ids)
        or not all(isinstance(i, str) and STORED.match(i) for i in ids)
    ):
        raise ValueError(f"Attach at most {MAX_FILES} uploaded files")
    paths = [directory / i for i in ids]
    if not all(path.is_file() for path in paths):
        raise ValueError("An attached file is missing; attach it again")
    return "\n".join([HEADING, *(f"- {p}" for p in paths)])


def without_note(text: str) -> str:
    """The text as typed, without the attached files' paths (which stay private)."""
    at = text.find(HEADING)
    return text[:at].rstrip() if at >= 0 else text


def attach(directory: Path, action: str, request: dict) -> None:
    """Move a request's attachments into the text its agent receives."""
    ids = request.pop("attachments", None)
    # Only attachments put paths there; one a client sends is never trusted.
    request.pop("task_files", None)
    field = TEXT_FIELDS.get(action)
    if ids is None or ids == []:
        return
    if field is None:
        raise ValueError("Files cannot be attached here")
    text = request.get(field, "")
    if not isinstance(text, str):
        return  # The action's own validation refuses it.
    if action == "workspace-new" and request.get("issue_title"):
        # The task becomes a public issue's body; local paths are for the agent only.
        request["task_files"] = note(directory, ids)
        return
    request[field] = f"{text.rstrip()}\n\n{note(directory, ids)}".lstrip()
