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
import sys
from collections.abc import Callable, Mapping, Sequence
from typing import NamedTuple, Protocol, TextIO

COMMANDS = ("wt", "wti", "wtpr")
ALIASES = {"wri": "wti", "wtissue": "wti"}
DEFAULT_REPO = "galaxy"
MULTIPLEXERS = ("herdr", "tmux", "cmux", "none", "auto")

MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,159}")
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})
CODEX_EFFORTS = frozenset({"none", "minimal", "ultra"})
REPO_SLUG = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
DIGITS = re.compile(r"[0-9]+")
GITHUB = r"(?:https?://)?(?:www\.)?github\.com/([^/]+)/([^/]+)/"
ISSUE_URL = re.compile(GITHUB + r"issues/([0-9]+)")
PR_URL = re.compile(GITHUB + r"pull/([0-9]+)")
UNSAFE = re.compile(r"[^A-Za-z0-9_-]")
CONFIG_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
HERDR_RUNNING = re.compile(r'"running"\s*:\s*true')

VALUE_OPTIONS = {
    "-r": "repo",
    "-p": "prompt",
    "--prompt": "prompt",
    "-F": "prompt_file",
    "--prompt-file": "prompt_file",
    "--model": "model",
    "--effort": "effort",
    "--name": "name",
    "--label": "label",
    "--repo-path": "repo_path",
    "--worktree-root": "worktree_root",
    "--agent-arg": "agent_args",
}
WT_OPTIONS = frozenset(
    {"--codex", "--claude", "--model", "--effort", "-r", "-p", "--prompt", "-F", "--prompt-file"}
)
DASHBOARD_OPTIONS = frozenset(
    {"--no-focus", "--name", "--label", "--repo-path", "--worktree-root", "--agent-arg"}
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

Open or create a Git worktree, then attach a multiplexer session.
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
  --model <id>  override the model for a new session only
  --effort <level>  override reasoning effort for a new session only
  -r repo       use ~/src/<repo> instead of ~/src/galaxy
  -p, --prompt <text>       start the agent with this initial prompt
  -F, --prompt-file <path>  start the agent with this file's contents as the prompt
  -h, --help    show this help

dashboard options (a new branch from the remote default branch unless <base> is given):
  wt --name <branch> [--repo-path <clone>] [--worktree-root <dir>] [--label <label>]
     [--no-focus] [--agent-arg <word>]... [<base>]
  --agent-arg <word>      append one word to the agent command line (repeatable)

The prompt only applies to a session being created; if one is already running for
the worktree it is reattached and the prompt is ignored (with a warning).

examples:
  # fire off a fix with a brief, no pasting
  wt -F /tmp/diagnosis.md release_26.1 workflow-copy-drops-readme
  wt -p 'Fix the failing tests in lib/galaxy/tools' dev my-branch
  wt --codex -p 'Review this PR for security issues' 22886
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
  --model <id>  override the model for a new session only
  --effort <level>  override reasoning effort for a new session only
  -r repo       use ~/src/<repo> instead of the repo from the issue URL or ~/src/galaxy
  -p, --prompt <text>       start the agent with this initial prompt
  -F, --prompt-file <path>  start the agent with this file's contents as the prompt
  --name <name>           reserve a new worktree and branch with this explicit name
  --label <label>         herdr workspace label (default: the branch name)
  --no-focus              leave native herdr focus unchanged
  --repo-path <path>      use this main clone (overrides -r location)
  --worktree-root <path>  parent directory for the new checkout
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
  --model <id>  override the model for a new session only
  --effort <level>  override reasoning effort for a new session only
  -r repo       use ~/src/<repo> instead of the repo from the PR URL or ~/src/galaxy
  -p, --prompt <text>       start the agent with this initial prompt
  -F, --prompt-file <path>  start the agent with this file's contents as the prompt
  --name <name>           reserve a new worktree and branch with this explicit name
  --label <label>         herdr workspace label (default: the branch name)
  --no-focus              leave native herdr focus unchanged
  --repo-path <path>      use this main clone (overrides -r location)
  --worktree-root <path>  parent directory for the new checkout
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


class HelpRequested(Exception):
    def __init__(self, command: str):
        super().__init__(command)
        self.command = command


class Options:
    """Parsed command line shared by the three commands (``wt`` lacks the dashboard flags)."""

    __slots__ = (
        "agent",
        "repo",
        "repo_override",
        "prompt",
        "model",
        "effort",
        "name",
        "label",
        "repo_path",
        "worktree_root",
        "focus",
        "agent_args",
        "positional",
    )

    def __init__(self) -> None:
        self.agent = "claude"
        self.repo = DEFAULT_REPO
        self.repo_override = False
        self.prompt = ""
        self.model = ""
        self.effort = ""
        self.name = ""
        self.label = ""
        self.repo_path = ""
        self.worktree_root = ""
        self.focus = True
        self.agent_args: list[str] = []
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
        elif flag == "--no-focus":
            options.focus = False
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
            elif field == "agent_args":
                if any(ord(char) < 32 or char == "\x7f" for char in value):
                    raise WtError(f"{command}: --agent-arg must not contain control characters")
                options.agent_args.append(value)
            else:
                setattr(options, field, value)
            args.pop(0)
        args.pop(0)
    options.positional = args
    return options


def validate_agent_options(agent: str, model: str, effort: str) -> None:
    """Syntax only; model availability belongs to the agent CLI and its provider."""
    if model and not MODEL.fullmatch(model):
        raise WtError("wt: invalid model id")
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


def agent_words(agent: str, model: str, effort: str, extra: Sequence[str] = ()) -> list[str]:
    words = [agent]
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
    """
    command = " ".join(shlex.quote(word) for word in agent_words(agent, model, effort, extra))
    if not prompt:
        return command
    # Extra words may end in a variadic option (Claude's --mcp-config <configs...>) that
    # would swallow the prompt; `--` ends option parsing before it.
    separator = " --" if extra else ""
    return f'{command}{separator} "$(cat {shlex.quote(stage(prompt))})"'


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


def parse_herdr_open(output: str) -> HerdrOpen:
    """``already_open`` and the root pane id from ``herdr worktree open``'s JSON reply."""
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
    return HerdrOpen(result.get("already_open") is True, pane if isinstance(pane, str) else "")


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
    ):
        self.runner = runner
        self.env = dict(env)
        self.stdout = stdout or sys.stdout
        self.stderr = stderr or sys.stderr
        self.isatty = isatty or (lambda: sys.stdin is not None and sys.stdin.isatty())
        self.stage = stage or stage_prompt
        self.home = self.env.get("HOME") or os.path.expanduser("~")

    # -- small process helpers --

    def warn(self, message: str) -> None:
        print(message, file=self.stderr, flush=True)

    def git_query(self, cwd: str, *args: str) -> Completed:
        return self.runner.run(["git", "-C", cwd, *args])

    def git_value(self, cwd: str, *args: str) -> str:
        result = self.git_query(cwd, *args)
        if result.returncode:
            raise WtError(result.stderr.strip() or f"git {' '.join(args)} failed")
        return result.stdout.strip()

    def git_action(self, cwd: str, *args: str) -> None:
        """Mutating git commands keep their own output (progress, hints) on stderr."""
        result = self.runner.run(["git", "-C", cwd, *args], output="stderr")
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

    def add_worktree(self, repo_path: str, directory: str, branch: str, base: str) -> None:
        """Reuse a local branch, else create from the remote or local base."""
        os.makedirs(os.path.dirname(directory), exist_ok=True)
        remotes = self.git_value(repo_path, "remote").splitlines()
        start_point = base
        if "origin" in remotes:
            self.git_action(repo_path, "fetch", "origin", base)
            start_point = f"origin/{base}"
        if self.branch_exists(repo_path, branch):
            self.git_action(repo_path, "worktree", "add", directory, branch)
        else:
            self.git_action(repo_path, "worktree", "add", "-b", branch, directory, start_point)

    # -- multiplexers --

    def herdr_running(self) -> bool:
        result = self.runner.run(["herdr", "status", "server", "--json"])
        return not result.returncode and herdr_is_running(result.stdout)

    def multiplexer(self) -> str:
        return select_multiplexer(self.env, self.runner.which, self.herdr_running)

    def command_for(self, options: Options) -> str:
        return agent_command(
            options.agent,
            options.prompt,
            options.model,
            options.effort,
            self.stage,
            options.agent_args,
        )

    def open_session(self, options: Options, directory: str, name: str, repo_path: str) -> None:
        """Left pane runs the agent, right pane is a spare terminal (herdr: the agent only).

        The prompt only applies to a session being created; an existing one already
        has an agent in it, so reattaching warns and ignores the prompt.
        """
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
                self.warn(
                    f"wt: a herdr workspace for {directory} already exists; ignoring the prompt"
                )
            return
        if not reply.root_pane:
            raise WtError(f"wt: herdr did not return a root pane for {directory}")
        command = self.command_for(options)
        # No spare terminal split here: the reviewr plugin already splits a pane into
        # every new worktree workspace, so a second one would sit empty.
        # pane run submits text + Enter; the pty buffers it until the new shell reads it.
        result = self.runner.run(["herdr", "pane", "run", reply.root_pane, command])
        if result.returncode:
            raise WtError(f"wt: herdr pane run failed: {result.stderr.strip()}".rstrip(": "))

    def session_tmux(self, options: Options, directory: str, name: str) -> None:
        session = sanitize_session_name(name)
        if self.runner.run(["tmux", "has-session", "-t", f"={session}"]).returncode:
            command = self.command_for(options)
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
        elif options.prompt:
            self.warn(f"wt: tmux session '{session}' already running; ignoring the prompt")
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
            self.cmux("select-workspace", "--workspace", ref)
            return
        # A surface's command is typed into the pane, so it is shell-parsed: shell-quote
        # it (agent_command), then JSON-encode it inside the layout (cmux_layout).
        layout = cmux_layout(self.command_for(options))
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
        validate_agent_options(options.agent, options.model, options.effort)
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
                self.add_worktree(repo_path, directory, branch, base)
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
        base = args[0] if args else ""
        if not base:
            head = self.git_query(
                repo_path, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"
            )
            base = head.stdout.strip().removeprefix("origin/") if not head.returncode else "main"
        self.add_worktree(repo_path, directory, branch, base)
        self.open_session(options, directory, branch, repo_path)

    def run_wti(self, options: Options) -> None:
        validate_agent_options(options.agent, options.model, options.effort)
        args = options.positional
        if not args:
            raise UsageError("wti", "wti: expected an issue number or GitHub issue URL")
        issue = args[0]
        # A positional branch name may reuse an existing checkout; --name reserves a new one.
        requested = args[1] if len(args) > 1 else options.name
        slug = ""
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
        slug = slug or self.repo_slug(repo_path, "wti")
        info = self.gh_json("wti", slug, "issue", "view", issue, "--json", "number,title")
        number, title = info.get("number"), info.get("title")
        if not isinstance(number, int) or not isinstance(title, str):
            raise WtError("wti: unexpected gh reply")
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
        head = self.git_query(
            repo_path, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"
        )
        base = head.stdout.strip().removeprefix("origin/") if not head.returncode else ""
        self.add_worktree(repo_path, directory, branch, base or "main")
        self.open_session(options, directory, branch, repo_path)

    def run_wtpr(self, options: Options) -> None:
        validate_agent_options(options.agent, options.model, options.effort)
        args = options.positional
        if not args:
            raise UsageError("wtpr", "wtpr: expected a PR number or GitHub PR URL")
        pr = args[0]
        slug = ""
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
        slug = slug or self.repo_slug(repo_path, "wtpr")
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
            common = self.git_value(
                directory, "rev-parse", "--path-format=absolute", "--git-common-dir"
            )
            expected = self.git_value(
                repo_path, "rev-parse", "--path-format=absolute", "--git-common-dir"
            )
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
        if not existing:
            self.git_action(repo_path, "remote", "add", remote, url)
        os.makedirs(root, exist_ok=True)
        self.git_action(
            repo_path, "fetch", remote, f"refs/heads/{branch}:refs/heads/{local_branch}"
        )
        self.git_action(repo_path, "worktree", "add", directory, local_branch)
        self.git_action(directory, "config", f"branch.{local_branch}.remote", remote)
        self.git_action(directory, "config", f"branch.{local_branch}.merge", f"refs/heads/{branch}")
        self.open_session(options, directory, local_branch, repo_path)

    def run(self, command: str, argv: Sequence[str]) -> None:
        options = parse_args(command, argv)
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
