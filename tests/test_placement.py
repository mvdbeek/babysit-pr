"""Placement as a pure function: filters, pins, ranking, Auto's opt-in and no-fit reasons."""

import machines
import placement as pl
import pytest

NOW = 1_000_000.0


def machine(
    id_, label=None, os="darwin", max_agents=4, labels=("safehouse",), enabled=True, auto=True
):
    return machines.Machine(
        id=id_,
        label=label or id_,
        ssh=id_,
        os=os,
        src="~/src",
        babysit_root="~/src/babysit-pr",
        python="python3",
        max_agents=max_agents,
        labels=tuple(labels),
        collie_url=None,
        enabled=enabled,
        auto=auto,
    )


MINI = machine("mini", "Mac mini", labels=("safehouse", "docker", "claude-account:work"))
BOX = machine("box", "Linux box", os="linux", max_agents=8, labels=("bwrap",))
FLEET = {"mini": MINI, "box": BOX}


def load(agents=0, working=0, **changes):
    return {"agents": agents, "working": working, "unreachable_since": None, **changes}


def choose(req, fleet=FLEET, loads=None, history=None, reservations=None, **kwargs):
    return pl.choose(req, fleet, loads or {}, history or {}, reservations or {}, now=NOW, **kwargs)


def test_requirements_follow_docker_sandbox_account_and_repo_rules():
    req = pl.request(
        {"repo": "Galaxy/Galaxy", "agent": "claude", "claude_account": "work", "docker": True},
        {"galaxy/galaxy": {"requires": ["gpu", "docker"], "prefer": "box"}},
    )
    assert req["prefer"] == "box"
    assert pl.requirements(req, MINI) == ["docker", "safehouse", "claude-account:work", "gpu"]
    assert pl.requirements(req, BOX) == ["docker", "bwrap", "claude-account:work", "gpu"]
    # A Codex launch needs no Claude login; an unsandboxed one no sandbox.
    codex = {"agent": "codex", "claude_account": "work", "sandboxed": False}
    assert pl.requirements(codex, MINI) == []
    assert pl.request({"repo": "x/y"}, {})["requires"] == []


def test_hard_filter_reasons():
    fleet = {
        "mini": MINI,
        "box": BOX,
        "off": machine("off", "Old box", enabled=False),
        "full": machine("full", "Full", max_agents=2),
        "gone": machine("gone", "Gone"),
    }
    loads = {
        "mini": load(clones=()),
        "full": load(agents=1),
        "gone": load(unreachable_since=NOW - 180),
    }
    req = {"agent": "codex", "docker": True, "also": ["base/other"]}
    found = pl.exclusions(req, fleet, loads, {"full": 1}, NOW)
    assert {k: tuple(v) for k, v in found.items()} == {
        "mini": ("cannot check out other repositories yet", False),
        "box": ("lacks docker", False),
        "off": ("disabled", False),
        "full": ("lacks docker", False),
        "gone": ("unreachable 3m", False),
    }
    found = pl.exclusions({"agent": "codex"}, fleet, loads, {"full": 1}, NOW)
    assert found["full"] == pl.Exclusion("full (2/2)", True)
    assert "mini" not in found and "box" not in found
    # Unreachable for less than one load TTL is still offered.
    recent = {"gone": load(unreachable_since=NOW - 5)}
    assert "gone" not in pl.exclusions({}, fleet, recent, {}, NOW)


@pytest.mark.parametrize("kind", ["issue", "sentry"])
def test_workers_only_take_pull_request_work_and_new_tasks(kind):
    req = {"sandboxed": False, "kind": kind}
    refused = pl.Exclusion("only takes pull request work and new tasks", False)
    assert pl.exclusions(req, FLEET, {}, {}, NOW) == {"mini": refused, "box": refused}
    # Auto keeps it here however idle the workers are; chosen by name it is refused.
    chosen = choose(req, loads={"local": load(agents=19)})
    assert chosen.machine is None
    assert chosen.reason == "auto: least busy, 1/20 free"
    with pytest.raises(ValueError, match="^Mac mini: only takes pull request work"):
        choose({**req, "override": "mini"})
    for kind in ("pr", "scratch", "cron"):
        assert pl.exclusions({"kind": kind}, FLEET, {}, {}, NOW) == {}


def test_a_cron_job_outside_its_sandbox_stays_here():
    req = {"kind": "cron", "sandboxed": False}
    refused = pl.Exclusion("runs jobs only in their sandbox", False)
    assert pl.exclusions(req, FLEET, {}, {}, NOW) == {"mini": refused, "box": refused}
    # Even with this machine full, Auto keeps it here; a worker by name refuses it.
    full = {"local": load(agents=20)}
    assert choose(req, loads=full).machine is None
    with pytest.raises(ValueError, match="^Linux box: runs jobs only in their sandbox$"):
        choose({**req, "override": "box"})
    # Other launches without a sandbox may still go to a worker.
    assert pl.exclusions({"kind": "scratch", "sandboxed": False}, FLEET, {}, {}, NOW) == {}


def test_other_repositories_keep_a_task_here():
    req = {"sandboxed": False, "also": ["base/other"]}
    refused = pl.Exclusion("cannot check out other repositories yet", False)
    assert pl.exclusions(req, FLEET, {}, {}, NOW) == {"mini": refused, "box": refused}
    assert choose(req, loads={"local": load(agents=19)}).machine is None
    # A local entry describing this machine is never refused for them.
    fleet = {**FLEET, "local": machine("local", "This Mac", labels=())}
    assert "local" not in pl.exclusions(req, fleet, {}, {}, NOW)


def test_a_load_not_refreshed_for_two_ttls_is_checked_again():
    stale, fresh = NOW - 2 * pl.LOAD_TTL - 1, NOW - 2 * pl.LOAD_TTL
    req = {"sandboxed": False}
    # Old counts are not trusted: neither an idle nor a full worker is offered to Auto.
    for agents in (0, 4):
        loads = {"mini": load(agents=agents, attempted_at=stale), "local": load(agents=8)}
        assert pl.exclusions(req, FLEET, loads, {}, NOW) == {
            "mini": pl.Exclusion("checking…", True)
        }
        assert choose(req, loads=loads).machine == "box"
    # Nor is old unreachability: chosen by name, it is no longer refused outright.
    gone = {"mini": load(unreachable_since=NOW - 600, attempted_at=stale)}
    assert choose({"override": "mini"}, loads=gone).reason == "chosen; checking…"
    # A hard reason still outranks it.
    with pytest.raises(ValueError, match="^Linux box: lacks docker$"):
        choose({"override": "box", "docker": True}, loads={"box": load(attempted_at=stale)})
    # Within two TTLs both are trusted.
    assert pl.exclusions(req, FLEET, {"mini": load(attempted_at=fresh)}, {}, NOW) == {}
    with pytest.raises(ValueError, match="^Mac mini: unreachable 10m$"):
        choose(
            {"override": "mini"},
            loads={"mini": load(unreachable_since=NOW - 600, attempted_at=fresh)},
        )


def test_auto_only_considers_workers_registered_for_it():
    idle = {"mini": load(), "box": load(agents=4), "local": load(agents=7, working=7)}
    manual = {m: machine._replace(auto=False) for m, machine in FLEET.items()}
    chosen = choose({"agent": "codex"}, manual, loads=idle)
    assert chosen == pl.Placement(None, "auto: least busy, 13/20 free", {})
    # Chosen by name, a worker without auto still takes the launch.
    assert choose({"override": "mini"}, manual, loads=idle).machine == "mini"
    # Registered for Auto, the idle worker wins over a busier local machine.
    chosen = choose({"agent": "codex"}, loads=idle)
    assert chosen == pl.Placement("mini", "auto: least busy, 4/4 free", {})


def test_this_machine_is_full_at_its_capacity_and_still_takes_what_nobody_else_can():
    loads = {"local": load(agents=10, max_agents=10), "mini": load(), "box": load()}
    found = pl.exclusions({"sandboxed": False}, FLEET, loads, {}, NOW)
    assert found == {"local": pl.Exclusion("full (10/10)", True)}
    assert choose({"sandboxed": False}, loads=loads).machine == "box"
    # Chosen by name it is only noted.
    chosen = choose({"override": "local"}, loads=loads)
    assert chosen.machine is None and chosen.reason == "chosen; full (10/10)"
    # No worker can take an issue: this machine does, as before workers existed.
    chosen = choose({"sandboxed": False, "kind": "issue"}, loads=loads)
    assert chosen.machine is None
    assert chosen.reason == "auto: this machine is full and no other machine can take it, 0/10 free"
    # Without a capacity from the settings it holds the default.
    assert pl.slots("local", FLEET, {}) == pl.LOCAL_SLOTS == 20
    # Reservations count toward it, as on workers.
    nearly = {"local": load(agents=9, max_agents=10)}
    assert "local" in pl.exclusions({}, {}, nearly, {"local": 1}, NOW)


def test_pins_beat_score_and_an_override_must_agree_with_a_pin():
    idle = {"mini": load(), "local": load(agents=8)}
    assert choose({"pin": "local"}, loads=idle).machine is None
    pinned = choose({"pin": "mini"})
    assert pinned.machine == "mini" and pinned.reason == "existing workspace"
    # A verified checkout stays where it is; choosing another machine is refused.
    with pytest.raises(
        ValueError, match="^The existing workspace is on This machine; choose it or Auto$"
    ):
        choose({"pin": "local", "override": "box", "sandboxed": False})
    with pytest.raises(ValueError, match="existing workspace is on Mac mini"):
        choose({"pin": "mini", "override": "local"})
    chosen = choose({"pin": "mini", "override": "mini"})
    assert chosen.machine == "mini" and chosen.reason == "chosen"
    assert choose({"override": "local"}).machine is None
    assert choose({"override": "auto", "pin": "mini"}).machine == "mini"
    with pytest.raises(ValueError, match="Unknown machine nope"):
        choose({"override": "nope"})


def test_sticky_beats_load_until_full():
    loads = {"mini": load(agents=3), "box": load(agents=0), "local": load(agents=8)}
    req = {"sandboxed": False}
    chosen = choose(req, loads=loads, history={"mini": 1})
    assert chosen.machine == "mini" and chosen.reason == "auto: clone present, 1/4 free"
    loads["mini"] = load(agents=4)
    chosen = choose(req, loads=loads, history={"mini": 2})
    assert chosen.machine == "box"
    assert chosen.excluded == {"mini": "full (4/4)"}


def test_preferred_machine_then_deterministic_ties():
    req = {"sandboxed": False, "prefer": "box"}
    loads = {"local": load(agents=8)}
    chosen = choose(req, loads=loads)
    assert chosen.machine == "box" and chosen.reason.startswith("auto: preferred")
    # Equal load and working: this machine first, then by label.
    assert choose({"sandboxed": False}).machine is None
    loads = {"local": load(agents=8)}
    twins = {"b": machine("b", "Bravo", max_agents=8), "a": machine("a", "Alpha", max_agents=8)}
    assert choose({}, twins, loads).machine == "a"
    # Fewer working agents breaks a load tie.
    loads = {"local": load(agents=8), "a": load(agents=2, working=2), "b": load(agents=2)}
    assert choose({}, twins, loads).machine == "b"


def test_override_bypasses_capacity_not_capability():
    loads = {"mini": load(agents=4)}
    chosen = choose({"override": "mini"}, loads=loads)
    assert chosen == pl.Placement("mini", "chosen; full (4/4)", {"mini": "full (4/4)"})
    with pytest.raises(ValueError, match="^Linux box: lacks docker$"):
        choose({"override": "box", "docker": True})
    with pytest.raises(ValueError, match="Mac mini: unreachable 2m"):
        choose({"override": "mini"}, loads={"mini": load(unreachable_since=NOW - 130)})
    off = {"off": machine("off", "Old box", enabled=False)}
    with pytest.raises(ValueError, match="Old box: disabled"):
        choose({"override": "off"}, off)


def test_a_worker_not_yet_checked_is_left_to_explicit_choice():
    loads = {"mini": load(pending=True), "local": load(agents=8)}
    found = pl.exclusions({"sandboxed": False}, FLEET, loads, {}, NOW)
    assert found == {"mini": pl.Exclusion("checking…", True)}
    chosen = choose({"sandboxed": False}, loads=loads)
    assert chosen.machine == "box"
    chosen = choose({"override": "mini"}, loads=loads)
    assert chosen.machine == "mini" and chosen.reason == "chosen; checking…"
    # A hard reason outranks not knowing the load yet.
    with pytest.raises(ValueError, match="^Linux box: lacks docker$"):
        choose({"override": "box", "docker": True}, loads={"box": load(pending=True)})


def test_no_fit_lists_every_machine():
    # A local entry declares this machine's capabilities; it is never refused for load.
    fleet = {**FLEET, "local": machine("local", "This Mac", labels=())}
    loads = {"mini": load(unreachable_since=NOW - 600), "local": load(agents=50)}
    with pytest.raises(ValueError) as caught:
        choose({"docker": True}, fleet, loads)
    assert str(caught.value) == (
        "No machine can take this launch (Linux box: lacks docker; "
        "This Mac: lacks docker, safehouse; Mac mini: unreachable 10m)"
    )


def test_reservations_spread_a_batch():
    loads = {"local": load(agents=20)}
    fleet = {"a": machine("a", max_agents=2), "b": machine("b", max_agents=2)}
    reservations: dict[str, int] = {}
    placed = []
    for _ in range(4):
        chosen = choose({}, fleet, loads, reservations=reservations).machine
        placed.append(chosen)
        reservations[chosen] = reservations.get(chosen, 0) + 1
    assert placed == ["a", "b", "a", "b"]
    # Both full, and this machine too: it takes the rest, as before workers existed.
    rest = choose({}, fleet, loads, reservations=reservations)
    assert (
        rest.machine is None
        and rest.reason == "auto: this machine is full and no other machine can take it, 0/20 free"
    )
