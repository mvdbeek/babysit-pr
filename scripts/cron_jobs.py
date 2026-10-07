"""Recurring shell commands the dashboard runs on a schedule, with their run history.

A job is a shell command, a working directory and a schedule: a five-field cron
expression in the dashboard host's local time, or a fixed interval. The scheduler
runs inside the dashboard process; nothing runs while the dashboard is stopped.
A run that was due while it was stopped starts once when it returns, up to a day
late. Each run's output is kept in a bounded log file next to its history.
"""

import contextlib
import fcntl
import json
import math
import os
import secrets
import selectors
import signal
import sqlite3
import subprocess
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import owned_process

MAX_JOBS = 100
RUN_HISTORY = 50  # Finished runs kept per job, with their logs.
RUNS_SHOWN = 20  # Runs per job included in a snapshot.
LOG_LIMIT = 512 * 1024  # Output kept per run; the rest is drained and dropped.
TAIL = 2000  # Characters of the latest output included in a snapshot.
DEFAULT_TIMEOUT = 3600
MAX_TIMEOUT = 86400
MIN_INTERVAL = 60
MAX_INTERVAL = 31 * 86400
MISSED_AFTER = 86400  # A run due longer ago than this is recorded as missed.
POLL_SECONDS = 30
STOP_GRACE = 5  # Seconds between SIGTERM and SIGKILL when a run is stopped.
QUIET_AFTER_EXIT = 2  # Seconds to wait for output a background child still writes.

ALIASES = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}
MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
DAYS = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"]
# (lowest, highest, names starting at lowest)
FIELDS = [(0, 59, None), (0, 23, None), (1, 31, None), (1, 12, MONTHS), (0, 7, DAYS)]
FIELD_NAMES = ["minute", "hour", "day of month", "month", "day of week"]


class Cron:
    """A five-field cron expression with Vixie cron's day-of-month/day-of-week rule."""

    def __init__(self, text: str):
        expression = ALIASES.get(text.strip().lower(), text)
        parts = expression.split()
        if len(parts) != 5:
            raise ValueError(
                "A cron expression has five fields: minute hour day-of-month month day-of-week"
            )
        sets = [
            self.field(part, *spec, name)
            for part, spec, name in zip(parts, FIELDS, FIELD_NAMES, strict=True)
        ]
        self.minutes, self.hours, self.days, self.months, weekdays = sets
        self.weekdays = frozenset(day % 7 for day in weekdays)
        # Both restricted: either may match. One starred: the other alone decides.
        self.days_star = parts[2].startswith("*")
        self.weekdays_star = parts[4].startswith("*")
        self.next_after(time.time())  # Rejects expressions that never match.

    @staticmethod
    def field(text, low, high, names, name):
        def value(token):
            token = token.lower()
            if names and token in names:
                return names.index(token) + low
            if not token.isdigit():
                raise ValueError(f"Unrecognized {name} value: {token!r}")
            return int(token)

        values: set[int] = set()
        for part in text.split(","):
            span, slash, step_text = part.partition("/")
            if slash and not step_text.isdigit() or slash and int(step_text) == 0:
                raise ValueError(f"Unrecognized {name} step: {part!r}")
            step = int(step_text) if slash else 1
            if span == "*":
                first, last = low, high
            elif "-" in span:
                start, _, end = span.partition("-")
                first, last = value(start), value(end)
            else:
                first = value(span)
                last = high if slash else first
            if not low <= first <= last <= high:
                raise ValueError(f"The {name} field allows {low}-{high}: {part!r}")
            values.update(range(first, last + 1, step))
        return frozenset(values)

    def day_matches(self, moment):
        day = moment.day in self.days
        weekday = (moment.weekday() + 1) % 7 in self.weekdays
        if self.days_star or self.weekdays_star:
            return day and weekday
        return day or weekday

    def matches(self, moment):
        return (
            moment.month in self.months
            and self.day_matches(moment)
            and moment.hour in self.hours
            and moment.minute in self.minutes
        )

    def next_after(self, timestamp):
        """The first matching minute strictly after ``timestamp``, in local time."""
        start = datetime.fromtimestamp(timestamp)
        moment = start.replace(second=0, microsecond=0) + timedelta(minutes=1)
        while moment.year <= start.year + 8:
            if moment.month not in self.months:
                year, month = divmod(moment.month, 12)
                moment = moment.replace(year=moment.year + year, month=month + 1, day=1)
                moment = moment.replace(hour=0, minute=0)
            elif not self.day_matches(moment):
                moment = (moment + timedelta(days=1)).replace(hour=0, minute=0)
            elif moment.hour not in self.hours:
                moment = (moment + timedelta(hours=1)).replace(minute=0)
            elif moment.minute not in self.minutes:
                moment += timedelta(minutes=1)
            elif moment.timestamp() <= timestamp:
                # Wall-clock stepping reads a repeated time as its first, past occurrence.
                moment += timedelta(minutes=1)
            else:
                break
        else:
            raise ValueError("This cron expression never matches a date")
        found = moment.timestamp()
        if len(self.hours) == 24:
            # Like Vixie cron, schedules for every hour also run in the hour a DST change
            # repeats, which wall-clock stepping skips; real minutes include it.
            minute = (int(timestamp) // 60 + 1) * 60
            while minute < min(found, timestamp + 7200):
                if self.matches(datetime.fromtimestamp(minute)):
                    return float(minute)
                minute += 60
        return found


def next_run(job, after):
    """When ``job`` is next due after ``after``; intervals count from the job's anchor."""
    schedule = job["schedule"]
    if "every" in schedule:
        every, anchor = schedule["every"], job["anchor"]
        return anchor + (math.floor((after - anchor) / every) + 1) * every
    return Cron(schedule["cron"]).next_after(after)


def upcoming(job, now, count=3):
    times, moment = [], now
    for _ in range(count):
        moment = next_run(job, moment)
        times.append(moment)
    return times


def default_shell():
    shell = os.environ.get("SHELL") or ("/bin/zsh" if Path("/bin/zsh").exists() else "/bin/sh")
    # A login shell gives commands the user's PATH even under launchd.
    return [shell, "-lc"]


def validate(request):
    """A job definition from an untrusted request; raises ValueError with a readable reason."""
    name = request.get("name")
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80:
        raise ValueError("Give the job a name of at most 80 characters")
    command = request.get("command")
    if not isinstance(command, str) or not command.strip() or len(command) > 20000:
        raise ValueError("Give the job a command of at most 20000 characters")
    if "\0" in command:
        raise ValueError("The command cannot contain NUL characters")
    cwd = request.get("cwd") or ""
    if not isinstance(cwd, str) or len(cwd) > 4096:
        raise ValueError("Expected a working directory path")
    cwd = cwd.strip()
    if cwd:
        path = Path(cwd).expanduser()
        if not path.is_absolute():
            raise ValueError("Use an absolute working directory, or ~ for your home")
        if not path.is_dir():
            raise ValueError(f"The working directory does not exist: {cwd}")
    schedule = request.get("schedule")
    if not isinstance(schedule, dict) or len(schedule) != 1:
        raise ValueError("Choose an interval or a cron expression")
    if "every" in schedule:
        every = schedule["every"]
        if (
            not isinstance(every, int)
            or isinstance(every, bool)
            or not MIN_INTERVAL <= every <= MAX_INTERVAL
            or every % 60
        ):
            raise ValueError("Repeat every 1 minute to 31 days, in whole minutes")
        schedule = {"every": every}
    elif "cron" in schedule:
        if not isinstance(schedule["cron"], str) or len(schedule["cron"]) > 200:
            raise ValueError("Expected a cron expression")
        expression = " ".join(schedule["cron"].split())
        Cron(expression)
        schedule = {"cron": expression}
    else:
        raise ValueError("Choose an interval or a cron expression")
    timeout = request.get("timeout", DEFAULT_TIMEOUT)
    if (
        not isinstance(timeout, int)
        or isinstance(timeout, bool)
        or not 60 <= timeout <= MAX_TIMEOUT
    ):
        raise ValueError("The time limit must be between 1 minute and 24 hours")
    enabled = request.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError("Expected enabled to be true or false")
    return {
        "name": name.strip(),
        "command": command,
        "cwd": cwd,
        "schedule": schedule,
        "timeout": timeout,
        "enabled": enabled,
    }


class Run:
    """A run in progress: its stop request and the thread that owns its process."""

    def __init__(self, record):
        self.record = record
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None


class CronJobs:
    def __init__(self, home: Path, shell: list[str] | None = None, clock=time.time):
        self.home = home
        self.shell = shell or default_shell()
        self.clock = clock
        self.path = home / "cron.sqlite"
        self.logs = home / "cron-logs"
        self.logs.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.running: dict[str, Run] = {}
        self.wake = threading.Event()
        self.stopping = threading.Event()
        self.scheduler: threading.Thread | None = None
        # One process schedules and runs these jobs; another dashboard over the same
        # state only shows them, and must not mistake the owner's runs for leftovers.
        self.owner = (home / "cron.lock").open("a")
        try:
            fcntl.flock(self.owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.active = True
        except BlockingIOError:
            self.active = False
        with self.db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, data TEXT)")
            db.execute(
                "CREATE TABLE IF NOT EXISTS runs "
                "(id INTEGER PRIMARY KEY AUTOINCREMENT, job TEXT, data TEXT)"
            )
            # A run the previous process left behind lost its process when it stopped.
            leftovers = db.execute(
                "SELECT id,data FROM runs WHERE json_extract(data,'$.status')='running'"
            ).fetchall()
            for key, data in leftovers if self.active else []:
                run = json.loads(data)
                run.update(
                    status="interrupted",
                    finished_at=run.get("updated_at") or run["started_at"],
                    message="The dashboard stopped during this run",
                )
                db.execute("UPDATE runs SET data=? WHERE id=?", (json.dumps(run), key))
        self.path.chmod(0o600)

    @contextlib.contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    # -- reading --

    def jobs(self, db):
        return [json.loads(data) for (data,) in db.execute("SELECT data FROM jobs")]

    def job(self, db, key):
        if not isinstance(key, str):
            raise ValueError("Supply a job ID")
        row = db.execute("SELECT data FROM jobs WHERE id=?", (key,)).fetchone()
        if not row:
            raise ValueError("This job no longer exists")
        return json.loads(row[0])

    def runs(self, db, key, limit):
        return [
            json.loads(data)
            for (data,) in db.execute(
                "SELECT data FROM runs WHERE job=? ORDER BY id DESC LIMIT ?", (key, limit)
            )
        ]

    def tail(self, run):
        try:
            with self.log_path(run["id"]).open("rb") as log:
                log.seek(max(0, log.seek(0, os.SEEK_END) - TAIL * 4))
                return log.read().decode("utf-8", errors="replace")[-TAIL:]
        except OSError:
            return ""

    def snapshot(self):
        now = self.clock()
        with self.lock, self.db() as db:
            jobs = self.jobs(db)
            for job in jobs:
                job["runs"] = self.runs(db, job["id"], RUNS_SHOWN)
                for run in job["runs"][:1]:
                    run["tail"] = self.tail(run)
                job["running"] = job["id"] in self.running
                try:
                    job["upcoming"] = upcoming(job, now) if job["enabled"] else []
                except ValueError:
                    job["upcoming"] = []
        jobs.sort(key=lambda job: job["name"].lower())
        return {
            "enabled": True,
            "active": self.active,
            "now": now,
            "shell": " ".join(self.shell),
            "timezone": time.strftime("%Z"),
            "limit": MAX_JOBS,
            "log_limit": LOG_LIMIT,
            "jobs": jobs,
        }

    def log_path(self, run_id):
        return self.logs / f"{int(run_id)}.log"

    def log(self, run_id):
        if not isinstance(run_id, str) or not run_id.isdigit():
            raise ValueError("Supply a run ID")
        with self.db() as db:
            row = db.execute("SELECT data FROM runs WHERE id=?", (int(run_id),)).fetchone()
        if not row:
            raise ValueError("This run is no longer kept")
        run = json.loads(row[0])
        try:
            data = self.log_path(run["id"]).read_bytes()
        except FileNotFoundError:
            data = b""
        return {"run": run, "text": data.decode("utf-8", errors="replace")}

    # -- changing --

    def action(self, request):
        kind = request.get("action")
        if kind == "save":
            return {"job": self.save(request)}
        if kind == "delete":
            return self.delete(request.get("id"))
        if kind == "run":
            return {"run": self.run_now(request.get("id"))}
        if kind == "stop":
            return self.stop(request.get("id"))
        if kind == "enable":
            if not isinstance(request.get("enabled"), bool):
                raise ValueError("Expected enabled to be true or false")
            return {"job": self.set_enabled(request.get("id"), request["enabled"])}
        raise ValueError("Unknown job action")

    def save(self, request):
        definition = validate(request)
        now = self.clock()
        with self.lock, self.db() as db:
            if request.get("id") is None:
                if db.execute("SELECT count(*) FROM jobs").fetchone()[0] >= MAX_JOBS:
                    raise ValueError(f"At most {MAX_JOBS} jobs can be defined; delete one first")
                job = {"id": secrets.token_hex(6), "created_at": now}
            else:
                job = self.job(db, request["id"])
            rescheduled = job.get("schedule") != definition["schedule"] or (
                definition["enabled"] and not job.get("enabled")
            )
            job.update(definition, updated_at=now)
            if rescheduled or job.get("next_run") is None:
                job["anchor"] = now
                job["next_run"] = next_run(job, now)
            db.execute(
                "INSERT INTO jobs VALUES (?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                (job["id"], json.dumps(job)),
            )
        self.wake.set()
        return job

    def set_enabled(self, key, enabled):
        now = self.clock()
        with self.lock, self.db() as db:
            job = self.job(db, key)
            if enabled and not job["enabled"]:
                # Resuming starts from now instead of replaying runs missed while paused.
                job.update(anchor=now)
                job["next_run"] = next_run(job, now)
            job.update(enabled=enabled, updated_at=now)
            db.execute("UPDATE jobs SET data=? WHERE id=?", (json.dumps(job), key))
        self.wake.set()
        return job

    def delete(self, key):
        with self.lock, self.db() as db:
            self.job(db, key)
            if key in self.running:
                raise ValueError("Stop the running job before deleting it")
            ids = [row[0] for row in db.execute("SELECT id FROM runs WHERE job=?", (key,))]
            db.execute("DELETE FROM runs WHERE job=?", (key,))
            db.execute("DELETE FROM jobs WHERE id=?", (key,))
        for run_id in ids:
            self.log_path(run_id).unlink(missing_ok=True)
        return {"deleted": key}

    def run_now(self, key):
        # Held through launch, so a delete or scheduled start cannot slip in between.
        with self.lock:
            with self.db() as db:
                job = self.job(db, key)
            if key in self.running:
                raise ValueError("This job is already running")
            return self.launch(job, "manual", self.clock())

    def stop(self, key):
        with self.lock:
            run = self.running.get(key)
            if not run:
                raise ValueError("This job is not running")
            run.stop.set()
        return {"stopping": run.record["id"]}

    # -- running --

    def record(self, db, job, **fields):
        run = {"job": job["id"], "updated_at": self.clock(), **fields}
        cursor = db.execute(
            "INSERT INTO runs (job, data) VALUES (?, '{}')",
            (job["id"],),
        )
        run["id"] = str(cursor.lastrowid)
        db.execute("UPDATE runs SET data=? WHERE id=?", (json.dumps(run), cursor.lastrowid))
        return run

    def update_run(self, run, **changes):
        run.update(changes, updated_at=self.clock())
        with self.db() as db:
            db.execute("UPDATE runs SET data=? WHERE id=?", (json.dumps(run), int(run["id"])))

    def prune(self, key):
        with self.db() as db:
            ids = [
                row[0]
                for row in db.execute(
                    "SELECT id FROM runs WHERE job=? AND json_extract(data,'$.status')!='running' "
                    "ORDER BY id DESC LIMIT -1 OFFSET ?",
                    (key, RUN_HISTORY),
                )
            ]
            db.executemany("DELETE FROM runs WHERE id=?", [(i,) for i in ids])
        for run_id in ids:
            self.log_path(run_id).unlink(missing_ok=True)

    def launch(self, job, trigger, due_at):
        """Start ``job`` in its own thread; a job never runs twice at once."""
        now = self.clock()
        with self.lock:
            if not self.active:
                raise ValueError("Another dashboard process runs these jobs")
            if self.stopping.is_set():
                raise ValueError("The dashboard is stopping")
            with self.db() as db:
                if job["id"] in self.running:
                    return self.skip(db, job, trigger, due_at, now)
                run = self.record(
                    db,
                    job,
                    trigger=trigger,
                    due_at=due_at,
                    started_at=now,
                    finished_at=None,
                    status="running",
                    exit_code=None,
                    message="Running",
                    command=job["command"],
                    cwd=job["cwd"],
                    output_bytes=0,
                    truncated=False,
                )
            handle = Run(run)
            self.running[job["id"]] = handle
            handle.thread = threading.Thread(
                target=self.execute, args=(job, handle), daemon=True, name=f"cron-{job['id']}"
            )
            handle.thread.start()
        return run

    def skip(self, db, job, trigger, due_at, now):
        """Count a start skipped behind a long run, adding to the latest skip if it is one.

        A frequent job behind a slow run would otherwise fill its history with skips
        and prune the real runs and their output.
        """
        [last] = self.runs(db, job["id"], 1) or [None]
        if last and last["status"] == "skipped":
            count = last.get("count", 1) + 1
            last.update(
                count=count,
                due_at=due_at,
                finished_at=now,
                updated_at=now,
                message=f"Skipped {count} times while the previous run was still going",
            )
            db.execute("UPDATE runs SET data=? WHERE id=?", (json.dumps(last), int(last["id"])))
            return last
        return self.record(
            db,
            job,
            trigger=trigger,
            due_at=due_at,
            started_at=now,
            finished_at=now,
            status="skipped",
            count=1,
            message="The previous run was still going",
        )

    def execute(self, job, handle):
        run = handle.record
        try:
            result = self.perform(job, handle)
        except Exception as exc:  # The run must always finish, whatever went wrong.
            result = {"status": "error", "message": f"Could not run: {exc}"}
        result.setdefault("finished_at", self.clock())
        # Together under the lock, so no snapshot shows a finished run of a running job.
        with self.lock:
            try:
                self.update_run(run, **result)
            finally:
                self.running.pop(job["id"], None)
        try:
            self.prune(job["id"])
        except (OSError, sqlite3.Error):
            pass

    def perform(self, job, handle):
        run = handle.record
        cwd = Path(job["cwd"]).expanduser() if job["cwd"] else Path.home()
        env = {**os.environ, "BABYSIT_CRON_JOB": job["id"], "BABYSIT_CRON_RUN": run["id"]}
        started = time.monotonic()
        written = 0
        timed_out = False
        stopped = interrupted = False
        fd = os.open(self.log_path(run["id"]), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with open(fd, "wb", buffering=0) as log, contextlib.ExitStack() as stack:
            try:
                proc = stack.enter_context(
                    owned_process.command(
                        [*self.shell, job["command"]],
                        cwd=cwd,
                        env=env,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                    )
                )
            except OSError as exc:
                return {
                    "status": "error",
                    "finished_at": self.clock(),
                    "message": f"Could not start: {exc}",
                }
            assert proc.stdout is not None
            exited_at = None
            with selectors.DefaultSelector() as selector:
                selector.register(proc.stdout, selectors.EVENT_READ)
                while True:
                    if (handle.stop.is_set() or self.stopping.is_set()) and proc.poll() is None:
                        # A command that already finished keeps its own result.
                        stopped = handle.stop.is_set()
                        interrupted = not stopped
                        owned_process.stop_group(proc, grace=STOP_GRACE)
                        break
                    if time.monotonic() - started > job["timeout"]:
                        timed_out = True
                        owned_process.stop_group(proc, grace=STOP_GRACE)
                        break
                    if selector.select(0.25):
                        chunk = os.read(proc.stdout.fileno(), 65536)
                        if not chunk:
                            break
                        if written < LOG_LIMIT:
                            log.write(chunk[: LOG_LIMIT - written])
                        written += len(chunk)
                    elif proc.poll() is not None:
                        # A background child can hold the pipe open after the shell exits.
                        exited_at = exited_at or time.monotonic()
                        if time.monotonic() - exited_at > QUIET_AFTER_EXIT:
                            break
            code = proc.wait()
        finished = {
            "finished_at": self.clock(),
            "exit_code": code,
            "output_bytes": written,
            "truncated": written > LOG_LIMIT,
        }
        if interrupted:
            return {**finished, "status": "interrupted", "message": "The dashboard stopped"}
        if stopped:
            return {**finished, "status": "stopped", "message": "Stopped from the dashboard"}
        if timed_out:
            minutes = job["timeout"] // 60
            return {
                **finished,
                "status": "timed_out",
                "message": f"Stopped after the {minutes} min time limit",
            }
        if code == 0:
            return {**finished, "status": "succeeded", "message": "Exited with status 0"}
        if code < 0:
            name = signal.Signals(-code).name if -code in signal.valid_signals() else -code
            return {**finished, "status": "failed", "message": f"Killed by signal {name}"}
        return {**finished, "status": "failed", "message": f"Exited with status {code}"}

    # -- scheduling --

    def tick(self):
        """Start every enabled job that is due and move it to its next time."""
        now = self.clock()
        with self.lock:
            due = []
            with self.db() as db:
                for job in self.jobs(db):
                    if not job["enabled"] or job.get("next_run") is None or job["next_run"] > now:
                        continue
                    due.append((dict(job), job["next_run"]))
                    try:
                        job["next_run"] = next_run(job, now)
                    except ValueError:
                        job["next_run"] = None
                    db.execute("UPDATE jobs SET data=? WHERE id=?", (json.dumps(job), job["id"]))
            for job, due_at in due:
                if now - due_at <= MISSED_AFTER:
                    self.launch(job, "schedule", due_at)
                    continue
                with self.db() as db:
                    self.record(
                        db,
                        job,
                        trigger="schedule",
                        due_at=due_at,
                        started_at=now,
                        finished_at=now,
                        status="missed",
                        message="The dashboard was stopped for more than a day when this was due",
                    )
                self.prune(job["id"])

    def next_due(self):
        with self.db() as db:
            times = [
                job["next_run"]
                for job in self.jobs(db)
                if job["enabled"] and job.get("next_run") is not None
            ]
        return min(times, default=None)

    def start(self):
        if self.scheduler is None and self.active:
            self.scheduler = threading.Thread(target=self.loop, daemon=True, name="cron")
            self.scheduler.start()

    def close(self):
        """Stop the scheduler and every run; runs end as interrupted."""
        self.stopping.set()
        self.wake.set()
        if self.scheduler:
            self.scheduler.join(10)
        with self.lock:
            threads = [run.thread for run in self.running.values() if run.thread]
        for thread in threads:
            thread.join(STOP_GRACE + 5)
        self.owner.close()

    def loop(self):
        while not self.stopping.is_set():
            self.wake.clear()
            try:
                self.tick()
            except Exception:
                pass  # The scheduler must outlive any single failure; the next pass retries.
            try:
                due = self.next_due()
            except Exception:
                due = None
            delay = POLL_SECONDS if due is None else due - self.clock()
            self.wake.wait(max(0.5, min(POLL_SECONDS, delay)))
