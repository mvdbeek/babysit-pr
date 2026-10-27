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
Collie and the run needs attention. A job on a worker is followed and exited the same
way through that worker's herdr and probe.
"""

import fcntl
import os
import re
import subprocess
import time
import uuid
from pathlib import Path

import claude_accounts
import herdr_handoff as handoff
import machines
import placement
import remote_probe
import safe_git
import sandbox
import workspace_agents
import workspace_exit
import wt
from pr_workspaces import BRANCH_NAME, SLUG, collie_url, herdr, run
from pr_workspaces import git as workspaces_git
from pr_workspaces import remote_slugs as workspaces_remote_slugs
from pr_workspaces import repo_config as workspaces_repo_config

START_WAIT = 30  # Seconds for the agent to show up in its pane after the command is typed.
RESULT_LIMIT = 20000  # Characters of the agent's final response kept with a run.
MAX_PROMPT = 32000
PROMPT_KEEP = 86400  # Staged prompts are read once at startup; older ones are removed.
# A worker's prompts, in a folder babysit-bwrap never grants an agent.
REMOTE_PROMPTS = "~/.local/state/babysit-pr/cron-prompts"
# How long a worker may take to make a job's checkout.
CHECKOUT_TIMEOUT = 180
# Seconds between looks at a run on a worker: each costs it ssh calls.
REMOTE_WATCH = 60
# A worker's sandbox follows its operating system.
BACKENDS = {"darwin": "safehouse", "linux": "bwrap"}
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
    machine = place(request, workspaces, repo)
    if machine is not None:
        return validate_remote(request, machine, repo, home)
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
    # The base is fetched from the job's repository, so it must name a branch.
    if base and (base.startswith("-") or "@" in base or not workspaces.valid_branch(clone, base)):
        raise ValueError("The base branch is not a valid branch name")
    return {
        "repo": repo,
        # Registry id of the machine the job runs on; None is this one.
        "machine": None,
        "clone": clone,
        "branch": branch,
        "base": base,
        **validate_settings(request, home),
    }


def validate_settings(request, home, machine=None):
    """The agent, its options and the prompt; a worker's Claude account is checked there."""
    agent = request.get("agent")
    if agent not in {"codex", "claude"}:
        raise ValueError("Select Codex or Claude")
    settings = {key: request.get(key) or "" for key in ("model", "effort", "claude_account")}
    if not all(isinstance(value, str) for value in settings.values()):
        raise ValueError("Invalid agent settings")
    workspace_agents.validate(agent, settings["model"], settings["effort"], home)
    if machine is None:
        claude_accounts.validate(agent, settings["claude_account"] or None)
    elif settings["claude_account"]:
        # The worker's own logins are checked there; only the name is checked here.
        if agent != "claude":
            raise ValueError("A Claude account can only be selected for Claude")
        if not claude_accounts.NAME.fullmatch(settings["claude_account"]):
            raise ValueError(
                "Use an account name containing letters, digits, underscores or hyphens"
            )
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
        "agent": agent,
        "model": settings["model"],
        "effort": settings["effort"],
        "claude_account": settings["claude_account"] if agent == "claude" else "",
        "docker": docker,
        "unsandboxed": unsandboxed,
        "prompt": prompt,
    }


def place(request, workspaces, repo):
    """The worker a job runs on, None for this machine; Auto is resolved once, here.

    A request without ``machine`` runs here, as before workers existed.
    """
    choice = request.get("machine")
    if "machine" not in request or choice is None:
        return None
    if not isinstance(choice, str):
        raise ValueError("Choose a machine")
    unsandboxed = request.get("unsandboxed") is True
    fields = {
        "kind": "cron",
        "repo": repo,
        "agent": request.get("agent"),
        "claude_account": request.get("claude_account") or None,
        "docker": request.get("docker") is True,
        "sandboxed": not unsandboxed,
    }
    # Here a job works in a local clone, as a new task does.
    machine_id = workspaces.place({"machine": choice}, fields, {placement.LOCAL: 1})
    return machines.resolve(workspaces.home, machine_id, placing=True)


def validate_remote(request, machine, repo, home):
    """A job for ``machine``: it works in that worker's clone, ``<src>/<repo name>``.

    The clone, branch and base are checked there, by the probe that makes the checkout.
    Nothing a run needs repairs for runs on a worker yet, and its agent is always
    sandboxed.
    """
    if request.get("unsandboxed", False) is not False:
        raise ValueError(f"Jobs on worker machines always run in their sandbox ({machine.label})")
    branch = request.get("branch")
    if not isinstance(branch, str) or not BRANCH_NAME.fullmatch(branch) or ".." in branch:
        raise ValueError("Name the job's branch: letters, digits, '.', '_' and '-'")
    base = request.get("base") or ""
    if not isinstance(base, str):
        raise ValueError("The base branch is not a valid branch name")
    base = base.strip()
    if base and (not BRANCH_NAME.fullmatch(base) or ".." in base):
        raise ValueError("The base branch is not a valid branch name")
    return {
        "repo": repo,
        "machine": machine.id,
        "clone": f"{machine.src.rstrip('/')}/{repo.split('/')[1]}",
        "branch": branch,
        "base": base,
        **validate_settings(request, home, machine),
    }


def git(clone, *args, timeout=30):
    return workspaces_git(clone, *args, timeout=timeout)


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
            # The job's own repository, never a URL its clone's config (which the agent
            # writes) names; it lands in the refs of the remote that tracks it.
            remotes = workspaces_remote_slugs(workspaces_repo_config(clone))
            tracking = [
                name
                for name in ("origin", "upstream", *sorted(remotes))
                if remotes.get(name) == job["repo"].lower()
            ]
            if job["base"] and tracking:
                git(
                    clone,
                    "fetch",
                    safe_git.github_url(job["repo"]),
                    f"+refs/heads/{job['base']}:refs/remotes/{tracking[0]}/{job['base']}",
                    timeout=120,
                )
                # In full, so no local branch or tag named <remote>/<base> wins.
                start = f"refs/remotes/{tracking[0]}/{job['base']}"
            add = ["-b", job["branch"], str(directory), start]
        else:
            add = [str(directory), job["branch"]]
        # The new worktree's branch and git dir can bring config the clone's listing never
        # sees, so its files are checked out by a reset that lists config inside it.
        git(clone, "worktree", "add", "--no-checkout", *add, timeout=120)
        try:
            git(str(directory), "reset", "--hard", "--quiet", timeout=120)
        except Exception:
            # Left behind, the empty checkout would be reused with every file deleted.
            git(clone, "worktree", "remove", "--force", str(directory), timeout=120)
            if add[0] == "-b":
                git(clone, "branch", "-D", job["branch"])
            raise
    branch = git(str(directory), "rev-parse", "--abbrev-ref", "HEAD")
    if branch != job["branch"]:
        # A run may leave its checkout on another branch, such as one it opened a pull
        # request from; the next run starts on the job's own again, unless that would
        # carry someone's uncommitted changes along.
        stated = f"{directory} has {branch} checked out instead of {job['branch']}"
        if git(str(directory), "status", "--porcelain", "--untracked-files=no"):
            raise ValueError(f"{stated}, with uncommitted changes")
        try:
            git(str(directory), "switch", "--quiet", job["branch"])
        except ValueError as exc:
            raise ValueError(f"{stated}, and switching back failed: {exc}") from exc
    return directory.resolve()


def workspace_for(path, machine=None):
    for space in herdr("workspace", "list", machine=machine)["workspaces"]:
        checkout_path = (space.get("worktree") or {}).get("checkout_path")
        if machine is not None:
            # A worker's path, as its probe resolved it there; never resolved here.
            if checkout_path == str(path):
                return space["workspace_id"]
        elif checkout_path and Path(checkout_path).resolve() == path:
            return space["workspace_id"]
    return None


def probe(home, machine, op, timeout, **args):
    """One probe op on a worker; every failure, a timeout included, names the worker."""
    try:
        return remote_probe.call(home, machine, op, timeout, **args)
    except subprocess.TimeoutExpired as exc:
        raise ValueError(f"{machine.label} did not answer within {timeout} seconds") from exc
    except OSError as exc:
        raise ValueError(f"{machine.label} could not be reached: {exc}") from exc


def remote_checkout(job, home, machine):
    """The job's checkout on its worker, made there with the worker's own hardened Git."""
    name = job["repo"].split("/")[1]
    return probe(
        home,
        machine,
        "cron_checkout",
        CHECKOUT_TIMEOUT,
        clone=job["clone"],
        repo=job["repo"],
        branch=job["branch"],
        base=job["base"],
        directory=f"{machine.src.rstrip('/')}/worktrees/{name}/{job['branch']}",
        agent=job["agent"],
        account=job["claude_account"] or None,
        babysit_root=machine.babysit_root,
        sandbox=BACKENDS[machine.os],
    )


def remote_command(job, machine, found, staged, effort, extra=()):
    """The line typed on ``machine``: its own sandbox, with the paths its probe reported.

    babysit-bwrap is given the checkout, its common dir and the agent's configuration
    there; Safehouse on a Mac worker grants those through the user's shell functions.
    A worker with the ``gpg`` label lets the agent sign. ``extra`` are agent arguments
    before the prompt, as a resume's.
    """
    backend = BACKENDS[machine.os]
    features = ("gpg",) if "gpg" in machine.labels else ()
    confined = None
    if backend == "bwrap":
        if not found["launcher"]:
            raise ValueError(f"babysit-bwrap is not installed under {machine.babysit_root} there")
        own = (
            f"--claude-config-dir={found['claude_home']}"
            if job["agent"] == "claude"
            else f"--codex-home={found['codex_home']}"
        )
        confined = [
            own,
            *sandbox.flags(sandbox.Grants(add_dirs=(found["path"], found["common"])), backend),
        ]
    home = found["claude_home"] if job["agent"] == "claude" else None
    return wt.agent_command(
        job["agent"],
        job["prompt"],
        job["model"],
        effort,
        stage=lambda _: staged,
        extra=extra,
        docker=job["docker"],
        claude_config_dir=home,
        claude_subscription=bool(job["claude_account"]),
        # As the worker reported it: the literal path, or None for its default login.
        claude_config_env=found["claude_env"] if home else None,
        backend=backend,
        features=features,
        launcher=found["launcher"],
        confined=confined,
    )


def start(job, run_id, home, src):
    """Type the agent command into a new pane of the job's workspace.

    A job with a ``machine`` does all of it on that worker, over herdr and ssh, holding
    the worker clone's lock until its agent is typed: cleanup and launches there take it,
    by the clone's real path there, so none removes the checkout before it has an agent.
    """
    machine = machines.resolve(home, job["machine"], placing=True) if job.get("machine") else None
    if machine is None:
        return started(job, run_id, home, src, None)
    real = probe(home, machine, "realpath", 30, path=job["clone"])["path"]
    with machines.clone_lock(home, machine, real).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return started(job, run_id, home, src, machine)


def started(job, run_id, home, src, machine):
    found = None
    if machine is None:
        path = checkout(job, src)
        clone = job["clone"]
    else:
        if job.get("unsandboxed"):
            raise ValueError(
                f"Jobs on worker machines always run in their sandbox ({machine.label})"
            )
        found = remote_checkout(job, home, machine)
        path, clone = found["path"], found["clone"]
    workspace = workspace_for(path, machine)
    anchor = None
    if workspace is None:
        opened = herdr(
            "worktree",
            "open",
            "--cwd",
            clone,
            "--path",
            str(path),
            "--label",
            wt.sanitize_session_name(f"cron-{job['name']}"),
            "--no-focus",
            machine=machine,
        )
        workspace = workspace_for(path, machine)
        anchor = (opened.get("root_pane") or {}).get("pane_id")
        if workspace is None or not anchor:
            raise ValueError("herdr did not open a workspace for the job's checkout")
    elif any(
        a.get("workspace_id") == workspace
        for a in herdr("agent", "list", machine=machine)["agents"]
    ):
        # Two agents in one checkout would trip over each other's changes.
        raise Busy("An agent is still open in the job's workspace")
    else:
        anchor = next(
            (
                p["pane_id"]
                for p in herdr("pane", "list", "--workspace", workspace, machine=machine)["panes"]
            ),
            None,
        )
        if anchor is None:
            raise ValueError("The job's herdr workspace has no pane to split")
    # Everything that can fail before typing is done first, so no empty pane is left.
    marker = workspace_exit.new_marker()
    brief = job["prompt"].rstrip() + "\n" + workspace_exit.brief(marker)
    # Left on Default: the repository's or global saved default, if any.
    effort = job["effort"] or workspace_agents.default_effort(
        home, job["repo"], job["agent"], job["model"]
    )
    if machine is None:
        config = claude_accounts.validate(job["agent"], job["claude_account"] or None)
        staged = stage(home, run_id, brief)
        command = wt.agent_command(
            job["agent"],
            job["prompt"],
            job["model"],
            effort,
            stage=lambda _: str(staged),
            docker=job["docker"],
            claude_config_dir=str(config) if config else None,
            claude_subscription=bool(job["claude_account"]),
            # Jobs saved before the option existed lack it.
            unsandboxed=job.get("unsandboxed", False),
            directory=str(path),
        )
    else:
        staged = machines.put_file(
            home,
            machine,
            machines.remote_path(REMOTE_PROMPTS),
            f"{run_id}-{uuid.uuid4().hex[:8]}",
            brief.encode(),
            keep=PROMPT_KEEP,
        )
        command = remote_command(job, machine, found, staged, effort)
    # A fresh pane, as resumes use; the root pane stays a plain shell for the user.
    pane = herdr(
        "pane",
        "split",
        anchor,
        "--direction",
        "right",
        "--cwd",
        str(path),
        "--no-focus",
        machine=machine,
    )["pane"]["pane_id"]
    launched = {
        "machine": job.get("machine"),
        "workspace_id": workspace,
        "pane": pane,
        "path": str(path),
        "url": collie_url(machine, workspace),
        "exit_marker": marker,
        "launched_at": time.time(),
    }
    if machine is not None:
        # The viewer and the run's card name the worker's workspace by its qualified ID.
        launched.update(
            workspace_id=machines.qualify(machine.id, workspace), machine_label=machine.label
        )
    try:
        run(*machines.herdr_argv(machine, "pane", "run", pane, sandbox.typed(command)))
    except subprocess.TimeoutExpired:
        return {**launched, "uncertain": "herdr did not answer while typing the command"}
    except (OSError, ValueError, subprocess.SubprocessError):
        close_pane(pane, machine)  # Nothing was typed: the pane is an empty shell.
        raise
    # From here the command is typed: trouble makes the outcome uncertain, never a failure
    # to start, since the agent may be running.
    deadline = time.monotonic() + START_WAIT
    try:
        while time.monotonic() < deadline:
            if any(
                a.get("pane_id") == pane and a.get("agent") == job["agent"]
                for a in herdr("agent", "list", machine=machine)["agents"]
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
    """What ``workspace_exit`` reads about a launch; on a worker, the workspace as its herdr
    names it, and the agent's own pane."""
    op = {
        "status": "complete",
        "exit_marker": launched["exit_marker"],
        "path": launched["path"],
        "result": {"workspace_id": launched["workspace_id"]},
        "agent": job["agent"],
        "claude_account": job["claude_account"] or None,
        "created_at": launched["launched_at"],
        "updated_at": launched["launched_at"],
    }
    if launched.get("machine"):
        op["result"]["workspace_id"] = machines.split(launched["workspace_id"])[1]
        op["pane_id"] = launched["pane"]
    return op


def final_response(record, agent, marker, machine=None, home=None):
    """The agent's last answer in the session the watch followed, without the marker."""
    target = (record or {}).get("target") or {}
    if not target.get("rollout") or not target.get("session_id"):
        return None
    try:
        _, text = handoff.latest_turn(target["rollout"], target["session_id"], agent, machine, home)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        return None
    if not text:
        return None
    return without_marker(text, marker)


def without_marker(text, marker):
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


def close_pane(pane, machine=None, shell=None):
    """Close a finished run's pane once only its shell is left in it; ``shell``, when
    named, must be that shell, so a pane id reused since is left alone."""
    try:
        if machine is None:
            procs = handoff.result("pane", "process-info", "--pane", pane)["process_info"]
        else:
            procs = herdr("pane", "process-info", "--pane", pane, machine=machine)["process_info"]
        if (shell is not None and procs.get("shell_pid") != shell) or any(
            p.get("pid") != procs.get("shell_pid") for p in procs["foreground_processes"]
        ):
            return False
        run(*machines.herdr_argv(machine, "pane", "close", pane))
        return True
    except (OSError, RuntimeError, ValueError, KeyError, subprocess.SubprocessError):
        return False
