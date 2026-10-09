"""Scheduled-agent exit decisions against faked herdr calls; no key reaches a terminal."""

import copy
import json
import os
import time

import herdr_handoff as handoff
import pytest
import workspace_exit as we

MARKER = "[babysit-done:0123456789abcdef]"
BRIEF = "Fix the bug" + we.brief(MARKER)
DONE = f"Fixed and tested.\n\n{MARKER}"
RULE = "\x1b[38;2;136;136;136m" + "─" * 40 + "\x1b[0m"
EMPTY = {
    "codex": "› \x1b[2mAsk Codex to do anything\x1b[0m",
    "claude": RULE + "\n❯\xa0\n" + RULE,
}
SID = "8f2c6f0e-3b7a-4d34-9a8f-2b8d6c1e9a10"


def write_jsonl(path, *items):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(item) + "\n" for item in items))


def transcript(path, agent, turns):
    """A Codex rollout or Claude transcript; each turn is (prompt, final response or None)."""
    if agent == "codex":
        items = [{"type": "session_meta", "payload": {"id": SID}}]
        for index, (prompt, final) in enumerate(turns):
            turn = f"turn{index}"
            items += [
                {"type": "event_msg", "payload": {"type": "task_started", "turn_id": turn}},
                {"type": "event_msg", "payload": {"type": "user_message", "message": prompt}},
            ]
            if final is not None:
                items.append(
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "task_complete",
                            "turn_id": turn,
                            "last_agent_message": final,
                        },
                    }
                )
    else:
        items = [{"type": "user", "isMeta": True, "sessionId": SID, "message": {"content": "ctx"}}]
        for index, (prompt, final) in enumerate(turns):
            items.append(
                {
                    "type": "user",
                    "uuid": f"turn{index}",
                    "sessionId": SID,
                    "message": {"content": prompt},
                }
            )
            if final is not None:
                items += [
                    {
                        "type": "assistant",
                        "sessionId": SID,
                        "message": {"content": [{"type": "text", "text": final}]},
                    },
                    {"type": "system", "subtype": "turn_duration"},
                ]
    write_jsonl(path, *items)


@pytest.fixture(params=["codex", "claude"])
def rig(tmp_path, monkeypatch, request):
    agent = request.param
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    rollout = tmp_path / "rollout.jsonl"
    op = {
        "status": "complete",
        "agent": agent,
        "path": str(worktree),
        "result": {"workspace_id": "w1"},
        "exit_marker": MARKER,
        "created_at": 990,
        "updated_at": 1000,
    }
    state = {
        "agent": agent,
        "agents": [
            {"workspace_id": "w1", "agent": agent, "cwd": str(worktree), "pane_id": "w1:p1"}
        ],
        "info": {
            "terminal_id": "terminal",
            "agent": agent,
            "agent_status": "idle",
            "scroll": {"offset_from_bottom": 0},
        },
        "procs": {
            "shell_pid": 100,
            "foreground_processes": [
                {"pid": 200, "argv0": agent, "argv": [agent, "brief"], "cwd": str(worktree)}
            ],
        },
        "seq": 5,
        "screen": f"{BRIEF}\n{DONE}\n{EMPTY[agent]}",
        "sent": [],
        "persisted": [],
        "clock": 1000,
        "inspections": 0,
    }

    def turns(*items):
        transcript(rollout, agent, items)

    turns((BRIEF, DONE))

    def result(*args):
        assert args == ("agent", "list")
        return {"agents": copy.deepcopy(state["agents"])}

    def inspect(pane):
        assert pane == "w1:p1"
        state["inspections"] += 1
        if state.get("on_inspect"):
            state["on_inspect"](state["inspections"])
        return (
            copy.deepcopy(state["info"]),
            copy.deepcopy(state["procs"]),
            {"state_change_seq": state["seq"]},
        )

    def send_exit(target, expected, before, owned, save):
        assert expected == handoff.latest_turn(rollout, SID, agent)
        assert before == handoff.fingerprint(rollout)
        # The record says "exiting" durably before any key goes out.
        assert state["persisted"][-1]["state"] == "exiting"
        state["sent"].append(target["pane_id"])
        if state.get("exit_error"):
            raise RuntimeError(state["exit_error"])

    def persist(record):
        if state.get("conflict"):
            raise we.Leave("This watch changed elsewhere; nothing was sent")
        state["persisted"].append(copy.deepcopy(record))

    monkeypatch.setattr(handoff, "result", result)
    monkeypatch.setattr(handoff, "inspect", inspect)
    monkeypatch.setattr(handoff, "read_screen", lambda pane: state["screen"])
    monkeypatch.setattr(handoff, "send_exit", send_exit)
    monkeypatch.setattr(
        we, "locate", lambda target, since: {"rollout": str(rollout), "session_id": SID}
    )
    record = we.watching(now=1000)

    def step(advance=we.SETTLE_SECONDS):
        state["clock"] += advance
        return we.step(record, op, persist, now=state["clock"])

    return op, record, state, step, turns


def test_brief_asks_for_the_marker_line():
    marker = we.new_marker()
    assert marker.startswith("[babysit-done:") and marker != we.new_marker()
    assert f"\n{marker}\n" in we.brief(marker)


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (DONE, True),
        (f"Done.\n`{MARKER}`\n", True),
        (f"Done.\n```\n{MARKER}\n```\n", True),
        (f"I won't add {MARKER} yet. Should I continue?", False),
        (f"{MARKER}\nShould I also update the docs?", False),
        ("", False),
    ],
)
def test_only_a_final_marker_line_confirms(response, expected):
    assert we.confirms(response, MARKER) is expected


def test_confirmed_idle_agent_exits_once_it_stays_settled(rig):
    op, record, state, step, _ = rig
    step()
    assert record["state"] == "watching" and not state["sent"]
    assert record["target"]["agent_pid"] == 200
    step(advance=we.SETTLE_SECONDS - 1)  # Too soon after the first settled pass.
    assert not state["sent"]
    step(advance=1)
    assert state["sent"] == ["w1:p1"]
    assert record["state"] == "exited"
    # A finished watch never acts again.
    step()
    assert state["sent"] == ["w1:p1"]


def test_change_between_passes_restarts_the_settle(rig):
    op, record, state, step, _ = rig
    step()
    state["seq"] = 6
    step()
    assert not state["sent"] and record["state"] == "watching"
    step()
    assert state["sent"] == ["w1:p1"]


def test_change_just_before_the_key_cancels_it(rig):
    op, record, state, step, _ = rig
    step()
    # The user starts typing between the pass's checks and the key.
    state["on_inspect"] = lambda n: state.update(seq=7) if n == 4 else None
    step()
    assert not state["sent"]
    assert record["state"] == "watching" and "ready" not in record
    assert state["persisted"][-1]["state"] == "exiting"


def test_using_the_pane_restarts_the_settle(rig):
    op, record, state, step, _ = rig
    step()
    state["info"]["scroll"] = {"offset_from_bottom": 3}  # The user scrolls up to read.
    step()
    state["info"]["scroll"] = {"offset_from_bottom": 0}
    step(advance=1)
    assert not state["sent"] and record["state"] == "watching"
    step()
    assert state["sent"] == ["w1:p1"]


def test_a_competing_claim_sends_nothing(rig):
    op, record, state, step, _ = rig
    step()
    state["conflict"] = True
    step()
    assert not state["sent"] and record["state"] == "left_open"


def test_left_open_when_the_turn_ends_without_confirming(rig):
    op, record, state, step, turns = rig
    turns((BRIEF, f"Should I also update the docs? (I'll add {MARKER} when done)"))
    step()
    assert record["state"] == "left_open" and "without confirming" in record["message"]
    assert not state["sent"]


def test_a_later_turn_is_never_exited_even_when_first_seen(rig):
    op, record, state, step, turns = rig
    # The watch first looks after the user already answered a question: the confirmed
    # turn is the user's, not the scheduled brief.
    turns((BRIEF, "Which branch?"), ("main, please", DONE))
    step()
    step()
    assert record["state"] == "left_open" and "another turn" in record["message"]
    assert not state["sent"]


def test_left_open_when_the_agent_process_is_gone(rig):
    op, record, state, step, _ = rig
    step()
    state["procs"] = {"shell_pid": 100, "foreground_processes": [{"pid": 100}]}
    step()
    assert record["state"] == "left_open" and "exited" in record["message"]
    assert not state["sent"]


@pytest.mark.parametrize("change", ["working", "status", "draft", "queued", "scrolled"])
def test_work_or_input_in_progress_only_delays_the_exit(rig, change):
    op, record, state, step, turns = rig
    saved = copy.deepcopy(state)
    if change == "working":
        turns((BRIEF, None))
    elif change == "status":
        state["info"]["agent_status"] = "working"
    elif change == "draft":
        state["screen"] = state["screen"].replace(EMPTY[state["agent"]], "› a draft reply")
    elif change == "queued":
        state["screen"] += "\nQueued follow-up inputs"
    else:
        state["info"]["scroll"] = {"offset_from_bottom": 3}
    step()
    step()
    assert record["state"] == "watching" and not state["sent"]
    if change == "working":
        turns((BRIEF, DONE))
    state.update(info=saved["info"], screen=saved["screen"])
    step()
    step()
    assert state["sent"] == ["w1:p1"]


def test_a_pane_too_short_for_the_composer_is_named_until_it_grows(rig):
    op, record, state, step, _ = rig
    saved = copy.deepcopy(state)
    # Only the agent's footer fits, so neither the composer nor the receipt is visible.
    state["info"]["scroll"] = {"offset_from_bottom": 0, "viewport_rows": 8}
    state["screen"] = RULE + "\n  bypass permissions on (shift+tab to cycle)"
    step()
    step()
    assert record["state"] == "watching" and not state["sent"]
    assert record["confirmed_at"] == 1000 + we.SETTLE_SECONDS
    assert record["blocked"] == (
        "Empty composer is not yet visible; no exit key sent; the pane shows only 8 rows, "
        "too few for the composer and final response"
    )
    assert record["message"] == f"Finished, but not exiting yet: {record['blocked']}"
    state.update(info=saved["info"], screen=saved["screen"])
    step()
    assert "blocked" not in record
    step()
    assert state["sent"] == ["w1:p1"]


@pytest.mark.parametrize("status", ["failed", "uncertain"])
def test_a_launch_that_did_not_finish_is_not_watched(rig, status):
    op, record, state, step, _ = rig
    op["status"] = status
    step()
    assert record["state"] == "left_open" and "launch did not finish" in record["message"]


def test_a_running_launch_is_waited_for_but_not_forever(rig):
    op, record, state, step, _ = rig
    op["status"] = "running"
    step()
    assert record["state"] == "watching" and "launch" in record["message"]
    step(advance=we.WATCH_LIMIT)
    assert record["state"] == "left_open" and "week" in record["message"]


def test_missing_transcript_is_waited_for_from_launch_completion(rig, monkeypatch):
    op, record, state, step, _ = rig

    def missing(target, since):
        assert since == op["created_at"]
        raise we.Wait("Waiting for the agent's session transcript")

    monkeypatch.setattr(we, "locate", missing)
    op["updated_at"] = record["since"] + 3600  # A long clone finished an hour later.
    step(advance=3600)
    assert record["state"] == "watching"
    step(advance=we.LOCATE_LIMIT + 1)
    assert record["state"] == "left_open" and "transcript" in record["message"]


@pytest.mark.parametrize(
    "change",
    [
        {"agents": []},
        {"procs": {"shell_pid": 200, "foreground_processes": []}},
        {"procs": {"shell_pid": 200, "foreground_processes": [{"pid": 200, "argv": ["x"]}]}},
    ],
)
def test_an_unidentified_agent_is_left_open(rig, change):
    op, record, state, step, _ = rig
    procs = change.get("procs")
    if procs and procs["foreground_processes"]:
        procs["foreground_processes"][0].update(argv0=state["agent"], cwd=op["path"])
    state.update(change)
    step()
    assert record["state"] == "left_open" and not state["sent"]


def test_unconfirmed_exit_is_reported_and_never_retried(rig):
    op, record, state, step, _ = rig
    state["exit_error"] = "Exit unconfirmed; no repeat key sent"
    step()
    step()
    assert record["state"] == "unconfirmed" and "check its pane" in record["message"]
    step()
    assert state["sent"] == ["w1:p1"]


def test_unexpected_error_leaves_the_agent_open(rig, monkeypatch):
    op, record, state, step, _ = rig

    def boom(*args):
        raise OSError("herdr went away")

    monkeypatch.setattr(handoff, "inspect", boom)
    step()
    assert record["state"] == "left_open" and "herdr went away" in record["message"]


def test_first_turn_skips_context_and_tool_results(tmp_path):
    path = tmp_path / "t.jsonl"
    write_jsonl(
        path,
        {"type": "user", "isMeta": True, "uuid": "meta"},
        {"type": "user", "isSidechain": True, "uuid": "side"},
        {"type": "user", "uuid": "tool", "message": {"content": [{"type": "tool_result"}]}},
        {"type": "user", "uuid": "brief", "message": {"content": "Fix it"}},
        {"type": "user", "uuid": "later", "message": {"content": "More"}},
    )
    assert we.first_turn(path, "claude") == "brief"
    write_jsonl(path, {"type": "session_meta", "payload": {}})
    assert we.first_turn(path, "claude") is None and we.first_turn(path, "codex") is None


def age(path, seconds):
    then = time.time() - seconds
    os.utime(path, (then, then))


def test_locates_the_claude_transcript_of_the_worktree(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    cwd = tmp_path / "wt" / "repo.x_1"
    cwd.mkdir(parents=True)
    folder = tmp_path / "claude" / "projects" / "".join(c if c.isalnum() else "-" for c in str(cwd))
    since = time.time() - 60
    write_jsonl(folder / f"{SID}.jsonl", {"type": "user", "cwd": str(cwd), "sessionId": SID})
    write_jsonl(folder / "not-a-session.jsonl", {"type": "user", "cwd": str(cwd)})
    # An earlier worktree at the same path left its transcript behind.
    old = "0f2c6f0e-3b7a-4d34-9a8f-2b8d6c1e9a10"
    write_jsonl(folder / f"{old}.jsonl", {"type": "user", "cwd": str(cwd), "sessionId": old})
    age(folder / f"{old}.jsonl", 3600)
    target = {"agent": "claude", "cwd": str(cwd)}
    assert we.locate(target, since) == {
        "rollout": str((folder / f"{SID}.jsonl").resolve()),
        "session_id": SID,
    }
    other = "1f2c6f0e-3b7a-4d34-9a8f-2b8d6c1e9a10"
    write_jsonl(folder / f"{other}.jsonl", {"type": "user", "cwd": str(cwd), "sessionId": other})
    with pytest.raises(we.Leave, match="Several sessions"):
        we.locate(target, since)


def test_locates_the_codex_rollout_of_the_worktree(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    cwd = tmp_path / "wt"
    cwd.mkdir()
    since = time.time() - 60
    day = tmp_path / "codex" / "sessions" / time.strftime("%Y/%m/%d")
    meta = {"type": "session_meta", "payload": {"id": "s1", "cwd": str(cwd)}}
    write_jsonl(day / "rollout-a.jsonl", meta)
    write_jsonl(day / "rollout-old.jsonl", {**meta, "payload": {"id": "s0", "cwd": str(cwd)}})
    age(day / "rollout-old.jsonl", 3600)
    write_jsonl(day / "rollout-b.jsonl", {**meta, "payload": {"id": "s2", "cwd": str(tmp_path)}})
    (day / "rollout-c.jsonl").write_text("not json")
    found = we.locate({"agent": "codex", "cwd": str(cwd)}, since)
    assert found == {"rollout": str((day / "rollout-a.jsonl").resolve()), "session_id": "s1"}


def test_missing_transcript_waits(tmp_path, monkeypatch):
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    with pytest.raises(we.Wait):
        we.locate({"agent": "codex", "cwd": str(tmp_path)}, time.time())
