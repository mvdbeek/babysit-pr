"""The registration broker babysit-bwrap puts between a worker's sandbox and its token.

The broker runs as on the worker (HOME is the fake worker's, whose worker.json names a
temporary dashboard); the dashboard reads the worker over fake ssh, as for any worker
registration.
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import pr_supervisor as supervisor
import pytest
import remote_probe
import worker_api
import worker_broker
from test_remote_cron import git, rollout, short_tmp  # noqa: F401 (fixture)
from test_remote_registration import (  # noqa: F401 (fixtures)
    URL,
    box,
    claude_transcript,
    fresh_rate_limits,
    request_body,
    served,
)

BROKER = Path(supervisor.__file__).resolve().parent / "worker_broker.py"


@pytest.fixture
def broker(box, served):  # noqa: F811
    """Start a broker for a Codex launch given ``worktrees`` and ``config``."""
    worker = box["worker"]
    marker = worker / ".config/babysit-pr/worker.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    token = worker_api.issue(box["home"], "box")
    url = f"http://127.0.0.1:{served.server_port}"
    marker.write_text(json.dumps({"version": 1, "controller": url, "token": token}))
    started = []
    runtime = tempfile.mkdtemp(dir="/tmp")  # Unix socket paths are short.

    def start(worktrees, config=worker / ".codex", agent="codex"):
        listen = Path(runtime) / f"b{len(started)}" / "register.sock"
        listen.parent.mkdir(mode=0o700)
        process = subprocess.Popen(
            [sys.executable, "-I", str(BROKER), str(listen), agent, str(config), *worktrees],
            env={**os.environ, "HOME": str(worker)},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
        )
        started.append(process)
        assert process.stdout.readline() == b"ready\n"
        return listen

    yield start
    for process in started:
        process.terminate()
        process.wait(timeout=10)


def ask(listen, request):
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(60)  # A broker that never answers fails the test, not CI.
        client.connect(str(listen))
        client.sendall(json.dumps(request).encode())
        client.shutdown(socket.SHUT_WR)
        data = b""
        while chunk := client.recv(65536):
            data += chunk
    return json.loads(data)


def jobs(box):  # noqa: F811
    return supervisor.jobs(supervisor.open_db(box["home"]))


def test_the_broker_forwards_only_this_sandboxs_own_session_in_its_worktree(box, broker):  # noqa: F811
    worker, tree, sid = box["worker"], box["tree"], box["sid"]
    # Another task's worktree and session on the same worker, with the same Codex home.
    other = worker / "src" / "worktrees" / "repo" / "other"
    git("worktree", "add", "-q", "-b", "other", str(other), cwd=worker / "src" / "repo")
    other_sid = str(uuid.uuid4())
    rollout(worker, other.resolve(), other_sid)
    listen = broker([str(tree)])
    refused = ask(listen, request_body(box, cwd=str(other), session=other_sid))
    assert "worktree this sandbox was given" in refused["error"]
    refused = ask(listen, request_body(box, agent="claude"))
    assert "This sandbox runs codex" in refused["error"]
    stray = worker / f"rollout-{sid}.jsonl"
    stray.write_text("{}\n")
    refused = ask(listen, request_body(box, rollout=str(stray)))
    assert "not one of this sandbox's own" in refused["error"]
    # A session its configuration directory does not hold is never its own.
    elsewhere = broker([str(tree)], config=worker / "empty-codex-home")
    refused = ask(elsewhere, request_body(box))
    assert "not one of this sandbox's own" in refused["error"]
    assert jobs(box) == []
    answer = ask(listen, request_body(box, session=sid.upper()))
    assert answer["machine"] == "box" and answer["session"] == sid, answer
    [job] = jobs(box)
    assert job["cwd"] == str(tree) and job["rollout"].startswith(str(worker.resolve() / ".codex"))


def test_register_inside_a_sandbox_goes_through_its_broker(box, broker, tmp_path):  # noqa: F811
    listen = broker([str(box["tree"])])
    sandbox_home = tmp_path / "sandbox-home"  # Empty, as babysit-bwrap's tmpfs is.
    sandbox_home.mkdir()
    done = subprocess.run(
        [
            sys.executable,
            str(supervisor.SCRIPT),
            "register",
            "--headless",
            "--pr",
            URL,
            "--session",
            box["sid"],
            "--instructions",
            "Fix CI",
        ],
        env={
            **os.environ,
            "HOME": str(sandbox_home),
            "BABYSIT_WORKER": "1",
            worker_api.SOCKET_ENV: str(listen),
        },
        cwd=box["tree"],
        text=True,
        capture_output=True,
        timeout=120,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert json.loads(done.stdout)["machine"] == "box"
    assert [job["cwd"] for job in jobs(box)] == [str(box["tree"])]


def test_the_broker_exits_with_its_parent_and_removes_its_socket(tmp_path):
    with tempfile.TemporaryDirectory(dir="/tmp") as runtime:
        listen = Path(runtime) / "broker" / "register.sock"
        listen.parent.mkdir(mode=0o700)
        # The sandbox's session records, which babysit-bwrap puts beside the socket.
        (listen.parent / "sessions" / "nested").mkdir(parents=True)
        (listen.parent / "sessions" / "2.json").write_text("{}")
        script = 'exec 3< <(exec "$0" -I "$1" "$2" codex /nowhere </dev/null); read -r line <&3; echo "$line"'
        argv = ["bash", "-c", script, sys.executable, str(BROKER), str(listen)]
        assert subprocess.run(argv, capture_output=True, text=True).stdout == "ready\n"
        for _ in range(100):
            if not listen.parent.exists():
                break
            time.sleep(0.1)
        assert not listen.parent.exists()


def test_a_claude_launch_registers_only_transcripts_in_its_own_store(box, broker):  # noqa: F811
    worker, tree = box["worker"], box["tree"]
    default = worker / ".claude"
    folder = str(tree).replace("/", "-")
    own = str(uuid.uuid4())
    claude_transcript(worker, tree, own)
    listen = broker([str(tree)], config=default, agent="claude")
    # Account B's transcript, reached from the default store through a link the agent
    # made: dangling inside its sandbox, resolved on the host.
    other = str(uuid.uuid4())
    claude_transcript(worker, tree, other, "b")
    (default / "projects" / "q").symlink_to(Path("..") / "accounts" / "b" / "projects" / folder)
    refused = ask(listen, request_body(box, agent="claude", session=other))
    assert "not one of this sandbox's own" in refused["error"]
    # A transcript that is a link out of the store.
    linked = str(uuid.uuid4())
    outside = claude_transcript(worker, tree, linked, "c")
    (default / "projects" / "p").mkdir()
    (default / "projects" / "p" / f"{linked}.jsonl").symlink_to(outside)
    refused = ask(listen, request_body(box, agent="claude", session=linked))
    assert "not one of this sandbox's own" in refused["error"]
    # Two transcripts of one session: neither is taken.
    twice = str(uuid.uuid4())
    claude_transcript(worker, tree, twice)
    (default / "projects" / "copy").mkdir()
    copy = default / "projects" / "copy" / f"{twice}.jsonl"
    copy.write_text((default / "projects" / folder / f"{twice}.jsonl").read_text())
    refused = ask(listen, request_body(box, agent="claude", session=twice))
    assert "not one of this sandbox's own" in refused["error"]
    assert jobs(box) == []
    answer = ask(listen, request_body(box, agent="claude", session=own))
    assert answer.get("session") == own, answer
    [job] = jobs(box)
    assert job["claude_config_dir"] == str(default.resolve())
    assert job["claude_account"] == "default"


@pytest.mark.parametrize(
    "body",
    [b"not json", b"[]", b'{"cwd": "/x"}', b"x" * (worker_api.MAX_BODY + 10)],
)
def test_the_broker_refuses_malformed_or_oversized_requests(box, broker, body):  # noqa: F811
    listen = broker([str(box["tree"])])
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(60)  # A broker that never answers fails the test, not CI.
        client.connect(str(listen))
        try:
            client.sendall(body)
            client.shutdown(socket.SHUT_WR)
        except OSError:
            pass  # It may stop reading an oversized request.
        data = b""
        while chunk := client.recv(65536):
            data += chunk
    assert "error" in json.loads(data)
    assert jobs(box) == []


def test_one_sandbox_registers_at_most_once_in_a_while(box, broker):  # noqa: F811
    listen = broker([str(box["tree"])])
    assert ask(listen, request_body(box))["machine"] == "box"
    # Forwarded again at once, it is refused here, not by the controller.
    assert "wait" in ask(listen, request_body(box))["error"]


def test_the_controller_checks_the_transcript_lies_in_the_launchs_store_as_it_reads(box):  # noqa: F811
    worker, tree = box["worker"], box["tree"]
    sid = str(uuid.uuid4())
    path = claude_transcript(worker, tree, sid, "b")
    os.environ["HOME"], home = str(worker), os.environ["HOME"]
    try:
        with pytest.raises(ValueError, match="not in the launch's configuration directory"):
            remote_probe._host_session_info(
                "claude", sid, str(tree), str(path), None, str(worker / ".claude")
            )
        found = remote_probe._host_session_info(
            "claude", sid, str(tree), str(path), None, str(worker / ".claude/accounts/b")
        )
        assert found["claude_account"] == "b"
        with pytest.raises(ValueError, match="not in the launch's configuration directory"):
            remote_probe._host_session_info(
                "codex", box["sid"], str(tree), None, None, str(worker / "elsewhere")
            )
    finally:
        os.environ["HOME"] = home


def fake_process(proc, pid, children=(), nspid=None, start="100", comm="claude"):
    """A process in a fake /proc: its children, its PIDs per namespace and start time."""
    folder = proc / str(pid)
    (folder / "task" / str(pid)).mkdir(parents=True)
    (folder / "task" / str(pid) / "children").write_text(" ".join(map(str, children)))
    (folder / "status").write_text(
        f"Name:\t{comm}\nNSpid:\t{pid}" + (f"\t{nspid}" if nspid else "") + "\n"
    )
    # starttime is field 22; the command name may hold spaces and parentheses.
    (folder / "stat").write_text(
        f"{pid} ({comm}) x) S 1 " + " ".join(["0"] * 17) + f" {start} 0 0\n"
    )


def session_record(store, nspid, session, start="100"):
    store.mkdir(parents=True, exist_ok=True)
    (store / f"{nspid}.json").write_text(
        json.dumps({"pid": nspid, "sessionId": session, "procStart": start, "kind": "interactive"})
    )


@pytest.fixture
def sandbox(tmp_path):
    """A sandbox as bwrap runs it: bwrap (10), then its broker (11) and its init (12) in
    a namespace of its own, which runs Claude (13), PID 2 there, whose shell (14) runs
    another Claude (15). Claude's store is <tmp>/.claude, its records this launch's own."""
    proc = tmp_path / "proc"
    fake_process(proc, 10, [11, 12], comm="bwrap")
    fake_process(proc, 11, comm="python3")
    fake_process(proc, 12, [13], nspid=1, comm="bwrap")
    fake_process(proc, 13, [14], nspid=2, start="777")
    fake_process(proc, 14, [15], nspid=3, comm="bash")
    fake_process(proc, 15, nspid=4, start="900")
    return {
        "proc": proc,
        "config": tmp_path / ".claude",
        "store": tmp_path / "launch-sessions",
        "tree": tmp_path / "tree",
    }


def test_the_broker_finds_the_session_its_sandboxs_claude_records_by_its_own_pid(sandbox, tmp_path):
    proc, config, store, tree = (sandbox[k] for k in ("proc", "config", "store", "tree"))

    def found():
        return worker_broker.running_session(10, config, store, proc)

    session = str(uuid.uuid4())
    # Recorded, but not written to yet: nothing to show, so nothing is reported.
    session_record(store, 2, session, start="777")
    assert found() is None
    path = claude_transcript(tmp_path, tree, session).resolve()
    assert found() == (session, str(path))
    # A record left by an earlier Claude of the same namespace PID is not this one's,
    # nor is the session of a Claude the launch's own one runs.
    nested = str(uuid.uuid4())
    claude_transcript(tmp_path, tree, nested)
    session_record(store, 4, nested, start="900")
    session_record(store, 2, session, start="776")
    assert found() is None
    # Nor is a session whose transcript lies in another account's store.
    other = str(uuid.uuid4())
    claude_transcript(tmp_path, tree, other, "b")
    session_record(store, 2, other, start="777")
    assert found() is None
    session_record(store, 2, "not a uuid", start="777")
    assert found() is None
    # A process outside the namespace (the broker, PID 11 on the host) never matches.
    session_record(store, 11, session, start="100")
    assert found() is None
    # Only the record itself is read: not a link to one, nor a pipe that never ends.
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text(json.dumps({"sessionId": session, "procStart": "777"}))
    (store / "2.json").unlink()
    (store / "2.json").symlink_to(elsewhere)
    assert found() is None
    (store / "2.json").unlink()
    os.mkfifo(store / "2.json")
    assert found() is None
    (store / "2.json").unlink()
    (store / "2.json").write_text("[" * 60000)
    assert found() is None


def fake_herdr(tmp_path, monkeypatch, body):
    calls = tmp_path / "herdr-calls"
    herdr = tmp_path / "herdr"
    herdr.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{calls}"\n{body}\n')
    herdr.chmod(0o755)
    monkeypatch.setenv("HERDR_ENV", "1")
    monkeypatch.setenv("HERDR_PANE_ID", "wQ:p1")
    monkeypatch.setenv("HERDR_BIN_PATH", str(herdr))
    monkeypatch.setattr(worker_broker, "SESSION_POLL", 0)
    return calls


def test_the_broker_tells_herdr_the_session_and_again_when_it_changes(
    sandbox, tmp_path, monkeypatch
):
    proc, config, store, tree = (sandbox[k] for k in ("proc", "config", "store", "tree"))
    first, second = str(uuid.uuid4()), str(uuid.uuid4())
    paths = {sid: claude_transcript(tmp_path, tree, sid).resolve() for sid in (first, second)}
    session_record(store, 2, first, start="777")
    calls = fake_herdr(tmp_path, monkeypatch, "true")
    rounds = iter(range(6))

    def running():
        index = next(rounds, None)
        if index == 3:  # A /clear starts another session in the same process.
            session_record(store, 2, second, start="777")
        return index is not None

    worker_broker.report_sessions(10, config, store, proc, running)
    reports = calls.read_text().splitlines()
    # Once per session, not once per look.
    assert len(reports) == 2
    for report, sid in zip(reports, (first, second), strict=True):
        args = report.split()
        assert args[:4] == [
            "pane",
            "report-agent-session",
            "--source=herdr:claude",
            "--agent=claude",
        ]
        assert args[4].startswith("--seq=")
        assert args[5:] == [
            f"--agent-session-id={sid}",
            f"--agent-session-path={paths[sid]}",
            "wQ:p1",
        ]
    first_seq, second_seq = (int(r.split()[4].removeprefix("--seq=")) for r in reports)
    assert second_seq > first_seq


def test_the_broker_reports_again_when_herdr_refused_and_not_outside_herdr(
    sandbox, tmp_path, monkeypatch
):
    proc, config, store, tree = (sandbox[k] for k in ("proc", "config", "store", "tree"))
    session = str(uuid.uuid4())
    claude_transcript(tmp_path, tree, session)
    session_record(store, 2, session, start="777")
    # herdr is not answering at first: the report is tried again.
    calls = fake_herdr(
        tmp_path, monkeypatch, f'[ "$(wc -l < "{tmp_path / "herdr-calls"}")" -gt 1 ]'
    )
    monkeypatch.delenv("HERDR_ENV")
    worker_broker.report_sessions(10, config, store, proc, lambda: True)  # Returns at once.
    assert not calls.exists()
    monkeypatch.setenv("HERDR_ENV", "1")
    rounds = iter(range(4))
    worker_broker.report_sessions(10, config, store, proc, lambda: next(rounds, None) is not None)
    assert len(calls.read_text().splitlines()) == 2
