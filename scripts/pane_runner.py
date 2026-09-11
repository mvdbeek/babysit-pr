"""Bind a watch to its original terminal and launch bounded work visibly there."""

import os
import shlex
import sys
from pathlib import Path

from herdr_handoff import herdr, result


def inspect(pane):
    info = result("pane", "get", pane)["pane"]
    procs = result("pane", "process-info", "--pane", pane)["process_info"]
    return info, procs


def capture(pane, cwd, agent_kind="codex"):
    info, procs = inspect(pane)
    if Path(info.get("foreground_cwd") or info.get("cwd", "")).resolve() != Path(cwd).resolve():
        raise RuntimeError("Pane worktree does not match this watch")
    if not procs.get("shell_pid"):
        raise RuntimeError("Pane has no identifiable shell")
    foreground = procs.get("foreground_processes") or []
    shell_only = foreground and all(p["pid"] == procs["shell_pid"] for p in foreground)
    if not shell_only and info.get("agent") != agent_kind:
        raise RuntimeError(f"Bind the original {agent_kind} pane or its remaining shell")
    return {"pane_id": pane, "terminal_id": info["terminal_id"], "shell_pid": procs["shell_pid"]}


def release_is_foreground(procs, pane):
    """Recognize this release command, invoked directly by the bound shell."""
    pid = os.getpid()
    foreground = procs.get("foreground_processes") or []
    if (
        os.getppid() != procs.get("shell_pid")
        or not os.isatty(0)
        or procs.get("foreground_process_group_id") != os.getpgrp()
        or os.tcgetpgrp(0) != os.getpgrp()
        or not any(p["pid"] == pid for p in foreground)
    ):
        return False
    # The synchronous process-info query may itself be present in its snapshot.
    # No other sibling pipeline command or agent is part of this exemption.
    for process in foreground:
        if process["pid"] == pid:
            continue
        argv = process.get("argv") or []
        if not (
            argv
            and Path(argv[0]).name == "herdr"
            and argv[1:] == ["pane", "process-info", "--pane", pane]
        ):
            return False
    return True


def locate(binding, cwd, require_shell=False, runner_pid=None, allow_release=False):
    # Terminal identity survives pane moves; never fall back to a matching cwd.
    panes = result("pane", "list")["panes"]
    matches = [p for p in panes if p.get("terminal_id") == binding["terminal_id"]]
    if len(matches) != 1:
        raise RuntimeError("Original herdr terminal is missing; no background fallback")
    pane = matches[0]["pane_id"]
    info, procs = inspect(pane)
    if (
        info["terminal_id"] != binding["terminal_id"]
        or procs.get("shell_pid") != binding["shell_pid"]
    ):
        raise RuntimeError("Original herdr terminal or shell was replaced")
    foreground = procs.get("foreground_processes") or []
    if Path(info.get("foreground_cwd") or info.get("cwd", "")).resolve() != Path(cwd).resolve():
        raise RuntimeError("Original pane changed worktree; pause and inspect it")
    shell_only = bool(foreground) and all(p["pid"] == binding["shell_pid"] for p in foreground)
    own_release = allow_release and not info.get("agent") and release_is_foreground(procs, pane)
    if require_shell and (info.get("agent") or not (shell_only or own_release)):
        raise RuntimeError("Original pane is busy; leave its agent or command undisturbed")
    if runner_pid is not None and not any(p["pid"] == runner_pid for p in foreground):
        raise RuntimeError("Repair runner is not in the original pane foreground")
    return pane


def launch(job, home, script):
    pane = locate(job["pane"], job["cwd"], require_shell=True)
    command = shlex.join(
        [sys.executable, str(script), "--home", str(home), "_repair", job["id"], job["attempt"]]
    )
    # Exactly one atomic text+Enter submission. Never retry ambiguous delivery.
    herdr("pane", "run", pane, command)  # successful CLI submission has no JSON body


def verify_runner(job):
    if not os.isatty(sys.stdout.fileno()):
        raise RuntimeError("Pane-bound work requires its original terminal; no headless fallback")
    return locate(job["pane"], job["cwd"], runner_pid=os.getpid())
