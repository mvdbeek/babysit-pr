"""Placement as a pure function: filters, pins, ranking, the rollout gate and no-fit reasons."""

import machines
import placement as pl
import pytest

NOW = 1_000_000.0


def machine(id_, label=None, os="darwin", max_agents=4, labels=("safehouse",), enabled=True):
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
        "mini": ("lacks clones of base/other", False),
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


def test_clones_are_only_checked_once_a_worker_lists_them():
    req = {"sandboxed": False, "also": ["base/other"]}
    # No clone inventory yet: the launch is recorded, then refused as not supported.
    assert pl.exclusions(req, FLEET, {"mini": load(), "box": load()}, {}, NOW) == {}
    loads = {"mini": load(clones=("base/other",)), "box": load(clones=("base/repo",))}
    assert pl.exclusions(req, FLEET, loads, {}, NOW) == {
        "box": pl.Exclusion("lacks clones of base/other", False)
    }


def test_a_load_not_refreshed_for_two_ttls_is_checked_again():
    stale, fresh = NOW - 2 * pl.LOAD_TTL - 1, NOW - 2 * pl.LOAD_TTL
    req = {"sandboxed": False}
    # Old counts are not trusted: neither an idle nor a full worker is offered to Auto.
    for agents in (0, 4):
        loads = {"mini": load(agents=agents, attempted_at=stale), "local": load(agents=8)}
        assert pl.exclusions(req, FLEET, loads, {}, NOW) == {
            "mini": pl.Exclusion("checking…", True)
        }
        assert choose(req, loads=loads, auto_remote=True).machine == "box"
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


def test_auto_stays_local_until_remote_auto_is_enabled():
    idle = {"mini": load(), "box": load(agents=4), "local": load(agents=7, working=7)}
    chosen = choose({"agent": "codex"}, loads=idle)
    assert chosen.machine is None
    assert chosen.reason == "auto: workers are only used when chosen, 1/8 free"
    # The gate open, the idle worker wins over a busy local machine.
    chosen = choose({"agent": "codex"}, loads=idle, auto_remote=True)
    assert chosen == pl.Placement("mini", "auto: least busy, 4/4 free", {})


def test_pins_beat_score_and_an_override_must_agree_with_a_pin():
    idle = {"mini": load(), "local": load(agents=8)}
    assert choose({"pin": "local"}, loads=idle, auto_remote=True).machine is None
    pinned = choose({"pin": "mini"}, auto_remote=True)
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
    chosen = choose(req, loads=loads, history={"mini": 1}, auto_remote=True)
    assert chosen.machine == "mini" and chosen.reason == "auto: clone present, 1/4 free"
    loads["mini"] = load(agents=4)
    chosen = choose(req, loads=loads, history={"mini": 2}, auto_remote=True)
    assert chosen.machine == "box"
    assert chosen.excluded == {"mini": "full (4/4)"}


def test_preferred_machine_then_deterministic_ties():
    req = {"sandboxed": False, "prefer": "box"}
    loads = {"local": load(agents=8)}
    chosen = choose(req, loads=loads, auto_remote=True)
    assert chosen.machine == "box" and chosen.reason.startswith("auto: preferred")
    # Equal load and working: this machine first, then by label.
    assert choose({"sandboxed": False}, auto_remote=True).machine is None
    loads = {"local": load(agents=8)}
    twins = {"b": machine("b", "Bravo", max_agents=8), "a": machine("a", "Alpha", max_agents=8)}
    assert choose({}, twins, loads, auto_remote=True).machine == "a"
    # Fewer working agents breaks a load tie.
    loads = {"local": load(agents=8), "a": load(agents=2, working=2), "b": load(agents=2)}
    assert choose({}, twins, loads, auto_remote=True).machine == "b"


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
    chosen = choose({"sandboxed": False}, loads=loads, auto_remote=True)
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
        choose({"docker": True}, fleet, loads, auto_remote=True)
    assert str(caught.value) == (
        "No machine can take this launch (Linux box: lacks docker; "
        "This Mac: lacks docker, safehouse; Mac mini: unreachable 10m)"
    )


def test_reservations_spread_a_batch():
    loads = {"local": load(agents=8)}
    fleet = {"a": machine("a", max_agents=2), "b": machine("b", max_agents=2)}
    reservations: dict[str, int] = {}
    placed = []
    for _ in range(4):
        chosen = choose({}, fleet, loads, reservations=reservations, auto_remote=True).machine
        placed.append(chosen)
        reservations[chosen] = reservations.get(chosen, 0) + 1
    assert placed == ["a", "b", "a", "b"]
    # Both full: this machine, never refused for load, takes the rest.
    assert choose({}, fleet, loads, reservations=reservations, auto_remote=True).machine is None
