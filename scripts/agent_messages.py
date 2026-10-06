"""Follow-up messages from the dashboard to an agent of a herdr workspace.

A message goes to one agent pane of the named workspace through `herdr agent prompt`,
which types it and presses Enter only while the pane hosts a recognized agent, so text
never reaches a plain shell. Nothing is sent to an agent waiting on a dialog, and a
running agent without Docker access is gracefully restarted once idle.

When no agent runs there (usually it exited after finishing), the message can instead
resume one of the sessions recorded in that checkout: the agent's own resume command,
with the message as its prompt, is typed into a new split of the workspace, the same way
launches type their command, so the user's shell wrappers apply. A session already open
in a pane gets the message there; one just resumed or owned by a babysit watch is
never resumed again.
"""

import fcntl
import json
import os
import re
import shlex
import sqlite3
import subprocess
import threading
import time
import uuid
from pathlib import Path

import agent_docker
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
# Claude Code's question dialog (as of 2.1): a number key picks an option, and on a
# single-choice question also moves on; the option after the last, "Type something.",
# takes typed text and Enter. On a multiple-choice question number keys toggle options
# and Right moves on. With several questions or a multiple-choice one, a review tab
# follows, where 1 submits. Each step waits for the dialog to show what it expects.
DIALOG_WAIT = 4
RULE = re.compile(r"^\s*─{8,}\s*$")
MAX_ANSWER = 2000
_answer_lock = threading.Lock()
_resume_lock = threading.RLock()
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
            # The session the pane runs, so its transcript's open question can be answered.
            "session": (agent.get("agent_session") or {}).get("value"),
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
    with _resume_lock:
        return deliver(target, text, home)


def deliver(target, text, home):
    """Ensure Docker access before delivering a follow-up to an existing session."""
    if target["status"] == "blocked":
        raise ValueError("The agent is waiting on a question or approval; answer it in Collie")
    try:
        info, procs, state, proc = agent_docker.inspect(target)
        if agent_docker.has_access(proc["pid"]):
            return prompt(target, text)
        record = agent_docker.prepare(target, info, procs, state, proc)
        session_id = record["session_id"]
        if not workspace_viewer.SESSION.fullmatch(session_id):
            raise ValueError("Could not verify the current conversation; refresh before sending")
        owner = watch_owner(session_id, record["agent"], home)
        if owner:
            raise ValueError(f"Not restarted: {owner}")
        recent = _recent.get(session_id)
        if recent and time.monotonic() - recent[1] < RECENT_RESUME:
            raise ValueError(f"This session was just resumed; {UNCERTAIN}")
        root = workspace_viewer.workspace_checkout(info["workspace_id"])
        chosen = next(
            (
                s
                for s in workspace_viewer.sessions(root)
                if s["id"] == session_id and s["agent"] == record["agent"]
            ),
            None,
        )
        if chosen is None:
            raise ValueError("The current session is not recorded yet; wait before sending")
        agent_docker.quit_agent(record)
        return launch(
            info["workspace_id"],
            chosen,
            target["pane"],
            text,
            home,
            options=record["options"],
            cwd=record["cwd"],
            launcher=record["launcher"],
        )
    except RuntimeError as exc:
        raise ValueError(str(exc)) from exc


def prompt(target, text):
    """Type the message into a running agent's pane and submit it."""
    pane = target["pane"]
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


def squash(text):
    return " ".join(text.split())


def dialog(pane):
    """The question dialog on the pane's screen, whitespace collapsed.

    It starts at the last rule above it, skipping the one that sets off its "Chat about
    this" line, and ends with its key hints (the review tab has none).
    """
    lines = run("herdr", "agent", "read", pane, "--source", "visible").splitlines()
    hints = [i for i, line in enumerate(lines) if "Esc to cancel" in line]
    end = hints[-1] + 1 if hints else len(lines)
    rules = [
        i
        for i, line in enumerate(lines[:end])
        if RULE.match(line) and "Chat about this" not in "".join(lines[i + 1 : i + 2])
    ]
    # No rule means no dialog: the screen may be a shell the agent left behind.
    return squash("\n".join(lines[rules[-1] : end])) if rules else ""


def wait_for(pane, shows):
    deadline = time.monotonic() + DIALOG_WAIT
    while True:
        if shows(dialog(pane)):
            return True
        if time.monotonic() > deadline:
            return False
        time.sleep(0.2)


def shows_question(question):
    """Whether the dialog shows this question: its text directly above its numbered
    options (a multiple-choice one has check boxes), not merely words from it."""
    options = [
        rf"(❯ )?{i}\. (\[.\] )?{re.escape(squash(o['label']))}"
        for i, o in enumerate(question["options"], 1)
    ]
    heading = re.compile(rf"(^| ){re.escape(squash(question['question']))} {options[0]}")
    rest = [re.compile(rf"(^| ){option}") for option in options[1:]]
    return lambda screen: bool(heading.search(screen)) and all(r.search(screen) for r in rest)


def answer_steps(questions, answers):
    """Validate one answer per question; return the keys (or text) each one needs."""
    if not isinstance(answers, list) or len(answers) != len(questions):
        raise ValueError("Answer every question")
    steps = []
    for question, given in zip(questions, answers, strict=True):
        count = len(question["options"])
        # Each choice is one digit key, "Type something." included.
        if not count or count >= 9:
            raise ValueError("This question has too many options to answer here; use Collie")
        if not isinstance(given, dict) or set(given) - {"options", "text"} or len(given) != 1:
            raise ValueError("Choose options or write an answer for each question")
        if "text" in given:
            text = given["text"]
            text = squash(clean(text)) if isinstance(text, str) else ""
            if question["multi"] or not text or len(text) > MAX_ANSWER:
                raise ValueError(f"Write an answer of 1–{MAX_ANSWER:,} characters")
            # "Type something." follows the options.
            steps.append([("key", str(count + 1)), ("text", text), ("key", "enter")])
            continue
        chosen = given["options"]
        if (
            not isinstance(chosen, list)
            or not chosen
            or any(
                isinstance(i, bool) or not isinstance(i, int) or not 0 <= i < count for i in chosen
            )
            or len(set(chosen)) != len(chosen)
            or (not question["multi"] and len(chosen) != 1)
        ):
            raise ValueError("Choose one listed option, or several where the question allows")
        keys = [("key", str(i + 1)) for i in sorted(chosen)]
        steps.append(keys + ([("key", "right")] if question["multi"] else []))
    return steps


def answer(request):
    """Answer the question a running Claude agent is waiting on, through its dialog."""
    if set(request) != {"workspace", "pane", "session", "tool", "answers"} or not all(
        isinstance(request[k], str) and request[k] for k in ("workspace", "pane", "session", "tool")
    ):
        raise ValueError("Invalid answer parameters")
    pane = request["pane"]
    target = next((a for a in agents(request["workspace"]) if a["pane"] == pane), None)
    if target is None or target["agent"] != "claude" or target["session"] != request["session"]:
        raise ValueError("That Claude session is no longer running in this pane; refresh")
    if target["status"] != "blocked":
        raise ValueError("The agent is not waiting on a question; refresh")
    root = workspace_viewer.workspace_checkout(request["workspace"])
    entries = workspace_viewer.transcript(root, request["session"])["entries"]
    asked = next(
        (e for e in reversed(entries) if e["role"] == "tool" and e.get("id") == request["tool"]),
        None,
    )
    if not asked or asked["output"] is not None or not asked.get("questions"):
        raise ValueError("That question was already answered; refresh")
    questions = asked["questions"]
    steps = answer_steps(questions, request["answers"])
    with _answer_lock:
        typed = False
        try:
            for question, keys in zip(questions, steps, strict=True):
                # The agent may have moved on, or exited and left its dialog on screen.
                if typed:
                    current: dict = next(
                        (a for a in agents(request["workspace"]) if a["pane"] == pane), {}
                    )
                    if (current.get("session"), current.get("status")) != (
                        request["session"],
                        "blocked",
                    ):
                        raise ValueError("the agent stopped waiting")
                if not wait_for(pane, shows_question(question)):
                    raise ValueError("the dialog did not show the next question")
                for kind, value in keys:
                    if kind == "text":
                        # The text field opens on the keypress before; its editor hint
                        # shows once it has focus.
                        if not wait_for(pane, lambda screen: "ctrl+g to edit" in screen):
                            raise ValueError("the answer field did not open")
                        run("herdr", "pane", "send-text", pane, "--", value)
                    else:
                        if value == str(
                            len(question["options"]) + 1
                        ) and "ctrl+g to edit" in dialog(pane):
                            raise ValueError("an answer field is already open")
                        herdr("agent", "send-keys", pane, value)
                    typed = True
            if len(questions) > 1 or any(q["multi"] for q in questions):
                if not wait_for(pane, lambda screen: "Submit answers" in screen):
                    raise ValueError("the dialog did not offer to submit")
                herdr("agent", "send-keys", pane, "1")
        except (ValueError, subprocess.SubprocessError) as exc:
            if not typed:
                raise ValueError(
                    "The question is not on the agent's screen; answer it in Collie"
                ) from exc
            # Something was typed: never invite answering again from the start.
            raise ValueError(f"Answered in part ({exc}); finish the question in Collie") from exc
    return {"answered": True, "pane": pane}


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
    """The running agent herdr sees on this session, in any workspace.

    herdr keeps a pane's session after its agent exits back to the shell, so only a
    pane that still hosts a recognized agent counts.
    """
    panes = {
        pane.get("pane_id")
        for pane in herdr("pane", "list")["panes"]
        if (pane.get("agent_session") or {}).get("value") == session_id
    }
    return next(
        (
            {
                "pane": agent["pane_id"],
                "status": agent.get("agent_status"),
                "session": session_id,
            }
            for agent in herdr("agent", "list")["agents"]
            if agent.get("pane_id") in panes
        ),
        None,
    )


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
            # Already running in another workspace: talk to that agent, never a second one.
            return deliver(elsewhere, text, home)
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
        return launch(workspace_id, chosen, pane, text, home)


def launch(workspace_id, chosen, pane, text, home, *, options=(), cwd=None, launcher=None):
    """Resume once with Docker enabled, into a fresh or verified empty shell pane."""
    session_id = chosen["id"]
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
        extra=[*options, *RESUME[chosen["agent"]], chosen["id"]],
        docker=launcher is None,
        stage=lambda _: str(staged),
    )
    if launcher:
        # Direct Safehouse invocation already contains the executable. Its original
        # permission flags are in options; shell wrappers must not add them again.
        command = shlex.join(launcher) + command[len(chosen["agent"]) :]
    if cwd:
        command = f"cd {shlex.quote(cwd)} && {command}"
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
            current = next((a for a in agents(workspace_id) if a["pane"] == pane), None)
            if (
                current
                and current.get("session") == session_id
                and current.get("agent") == chosen["agent"]
            ):
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
        "warning": f"The resume command was typed in {pane}, but its session is not confirmed yet; {UNCERTAIN}.",
    }
