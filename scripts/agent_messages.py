"""Follow-up messages from the dashboard to an agent of a herdr workspace.

A message goes to one agent pane of the named workspace through `herdr agent prompt`,
which types it and presses Enter only while the pane hosts a recognized agent, so text
never reaches a plain shell. Nothing is sent to an agent waiting on a dialog, and a
running agent is never interrupted or restarted.

When no agent runs there (usually it exited after finishing), the message can instead
resume one of the sessions recorded in that checkout: the agent's own resume command,
with the message as its prompt, is typed into a new split of the workspace, the same way
launches type their command, so the user's shell wrappers apply. A session already open
in a pane, just resumed, or owned by a babysit watch is never resumed again.
"""

import fcntl
import json
import os
import re
import sqlite3
import subprocess
import threading
import time
import uuid
from pathlib import Path

import claude_runner
import workspace_viewer
import wt
from pr_workspaces import herdr, run

MAX_MESSAGE = 32000
# The text is typed into a terminal: a carriage return would submit early, and other
# control characters (an escape sequence ending bracketed paste, say) could act as
# keystrokes. Quoted diff lines come from files nobody vetted, so only newlines and
# tabs survive.
CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
UNCERTAIN = "check it in Collie before resending"
RESUME_WAIT = 20
RESUME = {"claude": ["--resume"], "codex": ["resume"]}
# A resumed agent can take a while to be recognized; until then a repeat is refused.
RECENT_RESUME = 120
ENDED_WATCHES = {"closed", "stopped"}
_resume_lock = threading.Lock()
_recent: dict[str, tuple[str, float]] = {}


def clean(text):
    return CONTROL.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))


def agents(workspace_id):
    """The agents running in a herdr workspace, for choosing whom to message.

    Only a workspace on a Git checkout qualifies, the same ones the viewer shows.
    """
    workspace_viewer.workspace_checkout(workspace_id)  # Validates the workspace exists.
    return [
        {
            "pane": agent["pane_id"],
            "agent": agent.get("agent"),
            "status": agent.get("agent_status"),
            "title": agent.get("terminal_title_stripped") or None,
        }
        for agent in herdr("agent", "list")["agents"]
        if agent.get("workspace_id") == workspace_id and agent.get("pane_id")
    ]


def sessions(workspace_id, home=None):
    """Sessions recorded in the workspace's checkout that a message could resume."""
    root = workspace_viewer.workspace_checkout(workspace_id)
    watched = {
        job.get("session_id") for job in watch_jobs(home) if job.get("status") not in ENDED_WATCHES
    }
    return [
        {
            **{k: v for k, v in session.items() if k in {"id", "agent", "title", "updated"}},
            "watched": session["id"] in watched,
        }
        for session in workspace_viewer.sessions(root)
    ]


def send(request, home=None):
    """Submit one message to one agent of a workspace; report what herdr saw."""
    if set(request) - {"workspace", "pane", "text", "resume"}:
        raise ValueError("Invalid message parameters")
    text = request.get("text")
    text = clean(text) if isinstance(text, str) else None
    if not text or not text.strip() or len(text) > MAX_MESSAGE:
        raise ValueError(f"Write a message of 1–{MAX_MESSAGE:,} characters")
    if request.get("resume") is not None:
        if request.get("pane") is not None:
            raise ValueError("Resume a session or message a running agent, not both")
        return resume(request["workspace"], request["resume"], text, home)
    pane = request.get("pane")
    if pane is not None and not isinstance(pane, str):
        raise ValueError("Choose an agent")
    running = agents(request.get("workspace"))
    if not running:
        raise ValueError("No agent is running in this workspace; start one from Collie")
    if pane is None:
        if len(running) > 1:
            raise ValueError("Several agents run in this workspace; choose one")
        pane = running[0]["pane"]
    target = next((agent for agent in running if agent["pane"] == pane), None)
    if target is None:
        raise ValueError("That agent is no longer running; refresh")
    if target["status"] == "blocked":
        raise ValueError("The agent is waiting on a question or approval; answer it in Collie")
    try:
        # Wait only until the agent reacts (starts working or asks something), not for
        # the turn to end; herdr reports a stall when it shows no reaction in 5 s.
        herdr(
            "agent",
            "prompt",
            pane,
            text,
            "--wait",
            "--until",
            "working",
            "--until",
            "blocked",
            "--timeout",
            "8000",
        )
    except subprocess.TimeoutExpired:
        return {
            "sent": True,
            "pane": pane,
            "warning": f"herdr did not answer in time; {UNCERTAIN}.",
        }
    except (ValueError, subprocess.SubprocessError) as exc:
        detail = str(exc)
        if "agent_blocked" in detail:
            raise ValueError(
                "The agent is waiting on a question or approval; answer it in Collie"
            ) from exc
        if "agent_prompt_stalled" in detail or '"timeout"' in detail:
            # Typed and submitted, but no reaction seen: never invite sending it twice.
            return {
                "sent": True,
                "pane": pane,
                "warning": f"Sent, but the agent showed no reaction yet; {UNCERTAIN}.",
            }
        raise ValueError(f"herdr could not deliver the message: {detail[-300:]}") from exc
    return {"sent": True, "pane": pane, "warning": None}


def watch_owner(session_id, agent, home):
    """Why the babysit watcher owns this session, if it does.

    A watch resumes its registered session headless for repairs, under a per-session
    lock; an interactive copy alongside would make two writers on one conversation.
    """
    lock_home = (
        claude_runner.config_home()
        if agent == "claude"
        else Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    ) / "babysit-pr-locks"
    lock = lock_home / f"{session_id}.lock"
    if lock.exists():
        with lock.open("a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return "a babysit repair is running in this session right now"
            fcntl.flock(handle, fcntl.LOCK_UN)
    for job in watch_jobs(home):
        if job.get("session_id") == session_id and job.get("status") not in ENDED_WATCHES:
            return f"babysit watch {job.get('id')} ({job.get('status')}) resumes this session itself; stop it first"
    return None


def watch_jobs(home):
    path = Path(home) / "queue.sqlite" if home else None
    if path is None or not path.exists():
        return []
    # Read-only: never create the watcher's database or take part in its locking.
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)
    try:
        return [json.loads(row[0]) for row in db.execute("SELECT data FROM jobs")]
    finally:
        db.close()


def open_elsewhere(session_id):
    """A pane herdr already sees running this session, in any workspace."""
    for pane in herdr("pane", "list")["panes"]:
        if (pane.get("agent_session") or {}).get("value") == session_id:
            return pane.get("pane_id")
    return None


def prune(prompts):
    """Forget day-old prompts a resume left behind when its agent never showed up."""
    cutoff = time.time() - 86400
    for staged in prompts.glob("resume-*"):
        try:
            if staged.stat().st_mtime < cutoff:
                staged.unlink()
        except OSError:
            continue
    now = time.monotonic()
    for key, (_, at) in list(_recent.items()):
        if now - at >= RECENT_RESUME:
            _recent.pop(key, None)


def resume(workspace_id, session_id, text, home):
    """Resume a recorded session in the workspace with the message as its prompt."""
    if not isinstance(session_id, str) or not workspace_viewer.SESSION.match(session_id):
        raise ValueError("Choose a recorded session to resume")
    root = workspace_viewer.workspace_checkout(workspace_id)
    chosen = next((s for s in workspace_viewer.sessions(root) if s["id"] == session_id), None)
    if chosen is None or chosen["agent"] not in RESUME:
        raise ValueError("That session is not recorded for this workspace")
    # One resume at a time, held until its agent shows up: a second request then sees it.
    with _resume_lock:
        if agents(workspace_id):
            # Two agents on one conversation would interleave; talk to the running one.
            raise ValueError("An agent is already running in this workspace; send to it instead")
        recent = _recent.get(session_id)
        if recent and time.monotonic() - recent[1] < RECENT_RESUME:
            raise ValueError(
                f"This session was just resumed in {recent[0]}; check it in Collie before resending"
            )
        elsewhere = open_elsewhere(session_id)
        if elsewhere:
            raise ValueError(f"This session is already open in pane {elsewhere}; message it there")
        owner = watch_owner(session_id, chosen["agent"], home)
        if owner:
            raise ValueError(f"Not resumed: {owner}")
        # A session resumes from the directory it was recorded in (Claude looks its ID up
        # in that directory's project folder).
        details = workspace_viewer.details(chosen["_file"], chosen["agent"]) or {}
        cwd = details.get("cwd") or root
        if not Path(cwd).is_dir():
            raise ValueError("The session's directory no longer exists")
        # Always a fresh pane, as launches use: an existing shell may hold a half-typed
        # line or a program that replaced it, and typing would join or feed it.
        anchor = next(
            (p["pane_id"] for p in herdr("pane", "list", "--workspace", workspace_id)["panes"]),
            None,
        )
        if anchor is None:
            raise ValueError("That herdr workspace has no pane to split")
        split = herdr("pane", "split", anchor, "--direction", "right", "--cwd", cwd, "--no-focus")
        pane = split["pane"]["pane_id"]
        # The prompt is read back from a private file, as launches do: a typed line has
        # a length ceiling, and command-substitution output needs no escaping.
        prompts = Path(home or os.environ.get("TMPDIR") or "/tmp") / "message-prompts"
        prompts.mkdir(mode=0o700, parents=True, exist_ok=True)
        prune(prompts)
        staged = prompts / f"resume-{uuid.uuid4()}"
        descriptor = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text + "\n")
        command = wt.agent_command(
            chosen["agent"],
            text,
            extra=[*RESUME[chosen["agent"]], chosen["id"]],
            stage=lambda _: str(staged),
        )
        _recent[session_id] = (pane, time.monotonic())
        try:
            # `pane run` answers with plain text, not herdr's JSON envelope.
            run("herdr", "pane", "run", pane, command)
        except subprocess.TimeoutExpired:
            return {
                "sent": True,
                "pane": pane,
                "resumed": chosen["id"],
                "warning": f"herdr did not answer while typing the command; {UNCERTAIN}.",
            }
        except (ValueError, subprocess.SubprocessError):
            _recent.pop(session_id, None)
            staged.unlink(missing_ok=True)
            raise
        # From here the command is typed: any trouble makes the outcome uncertain, never
        # "not sent", which would invite a second agent on the same session.
        deadline = time.monotonic() + RESUME_WAIT
        try:
            while time.monotonic() < deadline:
                if any(agent["pane"] == pane for agent in agents(workspace_id)):
                    staged.unlink(missing_ok=True)  # The shell has read it by now.
                    # A recognized agent guards the session from here on.
                    _recent.pop(session_id, None)
                    return {"sent": True, "pane": pane, "resumed": chosen["id"], "warning": None}
                time.sleep(0.5)
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
        return {
            "sent": True,
            "pane": pane,
            "resumed": chosen["id"],
            "warning": f"The resume command was typed in {pane}, but no agent appeared yet; {UNCERTAIN}.",
        }
