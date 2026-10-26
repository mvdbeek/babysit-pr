"""Handoff of a worker's watch: asked there, scheduled and performed on the controller.

The worker is a directory under ``<tmp>/hosts/<ssh target>`` that the fake ssh uses as
HOME; the controller follows its agent through the fake herdr's store for its label and
reads its transcript there through the probe.
"""

import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import herdr_handoff
import pr_supervisor as supervisor
import pytest
import worker_api
from test_remote_cron import git, rollout, short_tmp  # noqa: F401 (fixture)
from test_remote_registration import (  # noqa: F401 (fixtures)
    box,
    claude_transcript,
    fresh_rate_limits,
    post,
    register,
    served,
)
from test_worker_broker import broker  # noqa: F401 (fixture)

PANE = "w1:p1"
EMPTY = "› \x1b[2mAsk Codex to do anything\x1b[0m"
CLAUDE_EMPTY = "─" * 20 + "\n❯ \n" + "─" * 20
CONTEXT = {
    "type": "turn_context",
    "payload": {"model": "gpt-fixture", "sandbox_policy": {"type": "workspace-write"}},
}
STARTED = {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "t1"}}


def bwrap(cwd):
    """The processes babysit-bwrap puts before a worker's agent in its pane's foreground."""
    argv = ["bwrap", "--die-with-parent", "--unshare-pid", "--", "claude"]
    return [
        {"pid": 4240, "name": "bwrap", "argv": argv, "cwd": cwd},
        {"pid": 4241, "name": "bwrap", "argv": argv, "cwd": cwd},
    ]


def worker_herdr(fake_tools, tree, agent="codex"):
    """The worker's herdr: one idle agent in PANE, its shell and process there."""
    store = json.loads(fake_tools.read_text())
    store["machines"] = {
        "Box": {
            "workspaces": [],
            "agents": [{"pane_id": PANE, "agent": agent, "cwd": str(tree), "agent_status": "idle"}],
            "calls": [],
            "process_info": {
                PANE: {
                    "shell_pid": 100,
                    # The agent runs in babysit-bwrap: bwrap, its child, then the agent.
                    "foreground_processes": [
                        {"pid": 100},
                        *bwrap(str(tree)),
                        {"pid": 4242, "name": agent, "argv": [agent], "cwd": str(tree)},
                    ],
                }
            },
        }
    }
    fake_tools.write_text(json.dumps(store))


def worker_store(fake_tools):
    return json.loads(fake_tools.read_text())["machines"]["Box"]


def screens(fake_tools, *screens):
    store = json.loads(fake_tools.read_text())
    store["machines"]["Box"]["screens"] = {PANE: list(screens)}
    fake_tools.write_text(json.dumps(store))


@pytest.fixture
def watched(box, fake_tools, monkeypatch):  # noqa: F811
    """A Codex watch registered on the worker, its agent in PANE mid-turn."""
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    path = rollout(box["worker"], box["tree"], box["sid"], CONTEXT, STARTED)
    worker_herdr(fake_tools, box["tree"])
    job = register(box, pane=PANE, headless=False)
    spawned = []
    # The dashboard answers in this process: perform is run by each test itself.
    monkeypatch.setattr(supervisor, "start_daemon", lambda *a: None)
    monkeypatch.setattr(
        herdr_handoff,
        "subprocess",
        SimpleNamespace(
            Popen=lambda argv, **kw: spawned.append(argv),
            DEVNULL=subprocess.DEVNULL,
            STDOUT=subprocess.STDOUT,
        ),
    )
    return {**box, "job": job, "rollout": path, "spawned": spawned}


def asked(watched, **changes):
    return {
        "id": watched["job"]["id"],
        "pane": PANE,
        "timeout": 60,
        "sessions": {"codex": watched["sid"], "claude": None},
        "cwd": str(watched["tree"]),
        "rollout": None,
        **changes,
    }


def job(watched):
    db = supervisor.open_db(watched["home"])
    try:
        return supervisor.get_job(db, watched["job"]["id"])
    finally:
        db.close()


def target(watched):
    [path] = (watched["home"] / "handoffs").glob("*.json")
    return json.loads(path.read_text())


def test_a_worker_hands_its_watch_to_the_controller_which_exits_its_agent_there(
    watched,
    served,  # noqa: F811
    fake_tools,
):
    auth = {"Authorization": f"Bearer {worker_api.issue(watched['home'], 'box')}"}
    status, answer = post(served, "/api/worker/handoff", asked(watched), auth)
    assert status == 200, answer
    assert answer["status"] == "handoff" and "audit_file" not in answer
    assert job(watched)["status"] == "handoff"
    handed = target(watched)
    assert (handed["machine"], handed["cwd"], handed["agent_pid"]) == (
        "box",
        str(watched["tree"]),
        4242,
    )
    assert watched["spawned"][0][-1] == handed["token"]
    done = {
        "type": "event_msg",
        "payload": {
            "type": "task_complete",
            "turn_id": "t1",
            "last_agent_message": f"Handing off.\n{answer['final_response_marker']}",
        },
    }
    with watched["rollout"].open("a") as stream:
        stream.write(json.dumps(done) + "\n")
    screens(fake_tools, f"{answer['final_response_marker']}\n{EMPTY}")
    db = supervisor.open_db(watched["home"])
    try:
        herdr_handoff.perform(handed, watched["home"], db)
    finally:
        db.close()
    assert job(watched)["status"] == "watching"
    worker = worker_store(fake_tools)
    assert [c for c in worker["calls"] if c[0] == "agent" and c[1] == "send-keys"] == [
        ["agent", "send-keys", PANE, "ctrl+d"]
    ]
    # Every herdr call went to the worker's server.
    calls = json.loads(fake_tools.read_text())["calls"]
    assert all(call[0] == "--machine=Box" for call in calls)


@pytest.mark.parametrize(
    "changes",
    [
        {"sessions": {"codex": "00000000-0000-4000-8000-000000000000", "claude": None}},
        {"cwd": "/elsewhere"},
        {"sessions": {"codex": None, "claude": "00000000-0000-4000-8000-000000000000"}},
        {"id": "0" * 16},
        # The transcript the broker found must be the watch's own, not one beside it.
        {"rollout": "/home/babysit/.codex/sessions/2026/10/25/rollout-x.jsonl"},
    ],
)
def test_a_worker_hands_off_only_its_own_conversations_watch(watched, served, changes):  # noqa: F811
    auth = {"Authorization": f"Bearer {worker_api.issue(watched['home'], 'box')}"}
    status, answer = post(served, "/api/worker/handoff", asked(watched, **changes), auth)
    assert status == 400 and "No watch" in answer["error"], answer
    assert job(watched)["status"] == "awaiting_release" and not watched["spawned"]


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"id": "../x"}, "Invalid watch"),
        ({"pane": "w1:p1\x15"}, "control characters"),
        ({"timeout": 10**6}, "Invalid timeout"),
        ({"cwd": "relative"}, "absolute"),
        ({"machine": "box"}, "handoff fields"),
        ({"sessions": {"codex": None, "claude": None}}, "Invalid sessions"),
        ({"sessions": {"codex": "x"}}, "Invalid sessions"),
    ],
)
def test_a_workers_handoff_request_is_checked_strictly(watched, served, changes, message):  # noqa: F811
    auth = {"Authorization": f"Bearer {worker_api.issue(watched['home'], 'box')}"}
    status, answer = post(served, "/api/worker/handoff", asked(watched, **changes), auth)
    assert status == 400 and message in answer["error"], answer
    assert not watched["spawned"]


def test_a_workers_watch_is_never_handed_off_here_nor_a_local_one_from_a_worker(
    watched, tmp_path, monkeypatch
):
    db = supervisor.open_db(watched["home"])
    try:
        monkeypatch.setenv("CODEX_THREAD_ID", watched["sid"])
        here = supervisor.argparse.Namespace(id=watched["job"]["id"], pane=PANE, timeout=60)
        with pytest.raises(ValueError, match="is on box; hand it off from there"):
            herdr_handoff.schedule(db, watched["home"], here)
        with db:
            current = supervisor.get_job(db, watched["job"]["id"])
            supervisor.save_job(db, {**current, "machine": None})
        machine = supervisor.machines.resolve(watched["home"], "box")
        args = worker_api.handoff_args(machine, asked(watched))
        with pytest.raises(ValueError, match="No watch"):
            herdr_handoff.schedule(db, watched["home"], args)
    finally:
        db.close()


def claude_target(watched, **changes):
    return {
        "job_id": watched["job"]["id"],
        "agent": "claude",
        "pane_id": PANE,
        "terminal_id": "terminal",
        "shell_pid": 100,
        "agent_pid": 4242,
        "agent_argv": ["claude"],
        "machine": "box",
        **changes,
    }


def test_a_claude_on_a_worker_is_asked_to_exit_with_a_typed_line(watched, fake_tools):
    worker_herdr(fake_tools, watched["tree"], "claude")
    typed = "─" * 20 + "\n❯ /exit\n" + "─" * 20 + "\n  /exit  Exit the REPL"
    screens(fake_tools, CLAUDE_EMPTY, typed)
    machine = supervisor.machines.resolve(watched["home"], "box")
    herdr_handoff.send_exit(
        claude_target(watched),
        None,
        None,
        lambda: None,
        lambda *a, **k: None,
        machine,
        watched["home"],
    )
    sent = [c for c in worker_store(fake_tools)["calls"] if c[0] in {"pane", "agent"}]
    sent = [c for c in sent if c[1] in {"send-text", "send-keys"}]
    assert sent == [["pane", "send-text", PANE, "/exit"], ["agent", "send-keys", PANE, "enter"]]


@pytest.mark.parametrize(
    "shown, removed",
    [
        # A dialog opened meanwhile: no key goes to it.
        ("Allow this command?\n❯ 1. Yes\n  2. No", False),
        # Someone typed too: only the /exit typed here is taken back.
        ("─" * 20 + "\n❯ draft/exit\n" + "─" * 20, True),
    ],
)
def test_enter_follows_a_typed_exit_only_once_the_composer_holds_exactly_it(
    watched, fake_tools, monkeypatch, shown, removed
):
    monkeypatch.setattr(herdr_handoff, "TYPED_EXIT_WAIT", 0.5)
    monkeypatch.setenv("FAKE_HERDR_KEEP_AGENT", "1")
    worker_herdr(fake_tools, watched["tree"], "claude")
    screens(fake_tools, CLAUDE_EMPTY, shown)
    machine = supervisor.machines.resolve(watched["home"], "box")
    with pytest.raises(RuntimeError, match="Enter not sent") as raised:
        herdr_handoff.send_exit(
            claude_target(watched),
            None,
            None,
            lambda: None,
            lambda *a, **k: None,
            machine,
            watched["home"],
        )
    keys = [c for c in worker_store(fake_tools)["calls"] if c[:2] == ["agent", "send-keys"]]
    assert keys == ([["agent", "send-keys", PANE, "backspace"]] * 5 if removed else [])
    assert ("taken back" if removed else "left in the pane") in str(raised.value)


def test_an_agent_still_running_off_the_foreground_has_not_exited(watched, fake_tools, monkeypatch):
    monkeypatch.setattr(herdr_handoff, "EXIT_WAIT", 1)
    # Stopped or moved out of the terminal's foreground: the worker still runs it.
    alive = subprocess.Popen(["sleep", "60"])
    try:
        worker_herdr(fake_tools, watched["tree"])
        target = {
            **claude_target(watched),
            "agent": "codex",
            "agent_pid": alive.pid,
            "agent_argv": ["sleep", "60"],
        }
        machine = supervisor.machines.resolve(watched["home"], "box")
        with pytest.raises(RuntimeError, match="Exit unconfirmed"):
            herdr_handoff.send_exit(
                target, None, None, lambda: None, lambda *a, **k: None, machine, watched["home"]
            )
        # Gone, or another program at that pid: exited.
        target["agent_argv"] = ["codex"]
        herdr_handoff.send_exit(
            target, None, None, lambda: None, lambda *a, **k: None, machine, watched["home"]
        )
    finally:
        alive.kill()
        alive.wait(timeout=10)


def test_an_agent_started_with_a_long_brief_is_still_seen_running(watched, fake_tools):
    # wt passes the whole brief as one argument, often longer than any path.
    brief = "Review it carefully.\n" * 500
    argv = ["/bin/sh", "-c", "sleep 60; :", brief]
    assert len(brief) > 4096
    # Its own group: the sleep the shell starts is stopped with it, never left behind.
    alive = subprocess.Popen(argv, start_new_session=True, stdout=subprocess.DEVNULL)
    try:
        machine = supervisor.machines.resolve(watched["home"], "box")
        target = {"agent_pid": alive.pid, "agent_argv": argv}
        assert herdr_handoff.alive(target, machine, watched["home"]) is True
        other = {**target, "agent_argv": [*argv[:-1], brief + "!"]}
        assert herdr_handoff.alive(other, machine, watched["home"]) is False
    finally:
        os.killpg(alive.pid, signal.SIGKILL)
        alive.wait(timeout=10)
    assert herdr_handoff.alive(target, machine, watched["home"]) is False


def test_a_linux_workers_command_line_is_read_whole_from_proc(monkeypatch):
    import io

    import remote_probe

    brief = b"Review it carefully.\n" * 500
    shown = {"/proc/7/cmdline": b"claude\0--\0" + brief + b"\0", "/proc/8/cmdline": b""}

    def fake_open(path, mode):
        if path not in shown:
            raise FileNotFoundError(path)
        return io.BytesIO(shown[path])

    monkeypatch.setattr(remote_probe, "open", fake_open, raising=False)
    assert remote_probe._proc_arguments(7) == b"claude\0--\0" + brief
    assert remote_probe._proc_arguments(8) is None  # A zombie.
    assert remote_probe._proc_arguments(9) is None  # Gone.


def test_the_alive_probe_compares_the_process_command_line(watched):
    import hashlib

    import remote_probe

    def digest(argv):
        return hashlib.sha256("\0".join(argv).encode()).hexdigest()

    alive = subprocess.Popen(["sleep", "60"])
    try:
        found = remote_probe._host_alive(alive.pid, digest(["sleep", "60"]))
        assert remote_probe.validate_alive(found) == {"alive": True}
        assert remote_probe._host_alive(alive.pid, digest(["sleep", "61"])) == {"alive": False}
    finally:
        alive.kill()
        alive.wait(timeout=10)
    assert remote_probe._host_alive(alive.pid, digest(["sleep", "60"])) == {"alive": False}
    for bad in ((1, digest(["x"])), ("2", digest(["x"])), (os.getpid(), "x"), (os.getpid(), None)):
        with pytest.raises(ValueError):
            remote_probe._host_alive(*bad)
    with pytest.raises(ValueError, match="Malformed"):
        remote_probe.validate_alive({"alive": 1})


def test_a_worker_hands_off_no_watch_without_a_bound_pane(watched, served):  # noqa: F811
    db = supervisor.open_db(watched["home"])
    with db:
        current = supervisor.get_job(db, watched["job"]["id"])
        supervisor.save_job(db, {**current, "pane": None})
    db.close()
    auth = {"Authorization": f"Bearer {worker_api.issue(watched['home'], 'box')}"}
    status, answer = post(served, "/api/worker/handoff", asked(watched), auth)
    assert status == 400 and "registered with its pane" in answer["error"], answer
    assert job(watched)["status"] == "awaiting_release" and not watched["spawned"]


def test_a_worker_hands_off_only_the_pane_its_watch_is_bound_to(watched, served, fake_tools):  # noqa: F811
    store = json.loads(fake_tools.read_text())
    worker = store["machines"]["Box"]
    tree = str(watched["tree"])
    worker["agents"].append(
        {"pane_id": "w1:p2", "agent": "codex", "cwd": tree, "agent_status": "idle"}
    )
    worker["process_info"]["w1:p2"] = {
        "shell_pid": 300,
        "foreground_processes": [
            {"pid": 300},
            {"pid": 4343, "name": "codex", "argv": ["codex"], "cwd": tree},
        ],
    }
    fake_tools.write_text(json.dumps(store))
    auth = {"Authorization": f"Bearer {worker_api.issue(watched['home'], 'box')}"}
    status, answer = post(served, "/api/worker/handoff", asked(watched, pane="w1:p2"), auth)
    assert status == 400 and "bound pane" in answer["error"], answer
    assert not watched["spawned"]


def test_a_machine_runs_only_so_many_handoffs_at_once(watched, served, monkeypatch):  # noqa: F811
    monkeypatch.setattr(herdr_handoff, "MAX_REMOTE_HANDOFFS", 1)
    db = supervisor.open_db(watched["home"])
    with db:
        supervisor.save_job(db, {"id": "f" * 16, "machine": "box", "status": "handoff"})
    db.close()
    auth = {"Authorization": f"Bearer {worker_api.issue(watched['home'], 'box')}"}
    status, answer = post(served, "/api/worker/handoff", asked(watched), auth)
    assert status == 400 and "Too many handoffs running on Box" in answer["error"], answer
    assert job(watched)["status"] == "awaiting_release" and not watched["spawned"]


def test_a_failure_to_start_on_the_controller_tells_the_worker_no_paths(
    watched,
    served,  # noqa: F811
    monkeypatch,
):
    def broken(*args):
        raise RuntimeError(f"Supervisor failed to start; see {watched['home']}/supervisor.log")

    monkeypatch.setattr(supervisor, "start_daemon", broken)
    auth = {"Authorization": f"Bearer {worker_api.issue(watched['home'], 'box')}"}
    status, answer = post(served, "/api/worker/handoff", asked(watched), auth)
    assert status == 400 and str(watched["home"]) not in answer["error"], answer
    assert "controller" in answer["error"]


def test_one_machines_requests_share_a_bucket_whatever_worktree_they_name(
    watched,
    served,  # noqa: F811
    monkeypatch,
):
    monkeypatch.setattr(worker_api, "MACHINE_RATE", 4)
    auth = {"Authorization": f"Bearer {worker_api.issue(watched['home'], 'box')}"}
    codes = [
        post(served, "/api/worker/handoff", asked(watched, cwd=f"/nowhere/{n}"), auth)[0]
        for n in range(4)
    ]
    assert codes[0] == 400 and codes[-1] == 429


def test_rate_buckets_are_never_more_than_the_cap(monkeypatch):
    monkeypatch.setattr(worker_api, "_buckets", {})
    now = worker_api.time.monotonic()
    worker_api._buckets.update({f"k{n}": (0.0, now) for n in range(worker_api.MAX_KEYS)})
    assert worker_api._take("fresh") is False
    assert len(worker_api._buckets) == worker_api.MAX_KEYS


def test_a_handoff_names_both_conversations_and_the_controller_picks_the_watchs(
    watched, monkeypatch
):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "11111111-1111-4111-8111-111111111111")
    monkeypatch.setenv("CODEX_THREAD_ID", watched["sid"])
    args = supervisor.argparse.Namespace(id=watched["job"]["id"], pane=PANE, timeout=60)
    request = worker_api.handoff_request(args)
    assert request["sessions"] == {
        "claude": "11111111-1111-4111-8111-111111111111",
        "codex": watched["sid"],
    }
    machine = supervisor.machines.resolve(watched["home"], "box")
    db = supervisor.open_db(watched["home"])
    try:
        request["cwd"] = str(watched["tree"])
        found = herdr_handoff.schedule(
            db, watched["home"], worker_api.handoff_args(machine, request)
        )
    finally:
        db.close()
    assert found["status"] == "handoff"


def test_register_through_a_running_broker_is_sent_as_it_always_was(monkeypatch, short_tmp):  # noqa: F811
    import socket
    import threading

    listen = short_tmp / "b.sock"
    server = socket.socket(socket.AF_UNIX)
    server.bind(str(listen))
    server.listen()
    server.settimeout(30)  # Nothing here waits unbounded, whatever goes wrong.
    seen = []

    def serve():
        for _ in range(2):
            client, _ = server.accept()
            client.settimeout(30)
            with client:
                data = b""
                while chunk := client.recv(65536):
                    data += chunk
                seen.append(json.loads(data))
                client.sendall(b"{}")

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    monkeypatch.setenv(worker_api.SOCKET_ENV, str(listen))
    worker_api.forward("register", {"pr": "x"})
    worker_api.forward("handoff", {"id": "y"})
    thread.join(10)
    server.close()
    # A broker started before handoff existed expects exactly the registration fields.
    assert seen == [{"pr": "x"}, {"id": "y", "action": "handoff"}]


def test_handoff_inside_a_sandbox_goes_through_its_broker(watched, broker, tmp_path):  # noqa: F811
    listen = broker([str(watched["tree"])])
    sandbox_home = tmp_path / "sandbox-home"
    sandbox_home.mkdir()

    def handoff(cwd, **env):
        return subprocess.run(
            [
                sys.executable,
                str(supervisor.SCRIPT),
                "handoff",
                watched["job"]["id"],
                "--pane",
                PANE,
            ],
            env={
                **os.environ,
                "HOME": str(sandbox_home),
                "BABYSIT_WORKER": "1",
                worker_api.SOCKET_ENV: str(listen),
                **env,
            },
            cwd=cwd,
            text=True,
            capture_output=True,
            timeout=120,
        )

    refused = handoff(watched["tree"])
    assert refused.returncode == 1 and "registered Codex or Claude conversation" in refused.stdout
    elsewhere = watched["worker"] / "src" / "repo"
    refused = handoff(elsewhere, CODEX_THREAD_ID=watched["sid"])
    assert "worktree this sandbox was given" in json.loads(refused.stdout)["error"]
    # Another conversation of the other kind beside it is not the launch's to name.
    done = handoff(
        watched["tree"],
        CODEX_THREAD_ID=watched["sid"].upper(),
        CLAUDE_CODE_SESSION_ID="11111111-1111-4111-8111-111111111111",
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert json.loads(done.stdout)["status"] == "handoff"
    handed = target(watched)
    assert handed["machine"] == "box" and handed["session_id"] == watched["sid"]
    assert job(watched)["status"] == "handoff"


def test_the_broker_refuses_a_handoff_of_another_launchs_session(box, broker, fake_tools):  # noqa: F811
    import socket

    listen = broker([str(box["tree"])], config=box["worker"] / "empty-codex-home")
    request = {
        "action": "handoff",
        "id": "0" * 16,
        "pane": PANE,
        "timeout": 60,
        "sessions": {"codex": box["sid"], "claude": None},
        "cwd": str(box["tree"]),
        "rollout": None,
    }
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(60)
        client.connect(str(listen))
        client.sendall(json.dumps(request).encode())
        client.shutdown(socket.SHUT_WR)
        data = b""
        while chunk := client.recv(65536):
            data += chunk
    assert "not one of this sandbox's own" in json.loads(data)["error"]
    for name in ("pane", "extra"):
        bad = (
            {k: v for k, v in request.items() if k != name}
            if name == "pane"
            else {**request, name: 1}
        )
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(60)
            client.connect(str(listen))
            client.sendall(json.dumps(bad).encode())
            client.shutdown(socket.SHUT_WR)
            data = client.recv(65536)
        assert "handoff fields" in json.loads(data)["error"]
    assert Path(box["home"] / "handoffs").exists() is False


def test_a_process_is_named_by_argv0_or_else_its_first_argument():
    # herdr on macOS adds argv0; on Linux it reports only argv and name.
    assert herdr_handoff.program({"argv0": "/usr/local/bin/claude", "argv": ["x"]}) == "claude"
    assert (
        herdr_handoff.program({"argv": ["claude", "--effort", "high"], "name": "claude"})
        == "claude"
    )
    assert herdr_handoff.program({"argv": ["/usr/bin/bwrap", "--"]}) == "bwrap"
    assert herdr_handoff.program({"pid": 100}) == ""
