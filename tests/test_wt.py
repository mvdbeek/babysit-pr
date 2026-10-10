"""Unit tests for the pure planning code in ``wt.py`` plus process-level end-to-end runs.

The end-to-end tests drive the tool as a subprocess against temporary repositories and
the fake ``gh``/``herdr``/``tmux``/``cmux``/agent executables from ``conftest.py``.
"""

import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import wt

TOOL = str(Path(wt.__file__).resolve())


class FakeRunner:
    """Scripted process results keyed by argv prefix; records every call."""

    def __init__(self, results=None, executables=()):
        self.results = list(results or [])
        self.executables = set(executables)
        self.calls = []

    def run(self, argv, *, cwd=None, env=None, output="capture"):
        self.calls.append((list(argv), output, dict(env or {}).get("CMUX_QUIET")))
        for prefix, result in self.results:
            if list(argv[: len(prefix)]) == list(prefix):
                return result
        return wt.Completed(0, "", "")

    def which(self, name):
        return f"/fake/{name}" if name in self.executables else None


def tool(runner=None, env=None, **kwargs):
    kwargs.setdefault("stdout", io.StringIO())
    kwargs.setdefault("stderr", io.StringIO())
    kwargs.setdefault("isatty", lambda: False)
    kwargs.setdefault("stage", lambda prompt: "/tmp/wt-prompt.fixture")
    return wt.Tool(runner or FakeRunner(), env or {"HOME": "/home/u"}, **kwargs)


def options(**values):
    parsed = wt.Options()
    for key, value in values.items():
        setattr(parsed, key, value)
    return parsed


# --- option parsing and validation ---------------------------------------------------


def test_parse_args_shared_options_and_positionals(tmp_path):
    brief = tmp_path / "brief.md"
    brief.write_text("Fix it\n\n")
    parsed = wt.parse_args(
        "wtpr",
        [
            "--codex",
            "--model",
            "gpt-5",
            "--effort",
            "high",
            "--no-focus",
            "--name",
            "pr-base-7",
            "--label",
            "pr-repo-feature",
            "--repo-path",
            "/clone",
            "--worktree-root",
            "/root",
            "-F",
            str(brief),
            "7",
        ],
    )
    assert parsed.agent == "codex" and parsed.model == "gpt-5" and parsed.effort == "high"
    assert parsed.focus is False and parsed.name == "pr-base-7"
    assert parsed.label == "pr-repo-feature"
    assert parsed.repo_path == "/clone" and parsed.worktree_root == "/root"
    assert parsed.prompt == "Fix it" and parsed.positional == ["7"]
    assert parsed.repo == "galaxy" and parsed.repo_override is False
    parsed = wt.parse_args("wt", ["-r", "other", "-p", "hi", "base", "branch"])
    assert parsed.repo == "other" and parsed.repo_override is True
    assert parsed.prompt == "hi" and parsed.positional == ["base", "branch"]
    assert wt.parse_args("wti", ["--claude", "12"]).agent == "claude"


def test_agent_args_are_repeatable_words_after_the_agent(tmp_path):
    parsed = wt.parse_args(
        "wt", ["--name", "n", "--agent-arg", "--mcp-config", "--agent-arg", "/p q.json"]
    )
    assert parsed.agent_args == ["--mcp-config", "/p q.json"] and parsed.positional == []
    assert wt.parse_args("wti", ["--agent-arg", "x", "12"]).agent_args == ["x"]
    with pytest.raises(wt.WtError, match="control characters"):
        wt.parse_args("wt", ["--agent-arg", "a\nb", "base"])

    def stage(prompt):
        return "/tmp/prompt"

    # Extra words come last and `--` stops a variadic option from taking the prompt.
    assert wt.agent_command("claude", "go", "", "", stage, ["--mcp-config", "/p q.json"]) == (
        "claude --mcp-config '/p q.json' -- \"$(cat /tmp/prompt)\""
    )
    assert wt.agent_command("claude", "go", "opus", "high", stage, ["--x"]) == (
        'claude --model opus --effort high --x -- "$(cat /tmp/prompt)"'
    )
    assert wt.agent_command("claude", "", "", "", stage, ["--x"]) == "claude --x"


@pytest.mark.parametrize("command", ["wt", "wti", "wtpr"])
def test_parse_args_errors_and_help(command, tmp_path):
    with pytest.raises(wt.UsageError, match=f"{command}: unknown option: --bogus"):
        wt.parse_args(command, ["--bogus"])
    with pytest.raises(wt.WtError, match="--model needs a value"):
        wt.parse_args(command, ["--model"])
    with pytest.raises(wt.WtError, match="--model needs a value"):
        wt.parse_args(command, ["--model", ""])
    with pytest.raises(wt.WtError, match="cannot read prompt file"):
        wt.parse_args(command, ["-F", str(tmp_path / "missing")])
    with pytest.raises(wt.HelpRequested):
        wt.parse_args(command, ["--model", "x", "-h"])


@pytest.mark.parametrize(
    "agent,model,effort,message",
    [
        ("codex", "$(touch INJECTED)", "", "invalid model id"),
        ("codex", "--help", "", "invalid model id"),
        ("codex", "a" * 161, "", "invalid model id"),
        ("claude", "", 'high";touch INJECTED', "invalid reasoning effort"),
        ("codex", "", "high;touch INJECTED", "invalid reasoning effort"),
        ("claude", "", "ultra", "unsupported Claude effort"),
        ("claude", "", "none", "unsupported Claude effort"),
    ],
)
def test_validate_agent_options_rejects(agent, model, effort, message):
    with pytest.raises(wt.WtError, match=message):
        wt.validate_agent_options(agent, model, effort)


def test_validate_agent_options_accepts():
    for effort in ("low", "medium", "high", "xhigh", "max"):
        wt.validate_agent_options("claude", "opus", effort)
        wt.validate_agent_options("codex", "gpt-5.1-codex", effort)
    for effort in ("none", "minimal", "ultra"):
        wt.validate_agent_options("codex", "", effort)
    wt.validate_agent_options("claude", "a" * 160, "")
    wt.validate_agent_options("claude", "org/model:tag", "")


@pytest.mark.parametrize("command", ["wt", "wti", "wtpr"])
@pytest.mark.parametrize(
    "option,value", [("--model", "$(touch INJECTED)"), ("--effort", 'high";touch INJECTED')]
)
def test_invalid_agent_options_run_no_process(command, option, value):
    runner = FakeRunner()
    with pytest.raises(wt.WtError, match="invalid"):
        tool(runner).run(command, [option, value, "7"])
    assert runner.calls == []


@pytest.mark.parametrize("name", ["../x", "a..b", "-x", ".hidden", "a b", "a/b", "a$b"])
def test_validate_name_rejects(name):
    with pytest.raises(wt.WtError, match="--name must be a simple worktree name"):
        wt.validate_name(name, "wtpr")


def test_validate_name_accepts():
    for name in ("pr-base-7", "issue-12.x_y", "A", ""):
        wt.validate_name(name, "wti")


# --- naming --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        (
            "https://github.com/galaxyproject/galaxy/issues/12345",
            ("galaxyproject", "galaxy", "12345"),
        ),
        ("https://github.com/o/r/issues/12345/comments", ("o", "r", "12345")),
        ("https://github.com/o/r/issues/12#issuecomment-99", ("o", "r", "12")),
        ("http://www.github.com/o/r/issues/7", ("o", "r", "7")),
        ("github.com/o/r/issues/7?x=1", ("o", "r", "7")),
        ("https://github.com/o/r/pull/7", None),
        ("12345", None),
        ("https://github.com/o/r/issues/", None),
    ],
)
def test_parse_issue_url(url, expected):
    assert wt.parse_issue_url(url) == expected


@pytest.mark.parametrize(
    "url,expected",
    [
        (
            "https://github.com/galaxyproject/galaxy/pull/22886",
            ("galaxyproject", "galaxy", "22886"),
        ),
        ("https://github.com/o/r/pull/7/files#diff-abc", ("o", "r", "7")),
        ("https://github.com/o/r/issues/7", None),
        ("7", None),
    ],
)
def test_parse_pr_url(url, expected):
    assert wt.parse_pr_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/galaxyproject/galaxy.git",
        "https://github.com/galaxyproject/galaxy",
        "https://github.com/galaxyproject/galaxy/",
        "git@github.com:galaxyproject/galaxy.git",
        "ssh://git@github.com/galaxyproject/galaxy.git",
        "  git@github.com:galaxyproject/galaxy\n",
    ],
)
def test_slug_from_remote_url(url):
    assert wt.slug_from_remote_url(url) == "galaxyproject/galaxy"


def test_parse_worktree_list_keeps_branches_and_skips_bare_and_detached():
    listing = (
        "worktree /home/u/src/repo\nHEAD aaa\nbranch refs/heads/main\n"
        "\nworktree /home/u/src/worktrees/repo/topic\nHEAD bbb\n"
        "branch refs/heads/topic\nlocked\n"
        "\nworktree /home/u/src/worktrees/repo/gone\nHEAD ccc\ndetached\nprunable gitdir\n"
        "\nworktree /home/u/src/repo.git\nbare\n"
    )
    assert wt.parse_worktree_list(listing) == {
        "main": "/home/u/src/repo",
        "topic": "/home/u/src/worktrees/repo/topic",
    }
    assert wt.parse_worktree_list("") == {}
    # A branch line without a preceding worktree line is not a checkout.
    assert wt.parse_worktree_list("branch refs/heads/main\n") == {}


def test_existing_checkout_ignores_missing_directories(tmp_path):
    live = tmp_path / "live"
    live.mkdir()
    listing = (
        f"worktree {live}\nHEAD aaa\nbranch refs/heads/main\n"
        f"\nworktree {tmp_path / 'gone'}\nHEAD bbb\nbranch refs/heads/stale\n"
    )
    runner = FakeRunner(
        [(["git", "-C", "/clone", "worktree", "list"], wt.Completed(0, listing, ""))]
    )
    t = tool(runner)
    assert t.existing_checkout("/clone", "main") == str(live)
    assert t.existing_checkout("/clone", "stale") == ""
    assert t.existing_checkout("/clone", "absent") == ""
    failing = FakeRunner([(["git"], wt.Completed(128, "", "not a repository"))])
    assert tool(failing).existing_checkout("/clone", "main") == ""


@pytest.mark.parametrize(
    "title,expected",
    [
        ("Crash on start!", "crash-on-start"),
        ("  --Weird   Title -- ", "weird-title"),
        ("Ünïcode ok", "n-code-ok"),
        ("!!!", ""),
        ("", ""),
        ("a" * 70, "a" * 60),
        ("a" * 59 + "-bcdef", "a" * 59),
        ("a" * 58 + "-b-cdef", "a" * 58 + "-b"),
    ],
)
def test_title_slug(title, expected):
    assert wt.title_slug(title) == expected
    assert len(wt.title_slug(title)) <= 60


def test_issue_branch():
    assert wt.issue_branch("12", "Crash on start!") == "issue-12-crash-on-start"
    assert wt.issue_branch("12", "!!!") == "issue-12"


def test_sanitize_session_name():
    assert wt.sanitize_session_name("feature/x.y:z") == "feature-x-y-z"
    assert wt.sanitize_session_name("pr-base_7") == "pr-base_7"


# --- agent command and prompt staging --------------------------------------------------


def test_agent_command_without_prompt_never_stages():
    def stage(prompt):
        raise AssertionError("no prompt to stage")

    assert wt.agent_command("claude", "", "", "", stage) == "claude"
    assert (
        wt.agent_command("claude", "", "opus", "high", stage) == "claude --model opus --effort high"
    )
    assert (
        wt.agent_command("codex", "", "gpt-5", "high", stage)
        == "codex -c check_for_update_on_startup=false --model gpt-5 -c 'model_reasoning_effort=\"high\"'"
    )
    assert wt.agent_command("codex", "", "", "ultra", stage) == (
        "codex -c check_for_update_on_startup=false -c 'model_reasoning_effort=\"ultra\"'"
    )


def test_only_codex_skips_its_startup_update_check():
    # A typed message must never land on Codex's update dialog, whose default installs.
    codex = wt.agent_words("codex", "gpt-5", "", ["resume", "x"])
    assert codex[:3] == ["codex", "-c", "check_for_update_on_startup=false"]
    assert codex[3:] == ["--model", "gpt-5", "resume", "x"]
    assert not any("check_for_update" in word for word in wt.agent_words("claude", "opus", "high"))


@pytest.mark.parametrize("command", ["wt", "wti", "wtpr"])
def test_docker_prefixes_the_safehouse_enable_variable(command):
    parsed = wt.parse_args(command, ["--docker", "--codex", "12"])
    assert parsed.docker is True and parsed.agent == "codex"
    assert wt.parse_args(command, ["12"]).docker is False
    assert tool().command_for(options(docker=True, prompt="go")) == (
        'SAFE_ENABLE=docker claude "$(cat /tmp/wt-prompt.fixture)"'
    )
    assert tool().command_for(options(prompt="go")) == 'claude "$(cat /tmp/wt-prompt.fixture)"'


def test_with_checkouts_widen_the_sandbox_and_become_agent_directories():
    also = [
        wt.AlsoCheckout("/src/a", "/wt/a/x", "/src/a/.git"),
        wt.AlsoCheckout("/src/b", "/wt/b/x y", "/src/b/.git"),
    ]
    assert wt.parse_args("wti", ["--with", "a", "--with", "/src/b", "12"]).also == ["a", "/src/b"]
    staged = []

    def stage(text):
        staged.append(text)
        return f"/tmp/staged {len(staged)}"

    # The grants extend safe.env's list and are read back from a file, like the prompt;
    # `--` keeps the variadic --add-dir off the prompt.
    assert wt.agent_command("claude", "go", stage=stage, docker=True, also=also) == (
        "SAFEHOUSE_ADD_DIRS=${SAFEHOUSE_ADD_DIRS:+$SAFEHOUSE_ADD_DIRS:}$(cat '/tmp/staged 1') "
        "SAFE_ENABLE=docker claude --add-dir /wt/a/x --add-dir '/wt/b/x y' -- "
        "\"$(cat '/tmp/staged 2')\""
    )
    assert staged == ["/wt/a/x:/src/a/.git:/wt/b/x y:/src/b/.git", "go"]
    assert wt.agent_command("codex", stage=lambda _: "/tmp/g", also=also[:1]) == (
        "SAFEHOUSE_ADD_DIRS=${SAFEHOUSE_ADD_DIRS:+$SAFEHOUSE_ADD_DIRS:}$(cat /tmp/g) "
        "codex -c check_for_update_on_startup=false --add-dir /wt/a/x"
    )
    with pytest.raises(ValueError, match="':'"):
        wt.agent_command("claude", also=[wt.AlsoCheckout("/a", "/wt/a:b", "/a/.git")])
    # A line the tty would silently drop fails loudly instead.
    long = [wt.AlsoCheckout("/a", "/wt/" + "x" * 300 + str(n), "/a/.git") for n in range(3)]
    with pytest.raises(ValueError, match="too long to type"):
        wt.agent_command("claude", stage=lambda _: "/tmp/g", also=long)
    with pytest.raises(wt.WtError, match="wt: The agent command is too long"):
        tool().command_for(options(also_checkouts=long))


@pytest.mark.parametrize("agent", ["claude", "codex"])
@pytest.mark.parametrize("docker", [False, True])
@pytest.mark.parametrize("config_dir", [None, "/home/u/.claude/accounts/work"])
def test_agent_command_goes_through_the_shell_wrapper_by_default(agent, docker, config_dir):
    # Nested launches (wt, resumes) type this: the claude()/codex() function and its
    # Safehouse must apply, so the command never bypasses it.
    command = wt.agent_command(
        agent,
        "go",
        "m",
        "high",
        lambda _: "/tmp/p",
        docker=docker,
        claude_config_dir=config_dir if agent == "claude" else None,
    )
    assert "command " not in command and "dangerously" not in command
    assert re.search(rf"(^|[ ;]){agent} ", command)


def test_wt_has_no_way_to_bypass_safehouse():
    # Even from an agent running outside Safehouse: nothing in the environment changes
    # what wt types.
    env = {"HOME": "/home/u", "CLAUDECODE": "1", "BABYSIT_CRON_JOB": "1"}
    for agent, flags in (("claude", ""), ("codex", "-c check_for_update_on_startup=false ")):
        command = tool(env=env).command_for(options(agent=agent, prompt="go"))
        assert command == f'{agent} {flags}"$(cat /tmp/wt-prompt.fixture)"'
    with pytest.raises(wt.UsageError):
        wt.parse_args("wt", ["--unsandboxed", "main"])


def test_unsandboxed_agent_commands_skip_the_wrapper_but_not_its_flags(monkeypatch, tmp_path):
    def stage(prompt):
        return "/tmp/p"

    assert wt.agent_command("claude", "go", "opus", "", stage, unsandboxed=True) == (
        'command claude --dangerously-skip-permissions --model opus "$(cat /tmp/p)"'
    )
    assert wt.agent_command("codex", "", "", "high", stage, unsandboxed=True) == (
        "command codex --dangerously-bypass-approvals-and-sandbox "
        "-c check_for_update_on_startup=false -c 'model_reasoning_effort=\"high\"'"
    )
    monkeypatch.setattr(wt.claude_accounts, "default_home", lambda: tmp_path / ".claude")
    account = tmp_path / ".claude" / "accounts" / "work"
    assert wt.agent_command(
        "claude",
        "go",
        stage=stage,
        claude_config_dir=str(account),
        claude_subscription=True,
        unsandboxed=True,
    ) == (
        f"(unset {' '.join(wt.claude_accounts.AUTH_ENV)}; CLAUDE_CONFIG_DIR={account} "
        "SAFEHOUSE_ENV_PASS=CLAUDE_CONFIG_DIR${SAFEHOUSE_ENV_PASS:+,$SAFEHOUSE_ENV_PASS} "
        'command claude --dangerously-skip-permissions "$(cat /tmp/p)")'
    )
    with pytest.raises(ValueError, match="inside Safehouse"):
        wt.agent_command("claude", docker=True, unsandboxed=True)


def test_agent_command_stages_prompt_and_quotes_only_the_path():
    staged = []

    def stage(prompt):
        staged.append(prompt)
        return "/tmp/odd dir's/wt-prompt.abc"

    prompt = "Fix quotes ' \" $() `touch X`\n" * 100
    command = wt.agent_command("codex", prompt, "gpt-5", "high", stage)
    assert staged == [prompt]
    assert command == (
        "codex -c check_for_update_on_startup=false --model gpt-5 "
        "-c 'model_reasoning_effort=\"high\"' "
        "\"$(cat '/tmp/odd dir'\"'\"'s/wt-prompt.abc')\""
    )
    assert "touch" not in command and len(command) < 160
    # The typed line does not grow with the prompt (MAX_CANON is 1024 bytes on macOS).
    short = wt.agent_command("codex", "hi", "gpt-5", "high", stage)
    assert len(command) == len(short)


def test_stage_prompt_writes_private_file_left_for_the_agent(tmp_path):
    path = wt.stage_prompt("brief\ntext", str(tmp_path))
    assert Path(path).parent == tmp_path and Path(path).name.startswith("wt-prompt.")
    assert Path(path).read_text() == "brief\ntext\n"
    assert stat.S_IMODE(Path(path).stat().st_mode) == 0o600
    assert (
        subprocess.check_output(["/bin/sh", "-c", f'printf %s "$(cat {path})"']) == b"brief\ntext"
    )


def test_stage_prompt_reports_unwritable_directory(tmp_path):
    with pytest.raises(wt.WtError, match="could not stage the prompt"):
        wt.stage_prompt("x", str(tmp_path / "missing"))


def test_stage_prompt_uses_tmpdir_env(tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    assert Path(wt.stage_prompt("x")).parent == tmp_path


def test_read_prompt_file(tmp_path):
    brief = tmp_path / "brief"
    brief.write_text("line\n\n\n")
    assert wt.read_prompt_file(str(brief)) == "line"
    with pytest.raises(wt.WtError, match="wti: --prompt-file needs a path"):
        wt.read_prompt_file("", "wti")


# --- multiplexer selection and configuration ------------------------------------------


@pytest.mark.parametrize(
    "env,executables,running,expected",
    [
        ({"WT_MULTIPLEXER": "tmux"}, {"herdr"}, True, "tmux"),
        ({"WT_MULTIPLEXER": "none"}, {"herdr", "tmux"}, True, "none"),
        ({"HERDR_ENV": "1"}, {"herdr", "tmux"}, False, "herdr"),
        ({}, {"herdr", "tmux"}, True, "herdr"),
        ({}, {"herdr", "tmux"}, False, "tmux"),
        ({"HERDR_ENV": "1"}, {"tmux"}, True, "tmux"),
        ({"CMUX_SURFACE_ID": "s1"}, {"cmux", "tmux"}, False, "cmux"),
        ({"CMUX_SURFACE_ID": "s1"}, {"tmux"}, False, "tmux"),
        ({}, {"cmux"}, False, "none"),
        ({}, set(), False, "none"),
        ({"WT_MULTIPLEXER": "auto", "HERDR_ENV": "1"}, {"herdr"}, False, "herdr"),
    ],
)
def test_select_multiplexer(env, executables, running, expected):
    probes = []

    def herdr_running():
        probes.append(True)
        return running

    assert wt.select_multiplexer(env, lambda n: n in executables and n, herdr_running) == expected
    # The server probe only runs when herdr is installed and HERDR_ENV does not settle it.
    assert bool(probes) == (
        "WT_MULTIPLEXER" not in env and "herdr" in executables and env.get("HERDR_ENV") != "1"
    )


def test_herdr_is_running():
    assert wt.herdr_is_running('{"running":true,"pid":1}')
    assert wt.herdr_is_running('{"running": true}')
    assert not wt.herdr_is_running('{"running":false}')
    assert not wt.herdr_is_running("")


def test_read_config_and_env_precedence(tmp_path):
    config = tmp_path / "config"
    config.write_text(
        "# comment\n\nexport WT_MULTIPLEXER=tmux\nOTHER='quoted value'\n"
        'PATH="/evil"\nbad line\n1BAD=x\n'
    )
    values = wt.read_config(str(config))
    assert values == {"WT_MULTIPLEXER": "tmux", "OTHER": "quoted value", "PATH": "/evil"}
    merged = wt.effective_env({"PATH": "/bin", "HOME": "/h"}, values)
    assert merged == {"WT_MULTIPLEXER": "tmux", "PATH": "/bin", "HOME": "/h"}
    assert wt.effective_env({"WT_MULTIPLEXER": "herdr"}, values)["WT_MULTIPLEXER"] == "herdr"
    assert wt.read_config(str(tmp_path / "missing")) == {}


def test_config_path_honours_xdg_and_home():
    assert wt.config_path({"HOME": "/h"}) == "/h/.config/worktree/config"
    assert wt.config_path({"HOME": "/h", "XDG_CONFIG_HOME": "/x"}) == "/x/worktree/config"


# --- multiplexer reply parsing ----------------------------------------------------------


def test_parse_herdr_open():
    reply = wt.parse_herdr_open(
        json.dumps({"result": {"already_open": False, "root_pane": {"pane_id": "w1:p1"}}})
    )
    assert reply == wt.HerdrOpen(False, "w1:p1")
    assert wt.parse_herdr_open(json.dumps({"result": {"already_open": True}})) == (True, "")
    assert wt.parse_herdr_open(json.dumps({"result": {}})) == (False, "")
    with pytest.raises(wt.WtError, match="herdr worktree open failed: no such checkout"):
        wt.parse_herdr_open(json.dumps({"error": {"message": "no such checkout"}}))
    with pytest.raises(wt.WtError, match="returned no JSON: garbage"):
        wt.parse_herdr_open("garbage\n")
    with pytest.raises(wt.WtError, match="unexpected herdr reply"):
        wt.parse_herdr_open("[]")
    with pytest.raises(wt.WtError, match="unexpected herdr reply"):
        wt.parse_herdr_open('{"result": 3}')


def test_herdr_error_message():
    assert wt.herdr_error_message('{"error":{"message":"boom"}}') == "boom"
    assert wt.herdr_error_message('{"error":"plain"}') == "plain"
    assert wt.herdr_error_message('{"result":{}}') == '{"result":{}}'
    assert wt.herdr_error_message("not json\n") == "not json"


def test_cmux_reply_helpers():
    listing = json.dumps(
        {
            "workspaces": [
                {"ref": "a", "current_directory": "/x"},
                {"ref": "b", "current_directory": "/y"},
                {"ref": "", "current_directory": "/z"},
            ]
        }
    )
    assert wt.cmux_workspace_ref(listing, "/y") == "b"
    assert wt.cmux_workspace_ref(listing, "/z") == ""
    assert wt.cmux_workspace_ref(listing, "/none") == ""
    assert wt.cmux_workspace_ref("nope", "/x") == ""
    groups = json.dumps({"groups": [{"name": "galaxy", "ref": "g1"}, {"name": "other"}]})
    assert wt.cmux_group_ref(groups, "galaxy") == "g1"
    assert wt.cmux_group_ref(groups, "other") == ""
    assert wt.cmux_group_ref("{}", "galaxy") == ""
    assert wt.cmux_created_group_ref('{"group": {"ref": "g2"}}') == "g2"
    assert wt.cmux_created_group_ref("{}") == "" and wt.cmux_created_group_ref("x") == ""


def test_cmux_layout_embeds_command_as_json_data():
    command = "codex \"$(cat '/tmp/wt-prompt.x')\""
    layout = json.loads(wt.cmux_layout(command))
    assert layout["direction"] == "horizontal" and layout["split"] == 0.5
    assert layout["children"][0]["pane"]["surfaces"][0] == {"type": "terminal", "command": command}
    assert layout["children"][1]["pane"]["surfaces"] == [{"type": "terminal"}]


# --- command planning through the process seam ---------------------------------------


def test_resolve_command():
    assert wt.resolve_command("/usr/local/bin/wtpr", ["7"]) == ("wtpr", ["7"])
    assert wt.resolve_command("wri", ["12"]) == ("wti", ["12"])
    assert wt.resolve_command("wtissue.py", ["12"]) == ("wti", ["12"])
    assert wt.resolve_command("wt.py", ["wtpr", "7"]) == ("wtpr", ["7"])
    assert wt.resolve_command("wt", ["wri", "12"]) == ("wti", ["12"])
    assert wt.resolve_command("wt", ["wt", "base"]) == ("wt", ["base"])
    assert wt.resolve_command("wt.py", ["issue", "12"]) == ("wt", ["issue", "12"])
    assert wt.resolve_command("python", ["7"]) == ("wt", ["7"])


def test_herdr_label_overrides_the_branch_name_and_is_sanitized():
    reply = json.dumps({"result": {"already_open": False, "root_pane": {"pane_id": "w1:p1"}}})
    runner = FakeRunner(
        [(["herdr", "worktree", "open"], wt.Completed(0, reply, ""))], executables={"herdr"}
    )
    t = tool(runner, {"HOME": "/home/u", "WT_MULTIPLEXER": "herdr"})
    t.open_session(options(label="pr-repo-fix/odd name"), "/wt/pr-base-7", "pr-base-7", "/clone")
    opened = runner.calls[0][0]
    assert opened[opened.index("--label") + 1] == "pr-repo-fix-odd-name"


def test_herdr_session_plan_for_a_new_workspace():
    runner = FakeRunner(
        [
            (
                ["herdr", "worktree", "open"],
                wt.Completed(
                    0,
                    json.dumps(
                        {"result": {"already_open": False, "root_pane": {"pane_id": "w1:p1"}}}
                    ),
                    "",
                ),
            )
        ],
        executables={"herdr"},
    )
    t = tool(runner, {"HOME": "/home/u", "WT_MULTIPLEXER": "herdr"})
    t.open_session(
        options(agent="codex", prompt="Fix", model="gpt-5", effort="high", focus=False),
        "/wt/pr-base-7",
        "pr-base-7",
        "/clone",
    )
    assert [c[0] for c in runner.calls] == [
        [
            "herdr",
            "worktree",
            "open",
            "--cwd",
            "/clone",
            "--path",
            "/wt/pr-base-7",
            "--label",
            "pr-base-7",
            "--no-focus",
        ],
        [
            "herdr",
            "pane",
            "run",
            "w1:p1",
            "codex -c check_for_update_on_startup=false --model gpt-5"
            ' -c \'model_reasoning_effort="high"\' "$(cat /tmp/wt-prompt.fixture)"',
        ],
    ]
    assert t.stderr.getvalue() == ""


def test_herdr_session_reattach_warns_and_skips_the_prompt():
    runner = FakeRunner(
        [
            (
                ["herdr", "worktree", "open"],
                wt.Completed(0, json.dumps({"result": {"already_open": True}}), ""),
            )
        ],
        executables={"herdr"},
    )
    t = tool(runner, {"WT_MULTIPLEXER": "herdr"})
    t.open_session(options(prompt="Fix"), "/wt/x", "x", "/clone")
    assert len(runner.calls) == 1 and runner.calls[0][0][-1] == "--focus"
    assert (
        t.stderr.getvalue()
        == "wt: a herdr workspace for /wt/x already exists; ignoring the prompt\n"
    )


def test_herdr_session_failures():
    runner = FakeRunner(
        [
            (
                ["herdr", "worktree", "open"],
                wt.Completed(1, "", '{"error":{"message":"not a checkout"}}'),
            )
        ],
        executables={"herdr"},
    )
    with pytest.raises(wt.WtError, match="herdr worktree open failed: not a checkout"):
        tool(runner, {"WT_MULTIPLEXER": "herdr"}).open_session(options(), "/wt/x", "x", "/clone")
    runner = FakeRunner(
        [(["herdr", "worktree", "open"], wt.Completed(0, '{"result":{"already_open":false}}', ""))],
        executables={"herdr"},
    )
    with pytest.raises(wt.WtError, match="did not return a root pane for /wt/x"):
        tool(runner, {"WT_MULTIPLEXER": "herdr"}).open_session(options(), "/wt/x", "x", "/clone")
    runner = FakeRunner(
        [
            (
                ["herdr", "worktree", "open"],
                wt.Completed(0, '{"result":{"root_pane":{"pane_id":"p"}}}', ""),
            ),
            (["herdr", "pane", "run"], wt.Completed(1, "", "")),
        ],
        executables={"herdr"},
    )
    with pytest.raises(wt.WtError, match="pane run failed"):
        tool(runner, {"WT_MULTIPLEXER": "herdr"}).open_session(options(), "/wt/x", "x", "/clone")


def test_tmux_session_plan_new_session_detached_hint():
    runner = FakeRunner([(["tmux", "has-session"], wt.Completed(1, "", ""))], executables={"tmux"})
    t = tool(runner, {"WT_MULTIPLEXER": "tmux"})
    t.open_session(options(agent="claude", prompt="Fix", model="opus"), "/wt/a.b", "a.b", "/clone")
    assert [c[0] for c in runner.calls] == [
        ["tmux", "has-session", "-t", "=a-b"],
        ["tmux", "new-session", "-d", "-s", "a-b", "-c", "/wt/a.b"],
        ["tmux", "split-window", "-h", "-t", "a-b", "-c", "/wt/a.b"],
        ["tmux", "select-pane", "-t", "a-b", "-L"],
        [
            "tmux",
            "send-keys",
            "-t",
            "a-b",
            'claude --model opus "$(cat /tmp/wt-prompt.fixture)"',
            "C-m",
        ],
    ]
    assert "started detached; attach with: tmux attach-session -t a-b" in t.stderr.getvalue()


def test_tmux_session_existing_switch_attach_and_failures():
    runner = FakeRunner(executables={"tmux"})
    t = tool(runner, {"WT_MULTIPLEXER": "tmux", "TMUX": "/tmp/tmux-1/default,1,0"})
    t.open_session(options(prompt="Fix"), "/wt/x", "x", "/clone")
    assert [c[0] for c in runner.calls] == [
        ["tmux", "has-session", "-t", "=x"],
        ["tmux", "switch-client", "-t", "x"],
    ]
    assert runner.calls[1][1] == "inherit"
    assert t.stderr.getvalue() == "wt: tmux session 'x' already running; ignoring the prompt\n"
    runner = FakeRunner(executables={"tmux"})
    tool(runner, {"WT_MULTIPLEXER": "tmux"}, isatty=lambda: True).open_session(
        options(), "/wt/x", "x", "/c"
    )
    assert (
        runner.calls[-1][0] == ["tmux", "attach-session", "-t", "x"]
        and runner.calls[-1][1] == "inherit"
    )
    runner = FakeRunner(
        [
            (["tmux", "has-session"], wt.Completed(1, "", "")),
            (["tmux", "split-window"], wt.Completed(1, "", "no")),
        ],
        executables={"tmux"},
    )
    with pytest.raises(wt.WtError, match="tmux split-window failed: no"):
        tool(runner, {"WT_MULTIPLEXER": "tmux"}).open_session(options(), "/wt/x", "x", "/c")


def test_cmux_session_plan_creates_group_and_workspace():
    runner = FakeRunner(
        [
            (["cmux", "list-workspaces"], wt.Completed(0, '{"workspaces":[]}', "")),
            (["cmux", "workspace-group", "list"], wt.Completed(0, '{"groups":[]}', "")),
            (["cmux", "workspace-group", "create"], wt.Completed(0, '{"group":{"ref":"g1"}}', "")),
        ],
        executables={"cmux"},
    )
    t = tool(runner, {"WT_MULTIPLEXER": "cmux"})
    t.open_session(options(agent="codex", prompt="Fix", repo="galaxy"), "/wt/x", "x", "/c")
    calls = [c[0] for c in runner.calls]
    assert calls[:3] == [
        ["cmux", "list-workspaces", "--json"],
        ["cmux", "workspace-group", "list", "--json"],
        [
            "cmux",
            "workspace-group",
            "create",
            "--name",
            "galaxy",
            "--cwd",
            "/wt/x",
            "--from",
            "",
            "--json",
        ],
    ]
    assert calls[3][:8] == [
        "cmux",
        "new-workspace",
        "--name",
        "x",
        "--cwd",
        "/wt/x",
        "--focus",
        "true",
    ]
    assert calls[3][8] == "--layout" and calls[3][10:] == [
        "--group",
        "g1",
        "--group-placement",
        "end",
    ]
    layout = json.loads(calls[3][9])
    assert (
        layout["children"][0]["pane"]["surfaces"][0]["command"]
        == 'codex -c check_for_update_on_startup=false "$(cat /tmp/wt-prompt.fixture)"'
    )
    assert all(c[2] == "1" for c in runner.calls), "every cmux call sets CMUX_QUIET=1"


def test_cmux_session_reuses_workspace_and_group_and_reports_failures():
    runner = FakeRunner(
        [
            (
                ["cmux", "list-workspaces"],
                wt.Completed(0, '{"workspaces":[{"ref":"w9","current_directory":"/wt/x"}]}', ""),
            )
        ],
        executables={"cmux"},
    )
    t = tool(runner, {"WT_MULTIPLEXER": "cmux"})
    t.open_session(options(prompt="Fix"), "/wt/x", "x", "/c")
    assert [c[0] for c in runner.calls] == [
        ["cmux", "list-workspaces", "--json"],
        ["cmux", "select-workspace", "--workspace", "w9"],
    ]
    assert (
        t.stderr.getvalue()
        == "wt: a cmux workspace for /wt/x already exists; ignoring the prompt\n"
    )
    runner = FakeRunner(
        [
            (["cmux", "list-workspaces"], wt.Completed(1, "", "")),
            (
                ["cmux", "workspace-group", "list"],
                wt.Completed(0, '{"groups":[{"name":"galaxy","ref":"g7"}]}', ""),
            ),
        ],
        executables={"cmux"},
    )
    tool(runner, {"WT_MULTIPLEXER": "cmux"}).open_session(
        options(repo="galaxy"), "/wt/x", "x", "/c"
    )
    assert runner.calls[-1][0][-4:] == ["--group", "g7", "--group-placement", "end"]
    assert not any(c[0][:3] == ["cmux", "workspace-group", "create"] for c in runner.calls)
    runner = FakeRunner(
        [
            (["cmux", "workspace-group", "list"], wt.Completed(0, "{}", "")),
            (["cmux", "workspace-group", "create"], wt.Completed(0, "{}", "")),
        ],
        executables={"cmux"},
    )
    with pytest.raises(wt.WtError, match="did not return a group reference for 'galaxy'"):
        tool(runner, {"WT_MULTIPLEXER": "cmux"}).open_session(
            options(repo="galaxy"), "/wt/x", "x", "/c"
        )
    runner = FakeRunner(
        [(["cmux", "workspace-group", "create"], wt.Completed(1, "", ""))], executables={"cmux"}
    )
    with pytest.raises(wt.WtError, match="workspace-group create failed"):
        tool(runner, {"WT_MULTIPLEXER": "cmux"}).open_session(
            options(repo="galaxy"), "/wt/x", "x", "/c"
        )
    runner = FakeRunner(
        [(["cmux", "new-workspace"], wt.Completed(1, "", "busy"))], executables={"cmux"}
    )
    with pytest.raises(wt.WtError, match="new-workspace failed: busy"):
        tool(runner, {"WT_MULTIPLEXER": "cmux"}).open_session(options(repo=""), "/wt/x", "x", "/c")


def test_none_and_missing_multiplexers_print_the_path():
    runner = FakeRunner()
    t = tool(runner, {"WT_MULTIPLEXER": "none"})
    t.open_session(options(prompt="Fix"), "/wt/x", "x", "/c")
    assert t.stdout.getvalue() == "/wt/x\n" and runner.calls == []
    assert t.stderr.getvalue() == "wt: no multiplexer (WT_MULTIPLEXER=none); ignoring the prompt\n"
    t = tool(FakeRunner(), {"WT_MULTIPLEXER": "herdr"})
    t.open_session(options(), "/wt/x", "x", "/c")
    assert t.stdout.getvalue() == "/wt/x\n"
    assert t.stderr.getvalue() == "wt: herdr is not on PATH; opening nothing\n"
    t = tool(FakeRunner(), {"WT_MULTIPLEXER": "screen"})
    t.open_session(options(), "/wt/x", "x", "/c")
    assert "unknown WT_MULTIPLEXER 'screen'" in t.stderr.getvalue()


def test_wt_dispatches_issue_and_pr_forms_with_repo_override():
    seen = []

    class Recorder(wt.Tool):
        def run_wti(self, options):
            seen.append(("wti", options.repo, options.repo_override, options.positional))

        def run_wtpr(self, options):
            seen.append(("wtpr", options.repo, options.repo_override, options.positional))

    t = Recorder(FakeRunner(), {"HOME": "/h"}, stdout=io.StringIO(), stderr=io.StringIO())
    t.run("wt", ["--codex", "issue", "12", "branch"])
    t.run("wt", ["-r", "other", "i", "https://github.com/o/r/issues/12"])
    t.run("wt", ["22886"])
    assert seen == [
        ("wti", "galaxy", False, ["12", "branch"]),
        ("wti", "other", True, ["https://github.com/o/r/issues/12"]),
        ("wtpr", "galaxy", False, ["22886"]),
    ]
    with pytest.raises(wt.UsageError, match="expected a branch"):
        t.run("wt", [])


def test_wti_and_wtpr_argument_errors_before_any_process(tmp_path):
    runner = FakeRunner()
    t = tool(runner, {"HOME": str(tmp_path)})
    with pytest.raises(wt.WtError, match="wti: expected an issue number or GitHub issue URL"):
        t.run("wti", ["abc"])
    with pytest.raises(wt.WtError, match="wtpr: expected a PR number or GitHub PR URL"):
        t.run("wtpr", ["https://github.com/o/r/issues/7"])
    with pytest.raises(wt.UsageError, match="wtpr: expected a PR number"):
        t.run("wtpr", [])
    with pytest.raises(wt.UsageError, match="wti: expected an issue number"):
        t.run("wti", ["--codex"])
    with pytest.raises(wt.WtError, match="wtpr: no main clone at .*src/galaxy"):
        t.run("wtpr", ["7"])
    with pytest.raises(wt.WtError, match="wti: no main clone at .*src/r "):
        t.run("wti", ["https://github.com/o/r/issues/7"])
    (tmp_path / "src/galaxy/.git").mkdir(parents=True)
    with pytest.raises(wt.WtError, match="--name must be a simple worktree name"):
        t.run("wtpr", ["--name", "../x", "7"])
    with pytest.raises(wt.WtError, match="--name must be a simple worktree name"):
        t.run("wti", ["--name", "a..b", "7"])
    assert runner.calls == []


def test_main_help_usage_and_errors(capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    assert wt.main(["wtpr", "--help"]) == 0
    out = capsys.readouterr()
    assert out.out.startswith("usage:\n  wtpr") and out.err == ""
    assert wt.main(["wt.py", "wti", "--bogus"]) == 1
    out = capsys.readouterr()
    assert out.out == "" and out.err.startswith("wti: unknown option: --bogus\nusage:")
    assert wt.main(["wtpr", "--model", "$(touch INJECTED)", "7"]) == 1
    assert capsys.readouterr().err == "wt: invalid model id\n"
    assert not (tmp_path / "INJECTED").exists()


def test_main_reads_config_file_with_env_precedence(capsys, monkeypatch, tmp_path):
    (tmp_path / "config/worktree").mkdir(parents=True)
    (tmp_path / "config/worktree/config").write_text("WT_MULTIPLEXER=none\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("WT_MULTIPLEXER", raising=False)
    worktree = tmp_path / "src/worktrees/galaxy/existing"
    worktree.mkdir(parents=True)
    assert wt.main(["wt", "-p", "Fix", "existing"]) == 0
    out = capsys.readouterr()
    assert out.out == f"{worktree}\n" and "ignoring the prompt" in out.err


def test_subprocess_runner_modes(tmp_path, capfd):
    runner = wt.SubprocessRunner({"PATH": os.environ["PATH"]})
    result = runner.run(
        [sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr)"]
    )
    assert result == wt.Completed(0, "out\n", "err\n")
    result = runner.run([sys.executable, "-c", "print('to stderr')"], output="stderr")
    assert result == wt.Completed(0, "", "") and capfd.readouterr().err == "to stderr\n"
    result = runner.run([sys.executable, "-c", "print('inherited')"], output="inherit")
    assert result.returncode == 0 and capfd.readouterr().out == "inherited\n"
    assert runner.run([str(tmp_path / "missing")]).returncode == 127
    assert (
        runner.run(["sh", "-c", "echo $CMUX_QUIET"], env={**os.environ, "CMUX_QUIET": "1"}).stdout
        == "1\n"
    )
    assert runner.which("sh") and runner.which("definitely-not-installed-xyz") is None
    assert wt.find_executable("sh", {"PATH": ""}) is None


# --- end-to-end against temporary repositories ---------------------------------------


@pytest.fixture
def repo(tmp_path, fake_tools, monkeypatch):
    """A ``repo`` clone under ``$HOME/src`` with an offline fork/base remote and a PR branch."""
    home = tmp_path / "home"
    src = home / "src"
    clone = src / "repo"
    clone.mkdir(parents=True)

    def git(*args, cwd=clone):
        return subprocess.check_output(["git", "-C", str(cwd), *args], text=True).strip()

    git("init", "-b", "main")
    git("config", "user.name", "Fixture")
    git("config", "user.email", "fixture@example.test")
    git("commit", "--allow-empty", "-m", "base")
    git("branch", "feature")
    git("remote", "add", "origin", "https://github.com/base/repo.git")
    head = tmp_path / "head.git"
    git("clone", "--bare", str(clone), str(head))
    config = tmp_path / "gitconfig"
    config.write_text(
        f'[url "{head}"]\n\tinsteadOf = https://github.com/fork/repo.git\n'
        f'[url "{head}"]\n\tinsteadOf = https://github.com/base/repo.git\n'
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("WT_MULTIPLEXER", "herdr")
    monkeypatch.delenv("HERDR_ENV", raising=False)
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("CMUX_SURFACE_ID", raising=False)
    # Only the fakes plus the few real tools the helper and the pane shell need are
    # reachable, so nothing can fall through to a real tmux, cmux or herdr.
    shim = tmp_path / "shim"
    shim.mkdir()
    for name in ("git", "cat"):
        os.symlink(shutil.which(name), shim / name)
    monkeypatch.setenv("PATH", f"{os.environ['PATH'].split(os.pathsep)[0]}{os.pathsep}{shim}")
    return home, git, fake_tools


def run_tool(*args, check=True, **kwargs):
    result = subprocess.run(
        [sys.executable, TOOL, *args],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        **kwargs,
    )
    if check:
        assert result.returncode == 0, result.stderr
    return result


def state_of(path):
    return json.loads(path.read_text())


def test_wtpr_fetches_fork_branch_and_records_provenance(repo):
    home, git, state = repo
    brief = home / "brief.md"
    brief.write_text("x" * 3000)
    result = run_tool(
        "wtpr",
        "-r",
        "repo",
        "--codex",
        "--model",
        "gpt-5",
        "--effort",
        "high",
        "-F",
        str(brief),
        "7",
    )
    assert result.stdout == ""
    # The local `feature` branch exists, so wtpr falls back to pr-<base-owner>-<n>.
    path = home / "src/worktrees/repo/pr-base-7"
    assert git("symbolic-ref", "--short", "HEAD", cwd=path) == "pr-base-7"
    assert git("config", "--get", "branch.pr-base-7.remote", cwd=path) == "wtpr-fork-repo"
    assert git("config", "--get", "branch.pr-base-7.merge", cwd=path) == "refs/heads/feature"
    assert git("config", "--get", "remote.wtpr-fork-repo.url") == "https://github.com/fork/repo.git"
    assert git("rev-parse", "HEAD", cwd=path) == git("rev-parse", "feature")
    data = state_of(state)
    assert data["gh"] == [
        [
            "-R",
            "base/repo",
            "pr",
            "view",
            "7",
            "--json",
            "headRefName,headRepository,headRepositoryOwner",
        ]
    ]
    opened = next(c for c in data["calls"] if c[:2] == ["worktree", "open"])
    assert opened == [
        "worktree",
        "open",
        "--cwd",
        str(home / "src/repo"),
        "--path",
        str(path),
        "--label",
        "pr-base-7",
        "--focus",
    ]
    typed = next(c for c in data["calls"] if c[:2] == ["pane", "run"])[3]
    assert typed.startswith(
        "codex -c check_for_update_on_startup=false --model gpt-5"
        ' -c \'model_reasoning_effort="high"\' "$(cat '
    )
    assert len(typed.encode()) < 300 < 1024, typed
    assert data["agents"][0]["argv"] == [
        "-c",
        "check_for_update_on_startup=false",
        "--model",
        "gpt-5",
        "-c",
        'model_reasoning_effort="high"',
        "x" * 3000,
    ]
    assert data["agents"][0]["cwd"] == str(path)
    # A second PR checkout allocates the next suffix; the first one is untouched.
    run_tool("wtpr", "https://github.com/base/repo/pull/7")
    assert (home / "src/worktrees/repo/pr-base-7-2").is_dir()
    assert git("symbolic-ref", "--short", "HEAD", cwd=path) == "pr-base-7"


def test_wtpr_reattach_verifies_checkout_and_ignores_prompt(repo):
    home, git, state = repo
    git("branch", "-D", "feature")
    run_tool("wtpr", "-r", "repo", "-p", "first", "7")
    path = home / "src/worktrees/repo/feature"
    assert git("symbolic-ref", "--short", "HEAD", cwd=path) == "feature"
    result = run_tool("wtpr", "-r", "repo", "-p", "second", "7")
    assert (
        result.stderr.strip()
        == f"wt: a herdr workspace for {path} already exists; ignoring the prompt"
    )
    data = state_of(state)
    assert [a["task"] for a in data["agents"]] == ["first"]
    assert len(data["workspaces"]) == 1
    # A directory that is not the requested checkout is refused, never reused.
    git("switch", "-c", "elsewhere", cwd=path)
    result = run_tool("wtpr", "-r", "repo", "7", check=False)
    assert result.returncode == 1
    assert result.stderr.strip() == "wtpr: existing directory is not the requested checkout"
    # Explicit names never reuse a directory or a branch.
    result = run_tool("wtpr", "-r", "repo", "--name", "feature", "7", check=False)
    assert "explicit worktree destination already exists" in result.stderr
    result = run_tool("wtpr", "-r", "repo", "--name", "elsewhere", "7", check=False)
    assert result.stderr.strip() == "wtpr: explicit branch already exists: elsewhere"


def test_wtpr_dashboard_flags_and_no_focus(repo, tmp_path):
    home, git, state = repo
    root = tmp_path / "elsewhere"
    result = run_tool(
        "wtpr",
        "--claude",
        "--model",
        "opus",
        "--effort",
        "max",
        "--no-focus",
        "--name",
        "pr-base-7",
        "--repo-path",
        str(home / "src/repo"),
        "--worktree-root",
        str(root),
        "-p",
        "Fix quotes ' \" $() `touch SHOULD_NOT_EXIST`\nsecond line",
        "https://github.com/base/repo/pull/7",
    )
    assert result.returncode == 0
    path = root / "pr-base-7"
    assert git("symbolic-ref", "--short", "HEAD", cwd=path) == "pr-base-7"
    data = state_of(state)
    opened = next(c for c in data["calls"] if c[:2] == ["worktree", "open"])
    assert opened[-1] == "--no-focus" and opened[opened.index("--cwd") + 1] == str(
        home / "src/repo"
    )
    assert data["agents"][0]["argv"] == [
        "--model",
        "opus",
        "--effort",
        "max",
        "Fix quotes ' \" $() `touch SHOULD_NOT_EXIST`\nsecond line",
    ]
    assert not (path / "SHOULD_NOT_EXIST").exists()


def test_wtpr_refuses_remote_name_of_another_repository_and_bad_heads(repo, monkeypatch):
    home, git, state = repo
    git("remote", "add", "wtpr-fork-repo", "https://github.com/someone-else/repo.git")
    result = run_tool("wtpr", "-r", "repo", "7", check=False)
    assert result.returncode == 1
    assert result.stderr.strip() == "wtpr: PR remote name belongs to another repository"
    monkeypatch.setenv(
        "FAKE_GH_PR",
        '{"headRefName": "bad..ref", "headRepository": {"name": "repo"}, "headRepositoryOwner": {"login": "fork"}}',
    )
    result = run_tool("wtpr", "-r", "repo", "7", check=False)
    assert result.stderr.strip() == "wtpr: invalid PR head repository or branch"
    monkeypatch.setenv("FAKE_GH_PR", '{"headRefName": "x", "headRepository": null}')
    result = run_tool("wtpr", "-r", "repo", "7", check=False)
    assert result.stderr.strip() == "wtpr: invalid PR head repository or branch"
    monkeypatch.setenv("FAKE_GH_PR", "not json")
    result = run_tool("wtpr", "-r", "repo", "7", check=False)
    assert result.stderr.strip() == "wtpr: unexpected gh reply"
    monkeypatch.setenv("FAKE_GH_FAIL", "GraphQL: Could not resolve to a PullRequest")
    result = run_tool("wtpr", "-r", "repo", "7", check=False)
    assert result.returncode == 1
    assert (
        result.stderr.strip()
        == "wtpr: gh pr view failed: GraphQL: Could not resolve to a PullRequest"
    )
    assert not (home / "src/worktrees").exists()


def test_wti_default_naming_reattach_and_explicit_name_rules(repo):
    home, git, state = repo
    run_tool("wti", "--codex", "-p", "Fix", "https://github.com/base/repo/issues/12/comments")
    path = home / "src/worktrees/repo/issue-12-crash-on-start"
    assert git("symbolic-ref", "--short", "HEAD", cwd=path) == "issue-12-crash-on-start"
    assert git("rev-parse", "HEAD", cwd=path) == git("rev-parse", "main")
    data = state_of(state)
    assert data["gh"] == [["-R", "base/repo", "issue", "view", "12", "--json", "number,title"]]
    assert data["agents"][0]["agent"] == "codex" and data["agents"][0]["task"] == "Fix"
    # Reattaching an existing directory warns about the prompt and starts nothing.
    result = run_tool("wri", "-r", "repo", "-p", "again", "12")
    assert "already exists; ignoring the prompt" in result.stderr
    assert len(state_of(state)["agents"]) == 1
    # --name reserves new resources: an existing directory or branch is an error.
    result = run_tool("wti", "-r", "repo", "--name", "issue-12-crash-on-start", "12", check=False)
    assert (
        result.returncode == 1 and "explicit worktree destination already exists" in result.stderr
    )
    result = run_tool("wti", "-r", "repo", "--name", "feature", "12", check=False)
    assert result.stderr.strip() == "wti: explicit branch already exists: feature"
    run_tool("wti", "-r", "repo", "--name", "issue-12-crash-on-start-2", "12")
    assert (home / "src/worktrees/repo/issue-12-crash-on-start-2").is_dir()
    # A positional branch name reuses an existing local branch instead of creating one.
    run_tool("wtissue", "-r", "repo", "12", "feature")
    assert (
        git("symbolic-ref", "--short", "HEAD", cwd=home / "src/worktrees/repo/feature") == "feature"
    )


def test_wt_name_reserves_a_new_branch_from_the_default_or_given_base(repo):
    home, git, state = repo
    run_tool(
        "wt",
        "--codex",
        "--no-focus",
        "--name",
        "sentry-x-1",
        "--label",
        "sentry-repo-X-1",
        "--agent-arg",
        "--flag",
        "-r",
        "repo",
        "-p",
        "Fix",
    )
    path = home / "src/worktrees/repo/sentry-x-1"
    assert git("symbolic-ref", "--short", "HEAD", cwd=path) == "sentry-x-1"
    assert git("rev-parse", "HEAD", cwd=path) == git("rev-parse", "main")
    data = state_of(state)
    assert data["agents"][0]["argv"][:3] == ["-c", "check_for_update_on_startup=false", "--flag"]
    assert data["agents"][0]["task"] == "Fix"
    opened = next(c for c in data["calls"] if c[:2] == ["worktree", "open"])
    assert opened[opened.index("--label") + 1] == "sentry-repo-X-1" and "--no-focus" in opened
    result = run_tool("wt", "-r", "repo", "--name", "sentry-x-1", check=False)
    assert "explicit worktree destination already exists" in result.stderr
    result = run_tool("wt", "-r", "repo", "--name", "feature", check=False)
    assert result.stderr.strip() == "wt: explicit branch already exists: feature"
    result = run_tool("wt", "-r", "repo", "--name", "y", "a", "b", check=False)
    assert "at most one base branch" in result.stderr
    run_tool("wt", "-r", "repo", "--name", "from-feature", "feature")
    assert git("rev-parse", "HEAD", cwd=home / "src/worktrees/repo/from-feature") == git(
        "rev-parse", "feature"
    )


def test_with_checks_the_branch_out_in_other_clones_for_the_agent(repo, monkeypatch):
    home, git, state = repo
    other = home / "src/other"
    other.mkdir()
    git("init", "-b", "main", cwd=other)
    git("commit", "--allow-empty", "-m", "other", cwd=other)
    git("branch", "both", cwd=other)
    monkeypatch.setenv("SAFEHOUSE_ADD_DIRS", "/granted")
    run_tool("wt", "-r", "repo", "--with", "other", "--name", "both", "-p", "Fix both")
    main, also = home / "src/worktrees/repo/both", home / "src/worktrees/other/both"
    assert git("symbolic-ref", "--short", "HEAD", cwd=main) == "both"
    # An existing local branch in the other clone is checked out, not recreated.
    assert git("rev-parse", "HEAD", cwd=also) == git("rev-parse", "both", cwd=other)
    agent = state_of(state)["agents"][0]
    real = also.resolve()
    assert agent["cwd"] == str(main.resolve())
    assert agent["safehouse_add_dirs"] == f"/granted:{real}:{other.resolve()}/.git"
    assert agent["argv"][:3] == ["--add-dir", str(real), "--"]
    assert agent["task"] == (
        "Fix both\n\nThis task also covers other repositories. Branch both is checked out in"
        f" each of these, and you can write and commit there:\n- {real}"
    )
    # A worktree already holding the branch is used where it is.
    linked = home / "linked"
    git("worktree", "add", "-q", "-b", "elsewhere", str(linked), cwd=other)
    result = run_tool("wt", "-r", "repo", "--with", str(other), "main", "elsewhere")
    assert f"elsewhere is already checked out at {linked}; using that" in result.stderr
    assert not (home / "src/worktrees/other/elsewhere").exists()
    assert state_of(state)["agents"][-1]["argv"][1] == str(linked.resolve())
    # A clone's own working tree is never handed over, and nothing is checked out first.
    second = home / "src/second"
    git("clone", "-q", str(other), str(second))
    git("switch", "-c", "main-too", cwd=other)
    result = run_tool(
        "wt",
        "-r",
        "repo",
        "--with",
        "second",
        "--with",
        "other",
        "feature",
        "main-too",
        check=False,
    )
    assert result.stderr.splitlines()[-1] == (
        f"wt: main-too is checked out in {other.resolve()} itself; "
        "switch that clone to another branch first"
    )
    assert not (home / "src/worktrees/second/main-too").exists()
    result = run_tool("wt", "-r", "repo", "--with", "missing", "main", check=False)
    assert result.stderr.strip() == f"wt: --with: no main clone at {home}/src/missing"
    result = run_tool("wt", "-r", "repo", "--with", "a b", "main", check=False)
    assert "--with needs a repository name or a clone path" in result.stderr


def test_with_refuses_a_line_too_long_to_type_before_making_any_checkout(repo):
    home, git, state = repo
    names = [f"{n}{'x' * 200}" for n in range(3)]
    for name in names:
        git("init", "-q", "-b", "main", str(home / "src" / name), cwd=home)
        git("commit", "-q", "--allow-empty", "-m", "c", cwd=home / "src" / name)
    withs = [word for name in names for word in ("--with", name)]
    result = run_tool("wt", "-r", "repo", *withs, "main", "wide", check=False)
    assert result.stderr.splitlines()[-1] == (
        "wt: The agent command is too long to type; use fewer --with clones"
    )
    assert not any((home / "src/worktrees" / name).exists() for name in names)
    assert not [c for c in state_of(state)["calls"] if c[:2] == ["worktree", "open"]]


def test_wti_slug_fallback_from_remote_and_origin_head(repo, monkeypatch):
    home, git, state = repo
    git("remote", "add", "upstream", "git@github.com:upstream-owner/repo.git")
    git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/feature")
    monkeypatch.setenv("FAKE_GH_ISSUE", '{"number": 12, "title": "!!!"}')
    run_tool("wti", "-r", "repo", "12")
    data = state_of(state)
    assert data["gh"][0][:2] == ["-R", "upstream-owner/repo"]
    path = home / "src/worktrees/repo/issue-12"
    assert git("symbolic-ref", "--short", "HEAD", cwd=path) == "issue-12"
    assert git("rev-parse", "HEAD", cwd=path) == git("rev-parse", "feature")
    monkeypatch.setenv("FAKE_GH_ISSUE", "[]")
    result = run_tool("wti", "-r", "repo", "13", check=False)
    assert result.stderr.strip() == "wti: unexpected gh reply"
    monkeypatch.setenv("FAKE_GH_ISSUE", '{"number": "12"}')
    result = run_tool("wti", "-r", "repo", "13", check=False)
    assert result.stderr.strip() == "wti: unexpected gh reply"


def test_wt_branch_worktree_and_dispatch(repo):
    home, git, state = repo
    # Numeric arguments are PRs and `issue` routes to wti, both in the -r repository.
    run_tool("wt", "-r", "repo", "7")
    run_tool("wt", "-r", "repo", "issue", "12")
    assert (home / "src/worktrees/repo/pr-base-7").is_dir()
    assert (home / "src/worktrees/repo/issue-12-crash-on-start").is_dir()
    run_tool("wt", "-r", "repo", "-p", "Go", "feature", "my-branch")
    path = home / "src/worktrees/repo/my-branch"
    assert git("symbolic-ref", "--short", "HEAD", cwd=path) == "my-branch"
    assert git("rev-parse", "HEAD", cwd=path) == git("rev-parse", "feature")
    assert state_of(state)["agents"][-1]["task"] == "Go"
    # An existing local branch is reused rather than recreated.
    run_tool("wt", "-r", "repo", "main", "feature")
    assert (
        git("symbolic-ref", "--short", "HEAD", cwd=home / "src/worktrees/repo/feature") == "feature"
    )
    # `main` is checked out in the main clone, so wt opens that instead of adding a
    # second checkout, which git would refuse anyway.
    result = run_tool("wt", "-r", "repo", "main")
    assert f"wt: main is already checked out at {home}/src/repo; opening that" in result.stderr
    assert not (home / "src/worktrees/repo/main").exists()
    opened = [c for c in state_of(state)["calls"] if c[:2] == ["worktree", "open"]]
    assert opened[-1][opened[-1].index("--path") + 1] == str(home / "src/repo")
    # The PR head branch now has a checkout of its own, so wtpr reattaches to it.
    run_tool("wt", "-r", "repo", "7")
    opened = [c for c in state_of(state)["calls"] if c[:2] == ["worktree", "open"]]
    assert opened[-1][opened[-1].index("--path") + 1] == str(home / "src/worktrees/repo/feature")
    # wt.py with a leading subcommand behaves like the symlinked name.
    result = run_tool("wtpr", "--help")
    assert result.stdout.startswith("usage:\n  wtpr")


@pytest.mark.parametrize("other_remote", [False, True])
def test_wt_local_branch_worktrees_without_origin(repo, monkeypatch, other_remote):
    home, git, _ = repo
    git("remote", "remove", "origin")
    if other_remote:
        git("remote", "add", "upstream", str(home / "missing-remote.git"))
    monkeypatch.setenv("WT_MULTIPLEXER", "none")
    git("commit", "--allow-empty", "-m", "local-only commit")
    root = home / "src/worktrees/repo"

    result = run_tool("wt", "-r", "repo", "main", "local-new")
    path = root / "local-new"
    assert result.stdout.strip() == str(path)
    assert git("symbolic-ref", "--short", "HEAD", cwd=path) == "local-new"
    assert git("rev-parse", "HEAD", cwd=path) == git("rev-parse", "main")
    assert run_tool("wt", "-r", "repo", "main", "local-new").stdout == result.stdout

    run_tool("wt", "-r", "repo", "feature")
    assert git("symbolic-ref", "--short", "HEAD", cwd=root / "feature") == "feature"
    assert git("rev-parse", "HEAD", cwd=root / "feature") == git("rev-parse", "feature")
    assert git("rev-parse", "feature") != git("rev-parse", "main")

    result = run_tool("wt", "-r", "repo", "missing-base", "invalid", check=False)
    assert result.returncode == 1
    assert "git worktree failed" in result.stderr
    assert not (root / "invalid").exists()


def test_wt_does_not_hide_origin_fetch_failure(repo):
    home, git, _ = repo
    git("remote", "set-url", "origin", str(home / "missing-remote.git"))
    result = run_tool("wt", "-r", "repo", "main", "fetch-failed", check=False)
    assert result.returncode == 1
    assert "git fetch failed" in result.stderr
    assert not (home / "src/worktrees/repo/fetch-failed").exists()


def test_auto_selects_running_herdr_then_tmux_then_none(repo, monkeypatch):
    home, git, state = repo
    monkeypatch.delenv("WT_MULTIPLEXER")
    run_tool("wt", "-r", "repo", "main", "auto-herdr")
    data = state_of(state)
    assert data["calls"][0] == ["status", "server", "--json"]
    assert data["workspaces"][0]["label"] == "auto-herdr"
    monkeypatch.setenv("FAKE_HERDR_RUNNING", "false")
    result = run_tool("wt", "-r", "repo", "-p", "Fix", "main", "auto-tmux")
    data = state_of(state)
    assert data["tmux"][0] == ["has-session", "-t", "=auto-tmux"]
    assert data["tmux"][1][:3] == ["new-session", "-d", "-s"]
    typed = data["tmux"][4]
    assert typed[:4] == ["send-keys", "-t", "auto-tmux", 'claude "$(cat '] or typed[3].startswith(
        'claude "$(cat '
    )
    assert typed[-1] == "C-m"
    assert "started detached; attach with: tmux attach-session -t auto-tmux" in result.stderr
    assert len(data["workspaces"]) == 1
    monkeypatch.setenv("WT_MULTIPLEXER", "none")
    result = run_tool("wt", "-r", "repo", "-p", "Fix", "main", "auto-none")
    assert result.stdout.strip() == str(home / "src/worktrees/repo/auto-none")
    assert result.stderr.splitlines()[-1] == (
        "wt: no multiplexer (WT_MULTIPLEXER=none); ignoring the prompt"
    )
    assert len(state_of(state)["tmux"]) == len(data["tmux"])


def test_cmux_end_to_end_groups_by_repository(repo, monkeypatch):
    home, git, state = repo
    monkeypatch.setenv("WT_MULTIPLEXER", "cmux")
    run_tool("wt", "-r", "repo", "--codex", "-p", "Fix", "main", "cmux-one")
    data = state_of(state)
    assert data["cmux"][0] == ["list-workspaces", "--json"]
    assert data["cmux"][2][:4] == ["workspace-group", "create", "--name", "repo"]
    created = data["cmux"][3]
    assert created[:4] == ["new-workspace", "--name", "cmux-one", "--cwd"]
    assert created[-4:] == ["--group", "g1", "--group-placement", "end"]
    layout = json.loads(created[created.index("--layout") + 1])
    command = layout["children"][0]["pane"]["surfaces"][0]["command"]
    launch = 'codex -c check_for_update_on_startup=false "$(cat '
    assert command.startswith(launch) and "Fix" not in command
    staged = command[len(launch) : -len(')"')]
    assert Path(staged.strip("'")).read_text() == "Fix\n"
    # The same worktree again selects the existing workspace; a second worktree joins the group.
    result = run_tool("wt", "-r", "repo", "-p", "again", "main", "cmux-one")
    assert "a cmux workspace for" in result.stderr and "ignoring the prompt" in result.stderr
    data = state_of(state)
    assert data["cmux"][-1] == ["select-workspace", "--workspace", "ws1"]
    run_tool("wt", "-r", "repo", "main", "cmux-two")
    data = state_of(state)
    assert data["cmux"][-1][-4:] == ["--group", "g1", "--group-placement", "end"]
    assert len(data["cmux_groups"]) == 1 and len(data["cmux_workspaces"]) == 2


def test_herdr_open_error_is_reported(repo, monkeypatch):
    home, git, state = repo
    monkeypatch.setenv("FAKE_HERDR_OPEN_ERROR", "checkout is not a worktree of --cwd")
    result = run_tool("wt", "-r", "repo", "main", "broken", check=False)
    assert result.returncode == 1
    assert result.stderr.splitlines()[-1] == (
        "wt: herdr worktree open failed: checkout is not a worktree of --cwd"
    )
    monkeypatch.delenv("FAKE_HERDR_OPEN_ERROR")
    monkeypatch.setenv("FAKE_HERDR_NO_ROOT", "1")
    result = run_tool("wt", "-r", "repo", "main", "rootless", check=False)
    assert result.stderr.splitlines()[-1] == (
        f"wt: herdr did not return a root pane for {home}/src/worktrees/repo/rootless"
    )
    assert state_of(state)["agents"] == []
