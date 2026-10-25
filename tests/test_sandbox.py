"""Sandbox launch lines: exact macOS (Safehouse) snapshots and the Linux bwrap wrapper.

The snapshots pin every command line and environment prefix the Safehouse integration
produced before ``sandbox.py`` existed, so the refactor stays byte-identical on macOS.
Nothing here starts a real agent or sandbox.
"""

import itertools
import sys
from pathlib import Path

import agent_docker
import claude_accounts
import claude_runner
import pr_supervisor
import pytest
import wt

SCRIPTS = Path(wt.__file__).resolve().parent
ACCOUNT = "/fixture/accounts/work dir"
PASS = "SAFEHOUSE_ENV_PASS=CLAUDE_CONFIG_DIR${SAFEHOUSE_ENV_PASS:+,$SAFEHOUSE_ENV_PASS}"
ADD = "SAFEHOUSE_ADD_DIRS=${SAFEHOUSE_ADD_DIRS:+$SAFEHOUSE_ADD_DIRS:}$(cat /tmp/g1)"
UNSET = " ".join(claude_accounts.AUTH_ENV)
CODEX = "codex -c check_for_update_on_startup=false"


@pytest.fixture
def default_home(monkeypatch, tmp_path):
    home = tmp_path / ".claude"
    monkeypatch.setattr(claude_accounts, "default_home", lambda: home)
    return home


# --- macOS snapshots ---------------------------------------------------------------------


def test_snapshot_headless_claude_launcher():
    launcher = ["/bin/zsh", str(SCRIPTS / "claude_safehouse.zsh")]
    assert claude_runner.safehouse_command() == launcher
    job = {
        "claude_command": launcher,
        "claude_permission_mode": "safehouse",
        "session_id": "sid",
        "model": "m",
    }
    assert claude_runner.command_for(job, {"type": "object"}) == [
        *launcher,
        "--print",
        "--resume",
        "sid",
        "--output-format",
        "stream-json",
        "--verbose",
        "--json-schema",
        '{"type": "object"}',
        "--model",
        "m",
    ]
    plain = {**job, "claude_command": ["claude"], "claude_permission_mode": "dontAsk"}
    assert claude_runner.command_for(plain, {})[-4:] == [
        "--permission-mode",
        "dontAsk",
        "--model",
        "m",
    ]
    with pytest.raises(ValueError, match="verified Safehouse launcher"):
        claude_runner.command_for({**job, "claude_command": ["claude"]}, {})


@pytest.mark.parametrize(
    ("launcher", "sandbox"),
    [
        (["/home/u/.local/bin/safehouse", "--enable=x", "codex"], "danger-full-access"),
        (["safehouse", "codex"], "danger-full-access"),
        (["/usr/bin/codex"], "read-only"),
        (["/opt/safehouse/codex"], "read-only"),
    ],
)
def test_snapshot_codex_repair_sandbox(tmp_path, launcher, sandbox):
    job = {"session_id": "sid", "cwd": str(tmp_path), "sandbox": "read-only"}
    job["codex_command"] = launcher
    assert pr_supervisor.codex_sandbox(job) == sandbox
    assert pr_supervisor.command_for(job, tmp_path) == [
        *launcher,
        "exec",
        "--sandbox",
        sandbox,
        "-c",
        'approval_policy="never"',
        "--output-schema",
        str(tmp_path / "schema.json"),
        "-o",
        str(tmp_path / "reply.json"),
        "resume",
        "sid",
        "-",
    ]


def _typed(agent, docker, also, account, unsandboxed):
    checkouts = [wt.AlsoCheckout("/src/a", "/wt/a", "/src/a/.git")] if also else []
    return wt.agent_command(
        agent,
        "go",
        stage=lambda text: "/tmp/p1" if text == "go" else "/tmp/g1",
        docker=docker,
        claude_config_dir=ACCOUNT if account else None,
        claude_subscription=account,
        unsandboxed=unsandboxed,
        also=checkouts,
    )


def _expected(agent, docker, also, account, unsandboxed):
    words = CODEX if agent == "codex" else agent
    if unsandboxed:
        flag = wt.UNSANDBOXED_FLAGS[agent]
        words = f"command {agent} {flag}" + words[len(agent) :]
    line = words + (" --add-dir /wt/a --" if also else "") + ' "$(cat /tmp/p1)"'
    if docker:
        line = f"SAFE_ENABLE=docker {line}"
    if also and not unsandboxed:
        line = f"{ADD} {line}"
    if account:
        line = f"(unset {UNSET}; CLAUDE_CONFIG_DIR='{ACCOUNT}' {PASS} {line})"
    return line


@pytest.mark.parametrize(
    ("agent", "docker", "also", "account", "unsandboxed"),
    [
        case
        for case in itertools.product(["claude", "codex"], *[[False, True]] * 4)
        if not (case[1] and case[4]) and not (case[3] and case[0] == "codex")
    ],
)
def test_snapshot_typed_launch_lines(default_home, agent, docker, also, account, unsandboxed):
    assert _typed(agent, docker, also, account, unsandboxed) == _expected(
        agent, docker, also, account, unsandboxed
    )


def test_snapshot_typed_launch_line_literals(default_home):
    # The table above derives its expectations; these spell out the riskiest lines.
    assert _typed("claude", True, True, True, False) == (
        f"(unset {UNSET}; CLAUDE_CONFIG_DIR='/fixture/accounts/work dir' "
        "SAFEHOUSE_ENV_PASS=CLAUDE_CONFIG_DIR${SAFEHOUSE_ENV_PASS:+,$SAFEHOUSE_ENV_PASS} "
        "SAFEHOUSE_ADD_DIRS=${SAFEHOUSE_ADD_DIRS:+$SAFEHOUSE_ADD_DIRS:}$(cat /tmp/g1) "
        'SAFE_ENABLE=docker claude --add-dir /wt/a -- "$(cat /tmp/p1)")'
    )
    assert _typed("codex", False, True, False, True) == (
        "command codex --dangerously-bypass-approvals-and-sandbox "
        '-c check_for_update_on_startup=false --add-dir /wt/a -- "$(cat /tmp/p1)"'
    )
    with pytest.raises(ValueError, match="Docker access only applies inside Safehouse"):
        _typed("claude", True, False, False, True)
    with pytest.raises(ValueError, match="A --with checkout path contains ':'"):
        wt.agent_command("claude", also=[wt.AlsoCheckout("/a", "/wt/a:b", "/a/.git")])


def test_snapshot_account_shell_prefix(default_home):
    assert claude_accounts.shell_command("claude", ACCOUNT) == (
        f"CLAUDE_CONFIG_DIR='{ACCOUNT}' {PASS} claude"
    )
    assert (
        claude_accounts.shell_command("claude", default_home) == "(unset CLAUDE_CONFIG_DIR; claude)"
    )
    assert claude_accounts.shell_command("claude", ACCOUNT, config_env=None) == (
        "(unset CLAUDE_CONFIG_DIR; claude)"
    )
    assert claude_accounts.shell_command("claude", default_home, config_env="/raw") == (
        f"CLAUDE_CONFIG_DIR=/raw {PASS} claude"
    )


def test_snapshot_account_cli_exec(default_home, monkeypatch):
    calls = []
    monkeypatch.setattr("os.execvpe", lambda *args: calls.append(args))
    monkeypatch.setattr(sys, "argv", ["claude-account", "login", "x", "--y"])
    claude_accounts.main()
    binary, argv, _ = calls.pop()
    assert binary == "/bin/zsh"
    assert argv == [
        "/bin/zsh",
        str(SCRIPTS / "claude_safehouse.zsh"),
        "auth",
        "login",
        "--claudeai",
        "--y",
    ]


@pytest.mark.parametrize(
    ("enabled", "prefix", "expected"),
    [
        (
            True,
            ["/usr/local/bin/safehouse", "--enable", "docker,ssh"],
            ["/usr/local/bin/safehouse", "--enable=ssh", "--enable=docker", "--", "claude"],
        ),
        (
            False,
            ["/bin/bash", "/x/safehouse", "--enable=docker", "--"],
            ["/x/safehouse", "--", "claude"],
        ),
    ],
)
def test_snapshot_docker_restart_launcher(enabled, prefix, expected):
    proc = {"argv": ["claude", "--dangerously-skip-permissions"]}
    procs = {"foreground_processes": [{"argv": [*prefix, *proc["argv"]]}, proc]}
    assert agent_docker.launcher(procs, proc, enabled) == expected
    relative = {"foreground_processes": [{"argv": ["safehouse", *proc["argv"]]}, proc]}
    with pytest.raises(ValueError, match="preserve the agent's Safehouse launch"):
        agent_docker.launcher(relative, proc, enabled)
