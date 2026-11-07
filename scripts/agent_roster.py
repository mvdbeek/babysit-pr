"""Every agent herdr runs, on this machine and each enabled worker, for the Agents tab.

Read-only: nothing here types into a pane or starts anything. A request reads the cache;
a machine's entry older than its time to live is refreshed by its own daemon thread
(``herdr agent list`` and ``herdr workspace list`` there), so a hung worker delays
nobody and an unwatched tab costs no herdr calls. This machine's entry lives
``LOCAL_TTL`` seconds, a worker's ``WORKER_TTL``.

Each agent says where it runs (machine, workspace, checkout, pane), what it is doing,
since when as far as this process has seen, and who started it: a babysit watch that
owns its session, a cron run that launched it in its pane, or otherwise the user.
"""

import sqlite3
import subprocess
import threading
import time
from pathlib import Path

import machines
from pr_workspaces import PANE, collie_url, herdr

LOCAL_TTL = 3
WORKER_TTL = 10
TIMEOUT = 8
STATUSES = {"idle", "working", "blocked", "done", "unknown"}
# Watches still in these states resume their session themselves.
ENDED_WATCHES = {"closed", "stopped"}
# Most agents one machine may list; herdr has no reason to report more.
MAX_AGENTS = 200


def text(value, limit=300):
    """A display string from herdr, or None; workers' fields are not trusted further."""
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()[:limit]


def listed(machine):
    """One machine's agents and workspaces from its herdr, malformed entries dropped."""
    agents = herdr("agent", "list", machine=machine, timeout=TIMEOUT).get("agents")
    spaces = herdr("workspace", "list", machine=machine, timeout=TIMEOUT).get("workspaces")
    if not isinstance(agents, list) or not isinstance(spaces, list):
        raise ValueError("herdr listed malformed agents or workspaces")
    workspaces = {}
    for space in spaces:
        if isinstance(space, dict) and isinstance(space.get("workspace_id"), str):
            worktree = space["worktree"] if isinstance(space.get("worktree"), dict) else {}
            workspaces[space["workspace_id"]] = {
                "label": text(space.get("label")),
                "checkout": text(worktree.get("checkout_path"), 4096),
                "repo": text(worktree.get("repo_name")),
            }
    found = []
    for agent in agents[:MAX_AGENTS]:
        if not (
            isinstance(agent, dict)
            and isinstance(agent.get("pane_id"), str)
            and PANE.fullmatch(agent["pane_id"])
            and isinstance(agent.get("workspace_id"), str)
        ):
            continue
        session = agent.get("agent_session")
        found.append(
            {
                "pane": agent["pane_id"],
                "workspace_id": agent["workspace_id"],
                "agent": text(agent.get("agent"), 40),
                "status": agent.get("agent_status")
                if agent.get("agent_status") in STATUSES
                else "unknown",
                "title": text(agent.get("terminal_title_stripped")),
                "agent_name": text(agent.get("name")),
                "cwd": text(agent.get("cwd"), 4096),
                "session": text(session.get("value"), 200) if isinstance(session, dict) else None,
            }
        )
    return found, workspaces


class Roster:
    """Cached agent listings per machine, joined with who started each agent.

    ``jobs`` returns the watcher's queue, ``cron`` the cron snapshot and ``overview`` the
    Workspaces inventory (or None); each is read as it is, never refreshed from here.
    """

    def __init__(self, home, jobs=None, cron=None, overview=None, clock=time.time):
        self.home = Path(home)
        self.jobs = jobs
        self.cron = cron
        self.overview = overview
        self.clock = clock
        self.lock = threading.Lock()
        self.entries: dict[str | None, dict] = {}
        self.refreshing: set[str | None] = set()
        # When each pane was first seen in its current status and session.
        self.seen: dict[tuple, tuple] = {}

    def snapshot(self):
        try:
            registry = machines.load(self.home)
        except (OSError, ValueError):
            registry = {}  # A malformed registry lists this machine's agents alone.
        workers = [m for m in registry.values() if m.enabled]
        now = self.clock()
        with self.lock:
            for machine in [None, *workers]:
                key = machine.id if machine else None
                entry = self.entries.get(key)
                ttl = WORKER_TTL if machine else LOCAL_TTL
                stale = entry is None or now - entry["attempted_at"] >= ttl
                if stale and key not in self.refreshing:
                    self.refreshing.add(key)
                    threading.Thread(target=self.refresh, args=(machine,), daemon=True).start()
            entries = dict(self.entries)
        origins = self.origins()
        links = self.links()
        agents = []
        listed_machines = []
        for machine in [None, *workers]:
            key = machine.id if machine else None
            entry = entries.get(key) or {}
            listed_machines.append(
                {
                    "id": key,
                    "label": machine.label if machine else "This machine",
                    "local": machine is None,
                    "pending": "synced_at" not in entry,
                    "synced_at": entry.get("synced_at"),
                    "error": entry.get("error"),
                }
            )
            for agent in entry.get("agents", []):
                agents.append(self.row(machine, agent, entry["workspaces"], origins, links))
        return {"agents": agents, "machines": listed_machines, "now": now}

    def refresh(self, machine):
        key = machine.id if machine else None
        attempted = self.clock()
        # Kept if something unforeseen escapes.
        update: dict = {"error": "Agent listing failed"}
        try:
            agents, workspaces = listed(machine)
            update = {
                "agents": agents,
                "workspaces": workspaces,
                "error": None,
                "synced_at": self.clock(),
            }
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
            update = {"error": str(exc)[-300:] or type(exc).__name__}
        finally:
            # Always released, or this machine would never be listed again. A failure keeps
            # the agents last seen, marked with the error.
            with self.lock:
                entry = {**self.entries.get(key, {}), **update, "attempted_at": attempted}
                self.entries[key] = entry
                self.refreshing.discard(key)
                if "agents" in update:
                    self.note(key, update["agents"], attempted)

    def note(self, key, agents, now):
        """Remember when each pane entered its status; panes gone are forgotten."""
        current = {}
        for agent in agents:
            pane = (key, agent["pane"])
            state = (agent["status"], agent["session"])
            before = self.seen.get(pane)
            current[pane] = before if before and before[0] == state else (state, now)
        self.seen = {k: v for k, v in self.seen.items() if k[0] != key} | current

    def origins(self):
        """Who started agents: watches by session, cron runs by machine and pane."""
        watches = {}
        try:
            for job in (self.jobs() if self.jobs else None) or []:
                if job.get("session_id") and job.get("status") not in ENDED_WATCHES:
                    watches[job["session_id"]] = job
        except (OSError, ValueError, sqlite3.Error):
            pass  # The watcher's queue unread: its agents read as the user's.
        runs = {}
        try:
            snapshot = (self.cron() if self.cron else None) or {}
        except (OSError, ValueError, sqlite3.Error):
            snapshot = {}
        for job in snapshot.get("jobs") or []:
            for run in job.get("runs") or []:
                launched = run.get("agent_run") or {}
                if run.get("status") in {"running", "starting"} and launched.get("pane"):
                    runs[(launched.get("machine"), launched["pane"])] = job
        return watches, runs

    def links(self):
        """The pull request or issue each workspace serves, by qualified workspace ID."""
        if not self.overview:
            return {}
        try:
            rows = self.overview.snapshot(start=False).get("workspaces") or []
        except Exception:  # The experiment's failures stay inside it.
            return {}
        found = {}
        for row in rows:
            for workspace in row.get("workspace_ids") or []:
                found[workspace] = {"links": row.get("links") or [], "branch": row.get("branch")}
        return found

    def row(self, machine, agent, workspaces, origins, links):
        key = machine.id if machine else None
        space = workspaces.get(agent["workspace_id"]) or {}
        workspace = machines.qualify(key, agent["workspace_id"])
        watches, runs = origins
        watch = watches.get(agent["session"]) if agent["session"] else None
        run = runs.get((key, agent["pane"]))
        if watch and (watch.get("machine") or None) == key:
            origin = {
                "origin": "watch",
                "origin_label": f"Watch {watch.get('id')}",
                "origin_url": watch.get("url"),
            }
        elif run:
            origin = {"origin": "cron", "origin_label": f"Cron: {run.get('name')}"}
        else:
            origin = {"origin": "you", "origin_label": None}
        seen = self.seen.get((key, agent["pane"]))
        joined = links.get(workspace) or {}
        checkout = space.get("checkout")
        return {
            **agent,
            **origin,
            "id": f"{key}/{agent['pane']}" if key else agent["pane"],
            "machine": key,
            "machine_label": machine.label if machine else None,
            "workspace": workspace,
            "workspace_label": space.get("label"),
            "checkout": checkout,
            "repo": space.get("repo"),
            "name": Path(checkout).name if checkout else space.get("label") or workspace,
            "branch": joined.get("branch"),
            "links": joined.get("links") or [],
            "url": collie_url(machine, agent["workspace_id"]),
            "since": seen[1] if seen else None,
        }
