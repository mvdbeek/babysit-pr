"""The terminal of a herdr workspace's panes, from the workspace viewer.

The viewer mirrors one pane's screen (``herdr pane read``), sends it named keys or
literal text, and runs shell commands in a new split of the workspace. Every action
names a workspace the viewer can show, resolved the way messages resolve it (a closed
workspace follows its checkout to the one reopened on it), and a pane herdr lists in
that workspace right now. Keys and text take the pane's lock, so they never land in the
middle of a message or an answer typed there.

Unlike messages, keys and text go to the pane whatever runs in it: an agent, a dialog,
or the shell an agent left behind. That is the point of the terminal view, and the same
access Collie gives.
"""

import re
import subprocess

import agent_messages
import machines
import sandbox
import workspace_viewer
from pr_workspaces import PANE, herdr, run

# Read once per poll: the visible screen, with its colours as escape sequences.
SCREEN_TIMEOUT = 8
MAX_SCREEN = 200_000
# One modifier chain and a key, in herdr's spelling ("esc", "ctrl+c", "shift+tab", "7").
KEY = re.compile(
    r"(?:(?:ctrl|alt|shift)\+){0,3}"
    r"(?:esc|enter|tab|backspace|delete|space|up|down|left|right|home|end|pageup|pagedown"
    r"|f(?:[1-9]|1[0-2])|[a-z0-9])"
)
MAX_KEYS = 32
MAX_TEXT = 4000
MAX_COMMAND = 2000
SHELL_LABEL = "babysit shell"


def live(workspace_id, machine=None):
    """The open workspace on the checkout ``workspace_id`` names, or refuse."""
    found = workspace_viewer.live_workspace(workspace_id, machine)
    if found is None:
        raise ValueError("No herdr workspace is open on this checkout; reopen it in Collie")
    return found


def panes(workspace_id, machine=None):
    """The workspace's panes, each with the agent herdr detects in it, if any."""
    listed = herdr("pane", "list", "--workspace", workspace_id, machine=machine)["panes"]
    agents = {
        a["pane_id"]: a
        for a in herdr("agent", "list", machine=machine)["agents"]
        if isinstance(a, dict) and isinstance(a.get("pane_id"), str)
    }
    found = []
    for pane in listed if isinstance(listed, list) else []:
        if not (
            isinstance(pane, dict)
            and isinstance(pane.get("pane_id"), str)
            and PANE.fullmatch(pane["pane_id"])
            and pane.get("workspace_id") == workspace_id
        ):
            continue
        agent = agents.get(pane["pane_id"]) or {}
        found.append(
            {
                "pane": pane["pane_id"],
                "label": pane["label"][:80] if isinstance(pane.get("label"), str) else None,
                "agent": agent.get("agent") if isinstance(agent.get("agent"), str) else None,
                "status": agent.get("agent_status")
                if isinstance(agent.get("agent_status"), str)
                else None,
                "session": (agent.get("agent_session") or {}).get("value")
                if isinstance(agent.get("agent_session"), dict)
                else None,
            }
        )
    return found


def target(workspace_id, pane, machine=None):
    """The live workspace and its panes, once ``pane`` is one of them."""
    if not isinstance(pane, str) or not PANE.fullmatch(pane):
        raise ValueError("Invalid pane")
    workspace = live(workspace_id, machine)
    listed = panes(workspace, machine)
    if not any(p["pane"] == pane for p in listed):
        raise ValueError("That pane is no longer in this workspace; refresh")
    return workspace, listed


def screen(workspace_id, pane=None, machine=None):
    """One pane's visible screen (the first agent's, or the first pane's, by default)."""
    workspace = live(workspace_id, machine)
    listed = panes(workspace, machine)
    if not listed:
        raise ValueError("This workspace has no panes")
    if pane is None:
        pane = next((p["pane"] for p in listed if p["agent"]), listed[0]["pane"])
    elif not any(p["pane"] == pane for p in listed):
        raise ValueError("That pane is no longer in this workspace; refresh")
    # Read as it is: ``run`` would strip the indentation of the screen's first line.
    # herdr reaches a worker itself, so it always runs here.
    result = machines.run(
        None,
        None,
        machines.herdr_argv(
            machine, "pane", "read", pane, "--source", "visible", "--format", "ansi"
        ),
        text=True,
        timeout=SCREEN_TIMEOUT,
    )
    if result.returncode:
        raise ValueError((result.stderr or result.stdout or "herdr failed")[-300:].strip())
    text = result.stdout.rstrip("\n")
    return {
        "workspace": machines.qualify(machine.id, workspace) if machine else workspace,
        "pane": pane,
        "panes": listed,
        "screen": text[-MAX_SCREEN:],
        "truncated": len(text) > MAX_SCREEN,
    }


def send(request, machine=None):
    """Named keys, or literal text (and optionally Enter), typed into one pane."""
    if set(request) not in ({"workspace", "pane", "keys"}, {"workspace", "pane", "text", "enter"}):
        raise ValueError("Send keys, or text and whether to press Enter")
    if not isinstance(request["workspace"], str):
        raise ValueError("Invalid workspace")
    keys = request.get("keys")
    text = request.get("text")
    if "keys" in request:
        if (
            not isinstance(keys, list)
            or not 0 < len(keys) <= MAX_KEYS
            or not all(isinstance(k, str) and KEY.fullmatch(k) for k in keys)
        ):
            raise ValueError("Unknown key")
    elif (
        not isinstance(text, str)
        or len(text) > MAX_TEXT
        or not isinstance(request["enter"], bool)
        or not (text or request["enter"])
    ):
        raise ValueError("Invalid text")
    elif sandbox.CONTROL.search(text):
        # A control character typed is a keystroke; those go as named keys.
        raise ValueError("Text cannot contain control characters; send them as keys")
    pane = request["pane"]
    target(request["workspace"], pane, machine)
    with agent_messages.locked(("pane", agent_messages.lock_name(machine, pane))):
        try:
            if keys:
                herdr("pane", "send-keys", pane, *keys, machine=machine)
            else:
                if text:
                    herdr("pane", "send-text", pane, text, machine=machine)
                if request["enter"]:
                    herdr("pane", "send-keys", pane, "enter", machine=machine)
        except ValueError as exc:
            raise ValueError(f"herdr could not type it: {str(exc)[-300:]}") from exc
    return {"pane": pane, "sent": True}


def shell(request, machine=None):
    """Run a command in a new split of the workspace, in its checkout, never in a pane
    an agent or the user is typing in. The split stays open, labelled, with its shell."""
    if set(request) != {"workspace", "command"} or not isinstance(request["workspace"], str):
        raise ValueError("Supply a workspace and a command")
    command = request["command"]
    if not isinstance(command, str) or not command.strip() or len(command) > MAX_COMMAND:
        raise ValueError("Invalid command")
    sandbox.typed(command)  # Refuses control characters, which would act as keystrokes.
    workspace = live(request["workspace"], machine)
    listed = panes(workspace, machine)
    if not listed:
        raise ValueError("This workspace has no pane to split")
    root = agent_messages.workspace_root(workspace, machine)
    pane = herdr(
        "pane",
        "split",
        listed[0]["pane"],
        "--direction",
        "down",
        "--cwd",
        str(root),
        "--no-focus",
        machine=machine,
    )["pane"]["pane_id"]
    try:
        herdr("pane", "rename", pane, SHELL_LABEL, machine=machine)
    except (OSError, ValueError, subprocess.SubprocessError):
        pass  # Unlabelled, the split still works.
    run(*machines.herdr_argv(machine, "pane", "run", pane, command))
    return {"pane": pane}
