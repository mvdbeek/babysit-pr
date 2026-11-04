#!/usr/bin/env python3
"""Loopback dashboard with watch cancellation for the shared babysit-pr watcher."""

import argparse
import errno
import gzip
import hashlib
import ipaddress
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import agent_messages
import attachments
import attention
import browser_extension
import dashboard_auth
import dashboard_settings
import llm_usage
import machines
import service_control
import voice_assistant
import work_tags
import worker_api
import workspace_agents
import workspace_move
import workspace_viewer
from dashboard_push import check_result
from issue_overview import Overview as IssueOverview
from pr_ci import CiDetails
from pr_ci_logs import BackgroundLogs
from pr_overview import Overview
from pr_workspaces import SUBTASK_WAIT, Workspaces, collie_url

ASSETS = Path(__file__).resolve().parent.parent / "assets" / "dashboard"
LOGS = {"agent": "agent.log", "guardian": "guardian.log", "result": "result.json"}
# Responses worth compressing: the polled JSON is large and repetitive, images are not.
COMPRESSIBLE = {
    "application/json",
    "application/manifest+json",
    "image/svg+xml",
    "text/css",
    "text/html",
    "text/javascript",
    "text/plain",
}
COMPRESS_MIN = 1024
COMPRESS_LEVEL = 5
# Bodies go out in chunks, so the socket timeout bounds a stall rather than a slow link.
WRITE_CHUNK = 256 * 1024
# A refused request's body up to this size is read and dropped to keep its connection.
DRAIN_MAX = 256 * 1024
# A queue write within this long of a read may share its timestamp: never cached.
RACY_NS = 1_000_000_000
_jobs_lock = threading.Lock()
_jobs_cache: dict[Path, tuple] = {}
_assets_lock = threading.Lock()
_assets_cache: dict[str, tuple] = {}


def read_json(path):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return None


def queue_signature(path):
    """What changes when the watcher writes its queue: the database file and its WAL."""
    signature: list[tuple[int, int, int] | None] = []
    for name in (path, path.with_name(path.name + "-wal")):
        try:
            stat = name.stat()
        except FileNotFoundError:
            signature.append(None)
        else:
            signature.append((stat.st_ino, stat.st_size, stat.st_mtime_ns))
    return tuple(signature)


def unsupported_machine(home, machine_id):
    machines.resolve(home, machine_id)  # An unknown machine is refused as such.
    return ValueError(f"Workspaces on machine {machine_id} cannot be opened from this dashboard")


def local_workspace(home, value):
    """A workspace ID from a request: ``w1`` as before, ``<machine>/w1`` refused."""
    if not isinstance(value, str) or "/" not in value:
        return value
    try:
        machine_id, _ = machines.split(value)
    except ValueError:
        return value  # Not a qualified ID either: refused as an invalid workspace, as before.
    raise unsupported_machine(home, machine_id)


# Composer actions an agent on a worker takes as well, named by its qualified workspace ID.
WORKER_ACTIONS = {
    "workspace-message",
    "workspace-interrupt",
    "workspace-answer",
    "workspace-choose",
}


# The browser actions post() accepts, each at /api/<action>.
ACTIONS = frozenset(
    {
        "cancel",
        "feedback",
        "feedback-addressed",
        "workspace-action",
        "workspace-batch",
        "workspace-new",
        "workspace-subtask",
        "schedule-cancel",
        "workspace-cleanup",
        "workspace-open",
        "workspace-message",
        "workspace-answer",
        "workspace-choose",
        "workspace-docker",
        "workspace-interrupt",
        "workspace-quit",
        "workspace-move",
        "workspace-prompt-forget",
        "attachment-upload",
        "sentry-action",
        "cron-action",
        "push-subscribe",
        "push-unsubscribe",
        "push-read",
        "push-seen",
        "notification-silence",
        "notification-focus",
        "attention-triage",
        "tags",
        "extension-pair",
        "effort-default",
        "settings",
        "codex-update",
        "worker-update",
        "service-restart",
        "voice-turn",
        "voice-log",
    }
)
# Every API route this dashboard answers: the report names only these, so anonymous
# requests cannot grow it.
API_ROUTES = frozenset(
    {
        "/api/attention",
        "/api/auth-report",
        "/api/codex-update",
        "/api/cron",
        "/api/cron-log",
        "/api/issues",
        "/api/llm-usage",
        "/api/log",
        "/api/machines",
        "/api/notification-preferences",
        "/api/placement",
        "/api/pr-ci",
        "/api/pr-ci-log",
        "/api/pr-reviews",
        "/api/prs",
        "/api/push-config",
        "/api/scheduled-tasks",
        "/api/sentry",
        "/api/service",
        "/api/session",
        "/api/settings",
        "/api/status",
        "/api/upstream-tests",
        "/api/voice-turn",
        "/api/watch",
        "/api/workspace-agents",
        "/api/workspace-diff",
        "/api/workspace-overview",
        "/api/workspace-prompts",
        "/api/workspace-repos",
        "/api/workspace-transcript",
        "/api/workspaces",
    }
    | {f"/api/{action}" for action in ACTIONS}
    | {f"/api/worker/{action}" for action in worker_api.ACTIONS}
    | {"/api/extension"}
)


def viewed_workspace(home, value):
    """A workspace ID from a view request as (machine, ID): ``w1`` on this machine, or
    ``<machine>/w1`` on a registered worker, whose checkout its views read there."""
    if not isinstance(value, str) or "/" not in value:
        return None, value
    try:
        machine_id, workspace_id = machines.split(value)
    except ValueError:
        return None, value  # Not a qualified ID either: refused as an invalid workspace.
    machine = machines.resolve(home, machine_id)
    if machine is not None and not machine.enabled:
        raise ValueError(f"{machine.label} is disabled")
    return machine, workspace_id


def local_label(home):
    """What the viewer calls this machine beside workers; None while there are none."""
    try:
        workers = machines.load(home)
    except (OSError, ValueError):
        workers = {}
    if not workers:
        return None
    return "this Mac" if sys.platform == "darwin" else "this machine"


def watch_checkout(home, watch_id):
    """A watch's registered checkout and the session its repairs resume."""
    job = next((j for j in read_jobs(home) if j.get("id") == watch_id), None)
    if job is None:
        raise ValueError("Unknown watch; refresh")
    if job.get("machine") is not None:
        raise unsupported_machine(home, job["machine"])
    return job["cwd"], job.get("session_id")


def read_jobs(home):
    """Every watch in the queue, parsed once per queue change and shared by all callers.

    The result is cached: callers must copy before mutating any part of it.
    """
    path = home / "queue.sqlite"
    before = queue_signature(path)
    if before[0] is None:
        return []
    with _jobs_lock:
        cached = _jobs_cache.get(path)
        if cached and cached[0] == before:
            return cached[1]
    # Never create a database or take ownership of monitoring just to display it.
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)
    try:
        jobs = [json.loads(row[0]) for row in db.execute("SELECT data FROM jobs")]
    finally:
        db.close()
    newest = max(part[2] for part in before if part)
    if queue_signature(path) == before and time.time_ns() - newest > RACY_NS:
        with _jobs_lock:
            _jobs_cache[path] = (before, jobs)
    return jobs


def compressible(content_type):
    return content_type.split(";")[0].strip().lower() in COMPRESSIBLE


def static_asset(filename, content_type):
    """An asset's bytes, gzip encoding and ETag, recomputed only when the file changes."""
    path = ASSETS / filename
    stat = path.stat()
    signature = (stat.st_ino, stat.st_size, stat.st_mtime_ns)
    with _assets_lock:
        cached = _assets_cache.get(filename)
    if cached and cached[0] == signature:
        return cached[1:]
    body = path.read_bytes()
    packed = gzip.compress(body, compresslevel=9, mtime=0) if compressible(content_type) else None
    # Weak: the gzip and identity encodings of one file share it.
    etag = f'W/"{hashlib.sha256(body).hexdigest()[:32]}"'
    with _assets_lock:
        _assets_cache[filename] = (signature, body, packed, etag)
    return body, packed, etag


def accepts_gzip(header):
    """Whether Accept-Encoding allows gzip; an explicit gzip entry overrides `*`."""
    qualities = {}
    for part in (header or "").split(","):
        name, _, params = part.partition(";")
        quality = 1.0
        for param in params.split(";"):
            key, _, value = param.partition("=")
            if key.strip().lower() == "q":
                try:
                    quality = float(value)
                except ValueError:
                    quality = 0.0
        qualities[name.strip().lower()] = quality
    gzip_quality = qualities.get("gzip", qualities.get("x-gzip", qualities.get("*", 0.0)))
    return gzip_quality > 0


def etag_matches(header, etag):
    """If-None-Match uses the weak comparison: W/ prefixes are ignored."""
    if not header:
        return False
    if header.strip() == "*":
        return True
    opaque = etag.removeprefix("W/")
    return any(tag.strip().removeprefix("W/") == opaque for tag in header.split(","))


def present_job(job):
    snapshot = job.get("snapshot") or {}
    pr = snapshot.get("pr") or {}
    ci = snapshot.get("ci") or {}
    from pr_supervisor import approved_feedback, feedback_token

    feedback = job.get("pending_reviews") or []
    check_details = snapshot.get("check_details") or []
    return {
        "feedback": feedback,
        "feedback_token": feedback_token(feedback),
        "feedback_approved": len(approved_feedback(job)),
        "notification_actions": job.get("notification_actions", []),
        "cleanup_ready": bool(
            job.get("cleanup_ready")
            or (job.get("status") == "closed" and (pr.get("closed") or pr.get("merged")))
        ),
        "pr_outcome": "merged" if pr.get("merged") else "closed" if pr.get("closed") else None,
        "id": job["id"],
        "url": job.get("url"),
        "repo": job.get("repo"),
        "branch": job.get("branch") or pr.get("head_branch"),
        "kind": "branch" if job.get("branch") else "pr",
        "number": pr.get("number"),
        "title": pr.get("title") if not job.get("branch") else None,
        "draft": pr.get("draft") if not job.get("branch") else None,
        "ci_repo": ci.get("repo") or job.get("ci_repo") or job.get("repo"),
        "sha": pr.get("head_sha"),
        "status": job.get("status", "unknown"),
        "summary": job.get("summary", ""),
        "agent": job.get("agent", "codex"),
        "claude_account": job.get("claude_account"),
        "attempts": job.get("attempts", 0),
        "max_repairs": job.get("max_repairs", 0),
        "pending_reviews": len(job.get("pending_reviews") or []),
        "checks": snapshot.get("checks"),
        "check_details": check_details,
        "checks_result": check_result(check_details),
        "failed_jobs": snapshot.get("failed_jobs") or [],
        "updated_at": job.get("updated_at"),
        "next_poll": job.get("next_poll"),
        "last_poll": (job.get("watcher_state") or {}).get("last_snapshot_at"),
        "started_at": job.get("started_at"),
        "cwd": job.get("cwd"),
        "machine": job.get("machine"),
        "session_id": job.get("session_id"),
        "attempt": job.get("attempt"),
        "poll_errors": job.get("poll_errors", 0),
        "pause_after_run": job.get("pause_after_run", False),
        "stop_after_run": job.get("stop_after_run", False),
    }


def daemon_health(home, now=None):
    """How the watcher daemon is doing, for the header badge and the Services dialog."""
    now = time.time() if now is None else now
    heartbeat = read_json(home / "heartbeat.json") or {}
    daemon = read_json(home / "daemon.json") or {}
    age = max(0, now - heartbeat["time"]) if heartbeat.get("time") else None
    return {
        "health": "healthy"
        if age is not None and age < 15
        else "stale"
        if age is not None
        else "offline",
        "heartbeat_age": age,
        "max_workers": daemon.get("max_workers"),
        "pid": daemon.get("pid"),
        "started_at": daemon.get("started_at"),
    }


def status(home):
    now = time.time()
    result = {
        "time": now,
        "home": str(home),
        "jobs": [],
        "error": None,
        "daemon": {"health": "unknown", "heartbeat_age": None, "max_workers": None},
    }
    try:
        jobs = [present_job(job) for job in read_jobs(home)]
        for job in jobs:
            # Every poll carries every watch: an ended one's CI details are loaded on
            # demand (/api/watch). Its overall result stays, for notifications.
            if job["status"] in agent_messages.ENDED_WATCHES:
                job.update(check_details=[], failed_jobs=[], details_omitted=True)
        result["jobs"] = sorted(jobs, key=lambda job: job.get("updated_at") or 0, reverse=True)
        result["daemon"] = daemon_health(home, now)
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
        result["error"] = f"Cannot read watcher state: {exc}"
    # Only with workers registered does the page poll their loads (/api/machines).
    try:
        workers = len(machines.load(home))
    except (OSError, ValueError):
        workers = 0
    if workers:
        result["workers"] = workers
    return result


def cancel_watch(home, job_id):
    from pr_supervisor import stop_watch

    # Cancellation must not create a new queue when the configured home is wrong.
    db = sqlite3.connect((home / "queue.sqlite").as_uri() + "?mode=rw", uri=True, timeout=2)
    try:
        return present_job(stop_watch(db, job_id))
    finally:
        db.close()


def handle_feedback(home, job_id, token, addressed_key=None):
    from pr_supervisor import approve_feedback, mark_feedback_addressed

    db = sqlite3.connect((home / "queue.sqlite").as_uri() + "?mode=rw", uri=True, timeout=2)
    try:
        job = (
            approve_feedback(db, job_id, token)
            if addressed_key is None
            else mark_feedback_addressed(db, job_id, token, addressed_key)
        )
        return present_job(job)
    finally:
        db.close()


def tail_log(home, kind, job_id=None):
    if kind == "supervisor":
        path = home / "supervisor.log"
    else:
        if kind not in LOGS:
            raise ValueError("Unknown log")
        job = next((job for job in read_jobs(home) if job["id"] == job_id), None)
        if job is None:
            raise ValueError("Unknown watch")
        attempt = job.get("attempt", "")
        if not re.fullmatch(r"[0-9a-f-]{36}", attempt):
            return {"text": "No repair has run for this watch yet.", "truncated": False}
        path = home / "runs" / attempt / LOGS[kind]
    # Only known watcher log files may be served, never arbitrary paths/symlinks.
    if not path.resolve().is_relative_to(home.resolve()):
        raise ValueError("Log is outside the watcher state directory")
    try:
        with path.open("rb") as stream:
            size = stream.seek(0, 2)
            start = max(0, size - 64 * 1024)
            stream.seek(start)
            data = stream.read(64 * 1024)
        return {"text": data.decode("utf-8", errors="replace"), "truncated": start > 0}
    except FileNotFoundError:
        return {"text": "This log is not available yet.", "truncated": False}


def attach_handling(value, workspaces):
    """Mark Sentry groups that Handle already started work on; best effort."""
    if not workspaces or not value.get("groups"):
        return
    try:
        state = workspaces.sentry_state()
    except Exception:
        return
    for group in value["groups"]:
        group["handling"] = state.get(f"sentry:{group['key']}")


def address(value):
    """An IP address as `tailscale serve` reports one, an IPv4-mapped one as IPv4; else None.

    Nothing else is accepted: no port, brackets, zone, list or surrounding space.
    """
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return None
    if "%" in value:
        return None
    return getattr(ip, "ipv4_mapped", None) or ip


def unfenced_workers(home):
    """Registered machines missing an IPv4 or IPv6 ``tailnet_ip``: not every request of
    theirs can be told apart."""
    try:
        return sorted(m.id for m in machines.load(home).values() if not machines.fenced(m))
    except (OSError, ValueError):
        return []


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    # Connections wait here while the accept loop is busy; the default of 5 overflows
    # under a few polling tabs, and the proxy in front then answers 502.
    request_queue_size = 128

    def __init__(
        self,
        home: Path,
        port: int,
        allowed_hosts: tuple[str, ...] | list[str] = (),
        overview: Overview | None = None,
        workspaces=None,
        ci=None,
        ci_logs=None,
        issues: IssueOverview | None = None,
        upstream_tests=None,
        workspace_overview=None,
        push=None,
        sentry=None,
        cron=None,
        attachments: Path | None = None,
        codex_updates=None,
        require_tailscale_user=False,
        auth: dashboard_auth.Auth | None = None,
    ) -> None:
        self.home = home
        self.tags = work_tags.Tags(home)
        # None while auth is off: every request is answered as before Phase 1.
        self.auth = auth
        # Through `tailscale serve`, only user-owned devices may use more than /api/worker/.
        self.require_tailscale_user = require_tailscale_user
        # Set when the dashboard stops to be started again on the checkout's code.
        self.restart = False
        self.attachments = attachments or home / "attachments"
        self.triage = attention.Triage(home)
        self.attention = attention.Cached(self.attention_feed)
        self.overview = overview
        self.issues = issues
        self.upstream_tests = upstream_tests
        self.sentry = sentry
        self.cron = cron
        self.codex_updates = codex_updates
        self.workspace_overview = workspace_overview
        self.workspaces = workspaces
        self.ci = ci
        self.ci_logs = ci_logs
        self.push = push
        self.allowed_hosts = set(allowed_hosts)
        self.extension = browser_extension.Extension(home)
        self.moves = workspace_move.Moves(
            home, lambda: self.workspaces, lambda: self.workspace_overview
        )
        super().__init__(("127.0.0.1", port), Handler)

    def tag_snapshot(self):
        rows = []
        warning = None
        if self.workspace_overview:
            try:
                rows = self.workspace_overview.snapshot(start=False).get("workspaces", [])
            except Exception:
                warning = "Workspace tag links unavailable; showing saved tags"
        value = self.tags.snapshot(rows)
        if warning:
            value["warning"] = warning
        return value

    def attention_feed(self):
        """Every source through its own snapshot; a failing one is named, the rest answer.

        The workspace inventory is read as it is: a scan starts only from its own tab.
        """
        return attention.collect(
            watcher=lambda: status(self.home),
            prs=self.overview.snapshot if self.overview else None,
            issues=self.issues.snapshot if self.issues else None,
            workspaces=self.workspaces.snapshot if self.workspaces else None,
            workspace_overview=(
                (lambda: self.workspace_overview.snapshot(start=False))
                if self.workspace_overview
                else None
            ),
            scheduled=self.workspaces.scheduled_tasks if self.workspaces else None,
            cron=self.cron.snapshot if self.cron else None,
            triage=self.triage,
        )

    def workspace_checkout(self, workspace_id):
        return workspace_viewer.workspace_checkout(
            workspace_id, self.workspaces.recorded_checkout if self.workspaces else None
        )

    def handle_error(self, request, client_address):
        # A phone that sleeps or loses the tailnet mid-request is routine, not a fault.
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            return
        super().handle_error(request, client_address)


class Handler(BaseHTTPRequestHandler):
    server: DashboardServer
    # Polls reuse one connection (keep-alive) instead of opening one per request.
    protocol_version = "HTTP/1.1"
    # Headers and body go out as separate writes; on a kept-alive connection Nagle's
    # algorithm would hold the body until the client's delayed ACK (40 ms on Linux).
    disable_nagle_algorithm = True
    # A client that stops sending or reading frees its thread, as does a connection idle
    # this long between requests; request bodies narrow it.
    timeout = 30
    # Whether this request's response has begun: never start a second one.
    started = False
    # The request body's bytes not read yet; None when unknown, which ends the connection.
    unread: int | None = 0

    def setup(self):
        try:
            super().setup()
        except OSError as exc:
            # macOS refuses socket options on a connection its client reset while it
            # waited to be accepted: a disconnect, not a fault.
            if exc.errno == errno.EINVAL:
                raise ConnectionResetError(errno.ECONNRESET, "Client left before setup") from exc
            raise

    def log_message(self, *args):
        pass

    def send_body(self, code, body, content_type, etag=None, compressed=None, headers=()):
        """Send a response; a client that has gone away is quietly sent nothing more.

        API data is never stored by the browser; static assets are revalidated by ETag.
        ``compressed`` is a precomputed gzip encoding of ``body``. A 304 sends the
        headers a 200 would have, without the body. ``headers`` are added as given
        (Location, Set-Cookie).
        """
        self.started = True
        if self.unread != 0:
            # The next request on this connection starts after this body: a small one
            # is read and dropped, otherwise the connection ends with this response.
            if self.unread is not None and self.unread <= DRAIN_MAX:
                try:
                    self.read_body(self.unread, 5)
                except OSError:
                    pass
            if self.unread != 0:
                self.close_connection = True
        encoding = None
        vary = []
        if compressible(content_type) and len(body) >= COMPRESS_MIN:
            vary.append("Accept-Encoding")
            if code != 304 and accepts_gzip(self.headers.get("Accept-Encoding")):
                if compressed is None:
                    compressed = gzip.compress(body, compresslevel=COMPRESS_LEVEL, mtime=0)
                body, encoding = compressed, "gzip"
        # Neither has a body, nor a length, which would describe one.
        bodiless = code in (204, 304)
        if bodiless:
            body = b""
        try:
            self.send_response(code)
            if self.close_connection:
                self.send_header("Connection", "close")
            if not bodiless:
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
            if encoding:
                self.send_header("Content-Encoding", encoding)
            self.send_header("Cache-Control", "no-cache" if etag else "no-store")
            if etag:
                self.send_header("ETag", etag)
            self.send_header("X-Content-Type-Options", "nosniff")
            origin = self.headers.get("Origin")
            if (
                origin
                and self.path == "/api/extension"
                and self.server.extension.known_origin(origin)
            ):
                self.send_header("Access-Control-Allow-Origin", origin)
                vary.insert(0, "Origin")
                self.send_header("Access-Control-Allow-Methods", "POST")
                self.send_header(
                    "Access-Control-Allow-Headers",
                    "Authorization, Content-Type, X-Babysit-Extension",
                )
            if vary:
                self.send_header("Vary", ", ".join(vary))
            for name, value in headers:
                self.send_header(name, value)
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
            )
            self.end_headers()
            view = memoryview(body)
            for start in range(0, len(view), WRITE_CHUNK):
                self.wfile.write(view[start : start + WRITE_CHUNK])
        except OSError:
            # Disconnected or stalled past the timeout: nothing more can reach it.
            self.close_connection = True

    def send_json(self, code, value):
        self.send_body(code, json.dumps(value).encode(), "application/json; charset=utf-8")

    def fail(self, code, message):
        """An error response, unless one already began (its client may be gone).

        A response cut short cannot be completed, so its connection closes rather than
        leave a kept-alive client waiting for the rest.
        """
        if self.started:
            self.close_connection = True
        else:
            self.send_json(code, {"error": message})

    def read_body(self, length, timeout):
        """Read the request body of ``length`` bytes, each read within ``timeout`` seconds.

        Only a whole body, of the length the request declared, keeps the connection open.
        """
        declared, self.unread = self.unread, None
        self.connection.settimeout(timeout)
        data = self.rfile.read(length)
        if len(data) == length == declared:
            self.unread = 0
            self.connection.settimeout(self.timeout)
        return data

    def guarded(self, handler):
        """Answer every request: an unexpected error is logged and becomes a 500."""
        self.started = False
        length = self.headers.get("Content-Length")
        try:
            self.unread = 0 if length is None else int(length)
        except ValueError:
            self.unread = None
        if self.headers.get("Transfer-Encoding") or (self.unread or 0) < 0:
            self.unread = None  # Chunked bodies are not read: never mistaken for a request.
        try:
            if refusal := self.fenced():
                self.send_json(403, {"error": refusal})
                return
            if self.server.auth is not None and not self.authorized():
                return
            handler()
        except Exception as exc:
            traceback.print_exc()
            # A response it interrupted may be incomplete: never send another after it.
            self.close_connection = True
            self.fail(500, f"Unexpected dashboard error: {type(exc).__name__}: {exc}")

    def fenced(self):
        """Why a request from the tailnet may not use this route; None when it may.

        Worker machines reach the dashboard through `tailscale serve` for /api/worker/
        and nothing else: their agents, sandboxed or not, share their network. The proxy
        sets X-Forwarded-For to the tailnet address a request came from, replacing any
        the client sent, but passes the client's Host through, so the address decides,
        never the Host. A request without the header came from this machine directly.
        """
        forwarded = self.headers.get_all("X-Forwarded-For")
        if not forwarded or (
            self.command == "POST"
            and self.path in {f"/api/worker/{action}" for action in worker_api.ACTIONS}
        ):
            return None
        # The proxy sends exactly one, a bare address: anything else did not come from it.
        source = address(forwarded[0]) if len(forwarded) == 1 else None
        if source is None:
            return "Unexpected X-Forwarded-For; only tailscale serve may proxy the dashboard"
        # The proxy names the user of a user-owned device, never of a tagged node, and
        # drops any name the client sent.
        if self.server.require_tailscale_user and not self.headers.get("Tailscale-User-Login"):
            return "Only a tailnet user's device may use the dashboard"
        try:
            registered = machines.load(self.server.home).values()
        except (OSError, ValueError):
            return "machines.json cannot be read, so no tailnet request may use the dashboard"
        workers = {address(ip) for m in registered for ip in m.tailnet_ip or ()}
        if source in workers:
            return "A worker machine may only use /api/worker/"
        return None

    def authorized(self):
        """Whether the route's handler may answer: sign-in routes are answered here.

        In report mode every request goes on, and those enforce would refuse are
        counted; in enforce mode they get 401 (no credential) or 403 (a key whose
        scope does not cover the route).
        """
        auth = self.server.auth
        assert auth is not None
        path = dashboard_auth.request_path(self.path)
        if path is not None and (path.startswith("/auth/") or path == "/api/session"):
            self.auth_request(path)
            return False
        decision = auth.decide(self.command, self.path, self.headers, self.secure_host())
        if decision.allowed:
            return True
        auth.report.record(self.command, path, decision, refused=auth.enforce, routes=API_ROUTES)
        if not auth.enforce:
            return True
        self.send_json(
            403 if decision.authenticated else 401,
            {
                "error": "This key may not use this route"
                if decision.authenticated
                else "Sign in to use the dashboard",
                "login": "/auth/login" if auth.oidc else None,
            },
        )
        return False

    def secure_host(self):
        """Every host but plain-http loopback is reached over https (tailscale serve)."""
        port = self.server.server_port
        return self.headers.get("Host") not in {f"127.0.0.1:{port}", f"localhost:{port}"}

    def same_origin_action(self, action):
        """The CSRF guard of post(), for a sign-in route's POST."""
        host = self.headers.get("Host")
        origin = self.headers.get("Origin")
        return (
            self.headers.get("X-Babysit-Action") == action
            and self.headers.get("Sec-Fetch-Site") != "cross-site"
            and (origin is None or origin in {f"http://{host}", f"https://{host}"})
        )

    def session_cookie(self, token, max_age):
        secure = self.secure_host()
        name = dashboard_auth.session_cookie_name(secure)
        return ("Set-Cookie", dashboard_auth.cookie(name, token, secure=secure, max_age=max_age))

    def auth_request(self, path):
        """/auth/login, /auth/callback, /auth/logout, /auth/break-glass and /api/session."""
        auth = self.server.auth
        assert auth is not None
        host = self.headers.get("Host")
        if not self.extension_host():
            self.send_json(403, {"error": "Use a configured dashboard URL"})
            return
        query = parse_qs(urlsplit(self.path).query)
        secure = self.secure_host()
        login_name = dashboard_auth.login_cookie_name(secure)
        # The login cookie is cleared by the callback, whatever its outcome.
        cleared = dashboard_auth.cookie(login_name, "", secure=secure, max_age=0, path="/auth/")
        try:
            if self.command == "GET" and path == "/api/session":
                if self.headers.get("Sec-Fetch-Site") == "cross-site":
                    self.send_json(403, {"error": "Cross-site access is disabled"})
                    return
                self.send_json(200, auth.view(self.headers, secure))
            elif self.command == "GET" and path == "/auth/login":
                forwarded = self.headers.get_all("X-Forwarded-For") or []
                source = forwarded[0] if len(forwarded) == 1 else self.client_address[0]
                url, state = auth.begin_login(host, query.get("next", ["/"])[0], source)
                login = dashboard_auth.cookie(
                    login_name,
                    state,
                    secure=secure,
                    max_age=dashboard_auth.LOGIN_TTL,
                    path="/auth/",
                    # The callback arrives as a cross-site navigation from the provider.
                    same_site="Lax",
                )
                self.send_body(
                    302, b"", "text/plain", headers=[("Location", url), ("Set-Cookie", login)]
                )
            elif self.command == "GET" and path == "/auth/callback":
                if "error" in query:
                    error = dashboard_auth.provider_error(query["error"][0])
                    raise dashboard_auth.AuthError(f"The identity provider refused: {error}", 403)
                bound = dashboard_auth.read_cookie(self.headers.get("Cookie"), login_name)
                token, result = auth.finish_login(
                    host,
                    query.get("state", [None])[0],
                    query.get("code", [None])[0],
                    bound,
                    secure,
                )
                self.send_body(
                    302,
                    b"",
                    "text/plain",
                    headers=[
                        ("Location", result["next"]),
                        ("Set-Cookie", cleared),
                        self.session_cookie(token, result["max_age"]),
                    ],
                )
            elif self.command == "POST" and path == "/auth/logout":
                if not self.same_origin_action("logout"):
                    self.send_json(403, {"error": "Sign out from the dashboard page"})
                    return
                redirect = auth.logout(
                    self.headers, f"{'https' if secure else 'http'}://{host}", secure
                )
                name = dashboard_auth.session_cookie_name(secure)
                self.send_body(
                    200,
                    json.dumps({"redirect": redirect}).encode(),
                    "application/json; charset=utf-8",
                    headers=[
                        ("Set-Cookie", dashboard_auth.cookie(name, "", secure=secure, max_age=0))
                    ],
                )
            elif self.command == "GET" and path == "/auth/break-glass":
                body, packed, etag = static_asset("break-glass.html", "text/html; charset=utf-8")
                code = 304 if etag_matches(self.headers.get("If-None-Match"), etag) else 200
                self.send_body(code, body, "text/html; charset=utf-8", etag=etag, compressed=packed)
            elif self.command == "POST" and path == "/auth/break-glass":
                length = int(self.headers.get("Content-Length", "0"))
                if (
                    not self.same_origin_action("break-glass")
                    or not 0 < length <= 1024
                    or self.headers.get("Content-Type") != "application/json"
                ):
                    self.send_json(403, {"error": "Open the sign-in link in a browser"})
                    return
                request = json.loads(self.read_body(length, 5))
                token, max_age = auth.redeem_link(
                    request.get("token") if isinstance(request, dict) else None, secure
                )
                self.send_body(
                    200,
                    b'{"ok": true}',
                    "application/json; charset=utf-8",
                    headers=[self.session_cookie(token, max_age)],
                )
            else:
                self.send_json(404, {"error": "Not found"})
        except dashboard_auth.AuthError as exc:
            if self.command == "GET" and path in {"/auth/login", "/auth/callback"}:
                # A browser navigation: a page, never JSON.
                self.send_body(
                    exc.status,
                    dashboard_auth.login_page(str(exc), exc.status),
                    "text/html; charset=utf-8",
                    headers=[("Set-Cookie", cleared)] if path == "/auth/callback" else [],
                )
            else:
                self.send_json(exc.status, {"error": str(exc)})
        except ValueError as exc:
            self.fail(400, str(exc))

    def do_GET(self):
        self.guarded(self.get)

    def do_POST(self):
        self.guarded(self.post)

    def do_OPTIONS(self):
        self.guarded(self.options)

    def post(self):
        if self.path == "/api/extension":
            self.extension_request()
            return
        if self.path.startswith("/api/worker/"):
            self.worker_request()
            return
        host = self.headers.get("Host")
        allowed = {
            f"127.0.0.1:{self.server.server_port}",
            f"localhost:{self.server.server_port}",
        } | self.server.allowed_hosts
        origin = self.headers.get("Origin")
        action = self.headers.get("X-Babysit-Action")
        # A custom header requires a browser preflight; no cross-origin requests
        # are allowed. Check Origin too, including for trusted tailnet proxies.
        if (
            host not in allowed
            or action not in ACTIONS
            or self.headers.get("Sec-Fetch-Site") == "cross-site"
            or (origin is not None and origin not in {f"http://{host}", f"https://{host}"})
        ):
            self.send_json(403, {"error": "This action requires the configured dashboard"})
            return
        if self.path != f"/api/{action}":
            self.send_json(404, {"error": "Not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if action == "attachment-upload":
                self.upload(length)
                return
            if (
                not 0
                < length
                <= (
                    200000
                    if action.startswith("push-")
                    or action
                    in {
                        "workspace-action",
                        "workspace-batch",
                        "workspace-new",
                        "workspace-subtask",
                        "workspace-cleanup",
                        "workspace-open",
                        "workspace-message",
                        "workspace-answer",
                        "workspace-prompt-forget",
                        "settings",
                        "tags",
                        "sentry-action",
                        "cron-action",
                        "voice-turn",
                        "voice-log",
                    }
                    else 1024
                )
                or self.headers.get("Content-Type") != "application/json"
            ):
                raise ValueError("Expected a small JSON action request")
            request = json.loads(self.read_body(length, 5))
            if not isinstance(request, dict):
                raise ValueError("Expected a JSON action request")
            attached = bool(request.get("attachments"))
            # Attached files become paths in the text the agent receives.
            attachments.attach(self.server.attachments, action, request)
            # The worker an agent action names, from the registry; None for this machine.
            machine = None
            if action in WORKER_ACTIONS and "workspace" in request:
                machine, request["workspace"] = viewed_workspace(
                    self.server.home, request["workspace"]
                )
            elif action == "workspace-docker" and isinstance(request.get("workspace"), str):
                if viewed_workspace(self.server.home, request["workspace"])[0] is not None:
                    raise ValueError("Docker access can be changed only for agents on this Mac")
            if action.startswith("workspace-"):
                for name in ("workspace", "workspace_id"):
                    if name in request and machine is None:
                        request[name] = local_workspace(self.server.home, request[name])
            if action == "notification-silence":
                if not self.server.push:
                    raise ValueError("Notification preferences are unavailable")
                self.send_json(200, self.server.push.silence(request))
                return
            if action == "notification-focus":
                if not self.server.push:
                    raise ValueError("Notification preferences are unavailable")
                self.send_json(200, self.server.push.focus(request))
                return
            if action == "tags":
                self.server.tags.change(request)
                self.send_json(200, self.server.tag_snapshot())
                return
            if action == "attention-triage":
                # Validated against the shared feed; the answer carries a fresh one.
                value = self.server.triage.set(request, self.server.attention.get())
                self.send_json(200, {**value, "feed": self.server.attention.get(fresh=True)})
                return
            if action.startswith("push-"):
                if not self.server.push:
                    raise ValueError("Web Push is not enabled")
                self.send_json(
                    200, self.server.push.action(action, request, origin or f"http://{host}")
                )
                return
            if action == "sentry-action":
                # Experimental Sentry actions own their validation and failure boundary.
                if not self.server.sentry:
                    raise ValueError("The Sentry experiment is disabled")
                try:
                    value = self.server.sentry.action(request)
                except ValueError:
                    raise
                except Exception:
                    value = {"error": "Sentry experiment unavailable"}
                self.send_json(200, value)
                return
            if action == "cron-action":
                if not self.server.cron:
                    raise ValueError("Cron jobs are not enabled")
                self.send_json(200, self.server.cron.action(request))
                return
            if action in {"workspace-cleanup", "workspace-open"}:
                # Experimental workspace actions own their validation.
                if not self.server.workspace_overview:
                    raise ValueError("The workspace experiment is disabled")
                try:
                    value = (
                        self.server.workspace_overview.open_workspace(request)
                        if action == "workspace-open"
                        else self.server.workspace_overview.cleanup(request)
                    )
                except ValueError:
                    raise
                except Exception:
                    value = {"error": "Workspace experiment unavailable"}
                self.send_json(200, value)
                return
            if action == "workspace-message":
                checkout = None
                if "key" in request and "watch" in request:
                    raise ValueError("Supply a workspace key or a watch")
                if "key" in request:
                    # A Workspaces-tab row listed while no herdr workspace was open.
                    if not self.server.workspace_overview:
                        raise ValueError("The workspace experiment is disabled")
                    key = request.pop("key")
                    worker = self.server.workspace_overview.worker_checkout(key)
                    if worker:
                        machine, checkout = worker
                    else:
                        checkout = self.server.workspace_overview.checkout(key)
                if "watch" in request:
                    # A watch's checkout, where its repairs run without a workspace.
                    checkout, _ = watch_checkout(self.server.home, request.pop("watch"))
                prepare = None
                if machine is not None and attached:
                    worker, home, folder = machine, self.server.home, self.server.attachments

                    def copy_files(text):
                        # Uploads are kept on this machine: its agent reads copies there.
                        return attachments.for_worker(home, worker, text, folder)

                    prepare = copy_files
                try:
                    self.send_json(
                        200,
                        agent_messages.send(
                            request,
                            self.server.home,
                            checkout=checkout,
                            machine=machine,
                            prepare=prepare,
                        ),
                    )
                except agent_messages.Refused as exc:
                    # The composer acts on the code: it offers to cancel the owning watch.
                    self.send_json(400, {"error": str(exc), **exc.details})
                except (OSError, subprocess.SubprocessError, sqlite3.Error) as exc:
                    self.send_json(503, {"error": f"Nothing was typed: {exc}"})
                return
            if action == "workspace-docker":
                try:
                    self.send_json(200, agent_messages.set_docker(request, self.server.home))
                except (OSError, subprocess.SubprocessError, sqlite3.Error) as exc:
                    self.send_json(503, {"error": f"Check Docker access in Collie: {exc}"})
                return
            if action == "workspace-interrupt":
                try:
                    self.send_json(200, agent_messages.interrupt(request, machine))
                except (OSError, subprocess.SubprocessError) as exc:
                    self.send_json(503, {"error": f"Check the agent in Collie: {exc}"})
                return
            if action == "workspace-quit":
                try:
                    self.send_json(200, agent_messages.exit_agent(request, self.server.home))
                except (OSError, subprocess.SubprocessError) as exc:
                    self.send_json(503, {"error": f"Check the agent in Collie: {exc}"})
                return
            if action == "workspace-move":
                # Started here and followed by its ID; the move itself runs on.
                try:
                    self.send_json(200, self.server.moves.action(request))
                except workspace_move.Refused as exc:
                    self.send_json(400, {"error": str(exc), **exc.details})
                except (OSError, subprocess.SubprocessError) as exc:
                    self.send_json(503, {"error": f"Nothing was moved: {exc}"})
                return
            if action == "workspace-answer":
                try:
                    self.send_json(200, agent_messages.answer(request, self.server.home, machine))
                except (OSError, subprocess.SubprocessError) as exc:
                    self.send_json(503, {"error": f"Check the question in Collie: {exc}"})
                return
            if action == "workspace-choose":
                try:
                    self.send_json(200, agent_messages.choose(request, machine))
                except (OSError, subprocess.SubprocessError) as exc:
                    self.send_json(503, {"error": f"Check the dialog in Collie: {exc}"})
                return
            if action == "service-restart":
                self.service_restart(request)
                return
            if action == "codex-update":
                if not self.server.codex_updates:
                    raise ValueError("Codex update checks are not enabled")
                self.server.codex_updates.update()
                self.send_json(200, self.server.codex_updates.snapshot())
                return
            if action == "worker-update":
                if not self.server.workspaces:
                    raise ValueError("Workspace actions are not enabled")
                self.send_json(
                    200, self.server.workspaces.worker_updates.update(request.get("machine"))
                )
                return
            if action == "settings":
                self.send_json(200, dashboard_settings.save(self.server.home, request))
                return
            if action == "voice-log":
                self.send_json(200, voice_assistant.log(self.server.home, request))
                return
            if action == "voice-turn":
                if "cancel" in request:
                    self.send_json(200, voice_assistant.cancel(self.server.home, request["cancel"]))
                    return
                # The supervisor answers; its tools read this dashboard on loopback.
                self.send_json(
                    200,
                    voice_assistant.enqueue(
                        self.server.home, request, f"http://127.0.0.1:{self.server.server_port}"
                    ),
                )
                return
            if action == "effort-default":
                self.send_json(
                    200,
                    {
                        "effort_defaults": workspace_agents.save_effort_default(
                            self.server.home, request
                        )
                    },
                )
                return
            if action == "workspace-prompt-forget":
                if not self.server.workspaces:
                    raise ValueError("Workspace actions are not enabled")
                self.send_json(200, self.server.workspaces.forget_prompt(request))
                return
            if action == "workspace-batch":
                if not self.server.workspaces:
                    raise ValueError("Workspace actions are not enabled")
                self.send_json(200, self.server.workspaces.batch(request))
                return
            if action == "workspace-new":
                if not self.server.workspaces:
                    raise ValueError("Workspace actions are not enabled")
                self.send_json(200, self.server.workspaces.new_task(request))
                return
            if action == "workspace-subtask":
                # From a `wt` inside Safehouse here: its parent is what it claims.
                if not self.server.workspaces:
                    raise ValueError("Workspace actions are not enabled")
                if set(request) != worker_api.LAUNCH:
                    raise ValueError("Expected exactly the launch and parent fields")
                self.send_json(
                    200,
                    self.server.workspaces.subtask(
                        request["launch"], worker_api.local_parent(request["parent"])
                    ),
                )
                return
            if action == "extension-pair":
                self.send_json(200, self.server.extension.pair(request))
                return
            if not isinstance(request.get("id"), str) or not request["id"]:
                raise ValueError("Supply a watch ID")
            if action == "workspace-action":
                if not self.server.workspaces:
                    raise ValueError("Workspace actions are not enabled")
                self.send_json(200, self.server.workspaces.action(request))
                return
            if action == "schedule-cancel":
                if not self.server.workspaces:
                    raise ValueError("Workspace actions are not enabled")
                self.send_json(
                    200, {"task": self.server.workspaces.cancel_scheduled(request["id"])}
                )
                return
            if action == "feedback":
                job = handle_feedback(self.server.home, request["id"], request.get("token"))
            elif action == "feedback-addressed":
                if not isinstance(request.get("item"), str) or not request["item"]:
                    raise ValueError("Supply a feedback item")
                job = handle_feedback(
                    self.server.home, request["id"], request.get("token"), request["item"]
                )
            else:
                job = cancel_watch(self.server.home, request["id"])
            self.send_json(200, {"job": job})
        except ValueError as exc:
            self.fail(400, str(exc))
        except (OSError, sqlite3.Error, subprocess.SubprocessError) as exc:
            self.fail(503, f"Cannot update watch: {exc}")

    def service_restart(self, request):
        """Restart the watcher daemon, or this dashboard, for the Services dialog."""
        service = request.get("service")
        if service not in {"daemon", "dashboard"}:
            raise ValueError("Choose the watcher daemon or the dashboard")
        if service == "daemon":
            try:
                service_control.restart_daemon(self.server.home)
            except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
                self.fail(503, f"Could not restart the watcher: {exc}")
                return
            self.send_json(200, {"daemon": daemon_health(self.server.home)})
            return
        # Answered before it stops: the page polls for the process that replaces this one.
        service_control.check_restartable()
        self.send_json(200, {"dashboard": service_control.dashboard_state()})
        service_control.restart_dashboard(self.server)

    def upload(self, length):
        """Store a file sent as the raw request body, named by its X-Filename header."""
        if (
            not 0 < length <= attachments.MAX_FILE
            or self.headers.get("Content-Type") != "application/octet-stream"
        ):
            raise ValueError(f"Attach a file of at most {attachments.MAX_FILE // (1024 * 1024)} MB")
        name = unquote(self.headers.get("X-Filename") or "file")
        # A phone over the tailnet can be slow; each read still has a limit.
        try:
            data = self.read_body(length, 30)
        except OSError:
            data = b""
        if len(data) != length:
            raise ValueError("The upload was cut short; attach the file again")
        self.send_json(200, attachments.save(self.server.attachments, name, data))

    def extension_host(self):
        return (
            self.headers.get("Host")
            in {
                f"127.0.0.1:{self.server.server_port}",
                f"localhost:{self.server.server_port}",
            }
            | self.server.allowed_hosts
        )

    def options(self):
        if (
            self.path == "/api/extension"
            and self.extension_host()
            and self.server.extension.known_origin(self.headers.get("Origin"))
            and self.headers.get("Access-Control-Request-Method") == "POST"
        ):
            self.send_body(204, b"", "application/json")
        else:
            self.send_json(403, {"error": "Pair this extension from the dashboard first"})

    def extension_request(self):
        client = self.server.extension.authenticate(self.headers) if self.extension_host() else None
        if not client:
            self.send_json(403, {"error": "Pair this extension from the dashboard settings"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 200000:
                raise ValueError("Invalid request size")
            request = json.loads(self.read_body(length, 10))
            if not isinstance(request, dict):
                raise ValueError("Expected an object")
            result = self.server.extension.dispatch(self.server, client, request)
            self.send_json(400 if "error" in result else 200, result)
        except ValueError as exc:
            self.fail(400, str(exc))
        except (OSError, sqlite3.Error, subprocess.SubprocessError) as exc:
            self.fail(503, f"Cannot deliver task: {exc}")

    def worker_request(self):
        """A registration or handoff forwarded by a worker machine; its token names the machine.

        Not a browser's: a request with an Origin or Fetch metadata is refused.
        """
        action = self.path.removeprefix("/api/worker/")
        machine = (
            worker_api.authenticate(self.server.home, self.headers.get("Authorization"))
            if self.extension_host()
            and action in worker_api.ACTIONS
            and self.headers.get("Origin") is None
            and self.headers.get("Sec-Fetch-Site") is None
            else None
        )
        # `tailscale serve` sets X-Forwarded-For to the tailnet address a request came
        # from, replacing any the client sent; a machine's own address, when registered,
        # must be the only one.
        if machine is not None and machine.tailnet_ip is not None:
            forwarded = self.headers.get_all("X-Forwarded-For") or []
            if len(forwarded) != 1 or forwarded[0] not in machine.tailnet_ip:
                machine = None
        if machine is None:
            self.send_json(403, {"error": "This needs a worker token issued by the controller"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if (
                not 0 < length <= worker_api.MAX_BODY
                or self.headers.get("Content-Type") != "application/json"
            ):
                raise ValueError("Send one JSON request body")
            request = json.loads(self.read_body(length, 10))
            answer = worker_api.handle(
                self.server.home, machine, action, request, launch=self.worker_launch
            )
            if action == "launch":
                # Recorded: from here nothing is a refusal, which would invite a retry.
                answer = self.worker_launched(answer["operation"])
            self.send_json(200, answer)
        except worker_api.RateLimited as exc:
            self.fail(429, str(exc))
        except (ValueError, RuntimeError) as exc:
            self.fail(400, str(exc))
        except (OSError, sqlite3.Error, subprocess.SubprocessError) as exc:
            self.fail(503, f"Cannot answer the worker: {exc}")

    def worker_launch(self, launch, parent):
        """A sub-task a worker's sandbox asked for, as soon as it is recorded: the worker's
        request limits are released before ``worker_launched`` waits for it."""
        if not self.server.workspaces:
            raise ValueError("Workspace actions are not enabled")
        return self.server.workspaces.subtask(launch, parent)

    def worker_launched(self, op):
        """A worker's sub-task once started or a few minutes on, without its log, which
        may be long."""
        op = self.server.workspaces.await_operation(op, SUBTASK_WAIT)
        result = op.get("result") or {}
        return {
            "operation": {
                "pr": op["pr"],
                "status": op["status"],
                "message": str(op.get("message") or "")[:2000],
                "machine": op.get("machine"),
                "result": {k: result.get(k) for k in ("branch", "path", "machine_label", "url")}
                if result
                else None,
            }
        }

    def get(self):
        port = self.server.server_port
        if (
            self.headers.get("Host")
            not in {f"127.0.0.1:{port}", f"localhost:{port}"} | self.server.allowed_hosts
        ):
            self.send_json(403, {"error": "Use a configured dashboard URL"})
            return
        route = urlsplit(self.path)
        # A cross-site link may open the dashboard, but never an API route: some GETs
        # (`?refresh=1`) start work. Same-origin navigations still reach every route.
        if self.headers.get("Sec-Fetch-Site") == "cross-site" and (
            self.headers.get("Sec-Fetch-Mode") != "navigate" or route.path.startswith("/api/")
        ):
            self.send_json(403, {"error": "Cross-site access is disabled"})
            return
        try:
            if route.path == "/api/status":
                value = status(self.server.home)
                if self.server.auth:
                    value["auth"] = self.server.auth.summary()
                self.send_json(200, value)
            elif route.path == "/api/session":
                # Reached only while auth is off; otherwise authorized() answers it.
                self.send_json(200, {"enabled": False})
            elif route.path == "/api/auth-report":
                # What enforce mode would refuse; report mode counts it without refusing.
                self.send_json(
                    200,
                    {"mode": self.server.auth.mode, "rows": self.server.auth.report.rows()}
                    if self.server.auth
                    else {"enabled": False},
                )
            elif route.path == "/api/watch":
                # One watch with all its CI details, which the status poll leaves out of
                # ended watches.
                job_id = parse_qs(route.query).get("id", [""])[0]
                job = next(
                    (job for job in read_jobs(self.server.home) if job["id"] == job_id), None
                )
                if job is None:
                    self.send_json(404, {"error": "Unknown watch"})
                else:
                    self.send_json(200, {"job": present_job(job)})
            elif route.path == "/api/llm-usage":
                refresh = parse_qs(route.query).get("refresh") == ["1"]
                llm_usage.request(self.server.home, refresh)
                heartbeat = read_json(self.server.home / "heartbeat.json") or {}
                reader = llm_usage.reader_state(self.server.home, heartbeat)
                self.send_json(200, llm_usage.present(self.server.home, reader=reader))
            elif route.path == "/api/service":
                self.send_json(
                    200,
                    {
                        "daemon": daemon_health(self.server.home),
                        "dashboard": service_control.dashboard_state(),
                    },
                )
            elif route.path == "/api/codex-update":
                self.send_json(
                    200,
                    self.server.codex_updates.snapshot()
                    if self.server.codex_updates
                    else {"available": False, "update": None},
                )
            elif route.path == "/api/notification-preferences":
                self.send_json(
                    200,
                    self.server.push.preferences()
                    if self.server.push
                    else {"login": None, "silenced": []},
                )
            elif route.path == "/api/push-config":
                self.send_json(
                    200, self.server.push.config() if self.server.push else {"available": False}
                )
            elif route.path == "/api/upstream-tests":
                # Experimental failures must stay outside the main dashboard boundary.
                try:
                    value = (
                        self.server.upstream_tests.snapshot()
                        if self.server.upstream_tests
                        else {"enabled": False}
                    )
                    self.send_json(200, value)
                except Exception:
                    self.send_json(
                        200, {"enabled": True, "error": "Upstream test experiment unavailable"}
                    )
            elif route.path == "/api/sentry":
                # Experimental failures must stay outside the main dashboard boundary.
                try:
                    if self.server.sentry:
                        if parse_qs(route.query).get("refresh") == ["1"]:
                            self.server.sentry.request_refresh()
                        value = self.server.sentry.snapshot()
                        attach_handling(value, self.server.workspaces)
                    else:
                        value = {"enabled": False}
                    self.send_json(200, value)
                except Exception:
                    self.send_json(200, {"enabled": True, "error": "Sentry experiment unavailable"})
            elif route.path == "/api/cron":
                self.send_json(
                    200, self.server.cron.snapshot() if self.server.cron else {"enabled": False}
                )
            elif route.path == "/api/cron-log":
                if not self.server.cron:
                    raise ValueError("Cron jobs are not enabled")
                query = parse_qs(route.query)
                if set(query) != {"run"} or len(query["run"]) != 1:
                    raise ValueError("Supply a run")
                self.send_json(200, self.server.cron.log(query["run"][0]))
            elif route.path == "/api/workspace-overview":
                # Experimental inventory must stay outside the main dashboard boundary.
                try:
                    if self.server.workspace_overview:
                        if parse_qs(route.query).get("refresh") == ["1"]:
                            self.server.workspace_overview.request_refresh()
                        value = self.server.workspace_overview.snapshot()
                    else:
                        value = {"enabled": False}
                    self.send_json(200, value)
                except Exception:
                    self.send_json(
                        200, {"enabled": True, "error": "Workspace experiment unavailable"}
                    )
            elif route.path == "/api/workspace-agents":
                query = parse_qs(route.query)
                if set(query) not in ({"workspace"}, {"key"}, {"watch"}) or any(
                    len(values) != 1 for values in query.values()
                ):
                    raise ValueError("Supply a workspace, a workspace key or a watch")
                # A worker's row or workspace is read and messaged there; the machine
                # comes from the inventory or the registry, never the client.
                machine = None
                if "key" in query or "watch" in query:
                    # A Workspaces-tab row listed while no herdr workspace was open, or a
                    # watch's checkout: the one open on it now, which the viewer then follows.
                    if "watch" in query:
                        path, _ = watch_checkout(self.server.home, query["watch"][0])
                    elif not self.server.workspace_overview:
                        raise ValueError("The workspace experiment is disabled")
                    elif worker := self.server.workspace_overview.worker_checkout(query["key"][0]):
                        machine, path = worker
                    else:
                        path = self.server.workspace_overview.checkout(query["key"][0])
                    workspace_id = workspace_viewer.checkout_workspace(path, machine)
                else:
                    machine, workspace_id = viewed_workspace(
                        self.server.home, query["workspace"][0]
                    )
                    if machine is None:
                        self.server.workspace_checkout(workspace_id)
                if workspace_id is None:
                    # Nobody to message, but a recorded session can be resumed.
                    value = {
                        "agents": [],
                        "sessions": agent_messages.checkout_sessions(
                            path, self.server.home, machine
                        ),
                        "path": path,
                        "workspace": None,
                        "url": None,
                    }
                else:
                    # The workspace now open on this checkout: none once closed, another
                    # ID once reopened, which the viewer then follows.
                    live = workspace_viewer.live_workspace(workspace_id, machine)
                    value = {
                        "agents": agent_messages.docker_status(workspace_id, live, machine),
                        "sessions": agent_messages.sessions(
                            workspace_id, self.server.home, machine
                        ),
                        # A draft names its checkout, so a reused workspace ID cannot
                        # deliver another checkout's comments.
                        "path": agent_messages.workspace_root(workspace_id, machine),
                        # A worker's named qualified, as the viewer names it.
                        "workspace": machines.qualify(machine.id, live)
                        if machine and live
                        else live,
                        "url": live and collie_url(machine, live),
                    }
                value["machine_label"] = machine.label if machine else local_label(self.server.home)
                self.send_json(200, value)
            elif route.path in {"/api/workspace-diff", "/api/workspace-transcript"}:
                query = parse_qs(route.query)
                if any(len(values) != 1 for values in query.values()):
                    raise ValueError("Supply each parameter once")
                single = {name: values[0] for name, values in query.items()}
                # A herdr workspace (what Collie opens), a row of the Workspaces tab, or
                # a watch's registered checkout, which needs no workspace.
                if len({"workspace", "key", "watch"} & set(single)) != 1:
                    raise ValueError("Supply a workspace, a workspace key or a watch")
                prefer = None
                # A worker machine's row or workspace is read there, through its probe;
                # the machine comes from the inventory or the registry, never the client.
                machine = path = workspace_id = None
                if "key" in single:
                    if not self.server.workspace_overview:
                        raise ValueError("The workspace experiment is disabled")
                    key = single.pop("key")
                    worker = self.server.workspace_overview.worker_checkout(key)
                    if worker:
                        machine, path = worker
                    else:
                        path = self.server.workspace_overview.checkout(key)
                if "watch" in single:
                    # The session its repairs resume opens first.
                    path, prefer = watch_checkout(self.server.home, single.pop("watch"))
                if "workspace" in single:
                    machine, workspace_id = viewed_workspace(
                        self.server.home, single.pop("workspace")
                    )
                # Read-only views own their failure boundary, like the inventory.
                try:
                    if machine is not None and path is None:
                        path = workspace_viewer.remote_checkout(machine, workspace_id)
                    elif path is None:
                        path = self.server.workspace_checkout(workspace_id)
                    if route.path == "/api/workspace-diff":
                        if set(single) - {"scope", "base"}:
                            raise ValueError("Unknown diff parameter")
                        scope, base = single.get("scope", "branch"), single.get("base")
                        value = (
                            workspace_viewer.diff(path, scope, base)
                            if machine is None
                            else workspace_viewer.remote_diff(
                                self.server.home, machine, path, scope, base
                            )
                        )
                    else:
                        if set(single) - {"session", "before", "after"}:
                            raise ValueError("Unknown transcript parameter")
                        positions = {}
                        for name in ("before", "after"):
                            if name in single:
                                if not single[name].isdigit():
                                    raise ValueError("Expected a numeric page position")
                                positions[name] = int(single[name])
                        value = (
                            workspace_viewer.transcript(
                                path, single.get("session"), prefer=prefer, **positions
                            )
                            if machine is None
                            else workspace_viewer.remote_transcript(
                                self.server.home, machine, path, single.get("session"), **positions
                            )
                        )
                except ValueError:
                    raise
                except Exception as exc:
                    value = {"error": f"Workspace view unavailable: {exc}"}
                self.send_json(200, value)
            elif route.path == "/api/attention":
                fresh = parse_qs(route.query).get("refresh") == ["1"]
                self.send_json(200, self.server.attention.get(fresh=fresh))
            elif route.path == "/api/tags":
                self.send_json(200, self.server.tag_snapshot())
            elif route.path == "/api/prs":
                self.send_json(
                    200,
                    self.server.overview.snapshot()
                    if self.server.overview
                    else {
                        "prs": [],
                        "synced_at": None,
                        "error": "PR overview is not enabled",
                    },
                )
            elif route.path == "/api/issues":
                self.send_json(
                    200,
                    self.server.issues.snapshot()
                    if self.server.issues
                    else {
                        "issues": [],
                        "synced_at": None,
                        "error": "Issue overview is not enabled",
                    },
                )
            elif route.path == "/api/pr-ci-log":
                if not self.server.ci_logs:
                    raise ValueError("Background job logs are not enabled")
                query = parse_qs(route.query, keep_blank_values=True)
                pr_id, check_id = query.get("id", [""])[0], query.get("check", [""])[0]
                if "page" in query:
                    if len(query["page"]) != 1 or "download" in query:
                        raise ValueError("Supply one log page without a download parameter")
                    self.send_json(
                        200,
                        self.server.ci_logs.log_page(pr_id, check_id, int(query["page"][0])),
                    )
                elif query.get("download") == ["1"]:
                    self.send_body(
                        200,
                        self.server.ci_logs.raw_log(pr_id, check_id),
                        "text/plain; charset=utf-8",
                    )
                else:
                    self.send_json(200, self.server.ci_logs.snapshot(pr_id, check_id))
            elif route.path == "/api/pr-reviews":
                if not self.server.ci:
                    raise ValueError("Review details are not enabled")
                query = parse_qs(route.query)
                self.send_json(200, self.server.ci.snapshot(query.get("id", [""])[0], reviews=True))
            elif route.path == "/api/pr-ci":
                if not self.server.ci:
                    raise ValueError("CI details are not enabled")
                query = parse_qs(route.query)
                self.send_json(
                    200,
                    self.server.ci.snapshot(
                        query.get("id", [""])[0], query.get("check", [None])[0]
                    ),
                )
            elif route.path == "/api/workspaces":
                self.send_json(
                    200,
                    self.server.workspaces.snapshot()
                    if self.server.workspaces
                    else {"prs": {}, "issues": {}, "error": "Workspace actions are not enabled"},
                )
            elif route.path == "/api/machines":
                if not self.server.workspaces:
                    raise ValueError("Workspace actions are not enabled")
                self.send_json(200, {"machines": self.server.workspaces.machine_list()})
            elif route.path == "/api/placement":
                if not self.server.workspaces:
                    raise ValueError("Workspace actions are not enabled")
                params = {k: v[0] for k, v in parse_qs(route.query).items()}
                self.send_json(200, self.server.workspaces.preview(params))
            elif route.path == "/api/workspace-repos":
                everything = parse_qs(route.query).get("all", [""])[0] == "1"
                self.send_json(
                    200,
                    self.server.workspaces.local_repositories(everything)
                    if self.server.workspaces
                    else {"repos": [], "idle": 0},
                )
            elif route.path == "/api/settings":
                self.send_json(200, dashboard_settings.settings(self.server.home))
            elif route.path == "/api/voice-turn":
                query = parse_qs(route.query)
                self.send_json(
                    200, voice_assistant.status(self.server.home, query.get("id", [""])[0])
                )
            elif route.path == "/api/workspace-prompts":
                self.send_json(
                    200,
                    self.server.workspaces.prompts() if self.server.workspaces else {"prompts": []},
                )
            elif route.path == "/api/scheduled-tasks":
                # Cheap: reads only the workspace database, so any tab may poll it.
                self.send_json(
                    200,
                    self.server.workspaces.scheduled_tasks()
                    if self.server.workspaces
                    else {"enabled": False, "tasks": []},
                )
            elif route.path == "/api/log":
                query = parse_qs(route.query)
                self.send_json(
                    200,
                    tail_log(
                        self.server.home,
                        query.get("kind", ["agent"])[0],
                        query.get("job", [None])[0],
                    ),
                )
            else:
                files = {
                    "/": ("index.html", "text/html"),
                    "/cat.svg": ("cat.svg", "image/svg+xml"),
                    "/favicon.png": ("favicon.png", "image/png"),
                    "/apple-touch-icon.png": ("apple-touch-icon.png", "image/png"),
                    "/app.js": ("app.js", "text/javascript"),
                    "/auth.js": ("auth.js", "text/javascript"),
                    "/extension.html": ("extension.html", "text/html"),
                    "/extension.js": ("extension.js", "text/javascript"),
                    "/collie.js": ("collie.js", "text/javascript"),
                    "/notifications.js": ("notifications.js", "text/javascript"),
                    "/usage.js": ("usage.js", "text/javascript"),
                    "/codex_update.js": ("codex_update.js", "text/javascript"),
                    "/service.js": ("service.js", "text/javascript"),
                    "/settings.js": ("settings.js", "text/javascript"),
                    "/push.js": ("push.js", "text/javascript"),
                    "/sw.js": ("sw.js", "text/javascript"),
                    "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
                    "/icon-192.png": ("icon-192.png", "image/png"),
                    "/icon-512.png": ("icon-512.png", "image/png"),
                    "/upstream-tests.js": ("upstream-tests.js", "text/javascript"),
                    "/upstream-tests.css": ("upstream-tests.css", "text/css"),
                    "/tags.js": ("tags.js", "text/javascript"),
                    "/workspaces.js": ("workspaces.js", "text/javascript"),
                    "/workspaces.css": ("workspaces.css", "text/css"),
                    "/viewer.js": ("viewer.js", "text/javascript"),
                    "/attachments.js": ("attachments.js", "text/javascript"),
                    "/viewer.css": ("viewer.css", "text/css"),
                    "/sentry.js": ("sentry.js", "text/javascript"),
                    "/sentry.css": ("sentry.css", "text/css"),
                    "/cron.js": ("cron.js", "text/javascript"),
                    "/cron.css": ("cron.css", "text/css"),
                    "/attention.js": ("attention.js", "text/javascript"),
                    "/attention.css": ("attention.css", "text/css"),
                    "/voice.js": ("voice.js", "text/javascript"),
                    "/voice.css": ("voice.css", "text/css"),
                    "/style.css": ("style.css", "text/css"),
                }
                if route.path not in files:
                    self.send_json(404, {"error": "Not found"})
                    return
                filename, mime = files[route.path]
                content_type = mime + ("; charset=utf-8" if mime.startswith("text/") else "")
                body, packed, etag = static_asset(filename, content_type)
                code = 304 if etag_matches(self.headers.get("If-None-Match"), etag) else 200
                self.send_body(code, body, content_type, etag=etag, compressed=packed)
        except ValueError as exc:
            self.fail(400, str(exc))
        except (OSError, sqlite3.Error, subprocess.SubprocessError) as exc:
            self.fail(503, f"Cannot read watcher data: {exc}")


def serve(
    home,
    port=8765,
    open_browser=False,
    allowed_hosts=(),
    attachments_dir=None,
    require_tailscale_user=False,
    auth_mode="report",
    auth_home=None,
    oidc_config=None,
):
    """Run the dashboard; the exit status asks launchd for a restart, or not."""
    auth_home = auth_home or dashboard_auth.DEFAULT_HOME
    # Before anything starts: enforce with a broken configuration must not serve.
    auth = dashboard_auth.load(auth_mode, auth_home, oidc_config)
    from codex_updates import CodexUpdates
    from cron_jobs import CronJobs
    from dashboard_push import PushInbox
    from github_notifications import NotificationWatch
    from sentry_issues import SentryIssues
    from upstream_tests import UpstreamTests
    from workspace_overview import WorkspaceOverview

    upstream_tests = UpstreamTests(home)
    workspace_overview = WorkspaceOverview(home, jobs=lambda: read_jobs(home))
    overview = Overview(home)
    issues = IssueOverview(home)
    notifications = NotificationWatch({"prs": overview, "issues": issues})
    sentry = SentryIssues(home)
    workspaces = Workspaces(
        home,
        overview,
        lambda: read_jobs(home),
        issues=issues,
        sentry=sentry if sentry.enabled else None,
        attachments_dir=attachments_dir,
    )
    cron = CronJobs(home, workspaces=workspaces)
    ci = CiDetails(home, overview)
    ci_logs = BackgroundLogs(home, overview, ci)
    push = PushInbox(
        home,
        lambda: {"prs": overview.snapshot(), "issues": issues.snapshot(), "watcher": status(home)},
        login=lambda: overview.snapshot().get("login"),
    )
    with DashboardServer(
        home,
        port,
        allowed_hosts,
        overview=overview,
        workspaces=workspaces,
        ci=ci,
        ci_logs=ci_logs,
        issues=issues,
        upstream_tests=upstream_tests,
        workspace_overview=workspace_overview,
        push=push,
        sentry=sentry,
        cron=cron,
        attachments=attachments_dir,
        codex_updates=CodexUpdates(home),
        require_tailscale_user=require_tailscale_user,
        auth=auth,
    ) as server:
        # Pushes and the Home Screen badge can follow what needs the user.
        push.attention = server.attention.get
        ci_logs.start()
        cron.start()
        push.start()
        notifications.start()
        workspaces.start()
        url = f"http://127.0.0.1:{server.server_port}"
        print(
            f"Babysitter dashboard: {url}\nReading {home}\nPress Ctrl-C to close the dashboard; monitoring continues.",
            flush=True,
        )
        print(
            f"Dashboard auth: {auth.mode} ({auth_home}"
            + (f", OIDC {auth.oidc.config.issuer}" if auth.oidc else ", no OIDC")
            + (
                ", own routes allowed (test override)"
                if os.environ.get(dashboard_auth.ALLOW_OWN_ROUTES_ENV)
                else ""
            )
            + ")"
            if auth
            else f"Dashboard auth: off (no {auth_home / dashboard_auth.OIDC_FILE})"
            if auth_mode == "report"
            else "Dashboard auth: off",
            flush=True,
        )
        if unfenced := unfenced_workers(home):
            print(
                "Warning: worker machines without an IPv4 and an IPv6 tailnet_ip in "
                f"machines.json can use the whole dashboard through the tailnet: {', '.join(unfenced)}",
                file=sys.stderr,
                flush=True,
            )
        if open_browser:
            webbrowser.open(url)

        def stopped(signum, frame):
            raise KeyboardInterrupt()

        # launchd restarts with SIGTERM; cleanup below must still stop running cron jobs.
        for sig in (signal.SIGHUP, signal.SIGTERM):
            signal.signal(sig, stopped)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            cron.close()
            workspaces.close()
            notifications.close()
            push.close()
            ci_logs.close()
    # KeepAlive starts a LaunchAgent again when it exits unsuccessfully, and only then.
    return 1 if server.restart else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, default=Path.home() / ".local/state/babysit-pr")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open", action="store_true")
    parser.add_argument(
        "--allow-host",
        action="append",
        default=[],
        help="Exact Host header accepted through a trusted local proxy",
    )
    parser.add_argument(
        "--require-tailscale-user",
        action="store_true",
        help="Through tailscale serve, refuse all but /api/worker/ without Tailscale-User-Login",
    )
    dashboard_auth.add_arguments(parser)
    args = parser.parse_args()
    raise SystemExit(
        serve(
            args.home.expanduser().resolve(),
            args.port,
            args.open,
            args.allow_host,
            require_tailscale_user=args.require_tailscale_user,
            auth_mode=args.auth,
            auth_home=args.auth_home.expanduser(),
            oidc_config=args.oidc_config.expanduser() if args.oidc_config else None,
        )
    )
