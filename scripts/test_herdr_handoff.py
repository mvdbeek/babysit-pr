"""Exercise handoff ownership without sending keys to any real terminal."""

import argparse
import copy
import json

import herdr_handoff as handoff
import pr_supervisor as supervisor
import pytest

EMPTY = "› \x1b[2mAsk Codex to do anything\x1b[0m"


@pytest.mark.parametrize(
    "screen,expected",
    [
        (EMPTY, True),
        (EMPTY + "  ⠁⣀", True),
        ("› Ask Codex to do anything", False),
        ("› \x1b[38;2;2;2;2mAsk Codex to do anything", False),
        ("› \x1b[48;2;30;30;30m\x1b[2mAsk Codex to do anything", True),
        (EMPTY + " draft", False),
        (EMPTY + "\n› please keep working", False),
        ("› please keep working", False),
        ("another UI", False),
    ],
)
def test_empty_composer(screen, expected):
    assert handoff.empty_composer(screen) is expected


@pytest.fixture
def rig(tmp_path, monkeypatch):
    db = supervisor.open_db(tmp_path)
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text("stable")
    target = dict(
        job_id="watch",
        session_id="session",
        rollout=str(rollout),
        cwd=str(tmp_path),
        pane_id="w1:p1",
        terminal_id="terminal",
        shell_pid=100,
        agent_pid=200,
        agent_argv=["codex"],
        turn_id="turn",
        token="a" * 32,
        marker="[receipt]",
        timeout=2,
        epoch=2,
    )
    with db:
        supervisor.save_job(
            db, dict(id="watch", status="handoff", epoch=2, handoff_token=target["token"])
        )
    info = dict(
        terminal_id="terminal",
        agent="codex",
        agent_status="idle",
        scroll=dict(offset_from_bottom=0),
    )
    procs = dict(
        shell_pid=100,
        foreground_processes=[dict(pid=200, argv0="codex", argv=["codex"], cwd=str(tmp_path))],
    )
    state = dict(
        screen="[receipt]\n" + EMPTY,
        info=info,
        procs=procs,
        turn=("turn", "[receipt]"),
        sent=[],
        exited=False,
        confirm=True,
        clock=0,
        reads=0,
        state_change_seq=5,
    )

    def inspect(_):
        return (
            copy.deepcopy(state["info"]),
            copy.deepcopy(state["procs"]),
            {"state_change_seq": state["state_change_seq"]},
        )

    def herdr(*args):
        if args[:2] == ("agent", "read"):
            state["reads"] += 1
            return state.get("screen2", state["screen"]) if state["reads"] == 2 else state["screen"]
        assert args == ("agent", "send-keys", "w1:p1", "ctrl+d")
        assert supervisor.get_job(db, "watch")["status"] == "handoff"
        state["sent"].append(args)
        state["exited"] = state["confirm"]
        return "{}"

    def result(*args):
        if args[:2] == ("pane", "get"):
            return {"pane": info}
        assert args[:2] == ("pane", "process-info")
        foreground = [{"pid": 100}] if state["exited"] else procs["foreground_processes"]
        return {"process_info": dict(shell_pid=100, foreground_processes=foreground)}

    def kill(pid, signal):
        assert (pid, signal) == (200, 0)
        if state["exited"]:
            raise ProcessLookupError()

    def sleep(seconds):
        state["clock"] += seconds
        if state.get("pause"):
            with db:
                supervisor.save_job(db, dict(id="watch", status="paused", epoch=3))
        if state.get("on_sleep"):
            state["on_sleep"]()

    monkeypatch.setattr(handoff, "inspect", inspect)
    monkeypatch.setattr(handoff, "herdr", herdr)
    monkeypatch.setattr(handoff, "result", result)
    monkeypatch.setattr(handoff, "latest_turn", lambda *a: state["turn"])
    monkeypatch.setattr(handoff.os, "kill", kill)
    monkeypatch.setattr(handoff.time, "monotonic", lambda: state["clock"])
    monkeypatch.setattr(handoff.time, "sleep", sleep)
    yield target, tmp_path, db, state
    db.close()


def test_success_exits_once_then_releases(rig):
    target, home, db, state = rig
    handoff.perform(target, home, db)
    assert len(state["sent"]) == 1
    assert supervisor.get_job(db, "watch")["status"] == "watching"
    assert (
        json.loads((home / "handoffs" / (target["token"] + ".audit.json")).read_text())["stage"]
        == "released"
    )


@pytest.mark.parametrize("screen", [EMPTY, "[receipt]", "[receipt]\n› "])
def test_waits_for_final_display_then_exits_once(rig, screen):
    target, home, db, state = rig
    state["screen"] = screen
    state["on_sleep"] = lambda: state.update(screen="[receipt]\n" + EMPTY)
    handoff.perform(target, home, db)
    assert len(state["sent"]) == 1
    assert state["clock"] == handoff.SCREEN_SETTLE_INTERVAL
    assert supervisor.get_job(db, "watch")["status"] == "watching"
    audit = json.loads((home / "handoffs" / (target["token"] + ".audit.json")).read_text())
    assert audit["first_failed_screen"] == screen
    assert audit["last_screen"] == "[receipt]\n" + EMPTY


def test_display_wait_is_bounded_and_saves_failed_screen(rig):
    target, home, db, state = rig
    target["timeout"] = 600
    state["screen"] = EMPTY
    with pytest.raises(RuntimeError, match="Timed out waiting for final display"):
        handoff.perform(target, home, db)
    assert state["clock"] == handoff.SCREEN_SETTLE_TIMEOUT
    assert not state["sent"]
    assert supervisor.get_job(db, "watch")["status"] == "awaiting_release"
    audit = json.loads((home / "handoffs" / (target["token"] + ".audit.json")).read_text())
    assert audit["stage"] == "error"
    assert audit["last_failed_screen"] == EMPTY
    assert "Final receipt" in audit["screen_error"]


@pytest.mark.parametrize(
    "change",
    [
        "draft",
        "working",
        "new_turn",
        "question",
        "process",
        "sequence",
        "rollout",
        "pause",
        "queued",
        "scroll",
    ],
)
def test_changes_during_display_wait_cancel(rig, change):
    target, home, db, state = rig
    state["screen"] = EMPTY

    def changed():
        state["screen"] = "[receipt]\n" + EMPTY
        if change == "draft":
            state["screen"] = "[receipt]\n› my draft"
        elif change == "working":
            state["info"]["agent_status"] = "working"
        elif change == "new_turn":
            state["turn"] = ("new", None)
        elif change == "question":
            state["info"]["agent_status"] = "blocked"
        elif change == "process":
            state["procs"]["foreground_processes"][0]["pid"] = 201
        elif change == "sequence":
            state["state_change_seq"] += 1
        elif change == "rollout":
            (home / "rollout.jsonl").write_text("new activity")
        elif change == "pause":
            with db:
                supervisor.save_job(db, dict(id="watch", status="paused", epoch=3))
        elif change == "queued":
            state["screen"] += "\nQueued follow-up inputs"
        elif change == "scroll":
            state["info"]["scroll"]["offset_from_bottom"] = 5

    state["on_sleep"] = changed
    with pytest.raises(RuntimeError):
        handoff.perform(target, home, db)
    assert not state["sent"]
    assert state["clock"] == handoff.SCREEN_SETTLE_INTERVAL
    assert supervisor.get_job(db, "watch")["status"] == (
        "paused" if change == "pause" else "awaiting_release"
    )


@pytest.mark.parametrize(
    "reason", ["draft", "question", "new_turn", "process", "terminal", "queued", "race", "scroll"]
)
def test_changes_cancel_without_keys(rig, reason):
    target, home, db, state = rig
    if reason == "draft":
        state["screen"] = "[receipt]\n› my draft"
    elif reason == "question":
        state["info"]["agent_status"] = "blocked"
    elif reason == "new_turn":
        state["turn"] = ("new", None)
    elif reason == "process":
        state["procs"]["foreground_processes"][0]["pid"] = 201
    elif reason == "terminal":
        state["info"]["terminal_id"] = "replaced"
    elif reason == "queued":
        state["screen"] += "\nQueued follow-up inputs"
    elif reason == "race":
        state["screen2"] = "[receipt]\n› new draft"
    elif reason == "scroll":
        state["info"]["scroll"]["offset_from_bottom"] = 5
    with pytest.raises(RuntimeError):
        handoff.perform(target, home, db)
    assert not state["sent"]
    assert state["clock"] == 0
    assert supervisor.get_job(db, "watch")["status"] == "awaiting_release"


def test_unconfirmed_exit_never_sends_twice_or_releases(rig):
    target, home, db, state = rig
    state["confirm"] = False
    with pytest.raises(RuntimeError, match="Exit unconfirmed"):
        handoff.perform(target, home, db)
    assert len(state["sent"]) == 1
    assert supervisor.get_job(db, "watch")["status"] == "awaiting_release"


def test_pause_cancels_wait_and_preserves_status(rig):
    target, home, db, state = rig
    state.update(turn=("turn", None), pause=True)
    with pytest.raises(RuntimeError, match="ownership changed"):
        handoff.perform(target, home, db)
    assert not state["sent"]
    assert supervisor.get_job(db, "watch")["status"] == "paused"


@pytest.mark.parametrize("failure", ["wrong_session", "root_process", "spawn"])
def test_schedule_failure_leaves_watch_inactive(rig, monkeypatch, failure):
    target, home, db, state = rig
    with db:
        supervisor.save_job(
            db,
            dict(
                id="watch",
                status="awaiting_release",
                epoch=1,
                session_id="session",
                rollout=target["rollout"],
                cwd=target["cwd"],
            ),
        )
    state["turn"] = ("turn", None)
    monkeypatch.setenv("CODEX_THREAD_ID", "wrong" if failure == "wrong_session" else "session")
    monkeypatch.setattr(supervisor, "start_daemon", lambda *a: None)
    if failure == "root_process":
        state["procs"]["shell_pid"] = 200

    def fail_spawn(*a, **kw):
        raise OSError("simulated spawn failure")

    monkeypatch.setattr(handoff.subprocess, "Popen", fail_spawn)
    with pytest.raises((ValueError, OSError)):
        handoff.schedule(
            db, home, argparse.Namespace(id="watch", pane="w1:p1", timeout=2, max_workers=2)
        )
    assert supervisor.get_job(db, "watch")["status"] == "awaiting_release"
    assert not state["sent"]


def test_rollout_requires_exact_turn_and_session(tmp_path):
    path = tmp_path / "rollout.jsonl"
    items = [
        dict(type="session_meta", payload=dict(id="session")),
        dict(type="event_msg", payload=dict(type="task_started", turn_id="one")),
        dict(
            type="event_msg",
            payload=dict(type="task_complete", turn_id="one", last_agent_message="done"),
        ),
    ]
    path.write_text("\n".join(json.dumps(item) for item in items))
    assert handoff.latest_turn(path, "session") == ("one", "done")
    with pytest.raises(RuntimeError):
        handoff.latest_turn(path, "other")
    with path.open("a") as stream:
        stream.write(
            "\n"
            + json.dumps(dict(type="event_msg", payload=dict(type="task_started", turn_id="two")))
        )
    assert handoff.latest_turn(path, "session") == ("two", None)
