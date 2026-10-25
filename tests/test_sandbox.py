"""Sandbox launch lines: exact macOS (Safehouse) snapshots and the Linux bwrap wrapper.

The snapshots pin every command line and environment prefix the Safehouse integration
produced before ``sandbox.py`` existed, so the refactor stays byte-identical on macOS.
No real agent ever starts; only the Linux-gated tests run bubblewrap, with ``sh``.
"""

import hashlib
import itertools
import os
import pty
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


def environment(home, bwrap="path", **extra):
    """The wrapper's environment, with a fake bwrap that binds by ``path`` or ``fd``.

    ``bwrap=None`` leaves the host's bwrap, for the tests that run it.
    """
    path = os.environ["PATH"] if bwrap is None else f"{FAKE_BWRAP / bwrap}:{os.environ['PATH']}"
    base = {"PATH": path, "HOME": str(home), "TERM": "dumb", "LANG": "C"}
    return {**base, "USER": "fixture", "GIT_CONFIG_NOSYSTEM": "1", **extra}


def sandbox_cache(home, config_var="", config_dir=""):
    key = hashlib.sha256(f"{config_var}={config_dir}".encode()).hexdigest()[:16]
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
    assert last_writable < argv.index("--ro-bind-try")
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
