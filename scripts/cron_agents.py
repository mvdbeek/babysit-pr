"""Agent cron jobs: each run starts a fresh agent session in the job's own workspace.

A job owns one checkout: the worktree for its branch under ``~/src/worktrees/<repo>``,
made from the base branch on the first run, or wherever that branch is already checked
out. Every run uses that checkout, so changes from one run carry into the next.

A run opens the checkout's herdr workspace, splits a new pane there and types the agent
command into it, the way launches do, so the user's shell wrappers (Safehouse, Claude
accounts) apply. A job marked ``unsandboxed`` types ``command claude``/``command codex``
instead, which skips the Safehouse wrapper so the agent can write anywhere, for example
to other clones when it makes worktrees with ``wt``. Agents those launch are typed into
panes the multiplexer's server starts, with fresh shells and the usual wrappers, so they
are still in Safehouse; nothing marks the environment as unsandboxed. The brief asks the
agent to end with a marker line once the task is finished, as scheduled launches do;
``workspace_exit`` then follows the session and exits the agent once it confirms and
stays idle. The run keeps the agent's final response and closes its pane. An agent that
stops to ask a question, or is still working at the job's time limit, is left open in
Collie and the run needs attention.
"""

import os
import re
import subprocess
import time
import uuid
from pathlib import Path
from urllib.parse import quote

import claude_accounts
import herdr_handoff as handoff
import workspace_agents
import workspace_exit
import wt
from pr_workspaces import BRANCH_NAME, COLLIE_URL, SLUG, herdr, run

START_WAIT = 30  # Seconds for the agent to show up in its pane after the command is typed.
RESULT_LIMIT = 20000  # Characters of the agent's final response kept with a run.
MAX_PROMPT = 32000
PROMPT_KEEP = 86400  # Staged prompts are read once at startup; older ones are removed.
FENCE = re.compile(r"`{3,}\w*")


class Busy(Exception):
    """The job's workspace already has an agent; this run is skipped."""


def validate(request, workspaces, home):
    """The agent part of a job definition, from an untrusted request."""
    if workspaces is None:
        raise ValueError("Agent jobs need the dashboard's workspace actions")
    repo, clone = request.get("repo"), request.get("clone")
    if not isinstance(repo, str) or not SLUG.fullmatch(repo) or repo.split("/")[1] in {".", ".."}:
        raise ValueError("Choose a repository")
    if not isinstance(clone, str) or not any(
        r["repo"] == repo and r["clone"] == clone
        for r in workspaces.local_repositories(everything=True)["repos"]
    ):
        raise ValueError(f"Choose a local clone of {repo}")
    branch = request.get("branch")
    if (
        not isinstance(branch, str)
        or not BRANCH_NAME.fullmatch(branch)
        or not workspaces.valid_branch(clone, branch)
    ):
        raise ValueError("Name the job's branch: letters, digits, '.', '_' and '-'")
    try:
        current = git(clone, "rev-parse", "--abbrev-ref", "HEAD")
    except ValueError:
        current = None
    if current == branch:
        raise ValueError(
            f"{branch} is checked out in the clone itself; choose a branch the job can "
            "work on in its own worktree"
        )
    base = request.get("base") or ""
    if not isinstance(base, str):
        raise ValueError("The base branch is not a valid branch name")
    base = base.strip()
    # The base is fetched from origin, so it must name a branch, not a revision.
    if base and (base.startswith("-") or "@" in base or not workspaces.valid_branch(clone, base)):
        raise ValueError("The base branch is not a valid branch name")
    agent = request.get("agent")
    if agent not in {"codex", "claude"}:
        raise ValueError("Select Codex or Claude")
    settings = {key: request.get(key) or "" for key in ("model", "effort", "claude_account")}
    if not all(isinstance(value, str) for value in settings.values()):
        raise ValueError("Invalid agent settings")
    workspace_agents.validate(agent, settings["model"], settings["effort"], home)
    claude_accounts.validate(agent, settings["claude_account"] or None)
    docker = request.get("docker", False)
    if not isinstance(docker, bool):
        raise ValueError("Expected docker to be true or false")
    unsandboxed = request.get("unsandboxed", False)
    if not isinstance(unsandboxed, bool):
        raise ValueError("Expected unsandboxed to be true or false")
    if docker and unsandboxed:
        raise ValueError("Docker access only applies inside Safehouse; choose one")
    prompt = request.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > MAX_PROMPT:
        raise ValueError("Give the agent a prompt of 1–32,000 characters")
    if "\0" in prompt:
        raise ValueError("The prompt cannot contain NUL characters")
    return {
        "repo": repo,
        "clone": clone,
        "branch": branch,
        "base": base,
        "agent": agent,
        "model": settings["model"],
        "effort": settings["effort"],
        "claude_account": settings["claude_account"] if agent == "claude" else "",
        "docker": docker,
        "unsandboxed": unsandboxed,
        "prompt": prompt,
    }


def git(clone, *args, timeout=30):
    return run("git", "-C", clone, *args, timeout=timeout)


def checkout(job, src):
    """The job's checkout, made on first use: wt's rules for a branch and its base."""
    clone = job["clone"]
    directory = Path(src) / "worktrees" / job["repo"].split("/")[1] / job["branch"]
    if not directory.is_dir():
        # Git checks a branch out only once: work wherever it already lives.
        listed = wt.parse_worktree_list(git(clone, "worktree", "list", "--porcelain"))
        existing = listed.get(job["branch"])
        if existing and Path(existing).is_dir():
            # An unattended agent never works in the user's own clone.
            if Path(existing).resolve() == Path(clone).resolve():
                raise ValueError(f"{job['branch']} is checked out in the clone itself")
            return Path(existing).resolve()
        directory.parent.mkdir(parents=True, exist_ok=True)
        try:
            git(clone, "rev-parse", "--verify", "--quiet", f"refs/heads/{job['branch']}")
        except ValueError:
            start = job["base"] or "HEAD"
            if job["base"] and "origin" in git(clone, "remote").split():
                git(clone, "fetch", "origin", job["base"], timeout=120)
                start = f"origin/{job['base']}"
            git(clone, "worktree", "add", "-b", job["branch"], str(directory), start, timeout=120)
        else:
            git(clone, "worktree", "add", str(directory), job["branch"], timeout=120)
    branch = git(str(directory), "rev-parse", "--abbrev-ref", "HEAD")
    if branch != job["branch"]:
        raise ValueError(f"{directory} has {branch} checked out instead of {job['branch']}")
    return directory.resolve()


def workspace_for(path):
    for space in herdr("workspace", "list")["workspaces"]:
        checkout_path = (space.get("worktree") or {}).get("checkout_path")
        if checkout_path and Path(checkout_path).resolve() == path:
            return space["workspace_id"]
    return None


def start(job, run_id, home, src):
    """Type the agent command into a new pane of the job's workspace."""
    path = checkout(job, src)
    workspace = workspace_for(path)
    anchor = None
    if workspace is None:
        opened = herdr(
            "worktree",
            "open",
            "--cwd",
            job["clone"],
            "--path",
            str(path),
            "--label",
            wt.sanitize_session_name(f"cron-{job['name']}"),
            "--no-focus",
        )
        workspace = workspace_for(path)
        anchor = (opened.get("root_pane") or {}).get("pane_id")
        if workspace is None or not anchor:
            raise ValueError("herdr did not open a workspace for the job's checkout")
    elif any(a.get("workspace_id") == workspace for a in herdr("agent", "list")["agents"]):
        # Two agents in one checkout would trip over each other's changes.
        raise Busy("An agent is still open in the job's workspace")
    else:
        anchor = next(
            (p["pane_id"] for p in herdr("pane", "list", "--workspace", workspace)["panes"]),
            None,
        )
        if anchor is None:
            raise ValueError("The job's herdr workspace has no pane to split")
    # Everything that can fail before typing is done first, so no empty pane is left.
    config = claude_accounts.validate(job["agent"], job["claude_account"] or None)
    marker = workspace_exit.new_marker()
    staged = stage(home, run_id, job["prompt"].rstrip() + "\n" + workspace_exit.brief(marker))
    command = wt.agent_command(
        job["agent"],
        job["prompt"],
        job["model"],
        # Left on Default: the repository's or global saved default, if any.
        job["effort"]
        or workspace_agents.default_effort(home, job["repo"], job["agent"], job["model"]),
        stage=lambda _: str(staged),
        docker=job["docker"],
        claude_config_dir=str(config) if config else None,
        claude_subscription=bool(job["claude_account"]),
        # Jobs saved before the option existed lack it.
        unsandboxed=job.get("unsandboxed", False),
        directory=str(path),
    )
    # A fresh pane, as resumes use; the root pane stays a plain shell for the user.
    pane = herdr("pane", "split", anchor, "--direction", "right", "--cwd", str(path), "--no-focus")[
        "pane"
    ]["pane_id"]
    launched = {
        "workspace_id": workspace,
        "pane": pane,
        "path": str(path),
        "url": f"{COLLIE_URL}/space/{quote(workspace, safe='')}",
        "exit_marker": marker,
        "launched_at": time.time(),
    }
    try:
        run("herdr", "pane", "run", pane, command)
    except subprocess.TimeoutExpired:
        return {**launched, "uncertain": "herdr did not answer while typing the command"}
    except (OSError, ValueError, subprocess.SubprocessError):
        close_pane(pane)  # Nothing was typed: the pane is an empty shell.
        raise
    # From here the command is typed: trouble makes the outcome uncertain, never a failure
    # to start, since the agent may be running.
    deadline = time.monotonic() + START_WAIT
    try:
        while time.monotonic() < deadline:
            if any(
                a.get("pane_id") == pane and a.get("agent") == job["agent"]
                for a in herdr("agent", "list")["agents"]
            ):
                return launched
            time.sleep(1)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return {**launched, "uncertain": f"Could not check that the agent started ({exc})"}
    return {**launched, "uncertain": "The agent did not show up in its pane"}


def stage(home, run_id, text):
    """A private file the typed command reads the prompt back from."""
    prompts = Path(home) / "cron-prompts"
    prompts.mkdir(mode=0o700, exist_ok=True)
    for old in prompts.iterdir():
        try:
            if time.time() - old.stat().st_mtime > PROMPT_KEEP:
                old.unlink()
        except OSError:
            continue
    staged = prompts / f"{run_id}-{uuid.uuid4().hex[:8]}"
    descriptor = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(text)
    return staged


def operation(job, launched):
    """What ``workspace_exit`` reads about a launch."""
    return {
        "status": "complete",
        "exit_marker": launched["exit_marker"],
        "path": launched["path"],
        "result": {"workspace_id": launched["workspace_id"]},
        "agent": job["agent"],
        "claude_account": job["claude_account"] or None,
        "created_at": launched["launched_at"],
        "updated_at": launched["launched_at"],
    }


def final_response(record, agent, marker):
    """The agent's last answer in the session the watch followed, without the marker."""
    target = (record or {}).get("target") or {}
    if not target.get("rollout") or not target.get("session_id"):
        return None
    try:
        _, text = handoff.latest_turn(target["rollout"], target["session_id"], agent)
    except (OSError, RuntimeError, ValueError):
        return None
    if not text:
        return None
    lines = text.rstrip().splitlines()
    end = len(lines)
    # The marker may sit in its own code fence, as workspace_exit.confirms accepts.
    fenced = bool(end) and FENCE.fullmatch(lines[end - 1].strip()) is not None
    end -= fenced
    if end and lines[end - 1].strip().strip("`*").strip() == marker:
        end -= 1
        if fenced and end and FENCE.fullmatch(lines[end - 1].strip()):
            end -= 1
        return "\n".join(lines[:end]).rstrip()[:RESULT_LIMIT]
    return text.rstrip()[:RESULT_LIMIT]


def close_pane(pane):
    """Close a finished run's pane once only its shell is left in it."""
    try:
        procs = handoff.result("pane", "process-info", "--pane", pane)["process_info"]
        if any(p.get("pid") != procs.get("shell_pid") for p in procs["foreground_processes"]):
            return False
        run("herdr", "pane", "close", pane)
        return True
    except (OSError, RuntimeError, ValueError, KeyError, subprocess.SubprocessError):
        return False
