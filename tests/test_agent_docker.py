"""Docker restarts use isolated panes and mocked policies, never live agent processes."""

import copy
from types import SimpleNamespace

import agent_docker as docker
import agent_messages as messages
import pytest

SID = "8f2c6f0e-3b7a-4d34-9a8f-2b8d6c1e9a10"
REAL_ACCESS = docker.has_access


@pytest.fixture(params=["codex", "claude"])
def rig(request, tmp_path, monkeypatch):
    kind = request.param
    target = {"pane": "w1:p1", "session": SID, "status": "idle"}
    info = {
        "workspace_id": "w1",
        "terminal_id": "t1",
        "agent": kind,
        "agent_status": "idle",
        "agent_session": {"value": SID},
        "scroll": {"offset_from_bottom": 0},
    }
    flag = (
        "--dangerously-bypass-approvals-and-sandbox"
        if kind == "codex"
        else "--dangerously-skip-permissions"
    )
    proc = {
        "pid": 200,
        "argv0": kind,
        "argv": [kind, flag, "--model", "fixture", "old prompt"],
        "cwd": str(tmp_path),
    }
    safehouse = {
        "pid": 150,
        "argv0": "bash",
        "argv": [
            "/bin/bash",
            "/fixture/safehouse",
            "--enable=playwright-chrome",
            "--append-profile",
            "/fixture/extra.sb",
            *proc["argv"],
        ],
    }
    procs = {"shell_pid": 100, "foreground_processes": [safehouse, proc]}
    state = {"state_change_seq": 1}
    screen = (
        "› \x1b[2mAsk Codex to do anything\x1b[0m"
        if kind == "codex"
        else "─" * 40 + "\n❯\n" + "─" * 40
    )
    calls = []
    rig = SimpleNamespace(
        target=target,
        info=info,
        procs=procs,
        proc=proc,
        state=state,
        screen=screen,
        calls=calls,
        stuck=False,
        kind=kind,
        access=False,
    )
    monkeypatch.setattr(docker, "_exiting", set())
    monkeypatch.setattr(
        docker.handoff,
        "inspect",
        lambda pane: (copy.deepcopy(info), copy.deepcopy(procs), copy.deepcopy(state)),
    )
    monkeypatch.setattr(docker.handoff, "read_screen", lambda pane: rig.screen)
    monkeypatch.setattr(docker, "has_access", lambda pid: rig.access)
    monkeypatch.setattr(docker, "EXIT_WAIT", 0)

    def send(*args):
        calls.append(args)
        if args[:3] == ("herdr", "pane", "run"):
            procs["foreground_processes"] = [safehouse, proc]
            rig.access = "--enable=docker" in args[-1]
        if args[:2] == ("agent", "send-keys") and not rig.stuck:
            # herdr joins every argument after the pane ID as literal text.
            # Claude only exits for /exit, not a normal prompt such as -- /exit.
            text = "".join(" ".join(c[3:]) for c in calls if c[:2] == ("pane", "send-text"))
            if kind == "codex" or text == "/exit":
                procs["foreground_processes"] = [{"pid": 100}]

    def result(*args):
        return (
            {"pane": copy.deepcopy(info)}
            if args[:2] == ("pane", "get")
            else {"process_info": copy.deepcopy(procs)}
        )

    def gone(pid, signal):
        assert pid == 200 and signal == 0
        raise ProcessLookupError

    monkeypatch.setattr(docker.handoff, "herdr", send)
    monkeypatch.setattr(docker.handoff, "result", result)
    monkeypatch.setattr(docker.os, "kill", gone)
    monkeypatch.setattr(messages, "watch_owner", lambda *a: None)
    monkeypatch.setattr(messages.workspace_viewer, "workspace_checkout", lambda wid: tmp_path)
    monkeypatch.setattr(
        messages.workspace_viewer, "sessions", lambda root: [{"id": SID, "agent": kind}]
    )
    monkeypatch.setattr(
        messages,
        "agents",
        lambda wid, interactions=False: [{"pane": "w1:p1", "session": SID, "agent": kind}],
    )
    monkeypatch.setattr(messages, "run", send)
    return rig


def test_restart_preserves_session_pane_cwd_options_and_enables_docker(rig, tmp_path):
    value = messages.change_docker(rig.target, True, tmp_path)
    assert value == {"docker": True, "pane": "w1:p1", "warning": None}
    if rig.kind == "codex":
        assert rig.calls[0] == ("agent", "send-keys", "w1:p1", "ctrl+d")
    else:
        assert rig.calls[:2] == [
            ("pane", "send-text", "w1:p1", "/exit"),
            ("agent", "send-keys", "w1:p1", "enter"),
        ]
    command = rig.calls[-1][-1]
    assert rig.calls[-1][:4] == ("herdr", "pane", "run", "w1:p1")
    assert command.startswith(f"cd {tmp_path} && /fixture/safehouse --enable=playwright-chrome ")
    assert f"--append-profile /fixture/extra.sb --enable=docker -- {rig.kind} " in command
    assert command.count(rig.proc["argv"][1]) == 1
    assert rig.proc["argv"][1] in command and "--model fixture" in command
    assert SID in command and "old prompt" not in command and "$(cat" not in command
    assert not list((tmp_path / "message-prompts").iterdir())


def test_matching_access_does_not_restart_even_while_working(rig, tmp_path, monkeypatch):
    rig.info["agent_status"] = "working"
    monkeypatch.setattr(docker, "has_access", lambda pid: True)
    assert messages.change_docker(rig.target, True, tmp_path) == {
        "pane": "w1:p1",
        "docker": True,
        "warning": None,
    }
    assert not rig.calls


@pytest.mark.parametrize(
    "problem", ["working", "blocked", "draft", "session", "root", "watch", "unrecorded", "option"]
)
def test_unsafe_restart_keeps_the_agent_and_message_untouched(rig, tmp_path, monkeypatch, problem):
    if problem in {"working", "blocked"}:
        rig.info["agent_status"] = problem
    elif problem == "draft":
        rig.screen = "› my unfinished message"
    elif problem == "session":
        rig.info["agent_session"]["value"] = "different"
    elif problem == "root":
        rig.procs["shell_pid"] = 200
    elif problem == "watch":
        monkeypatch.setattr(messages, "watch_owner", lambda *a: "a running watch")
    elif problem == "unrecorded":
        monkeypatch.setattr(messages.workspace_viewer, "sessions", lambda root: [])
    else:
        rig.proc["argv"].insert(1, "--unknown")
    with pytest.raises(ValueError):
        messages.change_docker(rig.target, True, tmp_path)
    assert not rig.calls


def test_failed_exit_is_never_repeated_and_never_resumes(rig, tmp_path):
    rig.stuck = True
    with pytest.raises(ValueError, match="Exit unconfirmed"):
        messages.change_docker(rig.target, True, tmp_path)
    calls = list(rig.calls)
    with pytest.raises(ValueError, match="already attempted"):
        messages.change_docker(rig.target, True, tmp_path)
    assert rig.calls == calls
    assert not any(c[:3] == ("herdr", "pane", "run") for c in rig.calls)


@pytest.mark.parametrize("change", ["pid", "session", "sequence", "terminal", "shell"])
def test_identity_is_rechecked_before_exit(rig, change):
    record = docker.prepare(rig.target, rig.info, rig.procs, rig.state, rig.proc)
    if change == "pid":
        rig.proc["pid"] = 201
    elif change == "session":
        rig.info["agent_session"]["value"] = "other"
    elif change == "sequence":
        rig.state["state_change_seq"] += 1
    elif change == "terminal":
        rig.info["terminal_id"] = "other"
    else:
        rig.procs["shell_pid"] = 101
    with pytest.raises((ValueError, RuntimeError)):
        docker.quit_agent(record)
    assert not rig.calls


@pytest.mark.parametrize("result, expected", [(0, True), (1, False), (-1, None)])
def test_policy_probe_distinguishes_allowed_denied_and_unknown(monkeypatch, result, expected):
    calls = []

    def check(*args):
        calls.append(args)
        return result

    monkeypatch.setattr(docker.ctypes, "CDLL", lambda path: SimpleNamespace(sandbox_check=check))
    monkeypatch.setattr(
        docker.ctypes,
        "c_int",
        type("Integer", (), {"in_dll": staticmethod(lambda *a: SimpleNamespace(value=16))}),
    )
    if expected is None:
        with pytest.raises(ValueError, match="Could not inspect"):
            REAL_ACCESS(200)
    else:
        assert REAL_ACCESS(200) is expected
    assert len(calls) == 4 and all(c[0] == 200 and c[2] == 17 for c in calls)


def test_unavailable_policy_api_fails_without_assuming_access(monkeypatch):
    def missing(path):
        raise OSError("not macOS")

    monkeypatch.setattr(docker.ctypes, "CDLL", missing)
    with pytest.raises(ValueError, match="Could not inspect"):
        REAL_ACCESS(200)


def test_resume_options_never_replay_prompts_and_preserve_permissions():
    assert docker.resume_options(
        ["codex", "-c", 'approval_policy="never"', "resume", SID, "--", "old"], "codex", SID
    ) == ["-c", 'approval_policy="never"']
    # The resume adds it again.
    assert docker.resume_options(["codex", "--no-daemon", "resume", SID], "codex", SID) == []
    # Every launch adds it again; other config values stay.
    skip = "check_for_update_on_startup=false"
    assert docker.resume_options(
        ["codex", "-c", skip, f"--config={skip}", "--config", "a=1", "resume", SID], "codex", SID
    ) == ["--config", "a=1"]
    assert docker.resume_options(
        ["codex", "-c", "check_for_update_on_startup=true"], "codex", SID
    ) == ["-c", "check_for_update_on_startup=true"]
    assert docker.resume_options(
        ["claude", "-c", "--permission-mode=default", "old"], "claude", SID
    ) == ["--permission-mode=default"]
    with pytest.raises(ValueError, match="differs"):
        docker.resume_options(["codex", "resume", "other"], "codex", SID)
    with pytest.raises(ValueError, match="Incomplete"):
        docker.resume_options(["codex", "--model"], "codex", SID)


def test_an_unverified_replacement_is_uncertain_and_cannot_be_relaunched(
    rig, tmp_path, monkeypatch
):
    monkeypatch.setattr(
        messages, "agents", lambda wid: [{"pane": "w1:p1", "session": "wrong", "agent": rig.kind}]
    )
    monkeypatch.setattr(messages, "RESUME_WAIT", 0.01)
    value = messages.change_docker(rig.target, True, tmp_path)
    assert value["docker"] is None and "not confirmed" in value["warning"]
    assert SID in messages._recent
    assert not list((tmp_path / "message-prompts").iterdir())


def test_original_safehouse_profile_options_survive_the_restart(rig):
    original = docker.launcher(rig.procs, rig.proc)
    assert original[:4] == [
        "/fixture/safehouse",
        "--enable=playwright-chrome",
        "--append-profile",
        "/fixture/extra.sb",
    ]
    assert original[-3:] == ["--enable=docker", "--", rig.kind]
    rig.procs["foreground_processes"] = [rig.proc]
    with pytest.raises(ValueError, match="preserve.*Safehouse"):
        docker.launcher(rig.procs, rig.proc)


@pytest.mark.parametrize(
    "flags",
    [
        ["--enable=docker"],
        ["--enable", "docker,ssh", "--enable=docker,playwright-chrome"],
    ],
)
def test_disabling_removes_docker_and_preserves_other_grants(rig, tmp_path, flags):
    rig.access = True
    rig.procs["foreground_processes"][0]["argv"][2:2] = flags
    result = messages.change_docker(rig.target, False, tmp_path)
    assert result == {"pane": "w1:p1", "docker": False, "warning": None}
    command = rig.calls[-1][-1]
    assert "docker" not in command.split(" && ", 1)[1]
    assert "--enable=playwright-chrome" in command
    assert "--append-profile /fixture/extra.sb" in command
    assert "$(cat" not in command
    if "ssh" in ",".join(flags):
        assert "--enable=ssh" in command


def test_uncontrolled_docker_grant_is_not_claimed_to_be_disabled(rig, tmp_path):
    rig.access = True
    with pytest.raises(ValueError, match="another policy"):
        messages.change_docker(rig.target, False, tmp_path)
    assert not rig.calls


def test_send_does_not_inspect_or_change_docker(rig, tmp_path, monkeypatch):
    rig.target["status"] = "working"
    monkeypatch.setattr(messages, "agents", lambda wid: [rig.target])
    monkeypatch.setattr(messages, "herdr", lambda *args: rig.calls.append(args))
    result = messages.send({"workspace": "w1", "pane": "w1:p1", "text": "go"}, tmp_path)
    assert result["sent"]
    assert len(rig.calls) == 1 and rig.calls[0][:2] == ("agent", "prompt")
    assert not rig.access


@pytest.mark.parametrize("enabled", [False, True, None])
def test_status_reports_current_access_or_unknown(rig, monkeypatch, enabled):
    def access(pid):
        if enabled is None:
            raise ValueError("Policy unavailable")
        return enabled

    monkeypatch.setattr(docker, "has_access", access)
    result = messages.docker_status("w1")[0]
    assert result["docker"] is enabled
    if enabled is None:
        assert result["docker_error"] == "Policy unavailable"
    assert not rig.calls


@pytest.mark.parametrize(
    "field,value", [("enabled", "false"), ("session", "other"), ("pane", "other")]
)
def test_toggle_rejects_invalid_or_stale_requests(rig, tmp_path, field, value):
    request = {"workspace": "w1", "pane": "w1:p1", "session": SID, "enabled": True}
    request[field] = value
    with pytest.raises(ValueError):
        messages.set_docker(request, tmp_path)
    assert not rig.calls


def test_toggle_checks_access_after_restart(rig, tmp_path, monkeypatch):
    monkeypatch.setattr(docker, "has_access", lambda pid: False)
    result = messages.set_docker(
        {"workspace": "w1", "pane": "w1:p1", "session": SID, "enabled": True}, tmp_path
    )
    assert result["docker"] is False
    assert "did not match" in result["warning"]
