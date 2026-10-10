"""One feed of everything waiting on the user, merged from every dashboard source.

Each source already knows its own signals: the watcher its blocked repairs and feedback,
the PR overview its CI and review state, the workspace scans their agents, the scheduler
and cron their failed runs. This module turns them into rows of one shape (an item, the
reasons it needs the user, and where to act) and merges rows about the same GitHub item,
so a pull request appears once however many sources mention it. It only reads the
snapshots each source already serves; it starts no GitHub or Git work of its own.
"""

import calendar
import json
import os
import threading
import time
from pathlib import Path

# Display order; a merged item takes the first group any of its reasons belongs to.
GROUPS = (
    ("answer", "Answer an agent"),
    ("review", "Review or merge"),
    ("unblock", "Unblock"),
    ("tidy", "Tidy up"),
    ("start", "Pick up"),
    ("parked", "Parked"),
)
ORDER = {key: index for index, (key, _) in enumerate(GROUPS)}
# Items without activity for this long are parked: listed last, folded, and not counted.
PARKED_AFTER = 14 * 86400
TRIAGE_FILE = "attention-triage.json"
TRIAGE_LIMIT = 500
# A set-aside entry whose item has not been listed for this long is forgotten.
TRIAGE_FORGET = 30 * 86400
PAGES = ("watcher", "prs", "issues", "workspaces", "scheduled", "cron")
ENDED_WATCHES = {"closed", "stopped"}
WAITING_AGENT = {"blocked"}
# herdr marks an agent `done` when it returned to its prompt and nobody has looked yet;
# `idle` is any agent sitting at its prompt, which needs nobody.
FINISHED_AGENT = {"done"}
# A paused watch does not repair CI, so its pull request's failures are the user's.
LIVE_WATCHES = {"watching", "running", "blocked", "awaiting_release", "handoff"}
CLOSED_LINKS = {"closed", "merged"}
FAILED_RUNS = {"error", "failed", "timed_out", "interrupted"}
# Failed launches and scheduled tasks older than this are history, not something to act on.
RECENT = 24 * 3600
# Feeds are shared by every open tab; one computation serves all polls in this window.
CACHE_SECONDS = 5.0


def reason(code, group, text):
    return {"code": code, "group": group, "text": text}


def epoch(value):
    """Seconds since the epoch from a number or an ISO-8601 GitHub timestamp, else None."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        try:
            return float(calendar.timegm(time.strptime(value, "%Y-%m-%dT%H:%M:%SZ")))
        except ValueError:
            return None
    return None


def clip(text, limit=160):
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def subject_key(url):
    """One key per GitHub item: owner and repository case-insensitive, no trailing slash."""
    url = str(url).rstrip("/")
    head, sep, rest = url.partition("://")
    if not sep or not head.startswith("http"):
        return url
    host, slash, path = rest.partition("/")
    parts = path.split("/", 2)
    if host.lower() != "github.com" or len(parts) < 2:
        return url
    parts[0], parts[1] = parts[0].lower(), parts[1].lower()
    return f"{head}://{host}{slash}{'/'.join(parts)}"


class Feed:
    """Collects rows from each source and merges them by GitHub URL."""

    def __init__(self, login=None, now=None):
        self.login = login
        self.now = time.time() if now is None else now
        self.items: dict[str, dict] = {}
        # herdr workspace -> item key, so a Workspaces row joins the item it belongs to even
        # when its own link lookup is stale or missing.
        self.by_workspace: dict[str, str] = {}

    def add(self, key, reasons, pages, **fields):
        """Record reasons for an item; a known key gains the reasons and pages."""
        reasons = [r for r in reasons if r]
        if not reasons:
            return
        key = subject_key(key)
        if fields.get("workspace_id"):
            key = self.by_workspace.setdefault(fields["workspace_id"], key)
        item = self.items.get(key)
        if item is None:
            item = self.items[key] = {
                "key": key,
                "kind": None,
                "repo": None,
                "number": None,
                "title": None,
                "url": None,
                "reasons": [],
                "pages": [],
                "since": None,
                "workspace_url": None,
                "workspace_id": None,
                "agent": None,
                "watch": None,
                "cron": None,
            }
        for field, value in fields.items():
            if value is not None and item.get(field) is None:
                item[field] = value
        for page in pages:
            if page not in item["pages"]:
                item["pages"].append(page)
        known = {r["code"] for r in item["reasons"]}
        for entry in reasons:
            if entry["code"] not in known:
                item["reasons"].append(entry)
                known.add(entry["code"])
        since = fields.get("since")
        if since is not None and (item["since"] is None or since > item["since"]):
            item["since"] = since

    def agent_reasons(self, statuses):
        """What a workspace's agents are waiting for; a working agent needs nothing."""
        statuses = [s for s in statuses if s]
        if any(s in WAITING_AGENT for s in statuses):
            return [reason("agent_blocked", "answer", "An agent is waiting for your answer")]
        if statuses and all(s in FINISHED_AGENT for s in statuses):
            return [reason("agent_finished", "review", "The agent finished its turn")]
        return []

    def workspace_fields(self, matches):
        """Workspace link and agent summary from a target's verified checkouts."""
        with_space = [m for m in matches if m.get("workspace_id")]
        if not with_space:
            return {}
        first = with_space[0]
        return {
            "workspace_url": first.get("url"),
            "workspace_id": first.get("workspace_id"),
            "agent": {"status": first.get("agent_status")},
        }

    def target_reasons(self, info):
        """Reasons from a PR, issue or watch's workspace state (`/api/workspaces`)."""
        if not info:
            return [], {}
        matches = info.get("matches") or []
        reasons = self.agent_reasons(
            [m.get("agent_status") for m in matches if m.get("workspace_id")]
        )
        op = info.get("operation") or {}
        if op.get("status") == "uncertain":
            reasons.append(
                reason("launch_uncertain", "unblock", clip(op.get("message") or "Check workspace"))
            )
        elif (
            op.get("status") == "failed" and self.now - (epoch(op.get("updated_at")) or 0) <= RECENT
        ):
            reasons.append(
                reason(
                    "launch_failed",
                    "unblock",
                    f"Workspace launch failed: {clip(op.get('message'))}",
                )
            )
        return reasons, self.workspace_fields(matches)

    # -- sources --

    def watcher(self, snapshot, targets):
        for job in (snapshot or {}).get("jobs") or []:
            status = job.get("status")
            reasons = []
            if status == "blocked":
                reasons.append(
                    reason("watch_blocked", "answer", f"Repair blocked: {clip(job.get('summary'))}")
                )
            elif status == "awaiting_release":
                # A new watch waits for the user to exit the CLI session it will resume.
                reasons.append(
                    reason("watch_release", "answer", "Exit your CLI session to release the watch")
                )
            # Items the user already approved are queued for the next repair, not waiting.
            pending = (job.get("pending_reviews") or 0) - (job.get("feedback_approved") or 0)
            if pending > 0 and status not in ENDED_WATCHES:
                text = (
                    "1 feedback item awaits approval"
                    if pending == 1
                    else f"{pending} feedback items await approval"
                )
                reasons.append(reason("feedback", "answer", text))
            if job.get("cleanup_ready"):
                outcome = (job.get("pr_outcome") or "finished").capitalize()
                reasons.append(
                    reason("cleanup", "tidy", f"{outcome}: its checkout is ready to clean up")
                )
            more, fields = self.target_reasons(targets.get(f"watch:{job.get('id')}"))
            if status not in ENDED_WATCHES:
                reasons.extend(more)
            title = job.get("title") or job.get("branch") or job.get("url")
            # The watch's `updated_at` moves with every poll; its activity is when it
            # started and when its status last changed.
            moments = [epoch(job.get("started_at"))] + [
                epoch(action.get("at")) for action in job.get("notification_actions") or []
            ]
            self.add(
                job.get("url") or f"watch:{job.get('id')}",
                reasons,
                ["watcher"],
                kind=job.get("kind") or "pr",
                repo=job.get("repo"),
                number=job.get("number"),
                title=title,
                url=job.get("url"),
                since=max((m for m in moments if m is not None), default=None),
                watch={"id": job.get("id"), "status": status},
                **fields,
            )

    def prs(self, snapshot, targets, watched):
        for pr in (snapshot or {}).get("prs") or []:
            roles = pr.get("roles") or []
            mine = "author" in roles
            reasons = []
            watch = watched.get(subject_key(pr.get("url")))
            if mine and pr.get("ci") in {"FAILURE", "ERROR"} and watch is None:
                # A live watch repairs failing CI itself; only unwatched failures need the user.
                reasons.append(
                    reason(
                        "ci_failing",
                        "unblock",
                        "CI failing" if pr["ci"] == "FAILURE" else "CI errored",
                    )
                )
            threads = pr.get("unresolved_threads") or 0
            if mine and pr.get("review_decision") == "CHANGES_REQUESTED":
                reasons.append(reason("changes_requested", "unblock", "Changes requested"))
            if mine and threads:
                plural = "" if threads == 1 else "s"
                reasons.append(
                    reason("threads", "unblock", f"{threads} unresolved review thread{plural}")
                )
            if (
                mine
                and not pr.get("draft")
                and pr.get("review_decision") == "APPROVED"
                and pr.get("ci") == "SUCCESS"
                and not threads
            ):
                reasons.append(
                    reason("approved", "review", "Approved with green CI: ready to merge")
                )
            if not mine and self.login and self.login in (pr.get("review_requests") or []):
                reasons.append(reason("review_requested", "review", "Your review is requested"))
            more, fields = self.target_reasons(targets.get(pr.get("id")))
            reasons.extend(more)
            self.add(
                pr.get("url") or f"pr:{pr.get('id')}",
                reasons,
                ["prs"],
                kind="pr",
                repo=pr.get("repo"),
                number=pr.get("number"),
                title=pr.get("title"),
                url=pr.get("url"),
                since=epoch(pr.get("updated_at")),
                **fields,
            )

    def issues(self, snapshot, targets, checkouts_known):
        for issue in (snapshot or {}).get("issues") or []:
            info = targets.get(issue.get("id")) or {}
            reasons, fields = self.target_reasons(info)
            open_prs = [
                pr
                for pr in issue.get("linked_prs") or []
                if str(pr.get("state") or "").lower() == "open"
            ]
            # Without the workspace inventory, "nothing started" would be a guess.
            if (
                checkouts_known
                and "assignee" in (issue.get("roles") or [])
                and not open_prs
                and not (info.get("matches") or [])
                and not (info.get("scheduled") or [])
            ):
                reasons.append(reason("assigned", "start", "Assigned to you; nothing started"))
            self.add(
                issue.get("url") or f"issue:{issue.get('id')}",
                reasons,
                ["issues"],
                kind="issue",
                repo=issue.get("repo"),
                number=issue.get("number"),
                title=issue.get("title"),
                url=issue.get("url"),
                since=epoch(issue.get("updated_at")),
                **fields,
            )

    def workspaces(self, snapshot):
        if not (snapshot or {}).get("enabled"):
            return
        for row in snapshot.get("workspaces") or []:
            agents = row.get("agents") or []
            reasons = self.agent_reasons([a.get("status") for a in agents])
            links = [
                link
                for link in row.get("links") or []
                if link.get("state") not in {"none", "unknown", "error", None}
            ]
            settled = bool(links) and all(link.get("state") in CLOSED_LINKS for link in links)
            if row.get("status") == "ready":
                reasons.append(
                    reason("workspace_ready", "tidy", "Its items are closed: ready to clean up")
                )
            elif row.get("status") == "blocked" and settled:
                blockers = "; ".join(b.get("text", "") for b in row.get("blockers") or [])
                reasons.append(
                    reason("workspace_blocked", "tidy", f"Its items are closed, but: {blockers}")
                )
            elif row.get("missing"):
                reasons.append(
                    reason("workspace_missing", "tidy", "Its checkout is gone: close the workspace")
                )
            link = next((link for link in links if link.get("url")), None)
            key = str(row.get("key") or "")
            self.add(
                link["url"]
                if link
                else key
                if key.startswith("workspace:")
                else f"workspace:{key}",
                reasons,
                ["workspaces"],
                kind=link["kind"] if link else "workspace",
                repo=row.get("repo"),
                number=link.get("number") if link else None,
                title=(link.get("title") if link else None) or row.get("name"),
                url=link["url"] if link else None,
                since=epoch(row.get("updated_at")),
                workspace_url=row.get("workspace_url"),
                workspace_id=(row.get("workspace_ids") or [None])[0],
                agent={"status": row.get("agent_status")} if row.get("agent_status") else None,
            )

    def scheduled(self, snapshot, now):
        for task in (snapshot or {}).get("tasks") or []:
            status = task.get("status")
            updated = epoch(task.get("updated_at")) or 0
            if status == "uncertain" or (
                status in {"missed", "failed"} and now - updated <= RECENT
            ):
                subject = task.get("subject") or {}
                self.add(
                    subject.get("url") or f"scheduled:{task.get('id')}",
                    # Per task, so several failed launches of one item each keep their message.
                    [reason(f"scheduled:{task.get('id')}", "unblock", clip(task.get("message")))],
                    ["scheduled"],
                    kind=subject.get("kind") or "scheduled",
                    repo=subject.get("repo"),
                    number=subject.get("number"),
                    title=subject.get("title") or "Scheduled task",
                    url=subject.get("url"),
                    since=updated or None,
                )

    def cron(self, snapshot):
        if not (snapshot or {}).get("enabled"):
            return
        for job in snapshot.get("jobs") or []:
            runs = job.get("runs") or []
            last = runs[0] if runs else None
            # A dismissed run was dealt with; the job's next run is judged afresh.
            if not last or last.get("dismissed_at"):
                continue
            reasons = []
            if last.get("status") == "attention":
                reasons.append(
                    reason(
                        "cron_attention",
                        "answer",
                        f"Its agent needs you: {clip(last.get('message'))}",
                    )
                )
            elif last.get("status") in FAILED_RUNS and job.get("enabled"):
                reasons.append(
                    reason(
                        "cron_failed", "unblock", f"Last run failed: {clip(last.get('message'))}"
                    )
                )
            self.add(
                f"cron:{job.get('id')}",
                reasons,
                ["cron"],
                kind="cron",
                title=job.get("name"),
                since=epoch(last.get("finished_at") or last.get("updated_at")),
                cron={"id": job.get("id"), "run": last.get("id")},
            )

    # -- output --

    def result(self, now, sources, triage=None):
        for item in self.items.values():
            item["group"] = min(item["reasons"], key=lambda r: ORDER[r["group"]])["group"]
            # Parked on age alone; an agent waiting for an answer is never parked.
            if (
                item["since"] is not None
                and now - item["since"] >= PARKED_AFTER
                and item["group"] != "answer"
            ):
                item["parked"] = True
                item["group"] = "parked"
            else:
                item["parked"] = False
        order = lambda item: (ORDER[item["group"]], -(item["since"] or 0), item["key"])  # noqa: E731
        items = sorted(self.items.values(), key=order)
        waiting = []
        if triage is not None:
            items, waiting = triage.split(self.login, items, now)
        active = [item for item in items if not item["parked"]]
        return {
            "time": now,
            "login": self.login,
            "items": items,
            "waiting": waiting,
            "groups": [
                {"key": key, "label": label, "count": sum(i["group"] == key for i in items)}
                for key, label in GROUPS
            ],
            "pages": {page: sum(page in i["pages"] for i in active) for page in PAGES},
            "agents_waiting": sum(
                any(r["code"] in {"agent_blocked", "cron_attention"} for r in i["reasons"])
                for i in active
            ),
            # A row's reasons are stable while nothing changes, so clients can skip rebuilds.
            "cached": False,
            "sources": sources,
        }


def source(name, provider):
    """One source's snapshot, or its failure: one broken source never empties the feed."""
    if provider is None:
        return None, {"available": False}
    try:
        value = provider()
    except Exception as exc:
        return None, {"available": True, "error": f"{name} unavailable: {exc}"}
    return value, {
        "available": True,
        "error": (value or {}).get("error"),
        "synced_at": (value or {}).get("synced_at"),
    }


def collect(
    watcher=None,
    prs=None,
    issues=None,
    workspaces=None,
    workspace_overview=None,
    scheduled=None,
    cron=None,
    now=None,
    triage=None,
):
    """The feed from snapshots or callables returning them (see `source`)."""
    now = time.time() if now is None else now
    sources = {}
    values = {}
    for name, provider in (
        ("watcher", watcher),
        ("prs", prs),
        ("issues", issues),
        ("workspaces", workspaces),
        ("workspace_overview", workspace_overview),
        ("scheduled", scheduled),
        ("cron", cron),
    ):
        if callable(provider):
            values[name], sources[name] = source(name, provider)
        else:
            values[name] = provider
            sources[name] = {"available": provider is not None}
    feed = Feed(login=(values["prs"] or {}).get("login"), now=now)
    described = values["workspaces"] or {}
    targets = {
        **(described.get("prs") or {}),
        **(described.get("issues") or {}),
        **(described.get("watches") or {}),
    }
    watched = {
        subject_key(job.get("url")): job
        for job in (values["watcher"] or {}).get("jobs") or []
        if job.get("url") and job.get("status") in LIVE_WATCHES
    }
    feed.watcher(values["watcher"], targets)
    feed.prs(values["prs"], targets, watched)
    feed.issues(
        values["issues"],
        targets,
        checkouts_known=values["workspaces"] is not None and not sources["workspaces"].get("error"),
    )
    feed.workspaces(values["workspace_overview"])
    feed.scheduled(values["scheduled"], now)
    feed.cron(values["cron"])
    return feed.result(now, sources, triage)


class Triage:
    """Items the user set aside until they change: "waiting on others".

    Kept per GitHub login in a private file in the state directory, so every device
    sees the same list. An entry records the item's activity time when it was set
    aside; the next activity on the item clears it, so nothing stays hidden for good.
    """

    def __init__(self, home):
        self.path = Path(home) / TRIAGE_FILE
        self.lock = threading.Lock()
        self.value: dict[str, dict[str, dict]] = {}
        self.error = None
        try:
            raw = json.loads(self.path.read_text())
            if not isinstance(raw, dict) or any(
                not isinstance(entries, dict)
                or any(not isinstance(entry, dict) for entry in entries.values())
                for entries in raw.values()
            ):
                raise ValueError("Invalid triage state")
            self.value = raw
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError):
            self.error = "The saved triage state needs repair"

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.fchmod(fd, 0o600)  # A leftover temporary file keeps no wider permissions.
        with os.fdopen(fd, "w") as stream:
            json.dump(self.value, stream)
        temp.replace(self.path)

    def set(self, request, feed):
        """Set an item aside (`wait`) or bring it back (`clear`); returns the login's entries."""
        if self.error:
            raise ValueError(self.error)
        login = feed.get("login")
        if not login or request.get("login") != login:
            raise ValueError("Wait for the overview to finish syncing")
        key = request.get("key")
        action = request.get("action")
        if not isinstance(key, str) or not key or action not in {"wait", "clear"}:
            raise ValueError("Supply an item and whether to wait for it")
        key = subject_key(key)
        with self.lock:
            entries = self.value.setdefault(login, {})
            if action == "wait":
                item = next((i for i in feed["items"] + feed["waiting"] if i["key"] == key), None)
                if item is None:
                    raise ValueError("This item is no longer listed; refresh the dashboard")
                if item["since"] is None:
                    raise ValueError("This item has no activity time to wait for")
                if len(entries) >= TRIAGE_LIMIT and key not in entries:
                    raise ValueError(f"At most {TRIAGE_LIMIT} items can wait at once")
                now = feed.get("time") or time.time()
                # Activity is a later timestamp or a reason the item did not have yet.
                entries[key] = {
                    "since": item["since"],
                    "codes": sorted(r["code"] for r in item["reasons"]),
                    "at": now,
                    "seen": now,
                }
            else:
                entries.pop(key, None)
            self.save()
            return {"login": login, "waiting": sorted(entries)}

    def split(self, login, items, now=None):
        """Listed items and the ones set aside.

        An entry is dropped once its item has a later activity time or a reason it did
        not have when set aside, and forgotten once its item has not been listed for
        TRIAGE_FORGET (it was merged, closed or cleaned up meanwhile).
        """
        now = time.time() if now is None else now
        with self.lock:
            entries = self.value.get(login or "", {})
            if not entries:
                return items, []
            listed, waiting = [], []
            changed = False
            for item in items:
                entry = entries.get(item["key"])
                if entry is None:
                    listed.append(item)
                    continue
                codes = {r["code"] for r in item["reasons"]}
                if (item["since"] or 0) > (entry.get("since") or 0) or not codes <= set(
                    entry.get("codes") or codes
                ):
                    del entries[item["key"]]
                    changed = True
                    listed.append(item)
                else:
                    if now - entry.get("seen", now) >= 3600:
                        entry["seen"] = now
                        changed = True
                    waiting.append({**item, "waiting_since": entry.get("at")})
            for key in [k for k, e in entries.items() if now - e.get("seen", now) >= TRIAGE_FORGET]:
                del entries[key]
                changed = True
            if changed:
                self.save()
            return listed, waiting


class Cached:
    """The feed recomputed at most every CACHE_SECONDS, shared by every poll and tab.

    Each poll would otherwise describe every workspace target again; `fresh=True`
    (the tab's Refresh button) computes at once.
    """

    def __init__(self, compute):
        self.compute = compute
        self.lock = threading.Lock()
        self.value: dict | None = None
        self.at = 0.0

    def get(self, fresh=False):
        with self.lock:
            now = time.monotonic()
            if self.value is None or fresh or now - self.at >= CACHE_SECONDS:
                self.value = self.compute()
                self.at = now
                return self.value
            return {**self.value, "cached": True}
