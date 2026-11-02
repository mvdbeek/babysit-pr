"""Files attached to the messages and tasks the dashboard sends to agents.

An upload is saved in the attachments directory under a new name that cannot collide
with another or leave the directory. A message or task names its attachments by those
names, and the agent gets their full paths after its text, so it reads them itself.
Nothing is ever overwritten. Saving prunes, at most hourly, the uploads saved here
long ago; no other file in the directory is ever touched.

An agent on a worker machine cannot read this machine's paths: before its task or
message is staged there, for_worker copies each attached upload to a private folder on
the worker (REMOTE) and names the copies instead.
"""

import datetime
import os
import re
import secrets
import stat
import threading
import time
from pathlib import Path

import machines

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
# Where a worker keeps the copies it gets: a private folder its agents read (Agent Safehouse
# grants all of ~/.local/state/babysit-pr, writes too; babysit-bwrap binds it read-only).
REMOTE = "~/.local/state/babysit-pr/attachments"
# Seconds to copy one file there: 25 MB over a slow link.
UPLOAD_TIMEOUT = 300
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


def _read_upload(directory: Path, name: str) -> bytes:
    """An upload's bytes: a regular file of its own in the directory, never a link."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        folder = os.open(directory, flags | os.O_DIRECTORY)
        try:
            fd = os.open(name, flags | getattr(os, "O_NONBLOCK", 0), dir_fd=folder)
        finally:
            os.close(folder)
    except OSError:
        raise ValueError("An attached file is missing; attach it again") from None
    try:
        info = os.fstat(fd)
        # A second hard link could be a file this directory was never given.
        if stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
            with os.fdopen(os.dup(fd), "rb") as stream:
                data = stream.read(MAX_FILE + 1)
        else:
            data = b""
    finally:
        os.close(fd)
    if not data or len(data) > MAX_FILE:
        raise ValueError(f"The attached file {name} is not one this dashboard saved")
    return data


def for_worker(home, machine, text: str, directory: Path) -> str:
    """``text`` with its attached files copied to ``machine``, naming the copies there.

    Only the note attach() ends a text with counts: the lines after its last HEADING.
    Each line naming an upload in ``directory`` (``- <directory>/<stored name>``) has the
    file copied and its path swapped for the copy's; nothing else is ever copied, so a
    heading typed by hand cannot send any other file of this machine. Other lines are
    left as they are, as is a text without a note. Each copy gets a new name, since a
    worker can be sent the same upload again (a batch, a retry, a later message) and
    never overwrites a file. A failed copy raises, so nothing is typed with a dead path.
    """
    at = text.rfind(HEADING)
    if at < 0:
        return text
    lines = text[at:].split("\n")
    prefix = f"- {directory}/"
    wanted = [
        (n, line[len(prefix) :])
        for n, line in enumerate(lines)
        if line.startswith(prefix) and STORED.fullmatch(line[len(prefix) :])
    ]
    if not wanted:
        return text
    if len(wanted) > MAX_FILES:
        raise ValueError(f"Attach at most {MAX_FILES} uploaded files")
    folder = machines.remote_path(REMOTE)
    for n, name in wanted:
        data = _read_upload(directory, name)
        copy = machines.put_file(
            home,
            machine,
            folder,
            f"{secrets.token_hex(4)}-{name}",
            data,
            keep=KEEP_SECONDS,
            timeout=UPLOAD_TIMEOUT,
        )
        lines[n] = f"- {copy}"
    return text[:at] + "\n".join(lines)
