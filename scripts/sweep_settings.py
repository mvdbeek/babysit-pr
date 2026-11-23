"""The review sweep's settings: which repositories it reviews, and how.

Kept in ``review-sweep.json`` under the dashboard's home, which you edit, and served
read-only at ``GET /api/review-sweep``, which is how the sweep's scripts
(``scripts/sweep/``) read them. The API does not change them: the notes become every
reviewer's instructions, and agents that read untrusted pull requests can reach the API.
See references/review-sweep.md.

- ``owner``: the organisation whose repositories are swept. Keys and checkout names of its
  pull requests leave the owner out (``planemo#1612``, ``review-planemo-pr-1612``).
- ``default_repo``: the repository a bare pull request number means (``24191``,
  ``review-pr-24191``).
- ``repos``: the owner's repositories whose open non-draft pull requests are all reviewed.
- ``review_requests``: also review every open pull request, anywhere, that requests the
  user's review.
- ``later_repos`` and ``bots``: queued after the others: pull requests of these
  repositories, then those these accounts opened.
- ``out``: the folder reports and run indexes are written to.
- ``parallel``: reviewers at a time; ``per_run``: pull requests started per run.
- ``notes``: per repository (``owner/repo``), what its reviewer reads around the change
  (``context``), looks for (``look``) and runs to verify (``verify``).
"""

import json
import os
import re
from pathlib import Path

FILE = "review-sweep.json"
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9_.-]+")
NOTE_KEYS = ("context", "look", "verify")
DEFAULTS: dict[str, object] = {
    "owner": None,
    "default_repo": None,
    "repos": [],
    "review_requests": True,
    "later_repos": [],
    "bots": [],
    "out": "~/src/agent-artefacts/outbox/pr-reviews",
    "parallel": 7,
    "per_run": 40,
    "notes": {},
}
MAX_TEXT = 4000


def validate(value):
    """A complete settings object, checked; raises ValueError."""
    if not isinstance(value, dict):
        raise ValueError("Review sweep settings must be an object")
    unknown = set(value) - set(DEFAULTS)
    if unknown:
        raise ValueError(f"Unknown review sweep settings: {', '.join(sorted(unknown))}")
    found = {**DEFAULTS, **value}
    for name in ("owner", "default_repo"):
        if found[name] is not None and not NAME.fullmatch(str(found[name])):
            raise ValueError(f"{name} must be a GitHub name")
    for name in ("repos", "later_repos", "bots"):
        items = found[name]
        if not isinstance(items, list) or not all(
            isinstance(item, str) and NAME.fullmatch(item) for item in items
        ):
            raise ValueError(f"{name} must be a list of GitHub names")
    if found["repos"] and not found["owner"]:
        raise ValueError("repos needs an owner")
    if not isinstance(found["review_requests"], bool):
        raise ValueError("review_requests must be true or false")
    if not isinstance(found["out"], str) or not found["out"].startswith(("/", "~/")):
        raise ValueError("out must be an absolute path, or one under ~/")
    for name, low, high in (("parallel", 1, 20), ("per_run", 1, 200)):
        number = found[name]
        if not isinstance(number, int) or isinstance(number, bool) or not low <= number <= high:
            raise ValueError(f"{name} must be a whole number from {low} to {high}")
    notes = found["notes"]
    if not isinstance(notes, dict):
        raise ValueError("notes must map owner/repo to {context, look, verify}")
    for slug, note in notes.items():
        if not SLUG.fullmatch(slug) or not isinstance(note, dict) or set(note) - set(NOTE_KEYS):
            raise ValueError(f"notes for {slug!r} must be {{context, look, verify}}")
        if not all(isinstance(text, str) and len(text) <= MAX_TEXT for text in note.values()):
            raise ValueError(f"notes for {slug} must be text of at most {MAX_TEXT} characters")
    return found


def load(home):
    """The saved settings, or the defaults (with no repositories, so nothing is swept)."""
    path = Path(home) / FILE
    try:
        return validate(json.loads(path.read_text()))
    except FileNotFoundError:
        return dict(DEFAULTS)


def save(home, value):
    found = validate(value)
    path = Path(home) / FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(found, indent=1) + "\n")
    os.replace(temporary, path)
    return found


def out_dir(home):
    """The sweep's report folder on this machine, or None when the settings are broken."""
    try:
        return Path(load(home)["out"]).expanduser()
    except (ValueError, OSError):
        return None
