import json

import agent_settings
import workspace_viewer

SID = "019a0000-0000-7000-8000-000000000001"


def jsonl(*items):
    return "".join(json.dumps(item) + "\n" for item in items)


def turn(model, effort):
    return {"type": "turn_context", "payload": {"model": model, "effort": effort}}


def test_the_command_line_names_codex_and_claude_settings():
    assert agent_settings.from_argv(
        ["codex", "-m", "gpt-5.5", "-c", 'model_reasoning_effort="xhigh"'], "codex"
    ) == {"model": "gpt-5.5", "effort": "xhigh"}
    assert agent_settings.from_argv(["claude", "--model=opus", "--effort", "max"], "claude") == {
        "model": "opus",
        "effort": "max",
    }
    # A level Claude lacks, or a model no launch could pass, is not reported.
    assert agent_settings.from_argv(["claude", "--effort", "minimal", "-m", "x y"], "claude") == {
        "model": "",
        "effort": "",
    }


def test_a_codex_session_reports_its_newest_turn_where_the_command_line_is_silent(
    tmp_path, monkeypatch
):
    rollout = tmp_path / f"rollout-2026-11-20T10-00-00-{SID}.jsonl"
    rollout.write_text(jsonl(turn("gpt-5.4", "low"), turn("gpt-5.5", "high")))
    monkeypatch.setattr(
        workspace_viewer,
        "session_anywhere",
        lambda session: {"agent": "codex", "_file": rollout} if session == SID else None,
    )
    assert agent_settings.observed(["codex", "-m", "gpt-5.3"], "codex", SID) == {
        "model": "gpt-5.3",
        "effort": "high",
    }
    # Only the file's end is read: a turn whose context starts in it still counts.
    monkeypatch.setattr(agent_settings, "TAIL", 200)
    with rollout.open("a") as stream:
        stream.write(jsonl({"type": "event_msg", "payload": {"text": "x" * 500}}))
        stream.write(jsonl(turn("gpt-5.5", "medium")))
    assert agent_settings.observed(["codex"], "codex", SID) == {
        "model": "gpt-5.5",
        "effort": "medium",
    }
    assert agent_settings.observed(["codex"], "codex", "other") == {"model": "", "effort": ""}
