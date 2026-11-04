"""Move a workspace's agent to a worker: its branch, uncommitted work and conversation.

A move takes one Claude or Codex agent of a herdr workspace on this machine to a
registered worker, where it continues the same session in the same branch:

1. The agent and its checkout are read and checked (``source``): a blocked agent, a
   draft in its composer, another agent in the workspace, a main clone, a detached
   head, submodules or an owner that would resume the session elsewhere refuse it.
   A working agent moves only once the reader confirmed stopping it (``confirm``); its
   child processes, which exiting it stops, are listed first.
2. The target is placed as a launch would be, never on this machine (``target``).
3. A working agent's turn is stopped with Esc (``agent_messages.interrupt``).
4. The branch's commits that no remote branch here has, its index and its whole working
   tree (untracked files too, ignored ones not) travel as one ``git bundle`` through ssh
   (``snapshot``, ``bundle``); the worker's ``move_receive`` probe op checks the branch
   out in a new worktree of its own clone, where launches put theirs, and answers the
   trees it made, which must equal this checkout's.
5. Only then is the agent here exited (``agent_messages.exit_agent``). Its checkout is
   read again: a change since is sent once more.
6. Its transcript (Claude's subagent ones too) goes to the worker's store, recorded in
   the new checkout (``move_session``), and the session is resumed there in its sandbox
   (``agent_messages.resume``) with a first message saying it was moved.
7. A babysit watch of the workspace, still waiting for release, follows it: stopped
   here first so nothing resumes the session meanwhile, then registered on the worker.
8. Once the worker's herdr shows the agent on that session, this machine's workspace,
   worktree and branch go through the Workspaces tab's cleanup.

Before the exit any failure leaves the agent here running (stopped, if it was working)
and removes the worker's new checkout; after it, the checkout here stays and the
result says how to resume the session here again. Nothing is pushed anywhere.
"""

import contextlib
import io
import json
import os
import re
import shlex
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import uuid
from argparse import Namespace
from pathlib import Path

import agent_docker
import agent_messages
import herdr_handoff
import machines
import owned_process
import placement
import remote_probe
import safe_git
import session_locks
import workspace_viewer
import wt
from pr_workspaces import PANE, SLUG, collie_url, remote_slugs, repo_config

STAGED = remote_probe.MOVE_STATE
STAGE_KEEP = 86400
RECEIVE_TIMEOUT = 1800
SESSION_TIMEOUT = 600
# How long the resumed agent may take to show in the worker's herdr, past the resume's.
CONFIRM_WAIT = 60
CLEANUP_WAIT = 600
CLEANUP_POLL = 1.0
# The system's ps, never one earlier on PATH.
PS = "/bin/ps"
MAX_SHOWN = 20
# A watch whose agent still runs interactively: it follows the agent to the worker.
MOVABLE_WATCHES = {"awaiting_release", "paused"}
LABEL = {"claude": "Claude", "codex": "Codex"}
REQUEST = {"workspace", "pane", "session", "machine", "confirm_working"}
# Snapshot commits are the move's own, never signed and never anyone's work.
IDENTITY = {
    "GIT_AUTHOR_NAME": "babysit-pr",
    "GIT_AUTHOR_EMAIL": "babysit-pr@localhost",
    "GIT_COMMITTER_NAME": "babysit-pr",
    "GIT_COMMITTER_EMAIL": "babysit-pr@localhost",
}
CODEX_DAY = re.compile(r".*/sessions/([0-9]{4}/[0-9]{2}/[0-9]{2})$")
# Ignored paths a move leaves behind and its cleanup may remove: anything Git ignores
# outside these keeps the checkout here, since it was not copied.
REBUILDABLE = {
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".tox",
    ".nox",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".cache",
    ".eggs",
    ".parcel-cache",
    ".next",
    ".turbo",
    "build",
    "dist",
    "target",
}
LFS = re.compile(rb"\bfilter\s*=\s*lfs\b")
CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
# An MCP server's program or package name: `@sentry/mcp-server`, `mcp-server.cjs`,
# `sentry-mcp`, `mcp_server_git`.
MCP = re.compile(r"(?i)(?:^|[-_.@/])mcp(?:$|[-_.@])")
# Programs that run the script, module or package named next on their command line.
RUNNERS = {"node", "bun", "deno", "python", "python3", "uv", "uvx", "npx", "bunx", "pnpx"}
PACKAGE_RUNNERS = {"npm", "pnpm", "yarn"}  # With exec, x or dlx.
VALUED = {"-m", "-p", "--package", "--from", "--with", "-r", "--require", "--import"}
KEPT_MOVES = 20


class Refused(ValueError):
    """Nothing was done; ``code`` and the details tell the dashboard what to offer."""

    def __init__(self, message, code, **details):
        super().__init__(message)
        self.details = {"code": code, **details}


class Failed(RuntimeError):
    """The move stopped part way; ``details`` say where and what was kept."""

    def __init__(self, message, **details):
        super().__init__(message)
        self.details = details


def git(path, *args, env=None, timeout=300):
    """Hardened Git in the checkout being moved."""
    argv, environment = safe_git.prepare(str(path), *args, env={**os.environ, **(env or {})})
    done = owned_process.run(argv, env=environment, text=True, timeout=timeout)
    if done.returncode:
        raise ValueError((done.stderr or done.stdout or "Git failed").strip()[-2000:])
    return done.stdout.strip()


@contextlib.contextmanager
def index_copy(path):
    """The environment pointing Git at a copy of the checkout's index: what is read or
    staged through it never takes the live index's lock under a working agent."""
    index = git(path, "rev-parse", "--path-format=absolute", "--git-path", "index")
    descriptor, temporary = tempfile.mkstemp(prefix=".babysit-move-", dir=os.path.dirname(index))
    os.close(descriptor)
    try:
        if os.path.isfile(index):
            Path(temporary).write_bytes(Path(index).read_bytes())
        else:
            os.unlink(temporary)
        yield {"GIT_INDEX_FILE": temporary}
    finally:
        with contextlib.suppress(OSError):
            os.unlink(temporary)


def tree_of(path):
    """The tree ``git add -A`` would stage: the index and every file Git does not ignore.

    Read through a copy of the index, which stays as it was; the worker reads its new
    checkout's the same way (``remote_probe._host_tree``).
    """
    with index_copy(path) as env:
        git(path, "add", "-A", env=env)
        return git(path, "write-tree", env=env)


def snapshot(path):
    """The state a move carries and compares: branch, head, index tree, working tree."""
    try:
        branch = git(path, "symbolic-ref", "--quiet", "--short", "HEAD")
    except ValueError:
        raise Refused(
            "Its checkout has no branch checked out; check one out first", "detached"
        ) from None
    try:
        with index_copy(path) as env:
            index_tree = git(path, "write-tree", env=env)
    except ValueError:
        raise Refused(
            "Its checkout has unresolved merge conflicts; resolve them first", "conflicts"
        ) from None
    return {
        "path": os.path.realpath(path),
        "branch": branch,
        "head": git(path, "rev-parse", "--verify", "HEAD"),
        "index_tree": index_tree,
        "work_tree": tree_of(path),
    }


def same_state(here, there):
    return all(here[k] == there[k] for k in ("branch", "head", "index_tree", "work_tree"))


def changes(path):
    """Counts of uncommitted changes, untracked files and commits no remote branch has."""
    listing = git(path, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    entries = [e for e in listing.split("\0") if len(e) > 3 and e[2] == " "]
    untracked = sum(1 for e in entries if e.startswith("??"))
    unpushed = int(git(path, "rev-list", "--count", "HEAD", "--not", "--remotes") or 0)
    return {"changed": len(entries) - untracked, "untracked": untracked, "unpushed": unpushed}


def rebuildable(name):
    return any(part in REBUILDABLE or part.endswith(".egg-info") for part in name.split("/"))


def ignored_kept(path):
    """What Git ignores in the checkout that is not rebuildable: a move does not copy it,
    so it keeps the checkout here. Folders are named once, with a trailing slash."""
    listing = git(path, "ls-files", "-z", "-o", "-i", "--exclude-standard", "--directory")
    return [name for name in listing.split("\0") if name and not rebuildable(name.rstrip("/"))]


DEFAULT_BRANCHES = ("main", "master", "dev", "develop", "trunk")


def base_branch(path):
    """The remote default branch a worker's clone has, or can fetch from GitHub: the
    ``upstream`` remote's, else ``origin``'s, as (ref, repo, branch); None without one.

    Only its history is left out of a move's bundle. A branch's own remote-tracking
    commits, and a fork's that only this clone has a remote for (a review checkout's),
    may be on no remote the worker knows, so they travel in the bundle."""
    remotes = remote_slugs(repo_config(path))
    for remote in ("upstream", "origin"):
        if remote not in remotes:
            continue
        try:
            head = git(path, "symbolic-ref", "--quiet", f"refs/remotes/{remote}/HEAD")
        except ValueError:
            head = ""
        named = head.removeprefix(f"refs/remotes/{remote}/") if head else None
        for name in dict.fromkeys([named, *DEFAULT_BRANCHES]):
            if not name or not remote_probe.BRANCH.fullmatch(name):
                continue
            ref = f"refs/remotes/{remote}/{name}"
            try:
                git(path, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
            except ValueError:
                continue
            return ref, remotes[remote], name
    return None


def history_size(path):
    """Bytes of the clone's objects, packed and loose: what a bundle of its whole history
    comes to at most."""
    counts = dict(
        line.split(": ", 1)
        for line in git(path, "count-objects", "-v").splitlines()
        if ": " in line
    )
    return sum(int(counts.get(key, 0) or 0) for key in ("size", "size-pack")) * 1024


def bundle(path, state, move_id):
    """The bundle of ``state``: one ref, a commit of the working tree on a commit of the
    index on the branch's head, without the history of the repository's default branch
    (``base_branch``), which the worker has or fetches.

    Also the ``[repo, branch]`` of that default branch, for a worker clone behind it to
    fetch. The ref is gone again once it is written.
    """
    signing = ["--no-gpg-sign"]
    index = git(
        path,
        "commit-tree",
        *signing,
        state["index_tree"],
        "-p",
        state["head"],
        "-m",
        "babysit-pr move: index",
        env=IDENTITY,
    )
    work = git(
        path,
        "commit-tree",
        *signing,
        state["work_tree"],
        "-p",
        index,
        "-m",
        "babysit-pr move: working tree",
        env=IDENTITY,
    )
    base = base_branch(path)
    # Without a known default branch, the whole history goes, up to the size limit.
    leave_out = ["--not", base[0]] if base else []
    ref = f"refs/babysit-move/{move_id}"
    folder = Path(tempfile.mkdtemp(prefix="babysit-move-"))
    file = folder / "move.bundle"
    git(path, "update-ref", ref, work)
    try:
        git(path, "bundle", "create", "-q", str(file), ref, *leave_out, timeout=1200)
        data = file.read_bytes()
    finally:
        with contextlib.suppress(ValueError):
            git(path, "update-ref", "-d", ref)
        with contextlib.suppress(OSError):
            file.unlink()
            folder.rmdir()
    if len(data) > remote_probe.MAX_MOVE_BUNDLE:
        raise Refused(
            f"Its branch and changes come to {len(data) >> 20} MB, more than a move takes",
            "too_large",
        )
    return data, [[base[1], base[2]]] if base else []


def descendants(listing, pid):
    """The processes under ``pid`` in a ``ps -o pid=,ppid=,args=`` listing, nearest first."""
    under: dict[int, list[int]] = {}
    commands: dict[int, str] = {}
    for line in listing.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 2 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        child, parent = int(parts[0]), int(parts[1])
        commands[child] = CONTROL.sub("", parts[2] if len(parts) > 2 else "")[:200]
        under.setdefault(parent, []).append(child)
    found: list[dict] = []
    queue, seen = [(child, pid) for child in under.get(pid, [])], {pid}
    while queue and len(found) < 500:
        child, parent = queue.pop(0)
        if child in seen:
            continue
        seen.add(child)
        found.append({"pid": child, "ppid": parent, "command": commands[child]})
        queue.extend((grandchild, child) for grandchild in under.get(child, []))
    return found


def served(command):
    """The name an agent's child runs by: its program's, or for a runner (node, npx,
    `npm exec`, uvx, `python -m`) the script, module or package it runs."""
    words = command.split()
    if not words:
        return ""
    program = Path(words[0]).name
    rest = words[1:]
    if program in PACKAGE_RUNNERS:
        if not rest or rest[0] not in {"exec", "x", "dlx"}:
            return program
        rest = rest[1:]
    elif program not in RUNNERS:
        return program
    if program == "uv" and rest[:1] == ["run"]:
        rest = rest[1:]
    while rest:
        word = rest.pop(0)
        if word == "--":
            continue
        if word in {"-m", "--from"} and rest:
            return Path(rest[0]).name  # The module or package run.
        if word in VALUED and rest:
            rest.pop(0)
        elif not word.startswith("-"):
            return Path(word).name if "/" in word and not word.startswith("@") else word
    return program


def mcp_servers(found, agent):
    """Split the agent's processes into those its exit stops for good and its MCP
    servers (with what they started), which end and start again with the agent.

    Only the agent's own children are servers, named so by what they run (``served``):
    a test or build that merely mentions MCP in its arguments is listed as stopped."""
    servers: set[int] = set()
    stopped, restarting = [], []
    for process in found:
        if process["ppid"] in servers or (
            process["ppid"] == agent and MCP.search(served(process["command"]))
        ):
            servers.add(process["pid"])
            restarting.append(process)
        else:
            stopped.append(process)
    return stopped, restarting


def split(found, agent):
    """``mcp_servers`` of a process listing that may be unknown (None)."""
    return (None, None) if found is None else mcp_servers(found, agent)


def children(pid):
    """What the agent started and its exit stops (background shells, tests, servers);
    None when this machine cannot list its processes."""
    try:
        listing = subprocess.run(
            [PS, "-A", "-o", "pid=,ppid=,args="],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return descendants(listing, pid)


def settings(argv, kind, rollout=None):
    """The model and effort the agent was started with, as `wt` would pass them again;
    a Codex session's last turn says what its command line does not."""
    model = effort = ""
    words = iter(argv[1:])
    for word in words:
        name, joined, value = word.partition("=")
        if not joined and name in {"--model", "-m", "--effort", "-c", "--config"}:
            value = next(words, "")
        if name == "--model" or (name == "-m" and kind == "codex"):
            model = value
        elif name == "--effort" and kind == "claude":
            effort = value
        elif name in {"-c", "--config"} and kind == "codex":
            key, _, setting = value.partition("=")
            if key.strip() == "model_reasoning_effort":
                effort = setting.strip().strip("\"'")
            elif key.strip() == "model":
                model = setting.strip().strip("\"'")
    if kind == "codex" and rollout and not (model and effort):
        context = {}
        with contextlib.suppress(OSError):
            for line in Path(rollout).read_text(errors="replace").splitlines():
                if '"turn_context"' in line:
                    with contextlib.suppress(ValueError):
                        item = json.loads(line)
                        if item.get("type") == "turn_context" and isinstance(
                            item.get("payload"), dict
                        ):
                            context = item["payload"]
        model = model or str(context.get("model") or "")
        effort = effort or str(context.get("effort") or "")
    efforts = wt.EFFORTS | (wt.CODEX_EFFORTS if kind == "codex" else set())
    return {
        "model": model if wt.MODEL.fullmatch(model) else "",
        "effort": effort if effort in efforts else "",
    }


def related_watch(home, session, root):
    """The unfinished babysit watch of this session or checkout, if any; more is refused."""
    found = [
        job
        for job in agent_messages.watch_jobs(home)
        if job.get("status") not in agent_messages.ENDED_WATCHES
        and (
            job.get("session_id") == session
            or (
                job.get("machine") is None
                and os.path.realpath(str(job.get("cwd") or "/nonexistent")) == root
            )
        )
    ]
    if len(found) > 1:
        raise Refused("Several babysit watches use this workspace; stop all but one", "watches")
    return found[0] if found else None


def source(home, request):
    """The agent to move and its checkout, read now, or Refused with the reason."""
    workspace = request.get("workspace")
    if not isinstance(workspace, str) or not workspace:
        raise ValueError("Supply a workspace")
    for key in ("pane", "session"):
        if request.get(key) is not None and not isinstance(request[key], str):
            raise ValueError(f"Invalid {key}")
    try:
        spelled = str(workspace_viewer.workspace_checkout(workspace))
        listed = agent_messages.agents(workspace, interactions=True)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError(f"herdr could not list the workspace: {exc}") from exc
    root = os.path.realpath(spelled)
    if request.get("pane"):
        target = next((a for a in listed if a["pane"] == request["pane"]), None)
    else:
        movable = [a for a in listed if a.get("agent") in LABEL]
        target = movable[0] if len(movable) == 1 else None
        if len(movable) > 1:
            raise Refused("Several agents run in this workspace; choose one", "choose")
    if target is None or (request.get("session") and target.get("session") != request["session"]):
        raise Refused("That agent is no longer running in this workspace; refresh", "changed")
    kind, pane, session = target.get("agent"), target["pane"], target.get("session")
    if kind not in LABEL:
        raise Refused("Only Claude and Codex agents can be moved", "agent")
    others = [a for a in listed if a["pane"] != pane]
    if others:
        raise Refused(
            f"Another agent runs in this workspace ({others[0]['pane']}); quit it first, since "
            "the workspace closes once this one has moved",
            "others",
        )
    try:
        busy = busy_panes(workspace, pane)
    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
        raise ValueError(f"herdr could not list the workspace's panes: {exc}") from exc
    if busy:
        raise Refused(
            "The workspace closes once the agent has moved, and its other panes are running "
            f"{', '.join(busy)}; finish or close them first",
            "panes",
            panes=busy,
        )
    if not isinstance(session, str) or not remote_probe.UUID.fullmatch(session.lower()):
        raise Refused("herdr names no session for this agent; it cannot be resumed", "session")
    status = target.get("status")
    if status == "blocked" and not (
        kind == "claude" and (target.get("interaction") or {}).get("idle")
    ):
        raise Refused("The agent is waiting on a question or approval; answer it first", "blocked")
    try:
        info, procs, _ = herdr_handoff.inspect(pane)
        screen = herdr_handoff.read_screen(pane)
    except RuntimeError as exc:
        raise ValueError(f"herdr could not read the pane: {str(exc)[-300:]}") from exc
    found = [p for p in procs["foreground_processes"] if herdr_handoff.program(p) == kind]
    if info.get("agent") != kind or len(found) != 1 or not isinstance(found[0].get("argv"), list):
        raise Refused("Could not identify the agent's process; refresh", "process")
    proc = found[0]
    if proc["pid"] == procs["shell_pid"]:
        raise Refused("The agent is the pane's root process; quitting it would close it", "root")
    # What the exit would refuse, refused before any key (a working agent's Esc) too.
    if (pane, proc["pid"]) in agent_messages._exiting:
        raise Refused("An exit was already attempted for this agent; check it in Collie", "exit")
    if not agent_messages.interactive_shell(procs["shell_pid"]):
        raise Refused(
            "The pane's root process is not an interactive shell, so the pane could close "
            "with the agent",
            "root",
        )
    if info.get("scroll", {}).get("offset_from_bottom", 0) != 0:
        raise Refused("The terminal is scrolled; scroll it back down first", "scrolled")
    if agent_messages.has_draft(screen, kind):
        raise Refused("The composer holds a draft; send or clear it first", "draft")
    watched = agent_messages.exit_watcher(pane, session, home)
    if watched:
        raise Refused(f"This agent quits by itself once its task is done ({watched})", "exit")
    chosen = next(
        (s for s in workspace_viewer.sessions(root) if s["id"] == session and s["agent"] == kind),
        None,
    )
    if chosen is None:
        raise Refused("Its session is not recorded in this checkout yet; try again", "session")
    if session_locks.held(kind, session, chosen.get("claude_config_dir")):
        raise Refused(f"Not moved: {agent_messages.REPAIR_RUNNING}", "repair")
    try:
        common = safe_git.common_dir(root)
    except ValueError as exc:
        raise Refused(str(exc), "checkout") from exc
    if common == os.path.join(root, ".git"):
        raise Refused("A clone's own checkout cannot be moved, only its worktrees", "main")
    if any(e.startswith("160000 ") for e in git(root, "ls-files", "--stage", "-z").split("\0")):
        raise Refused("Its checkout contains submodules, which a move cannot carry", "submodules")
    state = snapshot(root)
    check_tree(root, state["work_tree"])
    try:
        remote_probe._branch(state["branch"], "moved")
    except ValueError:
        raise Refused(
            f"Branch {state['branch']} cannot be checked out on a worker", "branch"
        ) from None
    if not remote_probe.MOVE_PART.fullmatch(Path(root).name):
        raise Refused(f"A worker cannot name a checkout {Path(root).name!r}", "name")
    if kind == "codex" and not CODEX_DAY.fullmatch(Path(chosen["_file"]).parent.as_posix()):
        raise Refused("Its Codex rollout is not in a sessions/YYYY/MM/DD folder", "rollout")
    if base_branch(root) is None and history_size(root) > remote_probe.MAX_MOVE_BUNDLE:
        # Its whole history would have to travel: refused now, not after a stopped turn.
        raise Refused(
            "No default branch of its upstream or origin remote is known here, and its "
            "whole history is more than a move takes; run `git remote set-head upstream "
            "--auto` (or origin) in its clone first",
            "base",
        )
    config = repo_config(root)
    remotes = remote_slugs(config)
    slug = remotes.get("upstream") or remotes.get("origin")
    if not slug or not SLUG.fullmatch(slug):
        raise Refused("Its clone has no GitHub upstream or origin remote", "remote")
    upstream = None
    remote, merge = (config.get(f"branch.{state['branch']}.{k}") for k in ("remote", "merge"))
    if (
        remote in remotes
        and remote_probe.MOVE_PART.fullmatch(remote)
        and merge
        and merge.startswith("refs/heads/")
        and remote_probe.BRANCH.fullmatch(merge.removeprefix("refs/heads/"))
        and ".." not in merge
    ):
        merged = merge.removeprefix("refs/heads/")
        try:
            # Where this clone's remote-tracking ref has it, for a remote the worker adds.
            tip = git(root, "rev-parse", "--verify", "--quiet", f"refs/remotes/{remote}/{merged}")
        except ValueError:
            tip = None
        upstream = [remote, merged, remotes[remote], tip or None]
    account = chosen.get("claude_account") if kind == "claude" else None
    if kind == "claude" and account is None:
        raise Refused("Its Claude home is not a named account here", "account")
    watch = related_watch(home, session, root)
    if watch is not None and watch.get("status") not in MOVABLE_WATCHES:
        raise Refused(
            f"babysit watch {watch.get('id')} ({watch.get('status')}) resumes this session "
            "itself; stop it first",
            "watch",
        )
    try:
        docker = sys.platform == "darwin" and agent_docker.has_access(proc["pid"])
    except ValueError:
        docker = False
    return {
        "workspace": workspace,
        "pane": pane,
        "session": session,
        "agent": kind,
        "status": "idle" if status == "blocked" else status,
        "root": root,
        "spelled": spelled,
        "cwd": proc.get("cwd") or root,
        "pid": proc["pid"],
        "argv": proc["argv"],
        "chosen": chosen,
        "account": account,
        "state": state,
        "changes": changes(root),
        "slug": slug,
        "upstream": upstream,
        "watch": watch,
        "docker": docker,
        **dict(zip(("children", "mcp"), split(children(proc["pid"]), proc["pid"]), strict=True)),
        "ignored_kept": ignored_kept(root),
        "suffix": project_suffix(kind, chosen, [root, spelled]),
        **settings(proc["argv"], kind, chosen.get("_file") if kind == "codex" else None),
    }


def busy_panes(workspace, pane):
    """The workspace's other panes running something beyond their shell, by pane and
    program: closing the workspace would end it."""
    live = workspace_viewer.live_workspace(workspace)
    panes = herdr_handoff.result("pane", "list", "--workspace", live)["panes"] if live else []
    busy = []
    for other in panes:
        name = other.get("pane_id") if isinstance(other, dict) else None
        if not name or name == pane:
            continue
        procs = herdr_handoff.result("pane", "process-info", "--pane", name)["process_info"]
        running = [
            herdr_handoff.program(p)
            for p in procs.get("foreground_processes") or []
            if p.get("pid") != procs.get("shell_pid")
        ]
        if running:
            busy.append(f"{', '.join(running)} in {name}")
    return busy


def check_tree(root, tree):
    """Refuse a working tree a worker cannot reproduce from Git alone."""
    attributes = []
    for entry in git(root, "ls-tree", "-r", "-z", tree).split("\0"):
        meta, _, name = entry.partition("\t")
        fields = meta.split()
        if len(fields) != 3:
            continue
        if fields[1] == "commit":
            # An untracked clone inside the checkout would travel as a commit it lacks.
            raise Refused(
                f"Its checkout holds another Git repository ({name}), which a move cannot carry",
                "nested",
            )
        if fields[1] == "blob" and Path(name).name == ".gitattributes":
            attributes.append(fields[2])
    for blob in attributes:
        if LFS.search(git_bytes(root, "cat-file", "blob", blob)):
            raise Refused(
                "Its checkout uses Git LFS, whose files a worker would get as pointers", "lfs"
            )


def git_bytes(path, *args):
    argv, environment = safe_git.prepare(str(path), *args)
    done = owned_process.run(argv, env=environment, timeout=120)
    if done.returncode:
        raise ValueError(done.stderr.decode(errors="replace").strip()[-2000:] or "Git failed")
    return done.stdout


def project_suffix(kind, chosen, roots):
    """What Claude's project folder adds to the checkout's for a session started below it
    (``-frontend``); empty otherwise."""
    if kind != "claude":
        return ""
    folder = Path(chosen["_file"]).parent.name
    for root in roots:
        name = re.sub(r"[^A-Za-z0-9]", "-", root)
        if folder == name or folder.startswith(name + "-"):
            suffix = folder[len(name) :]
            if remote_probe.MOVE_SUFFIX.fullmatch(suffix):
                return suffix
    raise Refused("Its transcript is not in the checkout's Claude project folder", "transcript")


def placement_request(src, override):
    return {
        "kind": "scratch",
        "repo": src["slug"],
        "agent": src["agent"],
        "claude_account": None if src["account"] in (None, "default") else src["account"],
        "docker": src["docker"],
        "sandboxed": True,
        "override": override,
    }


def target(home, workspaces, src, wanted):
    """Where the agent would go: (options, machine or None, why not), as New task places.

    Never this machine; a machine that cannot take it or is full is refused, as are
    labels it lacks (its sandbox, the Claude account, Docker, the repository's needs).
    """
    if wanted not in (None, "auto") and (
        not isinstance(wanted, str) or not machines.ID.fullmatch(wanted)
    ):
        raise ValueError("Choose a machine or Auto")
    config, loads = workspaces.loads.snapshot()
    reservations = workspaces.reservations()
    req = placement.request(
        placement_request(src, wanted or "auto"), config["repos"], config.get("docker")
    )
    found = placement.exclusions(req, config["machines"], loads, reservations, time.time())

    def full(machine_id):
        return machine_id in found and found[machine_id].why.startswith("full")

    options = [
        {
            "id": machine_id,
            "label": machine.label,
            "note": found[machine_id].why
            if machine_id in found
            else placement.free(machine_id, config["machines"], loads, reservations),
            "disabled": machine_id in found and (not found[machine_id].soft or full(machine_id)),
        }
        for machine_id, machine in config["machines"].items()
    ]
    try:
        chosen = placement.choose(req, config["machines"], loads, {}, reservations, local=False)
    except ValueError as exc:
        return options, None, str(exc)
    if full(chosen.machine):
        label = placement.label(chosen.machine, config["machines"])
        return options, None, f"{label}: {found[chosen.machine].why}"
    machine = machines.resolve(home, chosen.machine, placing=True)
    return options, {"machine": machine, "reason": chosen.reason}, None


def public(src):
    """What the dashboard shows of a source before it is moved."""
    shown = src["children"] or []
    return {
        "workspace": src["workspace"],
        "pane": src["pane"],
        "session": src["session"],
        "agent": src["agent"],
        "status": src["status"],
        "path": src["root"],
        "branch": src["state"]["branch"],
        "head": src["state"]["head"],
        **src["changes"],
        "children": shown[:MAX_SHOWN],
        "children_count": None if src["children"] is None else len(shown),
        # They end with the agent and start again with it on the worker.
        "mcp_count": len(src["mcp"] or []),
        "watch": None
        if src["watch"] is None
        else {"id": src["watch"].get("id"), "status": src["watch"].get("status")},
        "docker": src["docker"],
        # Not copied, and not rebuildable: they keep the checkout here after the move.
        "ignored_kept": src["ignored_kept"][:MAX_SHOWN],
        "ignored_kept_count": len(src["ignored_kept"]),
        "model": src["model"] or None,
        "effort": src["effort"] or None,
        "confirm": src["status"] == "working",
    }


def check_watch(src, machine):
    """A watch follows the agent only where a worker watch can be registered."""
    watch = src["watch"]
    if watch is None:
        return
    if machine.os != "linux":
        raise Refused(
            f"babysit watch {watch.get('id')} follows this workspace, and watches on "
            f"{machine.label} need a Linux worker; stop the watch first",
            "watch",
        )
    if src["changes"]["changed"] or src["changes"]["untracked"]:
        raise Refused(
            f"babysit watch {watch.get('id')} follows this workspace, and a watch on a worker "
            "needs a clean worktree; commit the changes or stop the watch first",
            "watch",
        )


def plan(home, request, workspaces):
    """What a move of this agent would do and where, without changing anything."""
    src = source(home, request)
    options, chosen, error = target(home, workspaces, src, request.get("machine"))
    checked = None
    if chosen is not None:
        try:
            check_watch(src, chosen["machine"])
            checked = check_worker(home, chosen["machine"], src)
        except (Refused, ValueError) as exc:
            chosen, error = None, str(exc)
    return {
        **public(src),
        # The worker's own answer: its clone, and where the checkout would go.
        "worker": checked,
        "options": options,
        "machine": chosen["machine"].id if chosen else None,
        "label": chosen["machine"].label if chosen else None,
        "reason": chosen["reason"] if chosen else None,
        "error": error,
    }


def worker_paths(machine, src):
    name = src["slug"].split("/")[1]
    return {
        "clone": f"{machine.src.rstrip('/')}/{name}",
        "repo": src["slug"],
        "root": f"{machine.src.rstrip('/')}/worktrees/{name}",
        "name": Path(src["root"]).name,
        "branch": src["state"]["branch"],
        "agent": src["agent"],
        "account": src["account"],
        "sandbox": placement.SANDBOX[machine.os],
    }


def check_worker(home, machine, src):
    """Whether the worker can make the checkout (its clone, a free branch and path, the
    Claude account) and which remote its branch would track there: asked before any key,
    so a refusal there changes nothing here.

    A babysit watch that follows the workspace needs that upstream (a review checkout
    is matched to its pull request by it), so one the worker cannot set refuses here."""
    try:
        answer = remote_probe.call(
            home,
            machine,
            "move_check",
            60,
            **worker_paths(machine, src),
            upstream=src["upstream"],
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError(f"{machine.label} did not answer within 60 seconds") from exc
    except OSError as exc:
        raise ValueError(f"{machine.label} could not be reached: {exc}") from exc
    if src["watch"] is not None and src["upstream"] and answer["upstream"] is None:
        raise Refused(
            f"babysit watch {src['watch'].get('id')} follows this workspace and needs its "
            f"branch's upstream, which cannot be set on {machine.label}: "
            f"{answer['upstream_note']}; stop the watch first",
            "watch",
        )
    return answer


def message(src, machine, path, stopped, watch, made=None):
    """The first message the agent reads on the worker."""
    state = src["state"]
    gone = "Background jobs, shells and other processes it ran before the move are gone"
    if stopped:
        names = ", ".join(c["command"].split(" ", 1)[0] for c in stopped[:8])
        gone += f" ({len(stopped)} stopped here: {names})"
    lines = [
        f"[babysit-pr] This conversation was moved from {local_label()} to {machine.label} "
        "by the dashboard and resumed there.",
        f"The worktree moved from {src['root']} to {path}: branch {state['branch']} at "
        f"{state['head'][:12]}, with the same staged, unstaged and untracked changes. Files "
        "Git ignores (virtualenvs, node_modules, build output) were not copied.",
        f"{gone}; re-run any you still need, and set up ignored dependencies again if a "
        "command needs them.",
    ]
    if made is not None and made.get("upstream") is None:
        # Said rather than discovered at the first push or pull.
        if src["upstream"]:
            lines.append(
                f"Branch {state['branch']} tracked {src['upstream'][1]} of "
                f"{src['upstream'][2]} before the move and has no upstream here "
                f"({made.get('upstream_note') or 'it could not be set'}); add the remote "
                "and set it before pushing or pulling."
            )
        else:
            lines.append(f"Branch {state['branch']} has no upstream, as before the move.")
    if watch:
        lines.append(f"The babysit watch {watch} now follows this worktree on {machine.label}.")
    lines.append("Continue the task.")
    return "\n\n".join(lines)


def local_label():
    return "this Mac" if sys.platform == "darwin" else "the controller"


def resume_here(src):
    """The command that resumes the session in its checkout here again."""
    words = (
        ["claude", "--resume", src["session"]]
        if src["agent"] == "claude"
        else ["codex", "resume", src["session"]]
    )
    config = src["chosen"].get("claude_config_env") if src["agent"] == "claude" else None
    prefix = f"CLAUDE_CONFIG_DIR={shlex.quote(config)} " if config else ""
    return f"cd {shlex.quote(src['cwd'])} && {prefix}{shlex.join(words)}"


def moved_cwd(value, old, new):
    for prefix in old:
        if value == prefix or value.startswith(prefix + "/"):
            return new + value[len(prefix) :]
    return None


def rewrite(data, old, new):
    """A JSONL transcript with each recorded ``cwd`` under ``old`` moved under ``new``:
    Claude records one on each turn, Codex in its session metadata and turn contexts."""
    out = []
    for line in data.splitlines(keepends=True):
        try:
            item = json.loads(line)
        except ValueError:
            out.append(line)
            continue
        changed = False
        for holder in (item, item.get("payload") if isinstance(item, dict) else None):
            if isinstance(holder, dict) and isinstance(holder.get("cwd"), str):
                moved = moved_cwd(holder["cwd"], old, new)
                if moved is not None:
                    holder["cwd"], changed = moved, True
        if changed:
            ending = b"\n" if line.endswith(b"\n") else b""
            # Escaped: a lone surrogate in what the agent read must not stop the copy.
            line = json.dumps(item, separators=(",", ":")).encode() + ending
        out.append(line)
    return b"".join(out)


def session_files(src):
    """The session's files to move, by their name in the store: Claude's transcript and
    its folder (subagents, tool results), or Codex's rollout. Names the worker would
    refuse are left out."""
    main = Path(src["chosen"]["_file"])
    found = [(main.name, main)]
    extra = main.parent / src["session"]
    if src["agent"] == "claude" and extra.is_dir() and not extra.is_symlink():
        for file in sorted(extra.rglob("*")):
            name = f"{src['session']}/{file.relative_to(extra).as_posix()}"
            parts = name.split("/")
            if (
                not file.is_symlink()
                and file.is_file()
                and len(parts) <= remote_probe.MOVE_DEPTH
                and all(remote_probe.MOVE_PART.fullmatch(part) for part in parts)
            ):
                found.append((name, file))
    return found


def session_size(src):
    return sum(file.stat().st_size for _, file in session_files(src))


def transcripts(src, path):
    """The session's files as a tar for ``move_session``, and the Codex rollout's day."""
    main = Path(src["chosen"]["_file"])
    old = list(dict.fromkeys([src["root"], src["spelled"]]))
    files = [
        (
            name,
            rewrite(file.read_bytes(), old, path) if name.endswith(".jsonl") else file.read_bytes(),
        )
        for name, file in session_files(src)
    ]
    day = None
    if src["agent"] == "codex":
        matched = CODEX_DAY.fullmatch(main.parent.as_posix())
        if not matched:
            raise Refused("Its Codex rollout is not in a sessions/YYYY/MM/DD folder", "rollout")
        day = matched[1]
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, data in files:
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(data), 0o600, int(time.time())
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue(), day


class Move:
    """One move as it runs; ``progress`` hears each step."""

    def __init__(self, home, request, workspaces, overview, progress, record=None):
        self.home = Path(home)
        # Facts a restarted dashboard needs about this move: its stage, the watch it
        # holds, and how to resume the session here (``Moves.recover``).
        self.record = record or (lambda **facts: None)
        self.request = request
        self.workspaces = workspaces
        self.overview = overview
        self.progress = progress
        self.id = uuid.uuid4().hex
        self.machine: machines.Machine  # Placed as the move starts.
        self.worker: dict | None = None  # The worker's checkout, once made.
        self.exited = False
        # The watch this move stopped: its ID, and its status and summary before.
        self.watch: tuple[str, str, str | None] | None = None

    def say(self, text):
        self.progress(text)

    def probe(self, op, timeout, **args):
        try:
            return remote_probe.call(self.home, self.machine, op, timeout, **args)
        except subprocess.TimeoutExpired as exc:
            raise ValueError(
                f"{self.machine.label} did not answer within {timeout} seconds"
            ) from exc
        except OSError as exc:
            raise ValueError(f"{self.machine.label} could not be reached: {exc}") from exc

    def stage(self, name, data):
        return machines.put_file(
            self.home,
            self.machine,
            machines.remote_path(STAGED),
            name,
            data,
            keep=STAGE_KEEP,
            timeout=RECEIVE_TIMEOUT,
        )

    def send_code(self, src, state):
        """The checkout ``state`` made (or brought up to date) on the worker, checked."""
        machine = self.machine
        data, fetch = bundle(src["root"], state, self.id)
        self.say(f"Copying {state['branch']} to {machine.label} ({max(1, len(data) >> 10)} KB)")
        staged = self.stage(f"{self.id}-{uuid.uuid4().hex[:8]}.bundle", data)
        made = self.probe(
            "move_receive",
            RECEIVE_TIMEOUT,
            **{**worker_paths(machine, src), "branch": state["branch"]},
            bundle=staged,
            move=self.id,
            head=state["head"],
            index_tree=state["index_tree"],
            work_tree=state["work_tree"],
            upstream=src["upstream"],
            fetch=fetch,
        )
        self.worker = made
        if not same_state(state, made):
            raise ValueError(
                f"The checkout made on {machine.label} does not match this one "
                f"(head {made['head'][:12]}, tree {made['work_tree'][:12]})"
            )
        return made

    def hold_watch(self, src):
        """Stop the watch that follows this workspace, so nothing resumes it meanwhile."""
        import pr_supervisor

        job_id = src["watch"]["id"]
        db = pr_supervisor.open_db(self.home)
        try:
            with db:
                db.execute("BEGIN IMMEDIATE")
                job = pr_supervisor.get_job(db, job_id)
                if job["status"] not in MOVABLE_WATCHES:
                    raise ValueError(f"babysit watch {job_id} changed to {job['status']}")
                self.watch = (job_id, job["status"], job.get("summary"))
                job.update(
                    status="stopped",
                    epoch=job["epoch"] + 1,
                    dispatch_ready=False,
                    summary=f"Moving with its workspace to {self.machine.label}",
                )
                pr_supervisor.save_job(db, job)
        finally:
            db.close()

    def restore_watch(self, note):
        """Give the watch back its status, while it is still the one this move stopped."""
        if self.watch is None:
            return
        job_id, status, summary = self.watch
        try:
            self.give_back(job_id, status, f"{note} {summary}")
        except Exception as exc:  # The move's own failure is what gets reported.
            self.say(f"babysit watch {job_id} could not be given back: {exc}")
        self.watch = None

    def give_back(self, job_id, status, summary):
        import pr_supervisor

        db = pr_supervisor.open_db(self.home)
        try:
            with db:
                db.execute("BEGIN IMMEDIATE")
                job = pr_supervisor.get_job(db, job_id)
                if job["status"] == "stopped" and str(job.get("summary")).startswith("Moving"):
                    job.update(status=status, epoch=job["epoch"] + 1, summary=summary)
                    pr_supervisor.save_job(db, job)
        finally:
            db.close()

    def follow_watch(self, src, path, pane):
        """Register the stopped watch again on the worker, for the resumed agent there."""
        import pr_supervisor
        import worker_api

        assert self.watch is not None
        job_id, status, _ = self.watch
        db = pr_supervisor.open_db(self.home)
        try:
            job = pr_supervisor.get_job(db, job_id)
            args = Namespace(
                home=self.home,
                machine=self.machine.id,
                pr="auto" if job.get("branch") else job["url"],
                repo=job.get("repo"),
                ci_repo=job.get("ci_repo"),
                branch=job.get("branch"),
                cwd=path,
                session=src["session"],
                agent=src["agent"],
                claude_account=src["account"],
                pane=pane,
                headless=False,
                rollout=None,
                instructions=job["instructions"],
                on_green=None if job.get("on_green_completed") else job.get("on_green"),
                codex_command=None,
                claude_command=None,
                poll_seconds=max(worker_api.MIN_POLL, job["poll_seconds"]),
                max_repairs=job["max_repairs"],
                repair_timeout=job["repair_timeout"],
                config_dir=None,
            )
            moved = pr_supervisor.register(db, args)
            if status == "paused":
                with db:
                    db.execute("BEGIN IMMEDIATE")
                    moved = pr_supervisor.get_job(db, job_id)
                    moved.update(status="paused", epoch=moved["epoch"] + 1, summary="pause")
                    pr_supervisor.save_job(db, moved)
        finally:
            db.close()
        self.watch = None
        return moved

    def remove_worker_checkout(self):
        """Take back the worker's new checkout and branch, made by this move alone; what
        stays there if that fails."""
        if self.worker is None:
            return None
        try:
            self.probe(
                "remove",
                1200,
                root=self.worker["clone"],
                path=self.worker["path"],
                force=True,
                branch=self.worker["branch"],
                safe=True,
            )
        except ValueError as exc:
            return f"The new checkout {self.worker['path']} on {self.machine.label} stays: {exc}"
        self.worker = None
        return None

    def still_here(self, src):
        """Whether the agent still runs its session in its pane here, as far as herdr says."""
        try:
            listed = agent_messages.agents(src["workspace"])
        except Exception:
            return False  # Unknown: it may have gone, so nothing assumes it runs.
        return any(a["pane"] == src["pane"] and a.get("session") == src["session"] for a in listed)

    def confirmed(self, session, kind, pane):
        """The worker's herdr agent on ``session``, in ``pane`` if one is named, once it
        shows; None if it does not within CONFIRM_WAIT."""
        deadline = time.monotonic() + CONFIRM_WAIT
        while True:
            try:
                found = next(
                    (
                        a
                        for a in agent_messages.listed_agents(self.machine)
                        if (a.get("agent_session") or {}).get("value") == session
                        and a.get("agent") == kind
                        and (pane is None or a.get("pane_id") == pane)
                    ),
                    None,
                )
            except (OSError, ValueError, subprocess.SubprocessError):
                found = None
            if found or time.monotonic() >= deadline:
                return found
            time.sleep(1)

    def clean_source(self, src):
        """Remove this machine's workspace, worktree and branch as the Workspaces tab does."""
        overview = self.overview
        if overview is None or not getattr(overview, "enabled", False):
            return False, (
                "The Workspaces experiment is off, so the checkout here was kept; remove it "
                "from the Workspaces tab or with `git worktree remove`"
            )
        request = {"targets": [{"key": src["root"], "approve": ["dirty", "unpushed"]}]}
        asked: list[bool] = []

        def guard(row):
            """Asked under the clone's lock, before the workspace closes and again before
            the worktree goes: only what the worker has is ever removed here, and nothing
            started in its other panes since the move began is closed with it."""
            if row["path"] != src["root"]:
                raise ValueError("That is not the moved checkout; it was kept")
            if not asked:
                asked.append(True)
                try:
                    busy = busy_panes(src["workspace"], src["pane"])
                except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
                    raise ValueError(
                        f"Its panes could not be read ({exc}), so it was kept"
                    ) from exc
                if busy:
                    raise ValueError(
                        f"Its workspace was kept open, with its worktree: {', '.join(busy)} "
                        "started there during the move"
                    )
            if not same_state(src["state"], snapshot(src["root"])):
                raise ValueError("The checkout here changed after it was copied, so it was kept")
            kept = ignored_kept(src["root"])
            if kept:
                shown = ", ".join(kept[:5]) + (
                    f" and {len(kept) - 5} more" if len(kept) > 5 else ""
                )
                return f"The worktree here was kept for files Git ignores, not copied: {shown}"
            return None

        deadline = time.monotonic() + CLEANUP_WAIT
        while True:
            started = overview.cleanup(request, guard=guard)
            if started["accepted"]:
                break
            if time.monotonic() >= deadline:
                return False, "Another cleanup kept running; remove the checkout here yourself"
            time.sleep(CLEANUP_POLL)
        job_id = started["cleanup"]["id"]
        while True:
            job = overview.snapshot(start=False)["cleanup"] or {}
            if job.get("id") != job_id or job.get("status") != "running":
                break
            if time.monotonic() >= deadline:
                return False, "The cleanup here did not finish in time; check the Workspaces tab"
            time.sleep(CLEANUP_POLL)
        result = (job.get("results") or [{}])[0] if job.get("id") == job_id else {}
        said = result.get("message") or "Cleanup unconfirmed"
        if result.get("status") == "kept":
            return False, said
        # "Already removed" also answers a checkout the inventory could not find.
        if result.get("status") != "done" or os.path.lexists(src["root"]):
            return False, f"The checkout here was kept: {said}"
        return True, said

    def run(self):
        """Move the agent; the result, or Failed saying what was kept where."""
        request = self.request
        first = source(self.home, request)
        if (request.get("pane"), request.get("session")) != (first["pane"], first["session"]):
            raise Refused("That agent is no longer running in this workspace; refresh", "changed")
        # Nothing else is typed into its pane, resumed in its checkout or session, or
        # restarted while it moves: such actions are refused at once (``reserved``).
        keys = [
            ("checkout", first["root"]),
            ("session", first["session"]),
            ("pane", first["pane"]),
        ]
        with agent_messages.reserved(*keys):
            return self.moving(request)

    def moving(self, request):
        src = source(self.home, request)  # Read again, now that nothing else acts on it.
        if (request.get("pane"), request.get("session")) != (src["pane"], src["session"]):
            raise Refused("That agent is no longer running in this workspace; refresh", "changed")
        if src["status"] == "working" and request.get("confirm_working") is not True:
            raise Refused(
                "The agent is working: confirm stopping its turn to move it",
                "confirm_working",
                plan=public(src),
            )
        _, chosen, error = target(self.home, self.workspaces, src, request.get("machine"))
        if chosen is None:
            raise Refused(error or "No machine can take this agent", "target")
        self.machine = chosen["machine"]
        check_watch(src, self.machine)
        # Before any key: what the worker would refuse is refused while nothing changed.
        try:
            check_worker(self.home, self.machine, src)
        except Refused:
            raise
        except ValueError as exc:
            raise Refused(str(exc), "worker") from exc
        # Its transcripts only travel once the agent left: one too large is refused first.
        if session_size(src) > remote_probe.MAX_MOVE_SESSION:
            raise Refused("Its conversation is too large to move", "too_large")
        label = LABEL[src["agent"]]
        stopped = src["children"] or []
        try:
            if src["status"] == "working":
                self.say(f"Stopping {label}'s turn")
                stop = agent_messages.interrupt(
                    {
                        "workspace": src["workspace"],
                        "pane": src["pane"],
                        "session": src["session"],
                    }
                )
                if not stop["stopped"]:
                    raise ValueError(stop["warning"] or "The agent kept working")
                after = source(self.home, request)
                if after["status"] == "working":
                    raise ValueError("The agent is working again; it stays here")
                # Recorded before Esc, and what is still running after it.
                pids = {c["pid"] for c in stopped}
                stopped += [c for c in after["children"] or [] if c["pid"] not in pids]
                src = {**after, "children": stopped}
                check_watch(src, self.machine)
            self.say(f"Packing {src['state']['branch']} for {self.machine.label}")
            made = self.send_code(src, src["state"])
            if src["watch"] is not None:
                self.say(f"Holding babysit watch {src['watch']['id']} for the move")
                self.hold_watch(src)
                self.record(watch=src["watch"]["id"])
            self.say(f"Exiting {label} here")
            self.record(stage="exit", resume_command=resume_here(src))
            try:
                # A turn it started since is stopped only if the reader confirmed that.
                quit_ = agent_messages.exit_agent(
                    {
                        "workspace": src["workspace"],
                        "pane": src["pane"],
                        "session": src["session"],
                    },
                    self.home,
                    stop=request.get("confirm_working") is True,
                )
            except Exception as exc:
                if self.still_here(src):
                    raise
                # Raised once the agent had gone: as an unconfirmed exit, never "running".
                quit_ = {"quit": False, "warning": str(exc)}
        except Exception as exc:
            self.record(stage="before_exit")
            kept = self.remove_worker_checkout()
            self.restore_watch("Move stopped before the agent left;")
            raise Failed(
                f"{exc}" + (f". {kept}" if kept else ""),
                stage="before_exit",
                source_kept=True,
                agent_running=True,
            ) from exc
        if not quit_["quit"]:
            self.restore_watch("Move stopped while the agent exited;")
            raise Failed(
                f"The agent's exit could not be confirmed: {quit_['warning']}",
                stage="exit",
                source_kept=True,
                worker_path=made["path"],
                resume_command=resume_here(src),
            )
        self.exited = True
        self.record(stage="after_exit")
        resumed = None
        try:
            self.say("Checking the checkout here again")
            state = snapshot(src["root"])
            if not same_state(src["state"], state):
                self.say("The checkout changed while the agent exited; sending it again")
                made = self.send_code(src, state)
            src["state"] = state
            path = made["path"]
            self.say(f"Copying the conversation to {self.machine.label}")
            archive, day = transcripts(src, path)
            staged = self.stage(f"{self.id}-session.tar.gz", archive)
            self.probe(
                "move_session",
                SESSION_TIMEOUT,
                archive=staged,
                agent=src["agent"],
                account=src["account"],
                sandbox=placement.SANDBOX[self.machine.os],
                path=path,
                day=day,
                suffix=src["suffix"],
            )
            self.say(f"Resuming {label} on {self.machine.label}")
            watch = self.watch[0] if self.watch else None
            resumed = agent_messages.resume(
                None,
                src["session"],
                message(src, self.machine, path, stopped, watch, made),
                self.home,
                root=path,
                machine=self.machine,
                agent_options={
                    "model": src["model"],
                    "effort": src["effort"],
                    "docker": src["docker"],
                },
            )
            running = self.confirmed(src["session"], src["agent"], resumed.get("pane"))
            if running is None:
                raise ValueError(
                    resumed.get("warning")
                    or f"{self.machine.label}'s herdr does not show the resumed session"
                )
        except Exception as exc:
            said = str(exc)
            if resumed is None:
                self.restore_watch("Move stopped after the agent left;")
            else:
                # Typed there: it may still start, and two agents must not share it,
                # so the watch stays stopped too.
                said += (
                    f". Its resume was typed in {resumed.get('pane')} on "
                    f"{self.machine.label}; check there before resuming it here"
                )
            raise Failed(
                said,
                stage="after_exit",
                source_kept=True,
                worker_path=self.worker["path"] if self.worker else None,
                resume_command=resume_here(src),
            ) from exc
        self.record(stage="resumed")
        warnings = []
        if src["upstream"] and made.get("upstream") is None:
            merge, slug = src["upstream"][1:3]
            warnings.append(
                f"Its branch tracks {merge} of {slug}, but has no upstream on "
                f"{self.machine.label}: {made.get('upstream_note') or 'it could not be set'}"
            )
        if resumed.get("warning"):
            warnings.append(resumed["warning"])
        followed = None
        pane = running["pane_id"]
        if self.watch is not None:
            self.say(f"Registering babysit watch {self.watch[0]} on {self.machine.label}")
            job_id = self.watch[0]
            try:
                followed = self.follow_watch(src, path, pane)["id"]
            except Exception as exc:
                # The agent runs there already: nothing here undoes the move for this.
                self.watch = None
                warnings.append(
                    f"babysit watch {job_id} stays stopped: it could not be registered on "
                    f"{self.machine.label} ({exc}); register it there again"
                )
        self.say("Removing the workspace here")
        try:
            removed, said = self.clean_source(src)
        except Exception as exc:
            # Moved all the same: what stays here is said, never undone.
            removed, said = False, f"The checkout here was kept: its cleanup failed ({exc})"
        if not removed:
            warnings.append(said)
        space = pane.split(":")[0]
        return {
            "machine": self.machine.id,
            "machine_label": self.machine.label,
            "path": path,
            "branch": src["state"]["branch"],
            "head": src["state"]["head"],
            "session": src["session"],
            "pane": pane,
            "workspace_id": machines.qualify(self.machine.id, space)
            if machines.WORKSPACE.fullmatch(space)
            else None,
            "url": collie_url(self.machine, space) if PANE.fullmatch(pane) else None,
            "stopped": [c["command"] for c in stopped[:MAX_SHOWN]],
            "watch": followed,
            "source": {"path": src["root"], "removed": removed, "message": said},
            "warnings": warnings,
        }


class Moves:
    """The dashboard's moves: started in the background, followed by ID, kept briefly.

    ``workspaces`` and ``overview`` return the dashboard's New task placement and its
    Workspaces tab, whose cleanup removes a moved workspace here.
    """

    def __init__(self, home, workspaces, overview):
        self.home = Path(home)
        self.workspaces = workspaces
        self.overview = overview
        self.lock = threading.Lock()
        self.ops: dict[str, dict] = {}
        self.starting: set[tuple[str, str]] = set()
        self.recover()

    def recover(self):
        """Read the moves saved before this dashboard started. One that was still running
        stopped with the dashboard: it is marked interrupted with where it got to, and a
        watch it held is given back to the status it had."""
        folder = self.home / "moves"
        try:
            files = sorted(folder.glob("*.json"), key=lambda f: f.stat().st_mtime)
        except OSError:
            return
        for file in files[-KEPT_MOVES:]:
            try:
                op = json.loads(file.read_text())
            except (OSError, ValueError):
                continue
            if not (
                isinstance(op, dict)
                and isinstance(op.get("id"), str)
                and remote_probe.MOVE_ID.fullmatch(op["id"])
                and file.name == f"{op['id']}.json"
                and isinstance(op.get("status"), str)
                and isinstance(op.get("started_at"), (int, float))
                and not isinstance(op.get("started_at"), bool)
            ):
                # Not one this dashboard wrote: the state home is writable by more than it.
                print(f"Skipped unreadable saved move {file}", file=sys.stderr)
                continue
            if op.get("status") == "running":
                stage = op.get("stage")
                left = stage in {"after_exit", "resumed"}
                said = f"The dashboard restarted during this move, at: {op.get('message')}. " + (
                    "The agent had left this machine; check the worker before resuming it here"
                    if left
                    else "Check whether the agent still runs here"
                )
                watch = op.get("watch")
                if watch:
                    said += self.give_back(watch)
                op.update(
                    status="interrupted",
                    message=said,
                    error=said,
                    finished_at=time.time(),
                    result={
                        "stage": stage or "before_exit",
                        "source_kept": True,
                        "interrupted": True,
                        "resume_command": op.get("resume_command") if stage else None,
                    },
                )
                self.save(op)
            self.ops[op["id"]] = op

    def give_back(self, job_id):
        """A watch an interrupted move held, given its status back; what became of it."""
        import pr_supervisor

        try:
            db = pr_supervisor.open_db(self.home)
            try:
                with db:
                    db.execute("BEGIN IMMEDIATE")
                    job = pr_supervisor.get_job(db, job_id)
                    if job["status"] != "stopped" or not str(job.get("summary")).startswith(
                        "Moving with its workspace"
                    ):
                        return ""
                    job.update(
                        status="awaiting_release",
                        epoch=job["epoch"] + 1,
                        summary="The dashboard restarted during a move of this workspace; "
                        "check where its agent runs",
                    )
                    pr_supervisor.save_job(db, job)
            finally:
                db.close()
        except Exception as exc:
            return f". babysit watch {job_id} stays stopped ({exc})"
        return f". babysit watch {job_id} is awaiting release again"

    def action(self, request):
        if not isinstance(request, dict):
            raise ValueError("Expected a move request")
        if set(request) == {"id"}:
            return {"move": self.get(request["id"])}
        workspaces = self.workspaces()
        if workspaces is None:
            raise ValueError("Workspace actions are not enabled")
        if request.get("plan") is True:
            fields = {k: v for k, v in request.items() if k != "plan"}
            if set(fields) - (REQUEST - {"confirm_working"}):
                raise ValueError("Invalid move parameters")
            return {"plan": plan(self.home, fields, workspaces)}
        if set(request) - REQUEST or not all(
            isinstance(request.get(k), str) and request[k] for k in ("workspace", "pane", "session")
        ):
            raise ValueError("Invalid move parameters")
        return {"move": self.start(request, workspaces)}

    def get(self, move_id):
        with self.lock:
            op = self.ops.get(move_id) if isinstance(move_id, str) else None
            if op is None:
                raise ValueError("Unknown move; it may have finished before a restart")
            return json.loads(json.dumps(op))

    def start(self, request, workspaces):
        key = (request["pane"], request["session"])
        with self.lock:
            if key in self.starting or any(
                op["status"] == "running" and (op["pane"], op["session"]) == key
                for op in self.ops.values()
            ):
                raise ValueError("This agent is already being moved")
            self.starting.add(key)
        try:
            return self.begin(request, workspaces)
        finally:
            with self.lock:
                self.starting.discard(key)

    def begin(self, request, workspaces):
        # Refusals come back at once; the move itself runs on.
        src = source(self.home, request)
        if src["status"] == "working" and request.get("confirm_working") is not True:
            raise Refused(
                "The agent is working: confirm stopping its turn to move it",
                "confirm_working",
                plan=public(src),
            )
        op = {
            "id": None,
            "status": "running",
            "workspace": request["workspace"],
            "pane": request["pane"],
            "session": request["session"],
            "machine": request.get("machine") or "auto",
            "message": "Starting",
            "steps": [],
            "started_at": time.time(),
            "finished_at": None,
            "result": None,
            "error": None,
        }
        move = Move(
            self.home,
            request,
            workspaces,
            self.overview(),
            lambda text: self.update(op, message=text, step=text),
            lambda **facts: self.update(op, **facts),
        )
        op["id"] = move.id
        with self.lock:
            self.ops[move.id] = op
            for old in sorted(self.ops.values(), key=lambda o: o["started_at"])[:-KEPT_MOVES]:
                if old["status"] != "running":
                    self.ops.pop(old["id"], None)
        self.save(op)
        threading.Thread(target=self.run, args=(op, move), daemon=True).start()
        return json.loads(json.dumps(op))

    def update(self, op, step=None, **changes):
        with self.lock:
            op.update(changes)
            if step:
                op["steps"].append(step)
        self.save(op)

    def save(self, op):
        if not remote_probe.MOVE_ID.fullmatch(str(op.get("id"))):
            raise ValueError("A move is saved only under its own ID")
        folder = self.home / "moves"
        with contextlib.suppress(OSError):
            folder.mkdir(mode=0o700, parents=True, exist_ok=True)
            with self.lock:
                text = json.dumps(op, indent=2)
            partial = folder / f".{op['id']}.{os.getpid()}"
            partial.write_text(text)
            os.replace(partial, folder / f"{op['id']}.json")

    def run(self, op, move):
        try:
            result = move.run()
        except Refused as exc:
            self.update(op, status="refused", message=str(exc), error=str(exc), result=exc.details)
        except Failed as exc:
            self.update(op, status="failed", message=str(exc), error=str(exc), result=exc.details)
        except Exception as exc:
            # A refusal or failure before any key was sent leaves everything as it was.
            stage = "after_exit" if move.exited else "before_exit"
            self.update(
                op,
                status="failed",
                message=str(exc) or repr(exc),
                error=str(exc) or repr(exc),
                result={"stage": stage, "source_kept": True},
            )
        else:
            text = f"Moved to {result['machine_label']}"
            if result["warnings"]:
                text += "; " + " ".join(result["warnings"])
            self.update(op, status="moved", message=text, result=result, step=text)
        finally:
            self.update(op, finished_at=time.time())


# The command line, through the dashboard ---------------------------------------------


def post(base, action, body):
    """One JSON request to the dashboard's ``action``; a refusal raises with its reason."""
    import urllib.error
    import urllib.request

    request = urllib.request.Request(
        f"{base}/api/{action}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Babysit-Action": action},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=120) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            detail = json.load(exc).get("error")
        except (ValueError, AttributeError):
            detail = None
        raise ValueError(detail or f"HTTP {exc.code}") from None
    except (OSError, ValueError) as exc:
        raise ValueError(f"Cannot reach the dashboard at {base}: {exc}") from None


def cli(args, stream=None):
    """``pr_supervisor.py workspace-move``: plan and run a move through the dashboard,
    which holds the locks and the cleanup the dashboard's own actions use."""
    stream = stream or sys.stderr
    base = (args.dashboard or os.environ.get("BABYSIT_DASHBOARD_URL") or wt.DASHBOARD).rstrip("/")
    request = {"workspace": args.workspace, "pane": args.pane, "machine": args.machine}
    found = post(base, "workspace-move", {**request, "plan": True})["plan"]
    print(
        f"{LABEL[found['agent']]} in {found['pane']} ({found['status']}), {found['branch']}: "
        f"{found['changed']} changed, {found['untracked']} untracked, "
        f"{found['unpushed']} unpushed commit(s)",
        file=stream,
    )
    for child in found["children"]:
        print(f"  stops: {child['command']}", file=stream)
    if found.get("mcp_count"):
        print(
            f"  restarts with the agent: {found['mcp_count']} MCP server process(es)", file=stream
        )
    if found["error"]:
        raise ValueError(found["error"])
    print(f"Target: {found['label']} ({found['reason']})", file=stream)
    if found.get("worker"):
        # The plan waited for the worker's own check.
        print(
            f"Checked on {found['label']}: clone {found['worker']['clone']}, new checkout "
            f"{found['worker']['path']}",
            file=stream,
        )
    if args.plan:
        return {"plan": found}
    if found["confirm"] and not args.yes:
        raise ValueError("The agent is working; pass --yes to stop its turn and move it")
    op = post(
        base,
        "workspace-move",
        {
            **request,
            "pane": found["pane"],
            "session": found["session"],
            "machine": found["machine"],
            "confirm_working": bool(args.yes),
        },
    )["move"]
    said = None
    while op["status"] == "running":
        if op["message"] != said:
            said = op["message"]
            print(said, file=stream)
        time.sleep(2)
        op = post(base, "workspace-move", {"id": op["id"]})["move"]
    if op["status"] != "moved":
        details = op.get("result") or {}
        if details.get("resume_command"):
            print(f"Resume it here: {details['resume_command']}", file=stream)
        raise ValueError(op["error"] or "The move failed")
    return {"move": op}
