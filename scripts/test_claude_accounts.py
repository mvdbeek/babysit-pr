"""Account selection uses isolated stores and fake launchers, never a live login."""

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import claude_accounts as accounts
import claude_runner
import pytest
import sentry_llm
import workspace_viewer
import wt


@pytest.fixture
def stores(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home with spaces"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    default = accounts.account_home("default", create=True)
    work = accounts.account_home("work", create=True)
    return default, work


def save_session(home, cwd, sid):
    folder = home / "projects" / "".join(c if c.isalnum() else "-" for c in str(cwd))
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{sid}.jsonl"
    path.write_text(
        json.dumps(
            {
                "type": "assistant",
                "sessionId": sid,
                "cwd": str(cwd),
                "message": {"model": "fixture-model", "content": []},
            }
        )
        + "\n"
    )
    return path


def test_accounts_discovery_validation_and_existing_custom_store(stores, tmp_path, monkeypatch):
    default, work = stores
    assert [a["id"] for a in accounts.catalog()] == ["default", "work"]
    custom = tmp_path / "custom"
    custom.mkdir()
    (default / "accounts" / "custom").symlink_to(custom)
    assert accounts.account_home("custom") == custom
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(work))
    assert accounts.homes() == [work, default, custom]
    assert accounts.validate("claude", "work") == work
    for name in ("../other", "a/b", "bad\nname", "", None):
        with pytest.raises(ValueError):
            accounts.account_home(name)
    with pytest.raises(ValueError, match="login missing"):
        accounts.account_home("missing")
    with pytest.raises(ValueError, match="only.*Claude"):
        accounts.validate("codex", "work")


def test_default_preserves_original_keychain_and_custom_environment(stores, monkeypatch):
    default, work = stores
    env = accounts.environment(default, base={"CLAUDE_CONFIG_DIR": str(work)})
    assert "CLAUDE_CONFIG_DIR" not in env
    raw = str(work) + "/"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", raw)
    assert accounts.config_environment(work) == raw
    assert accounts.environment(work, config_env=raw)["CLAUDE_CONFIG_DIR"] == raw
    base = {name: "wrong-account" for name in accounts.AUTH_ENV} | {"PATH": "kept"}
    env = accounts.environment(work, subscription=True, base=base)
    assert env == {"PATH": "kept", "CLAUDE_CONFIG_DIR": str(work)}


def test_cli_login_and_run_use_existing_safehouse_launcher(stores, monkeypatch):
    _, work = stores
    calls = []
    monkeypatch.setattr(os, "execvpe", lambda *args: calls.append(args))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key")
    monkeypatch.setattr(
        sys, "argv", ["claude-account", "login", "second", "--email", "x@example.invalid"]
    )
    accounts.main()
    binary, argv, env = calls.pop()
    assert binary == "/bin/zsh" and argv[1].endswith("/claude_safehouse.zsh")
    assert argv[2:] == ["auth", "login", "--claudeai", "--email", "x@example.invalid"]
    assert env["CLAUDE_CONFIG_DIR"] == str(accounts.account_home("second"))
    assert "ANTHROPIC_API_KEY" not in env
    monkeypatch.setattr(sys, "argv", ["claude-account", "run", "work", "--", "--resume", "sid"])
    accounts.main()
    assert calls[0][1][2:] == ["--resume", "sid"]
    assert calls[0][2]["CLAUDE_CONFIG_DIR"] == str(work)


def test_shell_launch_retains_function_quotes_path_and_restores_environment(stores):
    _, work = stores
    command = accounts.shell_command("claude --resume sid", work, subscription=True)
    script = (
        """
claude() { print -r -- "$CLAUDE_CONFIG_DIR|$SAFEHOUSE_ENV_PASS|${ANTHROPIC_API_KEY-unset}|$*"; }
"""
        + command
        + '\nprint -r -- "$CLAUDE_CONFIG_DIR|$ANTHROPIC_API_KEY"'
    )
    result = subprocess.run(
        ["/bin/zsh", "-f", "-c", script],
        env={
            **os.environ,
            "CLAUDE_CONFIG_DIR": "original",
            "ANTHROPIC_API_KEY": "original-key",
            "SAFEHOUSE_ENV_PASS": "EXISTING",
        },
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.splitlines() == [
        f"{work}|CLAUDE_CONFIG_DIR,EXISTING|unset|--resume sid",
        "original|original-key",
    ]


def test_safehouse_forwards_only_selected_directory(stores):
    _, work = stores
    home = Path(os.environ["HOME"])
    envfile = home / ".config/safehouse/safe.env"
    envfile.parent.mkdir(parents=True)
    envfile.write_text("")
    fake = home / ".local/bin/safehouse"
    fake.parent.mkdir(parents=True)
    fake.write_text(
        f"#!{sys.executable}\nimport json,os,sys\nprint(json.dumps([sys.argv[1:],os.environ['CLAUDE_CONFIG_DIR']]))\n"
    )
    fake.chmod(0o700)
    launcher = Path(claude_runner.__file__).with_name("claude_safehouse.zsh")
    result = subprocess.run(
        ["/bin/zsh", str(launcher), "--resume", "sid"],
        env=accounts.environment(work),
        capture_output=True,
        text=True,
        check=True,
    )
    args, directory = json.loads(result.stdout)
    assert directory == str(work)
    assert "--env-pass=CLAUDE_CONFIG_DIR" in args
    assert f"--add-dirs={work}" in args
    assert args[-2:] == ["--resume", "sid"]


def test_sessions_are_discovered_and_registration_pins_their_account(stores, tmp_path):
    default, work = stores
    sid, default_sid = str(uuid.uuid4()), str(uuid.uuid4())
    save_session(default, tmp_path, default_sid)
    path = save_session(work, tmp_path, sid)
    found = workspace_viewer.sessions(tmp_path)
    assert {s["id"] for s in found} == {sid, default_sid}
    selected = next(s for s in found if s["id"] == sid)
    assert selected["claude_account"] == "work"
    assert selected["claude_config_dir"] == str(work)
    info = claude_runner.session_info(sid, tmp_path)
    assert info["claude_config_dir"] == str(work) and info["claude_account"] == "work"
    assert info["claude_config_env"] == str(work)
    with pytest.raises(ValueError, match="different account"):
        claude_runner.session_info(sid, tmp_path, path, default)
    info = claude_runner.session_info(default_sid, tmp_path)
    assert info["claude_config_env"] is None


def test_worktree_selects_account_and_rejects_codex_before_launch(stores):
    _, work = stores
    options = wt.parse_args("wt", ["--claude-account", "work", "dev", "fix"])
    tool = wt.Tool(
        wt.SubprocessRunner({}), {}, stdout=sys.stdout, stderr=sys.stderr, isatty=lambda: False
    )
    command = tool.command_for(options)
    assert str(work) in command and "claude" in command and "unset ANTHROPIC_API_KEY" in command
    options.agent = "codex"
    with pytest.raises(wt.WtError, match="only.*Claude"):
        tool.command_for(options)


def test_quota_reads_selected_keychain_entry_without_default_fallback(stores, monkeypatch):
    import hashlib

    _, work = stores
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(work))
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 44, "", "missing")

    assert sentry_llm.claude_login(run)[0] is None
    service = "Claude Code-credentials-" + hashlib.sha256(str(work).encode()).hexdigest()[:8]
    assert calls == [["security", "find-generic-password", "-s", service, "-w"]]
