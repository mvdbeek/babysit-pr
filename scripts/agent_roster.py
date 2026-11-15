"""Every agent herdr runs, on this machine and each enabled worker, for the Agents tab.

Read-only: nothing here types into a pane or starts anything. A request reads the cache;
a machine's entry older than its time to live is refreshed by its own daemon thread
(``herdr agent list`` and ``herdr workspace list`` there), so a hung worker delays
nobody and an unwatched tab costs no herdr calls. This machine's entry lives
``LOCAL_TTL`` seconds, a worker's ``WORKER_TTL``.

Each agent says where it runs (machine, workspace, checkout, pane), what it is doing,
since when as far as this process has seen, and who started it: a babysit watch that
owns its session, a cron run that launched it in its pane, or otherwise the user.

It also says when it was last active: when its session last wrote its transcript, which
no restart of the dashboard or of herdr changes. The transcripts' times are read again
only when herdr reports a change in a machine's agents, or after ``ACTIVITY_TTL``.
"""

import os
import sqlite3
import subprocess
import threading
import time
from pathlib import Path

import claude_accounts
import machines
import remote_probe
from pr_workspaces import PANE, collie_url, herdr

LOCAL_TTL = 3
WORKER_TTL = 10
TIMEOUT = 8
# A session that writes while herdr reports no change (a command that starts no turn) is
# read again this often; an agent's turn ending is a change, and is read at once.
ACTIVITY_TTL = 60
# Agents whose transcripts are found by their session ID.
TRANSCRIPTS = {"claude", "codex"}
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
                # Counts the agent's changes of state, also those between two listings.
                "changes": agent["state_change_seq"]
                if type(agent.get("state_change_seq")) is int
                else None,
            }
        )
    return found, workspaces


def session_activity(home, machine, agents):
    """When each listed agent's session last wrote its transcript, by session ID."""
    wanted = {}
    for agent in agents:
        session, cwd = agent["session"], agent["cwd"]
        if agent["agent"] in TRANSCRIPTS and session and remote_probe.SESSION.match(session):
            known = cwd if cwd and cwd.startswith("/") and "\0" not in cwd else None
            wanted[session] = [agent["agent"], session, known]
    if not wanted:
        return {}
    if machine:
        return remote_probe.call(home, machine, "activity", TIMEOUT, sessions=list(wanted.values()))
    # The stores the viewer reads this machine's transcripts from.
    codex = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "sessions"
    projects = [config / "projects" for config in claude_accounts.homes()]
    return remote_probe.session_updates(wanted.values(), projects, codex)


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
        # Each pane's status and session, since when, and whether this process saw it get
        # there (rather than finding it so on a machine's first listing).
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
                agents.append(self.row(machine, agent, entry, origins, links))
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
            update.update(self.activity(machine, agents, attempted))
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
            update = {"error": str(exc)[-300:] or type(exc).__name__}
        finally:
            # Always released, or this machine would never be listed again. A failure keeps
            # the agents last seen, marked with the error.
            with self.lock:
                before = self.entries.get(key, {})
                entry = {**before, **update, "attempted_at": attempted}
                self.entries[key] = entry
                self.refreshing.discard(key)
                if "agents" in update:
                    self.note(key, entry, attempted, first="agents" not in before)

    def activity(self, machine, agents, now):
        """The transcripts' times, read again when the listing changed or they are old."""
        with self.lock:
            entry = self.entries.get(machine.id if machine else None, {})
        listing = sorted(
            (a["pane"], a["status"], a["session"] or "", a["changes"] or 0) for a in agents
        )
        # A worker that could not say is not asked again with every change either.
        settled = entry.get("activity_failed") or entry.get("activity_of") == listing
        if settled and now - entry["activity_at"] < ACTIVITY_TTL:
            return {}
        read = {"activity_of": listing, "activity_at": now}
        try:
            found = session_activity(self.home, machine, agents)
        except (OSError, ValueError, subprocess.SubprocessError):
            # The agents are listed all the same, with the times last read.
            return {**read, "activity_failed": True}
        # Only the sessions listed, and none active later than now: a worker's clock or
        # answer puts no agent above those really active.
        asked = {agent["session"] for agent in agents}
        times = {s: min(when, now) for s, when in found.items() if s in asked}
        return {**read, "activity_failed": False, "activity": times}

    def note(self, key, entry, now, first):
        """Remember when each pane entered its status; panes gone are forgotten."""
        current = {}
        for agent in entry["agents"]:
            pane = (key, agent["pane"])
            state = (agent["status"], agent["session"])
            before = self.seen.get(pane)
            if before and before[0] == state:
                current[pane] = before
            elif first:
                # Found so: it got there no later than its session last wrote.
                wrote = entry.get("activity", {}).get(agent["session"])
                current[pane] = (state, min(now, wrote or now), False)
            else:
                current[pane] = (state, now, True)
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
                found[workspace] = {
                    "links": row.get("links") or [],
                    "branch": row.get("branch"),
                    "updated_at": row.get("updated_at"),
                }
        return found

    def row(self, machine, agent, entry, origins, links):
        key = machine.id if machine else None
        space = entry["workspaces"].get(agent["workspace_id"]) or {}
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
        # An agent with no transcript found: its checkout's last change as the inventory
        # has it, or its own last change of state if this process saw that.
        fallback = [joined.get("updated_at"), seen[1] if seen and seen[2] else None]
        activity = entry.get("activity", {}).get(agent["session"]) or max(
            (when for when in fallback if when), default=None
        )
        return {
            **{k: v for k, v in agent.items() if k != "changes"},
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
            "activity": activity,
        }
