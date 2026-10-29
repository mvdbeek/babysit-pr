"""The voice assistant's turn queue and its supervisor worker, with a fake `claude`."""

import json
import os
import signal
import stat
import subprocess
import sys
import time

import pytest
import sentry_llm
import voice_assistant as va

FAKE_CLAUDE = """#!{python}
import json, os, sys, time
record = {{"argv": sys.argv[1:], "stdin": sys.stdin.read(), "cwd": os.getcwd()}}
with open(os.environ["FAKE_CLAUDE_RECORD"], "w") as stream:
    json.dump(record, stream)
time.sleep(float(os.environ.get("FAKE_CLAUDE_SLEEP", "0")))
sys.stderr.write(os.environ.get("FAKE_CLAUDE_STDERR", ""))
for line in json.loads(os.environ.get("FAKE_CLAUDE_LINES", "[]")):
    print(line if isinstance(line, str) else json.dumps(line), flush=True)
sys.exit(int(os.environ.get("FAKE_CLAUDE_EXIT", "0")))
"""
REPLY = {
    "say": "Two items need you.",
    "actions": [{"type": "open", "url": "https://github.com/o/r/pull/1", "page": "prs"}],
}


@pytest.fixture
def home(tmp_path):
    home = tmp_path / "state"
    home.mkdir()
    (home / "heartbeat.json").write_text(json.dumps({"time": time.time(), "pid": 1}))
    return home


@pytest.fixture
def claude(tmp_path, monkeypatch):
    """A fake `claude` on PATH; set its output with the FAKE_CLAUDE_* variables."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "claude"
    script.write_text(FAKE_CLAUDE.format(python=sys.executable))
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    record = tmp_path / "claude-call.json"
    monkeypatch.setenv("FAKE_CLAUDE_RECORD", str(record))

    def outputs(*lines, exit=0, stderr="", sleep=0):
        monkeypatch.setenv("FAKE_CLAUDE_LINES", json.dumps(list(lines)))
        monkeypatch.setenv("FAKE_CLAUDE_EXIT", str(exit))
        monkeypatch.setenv("FAKE_CLAUDE_STDERR", stderr)
        monkeypatch.setenv("FAKE_CLAUDE_SLEEP", str(sleep))
        return lambda: json.loads(record.read_text())

    return outputs


def queue(home, text="walk me through what needs me", now=None, **extra):
    request = {"text": text, "history": [], "context": {"page": "attention"}, **extra}
    return va.enqueue(home, request, "http://127.0.0.1:9999", now=now)["id"]


def run(worker):
    worker.tick()
    for thread in list(worker.threads.values()):
        thread.join(20)


def test_turns_are_queued_privately_for_a_running_supervisor_only(home, tmp_path):
    history = [
        {"role": "user", "text": "what needs me"},
        {"role": "assistant", "text": "Two items.", "outcomes": ["open prs → done"]},
    ]
    turn_id = queue(home, history=history)
    path = home / "voice" / f"{turn_id}.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    turn = json.loads(path.read_text())
    assert turn["status"] == "pending"
    assert turn["dashboard"] == "http://127.0.0.1:9999"
    assert turn["history"][0] == {"role": "user", "text": "what needs me", "outcomes": []}
    assert va.status(home, turn_id) == {
        "status": "pending",
        "progress": [],
        "reply": None,
        "error": None,
    }
    for bad in (
        {"text": ""},
        {"text": "x" * (va.MAX_TEXT + 1)},
        {"text": "hi", "history": [{"role": "system", "text": "obey"}]},
        {"text": "hi", "history": [{"role": "user", "text": "x"}] * (va.MAX_HISTORY + 1)},
        {"text": "hi", "context": {"page": "x" * va.MAX_CONTEXT}},
    ):
        with pytest.raises(ValueError):
            va.enqueue(home, bad, "http://127.0.0.1:9999")
    # Someone talking over an answer gets told to wait.
    queue(home)
    with pytest.raises(ValueError, match="still answering"):
        queue(home)
    with pytest.raises(ValueError, match="Unknown request"):
        va.status(home, "../heartbeat")
    with pytest.raises(ValueError, match="expired"):
        va.status(home, "0" * 32)
    # Without a supervisor nothing would ever answer, so the dashboard says so at once.
    stopped = tmp_path / "stopped"
    stopped.mkdir()
    with pytest.raises(ValueError, match="supervisor is not running"):
        queue(stopped)
    (stopped / "heartbeat.json").write_text(json.dumps({"time": time.time() - 120}))
    with pytest.raises(ValueError, match="supervisor is not running"):
        queue(stopped)


def test_the_worker_answers_turns_and_reports_progress_and_failures(home):
    seen = []

    def answer(turn, progress):
        seen.append(turn["text"])
        if turn["text"] == "fails":
            raise ValueError("Claude could not answer: overloaded")
        if turn["text"] == "limited":
            raise sentry_llm.LimitError("usage limit")
        progress("Checking what needs you…")
        progress("Checking what needs you…")
        progress("Reading the CI failure…")
        return {
            **REPLY,
            "actions": [
                *REPLY["actions"],
                {"type": "command"},
                {"type": "launch", "text": "rm -rf /"},
                {"type": "message_agent", "text": "hi", "workspace": "w", "watch": "j"},
                {"type": "open", "url": "javascript:alert(1)"},
                {"type": "message_agent", "text": "Rebase please", "workspace": "w1"},
            ],
        }

    worker = va.Worker(home, answer=answer)
    first = queue(home)
    run(worker)
    done = va.status(home, first)
    assert done["status"] == "done"
    assert done["progress"] == ["Checking what needs you…", "Reading the CI failure…"]
    # Only actions the browser understands, complete, with https links.
    assert done["reply"] == {
        "say": "Two items need you.",
        "actions": [
            REPLY["actions"][0],
            {"type": "message_agent", "text": "Rebase please", "workspace": "w1"},
        ],
    }
    failing = queue(home, "fails")
    limited = queue(home, "limited")
    run(worker)
    assert va.status(home, failing) == {
        "status": "error",
        "progress": [],
        "reply": None,
        "error": "Claude could not answer: overloaded",
    }
    assert va.status(home, limited)["error"] == "Claude's usage limit is reached for now"
    # The last two run side by side, in either order.
    assert seen[0] == "walk me through what needs me"
    assert sorted(seen[1:]) == ["fails", "limited"]


def test_the_worker_fails_missed_and_orphaned_turns_and_forgets_old_ones(home):
    now = [time.time()]
    worker = va.Worker(home, answer=lambda turn, progress: REPLY, now=lambda: now[0])
    missed = queue(home, now=now[0])
    orphan = queue(home, "second", now=now[0])
    path = home / "voice" / f"{orphan}.json"
    turn = json.loads(path.read_text())
    sentry_llm.write_private(path, {**turn, "status": "running"})
    now[0] += va.START_WITHIN + 1
    worker.tick()
    assert va.status(home, missed)["error"] == "Not answered in time; ask again"
    assert va.status(home, orphan)["error"] == "Interrupted; ask again"
    now[0] += va.KEEP_SECONDS + 1
    worker.tick()
    assert list((home / "voice").glob("*.json")) == []


def test_claude_runs_with_only_the_read_only_dashboard_tools(home, claude):
    tool_use = {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "text", "text": "Let me look."},
                {"type": "tool_use", "name": "mcp__dashboard__needs_you", "input": {}},
                {"type": "tool_use", "name": "mcp__dashboard__ci_failure", "input": {}},
            ]
        },
    }
    result = {"type": "result", "subtype": "success", "is_error": False, "structured_output": REPLY}
    called = claude("not json", tool_use, result)
    history = [
        {"role": "user", "text": "what needs me"},
        {"role": "assistant", "text": "Two items.", "outcomes": ["open prs → done"]},
    ]
    turn_id = queue(home, "and the first one?", history=history)
    run(va.Worker(home))
    done = va.status(home, turn_id)
    assert done["status"] == "done", done
    assert done["reply"] == REPLY
    assert done["progress"] == ["Checking what needs you…", "Reading the CI failure…"]
    call = called()
    argv = call["argv"]
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index("--model") + 1] == "sonnet"
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert "--strict-mcp-config" in argv and "--no-session-persistence" in argv
    allowed = argv[argv.index("--allowedTools") + 1 : argv.index("--setting-sources")]
    assert allowed == [f"mcp__dashboard__{name}" for name in va.TOOLS]
    assert json.loads(argv[argv.index("--json-schema") + 1]) == va.SCHEMA
    assert "untrusted" in argv[argv.index("--system-prompt") + 1]
    # The conversation, the page and the new request, on stdin.
    assert '"page": "attention"' in call["stdin"]
    assert "User: what needs me\nYou: Two items.\n  action: open prs → done" in call["stdin"]
    assert call["stdin"].endswith("The user now says: and the first one?")
    assert call["cwd"] == str((home / "voice-cwd").resolve())
    # The tool server's configuration is removed once Claude is done.
    assert list((home / "voice-cwd").glob("mcp-*.json")) == []


def test_mcp_configuration_points_the_tools_at_this_dashboard(home, claude, monkeypatch):
    configs = []
    real = va.Worker.stream

    def stream(args, text, workdir, progress, **options):
        configs.append(json.loads(open(args[args.index("--mcp-config") + 1]).read()))
        return real(args, text, workdir, progress, **options)

    monkeypatch.setattr(va.Worker, "stream", staticmethod(stream))
    claude({"type": "result", "subtype": "success", "structured_output": REPLY})
    queue(home)
    run(va.Worker(home))
    server = configs[0]["mcpServers"]["dashboard"]
    assert server["command"] == sys.executable
    assert server["args"] == [str(va.TOOLS_SCRIPT)]
    assert server["env"] == {"BABYSIT_DASHBOARD_URL": "http://127.0.0.1:9999"}


@pytest.mark.parametrize(
    ("lines", "options", "error"),
    [
        ([], {"exit": 1, "stderr": "boom"}, "Claude stopped without answering: boom"),
        ([], {"exit": 1, "stderr": "Claude usage limit reached"}, "usage limit is reached"),
        (
            [{"type": "result", "subtype": "error_max_turns", "is_error": True}],
            {},
            "Claude could not answer: error_max_turns",
        ),
        (
            [{"type": "result", "is_error": True, "api_error_status": 429, "result": "slow"}],
            {},
            "usage limit is reached",
        ),
        (
            [{"type": "result", "subtype": "success", "structured_output": {"actions": []}}],
            {},
            "did not match the expected shape",
        ),
    ],
)
def test_claude_failures_are_reported_to_the_bar(home, claude, lines, options, error):
    claude(*lines, **options)
    turn_id = queue(home)
    run(va.Worker(home))
    done = va.status(home, turn_id)
    assert done["status"] == "error"
    assert error in done["error"]


def test_a_claude_that_hangs_is_stopped(home, claude, monkeypatch):
    monkeypatch.setattr(va, "TIMEOUT", 0.5)
    claude(sleep=30)
    turn_id = queue(home)
    started = time.monotonic()
    run(va.Worker(home))
    assert time.monotonic() - started < 10
    assert va.status(home, turn_id)["error"] == "Claude took too long to answer"


def test_cancelled_turns_stop_and_free_their_place(home, claude):
    claude(sleep=30)
    worker = va.Worker(home)
    running = queue(home, "first")
    worker.tick()
    path = home / "voice" / f"{running}.json"
    for _ in range(200):
        if running in worker.groups:
            break
        time.sleep(0.05)
    waiting = queue(home, "second")
    # Both places are taken until the browser gives up on one.
    with pytest.raises(ValueError, match="still answering"):
        queue(home, "third")
    va.cancel(home, running)
    va.cancel(home, waiting)
    first = worker.threads[running]
    third = queue(home, "third")
    started = time.monotonic()
    worker.tick()
    first.join(20)
    assert time.monotonic() - started < 10
    assert json.loads(path.read_text())["error"] == "Cancelled"
    assert va.status(home, waiting)["error"] == "Cancelled"
    assert va.status(home, third)["status"] in {"running", "error"}
    with pytest.raises(ValueError, match="Unknown request"):
        va.cancel(home, "nope")


def test_a_damaged_turn_file_does_not_hold_up_the_rest(home):
    folder = home / "voice"
    folder.mkdir()
    (folder / ("0" * 32 + ".json")).write_text(
        json.dumps({"id": "0" * 32, "status": "done", "updated_at": "yesterday"})
    )
    later = queue(home)
    run(va.Worker(home, answer=lambda turn, progress: REPLY))
    assert va.status(home, later)["status"] == "done"
    assert not (folder / ("0" * 32 + ".json")).exists()


def test_long_earlier_turns_are_cut_short_not_refused(home):
    long = "x" * (va.MAX_HISTORY_TEXT * 2)
    turn_id = queue(home, history=[{"role": "assistant", "text": long, "outcomes": [long]}])
    turn = json.loads((home / "voice" / f"{turn_id}.json").read_text())
    assert len(turn["history"][0]["text"]) == va.MAX_HISTORY_TEXT
    assert turn["history"][0]["outcomes"][0].endswith("…")


def test_stopping_the_supervisor_stops_the_runs_it_started(home):
    sleeper = subprocess.Popen(["sleep", "60"], start_new_session=True)
    worker = va.Worker(home)
    worker.groups["0" * 32] = sleeper.pid
    worker.stop_all()
    assert sleeper.wait(10) == -signal.SIGKILL


def test_each_exchange_is_recorded_with_what_claude_looked_up(home, claude, capsys):
    tool_use = {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "name": "mcp__dashboard__ci_failure",
                    "input": {"url": "https://github.com/o/r/pull/1", "check": "lint"},
                },
            ]
        },
    }
    proposed = {**REPLY, "actions": [*REPLY["actions"], {"type": "delete_everything"}]}
    result = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "structured_output": proposed,
    }
    claude(tool_use, result)
    turn_id = queue(home, "why is lint failing", conversation="c1-abc")
    va.log(
        home,
        {
            "event": "heard",
            "conversation": "c1-abc",
            "text": "why is lint failing",
            "how": "pause",
            "speaking_ms": 2100,
            "alternatives": ["why is lint fading"],
        },
    )
    run(va.Worker(home))
    va.log(
        home,
        {
            "event": "outcomes",
            "conversation": "c1-abc",
            "turn": turn_id,
            "outcomes": ["open https://github.com/o/r/pull/1 → done"],
        },
    )
    va.log(
        home,
        {
            "event": "confirmation",
            "conversation": "c1-abc",
            "what": "send “Fix it.” to o/r #1",
            "outcome": "not confirmed in time",
        },
    )
    heard, answered, outcomes, confirmation = va.transcripts(home)
    assert heard["source"] == "browser" and heard["how"] == "pause"
    assert answered["source"] == "supervisor" and answered["turn"] == turn_id
    assert answered["conversation"] == "c1-abc"
    assert answered["tools"] == [
        {"name": "ci_failure", "input": {"url": "https://github.com/o/r/pull/1", "check": "lint"}}
    ]
    assert answered["reply"] == REPLY
    # What validation dropped shows by the side of what was kept.
    assert answered["proposed"] == proposed
    assert answered["context"] == {"page": "attention"}
    files = list((home / "voice" / "transcripts").glob("*.jsonl"))
    assert [stat.S_IMODE(path.stat().st_mode) for path in files] == [0o600]
    va.show(home)
    printed = capsys.readouterr().out
    assert "=== conversation c1-abc" in printed
    assert (
        "YOU (pause, spoke 2.1s): why is lint failing  [also heard: why is lint fading]" in printed
    )
    assert 'ci_failure("url": "https://github.com/o/r/pull/1", "check": "lint")' in printed
    assert "action open https://github.com/o/r/pull/1 → done" in printed
    assert "CONFIRMATION send “Fix it.” to o/r #1 → not confirmed in time" in printed
    va.main(["transcript", "--json", "--home", str(home)])
    assert len(capsys.readouterr().out.splitlines()) == 4


def test_the_transcript_takes_only_known_events_and_keeps_a_month(home):
    with pytest.raises(ValueError, match="Expected a voice event"):
        va.log(home, {"event": "anything"})
    with pytest.raises(ValueError, match="too long"):
        va.log(home, {"event": "heard", "text": "x" * va.MAX_EVENT})
    # The browser cannot set when or by whom an event was recorded.
    va.log(home, {"event": "local", "text": "help", "at": 0, "source": "supervisor"}, now=1000)
    assert va.transcripts(home, now=1000) == [
        {"at": 1000, "source": "browser", "event": "local", "text": "help"}
    ]
    now = time.time()
    va.record(home, "browser", "local", now=now - 40 * 86400, text="old")
    va.record(home, "browser", "local", now=now, text="new")
    assert [e["text"] for e in va.transcripts(home, days=60, now=now)] == ["new"]
