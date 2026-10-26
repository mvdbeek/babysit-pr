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
