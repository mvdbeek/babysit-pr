"""Local, repository-independent focus tags shared by wt and the dashboard."""

from __future__ import annotations

import re
import sqlite3
import unicodedata
from collections.abc import Iterable
from contextlib import closing
from pathlib import Path
from urllib.parse import urlsplit

ITEM = re.compile(r"(pr|issue):([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#([1-9][0-9]*)")


def tag_name(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("A tag must be text")
    normalized = unicodedata.normalize("NFC", value)
    name = normalized.strip()
    if not 1 <= len(name) <= 64 or any(unicodedata.category(c).startswith("C") for c in normalized):
        raise ValueError("A tag needs 1–64 characters without control characters")
    return name


def tag_names(values: object) -> list[str]:
    if not isinstance(values, list) or len(values) > 50:
        raise ValueError("Supply at most 50 tags per action")
    return sorted({tag_name(value) for value in values})


def target_key(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) > 4096
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
    ):
        raise ValueError("Supply a workspace target or GitHub issue/PR target")
    match = ITEM.fullmatch(value)
    if match:
        kind, repo, number = match.groups()
        if any(part in (".", "..") for part in repo.split("/")):
            raise ValueError("Invalid repository")
        return f"{kind}:{repo.lower()}#{number}"
    if value.startswith("workspace:") and value.removeprefix("workspace:").strip():
        return value
    raise ValueError("Supply a workspace target or GitHub issue/PR target")


def item_target(kind: str, repo: str, number: int) -> str:
    return target_key(f"{kind}:{repo}#{number}")


def cli_target(value: str) -> str:
    if value.startswith("https://"):
        url = urlsplit(value)
        match = re.fullmatch(r"/([^/]+/[^/]+)/(issues|pull)/([1-9][0-9]*)/?", url.path)
        if url.netloc.lower() != "github.com" or not match:
            raise ValueError("Expected a GitHub issue or pull request URL")
        repo, kind, number = match.groups()
        return item_target("pr" if kind == "pull" else "issue", repo, int(number))
    if value.startswith(("workspace:", "pr:", "issue:")):
        return target_key(value)
    path = Path(value).expanduser().resolve()
    if path.is_dir():
        # Running wt tag from a source subdirectory still tags its checkout.
        path = next((root for root in (path, *path.parents) if (root / ".git").exists()), path)
    return target_key(f"workspace:{path}")


def effective(snapshot: dict, target: str) -> list[str]:
    """Share direct tags across a workspace and its linked items, without transitive spread."""
    assignments = snapshot["assignments"]
    names = set(assignments.get(target, []))
    for workspace, items in snapshot["links"].items():
        if target == workspace:
            for item in items:
                names.update(assignments.get(item, []))
        elif target in items:
            names.update(assignments.get(workspace, []))
    return sorted(names)


class Tags:
    def __init__(self, home: Path):
        self.path = home / "tags.sqlite"

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        db = sqlite3.connect(self.path, timeout=10)
        db.execute(
            "CREATE TABLE IF NOT EXISTS tags (target TEXT, name TEXT, PRIMARY KEY(target,name))"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS links (workspace TEXT, item TEXT, PRIMARY KEY(workspace,item))"
        )
        return db

    def change(self, request: dict) -> dict:
        if set(request) - {"target", "add", "remove"}:
            raise ValueError("Unknown tag action field")
        target = target_key(request.get("target"))
        changes = {}
        for field in ("add", "remove"):
            values = request.get(field, [])
            changes[field] = tag_names(values)
        with closing(self.connect()) as db, db:
            db.executemany(
                "DELETE FROM tags WHERE target=? AND name=?",
                [(target, n) for n in changes["remove"]],
            )
            db.executemany(
                "INSERT OR IGNORE INTO tags VALUES (?, ?)", [(target, n) for n in changes["add"]]
            )
        return self.snapshot()

    def link(self, workspace: str, item: str) -> None:
        workspace, item = target_key(workspace), target_key(item)
        if not workspace.startswith("workspace:") or not ITEM.fullmatch(item):
            raise ValueError("A tag link needs a workspace and a GitHub item")
        with closing(self.connect()) as db, db:
            db.execute("INSERT OR IGNORE INTO links VALUES (?, ?)", (workspace, item))

    def snapshot(self, workspaces: Iterable[dict] = ()) -> dict:
        assignments: dict[str, list[str]] = {}
        links: dict[str, list[str]] = {}
        if self.path.exists():
            with closing(self.connect()) as db:
                for target, name in db.execute(
                    "SELECT target, name FROM tags ORDER BY target, name"
                ):
                    assignments.setdefault(target, []).append(name)
                for workspace, item in db.execute(
                    "SELECT workspace, item FROM links ORDER BY workspace, item"
                ):
                    links.setdefault(workspace, []).append(item)
        for row in workspaces:
            workspace = f"workspace:{row['key']}"
            items = set(links.get(workspace, []))
            for link in row.get("links", []):
                try:
                    items.add(item_target(link["kind"], link["repo"], link["number"]))
                except (KeyError, ValueError):
                    continue
            links[workspace] = sorted(items)
        return {
            "assignments": assignments,
            "links": links,
            "names": sorted({n for names in assignments.values() for n in names}),
        }
