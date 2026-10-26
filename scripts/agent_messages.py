"""Follow-up messages from the dashboard to an agent of a herdr workspace.

A message goes to one agent pane of the named workspace through `herdr agent prompt`,
which types it and presses Enter only while the pane hosts a recognized agent, so text
never reaches a plain shell. Nothing is sent to an agent waiting on a dialog, and
sending does not change the agent's Docker access. A Claude agent that herdr reports
as blocked while its screen shows an empty prompt box (and no dialog) is idle in fact;
the message is then pasted into that box and submitted.

When no agent runs there (usually it exited after finishing), the message can instead
resume one of the sessions recorded in that checkout: the agent's own resume command,
with the message as its prompt, is typed into a new split of the workspace, the same way
launches type their command, so the user's shell wrappers apply. A session already open
in a pane gets the message there; one just resumed or owned by a babysit watch is
never resumed again. The reader may cancel the watch that owns a session, and resume it
in the same request, unless the watch is in the middle of a repair. A resume may also go
without a message, leaving the session waiting for the reader in Collie.
"""

import contextlib
import hashlib
import json
import os
import re
import shlex
import subprocess
import threading
import time
import uuid
from pathlib import Path

import agent_docker
import claude_accounts
import cron_agents
import exit_watch
import herdr_handoff
import machines
import remote_probe
import sandbox
import session_locks
import workspace_viewer
import wt
from pr_workspaces import PANE, herdr, run

MAX_MESSAGE = 32000
# The text is typed into a terminal: a carriage return would submit early, and other
# control characters (an escape sequence ending bracketed paste, say) could act as
# keystrokes. Quoted diff lines come from files nobody vetted, so only newlines and
# tabs survive.
CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
UNCERTAIN = "check it in Collie before resending"
QUIT_UNWATCHED = "The agent will not quit by itself: its quit watch could not be set up."
RESUME_WAIT = 20
# A worker's resume prompts, private files there; one is read once, as its agent starts.
REMOTE_PROMPTS = "~/.local/state/babysit-pr/message-prompts"
PROMPT_KEEP = 86400
# Codex resumes in-process: on the shared background server a session whose feature
# settings differ opens a dialog whose default, Cancel, a typed message then selects.
RESUME = {"claude": ["--resume"], "codex": ["--no-daemon", "resume"]}
# A resumed agent can take a while to be recognized; until then a repeat is refused.
RECENT_RESUME = 120
ENDED_WATCHES = {"closed", "stopped"}
REPAIR_RUNNING = "a babysit repair is running in this session right now"
# Claude Code's question dialog (as of 2.1): a number key picks an option, and on a
# single-choice question also moves on; the option after the last, "Type something.",
# takes typed text and Enter. On a multiple-choice question number keys toggle options
# and Right moves on. With several questions or a multiple-choice one, a review tab
# follows, where 1 submits. Each step waits for the dialog to show what it expects.
DIALOG_WAIT = 4
RULE = re.compile(r"^\s*─{8,}\s*$")
# Newer Claude Code sets a question's text off with a quote bar.
QUOTE = re.compile(r"^(\s*)│ ?")
# Any other Claude dialog of numbered options (a permission prompt, a plan approval)
# takes the option's number key, which picks it and closes or advances the dialog.
OPTION = re.compile(r"\s*(?:❯\s*)?(\d)\.\s+(.+?)\s*")
DIALOG_HINTS = ("esc to cancel", "enter to select", "enter to confirm")
MAX_ANSWER = 2000
# How long a stop waits for the agent to leave its turn, in milliseconds.
STOP_WAIT = 5000
# Locks by (kind, name): a slow action waits only for others on its checkout, session or
# pane, never for those elsewhere. Taken in this order of kinds.
LOCK_ORDER = ("checkout", "session", "pane")
_locks_guard = threading.Lock()
_locks: dict[tuple[str, str], list] = {}
_recent: dict[str, tuple[str, float]] = {}
# Once any Codex answer keystroke was attempted, retries must finish in the terminal.
# Async questions can retain only an acknowledgment in the transcript after answering.
_codex_answered: set[tuple[str, str]] = set()
# A default meaning "not resolved yet", since None means "resolved: no workspace open".
_UNSET = object()


@contextlib.contextmanager
def locked(*keys):
    """Hold the locks of these (kind, name) keys; a thread may take one it holds again.

    Every caller takes its locks in one order (checkouts, then sessions, then panes), all
    at once or a pane's last, so no two callers can each wait for the other. A lock nobody
    holds or waits for is forgotten.
    """
    ordered = sorted(set(keys), key=lambda key: (LOCK_ORDER.index(key[0]), key[1]))
    with _locks_guard:
        entries = [_locks.setdefault(key, [threading.RLock(), 0]) for key in ordered]
        for entry in entries:
            entry[1] += 1
    try:
        with contextlib.ExitStack() as stack:
            for entry in entries:
                stack.enter_context(entry[0])
            yield
    finally:
        with _locks_guard:
            for key, entry in zip(ordered, entries, strict=True):
                entry[1] -= 1
                if not entry[1]:
                    del _locks[key]


def clean(text):
    return CONTROL.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))


def lock_name(machine, name):
    """A lock's name: a worker's checkouts and panes are its own, apart from this machine's."""
    return name if machine is None else f"{machine.id}:{name}"


def listed_agents(machine=None):
    """herdr's agents; a worker's only those whose fields a pane action can use."""
    found = herdr("agent", "list", machine=machine)["agents"]
    if machine is None:
        return found
    return [
        {
            **agent,
            "terminal_title_stripped": remote_probe.display_text(
                agent.get("terminal_title_stripped")
            ),
        }
        for agent in (found if isinstance(found, list) else [])
        if isinstance(agent, dict)
        and isinstance(agent.get("pane_id"), str)
        and PANE.fullmatch(agent["pane_id"])
        and all(
            isinstance(agent.get(field), (str, type(None)))
            for field in ("workspace_id", "agent", "agent_status", "terminal_title_stripped")
        )
        and isinstance(agent.get("agent_session") or {}, dict)
        and isinstance((agent.get("agent_session") or {}).get("value"), (str, type(None)))
    ]


def agents(workspace_id, interactions=False, live=_UNSET, machine=None):
    """The agents running in a herdr workspace, for choosing whom to message.

    Only a workspace on a Git checkout qualifies, the same ones the viewer shows.
    ``live`` is the open workspace a caller already resolved for this ID: None when it
    found none open, so it is not looked up again. A worker's come from its own herdr.
    """
    # Follows the checkout: a closed workspace has none, a reopened one has a new ID.
    if live is _UNSET:
        live = workspace_viewer.live_workspace(workspace_id, machine)
    if live is None:
        return []
    found = [
        {
            "pane": agent["pane_id"],
            "agent": agent.get("agent"),
            "status": agent.get("agent_status"),
            "title": agent.get("terminal_title_stripped") or None,
            # The session the pane runs, so its transcript's open question can be answered.
            "session": (agent.get("agent_session") or {}).get("value"),
        }
        for agent in listed_agents(machine)
        if agent.get("workspace_id") == live and agent.get("pane_id")
    ]
    if interactions:
        for agent in found:
            if agent["status"] == "blocked":
                try:
                    agent["interaction"] = live_interaction(agent, machine)
                # herdr_handoff reports a failed herdr call as a RuntimeError.
                except (ValueError, RuntimeError, OSError, subprocess.SubprocessError):
                    agent["interaction"] = None
    return found


def workspace_root(workspace_id, machine=None):
    """The checkout a workspace was opened on: this machine's, or a worker's as it spells it."""
    if machine is None:
        return workspace_viewer.workspace_checkout(workspace_id)
    return workspace_viewer.remote_checkout(machine, workspace_id)


def recorded(root, home=None, machine=None):
    """The sessions recorded in a checkout, read on the worker it is on."""
    if machine is None:
        return workspace_viewer.sessions(root)
    return workspace_viewer.remote_sessions(home, machine, root)


def sessions(workspace_id, home=None, machine=None):
    """Sessions recorded in the workspace's checkout that a message could resume."""
    return checkout_sessions(workspace_root(workspace_id, machine), home, machine)


def checkout_sessions(root, home=None, machine=None):
    """Sessions recorded in a checkout that a message could resume."""
    # The watch that owns a session is named up front, so the reader can cancel it
    # before resuming instead of meeting a refusal.
    watched = {
        job.get("session_id"): job
        for job in watch_jobs(home)
        if job.get("status") not in ENDED_WATCHES
    }
    return [
        {
            **{
                k: v
                for k, v in session.items()
                if k in {"id", "agent", "title", "updated", "claude_account"}
            },
            "watched": session["id"] in watched,
            **(
                {"watch": watch_summary(watched[session["id"]])} if session["id"] in watched else {}
            ),
        }
        for session in recorded(root, home, machine)
    ]


def send(request, home=None, *, expected_session=None, checkout=None, machine=None):
    """Submit one message to one agent of a workspace; report what herdr saw.

    With a checkout instead of a workspace (a Workspaces-tab row listed while none was
    open), only a resume can be sent: it reopens a workspace there if none is open.
    With ``exit_when_done`` the agent is asked to mark the end of its answer, and the
    dashboard exits it and closes its pane once it has (`wt --exit-when-done`).
    A ``machine`` names the worker the workspace or checkout is on.
    """
    if set(request) - {"workspace", "pane", "text", "resume", "stop_watch", "exit_when_done"}:
        raise ValueError("Invalid message parameters")
    quit_when_done = request.get("exit_when_done", False)
    if not isinstance(quit_when_done, bool):
        raise ValueError("Expected an exit_when_done flag")
    stop_watch = request.get("stop_watch")
    if stop_watch is not None and (
        not isinstance(stop_watch, str) or not stop_watch or request.get("resume") is None
    ):
        raise ValueError("Only a resume can cancel the watch that owns its session")
    text = request.get("text")
    text = clean(text) if isinstance(text, str) else None
    # A resume may go without one: the session then waits in Collie for the reader.
    if request.get("resume") is not None and not (text or "").strip():
        text = ""
    elif not text or not text.strip() or len(text) > MAX_MESSAGE:
        raise ValueError(f"Write a message of 1–{MAX_MESSAGE:,} characters")
    if checkout is not None and (request.get("workspace") is not None or not request.get("resume")):
        raise ValueError("Without a herdr workspace, only a recorded session can be resumed")
    if quit_when_done and not text:
        raise ValueError("Write a message for the agent to finish before it quits")
    if quit_when_done and home is None:
        raise ValueError("Quitting when done needs the dashboard's state directory")
    if quit_when_done and machine is not None:
        # Exit watches are followed on this machine's herdr and transcripts only.
        raise ValueError(f"Quitting when done is not available for agents on {machine.label} yet")
    marker = exit_watch.new_marker() if quit_when_done else None
    if marker:
        text += exit_watch.follow_up(marker)
    if request.get("resume") is not None:
        if request.get("pane") is not None:
            raise ValueError("Resume a session or message a running agent, not both")
        result = resume(
            request.get("workspace"),
            request["resume"],
            text,
            home,
            root=checkout,
            stop_watch=stop_watch,
            machine=machine,
        )
        return watch_until_done(result, home, marker, request["text"]) if marker else result
    pane = request.get("pane")
    if pane is not None and not isinstance(pane, str):
        raise ValueError("Choose an agent")
    running = agents(request.get("workspace"), machine=machine)
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
    result = prompt(target, text, machine)
    return watch_until_done(result, home, marker, request["text"]) if marker else result


def watch_until_done(result, home, marker, message):
    """Record the exit watch for the agent that took a message asking it to mark its end.

    The pane herdr shows the agent in names its checkout and session. The message is
    already typed: a watch that cannot be recorded leaves the agent open and says so.
    """

    def unwatched():
        return {
            **result,
            "warning": " ".join(filter(None, [result.get("warning"), QUIT_UNWATCHED])),
        }

    pane = result.get("pane")
    try:
        found = next(
            (a for a in herdr("agent", "list")["agents"] if pane and a.get("pane_id") == pane),
            None,
        )
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError):
        return unwatched()
    if (
        not found
        or found.get("agent") not in RESUME
        or not isinstance(found.get("cwd"), str)
        or not isinstance(found.get("workspace_id"), str)
    ):
        return unwatched()
    now = time.time()
    first = next((line.strip() for line in message.splitlines() if line.strip()), "")
    watch = {
        "id": uuid.uuid4().hex,
        "label": f"Message: {first[:80]}",
        "machine": None,
        "path": os.path.realpath(found["cwd"]),
        "workspace_id": found["workspace_id"],
        "pane_id": pane,
        "agent": found["agent"],
        "claude_account": None,
        # A resume names the session it confirmed; herdr's reading may lag behind it.
        "session_id": result.get("resumed") or (found.get("agent_session") or {}).get("value"),
        "marker": marker,
        "created_at": now,
        "follow_up": True,
        "exit": exit_watch.watching(now),
    }
    try:
        exit_watch.register(home, watch)
    except OSError:
        return unwatched()
    return {**result, "exit_watch": watch["id"]}


def docker_status(workspace_id, live=_UNSET, machine=None):
    """Report observed access; a failed probe must never look like disabled access.

    A worker's agents have none to report: their sandbox is not Safehouse on this Mac.
    """
    result = agents(workspace_id, interactions=True, live=live, machine=machine)
    if machine is not None:
        for target in result:
            target.update(
                docker=None,
                docker_supported=False,
                docker_error="Docker access can only be inspected for agents on this Mac",
            )
        return result
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
    # The agent is quit and launched again: no resume of its session or in its checkout,
    # and nothing typed into its pane, may come in between.
    root = workspace_viewer.workspace_checkout(request["workspace"])
    with locked(
        ("checkout", os.path.realpath(root)),
        ("session", request["session"]),
        ("pane", request["pane"]),
    ):
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


def prompt(target, text, machine=None):
    """Type the message into a running agent's pane and submit it."""
    # Never interleaved with another message, an answer or a stop typed into the pane.
    with locked(("pane", lock_name(machine, target["pane"]))):
        return prompt_locked(target, text, machine)


def prompt_locked(target, text, machine=None):
    pane = target["pane"]
    if target["status"] == "blocked":
        if target["agent"] != "claude" or not idle_screen(pane, machine=machine):
            raise ValueError(
                "The agent is waiting on a question or approval; "
                "use the transcript question card, or answer it in Collie"
            )
        return paste(pane, text, machine)
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
            machine=machine,
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
        if "agent_not_running" in detail:
            # The agent left before taking the message, as one stuck in a startup dialog
            # quits on the typed Enter; its pane shows why.
            raise ValueError(
                f"The agent in {pane} exited instead of taking the message; "
                "its pane in Collie may show why"
            ) from exc
        raise ValueError(f"herdr could not deliver the message: {detail[-300:]}") from exc
    return {"sent": True, "pane": pane, "warning": None}


def interrupt(request, machine=None):
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
        return next(
            (a for a in agents(request["workspace"], machine=machine) if a["pane"] == pane), None
        )

    # Typing a message or an answer holds the pane's lock; Esc must not land mid-answer.
    # Only this pane's: a stop never waits for actions on other agents.
    with locked(("pane", lock_name(machine, pane))):
        target = current()
        if target is None or target.get("session") != request["session"]:
            raise ValueError("That session is no longer running in this pane; refresh")
        if target["status"] != "working":
            raise ValueError("The agent is not working on anything right now; refresh")
        try:
            herdr("agent", "send-keys", pane, "esc", machine=machine)
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
            machine=machine,
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


def paste(pane, text, machine=None):
    """Submit a message through the prompt box of an agent herdr wrongly calls blocked.

    `herdr agent prompt` refuses a blocked agent, so the text is pasted (bracketed, as
    herdr does, so newlines do not submit early) and Enter pressed once the prompt box
    shows it. A dialog that opened meanwhile never gets that Enter.
    """
    # Dialog choices type into the same pane; neither interleaves with the other.
    with locked(("pane", lock_name(machine, pane))):
        if not idle_screen(pane, machine=machine):
            raise ValueError("The agent is waiting on a question or approval; answer it first")
        run(*machines.herdr_argv(machine, "pane", "send-text", pane, f"\x1b[200~{text}\x1b[201~"))
        if not wait_for(
            pane, lambda screen: pasted(screen, text), herdr_handoff.read_screen, machine
        ):
            raise ValueError(f"The text did not appear in the prompt box; {UNCERTAIN}")
        herdr("agent", "send-keys", pane, "enter", machine=machine)
    return {"sent": True, "pane": pane, "warning": None}


def pasted(screen, text):
    """Whether Claude's prompt box, under its rule and with no dialog, holds the text."""
    lines = herdr_handoff.SGR.sub("", screen).splitlines()
    tail = "\n".join(lines[-12:]).lower()
    if any(hint in tail for hint in (*DIALOG_HINTS, "esc to interrupt")):
        return False
    prompts = [i for i, line in enumerate(lines) if line.lstrip().startswith("❯")]
    if not prompts or prompts[-1] == 0 or not RULE.match(lines[prompts[-1] - 1]):
        return False
    shown = squash(lines[prompts[-1]].lstrip()[1:])
    start = squash(text)[:20]
    return bool(shown) and (shown.startswith("[Pasted text") or shown.startswith(start))


def idle_screen(pane, screen=None, machine=None):
    """Whether the pane shows Claude's empty prompt box and no dialog below it."""
    if screen is None:
        screen = herdr_handoff.read_screen(pane, machine)
    plain = herdr_handoff.SGR.sub("", screen)
    tail = "\n".join(plain.splitlines()[-12:]).lower()
    # A turn in progress also shows an empty box, above "esc to interrupt".
    return herdr_handoff.empty_claude_composer(screen) and not any(
        hint in tail for hint in (*DIALOG_HINTS, "esc to interrupt")
    )


def squash(text):
    return " ".join(text.split())


def last_run(numbered):
    """The options proper: the numbered lines from the last "1.", after any numbered
    list in the dialog's own text."""
    firsts = [k for k, (_, n, _) in enumerate(numbered) if n == 1]
    return numbered[firsts[-1] :] if firsts else numbered


def unquote(line):
    return QUOTE.sub(r"\1", line)


def dialog_lines(lines):
    """The lines of the dialog ending the screen, between its top rule and key hints.

    A rule that sets off a "Chat about this" option is inside the dialog, not its top.
    """
    hints = [i for i, line in enumerate(lines) if "esc to cancel" in line.lower()]
    if not hints or any(line.strip() for line in lines[hints[-1] + 1 :][2:]):
        return []
    end = hints[-1]
    rules = [
        i
        for i, line in enumerate(lines[:end])
        if RULE.match(line) and "Chat about this" not in "".join(lines[i + 1 : i + 2])
    ]
    return lines[rules[-1] + 1 : end] if rules else []


def screen_choices(lines):
    """A Claude dialog whose options can each be picked from the dashboard.

    Numbered options take their number key. A list without numbers (the folder trust
    prompt) takes arrow keys to its "❯" cursor, then Enter. Multiple-choice check boxes
    and an open text field need more, so they are left to Collie; a "Type something."
    option needs typing and is not offered.
    """
    body = dialog_lines(lines)
    if not body or any("ctrl+g to edit" in line for line in lines[-6:]):
        return None
    matches = [(i, OPTION.fullmatch(line)) for i, line in enumerate(body)]
    numbered = last_run([(i, int(m[1]), m[2]) for i, m in matches if m])
    if len(numbered) >= 2:
        if [n for _, n, _ in numbered] != list(range(1, len(numbered) + 1)) or any(
            re.match(r"\[.\]", label) for _, _, label in numbered
        ):
            return None
        first = numbered[0][0]
        options = [(str(n), label) for _, n, label in numbered if label != "Type something."]
        arrows, cursor = False, None
    else:
        listed = cursor_options(body, lines)
        if not listed:
            return None
        first, labels, cursor = listed
        options = [(str(n), label) for n, label in enumerate(labels, 1)]
        arrows = True
    text = [line.rstrip() for line in body[:first]]
    while text and not text[0].strip():
        text.pop(0)
    indent = min((len(t) - len(t.lstrip()) for t in text if t.strip()), default=0)
    # The cursor moves when the user looks through the options; that is the same dialog.
    shown = [squash(line.replace("❯", " ")) for line in body]
    return {
        "id": "dialog:" + hashlib.sha256(json.dumps(shown).encode()).hexdigest(),
        "text": "\n".join(t[indent:] for t in text).strip(),
        "options": [{"key": key, "label": label} for key, label in options],
        "arrows": arrows,
        "cursor": cursor,
    }


def cursor_options(body, lines):
    """An unnumbered option list: the lines aligned with the one the "❯" cursor marks.

    Returns where it starts, its labels and the cursor's option, or None. A wrapped
    label would read as two options; the cursor check before Enter catches that.
    """
    hints = "\n".join(lines[-4:]).lower()
    if "enter to confirm" not in hints and "enter to select" not in hints:
        return None
    marked = [i for i, line in enumerate(body) if line.lstrip().startswith("❯")]
    if len(marked) != 1:
        return None

    def column(line):
        return len(line) - len(line.lstrip().removeprefix("❯").lstrip())

    at = marked[0]
    start, end = at, at + 1
    while start > 0 and body[start - 1].strip() and column(body[start - 1]) == column(body[at]):
        start -= 1
    while end < len(body) and body[end].strip() and column(body[end]) == column(body[at]):
        end += 1
    labels = [line.lstrip().removeprefix("❯").strip() for line in body[start:end]]
    if len(labels) < 2 or len(labels) > 9:
        return None
    return start, labels, at - start + 1


def live_interaction(target, machine=None):
    """Expose a blocked pane even when its pending tool is absent from the transcript.

    Only a complete, single-choice, single-question Claude dialog can be answered
    from the screen. Other dialogs remain visible without guessing their controls.
    """
    screen = run(
        *machines.herdr_argv(machine, "agent", "read", target["pane"], "--source", "visible")
    )
    result = {"screen": screen, "question": None, "choices": None, "idle": False}
    if target["agent"] != "claude" or not target.get("session"):
        return result
    lines = [unquote(line) for line in screen.splitlines()]
    result["choices"] = screen_choices(lines)
    if not result["choices"]:
        result["idle"] = idle_screen(target["pane"], machine=machine)
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
        match = OPTION.fullmatch(line)
        if match:
            numbered.append((index, int(match[1]), match[2]))
    numbered = last_run(numbered)
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
    result["choices"] = None
    result["question"] = {
        "id": f"screen:{signature}",
        "name": "AskUserQuestion",
        "role": "tool",
        "questions": [question],
        "output": None,
    }
    return result


def dialog(pane, machine=None):
    """The question dialog on the pane's screen, whitespace collapsed.

    It starts at the last rule above it, skipping the one that sets off its "Chat about
    this" line, and ends with its key hints (the review tab has none).
    """
    screen = run(*machines.herdr_argv(machine, "agent", "read", pane, "--source", "visible"))
    lines = [unquote(line) for line in screen.splitlines()]
    hints = [i for i, line in enumerate(lines) if "Esc to cancel" in line]
    end = hints[-1] + 1 if hints else len(lines)
    rules = [
        i
        for i, line in enumerate(lines[:end])
        if RULE.match(line) and "Chat about this" not in "".join(lines[i + 1 : i + 2])
    ]
    # No rule means no dialog: the screen may be a shell the agent left behind.
    return squash("\n".join(lines[rules[-1] : end])) if rules else ""


def wait_for(pane, shows, read=dialog, machine=None):
    deadline = time.monotonic() + DIALOG_WAIT
    while True:
        if shows(read(pane, machine)):
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


def answer(request, home=None, machine=None):
    """Serialize answers, including validation, so a queued duplicate is checked again."""
    with locked(("pane", lock_name(machine, str(request.get("pane"))))):
        return answer_locked(request, home, machine)


def answer_locked(request, home=None, machine=None):
    """Answer the question a running agent is waiting on, through its dialog."""
    if set(request) != {"workspace", "pane", "session", "tool", "answers"} or not all(
        isinstance(request[k], str) and request[k] for k in ("workspace", "pane", "session", "tool")
    ):
        raise ValueError("Invalid answer parameters")
    pane = request["pane"]

    def running():
        return agents(request["workspace"], machine=machine)

    target = next((a for a in running() if a["pane"] == pane), None)
    if target is None or target["agent"] not in RESUME or target["session"] != request["session"]:
        raise ValueError("That agent session is no longer running in this pane; refresh")
    if target["status"] != "blocked":
        raise ValueError("The agent is not waiting on a question; refresh")
    if request["tool"].startswith("screen:"):
        asked = live_interaction(target, machine)["question"]
        if not asked or asked["id"] != request["tool"]:
            raise ValueError("The question on the agent's screen changed; refresh")
    else:
        root = workspace_root(request["workspace"], machine)
        if machine is None:
            entries = workspace_viewer.transcript(root, request["session"])["entries"]
        else:
            entries = workspace_viewer.remote_transcript(home, machine, root, request["session"])[
                "entries"
            ]
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
        return answer_codex(request, questions, steps, machine)
    typed = False
    try:
        for question, keys in zip(questions, steps, strict=True):
            # The agent may have moved on, or exited and left its dialog on screen.
            current: dict = next((a for a in running() if a["pane"] == pane), {})
            if (current.get("agent"), current.get("session"), current.get("status")) != (
                "claude",
                request["session"],
                "blocked",
            ):
                raise ValueError("the agent stopped waiting")
            if request["tool"].startswith("screen:"):
                live = live_interaction(current, machine)["question"]
                if not live or live["id"] != request["tool"]:
                    raise ValueError("the dialog changed")
            if not wait_for(pane, shows_question(question), machine=machine):
                raise ValueError("the dialog did not show the next question")
            for kind, value in keys:
                if kind == "text":
                    # The text field opens on the keypress before; its editor hint
                    # shows once it has focus.
                    if not wait_for(
                        pane, lambda screen: "ctrl+g to edit" in screen, machine=machine
                    ):
                        raise ValueError("the answer field did not open")
                    run(*machines.herdr_argv(machine, "pane", "send-text", pane, value))
                else:
                    if value == str(len(question["options"]) + 1) and "ctrl+g to edit" in dialog(
                        pane, machine
                    ):
                        raise ValueError("an answer field is already open")
                    herdr("agent", "send-keys", pane, value, machine=machine)
                typed = True
        if len(questions) > 1 or any(q["multi"] for q in questions):
            if not wait_for(pane, lambda screen: "Submit answers" in screen, machine=machine):
                raise ValueError("the dialog did not offer to submit")
            herdr("agent", "send-keys", pane, "1", machine=machine)
    except (ValueError, subprocess.SubprocessError) as exc:
        if not typed:
            raise ValueError(
                "The question is not on the agent's screen; answer it in Collie"
            ) from exc
        # Something was typed: never invite answering again from the start.
        raise ValueError(f"Answered in part ({exc}); finish the question in Collie") from exc
    return {"answered": True, "pane": pane}


def choose(request, machine=None):
    """Pick one option of the Claude dialog on an agent's screen, by its number key."""
    with locked(("pane", lock_name(machine, str(request.get("pane"))))):
        if set(request) != {"workspace", "pane", "session", "dialog", "option"} or not all(
            isinstance(request[k], str) and request[k] for k in request
        ):
            raise ValueError("Invalid choice parameters")
        pane = request["pane"]
        target = next(
            (a for a in agents(request["workspace"], machine=machine) if a["pane"] == pane), None
        )
        if target is None or target["agent"] != "claude" or target["session"] != request["session"]:
            raise ValueError("That agent session is no longer running in this pane; refresh")
        if target["status"] != "blocked":
            raise ValueError("The agent is not waiting on a dialog; refresh")
        choices = live_interaction(target, machine)["choices"]
        if not choices or choices["id"] != request["dialog"]:
            raise ValueError("The dialog on the agent's screen changed; refresh")
        if request["option"] not in {option["key"] for option in choices["options"]}:
            raise ValueError("Choose one of the dialog's options")
        if not choices["arrows"]:
            herdr("agent", "send-keys", pane, request["option"], machine=machine)
            return {"chosen": True, "pane": pane}
        wanted = int(request["option"])
        moves = wanted - choices["cursor"]
        for _ in range(abs(moves)):
            herdr("agent", "send-keys", pane, "down" if moves > 0 else "up", machine=machine)

        def on_target(_):
            moved = live_interaction(target, machine)["choices"]
            return bool(moved) and moved["id"] == choices["id"] and moved["cursor"] == wanted

        # Only the cursor moved so far; Enter goes only to the dialog and option chosen.
        if not wait_for(pane, on_target, read=lambda *_: None):
            raise ValueError("The cursor did not reach that option; nothing was confirmed")
        herdr("agent", "send-keys", pane, "enter", machine=machine)
        return {"chosen": True, "pane": pane}


def codex_dialog(pane, machine=None):
    """Only the final Codex question overlay, bounded by its progress and footer.

    Codex CLI 0.160 renders `Question i/n` and `enter to submit answer/all`.
    Unlike Claude, it has no horizontal rule around the dialog.
    """
    screen = run(*machines.herdr_argv(machine, "agent", "read", pane, "--source", "visible"))
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


def answer_codex(request, questions, steps, machine=None):
    pane = request["pane"]
    identity = (request["session"], request["tool"])
    if identity in _codex_answered:
        raise ValueError("An answer was already attempted; check the question in Collie")
    typed = False

    def send(kind, value, shows):
        nonlocal typed
        if not wait_for(pane, shows, codex_dialog, machine):
            raise ValueError("the expected question or answer field is not visible")
        current: dict = next(
            (a for a in agents(request["workspace"], machine=machine) if a["pane"] == pane), {}
        )
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
            run(*machines.herdr_argv(machine, "pane", "send-text", pane, value))
        else:
            herdr("agent", "send-keys", pane, value, machine=machine)

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
                screen = codex_dialog(pane, machine)
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


class Refused(ValueError):
    """A refusal the dashboard can act on: its code and details travel with the message."""

    def __init__(self, message, code, **details):
        super().__init__(message)
        self.details = {"code": code, **details}


def owning_watch(session_id, home):
    """The babysit watch, not yet ended, that resumes this session itself."""
    return next(
        (
            job
            for job in watch_jobs(home)
            if job.get("session_id") == session_id and job.get("status") not in ENDED_WATCHES
        ),
        None,
    )


def watch_summary(job):
    """What the composer shows of a watch that owns a session."""
    pr = (job.get("snapshot") or {}).get("pr") or {}
    return {
        "id": job.get("id"),
        "status": job.get("status"),
        "stop_after_run": bool(job.get("stop_after_run")),
        "repo": job.get("repo"),
        "number": pr.get("number"),
        "title": pr.get("title"),
        "branch": job.get("branch") or pr.get("head_branch"),
        "url": job.get("url"),
    }


def owned_message(job):
    return f"babysit watch {job.get('id')} ({job.get('status')}) resumes this session itself; stop it first"


def watch_owner(session_id, agent, home, config_dir=None):
    """Why the babysit watcher owns this session, if it does.

    A watch resumes its registered session headless for repairs, under a per-session
    lock; an interactive copy alongside would make two writers on one conversation.
    """
    if session_locks.held(agent, session_id, config_dir):
        return REPAIR_RUNNING
    job = owning_watch(session_id, home)
    return owned_message(job) if job else None


def claim_from_watch(session_id, agent, home, config_dir=None, stop_watch=None, machine=None):
    """Refuse a resume the watcher owns, after cancelling the watch the reader confirmed.

    The confirmed watch is cancelled only while it still owns the session. A watch in
    the middle of a repair stops only once the repair finishes, so nothing resumes then;
    otherwise the lock and the queue are read again before the resume goes ahead. A
    worker's session lease is the one this machine holds for it.
    """
    job = owning_watch(session_id, home)
    if job and stop_watch is not None and job.get("id") == stop_watch:
        from dashboard import cancel_watch  # The dashboard imports this module first.

        stopped = cancel_watch(Path(home), stop_watch)
        if stopped["status"] not in ENDED_WATCHES:
            raise Refused(
                f"Not resumed: babysit watch {stop_watch} is running a repair; it stops once "
                "that repair finishes. Send the message then.",
                "watch_finishing_repair",
                watch=watch_summary(stopped),
            )
    if session_locks.held(agent, session_id, config_dir, machine.id if machine else None):
        raise Refused(f"Not resumed: {REPAIR_RUNNING}", "repair_running")
    job = owning_watch(session_id, home)
    if job:
        raise Refused(
            f"Not resumed: {owned_message(job)}", "watch_owns_session", watch=watch_summary(job)
        )


def watch_jobs(home):
    """The watcher's queue, read-only, through the dashboard's shared cache; never mutate it."""
    if not home:
        return []
    from dashboard import read_jobs  # The dashboard imports this module first.

    return read_jobs(Path(home))


def open_elsewhere(session_id, machine=None):
    """The running agent herdr sees on this session, in any workspace of its machine.

    herdr keeps a pane's session after its agent exits back to the shell, so only a
    pane that still hosts a recognized agent counts.
    """
    panes = {
        pane.get("pane_id")
        for pane in herdr("pane", "list", machine=machine)["panes"]
        if isinstance(pane, dict)
        and isinstance(pane.get("agent_session") or {}, dict)
        and (pane.get("agent_session") or {}).get("value") == session_id
    }
    return next(
        (
            {
                "pane": agent["pane_id"],
                "agent": agent.get("agent"),
                "status": agent.get("agent_status"),
                "session": session_id,
            }
            for agent in listed_agents(machine)
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


def resume(workspace_id, session_id, text, home, root=None, stop_watch=None, machine=None):
    """Resume a recorded session in the workspace with the message, if any, as its prompt.

    Given a checkout root instead, the session resumes in the workspace open there, or
    in one opened on it again. Given the ID of the babysit watch that owns the session,
    the reader confirmed cancelling it first. A ``machine`` names the worker it all is
    on: its herdr, its session listing and its own paths, which its probe reports.
    """
    if not isinstance(session_id, str) or not workspace_viewer.SESSION.match(session_id):
        raise ValueError("Choose a recorded session to resume")
    if root is None:
        root = workspace_root(workspace_id, machine)
    chosen = next((s for s in recorded(root, home, machine) if s["id"] == session_id), None)
    if chosen is None or chosen["agent"] not in RESUME:
        raise ValueError("That session is not recorded for this workspace")
    if machine is not None and chosen["agent"] == "claude" and not chosen.get("claude_account"):
        raise ValueError(f"That session's Claude home is not an account on {machine.label}")
    # A worker's own paths, its probe's: the checkout's real one names its lock there.
    found = None
    if machine is not None:
        found = remote_resume(home, machine, root, chosen)
        root = found["path"]
    real = os.path.realpath(root) if machine is None else root
    # One resume of a session, and in a checkout, at a time, held until its agent shows up:
    # a second request then sees it.
    with locked(("checkout", lock_name(machine, real)), ("session", session_id)):
        if workspace_id is None:
            workspace_id = workspace_viewer.checkout_workspace(root, machine)
        if workspace_id and agents(workspace_id, machine=machine):
            # Two agents on one conversation would interleave; talk to the running one.
            raise ValueError("An agent is already running in this workspace; send to it instead")
        recent = _recent.get(session_id)
        if recent and time.monotonic() - recent[1] < RECENT_RESUME:
            raise ValueError(
                f"This session was just resumed in {recent[0]}; check it in Collie before resending"
            )
        elsewhere = open_elsewhere(session_id, machine)
        if elsewhere:
            # Already running in another workspace: talk to that agent, never a second one.
            if not text:
                raise ValueError(f"This session is already running in {elsewhere['pane']}")
            return prompt(elsewhere, text, machine)
        # A session resumes from the directory it was recorded in (Claude looks its ID up
        # in that directory's project folder).
        if found is None:
            details = workspace_viewer.details(chosen["_file"], chosen["agent"]) or {}
            cwd = details.get("cwd") or root
            exists = Path(cwd).is_dir()
        else:
            cwd, exists = found["cwd"], found["cwd_exists"]
        if not exists:
            raise ValueError("The session's directory no longer exists")
        # Last before herdr is asked for a pane, so a confirmed cancellation is spent only
        # on a resume that can go ahead.
        claim_from_watch(
            session_id, chosen["agent"], home, chosen.get("claude_config_dir"), stop_watch, machine
        )
        # Always a fresh pane, as launches use: an existing shell may hold a half-typed
        # line or a program that replaced it, and typing would join or feed it.
        live = workspace_id and workspace_viewer.live_workspace(workspace_id, machine)
        pane = None
        if not live:
            # The workspace was closed since the viewer opened, or none was open: open
            # one on its checkout again, whose first pane is a new shell.
            live, pane = reopen(root, machine, found)
            workspace_id = workspace_id or live
            if agents(workspace_id, machine=machine):
                raise ValueError(
                    "An agent is already running in this workspace; send to it instead"
                )
        if pane is None:
            anchor = next(
                (
                    p.get("pane_id")
                    for p in herdr("pane", "list", "--workspace", live, machine=machine)["panes"]
                    if isinstance(p, dict)
                ),
                None,
            )
            if anchor is None:
                raise ValueError("That herdr workspace has no pane to split")
            pane_id(anchor, machine)
            split = herdr(
                "pane",
                "split",
                anchor,
                "--direction",
                "right",
                "--cwd",
                cwd,
                "--no-focus",
                machine=machine,
            )
            return launch(
                workspace_id,
                chosen,
                pane_id(split["pane"]["pane_id"], machine),
                text,
                home,
                worktree=root,
                machine=machine,
                found=found,
            )
        # A reopened workspace's shell starts in the checkout, not the session's directory.
        return launch(
            workspace_id,
            chosen,
            pane,
            text,
            home,
            cwd=None if cwd == root else cwd,
            worktree=root,
            machine=machine,
            found=found,
        )


def pane_id(value, machine):
    """A pane a worker's herdr named, before it reaches a command line here."""
    if machine is not None and not (isinstance(value, str) and PANE.fullmatch(value)):
        raise ValueError(f"herdr on {machine.label} named no usable pane; open it in Collie")
    return value


def remote_resume(home, machine, root, chosen):
    """The worker's own paths for resuming ``chosen``: its checkout, common dir, the
    session's directory (and whether it still exists) and the agent's configuration."""
    return workspace_viewer.probe(
        home,
        machine,
        "resume",
        root=root,
        cwd=chosen["_cwd"],
        agent=chosen["agent"],
        account=chosen.get("claude_account"),
        babysit_root=machine.babysit_root,
        sandbox=cron_agents.BACKENDS[machine.os],
    )


def reopen(root, machine=None, found=None):
    """Open a herdr workspace on a checkout again; its ID, and its new shell pane if any.

    A linked worktree opens under its clone, as `wt` opens it; a clone's own checkout gets
    a workspace of its own. An open that finds one already there gives no pane to type in.
    A worker's common dir is the one its probe ``found``.
    """
    if machine is None:
        common = Path(
            workspace_viewer.git_text(
                root, "rev-parse", "--path-format=absolute", "--git-common-dir"
            )
        )
    else:
        common = Path(found["common"])
    if common == Path(root) / ".git":
        opened = herdr("workspace", "create", "--cwd", root, "--no-focus", machine=machine)
    else:
        opened = herdr(
            "worktree",
            "open",
            "--cwd",
            str(common.parent),
            "--path",
            root,
            "--no-focus",
            machine=machine,
        )
    pane = (opened.get("root_pane") or {}).get("pane_id")
    live = (opened.get("workspace") or {}).get("workspace_id") or (
        pane.split(":")[0] if isinstance(pane, str) and ":" in pane else None
    )
    if not live or (
        machine is not None and not (isinstance(live, str) and machines.WORKSPACE.fullmatch(live))
    ):
        raise ValueError("herdr did not report the reopened workspace; open it in Collie")
    # One that was already open may hold a shell in use: that one gets a split.
    if opened.get("already_open"):
        return live, None
    return live, pane if pane is None else pane_id(pane, machine)


def remote_command(machine, chosen, found, text, staged):
    """The resume line typed on a worker: its own sandbox, with the paths its probe found.

    The Claude configuration is the worker's account home, typed as the worker spells
    it; nothing about this machine's accounts applies there.
    """
    account = chosen.get("claude_account")
    job = {
        "agent": chosen["agent"],
        "prompt": text,
        "model": "",
        "docker": False,
        "claude_account": "" if account in (None, "default") else account,
    }
    return cron_agents.remote_command(
        job, machine, found, staged, "", extra=[*RESUME[chosen["agent"]], chosen["id"]]
    )


def launch(
    workspace_id,
    chosen,
    pane,
    text,
    home,
    *,
    options=(),
    cwd=None,
    launcher=None,
    worktree=None,
    machine=None,
    found=None,
):
    """Resume once into a fresh or verified empty shell pane, optionally with a prompt.

    ``worktree`` is the workspace checkout, which the agent may write. On a worker the
    prompt is staged there and the line names the paths its probe ``found``.
    """
    session_id = chosen["id"]
    # The prompt is read back from a private file, as launches do: a typed line has
    # a length ceiling, and command-substitution output needs no escaping.
    staged: Path | None = None  # This machine's; a worker's is pruned there after a day.
    if machine is not None:
        remote = None
        if text:
            # Older ones there, left by resumes whose agent never showed up, go first.
            remote = machines.put_file(
                home,
                machine,
                machines.remote_path(REMOTE_PROMPTS),
                f"resume-{uuid.uuid4()}",
                (text + "\n").encode(),
                keep=PROMPT_KEEP,
            )
        command = remote_command(machine, chosen, found, text, remote)
    else:
        prompts = Path(home or os.environ.get("TMPDIR") or "/tmp") / "message-prompts"
        prompts.mkdir(mode=0o700, parents=True, exist_ok=True)
        prune(prompts)
        staged = local = prompts / f"resume-{uuid.uuid4()}"
        if text:
            descriptor = os.open(local, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(text + "\n")
        command = wt.agent_command(
            chosen["agent"],
            text,
            extra=[*options, *RESUME[chosen["agent"]], chosen["id"]],
            stage=lambda _: str(local),
            worktree=worktree,
            sandbox_config_dir=chosen.get("claude_config_dir"),
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

    def discard():
        if staged is not None:
            staged.unlink(missing_ok=True)

    if cwd:
        command = f"cd {shlex.quote(cwd)} && {command}"
    # A transcript's recorded cwd is the agent's to write; quoting does not stop a
    # control character in it from acting as a keystroke.
    sandbox.typed(command)
    _recent[session_id] = (pane, time.monotonic())
    try:
        # `pane run` answers with plain text, not herdr's JSON envelope.
        run(*machines.herdr_argv(machine, "pane", "run", pane, command))
    except subprocess.TimeoutExpired:
        return {
            "sent": True,
            "pane": pane,
            "resumed": chosen["id"],
            "warning": f"herdr did not answer while typing the command; {UNCERTAIN}.",
        }
    except (ValueError, subprocess.SubprocessError):
        _recent.pop(session_id, None)
        discard()
        raise
    # From here the command is typed: any trouble makes the outcome uncertain, never
    # "not sent", which would invite a second agent on the same session.
    deadline = time.monotonic() + RESUME_WAIT
    try:
        while time.monotonic() < deadline:
            current = next(
                (a for a in agents(workspace_id, machine=machine) if a["pane"] == pane), None
            )
            if (
                current
                and current.get("session") == session_id
                and current.get("agent") == chosen["agent"]
            ):
                discard()  # The shell has read it by now.
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
