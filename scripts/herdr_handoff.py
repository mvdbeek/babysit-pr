#!/usr/bin/env python3
"""Close one verified idle Codex or Claude TUI in herdr, then release its PR watch."""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

import owned_process
import pr_supervisor as supervisor

SCRIPT = Path(__file__).resolve()
SGR = re.compile(r"\x1b\[([0-9;]*)m")
SCREEN_SETTLE_TIMEOUT = 5.0
SCREEN_SETTLE_INTERVAL = 0.25


class ScreenNotReady(RuntimeError):
    """The final receipt or empty input has not finished rendering."""


# Which TUI a watch was registered from: the conversation ID its CLI exports to
# child processes, the foreground executable name, and the human label.
AGENTS = {
    "codex": dict(env="CODEX_THREAD_ID", process="codex", label="Codex"),
    "claude": dict(env="CLAUDE_CODE_SESSION_ID", process="claude", label="Claude"),
}
CLAUDE_EXIT_HINT = "Press Ctrl-D again to exit"
RULE = re.compile(r"^\s*─+\s*$")


def herdr(*args):
    proc = owned_process.run(["herdr", *args], text=True, timeout=15)
    if proc.returncode:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "herdr failed")
    return proc.stdout


def result(*args):
    data = json.loads(herdr(*args))
    if data.get("error"):
        raise RuntimeError(str(data["error"]))
    return data["result"]


def inspect(pane):
    info = result("pane", "get", pane)["pane"]
    procs = result("pane", "process-info", "--pane", pane)["process_info"]
    agent = result("agent", "get", pane)["agent"]
    return info, procs, agent


def latest_turn(rollout, session_id, agent="codex"):
    if agent == "claude":
        return latest_claude_turn(rollout, session_id)
    return latest_codex_turn(rollout, session_id)


def latest_codex_turn(rollout, session_id):
    meta, turn, complete = None, None, None
    with Path(rollout).open() as stream:
        for line in stream:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue  # the writer may still be appending the final line
            payload = item.get("payload") or {}
            if item.get("type") == "session_meta":
                meta = payload.get("id")
            elif item.get("type") == "event_msg":
                if payload.get("type") == "task_started":
                    turn, complete = payload.get("turn_id"), None
                elif payload.get("type") == "task_complete" and payload.get("turn_id") == turn:
                    complete = payload.get("last_agent_message", "")
                elif payload.get("type") in {"turn_aborted", "user_message"}:
                    complete = None
    if meta != session_id or not turn:
        raise RuntimeError("Cannot identify the current turn in the registered rollout")
    return turn, complete


def latest_claude_turn(transcript, session_id):
    """A turn is the last typed prompt; it is complete once its turn_duration record lands.

    The completed text is the assistant text after the turn's final tool result, i.e. the
    response the user sees, which must carry the handoff marker.
    """
    turn, complete, seen = None, None, False
    texts: list[str] = []
    with Path(transcript).open() as stream:
        for line in stream:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue  # the writer may still be appending the final line
            if item.get("isSidechain"):
                continue  # subagent traffic never ends the main conversation's turn
            kind = item.get("type")
            if kind in {"user", "assistant"}:
                if item.get("sessionId") != session_id:
                    raise RuntimeError("Claude transcript session ID does not match")
                seen = True
                complete = None
            content = (item.get("message") or {}).get("content")
            blocks = content if isinstance(content, list) else []
            if kind == "user":
                if any(b.get("type") == "tool_result" for b in blocks):
                    texts = []  # tool output: the final response restarts after it
                elif not item.get("isMeta"):  # injected context is not a new prompt
                    turn, complete, texts = item.get("uuid"), None, []
            elif kind == "assistant":
                texts.extend(b.get("text", "") for b in blocks if b.get("type") == "text")
            elif kind == "system" and item.get("subtype") == "turn_duration" and turn:
                complete = "\n".join(texts)
    if not seen or not turn:
        raise RuntimeError("Cannot identify the current turn in the registered transcript")
    return turn, complete


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
    if Path(matches[0].get("cwd", "")).resolve() != Path(target["cwd"]).resolve():
        raise RuntimeError(f"Original {label} cwd changed")


def fingerprint(path):
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


def read_screen(pane):
    return herdr("agent", "read", pane, "--source", "visible", "--lines", "100", "--format", "ansi")


def schedule(db, home, args):
    job = supervisor.get_job(db, args.id)
    agent = job.get("agent", "codex")
    spec = AGENTS.get(agent)
    if spec is None:
        raise ValueError("Automatic initial exit supports Codex and Claude; exit manually first")
    if os.environ.get(spec["env"]) != job["session_id"]:
        raise ValueError(f"Schedule the handoff from the registered {spec['label']} conversation")
    if job["status"] not in {"awaiting_release", "paused"}:
        raise ValueError("Watch must be awaiting release or paused before handoff")
    turn, complete = latest_turn(job["rollout"], job["session_id"], agent)
    if complete is not None:
        raise ValueError("Schedule from an active turn, before its final response")
    info, procs, _ = inspect(args.pane)
    matches = [
        p
        for p in procs["foreground_processes"]
        if Path(p.get("argv0", "")).name == spec["process"]
        and Path(p.get("cwd", "")).resolve() == Path(job["cwd"]).resolve()
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
    }
    # Bootstrap before asking the original agent to exit. No repair can launch
    # while this job is in handoff; this also proves the service can start here.
    supervisor.start_daemon(home, args.max_workers)
    with db:
        db.execute("BEGIN IMMEDIATE")
        current = supervisor.get_job(db, job["id"])
        if current["epoch"] != job["epoch"] or current["status"] != job["status"]:
            raise ValueError("Watch ownership changed while scheduling")
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
        raise
    return {
        "id": job["id"],
        "status": "handoff",
        "final_response_marker": target["marker"],
        "audit_file": str(folder / f"{token}.audit.json"),
    }


def perform(target, home, db):
    audit = home / "handoffs" / f"{target['token']}.audit.json"
    diagnostics: dict[str, str] = {}
    agent = target.get("agent", "codex")

    def save(stage, **details):
        supervisor.watch.save_state(audit, {**target, **diagnostics, "stage": stage, **details})

    def check_screen(info):
        screen = herdr(
            "agent",
            "read",
            target["pane_id"],
            "--source",
            "visible",
            "--lines",
            "100",
            "--format",
            "ansi",
        )
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
        deadline = time.monotonic() + target["timeout"]
        settle_deadline = None
        settle_identity = None
        while True:
            still_owned()
            info, procs, state = inspect(target["pane_id"])
            verify_identity(target, info, procs)
            turn, complete = latest_turn(target["rollout"], target["session_id"], agent)
            if turn != target["turn_id"]:
                raise RuntimeError("A new turn started; handoff cancelled")
            if info.get("agent_status") == "blocked":
                raise RuntimeError("Dialog or question is pending; handoff cancelled")
            if settle_deadline is not None and (
                info.get("agent_status") not in {"idle", "done"}
                or (state.get("state_change_seq"), fingerprint(target["rollout"]))
                != settle_identity
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
                        settle_deadline = min(deadline, time.monotonic() + SCREEN_SETTLE_TIMEOUT)
                        settle_identity = (
                            state.get("state_change_seq"),
                            fingerprint(target["rollout"]),
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
            time.sleep(1)
        before = fingerprint(target["rollout"])
        info2, procs2, state2 = inspect(target["pane_id"])
        verify_identity(target, info2, procs2)
        screen2 = check_screen(info2)
        if (
            info2.get("agent_status") not in {"idle", "done"}
            or state.get("state_change_seq") is None
            or state2.get("state_change_seq") != state.get("state_change_seq")
            or target["marker"] not in SGR.sub("", screen2)
            or not empty_composer(screen2, agent)
            or fingerprint(target["rollout"]) != before
            or latest_turn(target["rollout"], target["session_id"], agent) != (turn, complete)
        ):
            raise RuntimeError("Session or input changed before exit; handoff cancelled")
        still_owned()
        save("exit_sent", screen_before=screen2)
        # Never repeat blindly: a Ctrl-D that reaches the shell could close it. Claude asks
        # for a second press within about a second; it goes only to the still-running TUI.
        herdr("agent", "send-keys", target["pane_id"], "ctrl+d")
        if agent == "claude":
            confirmation_deadline = time.monotonic() + 0.75
            while time.monotonic() < confirmation_deadline:
                still_owned()
                info3, procs3, _ = inspect(target["pane_id"])
                if not any(p["pid"] == target["agent_pid"] for p in procs3["foreground_processes"]):
                    break  # Already exiting; never send a confirmation to the shell.
                verify_identity(target, info3, procs3)
                if (
                    info3.get("agent_status") not in {"idle", "done"}
                    or info3.get("scroll", {}).get("offset_from_bottom", 0) != 0
                    or fingerprint(target["rollout"]) != before
                    or latest_turn(target["rollout"], target["session_id"], agent)
                    != (turn, complete)
                ):
                    raise RuntimeError(
                        "Session changed before exit confirmation; handoff cancelled"
                    )
                confirmation = read_screen(target["pane_id"])
                # Reuse every input check, allowing only our own exit confirmation hint.
                verify_screen(target, info3, confirmation, confirming=True)
                if CLAUDE_EXIT_HINT in SGR.sub("", confirmation):
                    still_owned()
                    save("exit_repeated", screen_before=confirmation)
                    herdr("agent", "send-keys", target["pane_id"], "ctrl+d")
                    break
                time.sleep(0.05)
        deadline = time.monotonic() + 20
        while True:
            still_owned()
            info = result("pane", "get", target["pane_id"])["pane"]
            procs = result("pane", "process-info", "--pane", target["pane_id"])["process_info"]
            if (
                info["terminal_id"] != target["terminal_id"]
                or procs["shell_pid"] != target["shell_pid"]
            ):
                raise RuntimeError("Shell or terminal changed after exit; watch was not released")
            foreground = procs["foreground_processes"]
            if foreground and all(p["pid"] == target["shell_pid"] for p in foreground):
                try:
                    os.kill(target["agent_pid"], 0)
                except ProcessLookupError:
                    break
            if time.monotonic() >= deadline:
                raise RuntimeError("Exit unconfirmed; no repeat key and no watch release")
            time.sleep(0.5)
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
