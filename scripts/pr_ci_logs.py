"""Rate-limited background collection of failed GitHub Actions job logs."""

import contextlib
import fcntl
import gzip
import hashlib
import json
import os
import re
import selectors
import sqlite3
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import github_cli

DISCOVERIES_PER_HOUR = 12
DOWNLOADS_PER_HOUR = 20
MAX_LOG_BYTES = 8 * 1024 * 1024
MAX_CACHE_BYTES = 128 * 1024 * 1024
MAX_CACHED_JOBS = 200
MAX_PENDING_JOBS = 500
RETENTION = 7 * 86400
EXCERPT_BYTES = 128 * 1024
PAGE_CHARACTERS = 64 * 1024
SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+")
ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))")
STAMP = re.compile(r"^\d{4}-\d\d-\d\dT[\d:.]+Z ?")
FAILED = {"failure", "timed_out", "startup_failure", "action_required"}


def job_target(check):
    """Only structured Actions provenance and a matching GitHub URL can select a job."""
    repo = check.get("repository") or ""
    run_id = check.get("run_id")
    check_id = check.get("database_id")
    if (
        check.get("bucket") != "fail"
        or not SLUG.fullmatch(repo)
        or repo.split("/")[1] in {".", ".."}
        or not isinstance(run_id, int)
        or run_id <= 0
        or not isinstance(check_id, int)
        or check_id <= 0
    ):
        return None
    url = urlsplit(check.get("url") or "")
    if url.scheme != "https" or url.netloc != "github.com":
        return None
    modern = re.fullmatch(r"/([^/]+/[^/]+)/actions/runs/(\d+)/job/(\d+)/?", url.path)
    legacy = re.fullmatch(r"/([^/]+/[^/]+)/runs/(\d+)/?", url.path)
    if modern and modern[1].lower() == repo.lower() and int(modern[2]) == run_id:
        job_id = int(modern[3])
    elif legacy and legacy[1].lower() == repo.lower() and int(legacy[2]) == check_id:
        job_id = check_id
    else:
        return None
    return {"repo": repo.lower(), "job_id": job_id, "run_id": run_id, "database_id": check_id}


def read_job(target):
    endpoint = f"repos/{target['repo']}/actions/jobs/{target['job_id']}"
    result = github_cli.run(
        ["gh", "api", "--hostname", "github.com", endpoint],
        text=True,
        timeout=30,
    )
    if result.returncode:
        raise ValueError(f"Cannot read job metadata: {result.stderr.strip()[:500]}")
    return json.loads(result.stdout)


def download_log(target, *, timeout=90, limit=MAX_LOG_BYTES):
    """Stream one plain-text job log, stopping transfer at the byte/time limits."""
    endpoint = f"repos/{target['repo']}/actions/jobs/{target['job_id']}/logs"
    started = time.monotonic()
    chunks = bytearray()
    with (
        tempfile.TemporaryFile() as errors,
        github_cli.command(
            ["gh", "api", "--hostname", "github.com", endpoint],
            stdout=subprocess.PIPE,
            stderr=errors,
        ) as proc,
    ):
        assert proc.stdout is not None
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(proc.stdout, selectors.EVENT_READ)
                while True:
                    if time.monotonic() - started >= timeout:
                        raise TimeoutError("Job log download timed out")
                    if selector.select(0.2):
                        data = os.read(proc.stdout.fileno(), min(65536, limit + 1 - len(chunks)))
                        if not data:
                            break
                        chunks.extend(data)
                        if len(chunks) > limit:
                            proc.kill()
                            proc.wait()
                            return bytes(chunks[:limit]), True
                    elif proc.poll() is not None:
                        break
            code = proc.wait(timeout=max(0.1, timeout - (time.monotonic() - started)))
            if code:
                errors.seek(0)
                message = errors.read(2000).decode("utf-8", errors="replace")
                raise ValueError(
                    f"Job logs unavailable (possibly expired or not yet published): {message[:500]}"
                )
            return bytes(chunks), False
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()


def summarize_log(data):
    lines = [
        STAMP.sub("", ANSI.sub("", line))
        for line in data.decode("utf-8", errors="replace").splitlines()
    ]
    tests: list[str] = []
    indices = set(range(max(0, len(lines) - 100), len(lines)))
    hits = 0
    for i, line in enumerate(lines):
        match = re.match(r"(?:FAILED\s+|(?:FAIL|ERROR):\s+)(.+)", line)
        if match:
            name = match[1].split(" - ", 1)[0][:500]
            if name not in tests and len(tests) < 100:
                tests.append(name)
        if hits < 300 and re.search(
            r"FAILED\s|FAIL:|ERROR:|AssertionError|Traceback \(|##\[error\]|^E\s{2,}", line
        ):
            indices.update(range(max(0, i - 5), min(len(lines), i + 13)))
            hits += 1
    parts = []
    previous = -2
    for i in sorted(indices):
        if i != previous + 1:
            parts.append("…")
        parts.append(lines[i])
        previous = i
    encoded = "\n".join(parts).encode()
    return {
        "tests": tests,
        "text": encoded[-EXCERPT_BYTES:].decode(errors="replace"),
        "excerpt": len(indices) < len(lines) or len(encoded) > EXCERPT_BYTES,
    }


class BackgroundLogs:
    def __init__(self, home, overview, ci):
        self.overview = overview
        self.ci = ci
        self.root = Path(home) / "ci-job-logs"
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = Path(home) / "pr-ci-logs.sqlite"
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        self.stopping = threading.Event()
        self.worker: threading.Thread | None = None
        self.error = None
        with self.db() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS jobs (key TEXT PRIMARY KEY, account TEXT, pr TEXT, head TEXT, check_id TEXT, state TEXT, retry REAL, updated REAL, data TEXT)"
            )
            db.execute("CREATE TABLE IF NOT EXISTS scans (key TEXT PRIMARY KEY, checked REAL)")
            db.execute("CREATE TABLE IF NOT EXISTS requests (kind TEXT, time REAL)")

    @contextlib.contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def start(self):
        if self.worker and self.worker.is_alive():
            return
        self.worker = threading.Thread(target=self.run, name="ci-log-downloads", daemon=True)
        self.worker.start()

    def close(self):
        self.stopping.set()

    def run(self):
        # The lock also prevents duplicate workers when another dashboard uses this state.
        with (self.root / "worker.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            with self.db() as db:
                db.execute("UPDATE jobs SET state='queued' WHERE state='downloading'")
            while not self.stopping.is_set():
                try:
                    self.step()
                    self.error = None
                except (
                    OSError,
                    ValueError,
                    KeyError,
                    TypeError,
                    sqlite3.Error,
                    subprocess.SubprocessError,
                ) as exc:
                    self.error = str(exc)
                self.stopping.wait(15)

    def budget(self, kind, limit, *, claim=False):
        now = time.time()
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM requests WHERE time < ?", (now - 3600,))
            used = db.execute("SELECT count(*) FROM requests WHERE kind=?", (kind,)).fetchone()[0]
            if used >= limit:
                return False
            if claim:
                db.execute("INSERT INTO requests VALUES (?,?)", (kind, now))
            return True

    @staticmethod
    def job_key(account, target):
        return hashlib.sha256(
            json.dumps([account, target["repo"], target["job_id"]]).encode()
        ).hexdigest()

    def enqueue(self, account, pr, checks):
        now = time.time()
        with self.db() as db:
            pending = db.execute(
                "SELECT count(*) FROM jobs WHERE state IN ('queued','error','downloading')"
            ).fetchone()[0]
            for check in checks["checks"]:
                target = job_target(check)
                if not target:
                    continue
                key = self.job_key(account, target)
                existing = db.execute("SELECT state FROM jobs WHERE key=?", (key,)).fetchone()
                if existing:
                    # A job may be shared by multiple PRs. Keep an active binding.
                    db.execute(
                        "UPDATE jobs SET pr=?,head=?,check_id=? WHERE key=?",
                        (pr["id"], pr["head_sha"], check["id"], key),
                    )
                    continue
                if pending >= MAX_PENDING_JOBS:
                    continue
                data = {
                    **target,
                    "key": key,
                    "name": check["name"],
                    "sha": checks["sha"],
                    "head_sha": pr["head_sha"],
                    "attempts": 0,
                    "error": None,
                }
                db.execute(
                    "INSERT OR IGNORE INTO jobs VALUES (?,?,?,?,?,'queued',0,?,?)",
                    (key, account, pr["id"], pr["head_sha"], check["id"], now, json.dumps(data)),
                )
                pending += 1

    def cached_checks(self, account, pr):
        key = self.ci.key(account, pr)
        with self.ci.lock:
            entry = self.ci.entries.get(key, {})
            return (
                entry.get("value")
                if entry.get("expires_at", 0) > time.time() and not entry.get("error")
                else None
            )

    def step(self):
        self.prune()
        overview = self.overview.snapshot()
        if overview.get("error") or overview.get("refreshing") or not overview.get("synced_at"):
            return
        account = overview.get("login") or ""
        prs = {p["id"]: p for p in overview["prs"] if p.get("head_sha")}
        for pr in prs.values():
            cached = self.cached_checks(account, pr)
            if cached:
                self.enqueue(account, pr, cached)
        with self.db() as db:
            queued = db.execute(
                "SELECT key,pr,head,data FROM jobs WHERE account=? AND state IN ('queued','error') AND retry <= ? ORDER BY updated",
                (account, time.time()),
            ).fetchall()
            for key, pr_id, head, _ in queued:
                if pr_id not in prs or prs[pr_id]["head_sha"] != head:
                    db.execute(
                        "UPDATE jobs SET state='obsolete',updated=? WHERE key=?", (time.time(), key)
                    )
            queued = [r for r in queued if r[1] in prs and prs[r[1]]["head_sha"] == r[2]]
        if queued and self.budget("download", DOWNLOADS_PER_HOUR, claim=True):
            self.fetch(queued[0][0], json.loads(queued[0][3]))
            return
        candidates = []
        with self.db() as db:
            for pr in prs.values():
                if pr.get("ci") not in {"FAILURE", "ERROR"} or self.cached_checks(account, pr):
                    continue
                key = self.ci.key(account, pr)
                row = db.execute("SELECT checked FROM scans WHERE key=?", (key,)).fetchone()
                checked = row[0] if row else 0
                if checked <= time.time() - 3600:
                    candidates.append((checked, key, pr))
        if candidates and self.budget("discovery", DISCOVERIES_PER_HOUR, claim=True):
            _, key, pr = min(candidates, key=lambda c: c[0])
            # Reuse CiDetails' in-flight request/cache; do not fetch every PR on each tick.
            result = self.ci.snapshot(pr["id"])
            if result.get("busy"):
                return
            with self.ci.lock:
                worker = self.ci.workers.get(key)
            if worker:
                worker.join(140)
            cached = self.cached_checks(account, pr)
            if cached:
                self.enqueue(account, pr, cached)
            with self.db() as db:
                db.execute("INSERT OR REPLACE INTO scans VALUES (?,?)", (key, time.time()))

    def save(self, key, state, data, retry=0):
        with self.db() as db:
            db.execute(
                "UPDATE jobs SET state=?,retry=?,updated=?,data=? WHERE key=?",
                (state, retry, time.time(), json.dumps(data), key),
            )

    def fetch(self, key, data):
        data["attempts"] += 1
        self.save(key, "downloading", data)
        try:
            job = read_job(data)
            expected_check = (
                f"https://api.github.com/repos/{data['repo']}/check-runs/{data['database_id']}"
            )
            if (
                job.get("id") != data["job_id"]
                or job.get("run_id") != data["run_id"]
                or job.get("head_sha") not in {data["sha"], data["head_sha"]}
                or (job.get("check_run_url") or "").lower() != expected_check.lower()
            ):
                raise ValueError("Job identity or commit changed; log was not downloaded")
            if job.get("status") != "completed" or job.get("conclusion") not in FAILED:
                self.save(
                    key, "obsolete", {**data, "error": "This job is no longer a completed failure"}
                )
                return
            raw, truncated = download_log(data)
            value = {
                **summarize_log(raw),
                "truncated": truncated,
                "bytes": len(raw),
                "failed_steps": [
                    s["name"] for s in job.get("steps", []) if s.get("conclusion") in FAILED
                ],
            }
            packed = gzip.compress(raw, mtime=0)
            temp = self.root / f"{key}.tmp"
            fd = os.open(temp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(packed)
            temp.replace(self.root / f"{key}.gz")
            data.update(value=value, error=None, downloaded_at=time.time(), size=len(packed))
            self.save(key, "ready", data)
            self.prune()
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
            data["error"] = str(exc)
            # Three attempts total; absent/expired logs cannot cause an endless loop.
            state = "unavailable" if data["attempts"] >= 3 else "error"
            self.save(key, state, data, time.time() + 3600)

    def prune(self):
        now = time.time()
        with self.db() as db:
            rows = db.execute(
                "SELECT key,state,updated,data FROM jobs ORDER BY updated DESC"
            ).fetchall()
            size = count = 0
            for key, state, updated, raw in rows:
                data = json.loads(raw)
                if state == "ready":
                    count += 1
                    size += data.get("size", 0)
                expired = updated < now - RETENTION
                evicted = state == "ready" and (count > MAX_CACHED_JOBS or size > MAX_CACHE_BYTES)
                if expired or evicted:
                    (self.root / f"{key}.gz").unlink(missing_ok=True)
                    if expired:
                        db.execute("DELETE FROM jobs WHERE key=?", (key,))
                    else:
                        # Keep a small tombstone so cache pressure cannot trigger a download loop.
                        data.pop("value", None)
                        db.execute(
                            "UPDATE jobs SET state='evicted',data=? WHERE key=?",
                            (json.dumps(data), key),
                        )
            db.execute("DELETE FROM scans WHERE checked < ?", (now - RETENTION,))
        for path in self.root.glob("*.tmp"):
            if path.stat().st_mtime < now - 600:
                path.unlink(missing_ok=True)

    def snapshot(self, pr_id, check_id):
        overview = self.overview.snapshot()
        account = overview.get("login") or ""
        pr = next((p for p in overview["prs"] if p["id"] == pr_id), None)
        if not pr or not pr.get("head_sha"):
            raise ValueError("Unknown PR or missing head commit")
        key = self.ci.key(account, pr)
        with self.ci.lock:
            parent = self.ci.entries.get(key, {}).get("value")
        check = next((c for c in (parent or {}).get("checks", []) if c["id"] == check_id), None)
        if not check:
            raise ValueError("Open the PR's current CI checks before reading a job log")
        target = job_target(check)
        if not target:
            return {
                "state": "unsupported",
                "message": "Automatic logs are available for failed GitHub Actions jobs. Open this check's provider for its logs.",
                "refreshing": False,
            }
        job_key = self.job_key(account, target)
        with self.db() as db:
            row = db.execute("SELECT state,data FROM jobs WHERE key=?", (job_key,)).fetchone()
        if not row:
            return {
                "state": "queued",
                "message": "Waiting for the background worker",
                "error": self.error,
                "refreshing": not self.error and self.budget("download", DOWNLOADS_PER_HOUR),
            }
        state, raw = row
        data = json.loads(raw)
        messages = {
            "queued": "Queued for background download",
            "downloading": "Downloading failed-job log",
            "ready": "Cached job log",
            "error": "Download failed; retrying within the hourly budget",
            "unavailable": "Job log unavailable after three attempts",
            "obsolete": "This job is no longer a completed failure",
            "evicted": "Cached log removed to keep disk usage within the limit. Open the job on GitHub.",
        }
        if state == "queued" and not self.budget("download", DOWNLOADS_PER_HOUR):
            messages[state] = "Hourly download budget reached; queued for the next available slot"
        return {
            "state": state,
            "message": messages[state],
            "error": data.get("error"),
            "value": data.get("value"),
            "downloaded_at": data.get("downloaded_at"),
            "refreshing": state == "downloading"
            or (state == "queued" and self.budget("download", DOWNLOADS_PER_HOUR)),
            "key": job_key,
        }

    def cached_log(self, pr_id, check_id):
        result = self.snapshot(pr_id, check_id)
        if result["state"] != "ready":
            raise ValueError("The job log has not been cached yet")
        return result, gzip.decompress((self.root / f"{result['key']}.gz").read_bytes())

    def raw_log(self, pr_id, check_id):
        return self.cached_log(pr_id, check_id)[1]

    def log_page(self, pr_id, check_id, page=1):
        if type(page) is not int or page < 1:
            raise ValueError("Supply a positive log page number")
        result, raw = self.cached_log(pr_id, check_id)
        text = ANSI.sub("", raw.decode("utf-8", errors="replace"))
        offsets = [0]
        while offsets[-1] < len(text):
            start = offsets[-1]
            end = min(start + PAGE_CHARACTERS, len(text))
            if end < len(text):
                # Prefer complete lines, but keep exceptionally long lines pageable too.
                newline = text.rfind("\n", start, end)
                if newline >= start:
                    end = newline + 1
            offsets.append(end)
        if len(offsets) == 1:
            offsets.append(0)
        pages = len(offsets) - 1
        if page > pages:
            raise ValueError(f"Log page must be between 1 and {pages}")
        start, end = offsets[page - 1 : page + 1]
        chunk = text[start:end]
        line_start = text.count("\n", 0, start) + 1 if text else 0
        return {
            "page": page,
            "pages": pages,
            "text": chunk,
            "line_start": line_start,
            "line_end": line_start + chunk.count("\n") - int(chunk.endswith("\n")),
            "truncated": result["value"]["truncated"],
        }
