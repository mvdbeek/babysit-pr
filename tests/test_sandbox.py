"""Sandbox launch lines: exact macOS (Safehouse) snapshots and the Linux bwrap wrapper.

The snapshots pin every command line and environment prefix the Safehouse integration
produced before ``sandbox.py`` existed, so the refactor stays byte-identical on macOS.
No real agent ever starts; only the Linux-gated tests run bubblewrap, with ``sh``.
"""

import contextlib
import hashlib
import itertools
import json
import os
import pty
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
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
    monkeypatch.setattr(sandbox, "DEFAULT", None)
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


def test_nothing_in_the_environment_selects_a_sandbox(monkeypatch):
    monkeypatch.setattr(sandbox, "DEFAULT", None)
    monkeypatch.setattr(sandbox.sys, "platform", "linux")
    monkeypatch.setenv("BABYSIT_SANDBOX", "none")
    assert sandbox.backend() == "bwrap"


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
    # Only babysit-bwrap takes gpg; Safehouse launches stay exactly as before.
    signing = grants._replace(features=("docker", "gpg"))
    assert sandbox.wrap(argv, signing, "bwrap")[3] == "--enable=docker,gpg"
    assert sandbox.wrap(argv, signing, "safehouse")[3] == "--enable=docker"
    assert sandbox.wrap(argv, sandbox.Grants(features=("gpg",)), "safehouse")[1:] == ["--", *argv]
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


def test_git_dirs_trust_only_a_common_directory_that_registers_the_worktree(checkout, tmp_path):
    worktree, common, home = checkout
    assert sandbox.git_dirs(str(worktree)) == (str(worktree), str(common))
    assert sandbox.git_dirs(str(home)) == (str(home),)  # no repository: nothing more
    (common.parent / "sub").mkdir()
    assert sandbox.git_dirs(str(common.parent / "sub")) == (str(common.parent / "sub"),)
    # Git may record a worktree relative to its entry's directory, never to ours.
    entry = next(common.glob("worktrees/*/gitdir"))
    entry.write_text(os.path.relpath(worktree / ".git", entry.parent) + "\n")
    assert sandbox.git_dirs(str(worktree)) == (str(worktree), str(common))
    # An agent redirecting its checkout's git files at a directory it wants next time.
    (home / "HEAD").write_text("ref: refs/heads/main\n")
    for name in ("objects", "refs"):
        (home / name).mkdir()
    (worktree / ".git").write_text(f"gitdir: {home}\n")
    with pytest.raises(ValueError, match="does not register the worktree"):
        sandbox.git_dirs(str(worktree))


def test_git_dirs_refuse_a_main_checkout_whose_git_directory_is_shared(checkout, tmp_path):
    _, common, _ = checkout
    # Its agent could swap .git for a link just before the linked worktree's sandbox binds it.
    with pytest.raises(ValueError, match="Refusing to sandbox the main checkout"):
        sandbox.git_dirs(str(common.parent))
    alone = tmp_path / "alone"
    subprocess.run(["git", "init", "-q", str(alone)], check=True)
    assert sandbox.git_dirs(str(alone)) == (str(alone.resolve()),)


def test_linux_typed_lines_invoke_the_wrapper_explicitly(checkout, monkeypatch):
    worktree, common, home = checkout
    staged = []

    def stage(text):
        staged.append(text)
        return "/tmp/g 1"

    grants = sandbox.Grants(add_dirs=("/wt/a", "/src/a/.git"), features=("docker",))
    words = ["claude", "--add-dir", "/wt/a"]
    flag = wt.UNSANDBOXED_FLAGS["claude"]
    line = sandbox.typed_line(words, grants, stage, flag, "bwrap", str(worktree), ACCOUNT)
    assert line == (
        f"{SCRIPTS / 'babysit-bwrap'} --enable=docker '--claude-config-dir={ACCOUNT}' "
        f"--add-dirs={worktree}:{common} \"--add-dirs=$(cat '/tmp/g 1')\" -- "
        "claude --dangerously-skip-permissions --add-dir /wt/a"
    )
    assert staged == ["/wt/a:/src/a/.git"]
    monkeypatch.setenv("CODEX_HOME", "/fixture/codex")
    assert sandbox.typed_line(["codex"], sandbox.Grants(), stage, "--yolo", "bwrap") == (
        f"{SCRIPTS / 'babysit-bwrap'} --codex-home=/fixture/codex -- codex --yolo"
    )
    # babysit-bwrap is given CLAUDE_CONFIG_DIR as an option.
    assert sandbox.env_pass_prefix(["CLAUDE_CONFIG_DIR"], "bwrap") is None
    assert sandbox.env_pass_prefix(["CLAUDE_CONFIG_DIR"], "safehouse") == PASS


def test_linux_codex_repairs_get_their_worktree_and_codex_home(checkout, monkeypatch, tmp_path):
    worktree, common, _ = checkout
    monkeypatch.setenv("CODEX_HOME", "/fixture/codex")
    wrapper = str(SCRIPTS / "babysit-bwrap")
    job = {"session_id": "sid", "cwd": str(worktree), "sandbox": "read-only"}
    job["codex_command"] = [wrapper, "--", "codex"]
    assert pr_supervisor.command_for(job, tmp_path)[:5] == [
        wrapper,
        "--codex-home=/fixture/codex",
        f"--add-dirs={worktree}:{common}",
        "--",
        "codex",
    ]


# --- babysit-bwrap -----------------------------------------------------------------------

WRAPPER = str(SCRIPTS / "babysit-bwrap")
FAKE_BWRAP = Path(__file__).resolve().parent / "fixtures" / "bwrap"


def _bash_version():
    """The version of the first bash on PATH, which runs the wrapper in these tests."""
    probe = 'echo "${BASH_VERSINFO[0]} ${BASH_VERSINFO[1]}"'
    found = subprocess.run(["bash", "-c", probe], capture_output=True, text=True, check=True)
    return tuple(int(part) for part in found.stdout.split())


# Binding by descriptor opens it with `exec {fd}<`, which needs bash 4.1; macOS ships 3.2.
fd_bash = pytest.mark.skipif(
    _bash_version() < (4, 1), reason="babysit-bwrap binds by descriptor only with bash 4.1+"
)


def environment(home, bwrap="path", **extra):
    """The wrapper's environment, with a fake bwrap that binds by ``path`` or ``fd``.

    ``bwrap=None`` leaves the host's bwrap, for the tests that run it.
    """
    path = os.environ["PATH"] if bwrap is None else f"{FAKE_BWRAP / bwrap}:{os.environ['PATH']}"
    base = {"PATH": path, "HOME": str(home), "TERM": "dumb", "LANG": "C"}
    return {**base, "USER": "fixture", "GIT_CONFIG_NOSYSTEM": "1", **extra}


def sandbox_cache(home, config_var="", config_dir="", granted=""):
    key = hashlib.sha256(f"{config_var}\0{config_dir}\0{granted}".encode()).hexdigest()[:16]
    return str(home / ".cache/babysit-sandbox" / key)


def run_wrapper(cwd, env, *options, stdin=subprocess.DEVNULL):
    return subprocess.run(
        ["bash", WRAPPER, "--print-argv", *options],
        cwd=cwd,
        env=env,
        stdin=stdin,
        capture_output=True,
    )


def printed(cwd, env, *options, stdin=subprocess.DEVNULL):
    result = run_wrapper(cwd, env, *options, stdin=stdin)
    assert result.returncode == 0, result.stderr.decode()
    return result.stdout.decode().split("\0")[:-1]


def refused(cwd, env, *options):
    result = run_wrapper(cwd, env, *options)
    assert result.returncode == 2 and not result.stdout, result
    return result.stderr.decode()


def pairs(argv, option):
    return [tuple(argv[i + 1 : i + 3]) for i, word in enumerate(argv) if word == option]


def test_wrapper_denies_by_default_and_binds_only_the_grants(checkout, tmp_path):
    worktree, common, home = checkout
    extra = tmp_path / "extra"
    extra.mkdir()
    account = home / "acct"
    account.mkdir()
    env = environment(home, PASSED="1", SECRET="x", UV_CACHE_DIR="/host/uv")
    argv = printed(
        worktree,
        env,
        f"--add-dirs={worktree}:{common}",
        f"--add-dirs={extra}:{tmp_path / 'missing'}",
        f"--claude-config-dir={account}",
        "--env-pass",
        "PASSED,UNSET_NAME,UV_CACHE_DIR",
        "--enable=playwright-chrome",
        "--",
        "agent",
        "--flag",
    )
    assert argv[:7] == [
        "bwrap",
        "--die-with-parent",
        "--new-session",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--clearenv",
    ]
    assert argv[-5:] == ["--chdir", str(worktree), "--", "agent", "--flag"]
    assert ("--tmpfs", str(home)) in [tuple(argv[i : i + 2]) for i in range(len(argv))]
    cache = sandbox_cache(home, "CLAUDE_CONFIG_DIR", account.resolve())
    writable = pairs(argv, "--bind")
    assert {source for source, target in writable if source == target} == {
        cache,
        str(account),
        str(worktree),
        str(common),
        str(extra),
    }
    assert not pairs(argv, "--bind-try")
    # Every read-only bind comes after every writable one, so none can be shadowed.
    last_writable = max(i for i, word in enumerate(argv) if word == "--bind")
    assert last_writable < min(i for i, word in enumerate(argv) if word.startswith("--ro-"))
    assert (str(home / ".config/gh"), str(home / ".config/gh")) in pairs(argv, "--ro-bind-try")
    assert ("/usr", "/usr") in pairs(argv, "--ro-bind-try")
    assert not any(target == "/etc" for _, target in pairs(argv, "--ro-bind-try"))
    assert dict(pairs(argv, "--setenv")) == {
        "PATH": env["PATH"],
        "HOME": str(home),
        "TERM": "dumb",
        "LANG": "C",
        "USER": "fixture",
        "PASSED": "1",
        "XDG_CACHE_HOME": cache,
        "UV_CACHE_DIR": f"{cache}/uv",
        "PIP_CACHE_DIR": f"{cache}/pip",
        "npm_config_cache": f"{cache}/npm",
        "PLAYWRIGHT_BROWSERS_PATH": f"{cache}/ms-playwright",
        "CLAUDE_CONFIG_DIR": str(account),
    }
    assert not any("docker" in word for word in argv)


def test_wrapper_ignores_the_working_directory_and_its_git_files(checkout):
    worktree, common, home = checkout
    # What an agent could leave for the next launch: git pointing outside its checkout.
    subprocess.run(["git", "-C", str(worktree), "config", "core.worktree", str(home)], check=True)
    argv = printed(worktree, environment(home), "--", "agent")
    assert {source for source, _ in pairs(argv, "--bind")} == {sandbox_cache(home)}
    # And from inside home itself, nothing of home becomes writable either.
    assert pairs(printed(home, environment(home), "agent"), "--bind") == pairs(argv, "--bind")


def test_wrapper_binds_one_agent_configuration_only(checkout):
    worktree, _, home = checkout
    (home / ".codex").mkdir()
    argv = printed(worktree, environment(home), f"--codex-home={home / '.codex'}", "codex")
    assert (str(home / ".codex"),) * 2 in pairs(argv, "--bind")
    assert ("CODEX_HOME", str(home / ".codex")) in pairs(argv, "--setenv")
    assert not any(word.startswith(str(home / ".claude")) for word in argv)
    message = refused(worktree, environment(home), "--codex-home=/a", "--claude-config-dir=/b", "x")
    assert "only one agent configuration directory" in message
    # A missing one would leave the agent configured inside the empty home's tmpfs.
    message = refused(worktree, environment(home), f"--claude-config-dir={home / 'gone'}", "x")
    assert "configuration directory" in message and "does not exist" in message


def test_wrapper_gives_each_agent_configuration_its_own_cache(checkout):
    worktree, _, home = checkout
    caches = set()
    for option in ("--claude-config-dir=a", "--claude-config-dir=b", "--codex-home=a"):
        (home / option[-1]).mkdir(exist_ok=True)
        config = option.split("=")[0] + f"={home / option[-1]}"
        argv = printed(worktree, environment(home), config, "agent")
        cache = pairs(argv, "--bind")[0][0]
        assert ("XDG_CACHE_HOME", cache) in pairs(argv, "--setenv")
        caches.add(cache)
    assert len(caches) == 3
    assert all(Path(cache).parent == home / ".cache/babysit-sandbox" for cache in caches)
    # The cache root stays protected as a whole: no grant reaches another agent's cache.
    Path(next(iter(caches))).mkdir(parents=True)
    message = refused(worktree, environment(home), f"--add-dirs={next(iter(caches))}", "x")
    assert "overlaps the protected" in message


@pytest.fixture
def docker_socket(tmp_path):
    """A rootless Docker socket in a fresh runtime directory."""
    # Unix socket paths are short; tmp_path can exceed the limit on macOS.
    with tempfile.TemporaryDirectory(dir="/tmp") as runtime:
        server = socket.socket(socket.AF_UNIX)
        try:
            server.bind(str(Path(runtime) / "docker.sock"))
            yield runtime
        finally:
            server.close()


def test_wrapper_keeps_a_separate_cache_for_each_grant(checkout, docker_socket):
    worktree, _, home = checkout
    account = home / "acct"
    account.mkdir()
    env = environment(home, XDG_RUNTIME_DIR=docker_socket)
    config = f"--claude-config-dir={account}"
    caches = {
        enable: dict(pairs(printed(worktree, env, config, *enable, "claude"), "--setenv"))[
            "XDG_CACHE_HOME"
        ]
        for enable in ((), ("--enable=playwright",), ("--enable=docker",))
    }
    # What an agent without Docker leaves in its cache never runs with the socket.
    assert (
        caches[()]
        == caches[("--enable=playwright",)]
        == sandbox_cache(home, "CLAUDE_CONFIG_DIR", account.resolve())
    )
    assert caches[("--enable=docker",)] == sandbox_cache(
        home, "CLAUDE_CONFIG_DIR", account.resolve(), "docker"
    )
    assert caches[()] != caches[("--enable=docker",)]


CLAUDE_CODE = (
    "settings.json CLAUDE.md agents commands hooks output-styles plugins rules session-env "
    "skills workflows"
).split()
CODEX_CODE = "config.toml hooks.json AGENTS.md AGENTS.override.md packages plugins prompts rules skills".split()


@pytest.mark.parametrize(
    ("option", "names", "present"),
    [
        ("--claude-config-dir", CLAUDE_CODE, ("settings.json", "skills")),
        ("--codex-home", CODEX_CODE, ("config.toml", "rules")),
    ],
)
def test_wrapper_binds_what_runs_code_in_the_agent_configuration_read_only(
    checkout, option, names, present
):
    worktree, _, home = checkout
    config = home / "agent"
    config.mkdir()
    for name in present:
        (config / name).mkdir() if "." not in name else (config / name).write_text("")
    (config / "projects").mkdir()
    argv = printed(worktree, environment(home), f"{option}={config}", "agent")
    assert (str(config),) * 2 in pairs(argv, "--bind")
    # Every launch, granted or not: one agent cannot leave code for the next to run.
    read_only = [target for source, target in pairs(argv, "--ro-bind") if source == target]
    assert sorted(read_only) == sorted(str(config / name) for name in names)
    assert str(config / "projects") not in argv
    last_writable = max(i for i, word in enumerate(argv) if word == "--bind")
    assert last_writable < argv.index("--ro-bind")
    # A dry run creates nothing; a launch creates empty placeholders first.
    assert sorted(path.name for path in config.iterdir()) == sorted([*present, "projects"])


def test_wrapper_creates_empty_placeholders_before_a_launch(checkout, tmp_path):
    worktree, _, home = checkout
    config = home / "agent"
    config.mkdir()
    (config / "skills").mkdir()
    (config / "skills" / "kept").write_text("x")
    # A bwrap that binds by path and exits at once, so the wrapper really launches.
    fake = tmp_path / "launching-bwrap"
    fake.mkdir()
    (fake / "bwrap").write_text("#!/bin/sh\nexit 0\n")
    (fake / "bwrap").chmod(0o755)
    env = environment(home)
    env["PATH"] = f"{fake}:{os.environ['PATH']}"
    result = subprocess.run(
        ["bash", WRAPPER, f"--claude-config-dir={config}", "--", "claude"],
        cwd=worktree,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert sorted(path.name for path in config.iterdir()) == sorted(CLAUDE_CODE)
    assert (config / "settings.json").read_text() == "{}\n"
    assert (config / "CLAUDE.md").read_text() == ""
    assert (config / "plugins").is_dir() and not any((config / "plugins").iterdir())
    assert (config / "skills" / "kept").read_text() == "x"


def test_wrapper_keeps_shell_snapshots_and_the_configuration_to_each_sandbox(checkout, tmp_path):
    worktree, _, home = checkout
    config = home / "agent"
    config.mkdir()
    env = environment(home)
    # Claude sources its snapshot before every command; a neighbour could rewrite it.
    for option, snapshots in (
        ("--claude-config-dir", "shell-snapshots"),
        ("--codex-home", "shell_snapshots"),
    ):
        argv = printed(worktree, env, f"{option}={config}", "agent")
        assert argv[argv.index("--tmpfs", argv.index(str(config))) + 1] == str(config / snapshots)
    # A grant holding the configuration could move it, read-only entries and all.
    outside = tmp_path / "accounts"
    (outside / "a").mkdir(parents=True)
    for grant in (outside, outside / "a", outside / "a" / "projects"):
        grant.mkdir(exist_ok=True)
        message = refused(
            worktree, env, f"--claude-config-dir={outside / 'a'}", f"--add-dirs={grant}", "x"
        )
        assert "overlaps the agent configuration" in message, grant


def test_wrapper_refuses_links_where_the_agent_configuration_runs_code(checkout, tmp_path):
    worktree, _, home = checkout
    config = home / "agent"
    config.mkdir()
    # Bound by itself, a link would be followed outside the sandbox, onto any file.
    (config / "settings.json").symlink_to(home / ".ssh" / "id_ed25519")
    message = refused(worktree, environment(home), f"--claude-config-dir={config}", "x")
    assert "settings.json" in message and "a link" in message
    (config / "settings.json").unlink()
    os.mkfifo(config / "hooks")
    message = refused(worktree, environment(home), f"--claude-config-dir={config}", "x")
    assert "hooks" in message and "neither a file nor a directory" in message


def test_wrapper_starts_a_granted_claude_without_mcp_servers(checkout, docker_socket):
    worktree, _, home = checkout
    config = home / "agent"
    config.mkdir()
    # Any agent of the account can add MCP servers to .claude.json, which stays writable.
    (config / ".claude.json").write_text(json.dumps({"mcpServers": {"x": {"command": "sh"}}}))
    env = environment(home, XDG_RUNTIME_DIR=docker_socket)
    claude = f"--claude-config-dir={config}"
    argv = printed(worktree, env, claude, "--enable=docker", "--", "/usr/bin/claude", "-p", "x")
    assert argv[-4:] == ["/usr/bin/claude", "--strict-mcp-config", "-p", "x"]
    # Without a grant there is nothing more for them to reach.
    assert printed(worktree, env, claude, "claude", "-p")[-2:] == ["claude", "-p"]
    message = refused(worktree, env, claude, "--enable=docker", "sh", "-c", "claude")
    assert "run claude itself" in message
    argv = printed(worktree, env, f"--codex-home={config}", "--enable=docker", "codex")
    assert argv[-1] == "codex"


@fd_bash
def test_wrapper_binds_checked_directories_by_descriptor_when_bwrap_can(checkout):
    worktree, common, home = checkout
    (home / ".codex").mkdir()
    options = (f"--codex-home={home / '.codex'}", f"--add-dirs={worktree}:{common}", "codex")
    by_path = printed(worktree, environment(home, bwrap="path"), *options)
    by_fd = printed(worktree, environment(home, bwrap="fd"), *options)
    granted = [str(home / ".codex"), str(worktree), str(common)]
    cache = sandbox_cache(home, "CODEX_HOME", (home / ".codex").resolve())
    assert pairs(by_path, "--bind") == [(cache, cache)] + [(path, path) for path in granted]
    assert not pairs(by_path, "--bind-fd")
    # The cache root is babysit's own; only checked grants go by descriptor.
    assert pairs(by_fd, "--bind") == [(cache, cache)]
    descriptors = pairs(by_fd, "--bind-fd")
    assert [target for _, target in descriptors] == granted
    assert all(fd.isdigit() and int(fd) > 2 for fd, _ in descriptors)
    assert len({fd for fd, _ in descriptors}) == len(granted)
    # Existing read-only entries of the configuration go by descriptor as well.
    (home / ".codex" / "rules").mkdir()
    by_fd = printed(worktree, environment(home, bwrap="fd"), *options)
    assert [target for _, target in pairs(by_fd, "--ro-bind-fd")] == [str(home / ".codex/rules")]
    assert str(home / ".codex/rules") not in [target for _, target in pairs(by_fd, "--ro-bind")]


def test_wrapper_refuses_paths_that_expose_home_or_protected_files(checkout, tmp_path):
    worktree, _, home = checkout
    for path in (".ssh/keys", ".config/babysit-pr", ".local/bin", ".cache/babysit-sandbox"):
        (home / path).mkdir(parents=True)
    (tmp_path / "dotfiles").mkdir()
    (tmp_path / "dotfiles" / "zshrc").write_text("")
    (home / ".zshrc").symlink_to(tmp_path / "dotfiles" / "zshrc")
    (tmp_path / "keys").symlink_to(home / ".ssh")
    env = environment(home)
    for path, message in (
        ("/", "would expose the home directory"),
        (home, "would expose the home directory"),
        (home.parent, "would expose the home directory"),
        (home / ".ssh", "overlaps the protected"),
        (home / ".ssh/keys", "overlaps the protected"),
        (home / ".config", "overlaps the protected"),  # holds babysit-pr's state
        (home / ".local/bin", "overlaps the protected"),
        (home / ".cache", "overlaps the protected"),  # the host caches
        (tmp_path / "keys", "overlaps the protected"),  # a link into ~/.ssh
        (tmp_path / "dotfiles", "overlaps the protected"),  # ~/.zshrc's real home
        ("relative/dir", "refusing relative path"),
    ):
        assert message in refused(worktree, env, f"--add-dirs={worktree}:{path}", "x"), path
        assert message in refused(worktree, env, f"--claude-config-dir={path}", "claude"), path
    assert "containing a newline" in refused(worktree, env, f"--add-dirs={worktree}\n/", "x")


@pytest.mark.parametrize(
    "path",
    [
        ".config/systemd",
        ".config/autostart",
        ".config/environment.d",
        ".config/fish",
        ".config/git",
        ".bashrc.d",
        ".pam_environment",
        ".gitconfig",
        ".profile",
        ".zprofile",
        ".zshenv",
        ".bash_profile",
        ".bash_login",
    ],
)
def test_wrapper_protects_session_and_git_startup_files(checkout, path):
    worktree, _, home = checkout
    (home / path).mkdir(parents=True)
    message = refused(worktree, environment(home), f"--add-dirs={home / path}", "x")
    assert "overlaps the protected" in message


@pytest.mark.parametrize(
    "path",
    [
        ".config/herdr",  # herdr.sock drives panes outside the sandbox
        ".local/share/systemd",  # user units
        ".local/share/dbus-1",  # session bus services
        ".bash_aliases",  # sourced by Ubuntu's .bashrc
        ".inputrc",
        ".tmux.conf",
        ".config/tmux",
        ".docker",  # credential helpers and CLI plugins
        ".local/share/uv",  # toolchains that also run outside
        ".local/share/claude",
        ".nvm",
        ".npm-global",
        ".claude",  # every Claude account
        ".claude/accounts/other",
        ".codex",
        ".local/share/babysit-pr",  # the deployed babysit-pr on a worker
    ],
)
def test_wrapper_protects_launchers_sockets_and_agent_homes(checkout, path):
    worktree, _, home = checkout
    (home / path).mkdir(parents=True)
    message = refused(worktree, environment(home), f"--add-dirs={worktree}:{home / path}", "x")
    assert "overlaps the protected" in message


def test_wrapper_binds_an_agent_home_only_as_that_agent_configuration(checkout):
    worktree, _, home = checkout
    for path in (".claude/accounts/work", ".codex"):
        (home / path).mkdir(parents=True)
    env = environment(home)
    claude, account, codex = home / ".claude", home / ".claude/accounts/work", home / ".codex"
    # The default account's directory holds the others, which stay out of reach.
    argv = printed(worktree, env, f"--claude-config-dir={claude}", "claude")
    assert (str(claude),) * 2 in pairs(argv, "--bind")
    assert argv[argv.index("--tmpfs", argv.index(str(claude))) + 1] == str(claude / "accounts")
    argv = printed(worktree, env, f"--claude-config-dir={account}", "claude")
    assert (str(account),) * 2 in pairs(argv, "--bind")
    assert str(claude / "accounts") not in argv
    # Hidden even before any named account exists, so the agent cannot plant a link there.
    shutil.rmtree(claude / "accounts")
    argv = printed(worktree, env, f"--claude-config-dir={claude}", "claude")
    assert ("--tmpfs", str(claude / "accounts")) in zip(argv, argv[1:], strict=False)
    (claude / "accounts").symlink_to(worktree)
    assert "a link" in refused(worktree, env, f"--claude-config-dir={claude}", "claude")
    (claude / "accounts").unlink()
    account.mkdir(parents=True)
    assert (str(codex),) * 2 in pairs(
        printed(worktree, env, f"--codex-home={codex}", "codex"), "--bind"
    )
    for option in (
        f"--codex-home={account}",
        f"--claude-config-dir={codex}",
        f"--claude-config-dir={claude / 'projects'}",
    ):
        (home / ".claude/projects").mkdir(exist_ok=True)
        assert "overlaps the protected" in refused(worktree, env, option, "x"), option


def test_wrapper_protects_its_own_checkout_and_git_directory(checkout, tmp_path):
    worktree, common, home = checkout
    # Deployed copies of the wrapper: in the main checkout and in a linked worktree.
    main, linked = common.parent, worktree
    for copy in (main, linked):
        (copy / "scripts").mkdir()
        shutil.copy(WRAPPER, copy / "scripts" / "babysit-bwrap")
    other = tmp_path / "other"
    other.mkdir()
    env = environment(home)
    for copy, paths in (
        (main, (main, main / "scripts", common)),
        (linked, (linked, linked / "scripts", common, main)),  # main holds the common dir
    ):
        launcher = str(copy / "scripts" / "babysit-bwrap")
        for path in paths:
            result = subprocess.run(
                ["bash", launcher, "--print-argv", f"--add-dirs={other}:{path}", "x"],
                cwd=other,
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
            )
            assert result.returncode == 2 and "overlaps the protected" in result.stderr, path
        result = subprocess.run(
            ["bash", launcher, "--print-argv", f"--add-dirs={other}", "x"],
            cwd=other,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
        )
        assert result.returncode == 0, result.stderr
    # Installed outside a scripts/ directory, only that directory is its own.
    (main / "bin").mkdir()
    shutil.copy(WRAPPER, main / "bin" / "babysit-bwrap")
    (main / "work").mkdir()
    launcher = str(main / "bin" / "babysit-bwrap")
    for path, allowed in ((main / "work", True), (main / "bin", False)):
        result = subprocess.run(
            ["bash", launcher, "--print-argv", f"--add-dirs={path}", "x"],
            cwd=other,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
        )
        assert (result.returncode == 0) is allowed, result.stderr


def test_wrapper_protects_missing_paths_under_linked_parents(checkout, tmp_path):
    worktree, _, home = checkout
    # ~/.config links elsewhere, and none of the protected entries in it exist yet.
    (tmp_path / "xdg").mkdir()
    (home / ".config").symlink_to(tmp_path / "xdg")
    # A dangling dotfile link: whoever creates its target writes ~/.bashrc.
    (tmp_path / "dots").mkdir()
    (home / ".bashrc").symlink_to(tmp_path / "dots" / "bashrc")
    for path in (tmp_path / "xdg", tmp_path / "dots"):
        message = refused(worktree, environment(home), f"--add-dirs={path}", "x")
        assert "overlaps the protected" in message, path


def tty_launch(worktree, home, *options):
    leader, follower = pty.openpty()
    try:
        return printed(worktree, environment(home), *options, "agent", stdin=follower)
    finally:
        os.close(leader)
        os.close(follower)


def test_wrapper_keeps_a_new_session_unless_the_terminal_cannot_be_injected(checkout, tmp_path):
    worktree, _, home = checkout
    sysctl = tmp_path / "legacy_tiocsti"
    for value, new_session in (("0\n", False), ("1\n", True)):
        sysctl.write_text(value)
        option = f"--tiocsti-sysctl={sysctl}"
        assert ("--new-session" in tty_launch(worktree, home, option)) is new_session
        # Without a terminal there is no job control to keep.
        assert "--new-session" in printed(worktree, environment(home), option, "agent")
    missing = f"--tiocsti-sysctl={tmp_path / 'missing'}"
    assert "--new-session" in tty_launch(worktree, home, missing)
    result = subprocess.run(
        ["bash", WRAPPER, f"--tiocsti-sysctl={sysctl}", "--", "x"],
        cwd=worktree,
        env=environment(home),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2 and "only applies with --print-argv" in result.stderr


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
        env = environment(home, XDG_RUNTIME_DIR=runtime)
        assert message in refused(worktree, env, "--enable=docker", "--", "x")


@pytest.fixture
def gnupg(checkout, tmp_path):
    """A ~/.gnupg with secrets, a gpg-agent socket and a gpgconf that names it."""
    _, _, home = checkout
    gnupghome = home / ".gnupg"
    (gnupghome / "private-keys-v1.d").mkdir(parents=True)
    (gnupghome / "private-keys-v1.d" / "GRIP.key").write_text("secret key")
    for name in ("pubring.kbx", "trustdb.gpg", "signing-subkey.pass", "gpg-agent.conf"):
        (gnupghome / name).write_text(name)
    fakes = tmp_path / "gpgconf-bin"
    fakes.mkdir()
    # Unix socket paths are short; tmp_path can exceed the limit on macOS.
    with tempfile.TemporaryDirectory(dir="/tmp") as runtime:
        sockets = Path(runtime) / "gnupg"
        sockets.mkdir()
        path = sockets / "S.gpg-agent"
        # Answers only for the default home, as babysit-bwrap must ask.
        (fakes / "gpgconf").write_text(
            '#!/bin/sh\n[ "$*" = "--list-dirs agent-socket" ] && [ -z "${GNUPGHOME+set}" ] || '
            f"exit 1\necho {path}\n"
        )
        (fakes / "gpgconf").chmod(0o755)
        server = socket.socket(socket.AF_UNIX)
        try:
            server.bind(str(path))
            env = environment(home, GNUPGHOME="/elsewhere")
            env["PATH"] = f"{fakes}:{env['PATH']}"
            yield env, path
        finally:
            server.close()


@fd_bash
def test_wrapper_gives_gpg_the_agent_socket_and_public_keyring_only(checkout, gnupg):
    worktree, _, home = checkout
    env, path = gnupg
    assert "S.gpg-agent" not in " ".join(printed(worktree, env, "agent"))
    argv = printed(worktree, env, "--env-pass=GNUPGHOME", "--enable=gpg", "agent")
    gnupghome = str(home / ".gnupg")
    # Never the agent's own socket: a proxy's, which passes on only what signing needs.
    sources = [source for source, target in pairs(argv, "--bind") if target == str(path)]
    assert len(sources) == 1 and sources[0] != str(path)
    assert sources[0].endswith("/S.gpg-agent") and "babysit-gpg." in sources[0]
    assert str(path) not in [source for source, _ in pairs(argv, "--bind")]
    # --perms MODE --file FD TARGET and --perms MODE --dir TARGET, in order.
    made = [tuple(argv[i + 1 : i + 5]) for i, word in enumerate(argv) if word == "--perms"]
    copies = {words[3]: words[0] for words in made if words[1] == "--file"}
    names = ("gpg.conf", "pubring.kbx", "trustdb.gpg")
    assert copies == {f"{gnupghome}/{name}": "0600" for name in names}
    # Private to the user, or gpg would look for the socket in the home instead.
    dirs = [(words[2], words[0]) for words in made if words[1] == "--dir"]
    assert dirs == [(str(p), "0700") for p in (gnupghome, path.parent.parent, path.parent)]
    # Even a passed-through GNUPGHOME cannot point gpg elsewhere.
    assert dict(pairs(argv, "--setenv"))["GNUPGHOME"] == gnupghome
    # Signing is a grant: its cache is apart from that of launches without it.
    assert dict(pairs(argv, "--setenv"))["XDG_CACHE_HOME"] == sandbox_cache(home, granted="gpg")
    joined = "\0".join(argv)
    for secret in ("private-keys-v1.d", "signing-subkey.pass", "gpg-agent.conf", "GRIP.key"):
        assert secret not in joined
    for other in ("S.gpg-agent.extra", "S.gpg-agent.browser", "S.gpg-agent.ssh"):
        assert other not in joined


@fd_bash
def test_wrapper_starts_a_gpg_agent_proxy_for_the_launch(checkout, gnupg, tmp_path):
    worktree, _, home = checkout
    env, path = gnupg
    # The fixture's socket becomes an agent that answers every command OK.
    path.unlink()
    agent = socket.socket(socket.AF_UNIX)
    agent.bind(str(path))
    agent.listen()
    received = []

    def answer():
        with contextlib.suppress(OSError):
            while True:
                connection, _ = agent.accept()
                with connection, connection.makefile("rb") as lines:
                    connection.sendall(b"OK hello\n")
                    for line in lines:
                        received.append(line)
                        connection.sendall(b"OK\n")

    threading.Thread(target=answer, daemon=True).start()
    # A bwrap that, instead of sandboxing, talks to whatever it binds at the agent socket.
    fake = tmp_path / "client-bwrap"
    fake.mkdir()
    (fake / "bwrap").write_text(
        f"#!{sys.executable}\n"
        "import socket, sys\n"
        "if sys.argv[1:] == ['--help']: sys.exit(0)\n"
        "argv = sys.argv\n"
        f"source = next(argv[i + 1] for i, w in enumerate(argv) if w == '--bind' and argv[i + 2] == {str(path)!r})\n"
        "client = socket.socket(socket.AF_UNIX)\n"
        "client.connect(source)\n"
        "lines = client.makefile('rb')\n"
        "lines.readline()\n"
        "for command in (b'DELETE_KEY --force GRIP\\n', b'HAVEKEY GRIP\\n'):\n"
        "    client.sendall(command)\n"
        "    sys.stdout.buffer.write(lines.readline())\n"
        "print(source)\n"
    )
    (fake / "bwrap").chmod(0o755)
    env["PATH"] = f"{fake}:{env['PATH']}"
    try:
        result = subprocess.run(
            ["bash", WRAPPER, "--enable=gpg", "--", "agent"],
            cwd=worktree,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        deleted, probed, source = result.stdout.splitlines()
        assert deleted.startswith("ERR 67109115 Forbidden") and probed == "OK"
        assert received == [b"HAVEKEY GRIP\n"]
        # The proxy leaves with the launch, and takes its socket with it.
        for _ in range(100):
            if not Path(source).parent.exists():
                break
            time.sleep(0.1)
        assert not Path(source).parent.exists()
    finally:
        agent.close()


def test_wrapper_refuses_gpg_without_an_agent_socket_or_keyring(checkout, gnupg):
    worktree, _, home = checkout
    env, path = gnupg
    path.unlink()
    assert f"no gpg-agent socket at {path}" in refused(worktree, env, "--enable=gpg", "x")
    path.write_text("")  # not a socket either
    assert "no gpg-agent socket" in refused(worktree, env, "--enable=gpg", "x")
    path.unlink()
    server = socket.socket(socket.AF_UNIX)
    try:
        server.bind(str(path))
        (home / ".gnupg/pubring.kbx").unlink()
        assert "no public keyring" in refused(worktree, env, "--enable=gpg", "x")
    finally:
        server.close()
    env["PATH"] = f"{FAKE_BWRAP / 'path'}:/usr/bin:/bin"
    if not shutil.which("gpgconf", path=env["PATH"]):
        assert "gpgconf is not installed" in refused(worktree, env, "--enable=gpg", "x")


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
    assert message in refused(worktree, environment(home), *options)


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
        ["bash", WRAPPER, f"--add-dirs={worktree}", "--", "sh", "-c", script],
        cwd=worktree,
        env=environment(home, bwrap=None, SECRET="x"),
        stdin=subprocess.DEVNULL,
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
            env = environment(home, bwrap=None, XDG_RUNTIME_DIR=runtime)
            visible = [
                subprocess.run(
                    ["bash", WRAPPER, f"--add-dirs={worktree}", *enable, "--"]
                    + ["sh", "-c", f"test -S {path}"],
                    cwd=worktree,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                ).returncode
                for enable in ([], ["--enable=docker"])
            ]
        finally:
            server.close()
    assert visible == [1, 0]


@linux_bwrap
def test_bwrap_shows_gpg_its_throwaway_home_and_the_agent_socket(checkout, gnupg):
    worktree, _, home = checkout
    env, path = gnupg
    env["PATH"] = env["PATH"].replace(f"{FAKE_BWRAP / 'path'}:", "")
    script = (
        'test "$GNUPGHOME" = "$HOME/.gnupg" && test -S ' + str(path) + " && "
        'test "$(ls "$GNUPGHOME" | tr "\\n" " ")" = "gpg.conf pubring.kbx trustdb.gpg " && '
        'grep -qx no-autostart "$GNUPGHOME/gpg.conf" && '
        'test "$(cat "$GNUPGHOME/pubring.kbx")" = pubring.kbx && '
        f"test ! -e {path.parent}/S.gpg-agent.extra"
    )
    result = subprocess.run(
        ["bash", WRAPPER, f"--add-dirs={worktree}", "--enable=gpg", "--", "sh", "-c", script],
        cwd=worktree,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


@linux_bwrap
def test_bwrap_keeps_the_code_in_the_agent_configuration_read_only(checkout):
    worktree, _, home = checkout
    config = home / "agent"
    (config / "projects").mkdir(parents=True)
    script = (
        f"! touch {config}/settings.json 2>/dev/null && ! mkdir {config}/skills/x 2>/dev/null "
        f"&& ! mv {config}/hooks {config}/h 2>/dev/null && ! mv {config}/settings.json "
        f"{config}/s 2>/dev/null && ! mv {config} {config}.moved 2>/dev/null "
        f"&& touch {config}/projects/t {config}/.claude.json"
    )
    result = subprocess.run(
        ["bash", WRAPPER, f"--add-dirs={worktree}", f"--claude-config-dir={config}", "--"]
        + ["sh", "-c", script],
        cwd=worktree,
        env=environment(home, bwrap=None),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert (config / "settings.json").read_text() == "{}\n"
    assert not (config / "skills" / "x").exists()
    assert (config / "projects" / "t").exists()
