"""Launches that wait for room: a machine named, or a sub-task's, that is full or not yet
checked records the launch as waiting, and the scheduler starts it once there is room."""

import json
import time

import dashboard
import pr_workspaces as pw
import pytest
from test_machine_loads import settle
from test_new_tasks import request as new_request
from test_pr_workspaces import finish, local, space, synced  # noqa: F401 (fixtures)
from test_remote_cron import short_tmp  # noqa: F401 (fixture)
from test_remote_launch import register, remote, store  # noqa: F401 (fixture)


def busy(state, agents):
    """The worker Mac mini's herdr lists ``agents`` working agents."""
    data = store(state)
    mini = data.setdefault("machines", {}).setdefault(
        "Mac mini", {"workspaces": [], "agents": [], "calls": []}
    )
    mini["workspaces"] = [
        {"workspace_id": f"busy{n}", "agent_status": "working"} for n in range(agents)
    ]
    state.write_text(json.dumps(data))


def reloaded(manager):
    """Forget the cached loads, as after their TTL, so placement asks the machines again."""
    with manager.loads.lock:
        manager.loads.entries.clear()


def waiting_rows(manager):
    with manager.db() as db:
        return [key for (key,) in db.execute("SELECT pr FROM waiting")]


def test_a_new_task_for_a_full_worker_waits_then_starts_there(remote):  # noqa: F811
    manager, _, state, worker = remote
    register(manager.home, max_agents=1)
    busy(state, 1)
    op = manager.new_task(new_request(manager, name="try", machine="mini"))["operation"]
    assert op["status"] == "waiting", op
    assert op["message"] == "Waiting for room: Mac mini: full (1/1)"
    assert op["machine"] == "mini" and op["pr"] not in manager.workers
    assert op["wait_until"] == pytest.approx(time.time() + pw.ROOM_WAIT, abs=60)
    # The Scheduled tab lists it, with what it is for.
    [shown] = manager.scheduled_tasks()["waiting"]
    assert (shown["pr"], shown["action"], shown["machine"]) == (op["pr"], "new", "mini")
    assert shown["subject"] == {"kind": "scratch", "repo": "base/repo", "branch": "try"}
    # Still full: it keeps waiting.
    reloaded(manager)
    manager.run_waiting()
    assert manager.operation(op["pr"])["status"] == "waiting"
    busy(state, 0)
    reloaded(manager)
    manager.run_waiting()
    started = finish(manager, op["pr"])
    assert started["status"] == "complete", started
    assert started["id"] == op["id"] and started["created_at"] == op["created_at"]
    assert started["result"]["path"] == str((worker / "src/worktrees/repo/try").resolve())
    assert waiting_rows(manager) == [] and manager.scheduled_tasks()["waiting"] == []


def test_auto_still_places_what_a_named_machine_would_wait_for(remote):  # noqa: F811
    manager, _, state, _ = remote
    register(manager.home, max_agents=1, auto=True)
    busy(state, 1)
    op = manager.new_task(new_request(manager, name="here", machine="auto"))["operation"]
    assert finish(manager, op["pr"])["machine"] is None


def test_a_handle_on_a_full_machine_named_waits_and_holds_its_item(synced):  # noqa: F811
    manager, pr, git, state, _ = synced
    pw.dashboard_settings.save(manager.home, {"local_max_agents": 1})
    space(state, manager.src / "elsewhere", manager.src / "repo")
    request = {"id": pr["id"], "action": "handle", "task": "Review it", "machine": "local"}
    op = manager.action(request)["operation"]
    assert op["status"] == "waiting", op
    assert op["message"] == "Waiting for room: This machine: full (1/1)"
    # A second launch for the item gets the waiting one, as for one under way.
    assert manager.action({**request, "task": "Again"})["operation"]["id"] == op["id"]
    [shown] = manager.scheduled_tasks()["waiting"]
    assert shown["action"] == "handle" and shown["subject"]["number"] == 7
    data = json.loads(state.read_text())
    data["workspaces"] = []
    state.write_text(json.dumps(data))
    manager.run_waiting()
    started = finish(manager, pr["id"])
    assert started["status"] == "complete", started
    assert started["id"] == op["id"]
    [agent] = json.loads(state.read_text())["agents"]
    assert "Review it" in agent["task"]


def test_a_waiting_launch_is_cancelled_or_gives_up(local):  # noqa: F811
    manager, _, _, state, _ = local
    pw.dashboard_settings.save(manager.home, {"local_max_agents": 1})
    space(state, manager.src / "elsewhere", manager.src / "repo")
    manager.scan()
    first = manager.new_task(new_request(manager, name="one", machine="local"))["operation"]
    second = manager.new_task(new_request(manager, name="two", machine="local"))["operation"]
    assert first["status"] == second["status"] == "waiting"
    cancelled = manager.cancel_waiting(first["pr"])
    assert cancelled["status"] == "failed"
    assert cancelled["message"] == "Cancelled while waiting for room"
    with pytest.raises(ValueError, match="no longer waiting"):
        manager.cancel_waiting(first["pr"])
    manager.run_waiting(now=time.time() + pw.ROOM_WAIT + 1)
    gone = manager.operation(second["pr"])
    assert gone["status"] == "failed"
    assert gone["message"] == "Not started: no room within 6 hours (This machine: full (1/1))"
    assert waiting_rows(manager) == []
    assert not json.loads(state.read_text())["agents"]


def test_a_launch_that_can_no_longer_start_fails_with_the_reason(local):  # noqa: F811
    manager, _, _, state, _ = local
    pw.dashboard_settings.save(manager.home, {"local_max_agents": 1})
    space(state, manager.src / "elsewhere", manager.src / "repo")
    manager.scan()
    op = manager.new_task(new_request(manager, name="gone", machine="local"))["operation"]
    with manager.db() as db:
        (saved,) = db.execute("SELECT data FROM waiting WHERE pr=?", (op["pr"],)).fetchone()
        saved = json.loads(saved)
        saved["request"]["clone"] = "/nowhere"
        db.execute("UPDATE waiting SET data=? WHERE pr=?", (json.dumps(saved), op["pr"]))
    pw.dashboard_settings.save(manager.home, {"local_max_agents": 20})
    manager.run_waiting()
    failed = manager.operation(op["pr"])
    assert failed["status"] == "failed"
    assert failed["message"] == "Not started: Choose a local clone of base/repo"


def test_the_dashboard_cancels_a_waiting_launch(local):  # noqa: F811
    import http.client
    import threading

    manager, _, _, state, _ = local
    pw.dashboard_settings.save(manager.home, {"local_max_agents": 1})
    space(state, manager.src / "elsewhere", manager.src / "repo")
    manager.scan()
    op = manager.new_task(new_request(manager, name="one", machine="local"))["operation"]
    with dashboard.DashboardServer(manager.home, 0, workspaces=manager) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:

            def post(action, body):
                conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=15)
                conn.request(
                    "POST",
                    f"/api/{action}",
                    json.dumps(body),
                    {"Content-Type": "application/json", "X-Babysit-Action": action},
                )
                response = conn.getresponse()
                result = response.status, json.loads(response.read())
                conn.close()
                return result

            status, value = post("waiting-cancel", {"id": op["pr"]})
            assert status == 200 and value["operation"]["status"] == "failed"
            status, value = post("waiting-cancel", {"id": op["pr"]})
            assert status == 400 and "no longer waiting" in value["error"]
        finally:
            server.shutdown()
            thread.join()


def test_a_workers_subtask_is_placed_once_the_load_nobody_asked_for_answers(remote):  # noqa: F811
    """A worker's load is refreshed only when asked for: placement waits for that answer
    rather than refusing every worker as still being checked."""
    from test_subtasks import launch, parent

    manager, _, _, _ = remote
    register(manager.home, auto=True)
    asked = parent(machine="mini", machine_label="Mac mini", sandbox="bwrap")
    op = manager.subtask(launch(), asked)["operation"]
    assert op["machine"] == "mini" and op["status"] in {"queued", "running", "complete"}, op
    assert manager.await_operation(op, 30)["status"] == "complete"
    settle(manager.loads)


def test_a_workers_subtask_waits_for_a_full_worker(remote):  # noqa: F811
    from test_subtasks import launch, parent

    manager, _, state, _ = remote
    register(manager.home, auto=True, max_agents=1)
    busy(state, 1)
    asked = parent(machine="mini", machine_label="Mac mini", sandbox="bwrap")
    op = manager.subtask(launch(), asked)["operation"]
    assert op["status"] == "waiting", op
    assert op["message"] == "Waiting for room: Mac mini: full (1/1)"
    assert op["started_by"].endswith(" on Mac mini, session 0b6f3c1e")
    busy(state, 0)
    reloaded(manager)
    manager.run_waiting()
    started = manager.await_operation(manager.operation(op["pr"]), 30)
    assert started["status"] == "complete", started
    assert started["machine"] == "mini" and started["parent"] == op["parent"]
