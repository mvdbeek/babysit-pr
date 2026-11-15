"""Opt-in dufs integration: the artefacts a workspace's agents made for the reader.

Agents leave reports, screenshots and patches in a directory that a dufs file server
(https://github.com/sigoden/dufs) shares, and name them in what they write: the link they
hand over in a reply, the path of a file they write, a command that copies one there.
This module finds those names in the transcripts of a checkout's sessions and asks dufs
what is there now, so the viewer's Artefacts tab lists each file with its size, its age,
a link that opens it on the share, and the transcript entry that named it.

A name is taken from an agent's reply or from the input of a tool call, never from a
prompt or a tool's output, and never from a tool that only reads. It is evidence that
the workspace made the file, not proof: an agent that writes about another task's
report names it too.

Nothing is written, here or on the share. The share is read through its JSON listings
(``<directory>/?json``) with bounded requests, and a path from a transcript only ever
selects a directory below the configured base.
"""

import http.client
import json
import posixpath
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import workspace_viewer

MAX_CONFIG = 65536
# Transcript entries that mention the share, read for names (not every mention is one),
# and the distinct paths kept from them.
MAX_MATCHES = 1000
MAX_NAMED = 200
# What one answer may ask of the share: requests, seconds in all and per request, bytes
# and entries per listing.
MAX_LISTINGS = 24
SHARE_BUDGET = 12
SHARE_TIMEOUT = 5
MAX_LISTING = 2 * 1024 * 1024
MAX_ENTRIES = 10000
READ_CHUNK = 64 * 1024
# Files shown per folder; the rest are counted, and the folder links to all of them.
MAX_FILES = 100
MAX_PATH = 1024
# Images this small are shown as thumbnails, straight from the share.
IMAGES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".avif"}
MAX_PREVIEW = 8 * 1024 * 1024
# dufs serves Markdown as a download; its viewer page shows it instead.
VIEWED = {".md", ".markdown"}
# Tools that read: naming a path to them does not make it this workspace's work.
READERS = frozenset({"Read", "Grep", "Glob", "LS", "WebFetch", "WebSearch"})
# Where a link or a path ends in what an agent wrote. A path also ends at anything a
# shell would expand, so `outbox/task/*.png` names the folder `outbox/task`.
LINK_END = r"\s<>\"'`\\|"
PATH_END = LINK_END + r";&$*?(){}\[\]"
FOLDERS = {"Dir", "SymlinkDir"}
FILES = {"File", "SymlinkFile"}
HOST = re.compile(r"^[A-Za-z0-9.-]+$")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A listing is answered where it was asked: a redirect is an error, not followed."""

    def redirect_request(self, *args, **kwargs):
        return None


class Unanswered(ValueError):
    """The share answered, but not with a listing of the path asked for: an error status,
    a body too large, or something that is not JSON. Other paths can still be asked."""


def _no_constant(name):
    raise ValueError(f"{name} is not a number")


def read_share(url, timeout=SHARE_TIMEOUT):
    """What the share answers for ``url`` as parsed JSON; None when it has no such path.

    ``timeout`` bounds the whole request, not each read. Raises ``Unanswered`` for an
    answer that is no listing, and ``OSError`` when the share could not be reached.
    """
    stop = time.monotonic() + timeout
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    # No proxy from the environment: the share is asked where the configuration says.
    opener = urllib.request.build_opener(_NoRedirect, urllib.request.ProxyHandler({}))
    chunks, size = [], 0
    try:
        with opener.open(request, timeout=timeout) as response:
            # One read of the socket at a time, so a trickle still meets the deadline.
            while chunk := response.read1(READ_CHUNK):
                chunks.append(chunk)
                size += len(chunk)
                if size > MAX_LISTING:
                    raise Unanswered("the listing exceeds the size limit")
                if time.monotonic() > stop:
                    raise TimeoutError("the share answered too slowly")
            # What is left of the length it announced: it hung up before the end.
            if response.length:
                raise ConnectionError("the share's answer broke off")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise Unanswered(f"HTTP {exc.code}") from exc
    except http.client.HTTPException as exc:
        raise OSError(f"the share's answer broke off: {type(exc).__name__}") from exc
    try:
        return json.loads(b"".join(chunks), parse_constant=_no_constant)
    except (ValueError, RecursionError) as exc:
        raise Unanswered("the share did not answer with JSON") from exc


def web_base(value, name):
    """A configured ``http(s)://host[:port][/path]`` as (base without a final slash,
    origin). Credentials, queries and fragments are refused: the base is shown in links
    and its origin goes into a response header."""
    if not isinstance(value, str):
        raise ValueError(f"`{name}` must be a web address")
    try:
        parts = urllib.parse.urlsplit(value)
        port = parts.port
    except ValueError as exc:
        raise ValueError(f"`{name}` is not a web address: {exc}") from exc
    host = parts.hostname or ""
    if (
        parts.scheme not in {"http", "https"}
        or not HOST.match(host)
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or re.search(r"[^\x21-\x7e]", value)
    ):
        raise ValueError(f"`{name}` must be http(s)://host[:port][/path] without credentials")
    origin = f"{parts.scheme}://{host}" + (f":{port}" if port else "")
    return origin + urllib.parse.quote(parts.path.rstrip("/"), safe="/%"), origin


def clean(path):
    """A relative path below the share's base, or None when it could leave it."""
    parts = [part for part in path.split("/") if part not in ("", ".")]
    if (
        not parts
        or ".." in parts
        or len(path) > MAX_PATH
        # A name is at most 255 bytes on the share's disk, whatever its characters.
        or any(len(part.encode("utf-8", "replace")) > 255 for part in parts)
        or re.search(r"[\x00-\x1f\x7f\\]", path)
    ):
        return None
    return "/".join(parts)


def trim(text):
    """``text`` without the sentence punctuation, emphasis and unmatched closing bracket
    that followed it where it was written."""
    while True:
        cut = text.rstrip(".,;:!?*")
        for opening, closing in ("()", "[]"):
            if cut.endswith(closing) and cut.count(opening) < cut.count(closing):
                cut = cut[:-1]
        if cut == text:
            return text
        text = cut


def number(value, limit):
    """``value`` if it is a finite count below ``limit``, else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    # Not a number, an infinity and an integer too large for a float all fail this.
    return value if 0 <= value < limit else None


def listed(value):
    """A dufs directory listing as ({name: {folder, size, modified}}, whether it was cut
    short); (None, False) when the share says there is no such directory."""
    if not isinstance(value, dict):
        raise Unanswered("the share did not answer with a listing")
    paths = value.get("paths")
    # With uploads allowed, dufs answers a directory that does not exist as an empty one
    # it could create; a file answers with its own details, without ``paths``.
    if not isinstance(paths, list) or value.get("dir_exists") is False:
        return None, False
    found = {}
    for item in paths[:MAX_ENTRIES]:
        if not isinstance(item, dict):
            continue
        name, kind = item.get("name"), item.get("path_type")
        size, modified = item.get("size"), item.get("mtime")
        if (
            not isinstance(name, str)
            or clean(name) != name
            or "/" in name
            or kind not in FOLDERS | FILES
        ):
            continue
        size, modified = number(size, 2**63), number(modified, 1e15)
        found[name] = {
            "folder": kind in FOLDERS,
            "size": size if isinstance(size, int) else None,
            # dufs counts milliseconds.
            "modified": None if modified is None else modified / 1000,
        }
    return found, len(paths) > MAX_ENTRIES


class Artefacts:
    """The configured share and the answer for one checkout; no state between answers."""

    def __init__(self, home, read=read_share):
        self.home = Path(home)
        self.directory = self.home / "experiments" / "artefacts"
        self.read = read
        self.enabled = False
        self.error = None
        # Where links lead and what transcripts name, where listings are read, and the
        # origin thumbnails load from; empty until a configuration is accepted.
        self.url = self.api = self.origin = ""
        # How this machine's agents spell the shared directory in a path.
        self.spellings: list[str] = []
        # What a link to the share and a path into it look like; neither matches yet.
        self.link = re.compile(r"(?!)")
        self.paths: re.Pattern[str] | None = None
        try:
            path = self.directory / "config.json"
            if not path.exists():
                return
            if path.stat().st_size > MAX_CONFIG:
                raise ValueError("configuration exceeds the size limit")
            config = json.loads(path.read_text())
            if not isinstance(config, dict):
                raise ValueError("configuration must be an object")
            self.enabled = config.get("enabled") is True
            if not self.enabled:
                return
            url, origin = web_base(config.get("url"), "url")
            api, _ = web_base(config.get("api", config.get("url")), "api")
            root = config.get("root")
            found = []
            if root is not None:
                if not isinstance(root, str) or not root.startswith(("/", "~/")):
                    raise ValueError("`root` must be an absolute path, or one under ~/")
                found = spellings(root)
        except Exception as exc:
            self.error = f"Artefact share configuration unavailable: {exc}"
            return
        self.url, self.api, self.origin, self.spellings = url, api, origin, found
        self.link = re.compile(re.escape(url) + rf"/([^{LINK_END}]*)")
        if found:
            # Not the tail of a longer path, and a whole directory name.
            self.paths = re.compile(
                r"(?<![\w./~-])(?:"
                + "|".join(re.escape(spelling) for spelling in found)
                + rf")/([^{PATH_END}]*)"
            )

    def status(self):
        """What the viewer needs before it asks for a checkout: whether to offer the tab."""
        if not self.enabled:
            return {"enabled": False}
        return {
            "enabled": True,
            "error": self.error,
            "share": None if self.error else self.url + "/",
        }

    def image_source(self):
        """The origin thumbnails load from, for the page's content security policy."""
        return self.origin or None

    def expression(self, local):
        """An FTS query for entries that name the share: its address, or (for this
        machine's transcripts) the shared directory in any of its spellings."""
        needles = [self.url] + (self.spellings if local else [])
        return " OR ".join('"' + needle.replace('"', '""') + '"' for needle in needles)

    def names(self, text, local):
        """The paths below the share's base that ``text`` names, in order."""
        found = []
        for match in self.link.finditer(text):
            # `?view` and the like say how to show it, not which file it is.
            rest = re.split(r"[?#]", match.group(1), maxsplit=1)[0]
            if len(rest) > 3 * MAX_PATH:
                continue  # No path is this long, even percent-encoded.
            rest = trim(rest)
            try:
                found.append((match.start(), urllib.parse.unquote(rest, errors="strict")))
            except UnicodeDecodeError:
                continue
        if local and self.paths:
            found.extend(
                (match.start(), trim(match.group(1)))
                for match in self.paths.finditer(text)
                if len(match.group(1)) <= MAX_PATH
            )
        return [path for _, name in sorted(found) if (path := clean(name))]

    def named(self, matches, local):
        """{path: where it was named} from transcript ``matches``, newest first, and
        whether there were more paths than are kept."""
        found: dict[str, dict] = {}
        for session, index, entry, _ in matches:
            if entry.get("role") == "assistant":
                text = entry.get("text")
            elif entry.get("role") == "tool" and entry.get("name") not in READERS:
                text = entry.get("input")
            else:
                continue
            if not isinstance(text, str):
                continue
            for path in self.names(text, local):
                if path in found:
                    continue
                if len(found) >= MAX_NAMED:
                    return found, True
                found[path] = {
                    "session": session["id"],
                    "agent": session["agent"],
                    "title": session["title"],
                    "index": index,
                    "time": entry.get("time"),
                    "tool": entry.get("name") if entry.get("role") == "tool" else None,
                }
        return found, False

    def address(self, base, path, folder=False):
        return (
            base
            + ("/" + urllib.parse.quote(path, safe="/") if path else "")
            + ("/" if folder else "")
        )

    def row(self, folder, name, item, mention):
        path = posixpath.join(folder, name)
        suffix = posixpath.splitext(name)[1].lower()
        url = self.address(self.url, path, item["folder"])
        return {
            "name": name,
            "path": path,
            "kind": "folder" if item["folder"] else "file",
            # A folder's size is how many entries it holds.
            "size": item["size"],
            "modified": item["modified"],
            "url": url + ("?view" if suffix in VIEWED and not item["folder"] else ""),
            "image": url
            if not item["folder"]
            and suffix in IMAGES
            and item["size"] is not None
            and item["size"] <= MAX_PREVIEW
            else None,
            "named": mention,
        }

    def shared(self, named):
        """What the share holds of the ``named`` paths, grouped by folder.

        A named file is listed in its folder, which also says how many other files it
        holds; a named folder is listed with all its files. A path the share does not
        have is ``missing``. A folder the share would not list costs only the names in
        it; a share that cannot be reached is not asked again.
        """
        stop = time.monotonic() + SHARE_BUDGET
        # Per directory: (entries or None for no such directory, whether cut short), or
        # why it was not read.
        listings: dict[str, tuple[dict | None, bool] | str] = {}
        unreachable: list[str] = []
        refused: list[str] = []
        skipped = {"limit": 0, "cut": 0}

        def listing(directory):
            if directory not in listings:
                left = stop - time.monotonic()
                if unreachable:
                    return "down"
                if len(listings) >= MAX_LISTINGS or left <= 0:
                    return "limit"
                try:
                    listings[directory] = listed(
                        self.read(
                            self.address(self.api, directory, True) + "?json",
                            min(SHARE_TIMEOUT, left),
                        )
                    )
                except ValueError as exc:
                    refused.append(f"{directory or 'its top folder'} ({exc})")
                    listings[directory] = "refused"
                except OSError as exc:
                    unreachable.append(str(getattr(exc, "reason", None) or exc))
                    return "down"
            return listings[directory]

        folders: dict[str, dict] = {}
        missing = []

        def unread(why):
            if why in skipped:
                skipped[why] += 1

        for path, mention in named.items():
            parent, name = posixpath.split(path)
            read = listing(parent)
            if isinstance(read, str):
                unread(read)
                continue
            content, cut = read
            if content is None or name not in content:
                # A listing cut short may hold the name past its end.
                if content is not None and cut:
                    unread("cut")
                else:
                    missing.append({"path": path, "named": mention})
            elif not content[name]["folder"]:
                group = folders.setdefault(parent, {"rows": {}, "named": None})
                group["rows"][name] = self.row(parent, name, content[name], mention)
            else:
                inside = listing(path)
                if isinstance(inside, str):
                    unread(inside)
                elif inside[0] is None:
                    missing.append({"path": path, "named": mention})
                else:
                    group = folders.setdefault(path, {"rows": {}, "named": None})
                    group["named"] = group["named"] or mention
                    for inner, item in inside[0].items():
                        group["rows"].setdefault(inner, self.row(path, inner, item, None))
                    if inside[1]:
                        skipped["cut"] += 1
        if unreachable and not folders and not missing:
            raise ValueError(f"The share at {self.api} could not be read: {unreachable[0]}")

        def newest(rows):
            return max((row["modified"] or 0 for row in rows), default=0)

        groups = []
        for path, found in folders.items():
            if not found["rows"]:
                continue  # A folder that was named and holds nothing.
            rows = sorted(
                found["rows"].values(),
                key=lambda row: (row["kind"] == "folder", -(row["modified"] or 0), row["name"]),
            )
            # Files of the folder that nothing named, when the folder itself was not.
            read = listings.get(path)
            others = (
                0
                if found["named"] or not isinstance(read, tuple)
                else sum(
                    1
                    for name, item in (read[0] or {}).items()
                    if not item["folder"] and name not in found["rows"]
                )
            )
            groups.append(
                {
                    "path": path,
                    "url": self.address(self.url, path, True),
                    "named": found["named"],
                    "files": rows[:MAX_FILES],
                    "more": max(0, len(rows) - MAX_FILES),
                    "others": others,
                }
            )
        groups.sort(key=lambda group: (-newest(group["files"]), group["path"]))
        notes = []
        if unreachable:
            notes.append(f"The share could not be read completely: {unreachable[0]}.")
        if refused:
            more = f", nor {len(refused) - 1} more" if len(refused) > 1 else ""
            notes.append(f"The share did not list {refused[0]}{more}.")
        if skipped["cut"]:
            notes.append(
                f"A folder on the share holds more than {MAX_ENTRIES:,} entries;"
                " what is past them was not looked up."
            )
        if skipped["limit"]:
            count = skipped["limit"]
            notes.append(
                f"{count} named {'path was' if count == 1 else 'paths were'} not looked up:"
                " one answer reads only so much of the share."
            )
        return {"folders": groups, "missing": missing, "notes": notes}

    def listing(self, path, machine=None):
        """The artefacts of the checkout at ``path`` (on ``machine``, a worker, whose
        agents are found by the links they wrote only: its paths are its own)."""
        if not self.enabled:
            return {"enabled": False}
        if self.error:
            raise ValueError(self.error)
        local = machine is None
        expression = self.expression(local)
        matches, _, searched, behind = (
            workspace_viewer.matching(path, expression, MAX_MATCHES)
            if local
            else workspace_viewer.remote_matching(self.home, machine, path, expression, MAX_MATCHES)
        )
        named, clipped = self.named(matches, local)
        value = self.shared(named)
        if len(matches) >= MAX_MATCHES:
            value["notes"].append(
                f"Only the newest {MAX_MATCHES:,} transcript entries that mention the share"
                " were read."
            )
        if clipped:
            value["notes"].append(f"Only the newest {MAX_NAMED} names were looked up.")
        return {
            "enabled": True,
            "share": self.url + "/",
            **value,
            "searched": searched,
            "behind": behind,
        }


def spellings(root):
    """How agents write the shared directory ``root``: as configured, expanded, and
    resolved through links; a ``~/`` path also as typed."""
    root = root.rstrip("/") or "/"
    expanded = Path(root).expanduser()
    found = [str(expanded)]
    try:
        found.append(str(expanded.resolve()))
    except OSError:
        pass
    if root.startswith("~/"):
        found.append(root)
    unique = list(dict.fromkeys(found))
    if any(len(spelling) < 3 or '"' in spelling for spelling in unique):
        raise ValueError("`root` is too short to search for, or holds a quote")
    return unique
