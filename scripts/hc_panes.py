#!/usr/bin/env python3
"""herdr's workspace, pane and agent commands, answered by herdr-core.

The rest of this project speaks herdr's vocabulary: ``workspace list``, ``agent list``,
``pane run`` and so on, each a ``herdr`` command line (``machines.herdr_argv``). Where a
machine keeps its terminals in herdr-core instead (``machines.terminals``), that
command line runs this module, which answers from ``hc`` in the same shapes. It is the
one place that knows both, and it goes when herdr support does.

What each herdr idea is here:

* A **pane** is an hc terminal running the machine's shell, and its id is the
  terminal's address (``t7``, the ``$HC_TERMINAL_ID`` of anything started in it). An
  agent is typed into that shell, so the terminal outlives it and holds its next run.
  The address is never handed out again, so it is also the pane's ``terminal_id``.
* A **workspace** is the set of terminals working in one checkout: those labelled with
  it (``babysit.checkout``), or started in it. Its id is derived from the path, so a
  checkout keeps one id for good.
* An **agent** is a terminal whose program reports a status (OSC 7501) as Claude Code
  or Codex do, or whose foreground program is one of them. The status is the agent's
  own word; no screen is read for it.
* A **session** is the ``babysit.session`` label, set by the sandbox's broker on a
  worker. On this machine it is read from the agent itself: Claude's per-process
  session record, or the rollout file a Codex has open.

Not carried over, because herdr-core has no such thing: window focus, scroll position
(a pane is never "scrolled"), styled screen reads (``read`` is plain text) and typing
text that is not a key without submitting it.
"""

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import hc

PREFIX = "hc-"
# Labels this project sets on a terminal. Herdr's own clients use ``herdr.`` ones.
CHECKOUT = "babysit.checkout"  # The checkout it works in: what makes a workspace.
CLONE = "babysit.clone"  # The clone that checkout belongs to.
TITLE = "babysit.title"  # The workspace's display name.
PANE = "babysit.pane"  # What the terminal was opened for ("babysit agent", "babysit shell").
SESSION = "babysit.session"  # The agent conversation running in it.
SESSION_OF = "babysit.session-of"  # The agent process that session was read from.
# Herdr's clients share which turn a person last looked at; a later one is unseen.
SEEN = "herdr.seen"

SHELLS = {"sh", "bash", "zsh", "fish", "dash", "ksh", "nu", "tcsh", "csh", "pwsh"}
# The 7501 ``app`` each agent reports, and the name records use for it.
AGENTS = {"claude-code": "claude", "claude": "claude", "codex": "codex"}
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
ROLLOUT = re.compile(rf"rollout-.*-({UUID.pattern})\.jsonl$")
# What ``hc keys`` can press that is also a character, by the character it types.
TYPED = {
    **{c: c for c in "abcdefghijklmnopqrstuvwxyz0123456789`[],=-.';/"},
    **{c.upper(): f"shift+{c}" for c in "abcdefghijklmnopqrstuvwxyz"},
    " ": "space",
    "\t": "tab",
    "\\": "backslash",
}
# What a worker runs to list the processes under a terminal's foreground program: there
# the agent is under its sandbox launcher, not the foreground program itself.
TREE = r"""
import json, os, sys
root = int(sys.argv[1]); parents = {}
for name in os.listdir("/proc"):
    if name.isdigit():
        try:
            with open(f"/proc/{name}/stat") as stream:
                parents[int(name)] = int(stream.read().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            pass
found, level = [], [root]
for _ in range(6):
    found += level
    level = [pid for pid, parent in parents.items() if parent in level]
out = []
for pid in found[:64]:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as stream:
            argv = [a.decode(errors="replace") for a in stream.read().split(b"\0")[:-1]]
        with open(f"/proc/{pid}/comm") as stream:
            name = stream.read().strip()
        out.append({"pid": pid, "name": name, "argv": argv, "cwd": os.readlink(f"/proc/{pid}/cwd")})
    except OSError:
        pass
print(json.dumps(out))
"""
MAX_PROCESSES = 64


class Refused(Exception):
    """A command herdr-core cannot carry out; ``code`` is herdr's word where it has one."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def workspace_id(path: str) -> str:
    return PREFIX + hashlib.sha256(path.encode()).hexdigest()[:12]


def is_workspace(value: str) -> bool:
    """Whether a workspace id is one of herdr-core's, which Collie cannot open."""
    return value.startswith(PREFIX)


def labels(record: dict) -> dict:
    value = record.get("labels")
    return value if isinstance(value, dict) else {}


def checkout(record: dict) -> str:
    return str(labels(record).get(CHECKOUT) or record.get("cwd") or "")


def program(argv) -> str:
    first = argv[0] if isinstance(argv, list) and argv and isinstance(argv[0], str) else ""
    return Path(first).name.lstrip("-")


def foreground(record: dict) -> dict:
    value = record.get("foreground")
    return value if isinstance(value, dict) else {}


def at_shell(record: dict) -> bool:
    """Whether the terminal's own program is a shell, which outlives what is typed into it."""
    return program(record.get("argv")) in SHELLS


def at_prompt(record: dict) -> bool:
    """Whether a shell terminal shows its prompt: nothing typed into it is running."""
    return (
        at_shell(record)
        and record.get("pid") is not None
        and foreground(record).get("pid") == record.get("pid")
    )


def status(record: dict) -> dict:
    value = record.get("status")
    return value if isinstance(value, dict) else {}


def agent_kind(record: dict, running: dict | None = None) -> str | None:
    """codex or claude for a terminal running one; ``running`` is its process if known."""
    if running is None and at_prompt(record):
        return None  # A status left behind by an agent that returned to the shell.
    app = status(record).get("app")
    if isinstance(app, str) and app:
        return AGENTS.get(app, app)
    # Not reporting yet: an agent still starting is told by its program's name.
    name = program((running or foreground(record)).get("argv"))
    return name if name in set(AGENTS.values()) else None


def agent_status(record: dict) -> str:
    """herdr's word for the agent's state, from the one the agent reports.

    ``error`` needs a person as ``blocked`` does. A finished turn is ``done`` until a
    person has looked at it, where a Herdr client records that (``herdr.seen``);
    without one it is whichever of idle and done the agent said.
    """
    state = status(record).get("state")
    if state in {"blocked", "error"}:
        return "blocked"
    if state == "working":
        return "working"
    if state in {"idle", "done"}:
        seen = labels(record).get(SEEN)
        if isinstance(seen, str) and seen.isdigit() and isinstance(record.get("turn"), int):
            return "done" if record["turn"] > int(seen) else "idle"
        return state
    return "unknown"


def processes() -> list[dict]:
    """This machine's processes: ``pid``, ``ppid``, ``pgid``, ``tpgid``, ``start``, ``argv``.

    ``tpgid`` is the process group in the foreground of the process's terminal. ``start``
    is when the process began, as Claude records it for its own (the kernel's tick count
    on Linux, ps's date on macOS). On macOS a command line comes from ps, split at its
    spaces, so an argument holding one is not exact; the same process reads the same way
    every time, which is what the comparisons made with it need.
    """
    found = []
    if sys.platform == "linux":
        for name in os.listdir("/proc"):
            if not name.isdigit():
                continue
            try:
                fields = Path(f"/proc/{name}/stat").read_text().rsplit(")", 1)[1].split()
                raw = Path(f"/proc/{name}/cmdline").read_bytes()
            except (OSError, IndexError):
                continue
            argv = [word.decode(errors="replace") for word in raw.split(b"\0")[:-1]]
            found.append(
                {
                    "pid": int(name),
                    "ppid": int(fields[1]),
                    "pgid": int(fields[2]),
                    "tpgid": int(fields[5]),
                    "start": fields[19],
                    "argv": argv,
                }
            )
        return found
    listed = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,pgid=,tpgid=,lstart=,args="],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    for line in listed.stdout.splitlines():
        words = line.split()
        # lstart is five words: Sun Nov 15 09:35:58 2026.
        if len(words) >= 10 and all(word.lstrip("-").isdigit() for word in words[:4]):
            pid, ppid, pgid, tpgid = (int(word) for word in words[:4])
            found.append(
                {
                    "pid": pid,
                    "ppid": ppid,
                    "pgid": pgid,
                    "tpgid": tpgid,
                    "start": " ".join(words[4:9]),
                    "argv": words[9:],
                }
            )
    return found


def process_cwd(pid: int) -> str:
    """The folder process ``pid`` is in, or an empty string when it cannot be read."""
    try:
        if sys.platform == "linux":
            return os.readlink(f"/proc/{pid}/cwd")
        listed = subprocess.run(
            ["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return next((line[1:] for line in listed.stdout.splitlines() if line.startswith("n")), "")
    except (OSError, subprocess.SubprocessError):
        return ""


def process_entry(process: dict) -> dict:
    """One process as herdr lists a pane's: ``argv0`` is its program's name."""
    return {**process, "argv0": program(process.get("argv"))}


def local_tree(record: dict, listed: list[dict] | None = None) -> list[dict]:
    """The terminal's foreground program on this machine and what runs with it, the
    program itself first.

    That is the process group in the foreground of the terminal, as herdr lists a
    pane's: an agent is in it beside a launcher that stays above it, as Safehouse's
    script does for Codex on macOS. Where the group holds no agent, one it started in a
    session of its own (under bwrap) is added with the launchers between them. A shell
    at its prompt stands alone: what it runs in the background is not in the pane's
    foreground. Neither is whatever is asking, when the question comes from a command
    typed in that terminal.

    Which group is in front is asked of the system, through the terminal's own program:
    hc samples it only about twice a second, too late for a command that checks itself
    as it starts. hc's word is used where the process table cannot be read.
    """
    front = foreground(record)
    if listed is None:
        try:
            listed = processes()
        except (OSError, subprocess.SubprocessError):
            listed = []
    by_pid = {p["pid"]: p for p in listed}
    root = by_pid.get(record.get("pid"))
    lead = root["tpgid"] if root and root["tpgid"] > 0 else front.get("pid")
    if not isinstance(lead, int):
        return []
    if lead == front.get("pid"):
        # hc's own reading of it carries each argument and its folder exactly.
        first = process_entry({k: front.get(k) for k in ("pid", "name", "argv", "cwd")})
    elif lead in by_pid:
        argv = by_pid[lead]["argv"]
        first = process_entry(
            {"pid": lead, "name": program(argv), "argv": argv, "cwd": process_cwd(lead)}
        )
    else:
        return []
    if lead in by_pid:
        first["start"] = by_pid[lead]["start"]
    if lead == record.get("pid") and at_shell(record):
        return [first]
    asking = {os.getpid()} | {p["pid"] for p in listed if p["ppid"] == os.getpid()}
    agents = set(AGENTS.values())
    members = {p["pid"]: p for p in listed if p["pgid"] == lead and p["pid"] not in asking | {lead}}
    if first["argv0"] not in agents and not any(
        program(p["argv"]) in agents for p in members.values()
    ):
        for process in listed:
            if program(process["argv"]) not in agents or process["pid"] in asking:
                continue
            chain: list[dict] = []
            above: dict | None = process
            while above is not None and above["pid"] != lead and len(chain) < 6:
                chain.append(above)
                above = by_pid.get(above["ppid"])
            if above is not None and above["pid"] == lead:
                members.update({p["pid"]: p for p in chain})
    rest = [
        process_entry(
            {"pid": pid, "name": program(p["argv"]), "argv": p["argv"], "start": p["start"]}
        )
        for pid, p in sorted(members.items())
    ]
    return [first, *rest[: MAX_PROCESSES - 1]]


def claude_session(pid: int, start: str | None = None) -> str | None:
    """The session Claude process ``pid`` records for itself, in whichever account.

    ``start`` is when that process began: a record left by an earlier process with the
    same id names another conversation and is passed over.
    """
    home = Path.home() / ".claude"
    for store in [home / "sessions", *sorted(home.glob("accounts/*/sessions"))]:
        try:
            data = json.loads((store / f"{pid}.json").read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or data.get("pid") != pid:
            continue
        began = data.get("procStart")
        # ps pads a one-digit day with a second space; the record may not.
        if start and isinstance(began, str) and began.split() != start.split():
            continue
        value = data.get("sessionId")
        if isinstance(value, str) and UUID.fullmatch(value):
            return value
    return None


def codex_session(pid: int, argv) -> str | None:
    """The session of Codex process ``pid``: the rollout it writes, else the one resumed."""
    opened: list[str] = []
    try:
        if sys.platform == "linux":
            folder = Path(f"/proc/{pid}/fd")
            opened = [os.readlink(folder / name) for name in os.listdir(folder)]
        else:
            listed = subprocess.run(
                ["lsof", "-a", "-p", str(pid), "-Fn"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            opened = [line[1:] for line in listed.stdout.splitlines() if line.startswith("n")]
    except (OSError, subprocess.SubprocessError):
        pass
    for path in opened:
        if found := ROLLOUT.search(path):
            return found.group(1)
    words = argv if isinstance(argv, list) else []
    for at, word in enumerate(words[:-1]):
        if word == "resume" and isinstance(words[at + 1], str) and UUID.fullmatch(words[at + 1]):
            return words[at + 1]
    return None


def session(record: dict, running: dict | None) -> str | None:
    """The conversation the terminal's agent runs, or None when nothing says.

    ``running`` is the agent's process when it is on this machine. Claude's is read from
    its own record. Codex's is the rollout it has open, kept on the terminal with the
    process and turn it was read at (``babysit.session-of``) so the open files are read
    once per turn: a label left by another process, or by an earlier turn, is not this
    agent's. A label without that mark is a sandbox broker's, which sets it while its
    agent runs and takes it off after: the only word for a worker's agent, and for one
    whose sandbox hides its own record.
    """
    kept = labels(record)
    labelled = kept.get(SESSION) if isinstance(kept.get(SESSION), str) else None
    told = labelled if not kept.get(SESSION_OF) else None
    if running is None:
        return told
    pid = running["pid"]
    if program(running.get("argv")) == "claude":
        return claude_session(pid, running.get("start")) or told
    mark = f"{pid}:{record.get('turn')}"
    if labelled and kept.get(SESSION_OF) == mark:
        return labelled
    found = codex_session(pid, running.get("argv"))
    if found:
        try:
            hc.label(record["address"], {SESSION: found, SESSION_OF: mark})
        except hc.Error:
            pass
    return found or told


def pane(record: dict, machine: str | None, listed: list[dict] | None = None) -> dict:
    """herdr's pane record for a terminal; ``listed`` is this machine's processes, read
    once for a whole listing."""
    kept = labels(record)
    out = {
        "pane_id": record["address"],
        "terminal_id": record["address"],
        "workspace_id": workspace_id(checkout(record)),
        "cwd": record.get("cwd"),
        "foreground_cwd": foreground(record).get("cwd") or record.get("cwd"),
        "label": kept.get(PANE) or record.get("name"),
        "terminal_title_stripped": record.get("title") or None,
        "focused": False,
    }
    running, alone = None, False
    if machine is None:
        tree = local_tree(record, listed)
        running = next((p for p in tree if p["argv0"] in set(AGENTS.values())), None)
        # The system's own word that the shell is in front outranks hc's last sample.
        alone = len(tree) == 1 and tree[0]["pid"] == record.get("pid") and at_shell(record)
    kind = None if alone else agent_kind(record, running)
    if kind:
        out.update(agent=kind, agent_status=agent_status(record), name=record.get("name"))
        found = session(record, running)
        if found:
            out["agent_session"] = {"agent": kind, "kind": "id", "value": found}
    return out


def local_processes(machine: str | None) -> list[dict] | None:
    """This machine's processes for a listing of its own terminals; None for a worker's."""
    if machine is not None:
        return None
    try:
        return processes()
    except (OSError, subprocess.SubprocessError):
        return []


def agent(record: dict, machine: str | None, listed: list[dict] | None = None) -> dict | None:
    entry = pane(record, machine, listed)
    if "agent" not in entry:
        return None
    entry["cwd"] = entry.pop("foreground_cwd")
    return entry


def workspaces(records: list[dict]) -> list[dict]:
    grouped: dict[str, list[dict]] = {}
    for record in records:
        if checkout(record):
            grouped.setdefault(checkout(record), []).append(record)
    urgency = ["blocked", "working", "done", "idle", "unknown"]
    out = []
    for path, members in grouped.items():
        kept = [labels(member) for member in members]
        clone: str | None = next((str(k[CLONE]) for k in kept if k.get(CLONE)), None)
        states = [agent_status(member) for member in members if agent_kind(member)]
        out.append(
            {
                "workspace_id": workspace_id(path),
                "label": next((k[TITLE] for k in kept if k.get(TITLE)), None) or Path(path).name,
                "agent_status": min(states, key=urgency.index) if states else "unknown",
                "pane_count": len(members),
                "focused": False,
                "worktree": {
                    "checkout_path": path,
                    "repo_root": clone or path,
                    "repo_name": Path(clone or path).name,
                },
            }
        )
    return out


def members(records: list[dict], workspace: str) -> list[dict]:
    return [r for r in records if workspace_id(checkout(r)) == workspace]


def process_tree(record: dict, machine: str | None) -> list[dict]:
    """The terminal's foreground program and what runs with it, the program first, each
    with its command line and, for an agent, the folder it is in.

    A worker's are read there from /proc, each command line exactly as it is.
    """
    front = foreground(record)
    if machine is None:
        tree = local_tree(record)
        for process in tree[1:]:
            # Only an agent's folder is ever compared; reading every process's costs.
            agentic = process["argv0"] in set(AGENTS.values())
            process["cwd"] = process_cwd(process["pid"]) if agentic else ""
        return tree
    if not isinstance(front.get("pid"), int):
        return []
    lead = process_entry({k: front.get(k) for k in ("pid", "name", "argv", "cwd")})
    if at_prompt(record):
        return [lead]
    try:
        tree = json.loads(hc.execute(["python3", "-I", "-c", TREE, str(front["pid"])], machine))
    except (ValueError, hc.Error):
        return [lead]
    if not isinstance(tree, list) or not all(isinstance(process, dict) for process in tree):
        return [lead]
    return [process_entry(process) for process in tree] or [lead]


def process_info(record: dict, machine: str | None) -> dict:
    tree = process_tree(record, machine)
    return {
        # Only a shell outlives what is typed into it: a terminal started with a program
        # of its own ends with that program, and nothing may be bound to it.
        "shell_pid": record.get("pid") if at_shell(record) else None,
        "foreground_process_group_id": tree[0]["pid"] if tree else None,
        "foreground_processes": tree,
    }


def keys_for(text: str) -> list[str]:
    """The keys that type ``text`` without submitting it."""
    missing = sorted({char for char in text if char not in TYPED})
    if missing:
        shown = " ".join(repr(char) for char in missing[:5])
        raise Refused(
            "untypable",
            f"herdr-core cannot type {shown} without submitting it; send the whole line instead",
        )
    return [TYPED[char] for char in text]


def take(args: list[str], name: str) -> str | None:
    """Remove ``name VALUE`` from ``args``; the value, or None when absent."""
    if name not in args:
        return None
    at = args.index(name)
    if at + 1 >= len(args):
        raise Refused("usage", f"{name} needs a value")
    value = args[at + 1]
    del args[at : at + 2]
    return value


def open_workspace(
    machine: str | None, path: str, clone: str | None, title: str | None, cwd: str | None = None
) -> dict:
    """A terminal for checkout ``path``; ``already_open`` when it has one, which is kept."""
    records = hc.terminals(machine)
    wid = workspace_id(path)
    existing = members(records, wid)
    if existing and cwd is None:
        return {
            "already_open": True,
            "root_pane": {"pane_id": existing[0]["address"]},
            "workspace": {"workspace_id": wid},
        }
    kept: dict[str, str] = {CHECKOUT: path}
    if clone:
        kept[CLONE] = clone
    if title:
        kept[TITLE] = title
    if existing:  # A further terminal takes the workspace's name and clone from the first.
        first = labels(existing[0])
        kept.update({k: str(first[k]) for k in (CLONE, TITLE) if first.get(k)})
    taken = {str(r.get("name")) for r in records if r.get("name")}
    name = hc.terminal_name(kept.get(TITLE) or Path(path).name, taken)
    started = hc.start(cwd or path, machine=machine, name=name, labels=kept)
    return {
        "already_open": False,
        "root_pane": {"pane_id": started["address"]},
        "pane": {"pane_id": started["address"]},
        "workspace": {"workspace_id": wid},
    }


def prompt(target: str, text: str, machine: str | None) -> dict:
    """Submit a message to the agent in ``target``, as herdr's ``agent prompt`` does.

    hc types only if the agent still reports the state it was seen in when its hold for
    a person's draft ends: one that left for its shell meanwhile reports nothing, so a
    message is never run as a command. An agent whose state hc cannot require (``done``)
    gets a short hold instead, after which the message is taken back.
    """
    record = hc.get(target, machine)
    if record.get("state") != "live" or not agent_kind(record):
        raise Refused("agent_not_running", f"no agent is running in {target}")
    state = status(record).get("state")
    if state == "blocked":
        raise Refused("agent_blocked", f"{target} is waiting on a dialog")
    required = state if state in {"idle", "working"} else None
    try:
        sent = hc.send(
            target,
            text,
            machine,
            only_if=required,
            hold=hc.DRAFT_WAIT if required else hc.BRIEF_HOLD,
        )
    except hc.Error as exc:
        if exc.kind == "blocked":
            raise Refused("agent_blocked", str(exc)) from exc
        if exc.kind == "finished":
            raise Refused("agent_not_running", str(exc)) from exc
        if exc.kind == "status_mismatch":
            raise Refused("agent_changed", f"{exc}; nothing was typed") from exc
        raise
    if sent.get("started") is False:
        raise Refused("agent_prompt_stalled", f"{target} did not start working on the message")
    return {}


def text_of(rest: list[str], what: str) -> str:
    if not rest:
        raise Refused("usage", f"{what} needs its text")
    return rest[0]


def run(args: list[str], machine: str | None):
    """Carry out one herdr command line; its ``result``, or text for a screen read."""
    verb, rest = tuple(args[:2]), list(args[2:])
    if verb == ("workspace", "list"):
        return {"workspaces": workspaces(hc.terminals(machine))}
    if verb == ("agent", "list"):
        listed = local_processes(machine)
        found = [agent(record, machine, listed) for record in hc.terminals(machine)]
        return {"agents": [entry for entry in found if entry]}
    if verb == ("pane", "list"):
        records = hc.terminals(machine)
        workspace = take(rest, "--workspace")
        if workspace is not None:
            records = members(records, workspace)
        listed = local_processes(machine)
        return {"panes": [pane(record, machine, listed) for record in records]}
    if verb in {("worktree", "open"), ("workspace", "create")}:
        cwd, path, title = take(rest, "--cwd"), take(rest, "--path"), take(rest, "--label")
        if not cwd:
            raise Refused("usage", "--cwd is required")
        # A clone's own checkout is its workspace: herdr's ``workspace create`` names only it.
        return open_workspace(machine, path or cwd, cwd, title)
    if verb == ("workspace", "focus"):
        raise Refused(
            "unsupported", "herdr-core has no window to focus; attach with hc attach <terminal>"
        )
    if verb == ("workspace", "close") and rest:
        for record in members(hc.terminals(machine), rest[0]):
            hc.close(record["address"], machine)
        return {}
    if verb == ("pane", "process-info"):
        target = take(rest, "--pane") or ""
        return {"process_info": process_info(hc.get(target, machine), machine)}
    if not rest:
        raise Refused("usage", f"herdr-core has no equivalent of: {' '.join(args) or '(nothing)'}")
    target, rest = rest[0], rest[1:]
    if verb == ("pane", "get"):
        record = hc.get(target, machine)
        if record.get("state") != "live":
            raise Refused("pane_not_found", f"{target} has ended")
        info = pane(record, machine)
        info["scroll"] = {"offset_from_bottom": 0, "viewport_rows": record.get("rows")}
        return {"pane": info}
    if verb == ("agent", "get"):
        record = hc.get(target, machine)
        # Changes whenever the agent's reported state does, which is all it is compared for.
        return {"agent": {"state_change_seq": record.get("status_ms"), "turn": record.get("turn")}}
    if verb in {("agent", "read"), ("pane", "read")}:
        return hc.read(target, machine)
    if verb == ("pane", "run"):
        # As herdr's: typed whatever runs there, so also into a dialog an answer is for.
        # A message to an agent goes through ``agent prompt``, which hc may refuse.
        hc.send(target, text_of(rest, "pane run"), machine, force=True)
        return None  # A submitted line has no reply.
    if verb == ("agent", "prompt"):
        return prompt(target, text_of(rest, "agent prompt"), machine)
    if verb == ("pane", "send-text"):
        hc.keys(target, keys_for(" ".join(rest)), machine)
        return {}
    if verb in {("agent", "send-keys"), ("pane", "send-keys")}:
        hc.keys(target, rest, machine)
        return {}
    if verb == ("agent", "wait"):
        limit = take(rest, "--timeout") or "5000"
        if not limit.isdigit():
            raise Refused("usage", "--timeout takes milliseconds")
        waited = hc.wait(target, int(limit) / 1000, machine)
        if not waited.get("ended"):
            raise Refused("timeout", f"{target} is still working")
        return {}
    if verb == ("pane", "close"):
        hc.close(target, machine)
        return {}
    if verb == ("pane", "rename"):
        hc.label(target, {PANE: " ".join(rest)}, machine)
        return {}
    if verb == ("pane", "split"):
        record = hc.get(target, machine)
        cwd = take(rest, "--cwd") or checkout(record)
        opened = open_workspace(machine, checkout(record), None, None, cwd=cwd)
        return {"pane": opened["pane"]}
    raise Refused("usage", f"herdr-core has no equivalent of: {' '.join(args)}")


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    machine = None
    if args and args[0].startswith("--machine="):
        machine = args.pop(0).split("=", 1)[1]
    try:
        result = run(args, machine)
    except Refused as exc:
        print(json.dumps({"error": {"code": exc.code, "message": str(exc)}}))
        return 1
    except hc.Error as exc:
        print(json.dumps({"error": {"code": exc.kind, "message": str(exc)}}))
        return 1
    if isinstance(result, str):
        print(result)
    elif result is not None:
        print(json.dumps({"result": result}))
    return 0


if __name__ == "__main__":
    # Run as the module of its own name, so whatever replaces a function there (the
    # tests' process table) also holds for the command line.
    from hc_panes import main as run_main

    raise SystemExit(run_main())
