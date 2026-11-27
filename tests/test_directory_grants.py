"""Directory grants validate the worker's sandbox and keep the selected launch's options."""

import shutil
from pathlib import Path
from types import SimpleNamespace

import agent_messages
import pytest
import remote_probe


def test_worker_checks_directory_against_the_launcher(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    scripts = tmp_path / "babysit" / "scripts"
    scripts.mkdir(parents=True)
    launcher = scripts / "babysit-bwrap"
    shutil.copy(Path(remote_probe.__file__).with_name("babysit-bwrap"), launcher)
    launcher.chmod(0o755)
    data = tmp_path / "data"
    data.mkdir()
    root = str(scripts.parent)
    assert remote_probe._host_directory_grant(str(data), root) == {"path": str(data)}
    with pytest.raises(ValueError, match="does not exist"):
        remote_probe._host_directory_grant(str(data / "missing"), root)
    with pytest.raises(ValueError, match="refused"):
        remote_probe._host_directory_grant(str(tmp_path), root)
    with pytest.raises(ValueError, match="unsupported"):
        remote_probe._host_directory_grant(str(data) + ":bad", root)


def test_recover_launch_directory_grants_and_options():
    procs = {
        "foreground_processes": [
            {
                "argv": [
                    "bwrap",
                    "--bind",
                    "/tmp/run/home",
                    "/home/u",
                    "--bind",
                    "/home/u/.cache/babysit-sandbox/0123456789abcdef",
                    "/home/u/.cache/babysit-sandbox/0123456789abcdef",
                    "--dir",
                    "/home/u/.cache/babysit-sandbox/0123456789abcdef/artifacts",
                    "--bind-fd",
                    "10",
                    "/home/u/.codex",
                    "--bind-fd",
                    "11",
                    "/work",
                    "--bind-fd",
                    "12",
                    "/data",
                    "--tmpfs",
                    "/home/u/.codex/shell_snapshots",
                    "--setenv",
                    "DOCKER_HOST",
                    "unix:/run/docker.sock",
                ]
            },
            {"argv": ["codex", "--model", "example", "resume", "session"]},
        ]
    }
    config, directories, options, features = agent_messages.worker_sandbox_options(
        procs, "codex", "session"
    )
    assert (config, directories, options, features) == (
        "/home/u/.codex",
        ["/work", "/data"],
        ["--model", "example"],
        ("docker",),
    )
    procs["foreground_processes"][0]["argv"].remove("--tmpfs")
    with pytest.raises(ValueError, match="Cannot recover"):
        agent_messages.worker_sandbox_options(procs, "codex", "session")


def test_directory_grant_checks_before_restart(monkeypatch):
    machine = SimpleNamespace(os="linux", babysit_root="~/babysit")
    calls = []
    monkeypatch.setattr(
        agent_messages, "agents", lambda *a, **k: [{"pane": "p", "session": "s", "agent": "codex"}]
    )
    monkeypatch.setattr(
        agent_messages.remote_probe,
        "call",
        lambda *a, **k: calls.append(("check", k["path"])) or {"path": k["path"]},
    )
    monkeypatch.setattr(
        agent_messages.herdr_handoff,
        "inspect",
        lambda *a: (
            {"agent_session": {"value": "s"}},
            {"foreground_processes": [{"pid": 7, "argv": ["codex"], "cwd": "/work"}]},
            {},
        ),
    )
    monkeypatch.setattr(
        agent_messages,
        "worker_sandbox_options",
        lambda *a: ("/config", ["/existing"], ["--model", "m"], ("gpg",)),
    )
    monkeypatch.setattr(agent_messages, "workspace_root", lambda *a: "/work")
    monkeypatch.setattr(agent_messages, "recorded", lambda *a: [{"id": "s", "agent": "codex"}])
    monkeypatch.setattr(
        agent_messages, "remote_resume", lambda *a: {"codex_home": "/config", "cwd": "/work"}
    )
    monkeypatch.setattr(
        agent_messages, "remote_command", lambda *a: calls.append(("command", a[-1])) or "command"
    )
    monkeypatch.setattr(
        agent_messages, "exit_agent", lambda *a, **k: calls.append(("quit",)) or {"quit": True}
    )
    monkeypatch.setattr(
        agent_messages,
        "resume",
        lambda *a, **k: (
            calls.append(("resume", k["agent_options"])) or {"pane": "p", "warning": None}
        ),
    )
    result = agent_messages.grant_directory(
        {"workspace": "w", "pane": "p", "session": "s", "directory": "/data"},
        "/state",
        machine,
    )
    assert result["changed"]
    assert [call[0] for call in calls] == ["check", "check", "command", "quit", "resume"]
    assert calls[-1][1]["directories"] == ["/existing", "/data"]
    assert calls[-1][1]["features"] == ("gpg",)
