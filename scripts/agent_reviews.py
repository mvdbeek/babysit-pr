"""Agents' reviews of pull requests: kept in sqlite, recorded through the API, imported from files.

Two kinds of review land in ``agent-reviews.sqlite`` under the dashboard's home:

- **sweep**: the review sweep (references/review-sweep.md) reviews open pull requests, one
  report per pull request and head. Its track step records each finished review with
  ``POST /api/agent-reviews``. Reviews from before the sweep was built in are imported
  from its old tracker, ``reviewed.tsv`` in the sweep's report folder.
- **pr-review**: the pr-review skill's rounds, in folders named
  ``pr-review-<owner>-<repo>-<number>``: in this machine's outbox, in a sandbox's artifact
  directory, or in its state fallback ``~/.local/state/pr-review``. Workers' folders reach
  the dufs share that the artefacts experiment configures (references/artefacts.md); one
  search there (``?q=pr-review-``, which needs dufs's ``--allow-search``) finds them all. A
  folder's newest round with a ``verified.md`` is imported, with the highest trim of each
  file in that round.

A report is one review: a row is keyed by its source and report path, so importing again
changes nothing. Imports run when an answer is asked for, at most once a minute.

Review files are written by agents: what is kept is their text, never markup, and pages
show it as text.
"""

import json
import os
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from stat import S_ISREG

import workspace_artefacts

SOURCES = ("sweep", "pr-review")
# pr-review files: `<date>-r<round>[-trim<k>]-<name>.md`.
FILE = re.compile(r"(\d{4}-\d{2}-\d{2})-r(\d+)-(?:trim(\d+)-)?([a-z][a-z0-9-]*)\.md")
FINDING = re.compile(r"^###\s+(P[123])\s+\d+:\s*(.+?)\s*$", re.M)
RISK = re.compile(r"<summary>\s*Risks\s*</summary>(.*?)</details>", re.S | re.I)
COMMIT = re.compile(r"reviewed (?:commit|head)\)?\**\s*:\**\s*`?([0-9a-f]{7,40})\b", re.I)
PULL = re.compile(r"https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/([1-9][0-9]*)")
SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9_.-]+")
SHA = re.compile(r"[0-9a-f]{7,40}")
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
# Sweep reports: `- Verdict: **ok, minors**. One sentence.`, or an older `## Verdict` section.
VERDICT = re.compile(r"^[-*\s]*\**Verdict:?\**:?\s*\*\*(.+?)\*\*\.?\s*(.*)$", re.M | re.I)
VERDICT_SECTION = re.compile(r"^##\s+Verdict\s*\n+(.+?)(?:\n\s*\n|\n#)", re.M | re.S | re.I)
BASE = re.compile(r"^[-*\s]*\**Base:?\**:?\s*`([^`]+)`", re.M | re.I)
SECTION = re.compile(r"^##\s+Findings\s*$(.*?)(?=^##\s|\Z)", re.M | re.S | re.I)
# `**M1. A claim.**`, `**1. A claim.**`, `**m1.** A claim.` or `- **n1 (…)** …`, under a
# `### Major` heading.
ITEM = re.compile(r"^(?:[-*]\s+)?\*\*[A-Za-z]{0,2}\d+[.:)]?\s*(.*?)\*\*[.:]?\s*(.*)$")
SEVERITIES = ("blocker", "critical", "major", "minor", "nit")
# How long an import is reused, how much of a review file is read, and how much is kept.
TTL = 60
MAX_FILE = 512 * 1024
MAX_FINDINGS = 20
MAX_TEXT = 400
MAX_FOLDERS = 200


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


def clip(text, limit=MAX_TEXT):
    text = " ".join(str(text or "").replace("**", "").split())
    return text[:limit] or None


def tone(severity):
    """How a severity reads: danger, attention or muted."""
    word = str(severity or "").lower()
    if word in {"p1", "blocker", "critical", "major"}:
        return "danger"
    return "attention" if word in {"p2", "minor"} else "muted"


def parse_report(text):
    """A sweep report's pull request, head, base, verdict and findings.

    Reports follow the brief's format (`- Verdict: **…**.`, `### Major|Minor|Nit` headings
    with `**M1. …**` items under `## Findings`); older ones have a `## Verdict` section and
    `### minor — <title>` headings. Anything not found is None or empty.
    """
    found: dict[str, object] = {"repo": None, "number": None, "head": None, "base": None}
    if match := PULL.search(text[:4000]):
        found["repo"], found["number"] = match[1], int(match[2])
    if match := COMMIT.search(text[:4000]):
        found["head"] = match[1]
    if match := BASE.search(text[:4000]):
        found["base"] = match[1]
    verdict = note = None
    if match := VERDICT.search(text[:6000]):
        verdict, note = clip(match[1], 60), clip(match[2])
    elif match := VERDICT_SECTION.search(text):
        note = clip(match[1])
        bold = re.search(r"\*\*(.+?)\*\*", match[1])
        verdict = clip(bold[1], 60) if bold else None
    findings = []
    if section := SECTION.search(text):
        severity = None
        for line in section[1].splitlines():
            if heading := re.match(r"^###\s+(.+)$", line):
                words = re.findall(r"[a-z]+", heading[1].lower())
                word = next((w for w in words[:1] if w in SEVERITIES), None)
                # `### minor — title`: the heading is the finding.
                rest = re.split(r"\s+[—–-]\s+|:\s+", heading[1], maxsplit=1)
                if word and len(rest) == 2:
                    findings.append({"severity": word.capitalize(), "title": clip(rest[1])})
                    severity = None
                else:
                    severity = word.capitalize() if word else None
            elif severity and (item := ITEM.match(line)):
                # The title is in bold, or follows a bold number; its first sentence.
                title = item[1].strip() or re.split(r"(?<=[.!?])\s", item[2].strip(), maxsplit=1)[0]
                title = clip(title.rstrip(".").strip(), 200)
                if title:
                    findings.append({"severity": severity, "title": title})
    found.update(verdict=verdict, note=note, findings=findings[:MAX_FINDINGS])
    return found


def latest_round(names):
    """The files of the newest pr-review round with findings, as (round, {name: file}): for
    each name the highest trim, then the round's own file. None without a ``verified.md``."""
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


def pr_review(round_, files, read):
    """A pr-review round as a review row, or None when its meta names no pull request.
    ``read(file)`` returns a file's text or None."""
    meta = read(files["meta"]) if "meta" in files else ""
    match = PULL.search(meta or "")
    if not match:
        return None  # A self-review of a branch with no PR yet.
    findings = [
        {"severity": priority, "title": clip(title.removesuffix(" (verified)"))}
        for priority, title in FINDING.findall(read(files["verified"]) or "")
    ]
    note = None
    if "reply" in files and (risk := RISK.search(read(files["reply"]) or "")):
        note = clip(risk[1].strip().split("\n\n")[0])
    head = COMMIT.search(meta or "")
    named = FILE.fullmatch(files["verified"])
    assert named  # latest_round() chose it by this pattern.
    return {
        "source": "pr-review",
        "repo": match[1],
        "number": int(match[2]),
        "head": head[1] if head else None,
        "date": named[1],
        "label": f"r{round_}" + (f" trim {named[3]}" if named[3] else ""),
        "verdict": None,
        "note": note,
        "findings": sorted(findings, key=lambda item: item["severity"])[:MAX_FINDINGS],
    }


def validate(review):
    """A review to record, checked and trimmed; raises ValueError."""
    if not isinstance(review, dict):
        raise ValueError("A review must be an object")
    known = {
        "source", "repo", "number", "head", "base", "date", "label", "verdict", "note",
        "findings", "session", "checkout", "report", "report_url", "reply_url", "modified",
    }  # fmt: skip
    if set(review) - known:
        raise ValueError(f"Unknown review fields: {', '.join(sorted(set(review) - known))}")
    if review.get("source") not in SOURCES:
        raise ValueError("source must be sweep or pr-review")
    if not SLUG.fullmatch(str(review.get("repo") or "")):
        raise ValueError("repo must be owner/name")
    number = review.get("number")
    if not isinstance(number, int) or isinstance(number, bool) or not 0 < number < 2**31:
        raise ValueError("number must be a positive integer")
    if review.get("head") is not None and not SHA.fullmatch(str(review["head"])):
        raise ValueError("head must be a commit sha")
    if review.get("date") is not None and not DATE.fullmatch(str(review["date"])):
        raise ValueError("date must be YYYY-MM-DD")
    if not isinstance(review.get("report"), str) or not review["report"]:
        raise ValueError("report must name the report file")
    modified = review.get("modified")
    if modified is not None and (
        not isinstance(modified, (int, float)) or isinstance(modified, bool)
    ):
        raise ValueError("modified must be a time")
    findings = review.get("findings") or []
    if not isinstance(findings, list) or not all(
        isinstance(f, dict) and isinstance(f.get("title"), str) for f in findings
    ):
        raise ValueError("findings must be a list of {severity, title}")
    for name in ("report_url", "reply_url"):
        url = review.get(name)
        if url is not None and urllib.parse.urlsplit(str(url)).scheme not in {"http", "https"}:
            raise ValueError(f"{name} must be a web address")
    clean = {
        name: (clip(review.get(name), 1024) if review.get(name) is not None else None)
        for name in ("base", "label", "session", "checkout", "report_url", "reply_url")
    }
    return {
        **clean,
        "source": review["source"],
        "repo": review["repo"],
        "number": number,
        "head": review.get("head"),
        "date": review.get("date"),
        "verdict": clip(review.get("verdict"), 60),
        "note": clip(review.get("note")),
        "findings": [
            {"severity": clip(f.get("severity"), 20), "title": clip(f["title"])}
            for f in findings[:MAX_FINDINGS]
        ],
        "report": review["report"][:1024],
        "modified": float(time.time() if modified is None else modified),
    }


def read_contained(path, root):
    """(text, mtime) of a regular file whose folder is below ``root``, read without
    following a link in its last part; None when there is no such file.

    Agents can write in the folders reviews are read from: a link there must not make the
    dashboard read, and answer with, a file elsewhere.
    """
    path = Path(path)
    try:
        if not path.parent.resolve().is_relative_to(Path(root).resolve()):
            return None
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return None
    try:
        stat = os.fstat(fd)
        if not S_ISREG(stat.st_mode):
            return None
        with os.fdopen(fd, "rb", closefd=False) as handle:
            return handle.read(MAX_FILE).decode("utf-8", "replace"), stat.st_mtime
    finally:
        os.close(fd)


def public(row):
    """A stored review as the API answers it."""
    findings = json.loads(row["findings"] or "[]")
    counts: dict[str, int] = {}
    for finding in findings:
        counts[finding["severity"]] = counts.get(finding["severity"], 0) + 1
    order = {"danger": 0, "attention": 1, "muted": 2}
    return {
        "source": row["source"],
        "repo": row["repo"],
        "number": row["number"],
        "url": f"https://github.com/{row['repo']}/pull/{row['number']}",
        "head": row["head"],
        "base": row["base"],
        "date": row["date"],
        "label": row["label"],
        "verdict": row["verdict"],
        "note": row["note"],
        "findings": [{**f, "tone": tone(f["severity"])} for f in findings],
        "counts": [
            {"severity": severity, "count": count, "tone": tone(severity)}
            for severity, count in sorted(counts.items(), key=lambda c: order[tone(c[0])])
        ],
        "session": row["session"],
        "checkout": row["checkout"],
        # Where the report is: a path on this machine, or `share:<path>` for a worker's.
        "report": row["report"],
        "links": {
            name: url
            for name, url in (("report", row["report_url"]), ("reply", row["reply_url"]))
            if url
        },
        "modified": row["modified"],
    }


class Store:
    """``agent-reviews.sqlite``: one row per review report."""

    def __init__(self, home):
        self.path = Path(home) / "agent-reviews.sqlite"

    @contextmanager
    def db(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute(
            "CREATE TABLE IF NOT EXISTS reviews (source TEXT, report TEXT, repo TEXT, "
            "number INTEGER, head TEXT, base TEXT, date TEXT, label TEXT, verdict TEXT, "
            "note TEXT, findings TEXT, session TEXT, checkout TEXT, report_url TEXT, "
            "reply_url TEXT, modified REAL, recorded_at REAL, PRIMARY KEY(source, report))"
        )
        db.execute("CREATE INDEX IF NOT EXISTS reviews_pr ON reviews (repo COLLATE NOCASE, number)")
        db.execute("CREATE TABLE IF NOT EXISTS imports (name TEXT PRIMARY KEY, state TEXT)")
        try:
            with db:
                yield db
        finally:
            db.close()

    def record(self, review, db=None):
        review = validate(review)
        values = {**review, "findings": json.dumps(review["findings"]), "recorded_at": time.time()}
        columns = ",".join(values)
        sql = (
            f"INSERT INTO reviews ({columns}) VALUES ({','.join('?' * len(values))}) "
            "ON CONFLICT(source, report) DO UPDATE SET "
            + ",".join(
                f"{name}=excluded.{name}" for name in values if name not in {"source", "report"}
            )
        )
        if db is not None:
            db.execute(sql, list(values.values()))
        else:
            with self.db() as own:
                own.execute(sql, list(values.values()))
        return review

    def known(self):
        """{(source, report): modified} of every stored review: what an import skips."""
        with self.db() as db:
            rows = db.execute("SELECT source, report, modified FROM reviews").fetchall()
        return {(row["source"], row["report"]): row["modified"] for row in rows}

    def imported(self, name):
        with self.db() as db:
            row = db.execute("SELECT state FROM imports WHERE name=?", (name,)).fetchone()
        return row["state"] if row else None

    def record_all(self, reviews, imports=()):
        """Record imported reviews, and what was imported, in one short transaction."""
        with self.db() as db:
            for review in reviews:
                try:
                    self.record(review, db)
                except ValueError:
                    continue
            for name, state in imports:
                db.execute("INSERT OR REPLACE INTO imports VALUES (?,?)", (name, state))

    def reviews(self, repo, number):
        """A pull request's reviews, newest first."""
        with self.db() as db:
            rows = db.execute(
                "SELECT * FROM reviews WHERE repo=? COLLATE NOCASE AND number=? "
                "ORDER BY date DESC, modified DESC",
                (repo, number),
            ).fetchall()
        return [public(row) for row in rows]

    def latest(self):
        """Every reviewed pull request's newest review, by `owner/repo#number` (lowercase)."""
        found = {}
        with self.db() as db:
            for row in db.execute("SELECT * FROM reviews ORDER BY date, modified"):
                found[f"{row['repo'].lower()}#{row['number']}"] = public(row)
        return found

    def heads(self, source="sweep"):
        """{`owner/repo#number` (lowercase): [reviewed heads]} for one source: what the
        sweep skips."""
        found: dict[str, list[str]] = {}
        with self.db() as db:
            for row in db.execute(
                "SELECT repo, number, head FROM reviews WHERE source=? AND head IS NOT NULL",
                (source,),
            ):
                # One list per pull request, however its owner and name are spelled.
                found.setdefault(f"{row['repo']}#{row['number']}".lower(), []).append(row["head"])
        return found


class Reviews:
    """The store, filled from the API and from files on disk and on the artefacts share."""

    def __init__(
        self, home, artefacts=None, settings=None, user_home=None, read_share=None, read=None
    ):
        self.store = Store(home)
        self.artefacts = artefacts
        self.user_home = Path(user_home or Path.home())
        # A callable: the review sweep's settings (sweep_settings.load), or None.
        self.settings = settings
        self.read_share = read_share or workspace_artefacts.read_share
        self.read = read or read_text
        self.lock = threading.Lock()
        self.refreshed = 0.0

    # Reading -------------------------------------------------------------------------

    def for_pr(self, repo, number):
        self.refresh()
        return self.store.reviews(repo, number)

    def latest(self):
        self.refresh()
        return self.store.latest()

    def heads(self):
        self.refresh()
        return self.store.heads()

    def record(self, review):
        """Record a finished sweep review (``POST /api/agent-reviews``).

        The report must be a file in the sweep's report folder; its verdict, findings and
        base are read from it, never taken from the caller, which may be any agent on this
        machine.
        """
        if not isinstance(review, dict):
            raise ValueError("A review must be an object")
        given = {
            "source",
            "repo",
            "number",
            "head",
            "base",
            "date",
            "label",
            "session",
            "checkout",
            "report",
        }
        if set(review) - given:
            raise ValueError(f"Unknown review fields: {', '.join(sorted(set(review) - given))}")
        if review.get("source") != "sweep":
            raise ValueError("Only the review sweep records reviews")
        out = self.sweep_folder()
        report = review.get("report")
        if not out or not isinstance(report, str) or not Path(report).is_absolute():
            raise ValueError("report must be a file in the review sweep's report folder")
        path = Path(report)
        read = read_contained(path, out)
        if read is None:
            raise ValueError(f"{path.name} is not a report in the review sweep's report folder")
        text, modified = read
        parsed = parse_report(text)
        return public_record(
            self.store.record(
                {
                    **review,
                    "base": review.get("base") or parsed["base"],
                    "label": review.get("label") or label_of(path.name),
                    "verdict": parsed["verdict"],
                    "note": parsed["note"],
                    "findings": parsed["findings"],
                    "report": str(path),
                    "report_url": self.share_link(path),
                    "modified": modified,
                }
            )
        )

    def sweep_folder(self):
        try:
            settings = self.settings() if self.settings else None
        except (ValueError, OSError):
            settings = None
        return Path(settings["out"]).expanduser() if settings else None

    # Importing -----------------------------------------------------------------------

    def refresh(self, force=False):
        """Import what changed since the last import, at most once per ``TTL``.

        Files and the share are read first; what changed is then written in one short
        transaction, so recording a review never waits behind a slow share. While another
        request imports, the others answer from what is stored.
        """
        if not self.lock.acquire(blocking=force):
            return
        try:
            if not force and time.monotonic() - self.refreshed < TTL:
                return
            self.refreshed = time.monotonic()
            known = self.store.known()
            found: list[dict] = []
            imports: list[tuple[str, str]] = []
            out = self.sweep_folder()
            if out:
                imports += self.import_tracker(out, known, found)
            self.import_local_rounds(known, found)
            self.import_shared_rounds(known, found)
            if found or imports:
                self.store.record_all(found, imports)
        finally:
            self.lock.release()

    def share_link(self, path):
        """The share's address of a file on this machine's disk, when the artefacts
        experiment names the directory it serves (``root``)."""
        artefacts = self.artefacts
        if not artefacts or not artefacts.enabled or artefacts.error:
            return None
        for spelling in artefacts.spellings:
            root = Path(spelling)
            try:
                relative = Path(path).resolve().relative_to(root.resolve())
            except (ValueError, OSError):
                continue
            return f"{artefacts.url}/{urllib.parse.quote(relative.as_posix())}?view"
        return None

    def import_tracker(self, out, known, found):
        """Rows of the sweep's old tracker, ``reviewed.tsv``: pr key, head, base, date,
        report path (below ``~/src/agent-artefacts``), session id. Returns the import state
        to keep. A row whose report is gone is kept too, so its head still counts as
        reviewed."""
        tracker = out / "reviewed.tsv"
        read = read_contained(tracker, out)
        if read is None:
            return []
        text, tracker_modified = read
        state = f"{tracker_modified}:{len(text)}"
        if self.store.imported(str(tracker)) == state:
            return []
        # Reports are in the outbox the sweep's folder is in.
        outbox = out.parent
        for line in text.splitlines():
            parts = line.split("\t")
            if len(parts) < 5 or parts[0].startswith("#"):
                continue
            key, head, base, date, report = parts[:5]
            session = parts[5] if len(parts) > 5 else None
            path = Path(report)
            if not path.is_absolute():
                # Tracker paths start at the artefacts directory: `outbox/<folder>/<file>`.
                path = outbox.parent / report
            report_read = read_contained(path, outbox)
            if report_read is None and (out / path.name).exists():
                path = out / path.name
                report_read = read_contained(path, out)
            text_, modified = report_read or ("", tracker_modified)
            if abs(known.get(("sweep", str(path)), -10) - modified) < 1:
                continue
            parsed = parse_report(text_)
            repo, number = parsed["repo"], parsed["number"]
            if not repo:
                repo, number = self.sweep_key(key)
            if not repo or not number:
                continue
            found.append(
                {
                    "source": "sweep",
                    "repo": repo,
                    "number": number,
                    "head": head if SHA.fullmatch(head) else parsed["head"],
                    "base": base or parsed["base"],
                    "date": date if DATE.fullmatch(date) else None,
                    "label": label_of(path.name),
                    "verdict": parsed["verdict"],
                    "note": parsed["note"],
                    "findings": parsed["findings"],
                    "session": session or None,
                    "report": str(path),
                    "report_url": self.share_link(path) if report_read else None,
                    "modified": modified,
                }
            )
        return [(str(tracker), state)]

    def sweep_key(self, key):
        """A sweep key: `<N>` (the default repository), `<repo>#<N>` (the owner's) or
        `<owner>/<repo>#<N>`, as (owner/repo, number); (None, None) when it names none."""
        match = re.fullmatch(r"(?:([\w.-]+)/([\w.-]+)#|([\w.-]+)#)?([1-9][0-9]*)", key.strip())
        if not match:
            return None, None
        owner, repo, short, number = match.groups()
        settings = self.settings() if self.settings else {}
        owner = owner or settings.get("owner")
        repo = repo or short or settings.get("default_repo")
        return (f"{owner}/{repo}", int(number)) if owner and repo else (None, None)

    def local_round_folders(self):
        home = self.user_home
        state = Path(os.environ.get("XDG_STATE_HOME") or home / ".local" / "state")
        found = [
            *sorted((home / "src" / "agent-artefacts" / "outbox").glob("pr-review-*")),
            *sorted((state / "pr-review").glob("*")),
            *sorted((home / ".cache" / "babysit-sandbox").glob("*/artifacts/pr-review-*")),
        ]
        return [path for path in found if path.is_dir()][:MAX_FOLDERS]

    def import_local_rounds(self, known, found):
        for folder in self.local_round_folders():
            try:
                names = {entry.name: entry for entry in folder.iterdir() if entry.is_file()}
            except OSError:
                continue
            if not (latest := latest_round(names)):
                continue
            round_, files = latest
            report = folder / files["verified"]
            try:
                modified = names[files["verified"]].stat().st_mtime
            except OSError:
                continue
            if abs(known.get(("pr-review", str(report)), -10) - modified) < 1:
                continue

            def read(file, folder=folder):
                read = read_contained(folder / file, folder)
                return read[0] if read else None

            review = pr_review(round_, files, read)
            if review:
                links = {
                    f"{name}_url": self.share_link(folder / files[name])
                    for name in ("verified", "reply")
                    if name in files
                }
                review.update(
                    report=str(report),
                    report_url=links.get("verified_url"),
                    reply_url=links.get("reply_url"),
                    modified=modified,
                )
                found.append(review)

    def import_shared_rounds(self, known, found):
        """Workers' pr-review folders, found on the share with one search."""
        artefacts = self.artefacts
        if not artefacts or not artefacts.enabled or artefacts.error:
            return
        try:
            value = self.read_share(f"{artefacts.api}/?q=pr-review-&json")
        except (OSError, ValueError):
            return
        # Without --allow-search dufs answers the plain listing instead.
        if not isinstance(value, dict) or value.get("allow_search") is False:
            return
        folders = []
        for item in value.get("paths") or []:
            if not isinstance(item, dict) or item.get("path_type") not in {"Dir", "SymlinkDir"}:
                continue
            path = workspace_artefacts.clean(str(item.get("name") or ""))
            if path and path.rsplit("/", 1)[-1].startswith("pr-review-"):
                folders.append(path)
        for path in folders[:MAX_FOLDERS]:
            quoted = urllib.parse.quote(path)
            try:
                listing, _ = workspace_artefacts.listed(
                    self.read_share(f"{artefacts.api}/{quoted}/?json")
                )
            except (OSError, ValueError):
                continue
            if not listing or not (
                latest := latest_round(n for n, v in listing.items() if not v["folder"])
            ):
                continue
            round_, files = latest
            report = f"share:{path}/{files['verified']}"
            # A share that gives no time: 0, so the folder is not read again every minute.
            modified = listing[files["verified"]]["modified"] or 0
            if abs(known.get(("pr-review", report), -10) - modified) < 1:
                continue

            def read(file, quoted=quoted):
                try:
                    return self.read(f"{artefacts.api}/{quoted}/{urllib.parse.quote(file)}")
                except (OSError, ValueError):
                    return None

            review = pr_review(round_, files, read)
            if review:
                link = f"{artefacts.url}/{quoted}/"
                review.update(
                    report=report,
                    report_url=link + urllib.parse.quote(files["verified"]) + "?view",
                    reply_url=(
                        link + urllib.parse.quote(files["reply"]) + "?view"
                        if "reply" in files
                        else None
                    ),
                    modified=modified,
                )
                found.append(review)


def public_record(review):
    """What recording answers: the review as stored, without its local paths."""
    return {k: v for k, v in review.items() if k not in {"checkout"}}


def label_of(name):
    """A sweep report's run label: `2026-11-23-run2-pr25053.md` -> `2026-11-23-run2`."""
    match = re.match(r"(\d{4}-\d{2}-\d{2}(?:-run\d+)?)-", name)
    return match[1] if match else None
