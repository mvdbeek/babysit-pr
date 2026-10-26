"""Repairs of watches on a worker: the guardian here, the agent there, over fake ssh.

The worker's babysit-pr is a copy of these scripts whose babysit-bwrap only records its
options and runs the command after ``--``; its codex and claude are fakes on PATH.
"""

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import herdr_handoff
import pr_supervisor as supervisor
import pytest
import remote_repair
import session_locks
from processes import wait_gone
from test_remote_cron import git, short_tmp  # noqa: F401 (fixture)
from test_remote_registration import box, claude_transcript, register, registry  # noqa: F401

SCRIPTS = Path(supervisor.__file__).resolve().parent
CHECK_VERSION = remote_repair.check_version
BWRAP = f"""#!{sys.executable}
import os, sys
args = sys.argv[1:]
with open(os.path.expanduser("~/bwrap-calls"), "a") as calls:
    calls.write(" ".join(args) + "\\n")
granted = []
while args[0] != "--":
    word = args.pop(0)
    if word.startswith("--add-dirs="):
        granted += word.split("=", 1)[1].split(":")
args.pop(0)
# As in the real sandbox, a file the agent is told to read or write must be mounted.
for flag in ("-o", "--output-schema"):
    if flag in args:
        path = os.path.realpath(args[args.index(flag) + 1])
        if not any(path.startswith(os.path.realpath(d) + "/") for d in granted):
            sys.exit(f"bwrap: {{path}} is not mounted in the sandbox")
os.execvp(args[0], args)
"""
AGENT = f"""#!{sys.executable}
import json, os, sys, time
from pathlib import Path
args = sys.argv[1:]
prompt = sys.stdin.read()
record = {{"argv": args, "cwd": os.getcwd(), "pid": os.getpid(), "prompt": prompt,
          "env": {{k: os.environ.get(k) for k in ("BABYSIT_PR_REPAIR", "GIT_TERMINAL_PROMPT",
                                                 "CLAUDE_CONFIG_DIR", "CODEX_THREAD_ID")}}}}
with open(os.environ["FAKE_REPAIR_LOG"], "a") as log:
    log.write(json.dumps(record) + "\\n")
mode = os.environ.get("FAKE_REPAIR", "")
print("working on it", flush=True)
if mode == "hang":
    time.sleep(120)
if mode == "fail":
    sys.exit(3)
reply = {{"status": "waiting", "summary": "Fixed on the worker"}}
if Path(sys.argv[0]).name == "claude":
    sid = args[args.index("--resume") + 1]
    print(json.dumps({{"type": "result", "subtype": "success", "session_id": sid,
                      "structured_output": reply}}), flush=True)
else:
    out = Path(args[args.index("-o") + 1])
    out.write_text(json.dumps(reply))
    if mode == "plant":
        (out.parent / "escape").symlink_to(os.environ["FAKE_PLANT"])
"""


@pytest.fixture
def worker(box, tmp_path, monkeypatch):  # noqa: F811
    """The worker's deployed babysit-pr and fake agents; the controller's own HOME."""
    monkeypatch.setenv("HOME", str(tmp_path / "me"))  # Leases live under it.
    # The worker imports its own copy, never this checkout's through the test sitecustomize.
    monkeypatch.delenv("PYTHONPATH", raising=False)
    app = box["worker"] / "app"
    shutil.copytree(
        SCRIPTS, app / "scripts", ignore=shutil.ignore_patterns("test_*", "__pycache__")
    )
    (app / "scripts" / "babysit-bwrap").write_text(BWRAP)
    (app / "scripts" / "babysit-bwrap").chmod(0o755)
    registry(box["home"], babysit_root=str(app))
    bin_dir = tmp_path / "agents"
    bin_dir.mkdir()
    for name in ("codex", "claude"):
        (bin_dir / name).write_text(AGENT)
        (bin_dir / name).chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_REPAIR_LOG", str(tmp_path / "agent-calls.jsonl"))
    # The worker runs this very code; the version handshake has its own test.
    monkeypatch.setattr(remote_repair, "check_version", lambda home, worker: None)
    return box


def dispatch(box, **changes):  # noqa: F811
    """A registered watch claimed for one repair, as start_repair leaves it."""
    job = register(box, **changes)
    attempt = str(uuid.uuid4())
    folder = box["home"] / "runs" / attempt
    folder.mkdir(parents=True)
    snapshot = {
        "pr": supervisor.watch.resolve_subject(job["url"], repo_override=job["repo"]),
        "checks": {"failed_count": 1},
    }
    job.update(status="running", attempt=attempt, snapshot=snapshot, dispatched_keys=["k"])
    db = supervisor.open_db(box["home"])
    with db:
        supervisor.save_job(db, job)
    db.close()
    return job, folder


def calls(tmp_path):
    path = tmp_path / "agent-calls.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def result(folder):
    return json.loads((folder / "result.json").read_text())


def remote_run(box, job):  # noqa: F811
    return box["worker"] / ".local/state/babysit-pr/runs" / job["attempt"]


def test_a_codex_repair_runs_on_the_worker_and_returns_here(worker, tmp_path):
    job, folder = dispatch(worker)
    supervisor.run_repair(worker["home"], job["id"], job["attempt"])
    assert result(folder) == {"status": "waiting", "summary": "Fixed on the worker"}
    # The worker's stream, copied into this machine's log; its reply fetched apart.
    assert "working on it" in (folder / "agent.log").read_text()
    assert json.loads((folder / "reply.json").read_text())["summary"] == "Fixed on the worker"
    [call] = calls(tmp_path)
    tree = str(worker["tree"])
    assert call["cwd"] == tree and "Fix CI only" in call["prompt"]
    assert "the babysit-pr skill's SKILL.md" in call["prompt"]
    assert call["env"]["BABYSIT_PR_REPAIR"] == job["id"]
    assert call["env"]["GIT_TERMINAL_PROMPT"] == "0"
    argv = call["argv"]
    assert argv[argv.index("--sandbox") + 1] == "danger-full-access"
    assert argv[-3:] == ["resume", worker["sid"], "-"]
    # Inside the worker's own babysit-bwrap, given the worktree and its common dir.
    [bwrap] = (worker["worker"] / "bwrap-calls").read_text().splitlines()
    common = str((worker["worker"] / "src/repo/.git").resolve())
    assert f"--codex-home={worker['worker']}/.codex" in bwrap
    assert f"--add-dirs={tree}:{common}" in bwrap and "-- codex exec" in bwrap
    run = remote_run(worker, job)
    assert json.loads((run / "result.json").read_text()) == {"exit": 0, "error": None}
    # Codex writes its reply in a folder of its own, never into the run folder.
    assert str(run) not in bwrap
    assert not (worker["worker"] / ".cache/babysit-repairs" / job["attempt"]).exists()
    assert not session_locks.held("codex", worker["sid"], None, "box")
    # The worker half never keeps a queue.
    assert not (worker["worker"] / ".local/state/babysit-pr/queue.sqlite").exists()


def test_a_claude_repair_on_the_worker_uses_its_account(worker, tmp_path):
    sid = str(uuid.uuid4())
    claude_transcript(worker["worker"], worker["tree"], sid, "oss")
    job, folder = dispatch(worker, agent="claude", session=sid, claude_account="oss")
    supervisor.run_repair(worker["home"], job["id"], job["attempt"])
    assert result(folder) == {"status": "waiting", "summary": "Fixed on the worker"}
    [call] = calls(tmp_path)
    assert call["env"]["CLAUDE_CONFIG_DIR"] == str(worker["worker"] / ".claude/accounts/oss")
    assert call["argv"][:3] == ["--dangerously-skip-permissions", "--print", "--resume"]
    assert "--permission-mode" not in call["argv"]
    [bwrap] = (worker["worker"] / "bwrap-calls").read_text().splitlines()
    assert (
        "--claude-config-dir=" + str((worker["worker"] / ".claude/accounts/oss").resolve()) in bwrap
    )


def test_a_failing_or_timed_out_repair_blocks_the_watch(worker, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_REPAIR", "fail")
    job, folder = dispatch(worker)
    supervisor.run_repair(worker["home"], job["id"], job["attempt"])
    assert result(folder)["status"] == "blocked"
    assert "codex exited 3 on Box" in result(folder)["summary"]
    monkeypatch.setenv("FAKE_REPAIR", "hang")
    db = supervisor.open_db(worker["home"])
    with db:
        db.execute("DELETE FROM jobs")
    db.close()
    job, folder = dispatch(worker, repair_timeout=2)
    started = time.monotonic()
    supervisor.run_repair(worker["home"], job["id"], job["attempt"])
    assert time.monotonic() - started < 30
    assert result(folder) == {"status": "blocked", "summary": "Box: Timed out after 2 s"}
    assert wait_gone(calls(tmp_path)[-1]["pid"])


def test_stopping_the_guardian_stops_the_agent_on_the_worker(worker, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_REPAIR", "hang")
    job, folder = dispatch(worker)

    def stop_when_started():
        deadline = time.monotonic() + 30
        while not calls(tmp_path) and time.monotonic() < deadline:
            time.sleep(0.1)
        os.kill(os.getpid(), signal.SIGTERM)

    threading.Thread(target=stop_when_started, daemon=True).start()
    supervisor.run_repair(worker["home"], job["id"], job["attempt"])
    assert result(folder) == {
        "status": "blocked",
        "summary": "Repair interrupted; inspect before resuming",
    }
    assert wait_gone(calls(tmp_path)[0]["pid"], 10), "The worker's agent outlived its guardian"
    # The worker confirmed it stopped: nothing stays held.
    assert not session_locks.held("codex", worker["sid"], None, "box")
    run = remote_run(worker, job)
    assert (
        "controller ended its connection" in json.loads((run / "result.json").read_text())["error"]
    )


def test_a_lost_worker_keeps_the_session_held_until_it_shows_the_run_ended(
    worker, tmp_path, monkeypatch
):
    monkeypatch.setenv("FAKE_REPAIR", "hang")
    monkeypatch.setenv("FAKE_SSH_DROP", "box.test:_repair_worker:2")
    job, folder = dispatch(worker)
    supervisor.run_repair(worker["home"], job["id"], job["attempt"])
    outcome = result(folder)
    assert outcome["status"] == "blocked"
    assert outcome["summary"].startswith("Box unreachable mid-repair; remote agent may still be")
    sid = worker["sid"]
    assert session_locks.held("codex", sid, None, "box")
    assert list(session_locks.orphans()) == [("box", sid, job["attempt"])]
    with pytest.raises(BlockingIOError, match="orphaned"):
        with session_locks.acquire("codex", sid, None, "box"):
            pass
    # The lifeline ended with the guardian, so the worker stops the agent.
    run = remote_run(worker, job)
    deadline = time.monotonic() + 30
    while not (run / "result.json").exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    monkeypatch.setenv("FAKE_SSH_DOWN", "box.test")
    assert not remote_repair.reconcile(worker["home"], "box", sid, job["attempt"])
    assert session_locks.held("codex", sid, None, "box")
    monkeypatch.delenv("FAKE_SSH_DOWN")
    assert remote_repair.reconcile(worker["home"], "box", sid, job["attempt"])
    assert not session_locks.held("codex", sid, None, "box")


def test_a_worker_running_other_code_blocks_the_repair(worker, monkeypatch):
    # The real version handshake, against the copy's own commit.
    monkeypatch.setattr(remote_repair, "check_version", CHECK_VERSION)
    app = worker["worker"] / "app"
    git("init", "-q", cwd=app)
    git("add", "-A", cwd=app)
    git("commit", "-q", "-m", "other", cwd=app)
    job, folder = dispatch(worker)
    supervisor.run_repair(worker["home"], job["id"], job["attempt"])
    summary = result(folder)["summary"]
    assert "update babysit-pr on Box (provision_worker.sh --user-step --sha" in summary


def test_the_worker_half_runs_a_spec_once_and_only_a_staged_one(worker, tmp_path):
    stray = worker["worker"] / "spec.json"
    stray.write_text("{}")
    env = {**os.environ, "HOME": str(worker["worker"])}
    script = str(worker["worker"] / "app/scripts/pr_supervisor.py")
    done = subprocess.run(
        [sys.executable, script, "_repair_worker", str(stray)],
        env=env,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )
    assert done.returncode == 1 and "staged specs folder" in done.stdout
    job, folder = dispatch(worker)
    supervisor.run_repair(worker["home"], job["id"], job["attempt"])
    [spec] = (worker["worker"] / ".local/state/babysit-pr/repair-specs").iterdir()
    before = (remote_run(worker, job) / "result.json").read_text()
    again = subprocess.run(
        [sys.executable, script, "_repair_worker", str(spec)],
        env=env,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )
    assert again.returncode == 1 and "File exists" in again.stdout
    assert (remote_run(worker, job) / "result.json").read_text() == before
    assert len(calls(tmp_path)) == 1


@pytest.fixture
def pane(worker, fake_tools, monkeypatch):
    """The watch's shell pane on the worker, as its herdr shows it, polled quickly."""
    store = json.loads(fake_tools.read_text())
    store["machines"] = {
        "Box": {
            "workspaces": [],
            "calls": [],
            "agents": [{"pane_id": "w1:p1", "cwd": str(worker["tree"])}],
            "panes": [{"pane_id": "w1:p1", "terminal_id": "terminal"}],
            "process_info": {"w1:p1": {"shell_pid": 1, "foreground_processes": [{"pid": 1}]}},
        }
    }
    fake_tools.write_text(json.dumps(store))
    monkeypatch.setattr(remote_repair, "PANE_POLL", 0.1)
    typed = []

    def routed(*args, machine=None):
        if args[:2] != ("pane", "run"):
            return real(*args, machine=machine)
        typed.append((machine.label if machine else None, args))
        return ""

    real = herdr_handoff.herdr

    monkeypatch.setattr(herdr_handoff, "herdr", routed)
    return typed


def finish_on_worker(box, job, reply, exit=0):  # noqa: F811
    run = remote_run(box, job)
    run.mkdir(parents=True)
    (run / "reply.json").write_text(json.dumps(reply))
    (run / "agent.log").write_text("shown in the pane\n")
    (run / "result.json").write_text(json.dumps({"exit": exit, "error": None}))


def test_a_pane_repair_is_typed_into_the_workers_pane_and_polled(worker, pane):
    job, folder = dispatch(worker, pane="w1:p1", headless=False)
    reply = {"status": "waiting", "summary": "Fixed in view"}
    original = remote_repair.status

    def status(home, machine, attempt, timeout=30):
        if not remote_run(worker, job).exists():
            finish_on_worker(worker, job, reply)  # What the typed line did meanwhile.
        return original(home, machine, attempt, timeout)

    remote_repair.status = status
    try:
        supervisor.run_repair(worker["home"], job["id"], job["attempt"])
    finally:
        remote_repair.status = original
    assert result(folder) == reply
    [(label, args)] = pane
    app = worker["worker"] / "app"
    [spec] = (worker["worker"] / ".local/state/babysit-pr/repair-specs").iterdir()
    assert label == "Box" and args[:3] == ("pane", "run", "w1:p1")
    assert args[3] == f"{sys.executable} {app}/scripts/pr_supervisor.py _repair_worker {spec}"
    assert json.loads(spec.read_text())["pane"] == job["pane"]
    # The end of the worker's log, for the dashboard.
    assert (folder / "agent.log").read_text() == "shown in the pane\n"


def test_a_pane_repair_without_a_result_by_its_deadline_keeps_the_session_held(
    worker, pane, monkeypatch
):
    monkeypatch.setattr(remote_repair, "SSH_SLACK", 0)
    job, folder = dispatch(worker, pane="w1:p1", headless=False, repair_timeout=1)
    supervisor.run_repair(worker["home"], job["id"], job["attempt"])
    assert "unreachable mid-repair" in result(folder)["summary"]
    assert session_locks.held("codex", worker["sid"], None, "box")


def test_the_worker_half_of_a_pane_repair_runs_only_in_its_terminal(worker, pane, tmp_path):
    job, folder = dispatch(worker, pane="w1:p1", headless=False)
    staged = worker["worker"] / ".local/state/babysit-pr/repair-specs"
    staged.mkdir(parents=True)
    spec = remote_repair.spec_for(
        remote_repair.machines.resolve(worker["home"], "box"),
        job,
        job["attempt"],
        "prompt",
        job["snapshot"]["pr"],
    )
    (staged / "spec.json").write_text(json.dumps(spec))
    done = subprocess.run(
        [
            sys.executable,
            str(worker["worker"] / "app/scripts/pr_supervisor.py"),
            "_repair_worker",
            str(staged / "spec.json"),
        ],
        env={**os.environ, "HOME": str(worker["worker"])},
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )
    assert done.returncode == 0
    recorded = json.loads((remote_run(worker, job) / "result.json").read_text())
    assert "requires its original terminal" in recorded["error"]
    assert not calls(tmp_path)


def test_a_run_cancelled_before_it_starts_can_never_start_late(worker, tmp_path):
    job, folder = dispatch(worker)
    machine = remote_repair.machines.resolve(worker["home"], "box")
    staged = worker["worker"] / ".local/state/babysit-pr/repair-specs"
    staged.mkdir(parents=True)
    spec = remote_repair.spec_for(machine, job, job["attempt"], "prompt", job["snapshot"]["pr"])
    (staged / "late.json").write_text(json.dumps(spec))
    assert remote_repair.cancel(worker["home"], machine, job["attempt"])
    record = remote_repair.status(worker["home"], machine, job["attempt"])
    assert record["result"] == {"exit": None, "error": "Cancelled before start"}
    late = subprocess.run(
        [
            sys.executable,
            str(worker["worker"] / "app/scripts/pr_supervisor.py"),
            "_repair_worker",
            str(staged / "late.json"),
        ],
        env={**os.environ, "HOME": str(worker["worker"])},
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )
    assert late.returncode == 1 and "File exists" in late.stdout
    assert not calls(tmp_path)


def test_reconciling_a_run_that_never_started_takes_it_first(worker, tmp_path):
    job, _ = dispatch(worker)
    sid = worker["sid"]
    session_locks.mark_orphan("codex", sid, None, job["attempt"], "box")
    assert remote_repair.reconcile(worker["home"], "box", sid, job["attempt"])
    assert not session_locks.held("codex", sid, None, "box")
    result = json.loads((remote_run(worker, job) / "result.json").read_text())
    assert result["error"] == "Cancelled before start"


def test_a_stop_while_typing_into_the_pane_cancels_the_run(worker, pane, monkeypatch):
    job, folder = dispatch(worker, pane="w1:p1", headless=False)
    real = herdr_handoff.herdr

    def interrupted(*args, machine=None):
        if args[:2] == ("pane", "run"):
            raise KeyboardInterrupt()  # A stop request lands while the line is sent.
        return real(*args, machine=machine)

    monkeypatch.setattr(herdr_handoff, "herdr", interrupted)
    supervisor.run_repair(worker["home"], job["id"], job["attempt"])
    assert result(folder)["summary"] == "Repair interrupted; inspect before resuming"
    run = json.loads((remote_run(worker, job) / "result.json").read_text())
    assert run["error"] == "Cancelled before start"
    assert not session_locks.held("codex", worker["sid"], None, "box")


def test_a_codex_reply_is_read_only_as_a_regular_file(tmp_path):
    folder = tmp_path / "out"
    folder.mkdir()
    (tmp_path / "target.json").write_text('{"status": "waiting", "summary": "x"}')
    (folder / "reply.json").symlink_to(tmp_path / "target.json")
    with pytest.raises(OSError):
        remote_repair.codex_reply(folder)
    (folder / "reply.json").unlink()
    os.mkfifo(folder / "reply.json")
    with pytest.raises(ValueError, match="no reply file"):
        remote_repair.codex_reply(folder)  # Without blocking on the FIFO.


def test_cleaning_up_codex_output_never_follows_a_link_out(worker, tmp_path, monkeypatch):
    kept = tmp_path / "kept"
    kept.mkdir()
    (kept / "file").write_text("x")
    monkeypatch.setenv("FAKE_REPAIR", "plant")
    monkeypatch.setenv("FAKE_PLANT", str(kept))
    job, folder = dispatch(worker)
    supervisor.run_repair(worker["home"], job["id"], job["attempt"])
    assert result(folder)["status"] == "waiting"
    assert (kept / "file").read_text() == "x"
    assert not (worker["worker"] / ".cache/babysit-repairs" / job["attempt"]).exists()
