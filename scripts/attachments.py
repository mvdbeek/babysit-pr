"""Files attached to the messages and tasks the dashboard sends to agents.

An upload is saved in the attachments directory under a new name that cannot collide
with another or leave the directory. A message or task names its attachments by those
names, and the agent gets their full paths after its text, so it reads them itself.
Nothing is ever overwritten or deleted.
"""

import datetime
import re
import secrets
from pathlib import Path

HEADING = "Attached files (read them from these paths):"
MAX_FILE = 25 * 1024 * 1024
MAX_FILES = 10
STORED = re.compile(r"^\d{4}-\d{2}-\d{2}-[0-9a-f]{8}-[A-Za-z0-9._-]{1,80}$")
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
    return {"id": stored, "name": filename[:200], "size": len(data)}


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
