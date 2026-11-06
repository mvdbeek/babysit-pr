#!/usr/bin/env python3
"""Portable ``wt``, ``wti`` and ``wtpr`` Git worktree helpers.

Create (or reattach to) a worktree for a branch, a GitHub issue or a pull request,
then open it in a terminal multiplexer (herdr, tmux or cmux) with a coding agent
already started in the left pane. This replaces the zsh functions of the same names:
same command line, but a plain program that any caller can run directly.

Only the standard library is used and imports stay cheap; the real cost of a run is
git, gh and the multiplexer. Every process call goes through one :class:`Runner`
so the planning code (option parsing, naming, command construction, reply parsing)
is pure and unit-testable without subprocesses.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sqlite3
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import NamedTuple, Protocol, TextIO

import claude_accounts
import safe_git
import sandbox
import work_tags

COMMANDS = ("wt", "wti", "wtpr")
ALIASES = {"wri": "wti", "wtissue": "wti"}
DEFAULT_REPO = "galaxy"
MULTIPLEXERS = ("herdr", "tmux", "cmux", "none", "auto")

MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,159}")
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})
CODEX_EFFORTS = frozenset({"none", "minimal", "ultra"})
# Claude permission modes wt starts instead of the shell function's bypass.
PERMISSION_MODES = frozenset({"auto"})
CODEX_NO_UPDATE_CHECK = "check_for_update_on_startup=false"
# Canonical tty input drops a longer typed line (MAX_CANON is 1024 bytes on macOS).
TYPED_LIMIT = 1000
REPO_SLUG = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
DIGITS = re.compile(r"[0-9]+")
GITHUB = r"(?:https?://)?(?:www\.)?github\.com/([^/]+)/([^/]+)/"
ISSUE_URL = re.compile(GITHUB + r"issues/([0-9]+)")
PR_URL = re.compile(GITHUB + r"pull/([0-9]+)")
UNSAFE = re.compile(r"[^A-Za-z0-9_-]")
CONFIG_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
HERDR_RUNNING = re.compile(r'"running"\s*:\s*true')
# A worker's registry id (machines.ID), and the dashboard that places launches on one.
MACHINE = re.compile(r"[a-z][a-z0-9-]{0,23}")
DASHBOARD = "http://127.0.0.1:8765"
# How often, and for how long, a launch on a worker is followed through the dashboard.
LAUNCH_POLL = 5.0
LAUNCH_WAIT = 900
# What a launch on a worker asks the controller for (see launch_request), and the new
# branch names its New task takes (pr_workspaces.BRANCH_NAME is this one).
LAUNCH_FIELDS = (
    "repo",
    "base",
    "name",
    "agent",
    "model",
    "effort",
    "claude_account",
    "task",
    "docker",
    "exit_when_done",
    "permission_mode",
    "machine",
)
LAUNCH_BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
# Why `wt` without a brief stays here: said only when a worker was asked for by name or auto.
NO_PROMPT = "a launch on a worker needs a prompt (-p or -F)"
# A conversation id as Claude and Codex give it to the commands they run.
SESSION = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
SESSION_ENV = (("claude", "CLAUDE_CODE_SESSION_ID"), ("codex", "CODEX_THREAD_ID"))

VALUE_OPTIONS = {
    "-r": "repo",
    "-p": "prompt",
    "--prompt": "prompt",
    "-F": "prompt_file",
    "--prompt-file": "prompt_file",
    "--model": "model",
    "--effort": "effort",
    "--claude-account": "claude_account",
    "--permission-mode": "permission_mode",
    "--name": "name",
    "--label": "label",
    "--repo-path": "repo_path",
    "--github-repo": "github_repo",
    "--worktree-root": "worktree_root",
    "--agent-arg": "agent_args",
    "--with": "also",
    "--tag": "tags",
    "--machine": "machine",
}
WT_OPTIONS = frozenset(
    {
        "--codex",
        "--claude",
        "--docker",
        "--model",
        "--effort",
        "--claude-account",
        "--permission-mode",
        "-r",
        "-p",
        "--prompt",
        "-F",
        "--prompt-file",
        "--with",
        "--tag",
        "--exit-when-done",
        "--machine",
    }
)
DASHBOARD_OPTIONS = frozenset(
    {
        "--no-focus",
        "--name",
        "--label",
        "--repo-path",
        "--github-repo",
        "--worktree-root",
        "--agent-arg",
        "--gpg",
        "--exit-watch-json",
    }
)
OPTIONS = {
    "wt": WT_OPTIONS | DASHBOARD_OPTIONS,
    "wti": WT_OPTIONS | DASHBOARD_OPTIONS,
    "wtpr": WT_OPTIONS | DASHBOARD_OPTIONS,
}

WT_HELP = """\
usage:
  wt [opts] <branch|pr-number> [dirname]
  wt [opts] issue <issue-number|issue-url> [branch-name]
  wt tag add|remove <tag> [workspace-path|github-url ...]
  wt tag list [tag]

Open or create a Git worktree, then attach a multiplexer session.
Tags span repositories and are shared with the dashboard in $BABYSIT_HOME.
Tag add/remove defaults to the current directory. Tag list shows assignments
and linked items, optionally limited to one tag. GitHub labels are unchanged.

Numeric arguments are treated as PR numbers. A branch that is already checked
out -- in the main clone or in a worktree under another name -- is opened where
it is instead of being checked out a second time. Without an origin remote,
branches are created from the requested local base.

The multiplexer is chosen by $WT_MULTIPLEXER (herdr|tmux|cmux|none|auto), set
in the env or as a KEY=VALUE line in ~/.config/worktree/config; "auto" (default)
uses herdr when a herdr server is running, cmux inside cmux, else tmux.
With "none" the worktree path is printed on stdout so a shell function can cd
to it: wt() { local d; d="$(WT_MULTIPLEXER=none command wt "$@")" && cd "$d"; }

options:
  --codex       start Codex in the left pane
  --claude      start Claude in the left pane (default)
  --claude-account <name>  use a named Claude login (or default)
  --permission-mode auto  run Claude in its auto permission mode inside the sandbox,
                instead of bypassing its permission checks (Claude only)
  --docker      let the sandboxed agent use Docker (SAFE_ENABLE=docker for safehouse)
  --with <repo>[:<base>]  also check the branch out in ~/src/<repo> (or a clone path),
                new from <base> or that clone's default branch, and let the
                sandboxed agent write there (repeatable)
  --tag <name>  add a shared local focus tag (repeatable)
  --model <id>  override the model for a new session only
  --effort <level>  override reasoning effort for a new session only
  -r repo       use ~/src/<repo> instead of ~/src/galaxy
  -p, --prompt <text>       start the agent with this initial prompt
  -F, --prompt-file <path>  start the agent with this file's contents as the prompt
  --exit-when-done  have the dashboard exit the agent once it says the task is done
  --machine <id>|auto|local  start the agent on a worker of the dashboard's machine
                registry (auto: where the dashboard's Auto places it) instead of
                here; --docker alone means auto when the registry prefers a Docker
                worker. A worker takes only a new <branch> from <base> with a prompt
  -h, --help    show this help

Inside an agent's sandbox that cannot write the clone (a worker's babysit-bwrap, or
Safehouse on macOS without a grant for ~/src/<repo>) wt makes no worktree: it hands a
new <branch> from <base> with a prompt to the controller's dashboard, which places it
(auto unless --machine names a machine) and starts it sandboxed. There -r also takes
owner/name, which a sandbox that cannot read the clone needs; --docker needs a launch
that has Docker itself. Brokered sub-tasks keep the same agent and account. For an
explicitly requested cross-agent review, start a separate sandbox from the host
terminal with --claude --claude-account <account|default> or --codex. Host re-login
does not change the worker sandbox's isolated HOME or expose another agent's account.
On Linux, save reports under $BABYSIT_ARTIFACT_DIR (the persistent sandbox cache's
artifacts directory), or an explicitly granted worktree; other HOME paths are temporary.

dashboard options (a new branch from the remote default branch unless <base> is given):
  wt --name <branch> [--repo-path <clone>] [--worktree-root <dir>] [--label <label>]
     [--no-focus] [--agent-arg <word>]... [<base>]
  --agent-arg <word>      append one word to the agent command line (repeatable)
  --gpg                   let the sandboxed agent sign commits (babysit-bwrap --enable=gpg)
  --exit-watch-json       with --exit-when-done, print the exit watch as JSON instead of
                          recording it (a worker, for the dashboard that launched it)

The prompt only applies to a session being created; if one is already running for
the worktree it is reattached and the prompt is ignored (with a warning).

--exit-when-done needs a prompt and herdr. The prompt asks the agent to end its final
answer with a marker line, and a watch is recorded for the dashboard (in
$BABYSIT_HOME, default ~/.local/state/babysit-pr). Once the agent's first turn ends
with the marker and it stays idle, the dashboard exits it and closes its pane, or its
workspace when that pane was all it held. The worktree and branch stay. A question, a
later turn or no marker leaves the agent open. Nothing exits while no dashboard runs.

examples:
  # fire off a fix with a brief, no pasting
  wt -F /tmp/diagnosis.md release_26.1 workflow-copy-drops-readme
  wt -p 'Fix the failing tests in lib/galaxy/tools' dev my-branch
  wt --codex -p 'Review this PR for security issues' 22886
  # an unattended review the dashboard exits once it is done
  wt -F /tmp/review-brief.md --exit-when-done 22886
"""

WTI_HELP = """\
usage:
  wti [--codex|--claude] [-r repo] [--name name] [--no-focus] <issue-number|issue-url> [branch-name]
  wri [--codex|--claude] [-r repo] <issue-number|issue-url> [branch-name]
  wtissue [--codex|--claude] [-r repo] <issue-number|issue-url> [branch-name]
  wt [--codex|--claude] [-r repo] issue <issue-number|issue-url> [branch-name]

Create or open a worktree for a GitHub issue. If branch-name is omitted,
the branch is generated from the issue number and title (issue-<number>-<slug>).

options:
  --codex       start Codex in the left pane
  --claude      start Claude in the left pane (default)
  --claude-account <name>  use a named Claude login (or default)
  --permission-mode auto  run Claude in its auto permission mode inside the sandbox,
                instead of bypassing its permission checks (Claude only)
  --docker      let the sandboxed agent use Docker (SAFE_ENABLE=docker for safehouse)
  --with <repo>[:<base>]  also check the branch out in ~/src/<repo> (or a clone path),
                new from <base> or that clone's default branch, and let the
                sandboxed agent write there (repeatable)
  --tag <name>  add a shared local focus tag (repeatable)
  --model <id>  override the model for a new session only
  --effort <level>  override reasoning effort for a new session only
  -r repo       use ~/src/<repo> instead of the repo from the issue URL or ~/src/galaxy
  -p, --prompt <text>       start the agent with this initial prompt
  -F, --prompt-file <path>  start the agent with this file's contents as the prompt
  --exit-when-done  have the dashboard exit the agent once it says the task is done
                (see wt --help)
  --machine <id>|auto|local  only this machine: workers take new branches from wt
                (see wt --help), so auto runs here and a worker is refused
  --name <name>           reserve a new worktree and branch with this explicit name
  --label <label>         herdr workspace label (default: the branch name)
  --no-focus              leave native herdr focus unchanged
  --repo-path <path>      use this main clone (overrides -r location)
  --github-repo <o/r>     the GitHub repository it is a clone of: fetches from it get
                          the GitHub token (others only when typed in a terminal)
  --worktree-root <path>  parent directory for the new checkout
  --gpg                   let the sandboxed agent sign commits (babysit-bwrap --enable=gpg)
  --exit-watch-json       with --exit-when-done, print the exit watch as JSON instead of
                          recording it (a worker, for the dashboard that launched it)
  -h, --help    show this help
"""

WTPR_HELP = """\
usage:
  wtpr [--codex|--claude] [-r repo] [--name name] [--no-focus] <pr-number|pr-url>
  wt [--codex|--claude] [-r repo] <pr-number>

Create or open a worktree for a GitHub pull request. PRs from forks are
handled by fetching the PR branch from the contributor repository.

options:
  --codex       start Codex in the left pane
  --claude      start Claude in the left pane (default)
  --claude-account <name>  use a named Claude login (or default)
  --permission-mode auto  run Claude in its auto permission mode inside the sandbox,
                instead of bypassing its permission checks (Claude only)
  --docker      let the sandboxed agent use Docker (SAFE_ENABLE=docker for safehouse)
  --with <repo>[:<base>]  also check the branch out in ~/src/<repo> (or a clone path),
                new from <base> or that clone's default branch, and let the
                sandboxed agent write there (repeatable)
  --tag <name>  add a shared local focus tag (repeatable)
  --model <id>  override the model for a new session only
  --effort <level>  override reasoning effort for a new session only
  -r repo       use ~/src/<repo> instead of the repo from the PR URL or ~/src/galaxy
  -p, --prompt <text>       start the agent with this initial prompt
  -F, --prompt-file <path>  start the agent with this file's contents as the prompt
  --exit-when-done  have the dashboard exit the agent once it says the task is done
                (see wt --help)
  --machine <id>|auto|local  only this machine: workers take new branches from wt
                (see wt --help), so auto runs here and a worker is refused
  --name <name>           reserve a new worktree and branch with this explicit name
  --label <label>         herdr workspace label (default: the branch name)
  --no-focus              leave native herdr focus unchanged
  --repo-path <path>      use this main clone (overrides -r location)
  --github-repo <o/r>     the GitHub repository it is a clone of: fetches from it get
                          the GitHub token (others only when typed in a terminal)
  --worktree-root <path>  parent directory for the new checkout
  --gpg                   let the sandboxed agent sign commits (babysit-bwrap --enable=gpg)
  --exit-watch-json       with --exit-when-done, print the exit watch as JSON instead of
                          recording it (a worker, for the dashboard that launched it)
  -h, --help    show this help
"""

HELP = {"wt": WT_HELP, "wti": WTI_HELP, "wtpr": WTPR_HELP}


class WtError(Exception):
    """A user-facing failure; the message is already prefixed (``wt:``, ``wtpr:``…)."""


class UsageError(WtError):
    """A command-line mistake; the command's help follows the message on stderr."""

    def __init__(self, command: str, message: str):
        super().__init__(message)
        self.command = command


class NotForWorker(ValueError):
    """Why a run cannot start on a worker: Auto runs it here, a named worker refuses it."""


class HelpRequested(Exception):
    def __init__(self, command: str):
        super().__init__(command)
        self.command = command


class AlsoCheckout(NamedTuple):
    """A ``--with`` checkout: the clone, the branch's checkout in it and its git common dir."""

    clone: str
    path: str
    common: str


class Options:
    """Parsed command line shared by the three commands (``wt`` lacks the dashboard flags)."""

    __slots__ = (
        "agent",
        "docker",
        "repo",
        "repo_override",
        "prompt",
        "model",
        "effort",
        "claude_account",
        "permission_mode",
        "name",
        "label",
        "repo_path",
        "github_repo",
        "worktree_root",
        "focus",
        "agent_args",
        "also",
        "also_bases",
        "also_checkouts",
        "tags",
        "tag_item",
        "exit_when_done",
        "exit_watch_json",
        "gpg",
        "machine",
        "positional",
    )

    def __init__(self) -> None:
        self.agent = "claude"
        self.docker = False
        self.repo = DEFAULT_REPO
        self.repo_override = False
        self.prompt = ""
        self.model = ""
        self.effort = ""
        self.claude_account = ""
        self.permission_mode = ""
        self.name = ""
        self.label = ""
        self.repo_path = ""
        self.github_repo = ""
        self.worktree_root = ""
        self.focus = True
        self.agent_args: list[str] = []
        # `--with` clones, then the checkouts open_session makes in them for the agent.
        self.also: list[str] = []
        self.tags: list[str] = []
        self.tag_item = ""
        # A new branch's base in a `--with` clone (`--with <clone>:<base>`), by clone.
        self.also_bases: dict[str, str] = {}
        self.also_checkouts: list[AlsoCheckout] = []
        self.exit_when_done = False
        # On a worker: print the exit watch for the controller instead of recording it.
        self.exit_watch_json = False
        self.gpg = False
        # `--machine`: a worker's registry id, auto or local; empty when not given.
        self.machine = ""
        self.positional: list[str] = []


# --- pure planning helpers ---------------------------------------------------------


def read_prompt_file(path: str, command: str = "wt") -> str:
    """The contents of ``-F <path>`` without trailing newlines, like ``$(<file)``."""
    if not path:
        raise WtError(f"{command}: --prompt-file needs a path")
    try:
        with open(path, encoding="utf-8", errors="replace") as stream:
            return stream.read().rstrip("\n")
    except OSError:
        raise WtError(f"{command}: cannot read prompt file: {path}") from None


def fetch_source(
    remotes: Mapping[str, str | None], slug: str, trust: bool
) -> tuple[str, bool, str] | None:
    """Where a base is fetched from: the GitHub repository, whether it gets the token,
    and the remote whose refs it lands in.

    ``slug``, the repository this run was asked for, with the token; failing that,
    origin's repository, with the token only when ``trust`` says so. Either must be
    tracked by a remote; with neither, the base is a local branch.
    """
    for target, token in ((slug, True), (remotes.get("origin") or "", trust)):
        tracking = [
            n for n in ("origin", "upstream", *sorted(remotes)) if remotes.get(n) == target.lower()
        ]
        if target and tracking:
            return target, token, tracking[0]
    return None


def parse_args(
    command: str, argv: Sequence[str], read_file: Callable[[str, str], str] = read_prompt_file
) -> Options:
    """Parse one command's options; no process is started and nothing is validated yet."""
    allowed = OPTIONS[command]
    options = Options()
    args = list(argv)
    while args and args[0].startswith("-"):
        flag = args[0]
        if flag in ("-h", "--help"):
            raise HelpRequested(command)
        if flag not in allowed:
            raise UsageError(command, f"{command}: unknown option: {flag}")
        if flag == "--codex":
            options.agent = "codex"
        elif flag == "--claude":
            options.agent = "claude"
        elif flag == "--docker":
            options.docker = True
        elif flag == "--no-focus":
            options.focus = False
        elif flag == "--exit-when-done":
            options.exit_when_done = True
        elif flag == "--exit-watch-json":
            options.exit_watch_json = True
        elif flag == "--gpg":
            options.gpg = True
        else:
            if len(args) < 2 or not args[1]:
                raise WtError(f"{command}: {flag} needs a value")
            value = args[1]
            field = VALUE_OPTIONS[flag]
            if field == "prompt_file":
                options.prompt = read_file(value, command)
            elif field == "repo":
                options.repo = value
                options.repo_override = True
            elif field == "tags":
                try:
                    options.tags.append(work_tags.tag_name(value))
                except ValueError as exc:
                    raise WtError(f"{command}: {exc}") from exc
            elif field == "also":
                options.also.append(value)
            elif field == "github_repo":
                try:
                    safe_git.github_url(value)
                except ValueError as exc:
                    raise WtError(f"{command}: --github-repo: {exc}") from exc
                options.github_repo = value
            elif field == "agent_args":
                if any(ord(char) < 32 or char == "\x7f" for char in value):
                    raise WtError(f"{command}: --agent-arg must not contain control characters")
                options.agent_args.append(value)
            else:
                setattr(options, field, value)
            args.pop(0)
        args.pop(0)
    if len(options.tags) > 50:
        raise WtError(f"{command}: supply at most 50 tags")
    options.positional = args
    return options


def validate_agent_options(agent: str, model: str, effort: str, permission_mode: str = "") -> None:
    """Syntax only; model availability belongs to the agent CLI and its provider."""
    if model and not MODEL.fullmatch(model):
        raise WtError("wt: invalid model id")
    if permission_mode:
        if permission_mode not in PERMISSION_MODES:
            raise WtError("wt: unsupported permission mode; only auto is accepted")
        if agent != "claude":
            raise WtError(f"wt: --permission-mode {permission_mode} is for Claude only")
    if not effort:
        return
    if effort in CODEX_EFFORTS:
        if agent != "codex":
            raise WtError("wt: unsupported Claude effort")
    elif effort not in EFFORTS:
        raise WtError("wt: invalid reasoning effort")


def validate_name(name: str, command: str) -> None:
    if name and (not NAME.fullmatch(name) or ".." in name):
        raise WtError(f"{command}: --name must be a simple worktree name")


def dashboard_run(options: Options) -> bool:
    """Whether the dashboard runs this ``wt``: it always passes some dashboard option."""
    return bool(
        options.name
        or options.label
        or options.repo_path
        or options.github_repo
        or options.worktree_root
        or options.agent_args
        or options.gpg
        or options.exit_watch_json
        or not options.focus
    )


def worker_refusal(command: str, options: Options) -> str:
    """Why this run cannot start on a worker, or empty when it can.

    The dashboard starts a new task there: a new branch from a base, with a brief. Issues
    and pull requests it does not start on workers from here yet, nor other clones, whose
    paths here mean nothing there (the dashboard's New task maps them to the worker's),
    and ``wt <branch>`` opens a branch that exists.
    """
    first = options.positional[0] if options.positional else ""
    if command == "wti" or first in ("issue", "i"):
        return "workers do not take issue work yet"
    if command == "wtpr" or DIGITS.fullmatch(first) or parse_pr_url(first):
        return "wt starts only new branches on a worker; use Handle in the dashboard for a pull request"
    if options.also:
        return "wt hands no --with clones to a worker yet; choose the worker in the dashboard's New task"
    if not options.prompt:
        return NO_PROMPT
    if len(options.positional) < 2:
        return "a worker takes only a new branch from a base (wt <base> <branch>)"
    return ""


def parse_issue_url(value: str) -> tuple[str, str, str] | None:
    """``(owner, repo, number)`` for a GitHub issue URL, tolerating ``/comments`` or ``#…`` tails."""
    match = ISSUE_URL.search(value)
    return (match[1], match[2], match[3]) if match else None


def parse_pr_url(value: str) -> tuple[str, str, str] | None:
    match = PR_URL.search(value)
    return (match[1], match[2], match[3]) if match else None


def slug_from_remote_url(url: str) -> str:
    """``owner/repo`` from the common GitHub remote URL spellings."""
    slug = url.strip()
    for prefix in ("https://github.com/", "git@github.com:", "ssh://git@github.com/"):
        slug = slug.removeprefix(prefix)
    return slug.removesuffix("/").removesuffix(".git")


def parse_worktree_list(output: str) -> dict[str, str]:
    """``branch -> checkout path`` from ``git worktree list --porcelain``.

    The main clone is listed alongside the linked worktrees. Bare and detached
    entries carry no ``branch`` line and so contribute nothing.
    """
    checkouts: dict[str, str] = {}
    path = ""
    for line in output.splitlines():
        key, _, value = line.partition(" ")
        if key == "worktree":
            path = value
        elif key == "branch" and path:
            checkouts.setdefault(value.removeprefix("refs/heads/"), path)
        elif not key:
            path = ""
    return checkouts


def title_slug(title: str) -> str:
    """Lower-case, non-alphanumerics collapsed to ``-``, trimmed, at most 60 characters."""
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug[:60].rstrip("-")


def issue_branch(number: str, title: str) -> str:
    slug = title_slug(title)
    return f"issue-{number}-{slug}" if slug else f"issue-{number}"


def sanitize_session_name(name: str) -> str:
    """A tmux session / herdr label / cmux workspace name limited to ``[A-Za-z0-9_-]``."""
    return UNSAFE.sub("-", name)


# What the user's claude()/codex() shell functions add after ``safe``; a command that
# bypasses them must add it itself.
UNSANDBOXED_FLAGS = {
    "claude": "--dangerously-skip-permissions",
    "codex": "--dangerously-bypass-approvals-and-sandbox",
}


def agent_words(agent: str, model: str, effort: str, extra: Sequence[str] = ()) -> list[str]:
    words = [agent]
    if agent == "codex":
        # Messages are typed into the pane; one landing on the startup update dialog would
        # choose its default, an npm install. The dashboard offers updates instead.
        words += ["-c", CODEX_NO_UPDATE_CHECK]
    if model:
        words += ["--model", model]
    if effort:
        if agent == "codex":
            words += ["-c", f'model_reasoning_effort="{effort}"']
        else:
            words += ["--effort", effort]
    return [*words, *extra]


def stage_prompt(prompt: str, tmpdir: str | None = None) -> str:
    """Write the prompt to a private file the agent's shell reads back later.

    The file is deliberately left behind: the agent reads it after we return. It
    lives in ``$TMPDIR``, which the OS reaps.
    """
    import tempfile

    directory = tmpdir or os.environ.get("TMPDIR") or "/tmp"
    try:
        fd, path = tempfile.mkstemp(prefix="wt-prompt.", dir=directory)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(prompt + "\n")
    except OSError:
        raise WtError(f"wt: could not stage the prompt in {directory}") from None
    if any(ord(char) < 32 or char == "\x7f" for char in path):
        raise WtError("wt: refusing a staged prompt path with control characters")
    return path


def agent_command(
    agent: str,
    prompt: str = "",
    model: str = "",
    effort: str = "",
    stage: Callable[[str], str] = stage_prompt,
    extra: Sequence[str] = (),
    docker: bool = False,
    claude_config_dir: str | None = None,
    claude_subscription: bool = False,
    unsandboxed: bool = False,
    also: Sequence[AlsoCheckout] = (),
    directory: str | None = None,
    worktree: str | None = None,
    sandbox_config_dir: str | None = None,
    claude_config_env: str | None | bool = False,
    backend: str | None = None,
    features: Sequence[str] = (),
    launcher: str | None = None,
    confined: Sequence[str] | None = None,
    permission_mode: str | None = None,
) -> str:
    """The shell line that starts the agent, optionally with an initial prompt.

    Every multiplexer delivers this by *typing* it into a shell (tmux send-keys,
    cmux surface command, herdr pane run), which puts a hard ceiling on its length:
    canonical-mode tty input silently drops a line over MAX_CANON (1024 bytes on
    macOS). A 629-char line runs, a 2130-char one never executes -- so inlining the
    prompt would break exactly the long briefs ``-F`` exists for, and fail silently.

    So the prompt is staged in a file and the shell reads it back at execution time:
    ``agent … "$(cat '/path')"`` stays ~60 characters whatever the prompt's size, and
    command-substitution output is not re-scanned, so the content needs no escaping.
    Only the *path* is shell-quoted.

    The sandbox line itself comes from ``sandbox.typed_line``. On macOS it relies on the
    user's shell functions, which run the agent in Safehouse.

    ``docker`` prefixes ``SAFE_ENABLE=docker``: the shell's ``safe`` wrapper around the
    agent adds it to safehouse's ``--enable`` list, opening the Docker daemon socket.

    ``unsandboxed`` types ``command <agent>`` with the flag the wrapper would add, so the
    shell runs the agent binary directly instead of through ``safe``: no Safehouse, and
    no permission prompts to stall an unattended run. Only cron jobs that opt in pass
    it; ``wt`` itself never does, so agents it starts stay in Safehouse.

    ``permission_mode`` (Claude's ``--permission-mode``, ``wt --permission-mode``)
    replaces the bypass flag. The shell's ``claude`` function would add the flag, so on
    macOS the line calls the ``safe`` function it wraps instead, keeping Safehouse.

    ``also`` (``--with`` checkouts) appends Safehouse grants to ``SAFEHOUSE_ADD_DIRS``,
    which the shell already exports from safe.env: each checkout and its git common
    dir become writable, so the agent can commit there too. The list is staged in a
    file like the prompt. Each checkout is also an ``--add-dir`` for the agent itself,
    which a Docker restart reads back to keep the grants. ``stage`` is called once for
    the grants and once for the prompt, so it must return a new path each time.

    ``directory`` is the checkout the line will be typed in. Claude is told to trust it,
    in the configuration that Claude reads, so its folder trust dialog cannot hold up a
    launch nobody is watching. Without a selected account, that is the one this process
    inherited.

    ``worktree`` is the checkout the agent may write under babysit-bwrap on Linux, which
    is given it explicitly; it defaults to ``directory``. ``sandbox_config_dir`` is the
    Claude configuration it may write, for callers that set the account up themselves;
    it defaults to ``claude_config_dir``.

    A line for another machine names its own facts instead: ``backend`` (its
    operating system's sandbox), ``features`` beyond Docker (``gpg`` where it can sign),
    its babysit-bwrap ``launcher`` and ``confined`` options, and ``claude_config_env``,
    the ``CLAUDE_CONFIG_DIR`` to type (None: Claude's default there), as
    ``claude_accounts.shell_command`` takes it.
    """
    if unsandboxed and docker:
        raise ValueError("Docker access only applies inside Safehouse")
    if permission_mode and agent != "claude":
        raise ValueError("--permission-mode is for Claude only")
    if permission_mode and unsandboxed:
        raise ValueError("A permission mode only applies inside the sandbox")
    grants = [path for checkout in also for path in (checkout.path, checkout.common)]
    if any(":" in path for path in grants):
        # Safehouse splits its directory lists on colons.
        raise ValueError("A --with checkout path contains ':'")
    added = [word for checkout in also for word in ("--add-dir", checkout.path)]
    extra = [*added, *extra]
    command = sandbox.typed_line(
        agent_words(agent, model, effort, extra),
        sandbox.Grants(
            add_dirs=tuple(grants), features=(("docker",) if docker else ()) + tuple(features)
        ),
        stage,
        UNSANDBOXED_FLAGS[agent],
        "none" if unsandboxed else backend,
        worktree or directory,
        sandbox_config_dir or claude_config_dir,
        launcher,
        confined,
        permission_mode,
    )
    if prompt:
        # `--` stops variadic options from swallowing the prompt.
        separator = " --" if extra else ""
        command = f'{command}{separator} "$(cat {shlex.quote(stage(prompt))})"'
    if claude_config_dir:
        command = claude_accounts.shell_command(
            command,
            claude_config_dir,
            subscription=claude_subscription,
            config_env=claude_config_env,
            backend=backend,
        )
    if also and len(command.encode()) > TYPED_LIMIT:
        raise ValueError("The agent command is too long to type; use fewer --with clones")
    sandbox.typed(command)  # The staged paths and account prefix as well.
    if directory and agent == "claude":
        claude_accounts.trust(claude_config_dir or claude_accounts.current_home(), directory)
    return command


def read_config(path: str) -> dict[str, str]:
    """``KEY=VALUE`` lines (``export`` prefix, quotes and ``#`` comments allowed)."""
    try:
        with open(path, encoding="utf-8", errors="replace") as stream:
            lines = stream.read().splitlines()
    except OSError:
        return {}
    values: dict[str, str] = {}
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not CONFIG_KEY.fullmatch(key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def config_path(env: Mapping[str, str]) -> str:
    base = env.get("XDG_CONFIG_HOME") or os.path.join(env.get("HOME") or "~", ".config")
    return os.path.join(os.path.expanduser(base), "worktree", "config")


def effective_env(env: Mapping[str, str], config: Mapping[str, str]) -> dict[str, str]:
    """The process environment, with ``WT_*`` config keys filling in unset variables."""
    merged = {key: value for key, value in config.items() if key.startswith("WT_")}
    merged.update(env)
    return merged


def select_multiplexer(
    env: Mapping[str, str],
    which: Callable[[str], str | None],
    herdr_running: Callable[[], bool],
) -> str:
    """``$WT_MULTIPLEXER`` wins; ``auto`` prefers a running herdr, then cmux inside cmux,
    then tmux, then none."""
    chosen = env.get("WT_MULTIPLEXER") or "auto"
    if chosen != "auto":
        return chosen
    if which("herdr") and (env.get("HERDR_ENV") == "1" or herdr_running()):
        return "herdr"
    if env.get("CMUX_SURFACE_ID") and which("cmux"):
        return "cmux"
    if which("tmux"):
        return "tmux"
    return "none"


def herdr_is_running(output: str) -> bool:
    return HERDR_RUNNING.search(output) is not None


class HerdrOpen(NamedTuple):
    already_open: bool
    root_pane: str
    workspace_id: str = ""


def parse_herdr_open(output: str) -> HerdrOpen:
    """``already_open``, the root pane id and the workspace id from ``herdr worktree open``.

    A reply without the workspace names it in the root pane id, ``<workspace>:<pane>``.
    """
    try:
        reply = json.loads(output)
    except ValueError:
        raise WtError(f"wt: herdr returned no JSON: {output.strip()[:200]}") from None
    if not isinstance(reply, dict):
        raise WtError("wt: unexpected herdr reply")
    if reply.get("error"):
        raise WtError(f"wt: herdr worktree open failed: {herdr_error_message(output)}")
    result = reply.get("result")
    if not isinstance(result, dict):
        raise WtError("wt: unexpected herdr reply")
    root = result.get("root_pane")
    pane = root.get("pane_id") if isinstance(root, dict) else None
    pane = pane if isinstance(pane, str) else ""
    space = result.get("workspace")
    workspace = space.get("workspace_id") if isinstance(space, dict) else None
    if not isinstance(workspace, str) or not workspace:
        workspace = pane.split(":")[0] if ":" in pane else ""
    return HerdrOpen(result.get("already_open") is True, pane, workspace)


def herdr_error_message(output: str) -> str:
    """``.error.message`` when the output is JSON, else the raw text (``jq '.error.message // .'``)."""
    try:
        reply = json.loads(output)
    except ValueError:
        return output.strip()
    error = reply.get("error") if isinstance(reply, dict) else None
    if isinstance(error, dict) and error.get("message"):
        return str(error["message"])
    if isinstance(error, str) and error:
        return error
    return output.strip()


def cmux_workspace_ref(listing: str, directory: str) -> str:
    """The ref of the first cmux workspace whose ``current_directory`` is the worktree."""
    try:
        reply = json.loads(listing)
    except ValueError:
        return ""
    workspaces = reply.get("workspaces") if isinstance(reply, dict) else None
    for workspace in workspaces or []:
        if isinstance(workspace, dict) and workspace.get("current_directory") == directory:
            ref = workspace.get("ref")
            if isinstance(ref, str) and ref:
                return ref
    return ""


def cmux_group_ref(listing: str, name: str) -> str:
    try:
        reply = json.loads(listing)
    except ValueError:
        return ""
    groups = reply.get("groups") if isinstance(reply, dict) else None
    for group in groups or []:
        if isinstance(group, dict) and group.get("name") == name:
            ref = group.get("ref")
            if isinstance(ref, str) and ref:
                return ref
    return ""


def cmux_created_group_ref(output: str) -> str:
    try:
        reply = json.loads(output)
    except ValueError:
        return ""
    group = reply.get("group") if isinstance(reply, dict) else None
    ref = group.get("ref") if isinstance(group, dict) else None
    return ref if isinstance(ref, str) else ""


def cmux_layout(command: str) -> str:
    """Left pane runs the agent, right pane is a spare terminal; the command is JSON data."""
    layout = {
        "direction": "horizontal",
        "split": 0.5,
        "children": [
            {"pane": {"surfaces": [{"type": "terminal", "command": command}]}},
            {"pane": {"surfaces": [{"type": "terminal"}]}},
        ],
    }
    return json.dumps(layout, separators=(",", ":"))


# --- process seam ---------------------------------------------------------------------


class Completed(NamedTuple):
    returncode: int
    stdout: str
    stderr: str


class Runner(Protocol):
    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        output: str = "capture",
    ) -> Completed: ...

    def which(self, name: str) -> str | None: ...


def find_executable(name: str, env: Mapping[str, str]) -> str | None:
    for directory in (env.get("PATH") or "").split(os.pathsep):
        candidate = os.path.join(directory, name) if directory else name
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


class SubprocessRunner:
    """Real processes. ``output`` is ``capture`` (both streams returned), ``stderr``
    (both streams to our stderr, keeping stdout clean for the ``none`` mode path) or
    ``inherit`` (the child owns the terminal, for ``tmux attach-session``)."""

    def __init__(self, env: Mapping[str, str]):
        self.env = env

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        output: str = "capture",
    ) -> Completed:
        import subprocess

        stream: int | None = None
        if output == "capture":
            stream = subprocess.PIPE
        elif output == "stderr":
            sys.stdout.flush()
            stream = 2
        try:
            proc = subprocess.run(
                list(argv),
                cwd=cwd,
                env=dict(env) if env is not None else None,
                text=True,
                errors="replace",
                stdout=stream,
                stderr=stream,
            )
        except FileNotFoundError:
            return Completed(127, "", f"{argv[0]}: command not found")
        return Completed(proc.returncode, proc.stdout or "", proc.stderr or "")

    def which(self, name: str) -> str | None:
        return find_executable(name, self.env)


# --- launches on worker machines ----------------------------------------------------


def launch_request(command: str, options: Options, repo: str, machine: str) -> dict[str, object]:
    """A worker launch: the required LAUNCH_FIELDS and an optional list of tags.

    Every field is checked here as the controller checks it, so any transport (the local
    dashboard's API, or a sandbox's broker) carries one shape: ``repo`` (``owner/name``),
    ``base`` and ``name`` (the new branch: ``options.positional``), ``agent``, ``model``,
    ``effort``, ``claude_account``, ``task`` (the brief), ``docker``, ``exit_when_done``,
    ``permission_mode`` (empty or ``auto``), ``machine`` (a registry id or ``auto``),
    and ``tags`` when supplied.
    A field a worker launch cannot take raises NotForWorker; agent options that are
    wrong anywhere raise WtError.
    """
    import remote_probe  # The branch names a worker's probe takes, as New task checks them.

    validate_agent_options(options.agent, options.model, options.effort, options.permission_mode)
    if options.agent not in ("codex", "claude"):
        raise WtError(f"{command}: unknown agent {options.agent}")
    if len(options.positional) < 2:
        raise NotForWorker("a worker takes only a new branch from a base (wt <base> <branch>)")
    base, name = options.positional[:2]
    checks = [
        (REPO_SLUG.fullmatch(repo) and repo.split("/")[1] not in (".", ".."), "repository"),
        (
            LAUNCH_BRANCH.fullmatch(name) and ".." not in name,
            "new branch for a worker (letters, digits, '.', '_' and '-')",
        ),
        (remote_probe.BRANCH.fullmatch(base) and ".." not in base, "base branch for a worker"),
        (
            not options.claude_account or claude_accounts.NAME.fullmatch(options.claude_account),
            "Claude account for a worker",
        ),
        (
            options.prompt.strip() and len(options.prompt) <= 32000 and "\0" not in options.prompt,
            "brief for a worker (1-32,000 characters)",
        ),
        (machine == "auto" or MACHINE.fullmatch(machine), "machine"),
    ]
    for ok, field in checks:
        if not ok:
            raise NotForWorker(f"not a valid {field}")
    return {
        "repo": repo,
        "base": base,
        "name": name,
        "agent": options.agent,
        "model": options.model,
        "effort": options.effort,
        "claude_account": options.claude_account,
        "task": options.prompt,
        "docker": options.docker,
        "exit_when_done": options.exit_when_done,
        "permission_mode": options.permission_mode,
        "machine": machine,
        **({"tags": options.tags} if options.tags else {}),
    }


class DashboardTransport:
    """A launch through this machine's dashboard API: ``$BABYSIT_DASHBOARD_URL`` or
    127.0.0.1:8765, reachable unsandboxed and from inside Safehouse.

    A transport offers ``place`` (where Auto would put a ``launch_request``), ``start``
    (the New task's operation) and ``status`` (that operation now). With ``parent``
    (what a run inside Safehouse says about its launch) ``start`` asks for a sub-task,
    which the dashboard places itself, under its sub-task rules.
    """

    def __init__(self, tool: Tool, command: str, parent: dict[str, object] | None = None):
        self.command = command
        self.base = (tool.env.get("BABYSIT_DASHBOARD_URL") or DASHBOARD).rstrip("/")
        self.parent = parent

    def place(self, request: Mapping[str, object]) -> dict[str, object]:
        import urllib.parse

        query = {"kind": "scratch", "repo": request["repo"], "agent": request["agent"]}
        if request["docker"]:
            query["docker"] = "1"
        if request["agent"] == "claude" and request["claude_account"]:
            query["account"] = request["claude_account"]
        return self.call("/api/placement?" + urllib.parse.urlencode(query))

    def start(self, request: Mapping[str, object]) -> dict[str, object]:
        if self.parent is None:
            answer = self.call("/api/workspace-new", dict(request))
        else:
            answer = self.call(
                "/api/workspace-subtask", {"launch": dict(request), "parent": self.parent}
            )
        op = answer.get("operation")
        if not isinstance(op, dict) or not isinstance(op.get("pr"), str):
            raise WtError(
                f"{self.command}: the dashboard answered the launch unexpectedly; it may "
                "have started, so check the dashboard before retrying"
            )
        return op

    def status(self, op: Mapping[str, object]) -> dict[str, object] | None:
        """The operation now, None once the dashboard no longer lists it."""
        listed = self.call("/api/workspaces").get("new") or {}
        found = (listed.get(op["pr"]) or {}).get("operation")
        return found if isinstance(found, dict) else None

    def call(self, path: str, body: dict[str, object] | None = None):
        """One JSON request to the dashboard; refusals carry its reason."""
        import urllib.error
        import urllib.request

        headers = {}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            # The dashboard's own guard against browsers: a custom header and no Origin.
            headers = {
                "Content-Type": "application/json",
                "X-Babysit-Action": path.rsplit("/", 1)[1],
            }
        request = urllib.request.Request(self.base + path, data=data, headers=headers)
        # No proxy from the environment: the dashboard is on this machine.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=120) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            try:
                detail = json.load(exc).get("error")
            except (ValueError, AttributeError):
                detail = None
            raise WtError(
                f"{self.command}: the dashboard refused: {detail or f'HTTP {exc.code}'}"
            ) from None
        except (OSError, ValueError) as exc:
            # urllib wraps what fails while connecting and sending in URLError; anything
            # after (a read timeout, a reset, an answer that is not JSON) may follow a
            # launch the dashboard already took.
            if body is not None and not isinstance(exc, urllib.error.URLError):
                raise WtError(
                    f"{self.command}: the launch may have started ({exc or type(exc).__name__}); "
                    "check the dashboard before retrying"
                ) from None
            reason = getattr(exc, "reason", None) or exc
            hint = (
                "start it, or pass --machine local to run here"
                if self.parent is None
                else "nothing was started"
            )
            raise WtError(
                f"{self.command}: cannot reach the dashboard at {self.base} ({reason}), which "
                f"starts launches on workers; {hint}"
            ) from None


class BrokerTransport:
    """A launch from inside a worker launch's babysit-bwrap, through that launch's broker
    (``$BABYSIT_REGISTER_SOCKET``, see worker_broker).

    The broker adds what the sandbox cannot forge (the launch's directory, its Docker
    grant, the transcript of the conversation asking) and the controller's
    ``/api/worker/launch`` places and starts the task, answering once it is ready or a
    few minutes on. Nothing follows it after that answer (``status`` is None).
    """

    status = None

    def __init__(self, tool: Tool, command: str):
        self.command = command
        self.socket = tool.env.get("BABYSIT_REGISTER_SOCKET") or ""
        self.sessions = {agent: tool.env.get(name) or None for agent, name in SESSION_ENV}

    def start(self, request: Mapping[str, object]) -> dict[str, object]:
        import worker_api

        if not os.path.exists(self.socket):
            raise WtError(f"{self.command}: this sandbox's broker is gone; nothing was started")
        try:
            answer = worker_api.forward(
                "launch",
                {"launch": dict(request), "sessions": self.sessions},
                path=self.socket,
            )
        except ValueError as exc:
            raise WtError(f"{self.command}: the controller refused: {exc}") from None
        except RuntimeError as exc:
            raise WtError(
                f"{self.command}: the launch may have started ({exc}); do not start it again"
            ) from None
        op = answer.get("operation")
        if not isinstance(op, dict) or not isinstance(op.get("status"), str):
            raise WtError(
                f"{self.command}: the controller answered the launch unexpectedly; it may "
                "have started, so do not start it again"
            )
        return op


# --- the tool ------------------------------------------------------------------------


class Tool:
    """One invocation's environment: process runner, env, streams and prompt staging."""

    def __init__(
        self,
        runner: Runner,
        env: Mapping[str, str],
        *,
        stdout: TextIO | None = None,
        stderr: TextIO | None = None,
        isatty: Callable[[], bool] | None = None,
        stage: Callable[[str], str] | None = None,
        confined: Callable[[], bool] | None = None,
    ):
        self.runner = runner
        self.confined = confined or sandbox.confined
        self.writable = sandbox.writable
        self.docker_granted = sandbox.docker_granted
        self.env = dict(env)
        self.stdout = stdout or sys.stdout
        self.stderr = stderr or sys.stderr
        self.isatty = isatty or (lambda: sys.stdin is not None and sys.stdin.isatty())
        self.stage = stage or stage_prompt
        self.home = self.env.get("HOME") or os.path.expanduser("~")
        # The dashboard's state home, where --exit-when-done records its watch.
        self.babysit_home = self.env.get("BABYSIT_HOME") or os.path.join(
            self.home, ".local", "state", "babysit-pr"
        )

    # -- small process helpers --

    def warn(self, message: str) -> None:
        print(message, file=self.stderr, flush=True)

    def git_command(
        self, cwd: str, *args: str, credentials: bool = True
    ) -> tuple[list[str], dict[str, str]]:
        """Hardened Git: the clone's config and hooks are writable by its agents."""

        def read(argv: list[str], env: Mapping[str, str]) -> str:
            result = self.runner.run(argv, env=env)
            if result.returncode:
                raise ValueError(result.stderr.strip() or "git config failed")
            return result.stdout

        try:
            return safe_git.prepare(cwd, *args, read=read, credentials=credentials)
        except ValueError as exc:
            raise WtError(str(exc)) from exc

    def git_query(self, cwd: str, *args: str, credentials: bool = True) -> Completed:
        argv, env = self.git_command(cwd, *args, credentials=credentials)
        return self.runner.run(argv, env=env)

    def git_value(self, cwd: str, *args: str) -> str:
        result = self.git_query(cwd, *args)
        if result.returncode:
            raise WtError(result.stderr.strip() or f"git {' '.join(args)} failed")
        return result.stdout.strip()

    def git_action(self, cwd: str, *args: str, credentials: bool = True) -> None:
        """Mutating git commands keep their own output (progress, hints) on stderr."""
        argv, env = self.git_command(cwd, *args, credentials=credentials)
        result = self.runner.run(argv, env=env, output="stderr")
        if result.returncode:
            raise WtError(f"git {args[0]} failed with status {result.returncode}")

    def branch_exists(self, repo_path: str, branch: str) -> bool:
        return not self.git_query(
            repo_path, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"
        ).returncode

    def repo_slug(self, repo_path: str, command: str) -> str:
        """``owner/repo`` from ``remote.upstream.url``, else ``remote.origin.url``."""
        for remote in ("upstream", "origin"):
            result = self.git_query(repo_path, "config", "--get", f"remote.{remote}.url")
            if not result.returncode and result.stdout.strip():
                return slug_from_remote_url(result.stdout)
        raise WtError(f"{command}: no upstream or origin remote in {repo_path}")

    def gh_json(self, command: str, slug: str, *args: str) -> dict[str, object]:
        result = self.runner.run(["gh", "-R", slug, *args])
        if result.returncode:
            detail = result.stderr.strip() or result.stdout.strip()
            raise WtError(f"{command}: gh {args[0]} {args[1]} failed: {detail}".rstrip(": "))
        try:
            value = json.loads(result.stdout)
        except ValueError:
            value = None
        if not isinstance(value, dict):
            raise WtError(f"{command}: unexpected gh reply")
        return value

    def existing_checkout(self, repo_path: str, branch: str) -> str:
        """Where ``branch`` is already checked out, if anywhere: the main clone or a worktree."""
        result = self.git_query(repo_path, "worktree", "list", "--porcelain")
        if result.returncode:
            return ""
        path = parse_worktree_list(result.stdout).get(branch, "")
        return path if path and os.path.isdir(path) else ""

    def remote_slugs(self, repo_path: str) -> dict[str, str | None]:
        """Each remote's GitHub repository as the clone's config (agent-writable) names it."""
        result = self.git_query(repo_path, "config", "--get-regexp", r"^remote\..*\.url$")
        found: dict[str, str | None] = {}
        for line in result.stdout.splitlines() if not result.returncode else []:
            key, _, url = line.partition(" ")
            found[key[len("remote.") : -len(".url")]] = safe_git.github_slug(url)
        return found

    def trusts_origin(self, options: Options) -> bool:
        """Whether origin's GitHub repository, as the clone's config names it, gets the token.

        Only for the user typing the command in a terminal: a dashboard launch names its
        repository (``--github-repo``) and runs with ``BABYSIT_HOME`` set, whatever its
        stdin is, and any other run without a terminal keeps the strict rule.
        """
        return not options.github_repo and "BABYSIT_HOME" not in self.env and self.isatty()

    def add_worktree(
        self,
        repo_path: str,
        directory: str,
        branch: str,
        base: str,
        slug: str = "",
        trust: bool = False,
    ) -> None:
        """Reuse a local branch, else create from the remote or local base.

        The base is fetched from a GitHub URL, never a remote's own (the agent writes
        those): from ``slug``, the repository this run was asked for, with the GitHub
        token, into the refs of the remote that tracks it; failing that from origin's
        GitHub repository, with the token only when ``trust`` says so (see
        ``trusts_origin``). Anything else starts from the local base.
        """
        os.makedirs(os.path.dirname(directory), exist_ok=True)
        remotes = self.remote_slugs(repo_path)
        start_point = base
        source = fetch_source(remotes, slug, trust)
        if source:
            target, token, remote = source
            try:
                self.git_action(
                    repo_path,
                    "fetch",
                    safe_git.github_url(target),
                    f"+refs/heads/{base}:refs/remotes/{remote}/{base}",
                    credentials=token,
                )
            except WtError:
                self.missing_base(repo_path, target, base, token)
                raise
            # In full, so no local branch or tag named <remote>/<base> wins.
            start_point = f"refs/remotes/{remote}/{base}"
        elif "origin" in remotes:
            self.warn(f"wt: origin is not a GitHub repository; starting from the local {base}")
        if self.branch_exists(repo_path, branch):
            self.checkout_worktree(repo_path, directory, branch)
        else:
            self.checkout_worktree(repo_path, directory, start_point, new_branch=branch)

    def missing_base(self, repo_path: str, target: str, base: str, token: bool) -> None:
        """After a failed fetch of ``base``: refuse it by name when GitHub has no such branch.

        GitHub is asked once more, as the fetch was, for the branch and its default
        branch; if it cannot be asked, the fetch's own failure stands.
        """
        listed = self.git_query(
            repo_path,
            "ls-remote",
            "--symref",
            safe_git.github_url(target),
            "HEAD",
            f"refs/heads/{base}",
            credentials=token,
        )
        if listed.returncode:
            return
        refs = [line.split("\t") for line in listed.stdout.splitlines()]
        if any(ref[-1] == f"refs/heads/{base}" for ref in refs):
            return
        default = next(
            (
                ref[0].removeprefix("ref: refs/heads/")
                for ref in refs
                if ref[0].startswith("ref: refs/heads/") and ref[-1] == "HEAD"
            ),
            "",
        )
        hint = f"; its default branch is {default}" if default else ""
        raise WtError(f"wt: {target} has no branch {base}{hint}")

    def checkout_worktree(
        self, repo_path: str, directory: str, commit: str, new_branch: str = ""
    ) -> None:
        """``git worktree add``, with the files checked out from inside the new worktree.

        Its branch and git dir can bring config the clone's own listing never sees, so
        the files are written by a reset that lists config where it runs.
        """
        create = ["-b", new_branch] if new_branch else []
        self.git_action(
            repo_path,
            "worktree",
            "add",
            "--no-checkout",
            *create,
            "--end-of-options",
            directory,
            commit,
        )
        try:
            self.git_action(directory, "reset", "--hard", "--quiet")
        except Exception:
            # Left behind, the empty checkout would be reused with every file deleted.
            self.git_action(repo_path, "worktree", "remove", "--force", directory)
            if new_branch:
                self.git_action(repo_path, "branch", "-D", new_branch)
            raise

    def default_branch(self, repo_path: str, slug: str = "", trust: bool = False) -> str:
        """The branch a new one starts from when no base is named.

        The ``HEAD`` of the remote the base is fetched into, which ``git clone``
        records but ``git remote add`` does not; else the default GitHub reports for
        that repository, asked only when its fetch would get the token (see
        ``add_worktree``); else origin's ``HEAD``, a fork's when origin is not the
        remote fetched into; else whichever of ``main`` and ``master`` those remotes,
        or failing them the clone itself, committed to last; else ``main``.
        """
        remotes = self.remote_slugs(repo_path)
        source = fetch_source(remotes, slug, trust)
        remote = source[2] if source else "origin"
        branch = self.remote_head(repo_path, remote)
        if not branch and source and source[1]:
            reply = self.runner.run(["gh", "api", f"repos/{source[0]}", "--jq", ".default_branch"])
            if not reply.returncode and self.valid_branch(repo_path, reply.stdout.strip()):
                branch = reply.stdout.strip()
        if not branch and remote != "origin":
            branch = self.remote_head(repo_path, "origin")
        if branch:
            return branch
        for prefix in dict.fromkeys(
            (f"refs/remotes/{remote}/", "refs/remotes/origin/", "refs/heads/")
        ):
            newest = self.git_query(
                repo_path,
                "for-each-ref",
                "--sort=-committerdate",
                "--count=1",
                "--format=%(refname)",
                f"{prefix}main",
                f"{prefix}master",
            )
            if not newest.returncode and newest.stdout.strip():
                branch = newest.stdout.strip().removeprefix(prefix)
                hint = f" (git remote set-head {remote} --auto records it)"
                self.warn(
                    f"wt: {repo_path} records no default branch; starting from {branch}"
                    + (hint if remote in remotes else "")
                )
                return branch
        return "main"

    def remote_head(self, repo_path: str, remote: str) -> str:
        """The branch ``refs/remotes/<remote>/HEAD`` names, if it names one.

        The agent writes the clone's refs, so the target is checked like any other
        name that reaches a refspec or Git's command line.
        """
        prefix = f"refs/remotes/{remote}/"
        head = self.git_query(repo_path, "symbolic-ref", "--quiet", f"{prefix}HEAD")
        target = head.stdout.strip()
        branch = target.removeprefix(prefix)
        if head.returncode or not target.startswith(prefix):
            return ""
        return branch if self.valid_branch(repo_path, branch) else ""

    def valid_branch(self, repo_path: str, branch: str) -> bool:
        """A branch name a fetch refspec can carry: never an option or a revision."""
        return (
            bool(branch)
            and not branch.startswith("-")
            and "@" not in branch
            and not self.git_query(repo_path, "check-ref-format", "--branch", branch).returncode
        )

    def also_clone(self, value: str, command: str) -> str:
        """A ``--with`` value: a repository under ``~/src`` or a clone path."""
        expanded = os.path.expanduser(value)
        if os.sep in value or value.startswith("~"):
            path = os.path.abspath(expanded)
        else:
            if not NAME.fullmatch(value) or ".." in value:
                raise WtError(f"{command}: --with needs a repository name or a clone path")
            path = os.path.join(self.home, "src", value)
        if not os.path.isdir(os.path.join(path, ".git")):
            raise WtError(f"{command}: --with: no main clone at {path}")
        return os.path.realpath(path)

    def add_also(self, options: Options, branch: str, repo_path: str) -> None:
        """Check ``branch`` out in each ``--with`` clone, next to the main worktree root.

        A worktree that already holds the branch is used where it is, like ``wt``; a
        local branch is reused, else the branch starts from the base ``--with`` named for
        the clone, or from its default branch.
        Every clone is checked before any checkout is made, and a clone's own working
        tree is never handed to the agent.
        """
        if options.also_checkouts:
            return
        root = os.path.dirname(options.worktree_root) if options.worktree_root else ""
        root = root or os.path.join(self.home, "src", "worktrees")
        main = os.path.realpath(repo_path)
        trust = self.trusts_origin(options)
        planned: list[tuple[AlsoCheckout, str]] = []
        for clone in dict.fromkeys(options.also):
            named = options.also_bases.get(clone)
            if clone == main:
                if named:
                    self.warn(f"wt: {clone} is the task's own clone; ignoring the base {named}")
                continue
            directory = self.existing_checkout(clone, branch)
            if directory and os.path.realpath(directory) == clone:
                raise WtError(
                    f"wt: {branch} is checked out in {clone} itself; "
                    "switch that clone to another branch first"
                )
            create = not directory
            if create:
                directory = os.path.join(root, os.path.basename(clone), branch)
                if os.path.lexists(directory) or directory in (c.path for c, _ in planned):
                    raise WtError(f"wt: {directory} exists but does not have {branch} checked out")
            # Granted to the agent: never what an agent-written commondir file names.
            try:
                common = safe_git.common_dir(clone)
            except ValueError as exc:
                raise WtError(f"wt: --with: {exc}") from exc
            checkout = AlsoCheckout(clone, directory, common)
            if ":" in checkout.path + checkout.common:
                raise WtError(f"wt: Safehouse cannot grant a path containing ':': {directory}")
            if named and (not create or self.branch_exists(clone, branch)):
                self.warn(f"wt: {branch} already exists in {clone}; ignoring the base {named}")
            base = (named or self.default_branch(clone, trust=trust)) if create else ""
            planned.append((checkout, base))
        # The typed line must fit before anything is made; staged paths look like this.
        sample = os.path.join(self.env.get("TMPDIR") or "/tmp", "wt-prompt.XXXXXXXX")
        trial = Options()
        for slot in Options.__slots__:
            setattr(trial, slot, getattr(options, slot))
        trial.also_checkouts = [checkout for checkout, _ in planned]
        self.command_for(trial, lambda _: sample)
        for checkout, base in planned:
            if base:
                self.add_worktree(checkout.clone, checkout.path, branch, base, trust=trust)
            else:
                self.warn(f"wt: {branch} is already checked out at {checkout.path}; using that")
            options.also_checkouts.append(checkout._replace(path=os.path.realpath(checkout.path)))
        if options.prompt and options.also_checkouts:
            listed = "\n".join(f"- {c.path}" for c in options.also_checkouts)
            options.prompt += (
                f"\n\nThis task also covers other repositories. Branch {branch} is checked out"
                f" in each of these, and you can write and commit there:\n{listed}"
            )

    # -- multiplexers --

    def herdr_running(self) -> bool:
        result = self.runner.run(["herdr", "status", "server", "--json"])
        return not result.returncode and herdr_is_running(result.stdout)

    def multiplexer(self) -> str:
        return select_multiplexer(self.env, self.runner.which, self.herdr_running)

    def command_for(
        self,
        options: Options,
        stage: Callable[[str], str] | None = None,
        directory: str | None = None,
    ) -> str:
        try:
            home = claude_accounts.validate(options.agent, options.claude_account)
        except ValueError as exc:
            raise WtError(f"wt: {exc}") from exc
        try:
            return agent_command(
                options.agent,
                options.prompt,
                options.model,
                options.effort,
                stage or self.stage,
                options.agent_args,
                options.docker,
                str(home) if home else None,
                bool(options.claude_account),
                also=options.also_checkouts,
                directory=directory,
                features=("gpg",) if options.gpg else (),
                permission_mode=options.permission_mode or None,
            )
        except ValueError as exc:
            raise WtError(f"wt: {exc}") from exc

    def open_session(self, options: Options, directory: str, name: str, repo_path: str) -> None:
        """Left pane runs the agent, right pane is a spare terminal (herdr: the agent only).

        The prompt only applies to a session being created; an existing one already
        has an agent in it, so reattaching warns and ignores the prompt.
        """
        self.add_also(options, name, repo_path)
        if options.tags:
            try:
                tags = work_tags.Tags(Path(self.babysit_home))
                target = work_tags.cli_target(directory)
                if options.tag_item:
                    tags.link(target, options.tag_item)
                tags.change({"target": target, "add": options.tags})
                for checkout in options.also_checkouts:
                    tags.change(
                        {"target": work_tags.cli_target(checkout.path), "add": options.tags}
                    )
            except (OSError, ValueError, sqlite3.Error) as exc:
                raise WtError(f"wt: cannot save tags: {exc}") from exc
        mux = self.multiplexer()
        if mux in ("herdr", "tmux", "cmux") and not self.runner.which(mux):
            self.warn(f"wt: {mux} is not on PATH; opening nothing")
            mux = "none"
        if mux == "herdr":
            self.session_herdr(options, directory, name, repo_path)
        elif mux == "tmux":
            self.session_tmux(options, directory, name)
        elif mux == "cmux":
            self.session_cmux(options, directory, name)
        else:
            if mux != "none":
                self.warn(f"wt: unknown WT_MULTIPLEXER '{mux}'; treating it as none")
            if options.prompt:
                self.warn("wt: no multiplexer (WT_MULTIPLEXER=none); ignoring the prompt")
            # A subprocess cannot cd its caller: hand the path back on stdout instead.
            print(directory, file=self.stdout, flush=True)

    def warn_reattached(self, options: Options) -> None:
        if options.also_checkouts:
            self.warn(
                "wt: the running agent keeps its sandbox; restart it to write to --with checkouts"
            )

    def session_herdr(self, options: Options, directory: str, name: str, repo_path: str) -> None:
        # herdr models a worktree as a workspace with checkout provenance and groups it
        # under a workspace for the parent checkout, which it creates on first use. The
        # open action must be issued *from* that parent (--cwd), not from the worktree.
        # The worktree already exists on disk, so open rather than create; open is
        # idempotent and reports already_open.
        focus = "--focus" if options.focus else "--no-focus"
        argv = ["herdr", "worktree", "open", "--cwd", repo_path, "--path", directory]
        argv += ["--label", sanitize_session_name(options.label or name), focus]
        result = self.runner.run(argv)
        if result.returncode:
            detail = herdr_error_message(result.stdout + result.stderr)
            raise WtError(f"wt: herdr worktree open failed: {detail}")
        reply = parse_herdr_open(result.stdout)
        if reply.already_open:
            # Existing workspaces retain their agents; focus follows the explicit option.
            if options.prompt:
                also = " and --exit-when-done" if options.exit_when_done else ""
                self.warn(
                    f"wt: a herdr workspace for {directory} already exists; "
                    f"ignoring the prompt{also}"
                )
            self.warn_reattached(options)
            return
        if not reply.root_pane:
            raise WtError(f"wt: herdr did not return a root pane for {directory}")
        watch, marker = None, ""
        if options.exit_when_done:
            import exit_watch

            if not reply.workspace_id:
                raise WtError(f"wt: herdr did not name the workspace for {directory}")
            marker = exit_watch.new_marker()
            # Last, after any --with note: the brief asks for the marker as the final line.
            options.prompt += exit_watch.brief(marker)
        command = self.command_for(options, directory=directory)
        if options.exit_when_done and not options.exit_watch_json:
            # Recorded before the command is typed, so no agent waits on an unsaved watch.
            watch = self.watch_exit(options, directory, name, reply, marker)
        # No spare terminal split here: reviewr's auto_open is off, so the workspace
        # holds just the agent; open reviewr with its toggle action when wanted.
        # pane run submits text + Enter; the pty buffers it until the new shell reads it.
        try:
            result = self.runner.run(["herdr", "pane", "run", reply.root_pane, command])
        except BaseException:
            self.forget_watch(watch)
            raise
        if result.returncode:
            self.forget_watch(watch)
            raise WtError(f"wt: herdr pane run failed: {result.stderr.strip()}".rstrip(": "))
        if options.exit_when_done and options.exit_watch_json:
            # Only for an agent that was typed: the controller records it once wt returns.
            print(
                json.dumps(self.exit_record(options, directory, name, reply, marker)),
                file=self.stdout,
                flush=True,
            )

    def forget_watch(self, watch: str | None) -> None:
        if watch:
            import exit_watch

            try:
                exit_watch.forget(self.babysit_home, watch)
            except OSError:
                pass  # The dashboard leaves it once no agent shows up in the pane.

    def watch_exit(
        self, options: Options, directory: str, name: str, reply: HerdrOpen, marker: str
    ) -> str:
        """Record the exit watch the dashboard follows for the agent about to be typed."""
        import exit_watch

        watch = self.exit_record(options, directory, name, reply, marker)
        try:
            exit_watch.register(self.babysit_home, watch)
        except OSError as exc:
            raise WtError(
                f"wt: could not record the exit watch in {self.babysit_home}: {exc}"
            ) from None
        return str(watch["id"])

    def exit_record(
        self, options: Options, directory: str, name: str, reply: HerdrOpen, marker: str
    ) -> dict[str, object]:
        import time
        import uuid

        import exit_watch

        now = time.time()
        return {
            "id": uuid.uuid4().hex,
            "label": sanitize_session_name(options.label or name),
            "path": os.path.realpath(directory),
            "workspace_id": reply.workspace_id,
            "pane_id": reply.root_pane,
            "agent": options.agent,
            "claude_account": options.claude_account or None,
            "marker": marker,
            "created_at": now,
            "exit": exit_watch.watching(now),
        }

    def session_tmux(self, options: Options, directory: str, name: str) -> None:
        session = sanitize_session_name(name)
        if self.runner.run(["tmux", "has-session", "-t", f"={session}"]).returncode:
            command = self.command_for(options, directory=directory)
            for step in (
                ["new-session", "-d", "-s", session, "-c", directory],
                ["split-window", "-h", "-t", session, "-c", directory],
                ["select-pane", "-t", session, "-L"],
                ["send-keys", "-t", session, command, "C-m"],
            ):
                result = self.runner.run(["tmux", *step])
                if result.returncode:
                    raise WtError(
                        f"wt: tmux {step[0]} failed: {result.stderr.strip()}".rstrip(": ")
                    )
        else:
            if options.prompt:
                self.warn(f"wt: tmux session '{session}' already running; ignoring the prompt")
            self.warn_reattached(options)
        if self.env.get("TMUX"):
            self.runner.run(["tmux", "switch-client", "-t", session], output="inherit")
        elif self.isatty():
            self.runner.run(["tmux", "attach-session", "-t", session], output="inherit")
        else:
            # No terminal to attach from (e.g. called from an agent's shell tool). The
            # session is running detached; say where instead of failing the whole call.
            self.warn(
                f"wt: tmux session '{session}' started detached; "
                f"attach with: tmux attach-session -t {session}"
            )

    def cmux(self, *args: str) -> Completed:
        return self.runner.run(["cmux", *args], env={**self.env, "CMUX_QUIET": "1"})

    def session_cmux(self, options: Options, directory: str, name: str) -> None:
        listing = self.cmux("list-workspaces", "--json")
        ref = cmux_workspace_ref(listing.stdout, directory) if not listing.returncode else ""
        if ref:
            if options.prompt:
                self.warn(
                    f"wt: a cmux workspace for {directory} already exists; ignoring the prompt"
                )
            self.warn_reattached(options)
            self.cmux("select-workspace", "--workspace", ref)
            return
        # A surface's command is typed into the pane, so it is shell-parsed: shell-quote
        # it (agent_command), then JSON-encode it inside the layout (cmux_layout).
        layout = cmux_layout(self.command_for(options, directory=directory))
        # Keep all workspaces made for a repository together. A cmux group always has
        # an anchor workspace, so create an empty group the first time and put this
        # worktree workspace (and all later ones) beneath it.
        group_ref = ""
        repo = options.repo
        if repo:
            groups = self.cmux("workspace-group", "list", "--json")
            group_ref = cmux_group_ref(groups.stdout, repo) if not groups.returncode else ""
            if not group_ref:
                # Pass an explicit empty --from value: older cmux releases otherwise add
                # the caller workspace to the new group along with its anchor.
                created = self.cmux(
                    "workspace-group",
                    "create",
                    "--name",
                    repo,
                    "--cwd",
                    directory,
                    "--from",
                    "",
                    "--json",
                )
                if created.returncode:
                    raise WtError(f"wt: cmux workspace-group create failed for '{repo}'")
                group_ref = cmux_created_group_ref(created.stdout)
                if not group_ref:
                    raise WtError(f"wt: cmux did not return a group reference for '{repo}'")
        argv = ["new-workspace", "--name", sanitize_session_name(name), "--cwd", directory]
        argv += ["--focus", "true", "--layout", layout]
        if group_ref:
            argv += ["--group", group_ref, "--group-placement", "end"]
        result = self.cmux(*argv)
        if result.returncode:
            raise WtError(f"wt: cmux new-workspace failed: {result.stderr.strip()}".rstrip(": "))

    # -- commands --

    def run_wt(self, options: Options) -> None:
        validate_agent_options(
            options.agent, options.model, options.effort, options.permission_mode
        )
        try:
            claude_accounts.validate(options.agent, options.claude_account)
        except ValueError as exc:
            raise WtError(f"wt: {exc}") from exc
        args = options.positional
        if options.name:
            self.reserve_wt(options)
            return
        if not args:
            raise UsageError("wt", "wt: expected a branch, PR number or 'issue'")
        if args[0] in ("issue", "i"):
            options.positional = args[1:]
            self.run_wti(options)
            return
        if DIGITS.fullmatch(args[0]):
            options.positional = args[:1]
            self.run_wtpr(options)
            return
        base = args[0]
        branch = args[1] if len(args) > 1 else base
        repo_path = options.repo_path or os.path.join(self.home, "src", options.repo)
        root = options.worktree_root or os.path.join(self.home, "src", "worktrees", options.repo)
        directory = os.path.join(root, branch)
        if not os.path.isdir(directory):
            # Git refuses to check a branch out twice, and a second checkout would be
            # the wrong thing anyway: work where the branch already lives. That covers
            # the main clone (wt main) as well as a worktree created under another name.
            existing = self.existing_checkout(repo_path, branch)
            if existing:
                self.warn(f"wt: {branch} is already checked out at {existing}; opening that")
                directory = existing
            else:
                self.add_worktree(
                    repo_path,
                    directory,
                    branch,
                    base,
                    options.github_repo,
                    trust=self.trusts_origin(options),
                )
        self.open_session(options, directory, branch, repo_path)

    def reserve_wt(self, options: Options) -> None:
        """``wt --name``: a new branch and worktree for the dashboard, never a reused one."""
        args = options.positional
        if len(args) > 1:
            raise UsageError("wt", "wt: --name takes at most one base branch")
        validate_name(options.name, "wt")
        repo_path = options.repo_path or os.path.join(self.home, "src", options.repo)
        root = options.worktree_root or os.path.join(self.home, "src", "worktrees", options.repo)
        if not os.path.isdir(os.path.join(repo_path, ".git")):
            raise WtError(f"wt: no main clone at {repo_path} (needed to attach a worktree)")
        branch = options.name
        directory = os.path.join(root, branch)
        if os.path.lexists(directory):
            raise WtError(f"wt: explicit worktree destination already exists: {directory}")
        if self.branch_exists(repo_path, branch):
            raise WtError(f"wt: explicit branch already exists: {branch}")
        trust = self.trusts_origin(options)
        base = args[0] if args else self.default_branch(repo_path, options.github_repo, trust)
        self.add_worktree(
            repo_path,
            directory,
            branch,
            base,
            options.github_repo,
            trust=trust,
        )
        self.open_session(options, directory, branch, repo_path)

    def run_wti(self, options: Options) -> None:
        validate_agent_options(
            options.agent, options.model, options.effort, options.permission_mode
        )
        try:
            claude_accounts.validate(options.agent, options.claude_account)
        except ValueError as exc:
            raise WtError(f"wt: {exc}") from exc
        args = options.positional
        if not args:
            raise UsageError("wti", "wti: expected an issue number or GitHub issue URL")
        issue = args[0]
        # A positional branch name may reuse an existing checkout; --name reserves a new one.
        requested = args[1] if len(args) > 1 else options.name
        slug = options.github_repo
        parsed = parse_issue_url(issue)
        if parsed:
            url_owner, url_repo, issue = parsed
            slug = f"{url_owner}/{url_repo}"  # look the issue up in the URL's repo
            if not options.repo_override:
                options.repo = url_repo  # unless -r was given, target ~/src/<url_repo>
        if not DIGITS.fullmatch(issue):
            raise WtError("wti: expected an issue number or GitHub issue URL")
        repo_path = options.repo_path or os.path.join(self.home, "src", options.repo)
        root = options.worktree_root or os.path.join(self.home, "src", "worktrees", options.repo)
        if not os.path.isdir(os.path.join(repo_path, ".git")):
            raise WtError(f"wti: no main clone at {repo_path} (needed to attach a worktree)")
        validate_name(options.name, "wti")
        # Named on the command line, not by the clone's config: its fetch gets the token.
        host = slug
        slug = slug or self.repo_slug(repo_path, "wti")
        info = self.gh_json("wti", slug, "issue", "view", issue, "--json", "number,title")
        number, title = info.get("number"), info.get("title")
        if not isinstance(number, int) or not isinstance(title, str):
            raise WtError("wti: unexpected gh reply")
        if options.tags:
            options.tag_item = work_tags.item_target("issue", slug, number)
        branch = requested or issue_branch(str(number), title)
        directory = os.path.join(root, branch)
        if os.path.lexists(directory):
            # Explicit dashboard names reserve new resources and never reuse a directory.
            if options.name:
                raise WtError(f"wti: explicit worktree destination already exists: {directory}")
            self.open_session(options, directory, branch, repo_path)
            return
        if options.name and self.branch_exists(repo_path, branch):
            raise WtError(f"wti: explicit branch already exists: {branch}")
        trust = self.trusts_origin(options)
        self.add_worktree(
            repo_path,
            directory,
            branch,
            self.default_branch(repo_path, host, trust),
            host,
            trust=trust,
        )
        self.open_session(options, directory, branch, repo_path)

    def run_wtpr(self, options: Options) -> None:
        validate_agent_options(
            options.agent, options.model, options.effort, options.permission_mode
        )
        try:
            claude_accounts.validate(options.agent, options.claude_account)
        except ValueError as exc:
            raise WtError(f"wt: {exc}") from exc
        args = options.positional
        if not args:
            raise UsageError("wtpr", "wtpr: expected a PR number or GitHub PR URL")
        pr = args[0]
        slug = options.github_repo
        parsed = parse_pr_url(pr)
        if parsed:
            url_owner, url_repo, pr = parsed
            slug = f"{url_owner}/{url_repo}"
            if not options.repo_override:
                options.repo = url_repo
        if not DIGITS.fullmatch(pr):
            raise WtError("wtpr: expected a PR number or GitHub PR URL")
        repo_path = options.repo_path or os.path.join(self.home, "src", options.repo)
        root = options.worktree_root or os.path.join(self.home, "src", "worktrees", options.repo)
        if not os.path.isdir(os.path.join(repo_path, ".git")):
            raise WtError(f"wtpr: no main clone at {repo_path}")
        validate_name(options.name, "wtpr")
        # A pull request of a repository named on the command line: its head repository
        # is GitHub's answer, so its fetch gets the token. One the clone's config names
        # could be any repository the token reads, and goes without unless the user typed
        # this in a terminal (see trusts_origin).
        host = slug
        slug = slug or self.repo_slug(repo_path, "wtpr")
        if options.tags:
            options.tag_item = work_tags.item_target("pr", slug, int(pr))
        info = self.gh_json(
            "wtpr",
            slug,
            "pr",
            "view",
            pr,
            "--json",
            "headRefName,headRepository,headRepositoryOwner",
        )
        head_owner = info.get("headRepositoryOwner")
        head_repo = info.get("headRepository")
        owner = head_owner.get("login") if isinstance(head_owner, dict) else None
        rname = head_repo.get("name") if isinstance(head_repo, dict) else None
        head_branch = info.get("headRefName")
        if (
            not isinstance(owner, str)
            or not isinstance(rname, str)
            or not isinstance(head_branch, str)
            or not REPO_SLUG.fullmatch(f"{owner}/{rname}")
            or self.git_query(repo_path, "check-ref-format", f"refs/heads/{head_branch}").returncode
        ):
            raise WtError("wtpr: invalid PR head repository or branch")
        branch: str = head_branch
        local_branch = options.name or branch
        directory = os.path.join(root, local_branch)
        if os.path.lexists(directory):
            # Explicit dashboard names reserve new resources. Terminal reuse still verifies
            # Git provenance before opening and never submits a task to an existing agent.
            if options.name:
                raise WtError(f"wtpr: explicit worktree destination already exists: {directory}")
            # From the layout: commondir files in the checkout's git dir are agent-written.
            try:
                common = safe_git.common_dir(directory)
                expected = safe_git.common_dir(repo_path)
            except ValueError as exc:
                raise WtError(f"wtpr: {exc}") from exc
            current = self.git_value(directory, "symbolic-ref", "--short", "HEAD")
            if common != expected or current != branch:
                raise WtError("wtpr: existing directory is not the requested checkout")
            self.open_session(options, directory, local_branch, repo_path)
            return
        if self.branch_exists(repo_path, local_branch):
            if options.name:
                raise WtError(f"wtpr: explicit branch already exists: {local_branch}")
            base_branch = local_branch = f"pr-{slug.split('/')[0]}-{pr}"
            n = 1
            while self.branch_exists(repo_path, local_branch) or os.path.lexists(
                os.path.join(root, local_branch)
            ):
                n += 1
                local_branch = f"{base_branch}-{n}"
            directory = os.path.join(root, local_branch)
        remote = f"wtpr-{owner}-{rname}"
        url = f"https://github.com/{owner}/{rname}.git"
        existing = self.git_query(
            repo_path, "config", "--get", f"remote.{remote}.url"
        ).stdout.strip()
        if existing and existing != url:
            raise WtError("wtpr: PR remote name belongs to another repository")
        common = os.path.realpath(os.path.join(repo_path, ".git"))
        try:
            safe_git.writable_config(common)
        except ValueError as exc:
            raise WtError(f"wtpr: {exc}") from exc
        if not existing:
            self.git_action(repo_path, "remote", "add", remote, url)
        os.makedirs(root, exist_ok=True)
        self.git_action(
            repo_path,
            "fetch",
            url,
            f"refs/heads/{branch}:refs/heads/{local_branch}",
            credentials=bool(host) or self.trusts_origin(options),
        )
        self.checkout_worktree(repo_path, directory, local_branch)
        try:
            safe_git.writable_config(common)  # Again: the clone was writable meanwhile.
        except ValueError as exc:
            raise WtError(f"wtpr: {exc}") from exc
        self.git_action(directory, "config", f"branch.{local_branch}.remote", remote)
        self.git_action(directory, "config", f"branch.{local_branch}.merge", f"refs/heads/{branch}")
        self.open_session(options, directory, local_branch, repo_path)

    def check_exit_when_done(self, options: Options, command: str) -> None:
        """Refuse ``--exit-when-done`` before anything is made when it cannot work.

        A worker machine's own state home has no dashboard following it: there the watch
        is printed for the controller that launched ``wt`` (``--exit-watch-json``).
        """
        if not options.prompt:
            raise UsageError(command, f"{command}: --exit-when-done needs a prompt (-p or -F)")
        if not options.exit_watch_json and self.on_worker():
            raise WtError(
                f"{command}: --exit-when-done on a worker machine needs --exit-watch-json: "
                "the controller's dashboard follows its watch"
            )
        import exit_watch

        mux = self.multiplexer()
        if mux != "herdr" or not self.runner.which("herdr"):
            chosen = self.env.get("WT_MULTIPLEXER") or "auto"
            reason = (
                "herdr is not on PATH"
                if mux == "herdr"
                else f"WT_MULTIPLEXER={chosen} chose {mux}"
                if chosen == "auto"
                else f"WT_MULTIPLEXER is {chosen}"
            )
            raise WtError(
                f"{command}: --exit-when-done needs herdr ({reason}): "
                "the dashboard finds, exits and closes the agent through its herdr pane"
            )
        if options.exit_watch_json:
            return
        try:
            with exit_watch.connect(self.babysit_home):
                pass
        except OSError as exc:
            raise WtError(
                f"{command}: --exit-when-done cannot record its watch in {self.babysit_home}: {exc}"
            ) from None

    def chosen_machine(self, command: str, options: Options) -> str:
        """Where this run starts: ``local``, ``auto`` or a worker's registry id.

        Without ``--machine``, ``--docker`` means ``auto`` on the controller when its
        registry prefers a Docker worker. Runs the dashboard makes, and runs on a worker,
        always stay where they are.
        """
        machine = options.machine
        if machine not in ("", "auto", "local") and not MACHINE.fullmatch(machine):
            raise UsageError(command, f"{command}: --machine takes a machine id, auto or local")
        if machine in ("", "local"):
            implicit = (
                not machine
                and options.docker
                and not dashboard_run(options)
                and not self.on_worker()
                and self.docker_worker(command)
            )
            return "auto" if implicit else "local"
        if self.on_worker():
            raise WtError(
                f"{command}: --machine {machine}: this is a worker; only the controller "
                "starts launches on other machines"
            )
        if dashboard_run(options):
            raise UsageError(
                command, f"{command}: --machine cannot be combined with the dashboard options"
            )
        return machine

    def docker_worker(self, command: str) -> str | None:
        """The worker the controller's registry prefers for Docker, if any."""
        path = os.path.join(self.babysit_home, "machines.json")
        if not os.path.exists(path):
            return None
        import machines

        try:
            config = machines.load_config(self.babysit_home)
        except (OSError, ValueError) as exc:
            self.warn(f"{command}: cannot read {path} ({exc}); --docker runs here")
            return None
        return config["docker"]["prefer"] if config else None

    def transport(self, command: str) -> DashboardTransport:
        """How this run reaches the controller's placement and New task."""
        return DashboardTransport(self, command)

    def launch_elsewhere(self, command: str, options: Options, machine: str) -> bool:
        """Start this run on a worker through the controller's New task; False to run it here.

        ``auto`` asks the controller's placement first and runs here when it says so, or
        when a worker cannot start this kind of run; a worker named outright refuses it.
        """
        try:
            request = self.worker_request(command, options, machine)
        except NotForWorker as exc:
            if machine != "auto":
                raise WtError(f"{command}: --machine {machine}: {exc}") from None
            # `--docker` alone asked for no worker: a run without a brief stays quietly.
            if options.machine or str(exc) != NO_PROMPT:
                self.warn(f"{command}: {exc}; running here")
            return False
        transport = self.transport(command)
        if machine == "auto":
            placed = transport.place(request)
            if placed.get("error"):
                raise WtError(f"{command}: {placed['error']}")
            if not isinstance(placed.get("machine"), str):
                raise WtError(f"{command}: the dashboard answered the placement unexpectedly")
            if placed.get("machine") == "local":
                self.warn(f"{command}: {placed.get('reason')}; running here")
                return False
            request["machine"] = str(placed["machine"])
            self.warn(f"{command}: {placed.get('reason')}; starting on {placed.get('label')}")
        self.follow_launch(command, transport, transport.start(request))
        return True

    def worker_request(self, command: str, options: Options, machine: str) -> dict[str, object]:
        """This run's launch_request, or NotForWorker when it belongs here.

        A new branch that already exists here, as a branch or a worktree directory, stays
        with its clone: on a worker it would become a second, diverging branch.
        """
        refusal = worker_refusal(command, options)
        if refusal:
            raise NotForWorker(refusal)
        repo_path = os.path.join(self.home, "src", options.repo)
        remotes = self.remote_slugs(repo_path)
        slug = remotes.get("upstream") or remotes.get("origin")
        if not slug:
            raise NotForWorker(f"no GitHub upstream or origin remote in {repo_path}")
        branch = options.positional[1]
        directory = os.path.join(self.home, "src", "worktrees", options.repo, branch)
        if os.path.lexists(directory) or self.branch_exists(repo_path, branch):
            raise NotForWorker(f"{branch} already exists here")
        return launch_request(command, options, slug, machine)

    def follow_launch(
        self, command: str, transport: DashboardTransport | BrokerTransport, op: dict[str, object]
    ) -> None:
        """Wait for the controller's launch to finish, or to wait for room on its machine,
        reporting its progress on stderr."""
        import time

        deadline = time.monotonic() + LAUNCH_WAIT
        said = None
        while True:
            if op.get("message") != said:
                said = op.get("message")
                self.warn(f"{command}: {said}")
            status = op.get("status")
            if status == "complete":
                result = op.get("result")
                if isinstance(result, dict):
                    where = f"{result.get('branch')} at {result.get('path')}"
                    url = f" ({result['url']})" if result.get("url") else ""
                    machine = result.get("machine_label") or "the controller"
                    self.warn(f"{command}: {where} on {machine}{url}")
                return
            if status == "waiting":
                # Started by the controller once there is room, hours later perhaps.
                self.warn(
                    f"{command}: the controller starts it once there is room, or gives up "
                    "after a few hours; its dashboard lists it under Scheduled. Do not "
                    "start it again"
                )
                return
            poll = transport.status
            # From a worker's sandbox there is no dashboard to look at: never retry.
            check = "check the dashboard before retrying" if poll else "do not start it again"
            if status in ("failed", "uncertain"):
                note = "" if status == "failed" else f"; {check}"
                raise WtError(f"{command}: the launch {status}: {said}{note}")
            if poll is None:
                raise WtError(
                    f"{command}: still launching on the controller, which finishes it; "
                    "do not start it again"
                )
            if time.monotonic() >= deadline:
                raise WtError(f"{command}: still launching; follow it in the dashboard")
            time.sleep(LAUNCH_POLL)
            try:
                found = poll(op)
            except WtError as exc:
                # The launch was handed over: never suggest starting it a second time here.
                raise WtError(
                    f"{command}: lost track of the launch ({exc}); check the dashboard "
                    "before retrying"
                ) from None
            if found is None:
                raise WtError(
                    f"{command}: the dashboard no longer lists the launch; check it before retrying"
                )
            op = found

    def sandboxed(self, options: Options) -> str:
        """The sandbox this run is in, where it cannot make its worktree: ``bwrap`` (a
        worker launch's, with its broker), ``safehouse`` (a macOS Seatbelt sandbox that
        cannot write the clone's Git directory) or empty.

        Safehouse may grant a clone (and herdr): there wt works as it always has.
        ``WT_SANDBOXED=0`` or ``1`` decides instead of the macOS checks. Nothing trusts the
        answer: it only picks the way to the controller, which decides what may start.
        """
        if self.env.get("BABYSIT_REGISTER_SOCKET"):
            return "bwrap"
        forced = self.env.get("WT_SANDBOXED")
        if forced in ("0", "1"):
            return "safehouse" if forced == "1" else ""
        if not self.confined():
            return ""
        # `-r owner/name` names the clone `~/src/<name>`.
        name = options.repo.split("/")[1] if REPO_SLUG.fullmatch(options.repo) else options.repo
        git_dir = os.path.join(self.home, "src", name, ".git")
        return "" if os.path.isdir(git_dir) and self.writable(git_dir) else "safehouse"

    def sandbox_repo(self, command: str, options: Options) -> str:
        """The GitHub repository a sandboxed run is for: ``-r owner/name``, else what its
        clone's upstream (or origin) names, when the sandbox can read the clone."""
        if REPO_SLUG.fullmatch(options.repo):
            return options.repo
        repo_path = os.path.join(self.home, "src", options.repo)
        try:
            remotes = self.remote_slugs(repo_path)
        except WtError:
            remotes = {}
        slug = remotes.get("upstream") or remotes.get("origin")
        if not slug:
            raise WtError(
                f"{command}: this sandbox cannot read the GitHub repository of {repo_path}; "
                f"name it as -r owner/{options.repo}"
            )
        return slug

    def parent_claims(self) -> dict[str, object]:
        """What a run inside Safehouse says about the launch it runs in: its directory,
        conversation and Docker grant, which the dashboard records and applies its
        sub-task rules to. Nothing on this machine can check them."""
        sessions = [
            (agent, value)
            for agent, name in SESSION_ENV
            if SESSION.fullmatch(value := self.env.get(name) or "")
        ]
        if not sessions:
            raise WtError(
                "wt: start sub-tasks from a Claude or Codex conversation "
                "(CLAUDE_CODE_SESSION_ID or CODEX_THREAD_ID names it)"
            )
        agent, session = sessions[0]
        homes = {
            "claude": self.env.get("CLAUDE_CONFIG_DIR") or os.path.join(self.home, ".claude"),
            "codex": self.env.get("CODEX_HOME") or os.path.join(self.home, ".codex"),
        }
        return {
            "cwd": os.path.realpath(os.getcwd()),
            "session": session,
            "agent": agent,
            "config_dir": os.path.realpath(homes[agent]),
            "docker": self.docker_granted(),
        }

    def launch_from_sandbox(self, command: str, options: Options, kind: str) -> None:
        """Hand this run to the controller's dashboard: a sandbox makes no worktree.

        From a worker launch's babysit-bwrap it goes through that launch's broker, from
        Safehouse to this machine's dashboard. The controller places it (``auto`` unless
        ``--machine`` names a machine), starts it sandboxed and applies its sub-task rules.
        """
        validate_agent_options(
            options.agent, options.model, options.effort, options.permission_mode
        )
        machine = options.machine or "auto"
        if machine == "local":
            raise WtError(
                f"{command}: --machine local: this sandbox cannot start a task here; "
                "leave --machine out or name a machine"
            )
        if machine != "auto" and not MACHINE.fullmatch(machine):
            raise UsageError(command, f"{command}: --machine takes a machine id, auto or local")
        refusal = worker_refusal(command, options)
        if refusal:
            raise WtError(
                f"{command}: inside a sandbox wt hands only new tasks to the dashboard: {refusal}"
            )
        try:
            request = launch_request(command, options, self.sandbox_repo(command, options), machine)
        except NotForWorker as exc:
            raise WtError(f"{command}: {exc}") from None
        transport: DashboardTransport | BrokerTransport
        if kind == "bwrap":
            transport = BrokerTransport(self, command)
        else:
            transport = DashboardTransport(self, command, parent=self.parent_claims())
        self.warn(f"{command}: inside a sandbox: handing the task to the controller's dashboard")
        self.follow_launch(command, transport, transport.start(request))

    def on_worker(self) -> bool:
        """Whether this is a worker machine: provisioning marks it, and launches say so."""
        marker = os.path.join(self.home, ".config", "babysit-pr", "worker.json")
        return bool(self.env.get("BABYSIT_WORKER")) or os.path.exists(marker)

    def run_tag(self, args: Sequence[str]) -> None:
        """Edit existing work without opening a checkout or starting an agent."""
        usage = "wt: usage: wt tag add|remove <tag> [target ...], or wt tag list [tag]"
        try:
            tags = work_tags.Tags(Path(self.babysit_home))
            if args and args[0] == "list" and len(args) <= 2:
                snapshot = tags.snapshot()
                name = work_tags.tag_name(args[1]) if len(args) == 2 else None
                targets = set(snapshot["assignments"])
                for workspace, items in snapshot["links"].items():
                    targets.update([workspace, *items])
                for target in sorted(targets):
                    names = work_tags.effective(snapshot, target)
                    if names and (name is None or name in names):
                        print(f"{target}\t{', '.join(names)}", file=self.stdout)
                return
            if len(args) < 2 or args[0] not in ("add", "remove"):
                raise WtError(usage)
            name = work_tags.tag_name(args[1])
            chosen_targets = [work_tags.cli_target(value) for value in (args[2:] or [os.getcwd()])]
            for target in chosen_targets:
                tags.change({"target": target, args[0]: [name]})
        except (OSError, ValueError, sqlite3.Error) as exc:
            raise WtError(f"wt: {exc}") from exc

    def run(self, command: str, argv: Sequence[str]) -> None:
        if command == "wt" and argv and argv[0] == "tag":
            self.run_tag(argv[1:])
            return
        options = parse_args(command, argv)
        if options.exit_watch_json and not options.exit_when_done:
            raise UsageError(command, f"{command}: --exit-watch-json needs --exit-when-done")
        # The dashboard's own runs always make their worktree where they run.
        kind = "" if dashboard_run(options) else self.sandboxed(options)
        if kind:
            self.launch_from_sandbox(command, options, kind)
            return
        machine = self.chosen_machine(command, options)
        if machine != "local" and self.launch_elsewhere(command, options, machine):
            return
        if options.exit_when_done:
            self.check_exit_when_done(options, command)
        also = []
        for value in options.also:
            # A clone path never holds ':', which Safehouse refuses; a branch never does.
            name, _, base = value.partition(":")
            clone = self.also_clone(name, command)
            if base:
                if not self.valid_branch(clone, base):
                    raise WtError(f"{command}: --with {value}: not a valid base branch")
                if options.also_bases.setdefault(clone, base) != base:
                    raise WtError(f"{command}: --with names two bases for {clone}")
            also.append(clone)
        options.also = also
        getattr(self, f"run_{command}")(options)


def resolve_command(prog: str, args: Sequence[str]) -> tuple[str, list[str]]:
    """Dispatch on the program name (``wtpr`` symlink) or a leading subcommand (``wt.py wtpr``)."""
    name = os.path.basename(prog)
    name = ALIASES.get(name.removesuffix(".py"), name.removesuffix(".py"))
    rest = list(args)
    if name in COMMANDS and name != "wt":
        return name, rest
    if rest and (rest[0] in COMMANDS or rest[0] in ALIASES):
        return ALIASES.get(rest[0], rest[0]), rest[1:]
    return "wt", rest


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    command, args = resolve_command(argv[0] if argv else "wt", argv[1:])
    env = effective_env(os.environ, read_config(config_path(os.environ)))
    tool = Tool(SubprocessRunner(env), env)
    try:
        tool.run(command, args)
    except HelpRequested as help_request:
        print(HELP[help_request.command], end="")
        return 0
    except UsageError as usage:
        print(usage, file=sys.stderr)
        print(HELP[usage.command], end="", file=sys.stderr)
        return 1
    except WtError as error:
        print(error, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
