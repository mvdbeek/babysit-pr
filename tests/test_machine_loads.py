"""Cached worker loads and manual placement: fake herdr per machine, temporary clones."""

import http.client
import json
import threading
import time

import cron_agents
import dashboard
import dashboard_settings
import pr_workspaces as pw
import pytest
from test_new_tasks import request as new_request
from test_pr_workspaces import (  # noqa: F401 (fixture)
    finish,
    helper_calls,
    local,
    make_checkout,
    scheduled_task,
    synced,
)

MINI = {"id": "mini", "label": "Mac mini", "ssh": "mini", "os": "darwin", "labels": ["safehouse"]}
BOX = {"id": "box", "label": "Linux box", "ssh": "box", "os": "linux", "max_agents": 8}
INVENTORY = {
    "workspaces": [{"agent_status": "working"}, {"agent_status": "idle"}, {}],
    "error": None,
    "synced_at": 123.0,
}


def register(home, *entries):
    home.mkdir(parents=True, exist_ok=True)
    (home / "machines.json").write_text(json.dumps({"version": 1, "machines": list(entries)}))


def settle(loads):
    for _ in range(400):
        with loads.lock:
            if not loads.refreshing and not loads.syncing:
                return
        time.sleep(0.05)
    pytest.fail("machine loads never settled")


def clone(name="repo", remotes=("base/repo",), worktrees=()):
    return {"name": name, "remotes": list(remotes), "worktrees": list(worktrees)}


def known(loads, machine_id, *clones):
    """Clones just listed for ``machine_id``, as its placement state."""
    now = time.time()
    with loads.lock:
        loads.states[machine_id] = {
            "clones": list(clones),
            "error": None,
            "synced_at": now,
            "attempted_at": now,
        }


def test_loads_come_from_each_machines_herdr_and_isolate_failures(
    tmp_path, fake_tools, monkeypatch
):
    register(tmp_path, MINI, BOX)
    data = json.loads(fake_tools.read_text())
    spaces = [{"agent_status": s} for s in ("working", "blocked", "idle", "unknown")]
    data["machines"] = {"Mac mini": {"workspaces": spaces, "agents": [], "calls": []}}
    fake_tools.write_text(json.dumps(data))
    monkeypatch.setenv("FAKE_HERDR_DOWN", "Linux box")
    # Concurrent fakes may overwrite each other's record of calls; count them here.
    asked = []
    original = pw.herdr

    def herdr(*args, machine=None, timeout=30):
        asked.append((machine.label, *args, timeout))
        return original(*args, machine=machine, timeout=timeout)

    monkeypatch.setattr(pw, "herdr", herdr)
    loads = pw.MachineLoads(tmp_path, lambda: INVENTORY)
    config, first = loads.snapshot()
    assert set(config["machines"]) == {"mini", "box"}
    # Nothing is known yet; the answer never waited for herdr.
    assert first["mini"]["synced_at"] is None and first["mini"]["agents"] == 0
    assert first["mini"]["pending"] is True
    assert first["local"] == {
        "workspaces": 3,
        "agents": 2,
        "working": 1,
        "blocked": 0,
        "error": None,
        "unreachable_since": None,
        "synced_at": 123.0,
        "pending": False,
        "max_agents": 20,
    }
    settle(loads)
    _, value = loads.snapshot()
    mini, box = value["mini"], value["box"]
    assert (mini["workspaces"], mini["agents"], mini["working"], mini["blocked"]) == (4, 3, 1, 1)
    assert mini["error"] is None and mini["synced_at"] and not mini["pending"]
    assert "cannot reach Linux box" in box["error"] and box["unreachable_since"]
    assert sorted(asked) == [
        ("Linux box", "workspace", "list", pw.LOAD_TIMEOUT),
        ("Mac mini", "workspace", "list", pw.LOAD_TIMEOUT),
    ]
    # Within the TTL the cache answers; after it each machine is asked again, and an
    # unreachable one keeps the time it was first missed.
    loads.snapshot()
    settle(loads)
    assert len(asked) == 2
    since = box["unreachable_since"]
    loads.snapshot(now=time.time() + pw.placement.LOAD_TTL + 1)
    settle(loads)
    assert len(asked) == 4
    assert loads.snapshot()[1]["box"]["unreachable_since"] == since
    monkeypatch.delenv("FAKE_HERDR_DOWN")
    loads.snapshot(now=time.time() + pw.placement.LOAD_TTL + 1)
    settle(loads)
    assert loads.snapshot()[1]["box"]["unreachable_since"] is None


def test_a_hung_machine_blocks_neither_requests_nor_other_machines(tmp_path, monkeypatch):
    register(
        tmp_path, MINI, BOX, {**MINI, "id": "off", "label": "Off", "ssh": "off", "enabled": False}
    )
    release = threading.Event()
    asked = []

    def herdr(*args, machine=None, timeout=30):
        asked.append(machine.id)
        if machine.id == "mini":
            release.wait(10)
        return {"workspaces": [{"agent_status": "working"}]}

    monkeypatch.setattr(pw, "herdr", herdr)
    loads = pw.MachineLoads(tmp_path, lambda: INVENTORY)
    try:
        started = time.monotonic()
        loads.snapshot()
        for _ in range(100):
            if loads.snapshot()[1]["box"]["synced_at"]:
                break
            time.sleep(0.05)
        assert time.monotonic() - started < 5
        _, value = loads.snapshot(now=time.time() + 60)
        assert value["box"]["agents"] == 1 and value["mini"]["synced_at"] is None
        # The hung refresh is never stacked, and a disabled machine is never asked.
        assert asked.count("mini") == 1 and "off" not in asked
    finally:
        release.set()
    settle(loads)


@pytest.mark.parametrize(
    "answer",
    [
        {"workspaces": ["w1"]},
        {"workspaces": {"w1": {}}},
        {"workspaces": None},
        ["w1"],
    ],
)
def test_a_malformed_herdr_answer_is_an_error_and_never_wedges_the_machine(
    tmp_path, monkeypatch, answer
):
    register(tmp_path, MINI)
    asked = []

    def herdr(*args, machine=None, timeout=30):
        asked.append(machine.id)
        return answer

    monkeypatch.setattr(pw, "herdr", herdr)
    loads = pw.MachineLoads(tmp_path, lambda: INVENTORY)
    loads.snapshot()
    settle(loads)
    mini = loads.snapshot()[1]["mini"]
    assert mini["error"] == "herdr listed malformed workspaces" and mini["unreachable_since"]
    assert mini["pending"] is True
    # The next refresh is still started.
    loads.snapshot(now=time.time() + pw.placement.LOAD_TTL + 1)
    settle(loads)
    assert asked == ["mini", "mini"]


def test_an_unforeseen_refresh_failure_is_recorded_and_releases_the_machine(tmp_path, monkeypatch):
    register(tmp_path, MINI)

    def herdr(*args, machine=None, timeout=30):
        raise RuntimeError("surprise")

    monkeypatch.setattr(pw, "herdr", herdr)
    monkeypatch.setattr(threading, "excepthook", lambda args: None)
    loads = pw.MachineLoads(tmp_path, lambda: INVENTORY)
    loads.snapshot()
    settle(loads)
    mini = loads.snapshot()[1]["mini"]
    assert mini["error"] == "Load refresh failed" and mini["unreachable_since"]
    assert not loads.refreshing


def test_without_a_registry_nothing_asks_another_machine(local):  # noqa: F811
    manager, pr, _, state, _ = local
    manager.action({"id": pr["id"], "action": "create", "task": "Local task", "machine": "auto"})
    op = finish(manager, pr["id"])
    assert op["status"] == "complete" and op["machine"] is None
    snapshot = manager.snapshot()
    assert [m["id"] for m in snapshot["machines"]] == ["local"]
    assert manager.preview({"repo": "base/repo"}) == {
        "machine": "local",
        "label": "This machine",
        "reason": "auto: clone present, 20/20 free",
        "options": [],
    }
    assert not [c for c in json.loads(state.read_text())["calls"] if "--machine" in c[0]]
    assert "workers" not in dashboard.status(manager.home)  # So the page never asks.
    with pytest.raises(ValueError, match="Unknown machine mini"):
        manager.action({"id": pr["id"], "action": "handle", "task": "x", "machine": "mini"})


def test_a_chosen_worker_is_recorded_and_its_launch_goes_there(local):  # noqa: F811
    manager, pr, _, state, _ = local
    register(manager.home, MINI)
    calls = helper_calls(manager)
    request = {"id": pr["id"], "action": "create", "task": "Remote task", "machine": "mini"}
    with pytest.raises(ValueError, match="^Mac mini: lacks docker$"):
        manager.action({**request, "docker": True})
    manager.action(request)
    op = finish(manager, pr["id"])
    # This worker has no babysit-pr deployed (test_remote_launch has one that does).
    assert op["machine"] == "mini" and op["status"] == "failed"
    assert "No babysit-pr at" in op["message"]
    assert not calls and not json.loads(state.read_text())["agents"]
    # A new task that files an issue is refused before the issue is filed.
    with pytest.raises(ValueError, match="^Mac mini: only takes pull request work and new tasks$"):
        manager.new_task(
            {
                "repo": "base/repo",
                "clone": str((manager.src / "repo").resolve()),
                "issue_title": "Remote issue",
                "task": "Explore",
                "machine": "mini",
            }
        )
    assert not json.loads(state.read_text()).get("gh")
    settle(manager.loads)
    snapshot = manager.snapshot()
    assert [(m["id"], m["label"]) for m in snapshot["machines"]] == [
        ("local", "This machine"),
        ("mini", "Mac mini"),
    ]
    # The fake herdr answers for the worker: chosen by name it may be, Auto skips it.
    assert [(m["available"], m["reason"], m["pending"]) for m in snapshot["machines"]] == [
        (True, None, False),
        (True, None, False),
    ]
    preview = manager.preview({"repo": "base/repo", "docker": "1"})
    assert preview["machine"] == "local"
    assert preview["options"] == [
        {"id": "mini", "label": "Mac mini", "note": "lacks docker", "disabled": True}
    ]
    with pytest.raises(ValueError, match="Unknown placement parameter"):
        manager.preview({"machine": "mini"})
    with pytest.raises(ValueError, match="Unknown launch kind"):
        manager.preview({"kind": "watch"})
    with pytest.raises(ValueError, match="Only a new workspace"):
        manager.action({"id": pr["id"], "action": "focus", "machine": "mini"})


def test_a_scheduled_launch_keeps_its_worker(synced):  # noqa: F811
    manager, pr, _, state, _ = synced
    register(manager.home, MINI)
    start = time.time() + 3600
    request = {"id": pr["id"], "action": "create", "task": "Later", "machine": "mini"}
    task = manager.action({**request, "start_at": start})["scheduled"]
    assert task["machine"] == "mini" and task["request"]["machine"] == "mini"
    local_task = manager.action({**request, "machine": "auto", "start_at": start + 60})
    assert local_task["scheduled"]["machine"] is None
    manager.run_due(now=start)
    listed = {t["id"]: t for t in manager.scheduled_tasks()["tasks"]}
    assert listed[task["id"]]["status"] == "started"
    op = finish(manager, pr["id"])
    assert op["id"] == listed[task["id"]]["operation_id"] and op["machine"] == "mini"
    settle(manager.loads)


def test_reservations_count_only_launches_herdr_cannot_list_yet(local):  # noqa: F811
    manager, *_ = local
    ops = {
        "queued": {"status": "queued", "machine": "mini"},
        "running": {"status": "running", "machine": "mini"},
        "here": {"status": "running", "machine": None},
        "listed": {"status": "running", "machine": "mini", "result": {"workspace_id": "w1"}},
        "complete": {"status": "complete", "machine": "mini", "result": {"workspace_id": "w2"}},
        "failed": {"status": "failed", "machine": "mini"},
    }
    for key, op in ops.items():
        manager.save_operation({"id": key, "pr": key, **op})
        manager.workers[key] = threading.Thread(target=lambda: None)
    try:
        assert manager.reservations() == {"mini": 2, "local": 1}
    finally:
        manager.workers.clear()


def test_handle_on_a_verified_checkout_stays_on_its_machine(local):  # noqa: F811
    manager, pr, git, *_ = local
    register(manager.home, MINI)
    make_checkout(local)
    git("remote", "add", "fork", "git@github.com:fork/repo.git")
    assert manager.matches(pr, manager.scan())[0]
    request = {"id": pr["id"], "action": "handle", "task": "Fix it"}
    with pytest.raises(
        ValueError, match="existing workspace is on This machine; choose it or Auto"
    ):
        manager.action({**request, "machine": "mini"})
    assert manager.operation(pr["id"]) is None
    settle(manager.loads)


def test_disabled_and_unreachable_workers_are_refused(local):  # noqa: F811
    manager, pr, *_ = local
    register(manager.home, {**MINI, "enabled": False}, BOX)
    now = time.time()
    manager.loads.entries["box"] = {"unreachable_since": now - 60, "attempted_at": now}
    launch = {"id": pr["id"], "action": "create", "task": "Remote task"}
    for machine, why in (
        ("mini", "^Mac mini: disabled$"),
        ("box", r"^Linux box: unreachable \d+s$"),
    ):
        with pytest.raises(ValueError, match=why):
            manager.action({**launch, "machine": machine})
        with pytest.raises(ValueError, match=why):
            manager.new_task(new_request(manager, name="remote", machine=machine))
    assert manager.operation(pr["id"]) is None and not manager.new_operations(manager.state())


def test_a_batch_records_its_machine_on_every_item(synced):  # noqa: F811
    manager, pr, *_ = synced
    register(manager.home, MINI)
    value = manager.batch(
        {"items": [{"id": pr["id"]}, {"id": "I_one"}], "task": "Review {url}", "machine": "mini"}
    )
    review, issue = value["results"]
    assert (review["scheduled"]["machine"], review["scheduled"]["request"]["machine"]) == (
        "mini",
        "mini",
    )
    # Issues may need repairs, which workers cannot run yet.
    assert issue["error"] == "Mac mini: only takes pull request work and new tasks"
    settle(manager.loads)


def test_a_scheduled_auto_launch_is_placed_again_and_starts_here(synced):  # noqa: F811
    manager, pr, *_ = synced
    register(manager.home, MINI)
    start = time.time() + 3600
    request = {"id": pr["id"], "action": "create", "task": "Later", "machine": "auto"}
    task = manager.action({**request, "start_at": start})["scheduled"]
    assert task["machine"] is None and task["request"]["machine"] == "auto"
    manager.run_due(now=start)
    assert scheduled_task(manager, task["id"])["status"] == "started"
    op = finish(manager, pr["id"])
    assert op["status"] == "complete", op["log"]
    assert op["machine"] is None
    settle(manager.loads)


def test_placement_preview_over_http(local):  # noqa: F811
    manager, *_ = local
    register(manager.home, MINI)
    with dashboard.DashboardServer(manager.home, 0, workspaces=manager) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:

            def get(path):
                conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=15)
                conn.request("GET", path)
                response = conn.getresponse()
                value = json.loads(response.read())
                conn.close()
                return response.status, value

            assert get("/api/status")[1]["workers"] == 1
            status, value = get("/api/machines")
            assert status == 200
            assert [(m["id"], m["available"]) for m in value["machines"]] == [
                ("local", True),
                ("mini", True),
            ]
            status, value = get("/api/placement?repo=base/repo&agent=claude&account=work")
            assert status == 200 and value["machine"] == "local"
            note = "lacks claude-account:work"
            assert value["options"] == [
                {"id": "mini", "label": "Mac mini", "note": note, "disabled": True}
            ]
            assert get("/api/placement?nope=1") == (400, {"error": "Unknown placement parameter"})
            assert get("/api/placement?kind=watch") == (400, {"error": "Unknown launch kind"})
            assert get("/api/placement?kind=issue")[1]["machine"] == "local"
        finally:
            server.shutdown()
            thread.join(5)
    settle(manager.loads)


def busy_here(manager, agents):
    manager.loads.local = lambda: {
        "workspaces": [{"agent_status": "working"}] * agents,
        "error": None,
        "synced_at": 1.0,
    }


def test_auto_places_on_a_worker_registered_for_it_only_what_can_start_there(synced):  # noqa: F811
    manager, pr, *_ = synced
    register(manager.home, {**MINI, "auto": True})
    manager.loads.snapshot()
    settle(manager.loads)
    known(manager.loads, "mini", clone("other", ("other/other",)))
    manager.scan()  # The inventory the dashboard keeps fresh says which clones are here.
    busy_here(manager, 3)
    # A new task starts from this machine's clone, so it stays here while the worker has
    # none; the preview says so, as the launch will do.
    preview = manager.preview({"repo": "base/repo"})
    assert (preview["machine"], preview["reason"]) == ("local", "auto: clone present, 17/20 free")
    assert manager.local_history("pr", "base/repo") == {"local": 1}
    assert manager.local_history("pr", "other/repo") == {"local": 0}
    assert manager.preview({"repo": "other/repo", "kind": "pr"})["machine"] == "mini"
    # The worker's clone draws its repository there.
    preview = manager.preview({"repo": "other/other", "kind": "pr"})
    assert (preview["machine"], preview["reason"]) == ("mini", "auto: clone present, 4/4 free")
    # Cloned on both machines, the less busy one takes it; at equal load, this one.
    known(manager.loads, "mini", clone())
    preview = manager.preview({"repo": "base/repo"})
    assert (preview["machine"], preview["reason"]) == ("mini", "auto: clone present, 4/4 free")
    busy_here(manager, 0)
    assert manager.preview({"repo": "base/repo"})["machine"] == "local"
    busy_here(manager, 3)
    dashboard_settings.save(manager.home, {"local_max_agents": 3})
    assert manager.place({"machine": "auto"}, {"kind": "pr", "repo": "base/repo"}) == "mini"
    # Filing an issue keeps a new task here, and so does Auto with other clones.
    assert manager.preview({"repo": "base/repo", "kind": "issue"})["machine"] == "local"
    assert manager.preview({"repo": "base/repo", "also": "1"})["machine"] == "local"
    for kind in ("issue", "sentry"):
        assert manager.place({"machine": "auto"}, {"kind": kind, "repo": "base/repo"}) is None
        assert manager.preview({"repo": "base/repo", "kind": kind})["machine"] == "local"
    # Cron jobs are new tasks: Auto may place them there, resolved once at save.
    request = {"machine": "auto", "agent": "codex", "unsandboxed": False}
    assert cron_agents.place(request, manager, "base/repo").id == "mini"
    # A job needs its clone there already, which a launch would otherwise make.
    known(manager.loads, "mini", clone("other", ("other/other",)))
    assert cron_agents.place(request, manager, "base/repo") is None
    assert cron_agents.place({**request, "machine": "mini"}, manager, "base/repo").id == "mini"
    assert manager.place({"machine": "auto"}, {"kind": "scratch", "repo": "base/repo"}) == "mini"
    known(manager.loads, "mini", clone())
    # A job outside its sandbox stays here, even with this machine full.
    assert cron_agents.place({**request, "unsandboxed": True}, manager, "base/repo") is None
    preview = manager.preview({"repo": "base/repo", "kind": "cron", "unsandboxed": "1"})
    assert preview["machine"] == "local"
    # Picking the worker unticks it, so the worker stays offered.
    assert preview["options"][0]["disabled"] is False
    with pytest.raises(ValueError, match="^Mac mini: runs jobs only in their sandbox$"):
        cron_agents.place({**request, "machine": "mini", "unsandboxed": True}, manager, "base/repo")
    # An issue launch with Auto starts here, without the worker refusing it.
    manager.action({"id": "I_one", "action": "create", "task": "Fix it", "machine": "auto"})
    assert finish(manager, "I_one")["machine"] is None
    settle(manager.loads)


def test_this_machines_capacity_comes_from_the_settings(local):  # noqa: F811
    manager, *_ = local
    register(manager.home, MINI)
    manager.loads.snapshot()
    settle(manager.loads)
    busy_here(manager, 3)
    _, loads = manager.loads.snapshot()
    assert loads["local"]["max_agents"] == 20
    dashboard_settings.save(manager.home, {"local_max_agents": 3})
    here = manager.machine_list()[0]
    assert (here["max_agents"], here["available"], here["reason"]) == (3, True, "full (3/3)")
    # Full, with no worker registered for Auto: it still takes the launch.
    preview = manager.preview({"repo": "base/repo"})
    assert (preview["machine"], preview["reason"]) == (
        "local",
        "auto: this machine is full and no other machine can take it, 0/3 free",
    )
    register(manager.home, {**MINI, "auto": True})
    assert manager.preview({"repo": "base/repo"})["machine"] == "mini"
    settle(manager.loads)


def test_docker_launches_go_to_the_worker_the_registry_prefers(synced):  # noqa: F811
    manager, *_ = synced
    worker = {**MINI, "auto": True, "labels": ["safehouse", "docker"]}
    (manager.home / "machines.json").write_text(
        json.dumps({"version": 1, "machines": [worker], "docker": {"prefer": "mini"}})
    )
    manager.loads.snapshot()
    settle(manager.loads)
    known(manager.loads, "mini")
    manager.scan()
    busy_here(manager, 3)
    # The clone here keeps a new task here, unless it uses Docker.
    assert manager.preview({"repo": "base/repo"})["machine"] == "local"
    preview = manager.preview({"repo": "base/repo", "docker": "1"})
    assert (preview["machine"], preview["reason"]) == (
        "mini",
        "auto: Docker prefers Mac mini, 4/4 free",
    )
    fields = {"kind": "scratch", "repo": "base/repo", "docker": True}
    assert manager.place({"machine": "auto"}, fields, {"local": 1}) == "mini"
    request = {"machine": "auto", "agent": "codex", "unsandboxed": False, "docker": True}
    known(manager.loads, "mini", clone())  # A cron job needs its clone there.
    assert cron_agents.place(request, manager, "base/repo").id == "mini"
    # An issue cannot start there, so Auto starts it here and says why.
    preview = manager.preview({"repo": "base/repo", "docker": "1", "kind": "issue"})
    assert preview["machine"] == "local"
    assert preview["reason"].endswith("; Mac mini: only takes pull request work and new tasks")
    settle(manager.loads)


@pytest.mark.placement_sync
def test_a_workers_clones_are_kept_on_a_slow_ttl_until_they_go_stale(tmp_path, monkeypatch):
    register(tmp_path, MINI, {**MINI, "id": "off", "label": "Off", "ssh": "off", "enabled": False})
    monkeypatch.setattr(pw, "herdr", lambda *args, machine=None, timeout=30: {"workspaces": []})
    answer = {"clones": [clone()]}
    asked = []

    def probe(home, machine, op, timeout, **args):
        asked.append((machine.id, op, args))
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(cron_agents, "probe", probe)
    loads = pw.MachineLoads(tmp_path, lambda: INVENTORY)
    first = loads.snapshot()[1]["mini"]
    assert (first["clones"], first["state_synced_at"]) == (None, None)
    settle(loads)
    # Asked of the worker's registered src; a disabled one is not asked.
    assert asked == [("mini", "placement_state", {"src": "~/src"})]
    mini = loads.snapshot()[1]["mini"]
    assert (mini["clones"], mini["state_error"]) == ([clone()], None)
    at = mini["state_synced_at"]
    # Load refreshes come and go; the clones are asked again only after their own TTL.
    loads.snapshot(now=time.time() + pw.placement.LOAD_TTL + 1)
    settle(loads)
    assert len(asked) == 1
    answer = ValueError("Mac mini did not answer within 60 seconds")
    loads.snapshot(now=time.time() + pw.placement.STATE_TTL + 1)
    settle(loads)
    assert len(asked) == 2
    # A failed refresh keeps the last clones, and says why.
    mini = loads.snapshot()[1]["mini"]
    assert (mini["clones"], mini["state_synced_at"]) == ([clone()], at)
    assert mini["state_error"] == "Mac mini did not answer within 60 seconds"
    # Too old, they are unknown again: a cron job is left to explicit choice there.
    later = at + pw.placement.STATE_STALE + 1
    config, stale = loads.snapshot(now=later)
    settle(loads)
    assert stale["mini"]["clones"] is None
    req = {"kind": "cron", "repo": "base/repo", "sandboxed": True}
    found = pw.placement.exclusions(req, config["machines"], stale, {}, later)
    assert found["mini"] == pw.placement.Exclusion("checking…", True, True)


def test_this_machine_says_it_is_still_counting_before_its_first_scan(local):  # noqa: F811
    manager, *_ = local
    register(manager.home, MINI)
    manager.loads.snapshot()
    settle(manager.loads)
    manager.loads.local = lambda: {"workspaces": [], "error": None, "synced_at": None}
    preview = manager.preview({"repo": "base/repo"})
    assert (preview["machine"], preview["reason"]) == (
        "local",
        "auto: clone present, still counting agents",
    )
    here = manager.machine_list()[0]
    assert (here["pending"], here["reason"]) == (True, None)
