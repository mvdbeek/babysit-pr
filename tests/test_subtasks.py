"""Sub-tasks agents start from their sandbox: the controller's rules, endpoints and broker.

``wt`` inside a sandbox hands a new task to the controller (see ``tests/test_wt.py``):
from Safehouse here to ``/api/workspace-subtask``, from a worker's babysit-bwrap through
its launch's broker to ``/api/worker/launch``. Both end in ``Workspaces.subtask``.
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

import dashboard
import pr_supervisor as supervisor
import pr_workspaces as pw
import pytest
import worker_api
import workspace_overview
import wt
from test_pr_workspaces import finish, helper_calls, local  # noqa: F401 (fixture)
from test_remote_cron import git, rollout, short_tmp  # noqa: F401 (fixture)
from test_remote_launch import remote  # noqa: F401 (fixture)
from test_remote_registration import (  # noqa: F401 (fixtures)
    box,
    fresh_rate_limits,
    post,
)
from test_worker_broker import ask

BROKER = Path(supervisor.__file__).resolve().parent / "worker_broker.py"
SID = "0b6f3c1e-2d4a-4e5f-9a7b-8c9d0e1f2a3b"


def launch(**changes):
    return {
        "repo": "base/repo",
        "base": "feature",
        "name": "sub",
        "agent": "codex",
        "model": "",
        "effort": "",
        "claude_account": "",
        "task": "Rebase it\nand resolve the conflicts",
        "docker": False,
        "exit_when_done": False,
        "permission_mode": "",
        "machine": "auto",
        **changes,
    }


def parent(**changes):
    return {
        "machine": None,
        "machine_label": None,
        "cwd": "/home/u/src/worktrees/repo/cron",
        "session": SID,
        "agent": "claude",
        "rollout": None,
        "docker": False,
        "sandbox": "safehouse",
        **changes,
    }


def record(manager, cwd, machine=None, status="complete", created=None, target=None, path=None):
    """An earlier sub-task's operation, as ``subtask`` records them."""
    key = f"{pw.NEW_PREFIX}{uuid.uuid4()}"
    op = {
        "id": str(uuid.uuid4()),
        "pr": key,
        "status": status,
        "machine": target,
        "path": path,
        "created_at": time.time() if created is None else created,
        "parent": parent(cwd=cwd, machine=machine),
    }
    with manager.db() as db:
        db.execute("INSERT INTO operations VALUES (?,?)", (key, json.dumps(op)))
    return op


def test_a_subtask_starts_here_in_this_machines_clone_and_names_who_asked(local):  # noqa: F811
    manager, *_ = local
    calls = helper_calls(manager)
    asked = parent()
    op = manager.subtask(launch(repo="Base/Repo"), asked)["operation"]
    op = finish(manager, op["pr"])
    assert op["status"] == "complete", op
    # The listed clone, under its listed name: the request named no clone.
    assert op["clone"] == str((manager.src / "repo").resolve())
    assert op["machine"] is None and op["branch"] == "sub"
    assert op["parent"] == asked
    assert op["started_by"] == f"Started by claude in {asked['cwd']}, session 0b6f3c1e"
    # A New task like any other: sandboxed, through wt's dashboard options.
    [call] = calls
    assert call[:2] == ["wt", "--codex"] and "--name" in call
    # The Workspaces tab finds who asked by the checkout's path.
    found = pw.subtask_parents(manager.home)
    assert found[(None, str(Path(op["path"]).resolve()))] == asked
    overview = workspace_overview.WorkspaceOverview(manager.home, src=manager.src)
    overview.parents = found
    item = {"path": str(Path(op["path"]).resolve()), "common": "/c/.git", "branch": "sub"}
    state = {"error": None, "changes": 0, "unpushed": 0}
    row = overview.row(item, state, [], False, [], [], set(), "base/repo", False)
    assert row["started_by"] == op["started_by"]
    assert "started_by" not in overview.row(
        {**item, "path": "/elsewhere"}, state, [], False, [], [], set(), "base/repo", False
    )


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"extra": 1}, "exactly the launch fields"),
        ({"docker": "yes"}, "Invalid launch fields"),
        ({"model": 5}, "Invalid launch fields"),
        ({"name": "a\x1bb"}, "Invalid launch fields"),
        ({"machine": "Chonk"}, "Name a machine, or auto"),
        ({"repo": "nope"}, "Choose a repository"),
        ({"repo": "other/repo"}, "This machine has no clone of other/repo"),
        ({"machine": "elsewhere"}, "Unknown machine elsewhere"),
        ({"name": "a b"}, "Name the new branch"),
        ({"task": ""}, "Supply a task"),
    ],
)
def test_a_subtask_is_checked_like_a_new_task_and_more(local, change, message):  # noqa: F811
    manager, *_ = local
    request = launch(**change)
    if "extra" in change:
        request = {**launch(), "extra": 1}
    with pytest.raises(ValueError, match=message):
        manager.subtask(request, parent())
    assert not [op for op in manager.state()["operations"].values() if op.get("parent")]


def test_docker_only_for_a_parent_that_has_it(local):  # noqa: F811
    manager, *_ = local
    with pytest.raises(ValueError, match="Only a launch with Docker starts sub-tasks with Docker"):
        manager.subtask(launch(docker=True), parent(docker=False))
    op = manager.subtask(launch(docker=True), parent(docker=True))["operation"]
    assert finish(manager, op["pr"])["docker"] is True


def test_a_parent_launch_and_its_machine_start_only_so_many_an_hour(local, monkeypatch):  # noqa: F811
    manager, *_ = local
    here = parent()["cwd"]
    for _ in range(pw.SUBTASK_RATE):
        record(manager, here)
    # An hour ago no longer counts, nor does another machine's launch in the same place.
    record(manager, here, created=time.time() - pw.SUBTASK_WINDOW - 1)
    record(manager, here, machine="chonk")
    with pytest.raises(worker_api.RateLimited, match="This launch started 6 sub-tasks"):
        manager.subtask(launch(), parent())
    # Another launch on this machine may still start one.
    op = manager.subtask(launch(), parent(cwd="/home/u/src/worktrees/repo/other"))["operation"]
    finish(manager, op["pr"])
    monkeypatch.setattr(pw, "SUBTASK_MACHINE_RATE", pw.SUBTASK_RATE + 1)
    with pytest.raises(worker_api.RateLimited, match="Launches there started 7 sub-tasks"):
        manager.subtask(launch(), parent(cwd="/third"))


def test_a_subtask_never_takes_a_full_machine_or_too_many_subtask_slots(local):  # noqa: F811
    manager, *_ = local
    # Full: Auto would still start a person's task here; a sub-task waits.
    pw.dashboard_settings.save(manager.home, {"local_max_agents": 1})
    manager.inventory = {**manager.inventory, "workspaces": [{"agent_status": "working"}]}
    with pytest.raises(ValueError, match=r"^This machine: full \(1/1\); a sub-task waits"):
        manager.subtask(launch(), parent())
    pw.dashboard_settings.save(manager.home, {"local_max_agents": 20})
    # Sub-task agents still running here: launches under way, and checkouts with an agent.
    live = manager.src / "worktrees/repo/live"
    live.mkdir(parents=True)
    manager.inventory = {
        **manager.inventory,
        "workspaces": [
            {"agent_status": "idle", "worktree": {"checkout_path": str(live)}},
            {"agent_status": None, "worktree": {"checkout_path": "/gone"}},
        ],
    }
    for status in ("queued", "running", "running"):
        record(manager, f"/p/{status}", status=status)
    record(manager, "/p/live", path=str(live), created=time.time() - 7200)
    record(manager, "/p/gone", path="/gone")
    record(manager, "/p/elsewhere", status="running", target="chonk")
    with pytest.raises(ValueError, match="This machine already runs 4 sub-task agents"):
        manager.subtask(launch(), parent())


def test_a_subtask_goes_to_the_worker_named_and_records_its_parent_there(remote):  # noqa: F811
    manager, _, state, _ = remote
    manager.loads.snapshot()
    from test_machine_loads import settle

    settle(manager.loads)
    asked = parent(machine="mini", machine_label="Mac mini", sandbox="bwrap", agent="codex")
    op = manager.subtask(launch(machine="mini"), asked, wait=30)["operation"]
    assert op["status"] == "complete", op
    assert op["machine"] == "mini" and op["result"]["machine_label"] == "Mac mini"
    assert op["started_by"].endswith(" on Mac mini, session 0b6f3c1e")
    assert pw.subtask_parents(manager.home)[("mini", op["path"])] == asked
    # The worker's herdr now lists that agent in its checkout, which counts as running.
    data = json.loads(Path(state).read_text())
    for space in data["machines"]["Mac mini"]["workspaces"]:
        space["agent_status"] = "working"
    Path(state).write_text(json.dumps(data))
    manager.loads.snapshot(now=time.time() + pw.placement.LOAD_TTL + 1)
    settle(manager.loads)
    assert op["path"] in manager.loads.agent_paths["mini"]
    ops = [o for o in manager.state()["operations"].values() if o.get("parent")]
    assert manager.subtask_agents("mini", ops) == 1
    assert manager.subtask_agents("local", ops) == 0


# --- the endpoints --------------------------------------------------------------------


class Recorder:
    """The dashboard's workspaces, recording each sub-task it is asked for."""

    def __init__(self, answer=None):
        self.asked = []
        self.answer = answer

    def subtask(self, launch, parent, wait=0):
        self.asked.append((launch, parent, wait))
        if isinstance(self.answer, Exception):
            raise self.answer
        return {"operation": self.answer}


@pytest.fixture
def served_with(box):  # noqa: F811
    started = []

    def start(workspaces):
        server = dashboard.DashboardServer(box["home"], 0, workspaces=workspaces)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        started.append(server)
        return server

    yield start
    for server in started:
        server.shutdown()
        server.server_close()


DONE = {
    "id": "x",
    "pr": "new:1",
    "status": "complete",
    "message": "Workspace ready on Box",
    "machine": "box",
    "log": "a long log " * 1000,
    "result": {"branch": "sub", "path": "/w/sub", "machine_label": "Box", "url": None, "x": 1},
}


def worker_parent(**changes):
    return {
        "cwd": "/home/w/src/worktrees/repo/cron",
        "session": SID.upper(),
        "agent": "codex",
        "rollout": "/home/w/.codex/sessions/2026/11/01/rollout-x.jsonl",
        "docker": True,
        **changes,
    }


def test_a_worker_launch_needs_its_token_and_exactly_the_launch_fields(
    box,  # noqa: F811
    served_with,
    monkeypatch,
):
    monkeypatch.setattr(worker_api, "RATE", 100)  # Many refusals in a row, on purpose.
    workspaces = Recorder(DONE)
    server = served_with(workspaces)
    token = worker_api.issue(box["home"], "box")
    auth = {"Authorization": f"Bearer {token}"}
    body = {"launch": launch(), "parent": worker_parent()}
    assert post(server, "/api/worker/launch", body)[0] == 403
    browser = {**auth, "Origin": f"http://127.0.0.1:{server.server_port}"}
    assert post(server, "/api/worker/launch", body, browser)[0] == 403
    assert post(server, "/api/worker/launch", body, {**auth, "Host": "evil.test"})[0] == 403
    for bad, message in (
        ({**body, "machine": "x"}, "exactly the launch and parent fields"),
        ({**body, "parent": {**worker_parent(), "machine": "x"}}, "exactly the parent fields"),
        ({**body, "parent": worker_parent(cwd="rel")}, "absolute"),
        ({**body, "parent": worker_parent(session="x")}, "Invalid parent session"),
        ({**body, "parent": worker_parent(docker="yes")}, "Invalid parent Docker grant"),
        ({**body, "parent": worker_parent(agent="sh")}, "Invalid parent agent"),
    ):
        status, answer = post(server, "/api/worker/launch", bad, auth)
        assert status == 400 and message in answer["error"], answer
    assert workspaces.asked == []
    status, answer = post(server, "/api/worker/launch", body, auth)
    assert status == 200, answer
    # Without the log, which may be long, and only the result's fields wt reports.
    assert answer == {
        "operation": {
            "pr": "new:1",
            "status": "complete",
            "message": "Workspace ready on Box",
            "machine": "box",
            "result": {"branch": "sub", "path": "/w/sub", "machine_label": "Box", "url": None},
        }
    }
    [(sent, asked, wait)] = workspaces.asked
    assert sent == launch() and wait == pw.SUBTASK_WAIT
    # The token decides the machine the parent is on.
    assert asked == {
        "machine": "box",
        "machine_label": "Box",
        "cwd": "/home/w/src/worktrees/repo/cron",
        "session": SID,
        "agent": "codex",
        "rollout": "/home/w/.codex/sessions/2026/11/01/rollout-x.jsonl",
        "docker": True,
        "sandbox": "bwrap",
    }
    workspaces.answer = worker_api.RateLimited("This launch started 6 sub-tasks")
    assert post(server, "/api/worker/launch", body, auth)[0] == 429
    without = served_with(None)
    status, answer = post(without, "/api/worker/launch", body, auth)
    assert status == 400 and "not enabled" in answer["error"]


def test_safehouse_asks_this_dashboard_with_what_it_claims(box, served_with):  # noqa: F811
    workspaces = Recorder(DONE)
    server = served_with(workspaces)
    action = {"X-Babysit-Action": "workspace-subtask"}
    claims = {"cwd": "/Users/u/src/worktrees/repo/cron", "session": SID, "agent": "claude"}
    body = {"launch": launch(), "parent": {**claims, "docker": False}}
    status, answer = post(server, "/api/workspace-subtask", body, action)
    assert status == 200 and answer["operation"]["log"] == DONE["log"]
    [(sent, asked, wait)] = workspaces.asked
    assert sent == launch() and wait == 0
    assert asked == {
        **claims,
        "machine": None,
        "machine_label": None,
        "rollout": None,
        "docker": False,
        "sandbox": "safehouse",
    }
    # The dashboard's own guard: never a browser of another origin, never without it.
    assert post(server, "/api/workspace-subtask", body)[0] == 403
    cross = {**action, "Origin": "http://evil.test"}
    assert post(server, "/api/workspace-subtask", body, cross)[0] == 403
    for bad in (
        {**body, "machine": "x"},
        {**body, "parent": {**claims, "docker": False, "machine": "chonk"}},
        {**body, "parent": {**claims, "docker": 1}},
        {**body, "parent": {**claims, "docker": False, "session": "nope"}},
    ):
        assert post(server, "/api/workspace-subtask", bad, action)[0] == 400
    assert len(workspaces.asked) == 1


# --- the broker -----------------------------------------------------------------------


@pytest.fixture
def launching_broker(box, served_with):  # noqa: F811
    """A broker for a Codex launch whose babysit-bwrap ran in its worktree with Docker,
    forwarding to a dashboard whose workspaces record sub-tasks."""
    worker = box["worker"]
    workspaces = Recorder(DONE)
    server = served_with(workspaces)
    marker = worker / ".config/babysit-pr/worker.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    token = worker_api.issue(box["home"], "box")
    url = f"http://127.0.0.1:{server.server_port}"
    marker.write_text(json.dumps({"version": 1, "controller": url, "token": token}))
    runtime = tempfile.mkdtemp(dir="/tmp")  # Unix socket paths are short.
    started = []

    def start(*options):
        listen = Path(runtime) / f"b{len(started)}" / "register.sock"
        listen.parent.mkdir(mode=0o700)
        argv = [sys.executable, "-I", str(BROKER), *options, str(listen), "codex"]
        process = subprocess.Popen(
            [*argv, str(worker / ".codex"), str(box["tree"])],
            env={**os.environ, "HOME": str(worker)},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
        )
        started.append(process)
        assert process.stdout.readline() == b"ready\n"
        return listen

    yield start, workspaces
    for process in started:
        process.terminate()
        process.wait(timeout=10)


def test_the_broker_adds_what_the_sandbox_cannot_forge(box, launching_broker):  # noqa: F811
    start, workspaces = launching_broker
    worker, sid = box["worker"], box["sid"]
    listen = start(f"--cwd={box['tree']}", "--granted=docker,gpg")
    request = {"action": "launch", "launch": launch(), "sessions": {"claude": None, "codex": sid}}
    for bad, message in (
        ({**request, "launch": {**launch(), "x": 1}}, "exactly the launch fields"),
        ({**request, "cwd": "/elsewhere"}, "exactly the launch fields"),
        ({**request, "sessions": {"codex": None}}, "own codex conversation"),
        ({**request, "sessions": {"codex": str(uuid.uuid4())}}, "not one of this sandbox's own"),
    ):
        assert message in ask(listen, bad)["error"]
    assert workspaces.asked == []
    answer = ask(listen, request)
    assert answer["operation"]["status"] == "complete", answer
    [(sent, asked, _)] = workspaces.asked
    assert sent == launch()
    assert asked["cwd"] == str(box["tree"]) and asked["docker"] is True
    assert asked["session"] == sid and asked["agent"] == "codex"
    assert asked["rollout"].startswith(str(worker.resolve() / ".codex/sessions"))
    assert (asked["machine"], asked["sandbox"]) == ("box", "bwrap")
    # At most one launch every 20 s from one sandbox.
    assert "moments ago" in ask(listen, request)["error"]
    # Without Docker granted to its launch, the parent has none.
    plain = start(f"--cwd={box['tree']}")
    ask(plain, request)
    assert workspaces.asked[-1][1]["docker"] is False
    # A broker that was not told its launch's directory starts nothing.
    unknown = start()
    assert "cannot start tasks" in ask(unknown, request)["error"]
    assert len(workspaces.asked) == 2


def test_wt_inside_a_worker_sandbox_reaches_the_controller_through_the_broker(
    box,  # noqa: F811
    launching_broker,
    tmp_path,
):
    start, workspaces = launching_broker
    listen = start(f"--cwd={box['tree']}")
    sandbox_home = tmp_path / "sandbox-home"  # Empty, as babysit-bwrap's tmpfs is.
    sandbox_home.mkdir()
    done = subprocess.run(
        [sys.executable, wt.__file__, "--codex", "-r", "base/repo", "-p", "Go", "main", "sub"],
        env={
            "PATH": os.environ["PATH"],
            "HOME": str(sandbox_home),
            "BABYSIT_WORKER": "1",
            "BABYSIT_REGISTER_SOCKET": str(listen),
            "CODEX_THREAD_ID": box["sid"],
        },
        cwd=box["tree"],
        text=True,
        capture_output=True,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr
    assert done.stderr.splitlines()[-1] == "wt: sub at /w/sub on Box"
    [(sent, asked, _)] = workspaces.asked
    assert (sent["base"], sent["name"], sent["machine"]) == ("main", "sub", "auto")
    assert asked["cwd"] == str(box["tree"])
