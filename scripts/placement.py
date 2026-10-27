"""Which machine a launch runs on: a pure function of the registry, cached loads and history.

``machines`` are the registry's workers by id. This machine is ``LOCAL``; without a
``LOCAL`` entry it is assumed to meet every requirement, which is how every launch ran
before workers existed. Past its capacity (``loads[LOCAL]["max_agents"]``, from the
dashboard's settings) Auto prefers a worker, and comes back here when none can take the
launch. ``loads`` are ``MachineLoads`` entries by id, ``history`` a stickiness level by
id (2: a prior workspace for the same target, 1: a clone of its repository is there),
and ``reservations`` the launches placed on each machine that herdr does not list yet.
"""

import time
from typing import NamedTuple

LOCAL = "local"
# Seconds a machine's cached load stays fresh, and how long it may stay unreachable
# before placement stops offering it.
LOAD_TTL = 15
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


def request(fields, repos):
    """A placement request from launch ``fields`` plus the registry's per-repo rules.

    ``fields``: ``kind`` (``pr``, ``issue``, ``sentry``, ``scratch`` or ``cron``),
    ``repo``, ``agent``, ``claude_account``, ``docker``, ``sandboxed`` (default true),
    ``also`` (repositories cloned beside it), ``override`` (a machine id, ``LOCAL`` or
    ``"auto"``) and ``pin`` (a machine an existing workspace is on).
    """
    rules = {slug.lower(): rule for slug, rule in repos.items()}
    rule = rules.get((fields.get("repo") or "").lower(), {})
    return {**fields, "requires": list(rule.get("requires", [])), "prefer": rule.get("prefer")}


def requirements(req, machine):
    """Capability labels ``machine`` needs for ``req``, in a stable order."""
    needed = []
    if req.get("docker"):
        needed.append("docker")
    if req.get("sandboxed", True):
        needed.append(SANDBOX[machine.os])
    if req.get("agent") == "claude" and req.get("claude_account"):
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
        if not machine.enabled:
            excluded[machine_id] = Exclusion("disabled", False)
        elif since is not None and not stale and now - since > LOAD_TTL:
            excluded[machine_id] = Exclusion(f"unreachable {age(now - since)}", False)
        elif lacking:
            excluded[machine_id] = Exclusion(f"lacks {', '.join(lacking)}", False)
        elif machine_id != LOCAL and req.get("kind", "scratch") not in WORKER_KINDS:
            excluded[machine_id] = Exclusion("only takes pull request work and new tasks", False)
        elif machine_id != LOCAL and req.get("also"):
            # `wt --with` checks every clone out on one host, so far only this one.
            excluded[machine_id] = Exclusion("cannot check out other repositories yet", False)
        elif machine_id != LOCAL and req.get("kind") == "cron" and not req.get("sandboxed", True):
            excluded[machine_id] = Exclusion("runs jobs only in their sandbox", False)
        elif machine_id != LOCAL and (stale or load.get("pending")):
            # No load has come back yet, so its agents are unknown.
            excluded[machine_id] = Exclusion("checking…", True)
        elif machine_id != LOCAL and used >= machine.max_agents:
            excluded[machine_id] = Exclusion(f"full ({used}/{machine.max_agents})", True)
    used, total = busy(LOCAL, loads, reservations), slots(LOCAL, machines, loads)
    if LOCAL not in excluded and used >= total:
        excluded[LOCAL] = Exclusion(f"full ({used}/{total})", True)
    return excluded


def label(machine_id, machines):
    machine = machines.get(machine_id)
    return machine.label if machine else "This machine"


def free(machine_id, machines, loads, reservations):
    total = slots(machine_id, machines, loads)
    return f"{max(0, total - busy(machine_id, loads, reservations))}/{total} free"


def choose(req, machines, loads, history, reservations, *, now=None):
    """The machine ``req`` runs on, or ValueError naming why each machine could not take it.

    An explicit choice (``override``) wins, then a pin, and the two must agree; Auto
    ranks what is left by stickiness, the repository's preferred machine, load, then
    this machine first and label, so equal candidates always resolve the same way.
    Auto only considers workers registered with ``auto``, and this machine when it is
    full only if nothing else can take the launch.
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
        if exclusion and not exclusion.soft:
            raise ValueError(f"{label(machine_id, machines)}: {exclusion.why}")
        if exclusion:
            reason = f"{reason}; {exclusion.why}"
        return Placement(None if machine_id == LOCAL else machine_id, reason, excluded)

    override = req.get("override")
    pin = req.get("pin")
    if override not in (None, "auto"):
        known(override)
        if pin is not None and override != pin:
            raise ValueError(
                f"The existing workspace is on {label(pin, machines)}; choose it or Auto"
            )
        return named(override, "chosen")
    if pin is not None:
        return named(pin, "existing workspace")
    offered = {LOCAL, *(m for m, machine in machines.items() if machine.auto)}
    candidates = [machine_id for machine_id in offered if machine_id not in found]
    local = found.get(LOCAL)
    if not candidates and local and local.soft:
        # Full, not unable: as before workers existed, it still takes the launch.
        return Placement(
            None,
            "auto: this machine is full and no other machine can take it, "
            + free(LOCAL, machines, loads, reservations),
            excluded,
        )
    if not candidates:
        reasons = "; ".join(f"{label(m, machines)}: {why}" for m, why in sorted(excluded.items()))
        raise ValueError(f"No machine can take this launch ({reasons})")

    def rank(machine_id):
        return (
            -history.get(machine_id, 0),
            machine_id != req.get("prefer"),
            busy(machine_id, loads, reservations) / slots(machine_id, machines, loads),
            (loads.get(machine_id) or {}).get("working", 0),
            machine_id != LOCAL,
            label(machine_id, machines),
        )

    best = min(candidates, key=rank)
    why = {2: "prior workspace", 1: "clone present"}.get(history.get(best, 0))
    if why is None and best == req.get("prefer"):
        why = "preferred for the repository"
    reason = f"auto: {why or 'least busy'}, {free(best, machines, loads, reservations)}"
    return Placement(None if best == LOCAL else best, reason, excluded)
