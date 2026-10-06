import io
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import claude_accounts
import claude_runner
import pane_runner
import pr_supervisor as supervisor
import pytest
from test_pr_supervisor import snapshot


def transcript(path, sid, cwd, **extra):
    item = {
        "type": "assistant",
        "sessionId": sid,
        "cwd": str(cwd),
        "isSidechain": False,
        "message": {"model": "claude-test-model", "content": []},
        **extra,
    }
    path.write_text(json.dumps(item) + "\n")


def test_validates_claude_identity_model_and_plan_mode(tmp_path):
    sid = str(uuid.uuid4())
    path = tmp_path / "session.jsonl"
    transcript(path, sid, tmp_path, permissionMode="plan")
    info = claude_runner.session_info(sid, tmp_path, path)
    assert info["model"] == "claude-test-model"
    assert info["claude_permission_mode"] == "plan"
    with pytest.raises(ValueError, match="session ID"):
        claude_runner.session_info(str(uuid.uuid4()), tmp_path, path)
    with pytest.raises(ValueError, match="cwd"):
        claude_runner.session_info(sid, tmp_path / "other", path)
    transcript(path, sid, tmp_path, isSidechain=True)
    with pytest.raises(ValueError, match="subagent"):
        claude_runner.session_info(sid, tmp_path, path)


def test_claude_cli_resumes_exact_session_without_permission_bypass():
    job = {
        "agent": "claude",
        "session_id": "exact-id",
        "claude_command": ["existing-launcher", "claude"],
        "model": "saved-model",
        "claude_permission_mode": "dontAsk",
    }
    argv = supervisor.command_for(job, Path("unused"))
    assert argv[:2] == ["existing-launcher", "claude"]
    assert argv[argv.index("--resume") + 1] == "exact-id"
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert "--fork-session" not in argv and "--continue" not in argv
    assert not any("bypass" in item or "skip-permissions" in item for item in argv)


@pytest.mark.parametrize(
    "problem", ["denied", "wrong_session", "error", "missing_structured", "duplicate"]
)
def test_bad_claude_results_never_acknowledge_work(tmp_path, problem):
    path = tmp_path / "agent.log"
    result = {
        "type": "result",
        "subtype": "success",
        "session_id": "sid",
        "is_error": False,
        "structured_output": {"status": "waiting", "summary": "done"},
    }
    if problem == "denied":
        result["permission_denials"] = [{"tool_name": "Bash"}]
    if problem == "wrong_session":
        result["session_id"] = "different"
    if problem == "error":
        result["is_error"] = True
    if problem == "missing_structured":
        result.pop("structured_output")
    path.write_text((json.dumps(result) + "\n") * (2 if problem == "duplicate" else 1))
    if problem == "denied":
        assert claude_runner.read_result(path, "sid")["status"] == "blocked"
    else:
        with pytest.raises(RuntimeError):
            claude_runner.read_result(path, "sid")


def test_claude_output_is_visible_and_raw_stream_is_logged(capsys):
    events = [
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "Checking CI"},
                    {"type": "tool_use", "name": "Bash", "input": {"command": "git status"}},
                ]
            },
        },
        {"type": "result", "structured_output": {"status": "waiting", "summary": "Done"}},
    ]
    raw = "".join(json.dumps(e) + "\n" for e in events).encode()
    log = io.BytesIO()
    claude_runner.stream_output(io.BytesIO(raw), log, True)
    visible = capsys.readouterr().out
    assert "Checking CI" in visible and "[Bash]" in visible and "Done" in visible
    assert log.getvalue() == raw


def test_capture_matches_original_agent_kind(monkeypatch, tmp_path):
    info = {"terminal_id": "term", "cwd": str(tmp_path), "agent": "claude"}
    procs = {"shell_pid": 100, "foreground_processes": [{"pid": 200}]}
    monkeypatch.setattr(pane_runner, "inspect", lambda pane: (info, procs))
    assert pane_runner.capture("w1:p1", tmp_path, "claude")["terminal_id"] == "term"
    with pytest.raises(RuntimeError):
        pane_runner.capture("w1:p1", tmp_path, "codex")


@pytest.mark.parametrize("denied", [False, True])
def test_claude_worker_registers_resumes_and_records_outcome(harness, monkeypatch, denied):
    h = harness
    old = h["job"]
    with h["db"]:
        old["status"] = "stopped"
        supervisor.save_job(h["db"], old)
    sid = str(uuid.uuid4())
    path = h["home"].parent / "claude.jsonl"
    transcript(path, sid, h["worktree"])
    command = h["home"].parent / "fake-claude.py"
    command.write_text("""
import json, os, sys
from pathlib import Path
args=sys.argv[1:]
assert 'CLAUDECODE' not in os.environ
assert 'ANTHROPIC_API_KEY' not in os.environ
assert args[args.index('--permission-mode') + 1] == 'dontAsk'
prompt=sys.stdin.read()
with Path(os.environ['FAKE_AGENT_CALLS']).open('a') as out:
 out.write(json.dumps({'argv': args, 'pid': os.getpid(), 'prompt': prompt, 'config_dir': os.environ.get('CLAUDE_CONFIG_DIR')})+'\\n')
print(json.dumps({'type':'assistant','message':{'content':[{'type':'text','text':'Resumed Claude work'}]}}),flush=True)
print(json.dumps({'type':'result','subtype':'success','session_id':args[args.index('--resume')+1],
 'is_error':False, 'permission_denials':[{'tool_name':'Bash'}] if os.environ.get('FAKE_DENIED') else [],
 'structured_output':{'status':'waiting','summary':'Claude work finished'}}),flush=True)
""")
    args = h["args"]
    args.agent = "claude"
    args.session = sid
    args.rollout = str(path)
    args.codex_command = None
    args.claude_command = json.dumps([sys.executable, str(command)])
    monkeypatch.setenv("HOME", str(h["home"].parent / "account-home"))
    account = claude_accounts.account_home("work", create=True)
    args.claude_account = "work"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(account))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "wrong-fixture-account")
    monkeypatch.setenv("CLAUDECODE", "1")
    if denied:
        monkeypatch.setenv("FAKE_DENIED", "1")
    job = supervisor.register(h["db"], args)
    assert job["agent"] == "claude" and "codex_command" not in job
    saved_home = job["claude_config_dir"]
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(h["home"].parent / "other-account"))
    attempt = str(uuid.uuid4())
    folder = h["home"] / "runs" / attempt
    folder.mkdir(parents=True)
    job.update(status="running", attempt=attempt, snapshot=snapshot())
    job["snapshot"]["pr"]["head_sha"] = supervisor.git(h["worktree"], "rev-parse", "HEAD")
    with h["db"]:
        supervisor.save_job(h["db"], job)
    subprocess.run(
        [
            sys.executable,
            str(supervisor.SCRIPT),
            "--home",
            str(h["home"]),
            "_repair",
            job["id"],
            attempt,
        ],
        check=True,
        capture_output=True,
        timeout=15,
    )
    outcome = json.loads((folder / "result.json").read_text())
    assert outcome["status"] == ("blocked" if denied else "waiting")
    call = json.loads(h["calls"].read_text())
    assert call["argv"][call["argv"].index("--resume") + 1] == sid
    assert call["config_dir"] == saved_home
    assert (Path(saved_home) / "babysit-pr-locks" / f"{sid}.lock").exists()
    with pytest.raises(ProcessLookupError):
        os.kill(call["pid"], 0)


@pytest.mark.parametrize(
    "message",
    [
        "Permission to use Bash has been denied because Claude Code is running in don't ask mode.",
        None,
        42,
        ["unexpected"],
        {"content": "plain content"},
        {
            "content": [
                {"type": "text", "text": None},
                {"type": "text", "text": {"unexpected": True}},
            ]
        },
    ],
)
def test_unexpected_message_shapes_do_not_drop_final_result(tmp_path, capsys, message):
    events = [
        {"type": "system", "subtype": "permission_denied", "message": message},
        {
            "type": "result",
            "subtype": "success",
            "session_id": "sid",
            "is_error": False,
            "permission_denials": [{"tool_name": "Bash"}],
            "structured_output": {"status": "blocked", "summary": "Permission required"},
        },
    ]
    raw = "".join(json.dumps(e) + "\n" for e in events).encode()
    log = tmp_path / "agent.log"
    with log.open("wb") as stream:
        claude_runner.stream_output(io.BytesIO(raw), stream, True)
    assert log.read_bytes() == raw
    assert claude_runner.read_result(log, "sid")["status"] == "blocked"
    if isinstance(message, str):
        assert message in capsys.readouterr().out


def test_display_failure_keeps_draining_large_stream_and_records_result(
    tmp_path, monkeypatch, capsys
):
    def broken(event):
        raise AttributeError("future event shape")

    monkeypatch.setattr(claude_runner, "display_event", broken)
    payload = {"type": "system", "message": "x" * 8192}
    result = {
        "type": "result",
        "subtype": "success",
        "session_id": "sid",
        "is_error": False,
        "structured_output": {"status": "waiting", "summary": "Completed"},
    }
    code = (
        "import json\nfor _ in range(32): print(json.dumps("
        + repr(payload)
        + "))\nprint(json.dumps("
        + repr(result)
        + "))"
    )
    proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE)
    path = tmp_path / "agent.log"
    try:
        with path.open("wb") as stream:
            claude_runner.stream_output(proc.stdout, stream, True)
        assert proc.wait(timeout=5) == 0
        assert claude_runner.read_result(path, "sid")["summary"] == "Completed"
        assert len(path.read_text().splitlines()) == 33
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        proc.stdout.close()


def test_safehouse_launcher_owns_permission_mode_and_preserves_session():
    job = {
        "claude_command": claude_runner.safehouse_command(),
        "claude_permission_mode": "safehouse",
        "session_id": "original-session",
        "model": "saved-model",
    }
    argv = claude_runner.command_for(job, supervisor.RESULT_SCHEMA)
    assert argv[:2] == claude_runner.safehouse_command()
    assert argv[argv.index("--resume") + 1] == "original-session"
    assert "--permission-mode" not in argv
    assert "--dangerously-skip-permissions" not in argv  # supplied only inside Safehouse
    job["claude_command"] = ["claude"]
    with pytest.raises(ValueError, match="verified Safehouse launcher"):
        claude_runner.command_for(job, supervisor.RESULT_SCHEMA)


def test_default_claude_registration_uses_safehouse(harness, monkeypatch):
    h = harness
    old = h["job"]
    supervisor.stop_watch(h["db"], old["id"])
    sid = str(uuid.uuid4())
    path = h["home"].parent / "claude-default.jsonl"
    transcript(path, sid, h["worktree"])
    args = h["args"]
    args.agent = "claude"
    args.session = sid
    args.rollout = str(path)
    args.codex_command = None
    args.claude_command = None
    job = supervisor.register(h["db"], args)
    assert job["claude_command"] == claude_runner.safehouse_command()
    assert job["claude_permission_mode"] == "safehouse"
