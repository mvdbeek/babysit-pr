"""The latest pr-review round of a pull request, summarised for the browser extension.

The pr-review skill keeps each pull request's rounds in a folder named
``pr-review-<owner>-<repo>-<number>``, in the outbox of the machine or sandbox it ran in,
or in its state fallback ``~/.local/state/pr-review/<owner>-<repo>-<number>``. This
machine's folders are read from disk. Workers' folders reach the dufs share that the
artefacts experiment configures (references/artefacts.md); one search there
(``?q=pr-review-``, which needs dufs's ``--allow-search``) finds them all.

Files are named ``<date>-r<round>[-trim<k>]-<name>.md``. A summary comes from the highest
round that has a ``verified.md``, with the highest trim of each file in that round. Several
folders can hold one pull request's reviews (each sandbox numbers its own rounds), so the
folder whose latest findings changed last wins.

Nothing is written. Review files are written by agents: a summary carries their text,
never markup, and the page shows it as text.
"""

import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import workspace_artefacts

FILE = re.compile(r"(\d{4}-\d{2}-\d{2})-r(\d+)-(?:trim(\d+)-)?([a-z][a-z0-9-]*)\.md")
FINDING = re.compile(r"^###\s+(P[123])\s+\d+:\s*(.+?)\s*$", re.M)
RISK = re.compile(r"<summary>\s*Risks\s*</summary>(.*?)</details>", re.S | re.I)
COMMIT = re.compile(r"reviewed commit\)?\s*:\s*`?([0-9a-f]{7,40})\b", re.I)
SLUG = re.compile(r"[A-Za-z0-9_.-]+")
# How long one search of the share and one summary are reused, how much of a review file
# is read, and how many findings and folders a summary looks at.
TTL = 60
MAX_FILE = 512 * 1024
MAX_FINDINGS = 8
MAX_FOLDERS = 6
MAX_RISK = 400


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def read_text(url, timeout=workspace_artefacts.SHARE_TIMEOUT):
    """A review file from the share, cut at ``MAX_FILE``; None when it is not there."""
    opener = urllib.request.build_opener(_NoRedirect, urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=timeout) as response:
            return response.read(MAX_FILE).decode("utf-8", "replace")
    except urllib.error.HTTPError:
        return None


def folder_name(owner, repo, number):
    return f"pr-review-{owner}-{repo}-{number}"


def latest(names):
    """The files of the newest round with findings, as {name: file name}: for each name
    the highest trim, then the round's own file. None without a ``verified.md``."""
    rounds: dict[int, dict[str, tuple[int, str]]] = {}
    for file in names:
        match = FILE.fullmatch(file)
        if not match:
            continue
        number, trim, name = int(match[2]), int(match[3] or 0), match[4]
        found = rounds.setdefault(number, {})
        if name not in found or trim > found[name][0]:
            found[name] = (trim, file)
    for number in sorted(rounds, reverse=True):
        if "verified" in rounds[number]:
            return number, {name: file for name, (_, file) in rounds[number].items()}
    return None


def summarise(number, files, read):
    """A round's summary from its files; ``read(file)`` returns a file's text or None."""
    verified = read(files["verified"]) or ""
    counts = {"P1": 0, "P2": 0, "P3": 0}
    findings = []
    for priority, title in FINDING.findall(verified):
        counts[priority] += 1
        findings.append({"priority": priority, "title": title.removesuffix(" (verified)")})
    risk = None
    if "reply" in files and (match := RISK.search(read(files["reply"]) or "")):
        paragraph = match[1].strip().split("\n\n")[0]
        risk = " ".join(paragraph.replace("**", "").split())[:MAX_RISK] or None
    commit = None
    if "meta" in files and (match := COMMIT.search(read(files["meta"]) or "")):
        commit = match[1]
    named = FILE.fullmatch(files["verified"])
    assert named  # latest() chose it by this pattern.
    return {
        "round": number,
        "trim": int(named[3]) if named[3] else None,
        "date": named[1],
        "counts": counts,
        # Most severe first, as verified.md orders them within a priority.
        "findings": sorted(findings, key=lambda item: item["priority"])[:MAX_FINDINGS],
        "risk": risk,
        "commit": commit,
    }


@dataclass
class Candidate:
    """One folder's newest round with findings."""

    modified: float  # When its latest verified.md changed.
    shared: bool  # Read from the share rather than this machine's disk.
    round: int
    files: dict[str, str]
    read: Callable[[str], str | None]
    link: Callable[[str], str] | None


class Reviews:
    """Summaries of pr-review rounds on this machine's disk and on the artefacts share."""

    def __init__(self, artefacts, home=None, read_share=None, read=None):
        self.artefacts = artefacts
        self.home = Path(home or Path.home())
        self.read_share = read_share or workspace_artefacts.read_share
        self.read = read or read_text
        self.lock = threading.Lock()
        self.index: tuple[float, dict] | None = None
        self.summaries: dict[tuple, tuple[float, dict | None]] = {}

    def local_folders(self, owner, repo, number):
        name = folder_name(owner, repo, number)
        state = Path(os.environ.get("XDG_STATE_HOME") or self.home / ".local" / "state")
        found = [
            self.home / "src" / "agent-artefacts" / "outbox" / name,
            state / "pr-review" / f"{owner}-{repo}-{number}",
            *sorted((self.home / ".cache" / "babysit-sandbox").glob(f"*/artifacts/{name}")),
        ]
        return [path for path in found if path.is_dir()]

    def shared(self):
        """{folder name: [paths below the share]} from one search, reused for ``TTL``."""
        artefacts = self.artefacts
        if not artefacts or not artefacts.enabled or artefacts.error:
            return {}
        now = time.monotonic()
        if self.index and now - self.index[0] < TTL:
            return self.index[1]
        folders: dict[str, list[str]] = {}
        try:
            value = self.read_share(f"{artefacts.api}/?q=pr-review-&json")
        except (OSError, ValueError):
            value = None
        # Without --allow-search dufs answers the plain listing instead.
        if isinstance(value, dict) and value.get("allow_search") is not False:
            for item in value.get("paths") or []:
                if not isinstance(item, dict) or item.get("path_type") not in {"Dir", "SymlinkDir"}:
                    continue
                path = workspace_artefacts.clean(str(item.get("name") or ""))
                if path and path.rsplit("/", 1)[-1].startswith("pr-review-"):
                    folders.setdefault(path.rsplit("/", 1)[-1], []).append(path)
        self.index = (now, folders)
        return folders

    def candidates(self, owner, repo, number):
        """Each folder's newest round with findings, on disk and on the share."""
        found: list[Candidate] = []
        for path in self.local_folders(owner, repo, number)[:MAX_FOLDERS]:
            try:
                names = {entry.name: entry for entry in path.iterdir() if entry.is_file()}
            except OSError:
                continue
            if result := latest(names):
                number_, files = result

                def read(file, path=path):
                    try:
                        with open(path / file, encoding="utf-8", errors="replace") as handle:
                            return handle.read(MAX_FILE)
                    except OSError:
                        return None

                modified = names[files["verified"]].stat().st_mtime
                found.append(Candidate(modified, False, number_, files, read, None))
        artefacts = self.artefacts
        for path in self.shared().get(folder_name(owner, repo, number), [])[:MAX_FOLDERS]:
            quoted = urllib.parse.quote(path)
            try:
                listing, _ = workspace_artefacts.listed(
                    self.read_share(f"{artefacts.api}/{quoted}/?json")
                )
            except (OSError, ValueError):
                continue
            if not listing or not (
                result := latest(n for n, v in listing.items() if not v["folder"])
            ):
                continue
            number_, files = result

            def share_read(file, quoted=quoted):
                try:
                    return self.read(f"{artefacts.api}/{quoted}/{urllib.parse.quote(file)}")
                except (OSError, ValueError):
                    return None

            def link(file, quoted=quoted):
                return f"{artefacts.url}/{quoted}/{urllib.parse.quote(file)}?view"

            modified = listing[files["verified"]]["modified"] or 0
            found.append(Candidate(modified, True, number_, files, share_read, link))
        return found

    def summary(self, owner, repo, number):
        """The latest review of ``owner/repo#number``, or None; reused for ``TTL``."""
        if not SLUG.fullmatch(owner) or not SLUG.fullmatch(repo):
            return None
        key = (owner.lower(), repo.lower(), number)
        now = time.monotonic()
        with self.lock:
            cached = self.summaries.get(key)
            if cached and now - cached[0] < TTL:
                return cached[1]
            found = self.candidates(owner, repo, number)
            value = None
            if found:
                # A local folder pulled to the share is the same review: its copy there,
                # with the same latest findings file, gives the links.
                best = max(found, key=lambda c: (c.modified, c.shared))
                files, link = best.files, best.link
                if link is None:
                    link = next(
                        (
                            c.link
                            for c in found
                            if c.shared and c.files["verified"] == files["verified"]
                        ),
                        None,
                    )
                value = summarise(best.round, files, best.read)
                value["modified"] = best.modified
                value["links"] = (
                    {name: link(files[name]) for name in ("verified", "reply") if name in files}
                    if link
                    else {}
                )
            self.summaries = {k: v for k, v in self.summaries.items() if now - v[0] < TTL}
            self.summaries[key] = (now, value)
            return value
