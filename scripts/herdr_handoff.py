#!/usr/bin/env python3
"""Close one verified idle Codex or Claude TUI in herdr, then release its PR watch.

A watch on a worker is handed off from there through the controller's dashboard
(``worker_api``); this machine then follows the agent through that worker's herdr and
reads its transcript there through the probe.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

import machines
import pr_supervisor as supervisor
import remote_probe
import sandbox

SCRIPT = Path(__file__).resolve()
SGR = re.compile(r"\x1b\[([0-9;]*)m")
SCREEN_SETTLE_TIMEOUT = 5.0
SCREEN_SETTLE_INTERVAL = 0.25
# A worker's checks each cross the network: its screen gets longer to settle, and its
# agent is looked at less often while it works.
REMOTE_SETTLE_TIMEOUT = 30.0
REMOTE_POLL = 4
# Handoffs one worker may have running here at once; each polls it until done.
MAX_REMOTE_HANDOFFS = 4
# How long an exit may take to show, and a typed /exit to reach the composer.
EXIT_WAIT = 20
TYPED_EXIT_WAIT = 5


class ScreenNotReady(RuntimeError):
    """The final receipt or empty input has not finished rendering."""


class Unreachable(RuntimeError):
    """A worker's herdr failed; whether the worker or the command, it cannot tell."""


# Which TUI a watch was registered from: the conversation ID its CLI exports to
# child processes, the foreground executable name, and the human label.
AGENTS = {
    "codex": dict(env="CODEX_THREAD_ID", process="codex", label="Codex"),
    "claude": dict(env="CLAUDE_CODE_SESSION_ID", process="claude", label="Claude"),
}
CLAUDE_EXIT_HINT = "Press Ctrl-D again to exit"
RULE = re.compile(r"^\s*─+\s*$")


def herdr(*args, machine: machines.Machine | None = None):
    # herdr reaches a worker's server itself, so the command always runs here.
    proc = machines.run(None, None, machines.herdr_argv(machine, *args), text=True, timeout=15)
    if proc.returncode:
        error = RuntimeError if machine is None else Unreachable
        raise error(proc.stderr.strip() or proc.stdout.strip() or "herdr failed")
    return proc.stdout


def program(process):
    """A foreground process's program name: herdr's argv0 where it reports one (macOS),
    else its first argument (herdr on Linux reports only argv and name)."""
    argv = process.get("argv") or [""]
    return Path(process.get("argv0") or argv[0] or "").name


def result(*args, machine: machines.Machine | None = None):
    data = json.loads(herdr(*args, machine=machine))
    if data.get("error"):
        raise RuntimeError(str(data["error"]))
    return data["result"]


def inspect(pane, machine=None):
    info = result("pane", "get", pane, machine=machine)["pane"]
    procs = result("pane", "process-info", "--pane", pane, machine=machine)["process_info"]
    agent = result("agent", "get", pane, machine=machine)["agent"]
    return info, procs, agent


def latest_turn(rollout, session_id, agent="codex", machine=None, home=None):
    """The transcript's current turn and its final response; a worker's is read there."""
    if machine is not None:
        try:
            found = remote_probe.call(
                home,
                machine,
                "turn",
                supervisor.PROBE_TIMEOUT,
                file=rollout,
                session=session_id,
                agent=agent,
            )
        except remote_probe.Failed as exc:
            if exc.kind != "RuntimeError":
                raise
            # No turn to identify yet, as a local read would say it.
            raise RuntimeError(str(exc)) from exc
        return found["turn"], found["complete"]
    if agent == "claude":
        return latest_claude_turn(rollout, session_id)
    return latest_codex_turn(rollout, session_id)


def latest_codex_turn(rollout, session_id):
    with Path(rollout).open() as stream:
        return remote_probe.codex_turn(stream, session_id)


def latest_claude_turn(transcript, session_id):
    """See ``remote_probe.claude_turn``, which a worker's probe reads its transcripts with."""
    with Path(transcript).open() as stream:
        return remote_probe.claude_turn(stream, session_id)


def styled_chars(line: str) -> list[tuple[str, bool]]:
    """Retain faint style so a user-typed placeholder is not mistaken for empty input."""
    out: list[tuple[str, bool]] = []
    faint, position = False, 0
    for match in SGR.finditer(line):
        out.extend((c, faint) for c in line[position : match.start()])
        codes = [int(c or 0) for c in match.group(1).split(";")]
        i = 0
        while i < len(codes):
            code = codes[i]
            if code in (38, 48, 58) and i + 1 < len(codes):
                i += 5 if codes[i + 1] == 2 else 3
                continue
            if code in (0, 22):
                faint = False
            elif code == 2:
                faint = True
            i += 1
        position = match.end()
    out.extend((c, faint) for c in line[position:])
    return out


def empty_composer(screen, agent="codex"):
    if agent == "claude":
        return empty_claude_composer(screen)
    return empty_codex_composer(screen)


def empty_codex_composer(screen):
    # The supported Codex TUI renders its empty input placeholder in faint text.
    # Unknown layouts fail closed. Animated Braille artwork can occupy the
    # padding on either side of the placeholder; the placeholder must stay faint.
    candidates = []
    for line in screen.splitlines():
        chars = styled_chars(line)
        text = "".join(c for c, _ in chars)
        if text.lstrip().startswith("›"):
            candidates.append((chars, text))
    if not candidates:
        return False
    chars, text = candidates[-1]
    marker = text.index("›")
    start = marker + 1
    while start < len(text) and (text[start].isspace() or "\u2800" <= text[start] <= "\u28ff"):
        start += 1
    placeholder = "Ask Codex to do anything"
    if not text[start:].startswith(placeholder):
        return False
    end = start + len(placeholder)
    return all(dim for _, dim in chars[start:end]) and all(
        c.isspace() or "\u2800" <= c <= "\u28ff" for c in text[end:]
    )


def empty_claude_composer(screen):
    # Claude draws its composer between two horizontal rules: a "❯" prompt line, then any
    # wrapped continuation lines of a draft. Only a faint placeholder may follow the prompt.
    # Unknown layouts fail closed.
    lines = screen.splitlines()
    prompt = None
    for index, line in enumerate(lines):
        if SGR.sub("", line).lstrip().startswith("❯"):
            prompt = index
    if prompt is None:
        return False
    if prompt == 0 or not RULE.fullmatch(SGR.sub("", lines[prompt - 1])):
        return False
    chars = styled_chars(lines[prompt])
    text = "".join(c for c, _ in chars)
    if any(not (c.isspace() or dim) for c, dim in chars[text.index("❯") + 1 :]):
        return False
    for line in lines[prompt + 1 :]:
        plain = SGR.sub("", line)
        if plain.strip():
            return bool(RULE.match(plain))
    return False


def claude_composer(screen):
    """The text in Claude's composer, between its rules; None for any other layout."""
    lines = [SGR.sub("", line) for line in screen.splitlines()]
    prompts = [index for index, line in enumerate(lines) if line.lstrip().startswith("❯")]
    if not prompts or prompts[-1] == 0 or not RULE.fullmatch(lines[prompts[-1] - 1]):
        return None
    text = [lines[prompts[-1]].lstrip()[1:].strip()]
    for line in lines[prompts[-1] + 1 :]:
        if RULE.match(line):
            return " ".join(part for part in text if part)
        text.append(line.strip())
    return None


def verify_identity(target, info, procs):
    agent = target.get("agent", "codex")
    label = AGENTS[agent]["label"]
    if info["terminal_id"] != target["terminal_id"] or procs["shell_pid"] != target["shell_pid"]:
        raise RuntimeError("Pane terminal or shell changed; handoff cancelled")
    matches = [p for p in procs["foreground_processes"] if p["pid"] == target["agent_pid"]]
    if (
        len(matches) != 1
        or matches[0].get("argv") != target["agent_argv"]
        or info.get("agent") != agent
    ):
        raise RuntimeError(f"Original {label} process changed; handoff cancelled")
    import pane_runner

    # A worker's target names its machine; its paths are compared as they are.
    if not pane_runner.same_path(matches[0].get("cwd", ""), target["cwd"], target.get("machine")):
        raise RuntimeError(f"Original {label} cwd changed")


def fingerprint(path, machine=None, home=None):
    if machine is not None:
        found = remote_probe.call(home, machine, "fingerprint", supervisor.PROBE_TIMEOUT, file=path)
        return found["inode"], found["size"], found["mtime_ns"]
    stat = Path(path).stat()
    return stat.st_ino, stat.st_size, stat.st_mtime_ns


def verify_screen(target, info, screen, *, confirming=False):
    plain = SGR.sub("", screen)
    if info.get("scroll", {}).get("offset_from_bottom", 0) != 0:
        raise RuntimeError("Terminal is scrolled; leave it under user control")
    agent = target.get("agent", "codex")
    if "Queued follow-up inputs" in plain or "esc to interrupt" in plain:
        raise RuntimeError("Queued input or background work is visible; agent left open")
    if (CLAUDE_EXIT_HINT in plain and not confirming) or "Press Ctrl-C again" in plain:
        raise RuntimeError("Exit keys are already being pressed in this pane; handoff cancelled")
    if not empty_composer(screen, agent):
        prompt_char = "❯" if agent == "claude" else "›"
        prompts = [
            line.lstrip()[1:].strip()
            for line in plain.splitlines()
            if line.lstrip().startswith(prompt_char)
        ]
        if prompts and prompts[-1]:
            raise RuntimeError("Composer contains input or an unknown layout; no exit key sent")
        raise ScreenNotReady("Empty composer is not yet visible; no exit key sent")
    if target["marker"] not in plain:
        raise ScreenNotReady("Final receipt is not yet visible; no exit key sent")


def read_screen(pane, machine=None):
    return herdr(
        "agent",
        "read",
        pane,
        "--source",
        "visible",
        "--lines",
        "100",
        "--format",
        "ansi",
        machine=machine,
    )


def schedule(db, home, args):
    """Hand the watch ``args.id`` over once its agent answers, then exit that agent.

    ``args.machine``, set for a worker's request (``worker_api.handoff_args``), is the
    machine its token names; ``args.sessions`` (by agent) and ``args.cwd`` are the
    conversations and worktree that asked there, of which the watch's must be one, and
    ``args.rollout`` the transcript a sandbox's broker found for it. On a worker, only the
    watch's bound pane is handed off, and at most MAX_REMOTE_HANDOFFS at once.
    """
    import pane_runner

    machine = getattr(args, "machine", None)
    if machine is not None:
        try:
            job = supervisor.get_job(db, args.id)
        except ValueError:
            job = None
        # The watch's own session and worktree, and on the token's machine; from a
        # sandbox, exactly the transcript its broker found in its configuration directory.
        if (
            job is None
            or (job.get("machine"), job["session_id"], job["cwd"])
            != (machine.id, args.sessions.get(job.get("agent", "codex")), args.cwd)
            or args.rollout not in {None, job["rollout"]}
        ):
            raise ValueError(
                f"No watch {args.id} of this conversation and worktree on {machine.label}"
            )
        if not job.get("pane"):
            # Its pane is what ties the agent to exit to the one that registered it.
            raise ValueError("Only a watch registered with its pane is handed off from a worker")
    else:
        job = supervisor.get_job(db, args.id)
        if job.get("machine") is not None:
            raise ValueError(f"Watch {job['id']} is on {job['machine']}; hand it off from there")
    agent = job.get("agent", "codex")
    spec = AGENTS.get(agent)
    if spec is None:
        raise ValueError("Automatic initial exit supports Codex and Claude; exit manually first")
    if machine is None and os.environ.get(spec["env"]) != job["session_id"]:
        raise ValueError(f"Schedule the handoff from the registered {spec['label']} conversation")
    if job["status"] not in {"awaiting_release", "paused"}:
        raise ValueError("Watch must be awaiting release or paused before handoff")
    turn, complete = latest_turn(job["rollout"], job["session_id"], agent, machine, home)
    if complete is not None:
        raise ValueError("Schedule from an active turn, before its final response")
    info, procs, _ = inspect(args.pane, machine)
    matches = [
        p
        for p in procs["foreground_processes"]
        if program(p) == spec["process"]
        and pane_runner.same_path(p.get("cwd", ""), job["cwd"], machine)
    ]
    if info.get("agent") != agent or len(matches) != 1:
        raise ValueError(
            f"Pane must contain exactly one {spec['label']} foreground process in this worktree"
        )
    if matches[0]["pid"] == procs["shell_pid"]:
        raise ValueError(f"{spec['label']} is the pane root process; exiting could close the pane")
    if info.get("agent_status") == "blocked":
        raise ValueError(
            "Pane has a pending dialog/question; use manual handoff after resolving it"
        )
    bound = job.get("pane") or {}
    if machine is not None and (info["terminal_id"], procs["shell_pid"]) != (
        bound.get("terminal_id"),
        bound.get("shell_pid"),
    ):
        raise ValueError("Hand off from the watch's bound pane")
    token = uuid.uuid4().hex
    target = {
        "job_id": job["id"],
        "agent": agent,
        "session_id": job["session_id"],
        "rollout": job["rollout"],
        "cwd": job["cwd"],
        "pane_id": args.pane,
        "terminal_id": info["terminal_id"],
        "shell_pid": procs["shell_pid"],
        "agent_pid": matches[0]["pid"],
        "agent_argv": matches[0]["argv"],
        "turn_id": turn,
        "token": token,
        "marker": f"[babysit-handoff:{token[:16]}]",
        "timeout": args.timeout,
        # perform runs here and follows the agent there.
        **({"machine": machine.id} if machine is not None else {}),
    }
    # Bootstrap before asking the original agent to exit. No repair can launch
    # while this job is in handoff; this also proves the service can start here.
    try:
        supervisor.start_daemon(home, args.max_workers)
    except Exception as exc:
        if machine is None:
            raise
        # This machine's paths are no worker's business; its own log says more.
        print(f"Handoff of {job['id']} for {machine.label}: {exc}", file=sys.stderr)
        raise RuntimeError("The controller's supervisor could not start") from None
    with db:
        db.execute("BEGIN IMMEDIATE")
        current = supervisor.get_job(db, job["id"])
        if current["epoch"] != job["epoch"] or current["status"] != job["status"]:
            raise ValueError("Watch ownership changed while scheduling")
        if machine is not None and (
            sum(
                1
                for other in supervisor.jobs(db)
                if other.get("machine") == machine.id and other["status"] == "handoff"
            )
            >= MAX_REMOTE_HANDOFFS
        ):
            raise ValueError(f"Too many handoffs running on {machine.label}; try again later")
        current.update(
            status="handoff",
            epoch=current["epoch"] + 1,
            handoff_token=token,
            summary="Waiting for final response and verified TUI exit",
        )
        target["epoch"] = current["epoch"]
        supervisor.save_job(db, current)
    folder = home / "handoffs"
    try:
        folder.mkdir(exist_ok=True, mode=0o700)
        supervisor.watch.save_state(folder / f"{token}.json", target)
        with (folder / f"{token}.log").open("a") as log:
            subprocess.Popen(
                [sys.executable, str(SCRIPT), "--home", str(home), token],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
    except Exception as exc:
        with db:
            db.execute("BEGIN IMMEDIATE")
            current = supervisor.get_job(db, job["id"])
            if current["status"] == "handoff" and current.get("handoff_token") == token:
                current.update(
                    status="awaiting_release",
                    epoch=current["epoch"] + 1,
                    summary=f"Could not start automatic handoff: {exc}",
                )
                supervisor.save_job(db, current)
        if machine is None:
            raise
        print(f"Handoff of {job['id']} for {machine.label}: {exc}", file=sys.stderr)
        raise RuntimeError("Could not start the automatic handoff on the controller") from None
    scheduled = {"id": job["id"], "status": "handoff", "final_response_marker": target["marker"]}
    if machine is not None:
        return scheduled  # The audit is this machine's, out of a worker's reach.
    return {**scheduled, "audit_file": str(folder / f"{token}.audit.json")}


def send_exit(target, expected, before, owned, save, machine=None, home=None, *, typed=False):
    """Press Ctrl-D in a verified idle TUI, then confirm its process exited.

    ``expected`` is the (turn, complete) pair and ``before`` the transcript fingerprint the
    caller verified; ``owned`` re-checks the caller's claim before every key. An
    unconfirmed exit raises without pressing again. A Claude on ``machine``, a worker, is
    asked with /exit instead: its second Ctrl-D would have to come sooner than the checks
    before it can read that worker. Enter follows only once the composer holds exactly
    /exit, so it never answers a dialog that opened meanwhile. ``typed`` asks a Claude on
    this machine the same way, for a caller without a transcript turn to check against.
    """
    agent = target.get("agent", "codex")
    pane = target["pane_id"]
    typed = agent == "claude" and (typed or machine is not None)
    if typed:
        herdr("pane", "send-text", pane, sandbox.typed("/exit"), machine=machine)
        typed_by = time.monotonic() + TYPED_EXIT_WAIT
        while (shown := claude_composer(read_screen(pane, machine))) != "/exit":
            if time.monotonic() >= typed_by:
                # Only the /exit typed here is taken back, and only from the composer: a
                # draft beside it stays, and no key goes to a dialog.
                if shown is not None and shown.endswith("/exit"):
                    for _ in "/exit":
                        herdr("agent", "send-keys", pane, "backspace", machine=machine)
                    left = "the typed /exit was taken back"
                else:
                    left = "a typed /exit may be left in the pane"
                raise RuntimeError(f"The composer does not hold just /exit; Enter not sent; {left}")
            time.sleep(0.25)
        owned()
        herdr("agent", "send-keys", pane, "enter", machine=machine)
    else:
        # Never repeat blindly: a Ctrl-D that reaches the shell could close it. Claude asks
        # for a second press within about a second; it goes only to the still-running TUI.
        herdr("agent", "send-keys", pane, "ctrl+d", machine=machine)
    if agent == "claude" and not typed:
        confirmation_deadline = time.monotonic() + 0.75
        while time.monotonic() < confirmation_deadline:
            owned()
            info, procs, _ = inspect(target["pane_id"])
            if not any(p["pid"] == target["agent_pid"] for p in procs["foreground_processes"]):
                break  # Already exiting; never send a confirmation to the shell.
            verify_identity(target, info, procs)
            if (
                info.get("agent_status") not in {"idle", "done"}
                or info.get("scroll", {}).get("offset_from_bottom", 0) != 0
                or fingerprint(target["rollout"]) != before
                or latest_turn(target["rollout"], target["session_id"], agent) != expected
            ):
                raise RuntimeError("Session changed before exit confirmation; exit cancelled")
            confirmation = read_screen(target["pane_id"])
            # Reuse every input check, allowing only our own exit confirmation hint.
            verify_screen(target, info, confirmation, confirming=True)
            if CLAUDE_EXIT_HINT in SGR.sub("", confirmation):
                owned()
                save("exit_repeated", screen_before=confirmation)
                herdr("agent", "send-keys", pane, "ctrl+d")
                break
            time.sleep(0.05)
    deadline = time.monotonic() + EXIT_WAIT
    while True:
        owned()
        info = result("pane", "get", pane, machine=machine)["pane"]
        procs = result("pane", "process-info", "--pane", pane, machine=machine)["process_info"]
        if (
            info["terminal_id"] != target["terminal_id"]
            or procs["shell_pid"] != target["shell_pid"]
        ):
            raise RuntimeError("Shell or terminal changed after exit")
        foreground = procs["foreground_processes"]
        if foreground and all(p["pid"] == target["shell_pid"] for p in foreground):
            if machine is not None:
                # Its herdr no longer sees an agent, and the worker no longer runs it there:
                # a stopped agent, or one moved out of the foreground, is still running.
                if not info.get("agent") and not alive(target, machine, home):
                    return
            else:
                try:
                    os.kill(target["agent_pid"], 0)
                except ProcessLookupError:
                    return
        if time.monotonic() >= deadline:
            raise RuntimeError("Exit unconfirmed; no repeat key sent")
        time.sleep(0.5)


def alive(target, machine, home):
    """Whether the worker still runs the target's agent process, named by its pid and the
    digest of its command line (``remote_probe._host_alive``)."""
    return remote_probe.call(
        home,
        machine,
        "alive",
        supervisor.PROBE_TIMEOUT,
        pid=target["agent_pid"],
        digest=hashlib.sha256("\0".join(target["agent_argv"]).encode()).hexdigest(),
    )["alive"]


def perform(target, home, db):
    audit = home / "handoffs" / f"{target['token']}.audit.json"
    diagnostics: dict[str, str] = {}
    agent = target.get("agent", "codex")
    machine: machines.Machine | None = None

    def turn_now():
        return latest_turn(target["rollout"], target["session_id"], agent, machine, home)

    def rollout_now():
        return fingerprint(target["rollout"], machine, home)

    def observe():
        """The current turn; on a worker, also the fingerprint read with it in one call."""
        if machine is None:
            return (*turn_now(), None)
        found = remote_probe.call(
            home,
            machine,
            "turn_state",
            supervisor.PROBE_TIMEOUT,
            file=target["rollout"],
            session=target["session_id"],
            agent=agent,
        )
        return found["turn"], found["complete"], (found["inode"], found["size"], found["mtime_ns"])

    def save(stage, **details):
        supervisor.watch.save_state(audit, {**target, **diagnostics, "stage": stage, **details})

    def check_screen(info):
        screen = read_screen(target["pane_id"], machine)
        diagnostics["last_screen"] = screen
        try:
            verify_screen(target, info, screen)
        except RuntimeError as exc:
            diagnostics.setdefault("first_failed_screen", screen)
            diagnostics["last_failed_screen"] = screen
            diagnostics["screen_error"] = str(exc)
            raise
        return screen

    def still_owned():
        job = supervisor.get_job(db, target["job_id"])
        if (
            job["status"] != "handoff"
            or job["epoch"] != target["epoch"]
            or job.get("handoff_token") != target["token"]
        ):
            raise RuntimeError("Watch paused, stopped, or ownership changed; handoff cancelled")
        return job

    try:
        save("waiting")
        # A worker's agent: followed through its herdr, its transcript read there.
        machine = machines.resolve(home, target.get("machine"))
        deadline = time.monotonic() + target["timeout"]
        settle_deadline = None
        settle_identity = None
        while True:
            still_owned()
            info, procs, state = inspect(target["pane_id"], machine)
            verify_identity(target, info, procs)
            turn, complete, seen = observe()
            if turn != target["turn_id"]:
                raise RuntimeError("A new turn started; handoff cancelled")
            if info.get("agent_status") == "blocked":
                raise RuntimeError("Dialog or question is pending; handoff cancelled")
            if settle_deadline is not None and (
                info.get("agent_status") not in {"idle", "done"}
                or (state.get("state_change_seq"), seen or rollout_now()) != settle_identity
                or complete is None
            ):
                raise RuntimeError(
                    "Session or input changed while waiting for display; handoff cancelled"
                )
            if complete is not None:
                if target["marker"] not in complete:
                    raise RuntimeError("Final response does not confirm this handoff")
                if info.get("agent_status") in {"idle", "done"}:
                    if settle_deadline is None:
                        settle = SCREEN_SETTLE_TIMEOUT if machine is None else REMOTE_SETTLE_TIMEOUT
                        settle_deadline = min(deadline, time.monotonic() + settle)
                        settle_identity = (
                            state.get("state_change_seq"),
                            seen or rollout_now(),
                        )
                    try:
                        check_screen(info)
                    except ScreenNotReady as exc:
                        save("waiting_for_screen")
                        if time.monotonic() >= settle_deadline:
                            raise RuntimeError(
                                f"Timed out waiting for final display: {exc}"
                            ) from exc
                        time.sleep(SCREEN_SETTLE_INTERVAL)
                        continue
                    break
            if time.monotonic() >= deadline:
                raise RuntimeError("Timed out waiting for the final response; agent left open")
            time.sleep(1 if machine is None else REMOTE_POLL)
        before = rollout_now()
        info2, procs2, state2 = inspect(target["pane_id"], machine)
        verify_identity(target, info2, procs2)
        screen2 = check_screen(info2)
        if (
            info2.get("agent_status") not in {"idle", "done"}
            or state.get("state_change_seq") is None
            or state2.get("state_change_seq") != state.get("state_change_seq")
            or target["marker"] not in SGR.sub("", screen2)
            or not empty_composer(screen2, agent)
            or rollout_now() != before
            or turn_now() != (turn, complete)
        ):
            raise RuntimeError("Session or input changed before exit; handoff cancelled")
        still_owned()
        save("exit_sent", screen_before=screen2)
        send_exit(target, (turn, complete), before, still_owned, save, machine, home)
        with db:
            db.execute("BEGIN IMMEDIATE")
            job = still_owned()
            job.update(
                status="watching",
                epoch=job["epoch"] + 1,
                next_poll=0,
                dispatch_ready=False,
                summary="Original TUI exited; watcher owns this session",
            )
            supervisor.save_job(db, job)
        save("released")
        supervisor.emit({"id": target["job_id"], "status": "watching"})
    except Exception as exc:
        save("error", error=str(exc))
        with db:
            db.execute("BEGIN IMMEDIATE")
            job = supervisor.get_job(db, target["job_id"])
            if job["status"] == "handoff" and job.get("handoff_token") == target["token"]:
                job.update(
                    status="awaiting_release",
                    epoch=job["epoch"] + 1,
                    summary=f"Automatic handoff stopped: {exc}",
                )
                supervisor.save_job(db, job)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("token")
    args = parser.parse_args()
    os.umask(0o077)
    if not re.fullmatch(r"[0-9a-f]{32}", args.token):
        parser.error("Invalid handoff token")
    db = supervisor.open_db(args.home)
    try:
        with supervisor.locked(args.home / "handoffs" / f"{args.token}.lock"):
            target = json.loads((args.home / "handoffs" / f"{args.token}.json").read_text())
            # A previous ambiguous key delivery is never retried on restart.
            audit = args.home / "handoffs" / f"{args.token}.audit.json"
            if audit.exists():
                raise RuntimeError("This handoff was already attempted; inspect its audit")
            perform(target, args.home, db)
    finally:
        db.close()


if __name__ == "__main__":
    main()
