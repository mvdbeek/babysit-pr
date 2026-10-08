"""Follow-up messages from the dashboard to an agent of a herdr workspace.

A message goes to one agent pane of the named workspace through `herdr agent prompt`,
which types it and presses Enter only while the pane hosts a recognized agent, so text
never reaches a plain shell. Nothing is sent to an agent waiting on a dialog, and
sending does not change the agent's Docker access.

When no agent runs there (usually it exited after finishing), the message can instead
resume one of the sessions recorded in that checkout: the agent's own resume command,
with the message as its prompt, is typed into a new split of the workspace, the same way
launches type their command, so the user's shell wrappers apply. A session already open
in a pane gets the message there; one just resumed or owned by a babysit watch is
never resumed again.
"""

import fcntl
import hashlib
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
import claude_accounts
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
# How long a stop waits for the agent to leave its turn, in milliseconds.
STOP_WAIT = 5000
_answer_lock = threading.Lock()
_resume_lock = threading.RLock()
_recent: dict[str, tuple[str, float]] = {}
# Once any Codex answer keystroke was attempted, retries must finish in the terminal.
# Async questions can retain only an acknowledgment in the transcript after answering.
_codex_answered: set[tuple[str, str]] = set()


def clean(text):
    return CONTROL.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))


def agents(workspace_id, interactions=False):
    """The agents running in a herdr workspace, for choosing whom to message.

    Only a workspace on a Git checkout qualifies, the same ones the viewer shows.
    """
    workspace_viewer.workspace_checkout(workspace_id)  # Validates the workspace exists.
    found = [
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
    if interactions:
        for agent in found:
            if agent["status"] == "blocked":
                try:
                    agent["interaction"] = live_interaction(agent)
                except (ValueError, subprocess.SubprocessError):
                    agent["interaction"] = None
    return found


def sessions(workspace_id, home=None):
    """Sessions recorded in the workspace's checkout that a message could resume."""
    root = workspace_viewer.workspace_checkout(workspace_id)
    watched = {
        job.get("session_id") for job in watch_jobs(home) if job.get("status") not in ENDED_WATCHES
    }
    return [
        {
            **{
                k: v
                for k, v in session.items()
                if k in {"id", "agent", "title", "updated", "claude_account"}
            },
            "watched": session["id"] in watched,
        }
        for session in workspace_viewer.sessions(root)
    ]


def send(request, home=None, *, expected_session=None):
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
    if expected_session is not None and target.get("session") != expected_session:
        raise ValueError("The agent session changed; reload the agent list")
    with _resume_lock:
        return prompt(target, text)


def docker_status(workspace_id):
    """Report observed access; a failed probe must never look like disabled access."""
    result = agents(workspace_id, interactions=True)
    for target in result:
        target["docker"] = None
        try:
            info, _, _, proc = agent_docker.inspect(target)
            if (info.get("agent_session") or {}).get("value") != target["session"]:
                raise ValueError("The agent changed; refresh")
            target["docker"] = agent_docker.has_access(proc["pid"])
        except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
            target["docker_error"] = str(exc)
    return result


def set_docker(request, home=None):
    """Change access independently of sending, for exactly the selected session."""
    if set(request) != {"workspace", "pane", "session", "enabled"} or not (
        isinstance(request["enabled"], bool)
        and all(
            isinstance(request[k], str) and request[k] for k in ("workspace", "pane", "session")
        )
    ):
        raise ValueError("Invalid Docker access parameters")
    with _resume_lock:
        target = next(
            (a for a in agents(request["workspace"]) if a["pane"] == request["pane"]), None
        )
        if target is None or target.get("session") != request["session"]:
            raise ValueError("That session is no longer running in this pane; refresh")
        return change_docker(target, request["enabled"], home)


def change_docker(target, enabled, home):
    """Restart an idle agent with the requested access, without submitting a prompt."""
    try:
        info, procs, state, proc = agent_docker.inspect(target)
        if (info.get("agent_session") or {}).get("value") != target.get("session"):
            raise ValueError("The agent changed; refresh")
        if agent_docker.has_access(proc["pid"]) == enabled:
            return {"pane": target["pane"], "docker": enabled, "warning": None}
        record = agent_docker.prepare(target, info, procs, state, proc, enabled)
        session_id = record["session_id"]
        if not workspace_viewer.SESSION.fullmatch(session_id):
            raise ValueError(
                "Could not verify the current conversation; refresh before changing Docker access"
            )
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
            raise ValueError(
                "The current session is not recorded yet; wait before changing Docker access"
            )
        owner = watch_owner(session_id, record["agent"], home, chosen.get("claude_config_dir"))
        if owner:
            raise ValueError(f"Not restarted: {owner}")
        agent_docker.quit_agent(record)
        result = launch(
            info["workspace_id"],
            chosen,
            target["pane"],
            "",
            home,
            options=record["options"],
            cwd=record["cwd"],
            launcher=record["launcher"],
        )
        observed = None
        if not result["warning"]:
            try:
                current, _, _, proc = agent_docker.inspect(target)
                if (current.get("agent_session") or {}).get("value") == target["session"]:
                    observed = agent_docker.has_access(proc["pid"])
            except (ValueError, RuntimeError, OSError, subprocess.SubprocessError):
                pass  # The restart happened; report uncertainty, never imply no action.
            if observed != enabled:
                result["warning"] = (
                    "Docker access did not match the requested setting; check Collie."
                )
        return {"pane": target["pane"], "docker": observed, "warning": result["warning"]}
    except RuntimeError as exc:
        raise ValueError(str(exc)) from exc


def prompt(target, text):
    """Type the message into a running agent's pane and submit it."""
    pane = target["pane"]
    if target["status"] == "blocked":
        raise ValueError(
            "The agent is waiting on a question or approval; "
            "use the transcript question card, or answer it in Collie"
        )
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
                "The agent is waiting on a question or approval; "
                "use the transcript question card, or answer it in Collie"
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


def interrupt(request):
    """Stop the turn a working agent is on, as Esc does in its terminal.

    Both Claude Code and Codex interrupt the running turn on Esc and keep the session
    open, so the agent can be messaged again. Only a pane still working on the named
    session gets the key: an idle agent's Esc would edit its prompt instead.
    """
    if set(request) != {"workspace", "pane", "session"} or not (
        all(isinstance(request[k], str) and request[k] for k in ("workspace", "pane"))
        and (request["session"] is None or isinstance(request["session"], str))
    ):
        raise ValueError("Invalid stop parameters")
    pane = request["pane"]

    def current():
        return next((a for a in agents(request["workspace"]) if a["pane"] == pane), None)

    # Typing an answer into a dialog holds the answer lock; Esc must not land mid-answer.
    with _resume_lock, _answer_lock:
        target = current()
        if target is None or target.get("session") != request["session"]:
            raise ValueError("That session is no longer running in this pane; refresh")
        if target["status"] != "working":
            raise ValueError("The agent is not working on anything right now; refresh")
        try:
            herdr("agent", "send-keys", pane, "esc")
        except ValueError as exc:
            raise ValueError(f"herdr could not send Esc: {str(exc)[-300:]}") from exc
    # The wait holds no lock, so other workspaces' messages need not queue behind it.
    try:
        herdr(
            "agent",
            "wait",
            pane,
            "--until",
            "idle",
            "--until",
            "done",
            "--until",
            "blocked",
            "--timeout",
            str(STOP_WAIT),
        )
    except (ValueError, subprocess.SubprocessError):
        # The wait also fails when the agent exited after Esc: only a pane still
        # working on the session means the stop did not take.
        try:
            after = current()
        except (ValueError, subprocess.SubprocessError):
            after = target
        if after and after.get("session") == request["session"] and after["status"] == "working":
            return {
                "stopped": False,
                "pane": pane,
                "warning": "Esc was sent, but the agent still shows as working; check it in Collie.",
            }
    return {"stopped": True, "pane": pane, "warning": None}


def squash(text):
    return " ".join(text.split())


def live_interaction(target):
    """Expose a blocked pane even when its pending tool is absent from the transcript.

    Only a complete, single-choice, single-question Claude dialog can be answered
    from the screen. Other dialogs remain visible without guessing their controls.
    """
    screen = run("herdr", "agent", "read", target["pane"], "--source", "visible")
    result = {"screen": screen, "question": None}
    if target["agent"] != "claude" or not target.get("session"):
        return result
    lines = screen.splitlines()
    rules = [i for i, line in enumerate(lines) if RULE.match(line)]
    if len(rules) < 2 or "Esc to cancel" not in screen or "ctrl+g to edit" in screen:
        return result
    start, end = rules[-2:]
    body = lines[start + 1 : end]
    if not body or "Chat about this" not in "\n".join(lines[end + 1 :]):
        return result
    body = [line for line in body if line.strip()]
    if not body or not re.fullmatch(r"\s*☐\s+[^☐✔←→]+", body[0]):
        return result
    numbered = []
    for index, line in enumerate(body[1:], 1):
        match = re.fullmatch(r"\s*(?:❯\s*)?(\d)\.\s+(.+?)\s*", line)
        if match:
            numbered.append((index, int(match[1]), match[2]))
    if (
        len(numbered) < 2
        or numbered[-1][2] != "Type something."
        or [n for _, n, _ in numbered] != list(range(1, len(numbered) + 1))
        or len(numbered) > 9
        or any(label.startswith("[") for _, _, label in numbered)
    ):
        return result
    question = {
        "header": body[0].strip().removeprefix("☐").strip(),
        "question": squash(" ".join(body[1 : numbered[0][0]])),
        "multi": False,
        "options": [
            {
                "label": label,
                "description": squash(" ".join(body[index + 1 : numbered[i + 1][0]])),
            }
            for i, (index, _, label) in enumerate(numbered[:-1])
        ],
    }
    if not question["question"]:
        return result
    signature = hashlib.sha256(json.dumps(question, sort_keys=True).encode()).hexdigest()
    result["question"] = {
        "id": f"screen:{signature}",
        "name": "AskUserQuestion",
        "role": "tool",
        "questions": [question],
        "output": None,
    }
    return result


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


def wait_for(pane, shows, read=dialog):
    deadline = time.monotonic() + DIALOG_WAIT
    while True:
        if shows(read(pane)):
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


def answer_steps(questions, answers, agent="claude"):
    """Validate one answer per question; return the keys (or text) each one needs."""
    if not isinstance(answers, list) or len(answers) != len(questions):
        raise ValueError("Answer every question")
    steps = []
    for question, given in zip(questions, answers, strict=True):
        count = len(question["options"])
        if agent == "codex" and question["multi"]:
            raise ValueError("Multiple selections are not supported by this Codex dialog")
        # Each choice is one digit key, "Type something." included.
        if (agent == "claude" and not count) or count >= 9:
            raise ValueError("This question has too many options to answer here; use Collie")
        if not isinstance(given, dict) or set(given) - {"options", "text"} or len(given) != 1:
            raise ValueError("Choose options or write an answer for each question")
        if "text" in given:
            text = given["text"]
            text = squash(clean(text)) if isinstance(text, str) else ""
            if question["multi"] or not text or len(text) > MAX_ANSWER:
                raise ValueError(f"Write an answer of 1–{MAX_ANSWER:,} characters")
            # "Type something." follows the options.
            steps.append(
                [("text", text)]
                if agent == "codex"
                else [("key", str(count + 1)), ("text", text), ("key", "enter")]
            )
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
    """Serialize answers, including validation, so a queued duplicate is checked again."""
    with _answer_lock:
        return answer_locked(request)


def answer_locked(request):
    """Answer the question a running agent is waiting on, through its dialog."""
    if set(request) != {"workspace", "pane", "session", "tool", "answers"} or not all(
        isinstance(request[k], str) and request[k] for k in ("workspace", "pane", "session", "tool")
    ):
        raise ValueError("Invalid answer parameters")
    pane = request["pane"]
    target = next((a for a in agents(request["workspace"]) if a["pane"] == pane), None)
    if target is None or target["agent"] not in RESUME or target["session"] != request["session"]:
        raise ValueError("That agent session is no longer running in this pane; refresh")
    if target["status"] != "blocked":
        raise ValueError("The agent is not waiting on a question; refresh")
    if request["tool"].startswith("screen:"):
        asked = live_interaction(target)["question"]
        if not asked or asked["id"] != request["tool"]:
            raise ValueError("The question on the agent's screen changed; refresh")
    else:
        root = workspace_viewer.workspace_checkout(request["workspace"])
        entries = workspace_viewer.transcript(root, request["session"])["entries"]
        asked = next(
            (
                e
                for e in reversed(entries)
                if e["role"] == "tool" and e.get("id") == request["tool"]
            ),
            None,
        )
    if not asked or not workspace_viewer.question_pending(asked):
        raise ValueError("That question was already answered; refresh")
    expected = {
        "claude": {"AskUserQuestion"},
        "codex": {"request_user_input", "request_user_input_async"},
    }
    if asked["name"] not in expected[target["agent"]]:
        raise ValueError("That question's agent is no longer running in this pane; refresh")
    questions = asked["questions"]
    steps = answer_steps(questions, request["answers"], target["agent"])
    if target["agent"] == "codex":
        return answer_codex(request, questions, steps)
    typed = False
    try:
        for question, keys in zip(questions, steps, strict=True):
            # The agent may have moved on, or exited and left its dialog on screen.
            current: dict = next((a for a in agents(request["workspace"]) if a["pane"] == pane), {})
            if (current.get("agent"), current.get("session"), current.get("status")) != (
                "claude",
                request["session"],
                "blocked",
            ):
                raise ValueError("the agent stopped waiting")
            if request["tool"].startswith("screen:"):
                live = live_interaction(current)["question"]
                if not live or live["id"] != request["tool"]:
                    raise ValueError("the dialog changed")
            if not wait_for(pane, shows_question(question)):
                raise ValueError("the dialog did not show the next question")
            for kind, value in keys:
                if kind == "text":
                    # The text field opens on the keypress before; its editor hint
                    # shows once it has focus.
                    if not wait_for(pane, lambda screen: "ctrl+g to edit" in screen):
                        raise ValueError("the answer field did not open")
                    run("herdr", "pane", "send-text", pane, value)
                else:
                    if value == str(len(question["options"]) + 1) and "ctrl+g to edit" in dialog(
                        pane
                    ):
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


def codex_dialog(pane):
    """Only the final Codex question overlay, bounded by its progress and footer.

    Codex CLI 0.160 renders `Question i/n` and `enter to submit answer/all`.
    Unlike Claude, it has no horizontal rule around the dialog.
    """
    screen = run("herdr", "agent", "read", pane, "--source", "visible")
    headings = list(re.finditer(r"(?m)^\s*Question \d+/\d+[^\n]*\n", screen))
    if not headings:
        return ""
    screen = screen[headings[-1].start() :]
    footer = re.search(r"(?m)(?:^\s*|\|\s*)enter to submit (?:answer|all)\b", screen)
    return screen[: footer.end()] if footer else ""


def codex_question_matches(screen, question, index, total):
    lines = screen.strip().splitlines()
    if not lines or not re.fullmatch(
        rf"Question {index + 1}/{total}(?: \(\d+ unanswered\))?(?: · auto-resolves in .+)?",
        lines[0].strip(),
    ):
        return False
    # The prompt ends at the first option or composer. Text elsewhere on the screen
    # (including a similar earlier question) must not authorize sending keys.
    body = "\n".join(lines[1:])
    stop = re.search(r"(?m)^\s*(?:› |\d+\. |Type your answer|Add notes)", body)
    if not stop or squash(body[: stop.start()]) != squash(question["question"]):
        return False
    return all(
        re.search(rf"(?m)^\s*(?:›\s*)?{i}\. {re.escape(squash(option['label']))}(?:\s|$)", body)
        for i, option in enumerate(question["options"], 1)
    )


def answer_codex(request, questions, steps):
    pane = request["pane"]
    identity = (request["session"], request["tool"])
    if identity in _codex_answered:
        raise ValueError("An answer was already attempted; check the question in Collie")
    typed = False

    def send(kind, value, shows):
        nonlocal typed
        if not wait_for(pane, shows, read=codex_dialog):
            raise ValueError("the expected question or answer field is not visible")
        current: dict = next((a for a in agents(request["workspace"]) if a["pane"] == pane), {})
        if (current.get("agent"), current.get("session"), current.get("status")) != (
            "codex",
            request["session"],
            "blocked",
        ):
            raise ValueError("the agent stopped waiting")
        # An I/O error may arrive after delivery. Never offer to replay the keys.
        typed = True
        _codex_answered.add(identity)
        if kind == "text":
            run("herdr", "pane", "send-text", pane, value)
        else:
            herdr("agent", "send-keys", pane, value)

    try:
        for index, (question, keys) in enumerate(zip(questions, steps, strict=True)):

            def matches(screen, question=question, index=index):
                return codex_question_matches(screen, question, index, len(questions))

            def options_visible(screen):
                return matches(screen) and "to clear notes" not in screen

            kind, value = keys[0]
            if kind == "key":
                send(kind, value, options_visible)
                continue
            placeholder = "Type your answer (optional)"
            if question["options"]:
                screen = codex_dialog(pane)
                other = len(question["options"]) + 1
                selected = re.search(r"(?m)^\s*› (\d+)\. ", screen)
                if (
                    not options_visible(screen)
                    or not selected
                    or not re.search(rf"(?m)^\s*(?:› )?{other}\. None of the above\s*$", screen)
                    and not re.search(
                        rf"(?m)^\s*(?:› )?{other}\. None of the above\s+Optionally,", screen
                    )
                ):
                    raise ValueError("the dialog does not offer an empty Other answer")
                # Digits submit immediately, including Other. Navigate to Other first,
                # then open its notes so free text never selects an unrelated option.
                for selection in range(int(selected[1]), other):
                    send(
                        "key",
                        "down",
                        lambda s, selection=selection: (
                            options_visible(s) and bool(re.search(rf"(?m)^\s*› {selection}\. ", s))
                        ),
                    )
                send(
                    "key",
                    "tab",
                    lambda s, other=other: (
                        options_visible(s)
                        and bool(re.search(rf"(?m)^\s*› {other}\. None of the above(?:\s|$)", s))
                    ),
                )

                placeholder = "Add notes"

            def empty(s, placeholder=placeholder):
                return matches(s) and bool(re.search(rf"(?m)^\s*› {re.escape(placeholder)}\s*$", s))

            send("text", value, empty)
            send("key", "enter", matches)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        if typed:
            raise ValueError(f"Answer attempted ({exc}); check or finish it in Collie") from exc
        raise ValueError(f"Nothing answered ({exc}); refresh or answer it in Collie") from exc
    return {"answered": True, "pane": pane}


def watch_owner(session_id, agent, home, config_dir=None):
    """Why the babysit watcher owns this session, if it does.

    A watch resumes its registered session headless for repairs, under a per-session
    lock; an interactive copy alongside would make two writers on one conversation.
    """
    lock_home = (
        Path(config_dir or claude_runner.config_home())
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
            return prompt(elsewhere, text)
        owner = watch_owner(session_id, chosen["agent"], home, chosen.get("claude_config_dir"))
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
    """Resume once into a fresh or verified empty shell pane, optionally with a prompt."""
    session_id = chosen["id"]
    # The prompt is read back from a private file, as launches do: a typed line has
    # a length ceiling, and command-substitution output needs no escaping.
    prompts = Path(home or os.environ.get("TMPDIR") or "/tmp") / "message-prompts"
    prompts.mkdir(mode=0o700, parents=True, exist_ok=True)
    prune(prompts)
    staged = prompts / f"resume-{uuid.uuid4()}"
    if text:
        descriptor = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text + "\n")
    command = wt.agent_command(
        chosen["agent"],
        text,
        extra=[*options, *RESUME[chosen["agent"]], chosen["id"]],
        stage=lambda _: str(staged),
    )
    if launcher:
        # Direct Safehouse invocation already contains the executable. Its original
        # permission flags are in options; shell wrappers must not add them again.
        command = shlex.join(launcher) + command[len(chosen["agent"]) :]
    if chosen["agent"] == "claude" and chosen.get("claude_config_dir"):
        command = claude_accounts.shell_command(
            command,
            chosen["claude_config_dir"],
            subscription=chosen.get("claude_account") not in (None, "default"),
            config_env=chosen.get("claude_config_env", False),
        )
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
