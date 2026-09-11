#!/usr/bin/env python3
"""Close one verified idle Codex TUI in herdr, then release its PR watch."""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

import pr_supervisor as supervisor

SCRIPT = Path(__file__).resolve()
SGR = re.compile(r"\x1b\[([0-9;]*)m")


def herdr(*args):
    proc = subprocess.run(["herdr", *args], capture_output=True, text=True, timeout=15)
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


def latest_turn(rollout, session_id):
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


def styled_chars(line):
    """Retain faint style so a user-typed placeholder is not mistaken for empty input."""
    out, faint, position = [], False, 0
    for match in SGR.finditer(line):
        out.extend((c, faint) for c in line[position:match.start()])
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


def empty_composer(screen):
    # The supported Codex TUI renders its empty input placeholder in faint text.
    # Unknown layouts fail closed. Braille after the placeholder is pet artwork.
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
    while start < len(text) and text[start].isspace():
        start += 1
    placeholder = "Ask Codex to do anything"
    if not text[start:].startswith(placeholder):
        return False
    end = start + len(placeholder)
    return (all(dim for _, dim in chars[start:end])
            and all(c.isspace() or "\u2800" <= c <= "\u28ff" for c in text[end:]))


def verify_identity(target, info, procs):
    if info["terminal_id"] != target["terminal_id"] or procs["shell_pid"] != target["shell_pid"]:
        raise RuntimeError("Pane terminal or shell changed; handoff cancelled")
    matches = [p for p in procs["foreground_processes"] if p["pid"] == target["agent_pid"]]
    if (len(matches) != 1 or matches[0].get("argv") != target["agent_argv"]
            or info.get("agent") != "codex"):
        raise RuntimeError("Original Codex process changed; handoff cancelled")
    if Path(matches[0].get("cwd", "")).resolve() != Path(target["cwd"]).resolve():
        raise RuntimeError("Original Codex cwd changed")


def fingerprint(path):
    stat = Path(path).stat()
    return stat.st_ino, stat.st_size, stat.st_mtime_ns


def verify_screen(target, info, screen):
    plain = SGR.sub("", screen)
    if info.get("scroll", {}).get("offset_from_bottom", 0) != 0:
        raise RuntimeError("Terminal is scrolled; leave it under user control")
    if target["marker"] not in plain or not empty_composer(screen):
        raise RuntimeError("Final receipt or empty composer is unverified; no exit key sent")
    if "Queued follow-up inputs" in plain or "esc to interrupt" in plain:
        raise RuntimeError("Queued input or background work is visible; agent left open")


def schedule(db, home, args):
    job = supervisor.get_job(db, args.id)
    if job.get("agent", "codex") != "codex":
        raise ValueError("Automatic initial exit currently supports Codex; exit Claude manually before release")
    if os.environ.get("CODEX_THREAD_ID") != job["session_id"]:
        raise ValueError("Schedule the handoff from the registered Codex conversation")
    if job["status"] not in {"awaiting_release", "paused"}:
        raise ValueError("Watch must be awaiting release or paused before handoff")
    turn, complete = latest_turn(job["rollout"], job["session_id"])
    if complete is not None:
        raise ValueError("Schedule from an active turn, before its final response")
    info, procs, _ = inspect(args.pane)
    matches = [p for p in procs["foreground_processes"]
               if Path(p.get("argv0", "")).name == "codex"
               and Path(p.get("cwd", "")).resolve() == Path(job["cwd"]).resolve()]
    if info.get("agent") != "codex" or len(matches) != 1:
        raise ValueError("Pane must contain exactly one Codex foreground process in this worktree")
    if matches[0]["pid"] == procs["shell_pid"]:
        raise ValueError("Codex is the pane root process; exiting could close the pane")
    if info.get("agent_status") == "blocked":
        raise ValueError("Pane has a pending dialog/question; use manual handoff after resolving it")
    token = uuid.uuid4().hex
    target = {
        "job_id": job["id"], "session_id": job["session_id"], "rollout": job["rollout"],
        "cwd": job["cwd"], "pane_id": args.pane, "terminal_id": info["terminal_id"],
        "shell_pid": procs["shell_pid"], "agent_pid": matches[0]["pid"],
        "agent_argv": matches[0]["argv"], "turn_id": turn, "token": token,
        "marker": f"[babysit-handoff:{token[:16]}]", "timeout": args.timeout,
    }
    # Bootstrap before asking the original agent to exit. No repair can launch
    # while this job is in handoff; this also proves the service can start here.
    supervisor.start_daemon(home, args.max_workers)
    with db:
        db.execute("BEGIN IMMEDIATE")
        current = supervisor.get_job(db, job["id"])
        if current["epoch"] != job["epoch"] or current["status"] != job["status"]:
            raise ValueError("Watch ownership changed while scheduling")
        current.update(status="handoff", epoch=current["epoch"] + 1,
                       handoff_token=token, summary="Waiting for final response and verified TUI exit")
        target["epoch"] = current["epoch"]
        supervisor.save_job(db, current)
    folder = home / "handoffs"
    try:
        folder.mkdir(exist_ok=True, mode=0o700)
        supervisor.watch.save_state(folder / f"{token}.json", target)
        with (folder / f"{token}.log").open("a") as log:
            subprocess.Popen([sys.executable, str(SCRIPT), "--home", str(home), token],
                             stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                             start_new_session=True)
    except Exception as exc:
        with db:
            db.execute("BEGIN IMMEDIATE")
            current = supervisor.get_job(db, job["id"])
            if current["status"] == "handoff" and current.get("handoff_token") == token:
                current.update(status="awaiting_release", epoch=current["epoch"] + 1,
                               summary=f"Could not start automatic handoff: {exc}")
                supervisor.save_job(db, current)
        raise
    return {"id": job["id"], "status": "handoff", "final_response_marker": target["marker"],
            "audit_file": str(folder / f"{token}.audit.json")}


def perform(target, home, db):
    audit = home / "handoffs" / f"{target['token']}.audit.json"

    def save(stage, **details):
        supervisor.watch.save_state(audit, {**target, "stage": stage, **details})

    def still_owned():
        job = supervisor.get_job(db, target["job_id"])
        if (job["status"] != "handoff" or job["epoch"] != target["epoch"]
                or job.get("handoff_token") != target["token"]):
            raise RuntimeError("Watch paused, stopped, or ownership changed; handoff cancelled")
        return job

    try:
        save("waiting")
        deadline = time.monotonic() + target["timeout"]
        while True:
            still_owned()
            info, procs, agent = inspect(target["pane_id"])
            verify_identity(target, info, procs)
            turn, complete = latest_turn(target["rollout"], target["session_id"])
            if turn != target["turn_id"]:
                raise RuntimeError("A new turn started; handoff cancelled")
            if info.get("agent_status") == "blocked":
                raise RuntimeError("Dialog or question is pending; handoff cancelled")
            if complete is not None:
                if target["marker"] not in complete:
                    raise RuntimeError("Final response does not confirm this handoff")
                if info.get("agent_status") in {"idle", "done"}:
                    break
            if time.monotonic() >= deadline:
                raise RuntimeError("Timed out waiting for the final response; agent left open")
            time.sleep(1)
        screen = herdr("agent", "read", target["pane_id"], "--source", "visible", "--lines", "100", "--format", "ansi")
        verify_screen(target, info, screen)
        before = fingerprint(target["rollout"])
        info2, procs2, agent2 = inspect(target["pane_id"])
        verify_identity(target, info2, procs2)
        screen2 = herdr("agent", "read", target["pane_id"], "--source", "visible", "--lines", "100", "--format", "ansi")
        verify_screen(target, info2, screen2)
        if (info2.get("agent_status") not in {"idle", "done"}
                or agent.get("state_change_seq") is None
                or agent2.get("state_change_seq") != agent.get("state_change_seq")
                or target["marker"] not in SGR.sub("", screen2) or not empty_composer(screen2)
                or fingerprint(target["rollout"]) != before
                or latest_turn(target["rollout"], target["session_id"]) != (turn, complete)):
            raise RuntimeError("Session or input changed before exit; handoff cancelled")
        still_owned()
        save("exit_sent", screen_before=screen2)
        # Send once. Never repeat: a second Ctrl-D could close the shell.
        herdr("agent", "send-keys", target["pane_id"], "ctrl+d")
        deadline = time.monotonic() + 20
        while True:
            still_owned()
            info = result("pane", "get", target["pane_id"])["pane"]
            procs = result("pane", "process-info", "--pane", target["pane_id"])["process_info"]
            if info["terminal_id"] != target["terminal_id"] or procs["shell_pid"] != target["shell_pid"]:
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
            job.update(status="watching", epoch=job["epoch"] + 1, next_poll=0,
                       dispatch_ready=False, summary="Original TUI exited; watcher owns this session")
            supervisor.save_job(db, job)
        save("released")
        supervisor.emit({"id": target["job_id"], "status": "watching"})
    except Exception as exc:
        save("error", error=str(exc))
        with db:
            db.execute("BEGIN IMMEDIATE")
            job = supervisor.get_job(db, target["job_id"])
            if job["status"] == "handoff" and job.get("handoff_token") == target["token"]:
                job.update(status="awaiting_release", epoch=job["epoch"] + 1,
                           summary=f"Automatic handoff stopped: {exc}")
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
