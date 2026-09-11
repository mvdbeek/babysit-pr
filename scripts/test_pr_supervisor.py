"""Behavioral tests use fake GitHub/Codex executables; never touch live PRs."""

import argparse
import copy
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid

import pytest

import gh_pr_watch as watch
import pr_supervisor as supervisor


def snapshot():
    return {
        "pr": {"head_sha": "abc", "base_sha": "base", "head_branch": "feature", "closed": False,
               "merged": False, "mergeable": "MERGEABLE", "merge_state_status": "CLEAN"},
        "checks": {"failed_count": 1, "passed_count": 0, "pending_count": 0, "all_terminal": True},
        "failed_runs": [{"run_id": 1, "run_attempt": 1}], "failed_jobs": [],
        "new_review_items": [{"kind": "review_comment", "id": "1", "body": "Fix this"}],
    }


def job():
    return {"id": "job", "status": "watching", "pending_reviews": [], "handled": [],
            "watcher_state": {}, "poll_seconds": 120, "poll_errors": 0}


def test_pending_reviews_and_cursor_survive_restart(tmp_path):
    db = supervisor.open_db(tmp_path)
    j = job()
    with db:
        supervisor.save_job(db, supervisor.ingest(j, snapshot(), {"seen_review_comment_ids": ["1"]}))
    db.close()
    db = supervisor.open_db(tmp_path)
    j = supervisor.get_job(db, "job")
    quiet = snapshot()
    quiet["new_review_items"] = []
    supervisor.ingest(j, quiet, j["watcher_state"])
    assert j["pending_reviews"][0]["id"] == "1"
    assert j["watcher_state"]["seen_review_comment_ids"] == ["1"]
    assert supervisor.actionable(j)
    db.close()


def test_transaction_rollback_does_not_consume_review(tmp_path):
    db = supervisor.open_db(tmp_path)
    with db:
        supervisor.save_job(db, job())
    with pytest.raises(RuntimeError), db:
        supervisor.save_job(db, supervisor.ingest(job(), snapshot(), {"seen_review_comment_ids": ["1"]}))
        raise RuntimeError("simulated crash before commit")
    assert supervisor.get_job(db, "job")["watcher_state"] == {}
    db.close()


def test_acknowledged_failure_waits_until_rerun_or_new_sha():
    j = supervisor.ingest(job(), snapshot(), {})
    j.update(dispatched_keys=supervisor.event_keys(j["snapshot"]), dispatched_reviews=["review_comment:1"])
    supervisor.finish_repair(j, {"status": "waiting", "summary": "Rerun requested"})
    assert not supervisor.actionable(j)
    j["snapshot"]["checks"]["pending_count"] = 5
    assert not supervisor.actionable(j)  # unrelated matrix progress
    j["snapshot"]["failed_runs"][0]["run_attempt"] = 2
    assert supervisor.actionable(j)
    j["snapshot"] = snapshot()
    j["snapshot"]["pr"]["head_sha"] = "new"
    assert supervisor.actionable(j)


def test_blocked_repair_retains_feedback_and_stop_is_honored():
    j = supervisor.ingest(job(), snapshot(), {})
    j.update(dispatched_keys=[], dispatched_reviews=["review_comment:1"], stop_after_run=True)
    supervisor.finish_repair(j, {"status": "blocked", "summary": "Needs help"})
    assert j["status"] == "stopped"
    assert j["pending_reviews"]


def test_parent_run_completion_does_not_repeat_failed_job():
    s = snapshot()
    s["failed_runs"] = []
    s["failed_jobs"] = [{"run_id": 1, "job_id": 10}]
    before = supervisor.event_keys(s)
    s["failed_runs"] = [{"run_id": 1, "run_attempt": 1}]
    assert supervisor.event_keys(s) == before
    s["failed_jobs"][0]["job_id"] = 11
    assert supervisor.event_keys(s) != before


def test_closed_pr_never_dispatches_and_empty_checks_not_green():
    s = snapshot()
    s["pr"]["closed"] = True
    assert supervisor.ingest(job(), s, {})["status"] == "closed"
    s = snapshot()
    s["checks"].update(failed_count=0)
    assert "green" not in supervisor.ingest(job(), s, {})["summary"]


@pytest.mark.parametrize("code", [1, 8])
def test_failed_or_pending_checks_payload_is_not_a_transport_error(monkeypatch, code):
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, code, "[]", ""))
    assert watch.gh_json(["pr", "checks", "1", "--json", "name"]) == []
    with pytest.raises(watch.GhCommandError):
        watch.gh_json(["api", "user"])


def wait_until(predicate, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError("Timed out waiting for test process")


@pytest.fixture
def harness(tmp_path, monkeypatch):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    subprocess.run(["git", "init", "-b", "feature", str(worktree)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(worktree), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                    "commit", "--allow-empty", "-m", "fixture"], check=True, capture_output=True)
    sha = supervisor.git(worktree, "rev-parse", "HEAD")
    sid = str(uuid.uuid4())
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text(json.dumps({"type": "session_meta", "payload": {"id": sid, "cwd": str(worktree)}}) + "\n" +
                       json.dumps({"type": "turn_context", "payload": {"model": "fixture-model", "sandbox_policy": {"type": "workspace-write"}}}) + "\n")
    state_file = tmp_path / "github.json"
    state_file.write_text(json.dumps({"sha": sha, "failed": True, "attempt": 1}))
    calls = tmp_path / "calls.jsonl"
    monkeypatch.setenv("FAKE_GH_STATE", str(state_file))
    monkeypatch.setenv("FAKE_AGENT_CALLS", str(calls))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_gh = bin_dir / "gh"
    fake_gh.write_text(f"#!{sys.executable}\n" + '''
import json, os, sys
from pathlib import Path
state = json.loads(Path(os.environ['FAKE_GH_STATE']).read_text())
args = sys.argv[1:]
if args[0] == '-R': args = args[2:]
if os.environ.get('FAKE_BRANCH_ONLY') and not (args[0] == 'api' and
    ('/branches/' in args[1] or '/actions/' in args[1])):
 raise RuntimeError('PR/auth/review lookup in branch-only watch')
if args[:2] == ['pr', 'view']:
 data = {'number': 1, 'url': 'https://github.com/test/repo/pull/1', 'state': 'OPEN',
         'headRefName': 'feature', 'headRefOid': state['sha'], 'baseRefOid': 'base',
         'mergeable': 'MERGEABLE', 'mergeStateStatus': 'CLEAN', 'reviewDecision': ''}
elif args[:2] == ['pr', 'checks']:
 data = [{'name': 'tests', 'bucket': 'fail' if state['failed'] else 'pass',
          'state': 'FAILURE' if state['failed'] else 'SUCCESS', 'link': 'https://github.com/test/repo/actions/runs/1'}]
elif args == ['api', 'user']: data = {'login': 'test'}
elif args[0] == 'api' and '/branches/feature' in args[1]:
 data = {'name': 'feature', 'commit': {'sha': state['sha']}}
elif args[0] == 'api' and '/actions/runs/1/jobs' in args[1]:
 data = {'jobs': [{'id': 10, 'name': 'tests', 'status': 'completed', 'conclusion': 'failure'}]}
elif args[0] == 'api' and '/actions/runs' in args[1]:
 data = {'workflow_runs': [{'id': 1, 'run_attempt': state['attempt'], 'head_sha': state['sha'],
         'head_branch': 'feature', 'workflow_id': 1, 'event': 'push',
         'html_url': 'https://github.com/test/repo/actions/runs/1',
         'name': 'CI', 'status': 'completed', 'conclusion': 'failure' if state['failed'] else 'success'}]}
else: data = []
print(json.dumps(data))
if args[:2] == ['pr', 'checks'] and state['failed']: sys.exit(1)
''')
    fake_gh.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ["PATH"])
    agent = tmp_path / "fake-agent.py"
    agent.write_text('''
import json, os, sys, time
from pathlib import Path
args = sys.argv[1:]
state = json.loads(Path(os.environ['FAKE_GH_STATE']).read_text())
with Path(os.environ['FAKE_AGENT_CALLS']).open('a') as f:
 f.write(json.dumps({'argv': args, 'pid': os.getpid(), 'prompt': sys.stdin.read(), 'cwd': os.getcwd()})+'\\n')
if state.get('hang'): time.sleep(60)
time.sleep(state.get('delay', 0))
Path(args[args.index('-o') + 1]).write_text(json.dumps({'status': 'waiting', 'summary': 'Fixture repair returned'}))
''')
    home = tmp_path / "watch-home"
    db = supervisor.open_db(home)
    args = argparse.Namespace(cwd=str(worktree), session=sid, rollout=str(rollout), pr="1", repo="test/repo",
                              codex_command=json.dumps([sys.executable, str(agent)]), instructions="Test scope only",
                              poll_seconds=1, max_repairs=3, repair_timeout=2, headless=True)
    registered = supervisor.register(db, args)
    yield {"db": db, "job": registered, "home": home, "calls": calls, "state": state_file,
           "args": args, "worktree": worktree, "rollout": rollout}
    db.close()


def start_service(h):
    log = (h["home"] / "test-service.log").open("a")
    proc = subprocess.Popen([sys.executable, str(supervisor.SCRIPT), "--home", str(h["home"]), "serve", "--max-workers", "1"],
                            stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    log.close()
    return proc


def release_fixture(h):
    with h["db"]:
        j = supervisor.get_job(h["db"], h["job"]["id"])
        j.update(status="watching", next_poll=0)
        supervisor.save_job(h["db"], j)


def test_branch_only_worker_lifecycle_and_remote_divergence(harness, monkeypatch):
    h = harness
    with h["db"]:
        old = supervisor.get_job(h["db"], h["job"]["id"])
        old["status"] = "stopped"
        supervisor.save_job(h["db"], old)
    monkeypatch.setenv("FAKE_BRANCH_ONLY", "1")
    h["args"].pr = "auto"
    h["args"].branch = "feature"
    h["job"] = supervisor.register(h["db"], h["args"])
    assert h["job"]["branch"] == "feature"
    proc = start_service(h)
    try:
        release_fixture(h)
        wait_until(lambda: supervisor.get_job(h["db"], h["job"]["id"])["attempts"] == 1
                   and supervisor.get_job(h["db"], h["job"]["id"])["status"] == "watching")
        calls = [json.loads(line) for line in h["calls"].read_text().splitlines()]
        assert len(calls) == 1
        assert '"kind": "branch"' in calls[0]["prompt"]
        with pytest.raises(ProcessLookupError):
            os.kill(calls[0]["pid"], 0)
        remote = json.loads(h["state"].read_text())
        remote["sha"] = "different-remote-commit"
        h["state"].write_text(json.dumps(remote))
        wait_until(lambda: supervisor.get_job(h["db"], h["job"]["id"])["status"] == "blocked")
        assert "Local HEAD differs" in supervisor.get_job(h["db"], h["job"]["id"])["summary"]
        assert len(h["calls"].read_text().splitlines()) == 1
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_process_lifecycle_no_idle_agent_and_no_duplicate_after_restart(harness):
    h = harness
    proc = start_service(h)
    try:
        wait_until(lambda: (h["home"] / "heartbeat.json").exists())
        assert not h["calls"].exists()  # registration alone never runs an agent
        release_fixture(h)
        wait_until(lambda: supervisor.get_job(h["db"], h["job"]["id"])["status"] == "watching"
                   and supervisor.get_job(h["db"], h["job"]["id"])["attempts"] == 1)
        calls = [json.loads(v) for v in h["calls"].read_text().splitlines()]
        assert len(calls) == 1
        assert calls[0]["argv"][-3:] == ["resume", h["job"]["session_id"], "-"]
        assert calls[0]["cwd"] == str(h["worktree"])
        with pytest.raises(ProcessLookupError):
            os.kill(calls[0]["pid"], 0)  # agent process actually exited
        proc.terminate()
        proc.wait(timeout=5)
        proc = start_service(h)
        time.sleep(3)
        assert len(h["calls"].read_text().splitlines()) == 1
        state = json.loads(h["state"].read_text())
        state["attempt"] = 2
        h["state"].write_text(json.dumps(state))
        wait_until(lambda: supervisor.get_job(h["db"], h["job"]["id"])["attempts"] == 2)
        wait_until(lambda: supervisor.get_job(h["db"], h["job"]["id"])["status"] == "watching")
        assert len(h["calls"].read_text().splitlines()) == 2
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_timeout_kills_agent_and_blocks_watch(harness):
    h = harness
    state = json.loads(h["state"].read_text())
    state["hang"] = True
    h["state"].write_text(json.dumps(state))
    release_fixture(h)
    proc = start_service(h)
    try:
        wait_until(lambda: supervisor.get_job(h["db"], h["job"]["id"])["status"] == "blocked")
        call = json.loads(h["calls"].read_text().splitlines()[0])
        with pytest.raises(ProcessLookupError):
            os.kill(call["pid"], 0)
        assert supervisor.get_job(h["db"], h["job"]["id"])["attempts"] == 1
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_duplicate_registration_and_wrong_session_refused(harness):
    h = harness
    with pytest.raises(ValueError, match="already owns"):
        supervisor.register(h["db"], h["args"])
    with pytest.raises(ValueError, match="does not match"):
        supervisor.session_info(str(uuid.uuid4()), h["worktree"], h["rollout"])


def test_live_session_cannot_release_itself(harness):
    h = harness
    env = os.environ.copy()
    env["CODEX_THREAD_ID"] = h["job"]["session_id"]
    result = subprocess.run([sys.executable, str(supervisor.SCRIPT), "--home", str(h["home"]),
                             "release", h["job"]["id"]], env=env, text=True, capture_output=True)
    assert result.returncode == 1
    assert "Exit this interactive" in result.stdout
    assert supervisor.get_job(h["db"], h["job"]["id"])["status"] == "awaiting_release"
    assert not (h["home"] / "daemon.json").exists()


def test_dirty_worktree_blocks_before_agent_launch(harness):
    h = harness
    (h["worktree"] / "user-work.txt").write_text("unfinished user work")
    release_fixture(h)
    proc = start_service(h)
    try:
        wait_until(lambda: supervisor.get_job(h["db"], h["job"]["id"])["status"] == "blocked")
        assert not h["calls"].exists()
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_restart_during_repair_keeps_single_worker(harness):
    h = harness
    state = json.loads(h["state"].read_text())
    state["delay"] = 1
    h["state"].write_text(json.dumps(state))
    release_fixture(h)
    proc = start_service(h)
    try:
        wait_until(lambda: h["calls"].exists())
        proc.terminate()
        proc.wait(timeout=5)
        proc = start_service(h)
        wait_until(lambda: supervisor.get_job(h["db"], h["job"]["id"])["status"] == "watching")
        time.sleep(2)
        assert len(h["calls"].read_text().splitlines()) == 1
        assert supervisor.get_job(h["db"], h["job"]["id"])["attempts"] == 1
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_global_limit_queues_second_repair(harness, tmp_path):
    h = harness
    state = json.loads(h["state"].read_text())
    state["delay"] = 1
    h["state"].write_text(json.dumps(state))
    other_worktree = tmp_path / "other-worktree"
    subprocess.run(["git", "clone", str(h["worktree"]), str(other_worktree)], check=True, capture_output=True)
    second = copy.deepcopy(h["job"])
    second.update(id="second", cwd=str(other_worktree), session_id=str(uuid.uuid4()), status="watching")
    with h["db"]:
        supervisor.save_job(h["db"], second)
    release_fixture(h)
    proc = start_service(h)
    try:
        wait_until(lambda: h["calls"].exists())
        states = supervisor.jobs(h["db"])
        assert len([j for j in states if j["status"] == "running"]) == 1
        assert len(h["calls"].read_text().splitlines()) == 1
        wait_until(lambda: all(j["attempts"] == 1 and j["status"] == "watching" for j in supervisor.jobs(h["db"])))
        assert len(h["calls"].read_text().splitlines()) == 2
    finally:
        proc.terminate()
        proc.wait(timeout=5)
