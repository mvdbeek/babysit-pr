"""Sub-tasks agents start from their sandbox: the controller's rules, endpoints and broker.

``wt`` inside a sandbox hands a new task to the controller (see ``tests/test_wt.py``):
from Safehouse here to ``/api/workspace-subtask``, from a worker's babysit-bwrap through
its launch's broker to ``/api/worker/launch``. Both end in ``Workspaces.subtask``.
"""

import http.server
import json
import os
import sqlite3
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
        "agent": "codex",
        "config_dir": "/home/u/.codex",
        "rollout": None,
        "docker": False,
        "sandbox": "safehouse",
        **changes,
    }


def record(
    manager, cwd, machine=None, status="complete", created=None, target=None, path=None, **lineage
):
    """An earlier sub-task's operation, as ``subtask`` records them."""
    key = f"{pw.NEW_PREFIX}{uuid.uuid4()}"
    op = {
        "id": str(uuid.uuid4()),
        "pr": key,
        "status": status,
        "machine": target,
        "path": path,
        "created_at": time.time() if created is None else created,
        "parent": parent(cwd=cwd, machine=machine, **lineage),
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
    # Its lineage starts at the launch asking, which was no sub-task.
    assert op["parent"] == {**asked, "root": f"local:{asked['cwd']}", "depth": 1}
    assert op["started_by"] == f"Started by codex in {asked['cwd']}, session 0b6f3c1e"
    # A New task like any other: sandboxed, through wt's dashboard options.
    [call] = calls
    assert call[:2] == ["wt", "--codex"] and "--name" in call
    # The Workspaces tab finds who asked by the checkout's path.
    found = pw.subtask_parents(manager.home)
    assert found[(None, str(Path(op["path"]).resolve()))] == op["parent"]
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


def captured(manager):
    """What ``subtask`` hands New task, which then starts nothing."""
    asked = []

    def new_task(request, parent=None, room=None, **_):
        asked.append((request, parent))
        status = "queued" if room is None else "waiting"
        return {"operation": {"pr": "new:x", "status": status, "parent": parent, "room": room}}

    manager.new_task = new_task
    return asked


def test_a_subtask_runs_the_parents_agent_as_its_claude_account(local):  # noqa: F811
    manager, *_ = local
    asked = captured(manager)
    with pytest.raises(ValueError, match="A sub-task runs codex, as the launch asking does"):
        manager.subtask(launch(agent="claude"), parent())
    named = parent(agent="claude", config_dir="/home/u/.claude/accounts/oss")
    with pytest.raises(ValueError, match="A sub-task runs claude, as the launch asking does"):
        manager.subtask(launch(), named)
    with pytest.raises(ValueError, match="runs as Claude account oss, as the launch asking does"):
        manager.subtask(launch(agent="claude", claude_account="work"), named)
    with pytest.raises(ValueError, match="runs as Claude account default"):
        manager.subtask(
            launch(agent="claude", claude_account="oss"),
            parent(agent="claude", config_dir="/home/u/.claude"),
        )
    with pytest.raises(ValueError, match="outside its accounts"):
        manager.subtask(launch(agent="claude"), parent(agent="claude", config_dir="/elsewhere"))
    assert asked == []
    # None named: the parent's, filled in; the default account stays unnamed.
    manager.subtask(launch(agent="claude"), named)
    manager.subtask(launch(agent="claude", claude_account="oss"), named)
    manager.subtask(
        launch(agent="claude", claude_account="default"),
        parent(agent="claude", config_dir="/home/u/.claude"),
    )
    assert [request["claude_account"] for request, _ in asked] == ["oss", "oss", ""]


def test_cross_agent_review_refusal_explains_the_trusted_launch_route(local):  # noqa: F811
    manager, *_ = local
    asked = captured(manager)
    for agent, account in (("codex", ""), ("claude", "reviewer")):
        other = "claude" if agent == "codex" else "codex"
        with pytest.raises(ValueError) as error:
            manager.subtask(
                launch(agent=other, task="The user explicitly requested a cross-agent review"),
                parent(
                    agent=agent,
                    config_dir=f"/home/u/.{agent}" + (f"/accounts/{account}" if account else ""),
                ),
            )
        message = str(error.value)
        assert "same agent and account" in message
        assert "wt --review" in message and "authenticated approval" in message
        assert "isolated configuration" in message and "re-login" in message
    assert asked == []
    assert not [op for op in manager.state()["operations"].values() if op.get("parent")]


def test_a_workers_subtask_never_lands_on_the_controller(local):  # noqa: F811
    manager, *_ = local
    asked = captured(manager)
    from_worker = parent(machine="chonk", machine_label="Chonk", sandbox="bwrap")
    with pytest.raises(ValueError, match="runs on a worker, never on the controller"):
        manager.subtask(launch(machine="local"), from_worker)
    # Auto would put it here, the only machine there is: refused.
    with pytest.raises(ValueError, match="^No worker can take this sub-task now"):
        manager.subtask(launch(), from_worker)
    assert asked == []
    # This machine's own agents may still start them here.
    manager.subtask(launch(), parent())
    assert asked[0][0]["machine"] == "local"


def test_a_lineage_goes_only_so_deep_and_starts_only_so_many(local):  # noqa: F811
    manager, *_ = local
    asked = captured(manager)
    first = "/home/u/src/worktrees/repo/cron"
    child = str((manager.src / "worktrees/repo/child").resolve())
    grandchild = str((manager.src / "worktrees/repo/grandchild").resolve())
    # The parent asking runs in a sub-task's checkout: the lineage goes on.
    record(manager, first, path=child, root=f"local:{first}", depth=1)
    manager.subtask(launch(), parent(cwd=child))
    assert asked[-1][1]["root"] == f"local:{first}" and asked[-1][1]["depth"] == 2
    record(manager, child, path=grandchild, root=f"local:{first}", depth=2)
    with pytest.raises(ValueError, match="at most 2 levels below the first launch"):
        manager.subtask(launch(), parent(cwd=grandchild))
    # Six an hour for the whole lineage, whichever of its launches asks.
    for _ in range(pw.SUBTASK_RATE - 2):
        record(manager, child, root=f"local:{first}", depth=2)
    with pytest.raises(worker_api.RateLimited, match="This lineage started 6 sub-tasks"):
        manager.subtask(launch(), parent(cwd=child))
    with pytest.raises(worker_api.RateLimited, match="This lineage started 6 sub-tasks"):
        manager.subtask(launch(), parent(cwd=first))
    assert len(asked) == 1


def test_a_recorded_launch_is_never_answered_as_a_failure(local, monkeypatch):  # noqa: F811
    manager, *_ = local
    op = {"pr": "new:x", "status": "running", "message": "Branching"}

    def unreadable(key):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(manager, "operation", unreadable)
    found = manager.await_operation(op, 5)
    assert found["status"] == "uncertain" and "could not be read" in found["message"]
    done = {**op, "status": "complete"}
    assert manager.await_operation(done, 5) == done


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
    with pytest.raises(worker_api.RateLimited, match="This lineage started 6 sub-tasks"):
        manager.subtask(launch(), parent())
    # Another launch on this machine may still start one.
    op = manager.subtask(launch(), parent(cwd="/home/u/src/worktrees/repo/other"))["operation"]
    finish(manager, op["pr"])
    monkeypatch.setattr(pw, "SUBTASK_MACHINE_RATE", pw.SUBTASK_RATE + 1)
    with pytest.raises(worker_api.RateLimited, match="Launches there started 7 sub-tasks"):
        manager.subtask(launch(), parent(cwd="/third"))


def test_a_subtask_waits_for_a_full_machine_or_too_many_subtask_slots(local):  # noqa: F811
    manager, *_ = local
    # Full: Auto would still start a person's task here; a sub-task waits for room.
    pw.dashboard_settings.save(manager.home, {"local_max_agents": 1})
    manager.inventory = {**manager.inventory, "workspaces": [{"agent_status": "working"}]}
    op = manager.subtask(launch(), parent())["operation"]
    assert op["status"] == "waiting", op
    assert op["message"] == "Waiting for room: This machine: full (1/1)"
    assert op["machine"] is None and op["parent"]["depth"] == 1
    pw.dashboard_settings.save(manager.home, {"local_max_agents": 20})
    manager.cancel_waiting(op["pr"])
    # Working sub-task agents here: launches under way, and checkouts with a working
    # agent; one idle agent left open does not count.
    live = manager.src / "worktrees/repo/live"
    idle = manager.src / "worktrees/repo/idle"
    live.mkdir(parents=True)
    idle.mkdir(parents=True)
    manager.inventory = {
        **manager.inventory,
        "workspaces": [
            {"agent_status": status, "worktree": {"checkout_path": path, "repo_root": "/r"}}
            for status, path in (("working", str(live)), ("idle", str(idle)), (None, "/gone"))
        ],
    }
    for status in ("queued", "running"):
        record(manager, f"/p/{status}", status=status)
    record(manager, "/p/live", path=str(live), created=time.time() - 7200)
    record(manager, "/p/idle", path=str(idle))
    record(manager, "/p/gone", path="/gone")
    record(manager, "/p/elsewhere", status="running", target="chonk")
    asked = captured(manager)
    manager.subtask(launch(), parent())  # Three: there is room for one more.
    assert len(asked) == 1
    record(manager, "/p/running", status="running")
    waited = manager.subtask(launch(), parent())["operation"]
    assert waited["status"] == "waiting"
    assert str(waited["room"]) == "This machine already runs 4 working sub-task agents"


def test_a_subtask_goes_to_the_worker_named_and_records_its_parent_there(remote):  # noqa: F811
    manager, _, state, _ = remote
    manager.loads.snapshot()
    from test_machine_loads import settle

    settle(manager.loads)
    asked = parent(machine="mini", machine_label="Mac mini", sandbox="bwrap")
    op = manager.subtask(launch(machine="mini"), asked)["operation"]
    op = manager.await_operation(op, 30)
    assert op["status"] == "complete", op
    assert op["machine"] == "mini" and op["result"]["machine_label"] == "Mac mini"
    assert op["started_by"].endswith(" on Mac mini, session 0b6f3c1e")
    recorded = pw.subtask_parents(manager.home)[("mini", op["path"])]
    assert recorded == {**asked, "root": f"mini:{asked['cwd']}", "depth": 1}
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
        self.waited = []
        self.answer = answer

    def subtask(self, launch, parent):
        self.asked.append((launch, parent))
        if isinstance(self.answer, Exception):
            raise self.answer
        return {"operation": self.answer}

    def await_operation(self, op, wait):
        self.waited.append(wait)
        return op


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
        "config_dir": "/home/w/.codex",
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
    [(sent, asked)] = workspaces.asked
    # The wait comes after the worker's request limits are released.
    assert sent == launch() and workspaces.waited == [pw.SUBTASK_WAIT]
    # The token decides the machine the parent is on.
    assert asked == {
        "machine": "box",
        "machine_label": "Box",
        "cwd": "/home/w/src/worktrees/repo/cron",
        "session": SID,
        "agent": "codex",
        "config_dir": "/home/w/.codex",
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
    claims = {
        "cwd": "/Users/u/src/worktrees/repo/cron",
        "session": SID,
        "agent": "claude",
        "config_dir": "/Users/u/.claude",
    }
    body = {"launch": launch(), "parent": {**claims, "docker": False}}
    status, answer = post(server, "/api/workspace-subtask", body, action)
    assert status == 200 and answer["operation"]["log"] == DONE["log"]
    [(sent, asked)] = workspaces.asked
    assert sent == launch() and workspaces.waited == []
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
        {**body, "parent": {**claims, "docker": False, "agent": None}},
        {**body, "parent": {**claims, "docker": False, "config_dir": "rel"}},
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
    # Started from a link to the worktree: the directory it names is the real one.
    link = box["worker"] / "link"
    link.symlink_to(box["tree"])
    listen = start(f"--cwd={link}", "--granted=docker,gpg")
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
    [(sent, asked)] = workspaces.asked
    assert sent == launch()
    assert asked["cwd"] == str(box["tree"]) and asked["docker"] is True
    assert asked["session"] == sid and asked["agent"] == "codex"
    assert asked["config_dir"] == str((worker / ".codex").resolve())
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
    nothing = tmp_path / "no-tools"  # Nothing to fall through to: wt runs no tool here.
    nothing.mkdir()
    done = subprocess.run(
        [sys.executable, wt.__file__, "--codex", "-r", "base/repo", "-p", "Go", "main", "sub"],
        env={
            "PATH": str(nothing),
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
    [(sent, asked)] = workspaces.asked
    assert (sent["base"], sent["name"], sent["machine"]) == ("main", "sub", "auto")
    assert asked["cwd"] == str(box["tree"])


class Failing(http.server.BaseHTTPRequestHandler):
    """A controller, or the proxy before it, answering every request with ``status``."""

    status = 502

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        body = json.dumps({"error": "Bad gateway"}).encode()
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.mark.parametrize(("status", "uncertain"), [(502, True), (503, True), (400, False)])
def test_a_failing_controller_leaves_a_launch_uncertain_never_refused(
    tmp_path, monkeypatch, status, uncertain
):
    handler = type("Handler", (Failing,), {"status": status})
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        marker = tmp_path / ".config/babysit-pr/worker.json"
        marker.parent.mkdir(parents=True)
        url = f"http://127.0.0.1:{server.server_port}"
        marker.write_text(json.dumps({"controller": url, "token": "t" * 43}))
        monkeypatch.setenv("HOME", str(tmp_path))
        failure = RuntimeError if uncertain else ValueError
        with pytest.raises(failure):
            worker_api.post("launch", {})
        # Other actions start nothing, so a failure is only a refusal.
        with pytest.raises(ValueError, match="The controller refused"):
            worker_api.post("register", {})
    finally:
        server.shutdown()
        server.server_close()


def test_a_subtasks_record_outlives_the_day_new_tasks_are_listed(local):  # noqa: F811
    manager, *_ = local
    old = time.time() - pw.NEW_LISTED - 60
    kept = record(manager, "/p/kept", created=old)
    gone = record(manager, "/p/gone", created=time.time() - pw.SUBTASK_KEPT - 60)
    plain = {"id": "y", "pr": f"{pw.NEW_PREFIX}plain", "status": "complete", "created_at": old}
    with manager.db() as db:
        db.execute("INSERT INTO operations VALUES (?,?)", (plain["pr"], json.dumps(plain)))
    manager.pruned_at = 0
    manager.new_operations(manager.state())
    left = manager.state()["operations"]
    assert kept["pr"] in left and gone["pr"] not in left and plain["pr"] not in left
