import json
import os
import pty
import shlex
import subprocess
import sys
import threading
from pathlib import Path

import pane_runner
import pr_supervisor as supervisor
import pytest
from test_pr_supervisor import snapshot


@pytest.fixture
def terminal(monkeypatch, tmp_path):
    state = {
        "pane": "w2:p3",
        "terminal": "original",
        "shell": 100,
        "foreground": [{"pid": 100}],
        "agent": None,
        "sent": [],
    }

    def api(*args):
        if args == ("pane", "list"):
            return {"panes": [{"pane_id": state["pane"], "terminal_id": state["terminal"]}]}
        if args[:2] == ("pane", "get"):
            return {
                "pane": {
                    "terminal_id": state["terminal"],
                    "foreground_cwd": str(tmp_path),
                    "agent": state["agent"],
                }
            }
        if args[:2] == ("pane", "process-info"):
            return {
                "process_info": {
                    "shell_pid": state["shell"],
                    "foreground_processes": state["foreground"],
                }
            }
        if args[:2] == ("pane", "run"):
            state["sent"].append(args)
            return ""  # herdr pane run is silent on success
        raise AssertionError(args)

    monkeypatch.setattr(pane_runner, "result", api)
    monkeypatch.setattr(pane_runner, "herdr", api)
    job = {
        "id": "watch",
        "attempt": "attempt",
        "cwd": str(tmp_path),
        "pane": {"pane_id": "w1:p1", "terminal_id": "original", "shell_pid": 100},
    }
    return state, job


def test_moves_follow_terminal_and_shell_arguments_are_quoted(terminal, tmp_path):
    state, job = terminal
    script = tmp_path / "space ' quote $(literal).py"
    pane_runner.launch(job, tmp_path, script)
    assert len(state["sent"]) == 1
    assert state["sent"][0][2] == "w2:p3"
    assert shlex.split(state["sent"][0][3]) == [
        sys.executable,
        str(script),
        "--home",
        str(tmp_path),
        "_repair",
        "watch",
        "attempt",
    ]


@pytest.mark.parametrize("change", ["terminal", "shell", "busy", "agent"])
def test_changed_or_busy_pane_sends_nothing(terminal, tmp_path, change):
    state, job = terminal
    if change == "terminal":
        state["terminal"] = "replacement"
    if change == "shell":
        state["shell"] = 101
    if change == "busy":
        state["foreground"] = [{"pid": 200}]
    if change == "agent":
        state["agent"] = "codex"
    with pytest.raises(RuntimeError):
        pane_runner.launch(job, tmp_path, Path("runner.py"))
    assert not state["sent"]


def test_delivery_error_is_not_retried_and_retains_ownership(harness, monkeypatch):
    h = harness
    job = h["job"]
    job.update(pane={"terminal_id": "original"}, snapshot=snapshot())
    monkeypatch.setattr(pane_runner, "locate", lambda *a, **kw: "w1:p1")
    calls = []

    def uncertain(*a):
        calls.append(a)
        raise RuntimeError("lost reply after delivery")

    monkeypatch.setattr(pane_runner, "launch", uncertain)
    supervisor.start_repair(h["db"], h["home"], job)
    assert job["status"] == "running"
    assert "unconfirmed" in job["summary"]
    assert len(calls) == 1


@pytest.mark.parametrize("hang", [False, True])
def test_pane_worker_streams_to_terminal_exits_and_cannot_replay(harness, monkeypatch, hang):
    h = harness
    if hang:
        state = json.loads(h["state"].read_text())
        state["hang"] = True
        h["state"].write_text(json.dumps(state))
    else:
        agent = Path(json.loads(h["args"].codex_command)[1])
        with agent.open("a") as f:
            f.write('\nprint("VISIBLE AGENT OUTPUT", flush=True)\n')
    # This fake herdr identifies the test worker's actual PID in a real PTY.
    fake = Path(os.environ["PATH"].split(os.pathsep)[0]) / "herdr"
    fake.write_text(
        f"#!{sys.executable}\n"
        + """
import json, os, sys
args = sys.argv[1:]
if args[:2] == ['pane', 'list']:
 data = {'panes': [{'pane_id': 'w1:p1', 'terminal_id': 'original'}]}
elif args[:2] == ['pane', 'get']:
 data = {'pane': {'terminal_id': 'original', 'foreground_cwd': os.getcwd()}}
else:
 data = {'process_info': {'shell_pid': 100, 'foreground_processes': [{'pid': os.getppid()}]}}
print(json.dumps({'result': data}))
"""
    )
    fake.chmod(0o755)
    monkeypatch.setattr(pane_runner, "locate", lambda *a, **kw: "w1:p1")
    spawned = []
    master, slave = pty.openpty()
    output = bytearray()

    def read_terminal():
        while True:
            try:
                chunk = os.read(master, 8192)
            except OSError:
                break
            if not chunk:
                break
            output.extend(chunk)

    reader = threading.Thread(target=read_terminal, daemon=True)
    reader.start()

    def launch(job, home, script):
        spawned.append(
            subprocess.Popen(
                [
                    sys.executable,
                    str(script),
                    "--home",
                    str(home),
                    "_repair",
                    job["id"],
                    job["attempt"],
                ],
                cwd=job["cwd"],
                stdin=subprocess.DEVNULL,
                stdout=slave,
                stderr=slave,
            )
        )

    monkeypatch.setattr(pane_runner, "launch", launch)
    job = h["job"]
    job.update(
        pane={"pane_id": "w1:p1", "terminal_id": "original", "shell_pid": 100}, snapshot=snapshot()
    )
    job["snapshot"]["pr"]["head_sha"] = supervisor.git(h["worktree"], "rev-parse", "HEAD")
    supervisor.start_repair(h["db"], h["home"], job)
    try:
        assert spawned[0].wait(timeout=15) == 0
        os.close(slave)
        reader.join(timeout=5)
        folder = h["home"] / "runs" / job["attempt"]
        outcome = json.loads((folder / "result.json").read_text())
        assert outcome["status"] == ("blocked" if hang else "waiting")
        assert b"Babysitter: resuming" in output
        if not hang:
            assert b"VISIBLE AGENT OUTPUT" in output
            assert "VISIBLE AGENT OUTPUT" in (folder / "agent.log").read_text()
        calls = h["calls"].read_text().splitlines()
        pid = json.loads(calls[0])["pid"]
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
        # Still 'running' in SQLite until reconciliation: replay must be inert.
        subprocess.run(
            [
                sys.executable,
                str(supervisor.SCRIPT),
                "--home",
                str(h["home"]),
                "_repair",
                job["id"],
                job["attempt"],
            ],
            check=True,
            capture_output=True,
        )
        assert h["calls"].read_text().splitlines() == calls
        assert json.loads((folder / "result.json").read_text()) == outcome
    finally:
        if spawned[0].poll() is None:
            spawned[0].terminate()
            spawned[0].wait(timeout=5)
        os.close(master)


@pytest.fixture
def releasing(terminal, monkeypatch):
    state, job = terminal
    monkeypatch.setattr(pane_runner.os, "getpid", lambda: 200)
    monkeypatch.setattr(pane_runner.os, "getppid", lambda: 100)
    monkeypatch.setattr(pane_runner.os, "getpgrp", lambda: 200)
    monkeypatch.setattr(pane_runner.os, "isatty", lambda fd: True)
    monkeypatch.setattr(pane_runner.os, "tcgetpgrp", lambda fd: 200)
    original = pane_runner.result

    def api(*args):
        response = original(*args)
        if args[:2] == ("pane", "process-info"):
            response["process_info"]["foreground_process_group_id"] = 200
        return response

    monkeypatch.setattr(pane_runner, "result", api)
    state["foreground"] = [
        {"pid": 200},
        {
            "pid": 201,
            "argv": ["/opt/homebrew/bin/herdr", "pane", "process-info", "--pane", "w2:p3"],
        },
    ]
    return state, job


def test_release_can_run_in_original_shell_but_launch_still_requires_idle(releasing):
    state, job = releasing
    assert (
        pane_runner.locate(job["pane"], job["cwd"], require_shell=True, allow_release=True)
        == "w2:p3"
    )
    with pytest.raises(RuntimeError, match="busy"):
        pane_runner.locate(job["pane"], job["cwd"], require_shell=True)


@pytest.mark.parametrize(
    "condition",
    ["agent", "different_parent", "not_tty", "other_foreground", "pipeline", "background"],
)
def test_release_exemption_does_not_allow_other_work(releasing, monkeypatch, condition):
    state, job = releasing
    if condition == "agent":
        state["agent"] = "claude"
    if condition == "different_parent":
        monkeypatch.setattr(pane_runner.os, "getppid", lambda: 300)
    if condition == "not_tty":
        monkeypatch.setattr(pane_runner.os, "isatty", lambda fd: False)
    if condition == "other_foreground":
        state["foreground"] = [{"pid": 300, "argv": ["claude"]}]
    if condition == "pipeline":
        state["foreground"].append({"pid": 300, "argv": ["sleep", "30"]})
    if condition == "background":
        monkeypatch.setattr(pane_runner.os, "tcgetpgrp", lambda fd: 300)
    with pytest.raises(RuntimeError, match="busy"):
        pane_runner.locate(job["pane"], job["cwd"], require_shell=True, allow_release=True)
