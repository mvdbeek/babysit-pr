#!/usr/bin/env python3
"""Loopback dashboard with watch cancellation for the shared babysit-pr watcher."""

import argparse
import json
import re
import signal
import sqlite3
import subprocess
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

import agent_messages
import attachments
import llm_usage
import workspace_agents
import workspace_viewer
from issue_overview import Overview as IssueOverview
from pr_ci import CiDetails
from pr_ci_logs import BackgroundLogs
from pr_overview import Overview
from pr_workspaces import COLLIE_URL, Workspaces

ASSETS = Path(__file__).resolve().parent.parent / "assets" / "dashboard"
LOGS = {"agent": "agent.log", "guardian": "guardian.log", "result": "result.json"}


def read_json(path):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return None


def read_jobs(home):
    path = home / "queue.sqlite"
    try:
        path.stat()
    except FileNotFoundError:
        return []
    # Never create a database or take ownership of monitoring just to display it.
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)
    try:
        return [json.loads(row[0]) for row in db.execute("SELECT data FROM jobs")]
    finally:
        db.close()


def present_job(job):
    snapshot = job.get("snapshot") or {}
    pr = snapshot.get("pr") or {}
    ci = snapshot.get("ci") or {}
    from pr_supervisor import approved_feedback, feedback_token

    feedback = job.get("pending_reviews") or []
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
        "check_details": snapshot.get("check_details") or [],
        "failed_jobs": snapshot.get("failed_jobs") or [],
        "updated_at": job.get("updated_at"),
        "next_poll": job.get("next_poll"),
        "last_poll": (job.get("watcher_state") or {}).get("last_snapshot_at"),
        "started_at": job.get("started_at"),
        "cwd": job.get("cwd"),
        "attempt": job.get("attempt"),
        "poll_errors": job.get("poll_errors", 0),
        "pause_after_run": job.get("pause_after_run", False),
        "stop_after_run": job.get("stop_after_run", False),
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
        result["jobs"] = sorted(
            (present_job(job) for job in read_jobs(home)),
            key=lambda job: job.get("updated_at") or 0,
            reverse=True,
        )
        heartbeat = read_json(home / "heartbeat.json") or {}
        daemon = read_json(home / "daemon.json") or {}
        age = max(0, now - heartbeat["time"]) if heartbeat.get("time") else None
        result["daemon"] = {
            "health": "healthy"
            if age is not None and age < 15
            else "stale"
            if age is not None
            else "offline",
            "heartbeat_age": age,
            "max_workers": daemon.get("max_workers"),
        }
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
        result["error"] = f"Cannot read watcher state: {exc}"
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


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True

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
    ) -> None:
        self.home = home
        self.attachments = attachments or home / "attachments"
        self.overview = overview
        self.issues = issues
        self.upstream_tests = upstream_tests
        self.sentry = sentry
        self.cron = cron
        self.workspace_overview = workspace_overview
        self.workspaces = workspaces
        self.ci = ci
        self.ci_logs = ci_logs
        self.push = push
        self.allowed_hosts = set(allowed_hosts)
        super().__init__(("127.0.0.1", port), Handler)


class Handler(BaseHTTPRequestHandler):
    server: DashboardServer

    def log_message(self, *args):
        pass

    def send_body(self, code, body, content_type):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
        )
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, code, value):
        self.send_body(code, json.dumps(value).encode(), "application/json; charset=utf-8")

    def do_POST(self):
        self.close_connection = True
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
            or action
            not in {
                "cancel",
                "feedback",
                "feedback-addressed",
                "workspace-action",
                "workspace-batch",
                "workspace-new",
                "schedule-cancel",
                "workspace-cleanup",
                "workspace-open",
                "workspace-message",
                "workspace-answer",
                "workspace-choose",
                "workspace-docker",
                "workspace-interrupt",
                "workspace-prompt-forget",
                "attachment-upload",
                "sentry-action",
                "cron-action",
                "push-subscribe",
                "push-unsubscribe",
                "push-read",
                "push-seen",
                "notification-silence",
                "effort-default",
            }
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
                        "workspace-cleanup",
                        "workspace-open",
                        "workspace-message",
                        "workspace-answer",
                        "workspace-prompt-forget",
                        "sentry-action",
                        "cron-action",
                    }
                    else 1024
                )
                or self.headers.get("Content-Type") != "application/json"
            ):
                raise ValueError("Expected a small JSON action request")
            self.connection.settimeout(5)
            request = json.loads(self.rfile.read(length))
            if not isinstance(request, dict):
                raise ValueError("Expected a JSON action request")
            # Attached files become paths in the text the agent receives.
            attachments.attach(self.server.attachments, action, request)
            if action == "notification-silence":
                if not self.server.push:
                    raise ValueError("Notification preferences are unavailable")
                self.send_json(200, self.server.push.silence(request))
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
                try:
                    self.send_json(200, agent_messages.send(request, self.server.home))
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
                    self.send_json(200, agent_messages.interrupt(request))
                except (OSError, subprocess.SubprocessError) as exc:
                    self.send_json(503, {"error": f"Check the agent in Collie: {exc}"})
                return
            if action == "workspace-answer":
                try:
                    self.send_json(200, agent_messages.answer(request))
                except (OSError, subprocess.SubprocessError) as exc:
                    self.send_json(503, {"error": f"Check the question in Collie: {exc}"})
                return
            if action == "workspace-choose":
                try:
                    self.send_json(200, agent_messages.choose(request))
                except (OSError, subprocess.SubprocessError) as exc:
                    self.send_json(503, {"error": f"Check the dialog in Collie: {exc}"})
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
            self.send_json(400, {"error": str(exc)})
        except (OSError, sqlite3.Error, subprocess.SubprocessError) as exc:
            self.send_json(503, {"error": f"Cannot update watch: {exc}"})

    def upload(self, length):
        """Store a file sent as the raw request body, named by its X-Filename header."""
        if (
            not 0 < length <= attachments.MAX_FILE
            or self.headers.get("Content-Type") != "application/octet-stream"
        ):
            raise ValueError(f"Attach a file of at most {attachments.MAX_FILE // (1024 * 1024)} MB")
        name = unquote(self.headers.get("X-Filename") or "file")
        # A phone over the tailnet can be slow; each read still has a limit.
        self.connection.settimeout(30)
        try:
            data = self.rfile.read(length)
        except OSError:
            data = b""
        if len(data) != length:
            raise ValueError("The upload was cut short; attach the file again")
        self.send_json(200, attachments.save(self.server.attachments, name, data))

    def do_GET(self):
        port = self.server.server_port
        if (
            self.headers.get("Host")
            not in {f"127.0.0.1:{port}", f"localhost:{port}"} | self.server.allowed_hosts
        ):
            self.send_json(403, {"error": "Use a configured dashboard URL"})
            return
        if (
            self.headers.get("Sec-Fetch-Site") == "cross-site"
            and self.headers.get("Sec-Fetch-Mode") != "navigate"
        ):
            self.send_json(403, {"error": "Cross-site access is disabled"})
            return
        route = urlsplit(self.path)
        try:
            if route.path == "/api/status":
                self.send_json(200, status(self.server.home))
            elif route.path == "/api/llm-usage":
                refresh = parse_qs(route.query).get("refresh") == ["1"]
                llm_usage.request(self.server.home, refresh)
                heartbeat = read_json(self.server.home / "heartbeat.json") or {}
                reader = llm_usage.reader_state(self.server.home, heartbeat)
                self.send_json(200, llm_usage.present(self.server.home, reader=reader))
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
                if set(query) != {"workspace"} or len(query["workspace"]) != 1:
                    raise ValueError("Supply a workspace")
                workspace_id = query["workspace"][0]
                self.send_json(
                    200,
                    {
                        "agents": agent_messages.docker_status(workspace_id),
                        "sessions": agent_messages.sessions(workspace_id, self.server.home),
                        # A draft names its checkout, so a reused workspace ID cannot
                        # deliver another checkout's comments.
                        "path": workspace_viewer.workspace_checkout(workspace_id),
                        "url": f"{COLLIE_URL}/space/{quote(workspace_id, safe='')}",
                    },
                )
            elif route.path in {"/api/workspace-diff", "/api/workspace-transcript"}:
                query = parse_qs(route.query)
                if any(len(values) != 1 for values in query.values()):
                    raise ValueError("Supply each parameter once")
                single = {name: values[0] for name, values in query.items()}
                # A herdr workspace (what Collie opens) or a row of the Workspaces tab.
                if ("workspace" in single) == ("key" in single):
                    raise ValueError("Supply a workspace or a workspace key")
                if "key" in single:
                    if not self.server.workspace_overview:
                        raise ValueError("The workspace experiment is disabled")
                    path = self.server.workspace_overview.checkout(single.pop("key"))
                # Read-only views own their failure boundary, like the inventory.
                try:
                    if "workspace" in single:
                        path = workspace_viewer.workspace_checkout(single.pop("workspace"))
                    if route.path == "/api/workspace-diff":
                        if set(single) - {"scope", "base"}:
                            raise ValueError("Unknown diff parameter")
                        value = workspace_viewer.diff(
                            path, single.get("scope", "branch"), single.get("base")
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
                        value = workspace_viewer.transcript(
                            path, single.get("session"), **positions
                        )
                except ValueError:
                    raise
                except Exception as exc:
                    value = {"error": f"Workspace view unavailable: {exc}"}
                self.send_json(200, value)
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
            elif route.path == "/api/workspace-repos":
                everything = parse_qs(route.query).get("all", [""])[0] == "1"
                self.send_json(
                    200,
                    self.server.workspaces.local_repositories(everything)
                    if self.server.workspaces
                    else {"repos": [], "idle": 0},
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
                    "/collie.js": ("collie.js", "text/javascript"),
                    "/notifications.js": ("notifications.js", "text/javascript"),
                    "/usage.js": ("usage.js", "text/javascript"),
                    "/push.js": ("push.js", "text/javascript"),
                    "/sw.js": ("sw.js", "text/javascript"),
                    "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
                    "/icon-192.png": ("icon-192.png", "image/png"),
                    "/icon-512.png": ("icon-512.png", "image/png"),
                    "/upstream-tests.js": ("upstream-tests.js", "text/javascript"),
                    "/upstream-tests.css": ("upstream-tests.css", "text/css"),
                    "/workspaces.js": ("workspaces.js", "text/javascript"),
                    "/workspaces.css": ("workspaces.css", "text/css"),
                    "/viewer.js": ("viewer.js", "text/javascript"),
                    "/attachments.js": ("attachments.js", "text/javascript"),
                    "/viewer.css": ("viewer.css", "text/css"),
                    "/sentry.js": ("sentry.js", "text/javascript"),
                    "/sentry.css": ("sentry.css", "text/css"),
                    "/cron.js": ("cron.js", "text/javascript"),
                    "/cron.css": ("cron.css", "text/css"),
                    "/style.css": ("style.css", "text/css"),
                }
                if route.path not in files:
                    self.send_json(404, {"error": "Not found"})
                    return
                filename, mime = files[route.path]
                content_type = mime + ("; charset=utf-8" if mime.startswith("text/") else "")
                self.send_body(200, (ASSETS / filename).read_bytes(), content_type)
        except ValueError as exc:
            self.send_json(400, {"error": str(exc)})
        except (OSError, sqlite3.Error, subprocess.SubprocessError) as exc:
            self.send_json(503, {"error": f"Cannot read watcher data: {exc}"})


def serve(home, port=8765, open_browser=False, allowed_hosts=(), attachments_dir=None):
    from cron_jobs import CronJobs
    from dashboard_push import PushInbox
    from sentry_issues import SentryIssues
    from upstream_tests import UpstreamTests
    from workspace_overview import WorkspaceOverview

    upstream_tests = UpstreamTests(home)
    workspace_overview = WorkspaceOverview(home, jobs=lambda: read_jobs(home))
    overview = Overview(home)
    issues = IssueOverview(home)
    sentry = SentryIssues(home)
    workspaces = Workspaces(
        home,
        overview,
        lambda: read_jobs(home),
        issues=issues,
        sentry=sentry if sentry.enabled else None,
    )
    cron = CronJobs(home, workspaces=workspaces)
    ci = CiDetails(home, overview)
    ci_logs = BackgroundLogs(home, overview, ci)
    push = PushInbox(
        home,
        lambda: {"prs": overview.snapshot(), "issues": issues.snapshot(), "watcher": status(home)},
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
    ) as server:
        ci_logs.start()
        cron.start()
        push.start()
        workspaces.start()
        url = f"http://127.0.0.1:{server.server_port}"
        print(
            f"Babysitter dashboard: {url}\nReading {home}\nPress Ctrl-C to close the dashboard; monitoring continues.",
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
            push.close()
            ci_logs.close()


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
    args = parser.parse_args()
    serve(args.home.expanduser().resolve(), args.port, args.open, args.allow_host)
