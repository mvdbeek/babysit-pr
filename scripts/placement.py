"""Which machine a launch runs on: a pure function of the registry, cached loads and history.

``machines`` are the registry's workers by id. This machine is ``LOCAL``; without a
``LOCAL`` entry it is assumed to meet every requirement, which is how every launch ran
before workers existed. Past its capacity (``loads[LOCAL]["max_agents"]``, from the
dashboard's settings) Auto prefers a worker, and comes back here when none can take the
launch. ``loads`` are ``MachineLoads`` entries by id, ``history`` a stickiness level by
id (2: a prior workspace for the same target, 1: a clone of its repository is there),
and ``reservations`` the launches placed on each machine that herdr does not list yet.
A launch that uses Docker goes to the registry's ``docker`` ``prefer`` machine ahead of
a clone here, when Auto may place launches there and it can take this one.
A worker's load carries its ``clones`` from its placement state, None while unknown.
"""

import re
import time
from typing import NamedTuple

LOCAL = "local"
# Seconds a machine's cached load stays fresh, and how long it may stay unreachable
# before placement stops offering it.
LOAD_TTL = 15
# Seconds a worker's placement state stays fresh, and after which it counts as unknown.
STATE_TTL = 300
STATE_STALE = 600
# Agents this machine takes before Auto prefers a worker, unless the settings say otherwise.
LOCAL_SLOTS = 20
# What a launch starts work on, and what of it a worker can start; anything else stays
# here. Cron jobs are new tasks too.
KINDS = {"pr", "issue", "sentry", "scratch", "cron"}
WORKER_KINDS = {"pr", "scratch", "cron"}
# The capability label a sandboxed agent needs, by operating system.
SANDBOX = {"darwin": "safehouse", "linux": "bwrap"}


class Placement(NamedTuple):
    machine: str | None  # Registry id; None is this machine.
    reason: str
    excluded: dict[str, str]  # Why each machine left out could not take the launch.


class Exclusion(NamedTuple):
    why: str
    # Full or not yet checked: an explicit choice may still take it; Auto never does.
    soft: bool
    # Full or not yet checked: a launch that waits for room may take it later.
    room: bool = False


class NoRoom(ValueError):
    """No machine a launch may use has room for it now; it can wait for some.

    ``machine`` is the registry id it waits for, None for this machine or under Auto.
    """

    def __init__(self, message, machine=None):
        super().__init__(message)
        self.machine = machine


def request(fields, repos, docker=None):
    """A placement request from launch ``fields`` plus the registry's per-repo rules and
    its ``docker`` preference.

    ``fields``: ``kind`` (``pr``, ``issue``, ``sentry``, ``scratch`` or ``cron``),
    ``repo``, ``agent``, ``claude_account``, ``docker``, ``sandboxed`` (default true),
    ``also`` (repositories cloned beside it), ``override`` (a machine id, ``LOCAL`` or
    ``"auto"``) and ``pin`` (a machine an existing workspace is on).
    """
    rules = {slug.lower(): rule for slug, rule in repos.items()}
    rule = rules.get((fields.get("repo") or "").lower(), {})
    return {
        **fields,
        "requires": list(rule.get("requires", [])),
        "prefer": rule.get("prefer"),
        "docker_prefer": (docker or {}).get("prefer") if fields.get("docker") else None,
    }


def requirements(req, machine):
    """Capability labels ``machine`` needs for ``req``, in a stable order."""
    needed = []
    if req.get("docker"):
        needed.append("docker")
    if req.get("sandboxed", True):
        needed.append(SANDBOX[machine.os])
    if req.get("agent") == "claude" and req.get("claude_account") not in (None, "", "default"):
        needed.append(f"claude-account:{req['claude_account']}")
    return list(dict.fromkeys([*needed, *req.get("requires", [])]))


def slots(machine_id, machines, loads):
    machine = machines.get(machine_id)
    if machine:
        return machine.max_agents
    return (loads.get(LOCAL) or {}).get("max_agents", LOCAL_SLOTS)


def busy(machine_id, loads, reservations):
    return (loads.get(machine_id) or {}).get("agents", 0) + reservations.get(machine_id, 0)


def age(seconds):
    return f"{round(seconds)}s" if seconds < 120 else f"{round(seconds / 60)}m"


def exclusions(req, machines, loads, reservations, now):
    """Why each machine cannot take ``req``; machines that can are left out."""
    excluded = {}
    for machine_id, machine in machines.items():
        load = loads.get(machine_id) or {}
        since = load.get("unreachable_since")
        # A refresh that has not finished for two TTLs leaves counts and unreachability
        # that may no longer hold; the machine is checked again instead.
        stale = now - load.get("attempted_at", now) > 2 * LOAD_TTL
        lacking = [label for label in requirements(req, machine) if label not in machine.labels]
        used = busy(machine_id, loads, reservations)
        job_clone = (
            cloned(load, req["repo"]) if req.get("kind") == "cron" and req.get("repo") else True
        )
        if not machine.enabled:
            excluded[machine_id] = Exclusion("disabled", False)
        elif since is not None and not stale and now - since > LOAD_TTL:
            excluded[machine_id] = Exclusion(f"unreachable {age(now - since)}", False)
        elif lacking:
            excluded[machine_id] = Exclusion(f"lacks {', '.join(lacking)}", False)
        elif machine_id != LOCAL and req.get("kind", "scratch") not in WORKER_KINDS:
            excluded[machine_id] = Exclusion("only takes pull request work and new tasks", False)
        elif machine_id != LOCAL and req.get("kind") == "cron" and not req.get("sandboxed", True):
            excluded[machine_id] = Exclusion("runs jobs only in their sandbox", False)
        elif machine_id != LOCAL and job_clone is False:
            # Launches clone a missing repository; a cron job needs its clone already.
            # Listed up to minutes ago, so a person may still choose it by name.
            excluded[machine_id] = Exclusion(f"no clone of {req['repo']}", True)
        elif machine_id != LOCAL and (stale or load.get("pending") or job_clone is None):
            # No load (or, for a cron job, clone listing) has come back yet.
            excluded[machine_id] = Exclusion("checking…", True, True)
        elif machine_id != LOCAL and used >= machine.max_agents:
            excluded[machine_id] = Exclusion(f"full ({used}/{machine.max_agents})", True, True)
        elif machine_id != LOCAL and req.get("also"):
            # The other clones are picked from this machine's; a worker has them only
            # where someone cloned them, so only a person sends the task there.
            excluded[machine_id] = Exclusion("takes other repositories only when chosen", True)
    used, total = busy(LOCAL, loads, reservations), slots(LOCAL, machines, loads)
    if LOCAL not in excluded and used >= total:
        excluded[LOCAL] = Exclusion(f"full ({used}/{total})", True, True)
    return excluded


def cloned(load, repo):
    """Whether a worker has ``repo``'s clone where its launches use it, ``<src>/<name>``;
    None while its clones are unknown."""
    clones = load.get("clones")
    if clones is None or not repo:
        return None
    name = repo.split("/")[-1]
    return any(c["name"] == name and repo.lower() in c["remotes"] for c in clones)


def matched(pr, loads):
    """Workers with a checkout of ``pr``'s head branch at its head commit, in the clone a
    launch there uses, as ``verified_head`` would find it there, from the last placement
    state."""
    head_repo = (pr.get("head_repo") or "").lower()
    branch = pr.get("head_branch")
    sha = (pr.get("head_sha") or "").lower()
    if not head_repo or not branch or not re.fullmatch(r"[0-9a-f]{40}", sha):
        return []
    name, repo = pr["repo"].split("/")[-1], pr["repo"].lower()
    found = []
    for machine_id, load in sorted(loads.items()):
        if machine_id == LOCAL:
            continue
        clones = [c for c in load.get("clones") or () if c["name"] == name and repo in c["remotes"]]
        if any(
            (
                item["upstream"] == [head_repo, branch]
                or (item["branch"] == branch and head_repo in clone["remotes"])
            )
            and item["sha"] == sha
            for clone in clones
            for item in clone["worktrees"]
        ):
            found.append(machine_id)
    return found


def history(repo, loads, here, matches=()):
    """Stickiness by machine: ``here`` for this one, and for each worker 2 when it is in
    ``matches``, 1 when it has the clone a launch there uses, else 0."""
    scores = {LOCAL: here}
    for machine_id, load in loads.items():
        if machine_id != LOCAL:
            scores[machine_id] = 2 if machine_id in matches else 1 if cloned(load, repo) else 0
    return scores


def label(machine_id, machines):
    machine = machines.get(machine_id)
    return machine.label if machine else "This machine"


def free(machine_id, machines, loads, reservations):
    # Until a first scan answers, its agents are unknown: "20/20 free" would mislead.
    if (loads.get(machine_id) or {}).get("pending"):
        return "still counting agents"
    total = slots(machine_id, machines, loads)
    return f"{max(0, total - busy(machine_id, loads, reservations))}/{total} free"


def choose(req, machines, loads, history, reservations, *, now=None, local=True, wait=False):
    """The machine ``req`` runs on, or ValueError naming why each machine could not take it.

    An explicit choice (``override``) wins, then a pin, and the two must agree; Auto
    ranks what is left by a prior workspace, the machine Docker prefers, a clone, the
    repository's preferred machine, load, then this machine first and label, so equal
    candidates always resolve the same way.
    Auto only considers workers registered with ``auto``, and this machine when it is
    full only if nothing else can take the launch. Without ``local`` this machine is
    never chosen, by name or by Auto. With ``wait`` no machine without room (full, or
    not checked yet) is chosen, by name or by Auto: NoRoom says why, so the launch can
    wait for one.
    """
    now = time.time() if now is None else now
    found = exclusions(req, machines, loads, reservations, now)
    excluded = {machine_id: e.why for machine_id, e in found.items()}

    def known(machine_id):
        if machine_id != LOCAL and machine_id not in machines:
            raise ValueError(f"Unknown machine {machine_id}")

    def named(machine_id, reason):
        known(machine_id)
        exclusion = found.get(machine_id)
        if exclusion and exclusion.room and wait:
            raise NoRoom(
                f"{label(machine_id, machines)}: {exclusion.why}",
                None if machine_id == LOCAL else machine_id,
            )
        if exclusion and not exclusion.soft:
            raise ValueError(f"{label(machine_id, machines)}: {exclusion.why}")
        if exclusion:
            reason = f"{reason}; {exclusion.why}"
        return Placement(None if machine_id == LOCAL else machine_id, reason, excluded)

    override = req.get("override")
    pin = req.get("pin")
    if not local and LOCAL in (override, pin):
        raise ValueError(f"{label(LOCAL, machines)} cannot take this launch")
    if override not in (None, "auto"):
        known(override)
        if pin is not None and override != pin:
            raise ValueError(
                f"The existing workspace is on {label(pin, machines)}; choose it or Auto"
            )
        return named(override, "chosen")
    if pin is not None:
        return named(pin, "existing workspace")
    offered = {*([LOCAL] if local else []), *(m for m, machine in machines.items() if machine.auto)}
    candidates = [machine_id for machine_id in offered if machine_id not in found]
    here = found.get(LOCAL)
    reasons = "; ".join(
        f"{label(m, machines)}: {why}" for m, why in sorted(excluded.items()) if local or m != LOCAL
    )
    if not candidates and wait and any(found[m].room for m in offered):
        raise NoRoom(reasons)
    if not candidates and local and here and here.soft:
        # Full, not unable: as before workers existed, it still takes the launch.
        return Placement(
            None,
            "auto: this machine is full and no other machine can take it, "
            + free(LOCAL, machines, loads, reservations),
            excluded,
        )
    if not candidates:
        raise ValueError(f"No machine can take this launch ({reasons})")

    def rank(machine_id):
        return (
            history.get(machine_id, 0) < 2,
            machine_id != req.get("docker_prefer"),
            -history.get(machine_id, 0),
            machine_id != req.get("prefer"),
            busy(machine_id, loads, reservations) / slots(machine_id, machines, loads),
            (loads.get(machine_id) or {}).get("working", 0),
            machine_id != LOCAL,
            label(machine_id, machines),
        )

    best = min(candidates, key=rank)
    why = {2: "prior workspace", 1: "clone present"}.get(history.get(best, 0))
    if best == req.get("docker_prefer") and history.get(best, 0) < 2:
        why = f"Docker prefers {label(best, machines)}"
    if why is None and best == req.get("prefer"):
        why = "preferred for the repository"
    reason = f"auto: {why or 'least busy'}, {free(best, machines, loads, reservations)}"
    docker = req.get("docker_prefer")
    if docker in machines and best != docker:
        # Why the machine Docker prefers was passed over, which the dialog shows.
        passed = excluded.get(docker) or ("" if machines[docker].auto else "not used by Auto")
        reason += f"; {label(docker, machines)}: {passed or 'ranked lower'}"
    return Placement(None if best == LOCAL else best, reason, excluded)
