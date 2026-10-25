"""Which machine a launch runs on: a pure function of the registry, cached loads and history.

``machines`` are the registry's workers by id. This machine is ``LOCAL``; without a
``LOCAL`` entry it is assumed to meet every requirement and is never refused for load,
which is how every launch ran before workers existed. ``loads`` are ``MachineLoads``
entries by id, ``history`` a stickiness level by id (2: a prior workspace for the same
target, 1: a clone of its repository is there), and ``reservations`` the launches placed
on each machine that herdr does not list yet.
"""

import time
from typing import NamedTuple

LOCAL = "local"
# Auto never picks a worker until remote launching, inventory and repairs have landed.
# Until then a worker is only ever chosen by name.
AUTO_REMOTE = False
# Seconds a machine's cached load stays fresh, and how long it may stay unreachable
# before placement stops offering it.
LOAD_TTL = 15
# Slots this machine counts as when its load is ranked against workers'.
LOCAL_SLOTS = 8
# The capability label a sandboxed agent needs, by operating system.
SANDBOX = {"darwin": "safehouse", "linux": "bwrap"}


class Placement(NamedTuple):
    machine: str | None  # Registry id; None is this machine.
    reason: str
    excluded: dict[str, str]  # Why each machine left out could not take the launch.


class Exclusion(NamedTuple):
    why: str
    capacity: bool  # Only full: an explicit choice may still overfill it.


def request(fields, repos):
    """A placement request from launch ``fields`` plus the registry's per-repo rules.

    ``fields``: ``repo``, ``agent``, ``claude_account``, ``docker``, ``sandboxed``
    (default true), ``also`` (repositories cloned beside it), ``override`` (a machine id,
    ``LOCAL`` or ``"auto"``) and ``pin`` (a machine an existing workspace is on).
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


def slots(machine_id, machines):
    machine = machines.get(machine_id)
    return machine.max_agents if machine else LOCAL_SLOTS


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
        lacking = [label for label in requirements(req, machine) if label not in machine.labels]
        missing = [repo for repo in req.get("also", []) if repo not in load.get("clones", ())]
        used = busy(machine_id, loads, reservations)
        if not machine.enabled:
            excluded[machine_id] = Exclusion("disabled", False)
        elif since is not None and now - since > LOAD_TTL:
            excluded[machine_id] = Exclusion(f"unreachable {age(now - since)}", False)
        elif lacking:
            excluded[machine_id] = Exclusion(f"lacks {', '.join(lacking)}", False)
        elif missing and machine_id != LOCAL:
            # `wt --with` checks every clone out on one host.
            excluded[machine_id] = Exclusion(f"lacks clones of {', '.join(missing)}", False)
        elif machine_id != LOCAL and used >= machine.max_agents:
            excluded[machine_id] = Exclusion(f"full ({used}/{machine.max_agents})", True)
    return excluded


def label(machine_id, machines):
    machine = machines.get(machine_id)
    return machine.label if machine else "This machine"


def free(machine_id, machines, loads, reservations):
    total = slots(machine_id, machines)
    return f"{max(0, total - busy(machine_id, loads, reservations))}/{total} free"


def choose(req, machines, loads, history, reservations, *, now=None, auto_remote=AUTO_REMOTE):
    """The machine ``req`` runs on, or ValueError naming why each machine could not take it.

    An explicit choice (``override``) wins, then a pin; Auto ranks what is left by
    stickiness, the repository's preferred machine, load, then this machine first and
    label, so equal candidates always resolve the same way.
    """
    now = time.time() if now is None else now
    found = exclusions(req, machines, loads, reservations, now)
    excluded = {machine_id: e.why for machine_id, e in found.items()}

    def named(machine_id, reason):
        if machine_id != LOCAL and machine_id not in machines:
            raise ValueError(f"Unknown machine {machine_id}")
        exclusion = found.get(machine_id)
        if exclusion and not exclusion.capacity:
            raise ValueError(f"{label(machine_id, machines)}: {exclusion.why}")
        if exclusion:
            reason = f"{reason}; {exclusion.why}"
        return Placement(None if machine_id == LOCAL else machine_id, reason, excluded)

    override = req.get("override")
    if override not in (None, "auto"):
        return named(override, "chosen")
    if req.get("pin") is not None:
        return named(req["pin"], "existing workspace")
    candidates = [
        machine_id
        for machine_id in ({LOCAL, *machines} if auto_remote else {LOCAL})
        if machine_id not in found
    ]
    if not candidates:
        reasons = "; ".join(f"{label(m, machines)}: {why}" for m, why in sorted(excluded.items()))
        raise ValueError(f"No machine can take this launch ({reasons})")

    def rank(machine_id):
        return (
            -history.get(machine_id, 0),
            machine_id != req.get("prefer"),
            busy(machine_id, loads, reservations) / slots(machine_id, machines),
            (loads.get(machine_id) or {}).get("working", 0),
            machine_id != LOCAL,
            label(machine_id, machines),
        )

    best = min(candidates, key=rank)
    why = {2: "prior workspace", 1: "clone present"}.get(history.get(best, 0))
    if why is None and best == req.get("prefer"):
        why = "preferred for the repository"
    if why is None and not auto_remote:
        why = "workers are only used when chosen"
    reason = f"auto: {why or 'least busy'}, {free(best, machines, loads, reservations)}"
    return Placement(None if best == LOCAL else best, reason, excluded)
