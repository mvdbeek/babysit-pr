"""Sandbox launch lines: exact macOS (Safehouse) snapshots and the Linux bwrap wrapper.

The snapshots pin every command line and environment prefix the Safehouse integration
produced before ``sandbox.py`` existed, so the refactor stays byte-identical on macOS.
No real agent ever starts; only the Linux-gated tests run bubblewrap, with ``sh``.
"""

import itertools
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

import agent_docker
import claude_accounts
import claude_runner
import pr_supervisor
import pytest
import sandbox
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
    assert sandbox.claude_launcher() == launcher
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


# --- backend selection and the neutral argv ----------------------------------------------


def test_backend_follows_the_platform_unless_named(monkeypatch):
    monkeypatch.delenv("BABYSIT_SANDBOX")
    monkeypatch.setattr(sandbox.sys, "platform", "darwin")
    assert sandbox.backend() == "safehouse"
    monkeypatch.setattr(sandbox.sys, "platform", "linux")
    assert sandbox.backend() == "bwrap"
    assert sandbox.backend("none") == "none"
    monkeypatch.setattr(sandbox.sys, "platform", "win32")
    with pytest.raises(ValueError, match="No agent sandbox for win32"):
        sandbox.backend()
    with pytest.raises(ValueError, match="Unknown sandbox backend"):
        sandbox.backend("podman")


def test_the_environment_can_pin_a_sandbox_but_never_remove_it(monkeypatch):
    monkeypatch.setattr(sandbox.sys, "platform", "linux")
    assert sandbox.backend() == "safehouse"  # the conftest pin
    monkeypatch.setenv("BABYSIT_SANDBOX", "none")
    with pytest.raises(ValueError, match="must be safehouse or bwrap"):
        sandbox.backend()
    with pytest.raises(ValueError):
        wt.agent_command("claude")


def test_wrap_gives_each_backend_the_same_grants():
    grants = sandbox.Grants(("/a", "/b", "/a"), ("X", "Y"), ("docker", "playwright"))
    argv = ["agent", "--no-permissions"]
    options = ["--add-dirs=/a:/b", "--env-pass=X,Y"]
    assert sandbox.wrap(argv, grants, "safehouse") == [
        str(Path.home() / ".local/bin/safehouse"),
        *options,
        "--enable=docker,playwright-chrome",
        "--",
        *argv,
    ]
    assert sandbox.wrap(argv, grants, "bwrap") == [
        str(SCRIPTS / "babysit-bwrap"),
        *options,
        "--enable=docker,playwright",
        "--",
        *argv,
    ]
    assert sandbox.wrap(argv, grants, "none") == argv
    assert sandbox.wrap(argv, sandbox.Grants(), "bwrap")[1:] == ["--", *argv]
    with pytest.raises(ValueError, match="contains ':'"):
        sandbox.wrap(argv, sandbox.Grants(add_dirs=("/a:b",)), "bwrap")
    with pytest.raises(ValueError, match="Unknown sandbox feature 'ssh'"):
        sandbox.wrap(argv, sandbox.Grants(features=("ssh",)), "safehouse")


def test_linux_launchers_run_through_babysit_bwrap(tmp_path):
    launcher = sandbox.claude_launcher("bwrap")
    assert launcher == [
        str(SCRIPTS / "babysit-bwrap"),
        "--env-pass=" + ",".join(sandbox.REPAIR_ENV),
        "--enable=playwright",
        "--",
        "claude",
        "--dangerously-skip-permissions",
    ]
    with pytest.raises(ValueError, match="only run inside a sandbox"):
        sandbox.claude_launcher("none")
    job = {"session_id": "sid", "cwd": str(tmp_path), "sandbox": "workspace-write"}
    job["codex_command"] = [str(SCRIPTS / "babysit-bwrap"), "--", "codex"]
    assert pr_supervisor.codex_sandbox(job) == "danger-full-access"
    assert sandbox.launcher_backend("/x/babysit-bwrap") == "bwrap"
    assert sandbox.launcher_backend("/x/claude") is None


def test_linux_typed_lines_invoke_the_wrapper_explicitly():
    staged = []

    def stage(text):
        staged.append(text)
        return "/tmp/g 1"

    grants = sandbox.Grants(add_dirs=("/wt/a", "/src/a/.git"), features=("docker",))
    words = ["claude", "--add-dir", "/wt/a"]
    flag = wt.UNSANDBOXED_FLAGS["claude"]
    assert sandbox.typed_line(words, grants, stage, flag, "bwrap") == (
        f"{SCRIPTS / 'babysit-bwrap'} --enable=docker \"--add-dirs=$(cat '/tmp/g 1')\" -- "
        "claude --dangerously-skip-permissions --add-dir /wt/a"
    )
    assert staged == ["/wt/a:/src/a/.git"]
    assert sandbox.typed_line(["codex"], sandbox.Grants(), stage, "--yolo", "bwrap") == (
        f"{SCRIPTS / 'babysit-bwrap'} -- codex --yolo"
    )
    # babysit-bwrap passes CLAUDE_CONFIG_DIR itself.
    assert sandbox.env_pass_prefix(["CLAUDE_CONFIG_DIR"], "bwrap") is None
    assert sandbox.env_pass_prefix(["CLAUDE_CONFIG_DIR"], "safehouse") == PASS


def test_inspect_reports_unknown_rather_than_guessing(monkeypatch):
    monkeypatch.setattr(agent_docker, "has_access", lambda pid: pid == 1)
    assert sandbox.inspect(1, "safehouse") == {"docker": True}
    assert sandbox.inspect(2, "safehouse") == {"docker": False}

    def broken(pid):
        raise ValueError("Could not inspect the agent's Docker access")

    monkeypatch.setattr(agent_docker, "has_access", broken)
    assert sandbox.inspect(1, "safehouse") == {"docker": None}
    assert sandbox.inspect(1, "none") == {"docker": None}
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/fixture/run")
    assert sandbox.docker_socket() == Path("/fixture/run/docker.sock")
    assert sandbox.inspect(2**22 + 7, "bwrap") == {"docker": False}  # no such process


# --- babysit-bwrap -----------------------------------------------------------------------

WRAPPER = str(SCRIPTS / "babysit-bwrap")


@pytest.fixture
def checkout(tmp_path):
    """A linked worktree, so its git common directory lies outside it."""
    main = tmp_path / "main"
    git = [
        "git",
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.invalid",
        "-c",
        "commit.gpgsign=false",
    ]
    subprocess.run([*git, "init", "-q", str(main)], check=True)
    subprocess.run([*git, "-C", str(main), "commit", "-q", "--allow-empty", "-m", "x"], check=True)
    linked = tmp_path / "linked"
    subprocess.run([*git, "-C", str(main), "worktree", "add", "-q", str(linked)], check=True)
    home = tmp_path / "home"
    home.mkdir()
    return linked.resolve(), (main / ".git").resolve(), home


def environment(home, **extra):
    base = {"PATH": os.environ["PATH"], "HOME": str(home), "TERM": "dumb", "LANG": "C"}
    return {**base, "USER": "fixture", "GIT_CONFIG_NOSYSTEM": "1", **extra}


def printed(cwd, env, *options):
    result = subprocess.run(
        ["bash", WRAPPER, "--print-argv", *options],
        cwd=cwd,
        env=env,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr.decode()
    return result.stdout.decode().split("\0")[:-1]


def pairs(argv, option):
    return [tuple(argv[i + 1 : i + 3]) for i, word in enumerate(argv) if word == option]


def test_wrapper_denies_by_default_and_binds_only_the_grants(checkout):
    worktree, common, home = checkout
    env = environment(home, PASSED="1", SECRET="x", CLAUDE_CONFIG_DIR=str(home / "acct"))
    argv = printed(
        worktree,
        env,
        "--add-dirs=/extra/one:/extra/two",
        "--env-pass",
        "PASSED,UNSET_NAME",
        "--enable=playwright-chrome",
        "--",
        "agent",
        "--flag",
    )
    assert argv[:8] == [
        "bwrap",
        "--die-with-parent",
        "--new-session",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--clearenv",
        "--ro-bind-try",
    ]
    assert argv[-5:] == ["--chdir", str(worktree), "--", "agent", "--flag"]
    assert ("--tmpfs", str(home)) in [tuple(argv[i : i + 2]) for i in range(len(argv))]
    assert argv.index(str(home)) < argv.index(str(home / ".config/gh"))
    writable = pairs(argv, "--bind") + pairs(argv, "--bind-try")
    assert {source for source, target in writable if source == target} == {
        str(worktree),
        str(common),
        "/extra/one",
        "/extra/two",
        str(home / ".codex"),
        str(home / "acct"),
        *(str(home / cache) for cache in (".cache/uv", ".npm", ".cache/pip")),
        str(home / ".cache/ms-playwright"),
    }
    assert (str(home / ".config/gh"), str(home / ".config/gh")) in pairs(argv, "--ro-bind-try")
    assert ("/usr", "/usr") in pairs(argv, "--ro-bind-try")
    assert not any(target == "/etc" for _, target in pairs(argv, "--ro-bind-try"))
    passed = dict(pairs(argv, "--setenv"))
    assert passed == {
        "PATH": env["PATH"],
        "HOME": str(home),
        "TERM": "dumb",
        "LANG": "C",
        "USER": "fixture",
        "CLAUDE_CONFIG_DIR": str(home / "acct"),
        "PASSED": "1",
    }
    assert not any("docker" in word for word in argv)


def test_wrapper_without_an_account_binds_the_default_claude_config(checkout):
    worktree, _, home = checkout
    argv = printed(worktree, environment(home), "claude")
    writable = {source for source, _ in pairs(argv, "--bind-try")}
    assert {str(home / ".claude"), str(home / ".claude.json")} <= writable
    assert argv[-2:] == ["--", "claude"]


def test_wrapper_grants_only_a_rootless_docker_socket(checkout):
    worktree, _, home = checkout
    with tempfile.TemporaryDirectory(dir="/tmp") as runtime:
        # Unix socket paths are short; tmp_path can exceed the limit on macOS.
        path = Path(runtime) / "docker.sock"
        server = socket.socket(socket.AF_UNIX)
        try:
            server.bind(str(path))
            argv = printed(
                worktree, environment(home, XDG_RUNTIME_DIR=runtime), "--enable=docker", "x"
            )
        finally:
            server.close()
    assert (str(path), str(path)) in pairs(argv, "--bind")
    assert ("DOCKER_HOST", f"unix://{path}") in pairs(argv, "--setenv")
    for runtime, message in (("/var/run", "refusing the system"), (str(home), "no rootless")):
        result = subprocess.run(
            ["bash", WRAPPER, "--print-argv", "--enable=docker", "--", "x"],
            cwd=worktree,
            env=environment(home, XDG_RUNTIME_DIR=runtime),
            capture_output=True,
            text=True,
        )
        assert result.returncode == 2 and message in result.stderr


@pytest.mark.parametrize(
    ("options", "message"),
    [
        (["--enable=ssh", "x"], "unknown feature ssh"),
        (["--bogus", "x"], "unknown option --bogus"),
        (["--env-pass=BAD-NAME", "x"], "invalid variable name BAD-NAME"),
        (["--add-dirs"], "--add-dirs needs a value"),
        (["--"], "no command to run"),
    ],
)
def test_wrapper_rejects_what_it_cannot_honour(checkout, options, message):
    worktree, _, home = checkout
    result = subprocess.run(
        ["bash", WRAPPER, "--print-argv", *options],
        cwd=worktree,
        env=environment(home),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2 and message in result.stderr


def _bwrap_works():
    if not sys.platform.startswith("linux") or not shutil.which("bwrap"):
        return False
    probe = subprocess.run(["bwrap", "--ro-bind", "/", "/", "true"], capture_output=True)
    return probe.returncode == 0


linux_bwrap = pytest.mark.skipif(not _bwrap_works(), reason="needs Linux with working bwrap")


@linux_bwrap
def test_bwrap_contains_writes_and_hides_home(checkout, tmp_path):
    worktree, _, home = checkout
    (home / "secret").write_text("token")
    outside = tmp_path / "outside"
    outside.mkdir()
    script = (
        f"test ! -e {home}/secret && touch {worktree}/inside && "
        f"! touch /usr/babysit-denied 2>/dev/null && touch {outside}/escaped 2>/dev/null; "
        'test -z "${SECRET:-}"'
    )
    result = subprocess.run(
        ["bash", WRAPPER, "--", "sh", "-c", script],
        cwd=worktree,
        env=environment(home, SECRET="x"),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert (worktree / "inside").exists()
    assert not (outside / "escaped").exists()


@linux_bwrap
def test_bwrap_shows_the_docker_socket_only_when_enabled(checkout):
    worktree, _, home = checkout
    with tempfile.TemporaryDirectory(dir="/tmp") as runtime:
        path = Path(runtime) / "docker.sock"
        server = socket.socket(socket.AF_UNIX)
        try:
            server.bind(str(path))
            env = environment(home, XDG_RUNTIME_DIR=runtime)
            visible = [
                subprocess.run(
                    ["bash", WRAPPER, *enable, "--", "sh", "-c", f"test -S {path}"],
                    cwd=worktree,
                    env=env,
                    capture_output=True,
                ).returncode
                for enable in ([], ["--enable=docker"])
            ]
        finally:
            server.close()
    assert visible == [1, 0]
