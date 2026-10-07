"""Inspect Safehouse's Docker grant and prepare an exact-session restart in herdr."""

import ctypes
import os
import time
from pathlib import Path

import herdr_handoff as handoff

EXIT_WAIT = 20
_exiting: set[tuple[str, int]] = set()
# Safehouse's docker integration grants these paths even when the daemon is stopped.
# Inspect the policy, not `docker info` in the dashboard's unrelated sandbox.
SOCKETS = ("/private/var/run/docker.sock", "~/.docker/run/docker.sock")


def has_access(pid):
    """Check the running process's Safehouse socket file grants (macOS Seatbelt)."""
    try:
        library = ctypes.CDLL("/usr/lib/libsandbox.dylib")
        check = library.sandbox_check
        # sandbox_check has three fixed arguments followed by the filter's argument.
        # Declaration: WebKit/Source/WTF/wtf/spi/darwin/SandboxSPI.h.
        check.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
        check.restype = ctypes.c_int
        quiet = ctypes.c_int.in_dll(library, "SANDBOX_CHECK_NO_REPORT").value
        results = [
            check(pid, operation, 1 | quiet, ctypes.c_char_p(os.fsencode(Path(path).expanduser())))
            for path in SOCKETS
            for operation in (b"file-read-data", b"file-write-data")
        ]
    except (OSError, AttributeError, ValueError) as exc:
        raise ValueError("Could not inspect the agent's Docker access") from exc
    if any(result < 0 for result in results):
        raise ValueError("Could not inspect the agent's Docker access")
    return all(result == 0 for result in results)


def process(procs, kind):
    matches = [p for p in procs["foreground_processes"] if Path(p.get("argv0", "")).name == kind]
    if len(matches) != 1:
        raise ValueError("Could not identify the agent process; nothing was sent")
    return matches[0]


def resume_options(argv, kind, session):
    """Keep known startup options; refuse unknown flags rather than change their meaning."""
    flags = {
        "--dangerously-bypass-approvals-and-sandbox",
        "--yolo",
        "--full-auto",
        "--dangerously-skip-permissions",
        "--allow-dangerously-skip-permissions",
        "--no-alt-screen",
        "--verbose",
    }
    valued = {
        "--model",
        "-m",
        "--effort",
        "--config",
        "-c",
        "--sandbox",
        "-s",
        "--ask-for-approval",
        "-a",
        "--permission-mode",
        "--profile",
        "-p",
        "--add-dir",
        "--enable",
        "--disable",
        "--settings",
        "--setting-sources",
        "--append-system-prompt",
        "--system-prompt",
        "--agent",
    }
    # Claude -p means print, not Codex's profile: an interactive restart must not add it.
    if kind == "claude":
        valued.difference_update({"-p", "-c"})
    words = iter(argv[1:])
    kept = []
    for word in words:
        if word == "--":
            break  # The old prompt is never replayed.
        if word in {"resume", "--resume", "-r"}:
            previous = next(words, None)
            if previous != session:
                raise ValueError("The process resume ID differs from the current session")
        elif word in {"--continue", "--last"} or (kind == "claude" and word == "-c"):
            continue  # Replaced with the exact current session UUID.
        elif word in flags:
            kept.append(word)
        elif word.partition("=")[0] in valued:
            kept.append(word)
            if "=" not in word:
                value = next(words, None)
                if value is None:
                    raise ValueError("Incomplete agent launch option; nothing was sent")
                kept.append(value)
        elif word.startswith("-"):
            raise ValueError(f"Cannot preserve agent launch option {word}; restart it in Collie")
        # A positional initial prompt is intentionally omitted.
    return kept


def launcher(procs, proc, enabled=True):
    """Reuse the original Safehouse invocation, avoiding shell-wrapper flag duplication."""
    for parent in procs["foreground_processes"]:
        argv = parent.get("argv", [])
        # Safehouse is a shell script, so argv can start with its interpreter.
        start = next(
            (
                i
                for i, word in enumerate(argv[:2])
                if Path(word).name == "safehouse" and Path(word).is_absolute()
            ),
            None,
        )
        if start is None:
            continue
        command = len(argv) - len(proc["argv"])
        if (
            command <= start
            or Path(argv[command]).name != Path(proc["argv"][0]).name
            or argv[command + 1 :] != proc["argv"][1:]
        ):
            continue
        prefix = argv[start:command]
        if prefix[-1] == "--":
            prefix = prefix[:-1]
        # Remove Docker from repeated --enable lists, preserving other grants.
        updated = []
        removed = False
        words = iter(prefix)
        for word in words:
            if word == "--enable" or word.startswith("--enable="):
                value = next(words) if word == "--enable" else word.split("=", 1)[1]
                features = value.split(",")
                kept = [feature for feature in features if feature.strip().lower() != "docker"]
                removed |= len(kept) != len(features)
                if kept:
                    updated.append("--enable=" + ",".join(kept))
            else:
                updated.append(word)
        if not enabled and not removed:
            raise ValueError("Docker access comes from another policy; change it in Collie")
        if enabled:
            updated.append("--enable=docker")
        return [*updated, "--", proc["argv"][0]]
    raise ValueError("Could not preserve the agent's Safehouse launch; restart it in Collie")


def inspect(target):
    info, procs, state = handoff.inspect(target["pane"])
    kind = info.get("agent")
    if kind not in {"claude", "codex"}:
        raise ValueError("That agent is no longer running; refresh")
    proc = process(procs, kind)
    return info, procs, state, proc


def prepare(target, info, procs, state, proc, enabled=True):
    session = (info.get("agent_session") or {}).get("value")
    if not session or session != target.get("session"):
        raise ValueError("Could not verify the current conversation; refresh before sending")
    if proc["pid"] == procs["shell_pid"]:
        raise ValueError("The agent is the pane's root process; restart it in Collie")
    if info.get("agent_status") not in {"idle", "done"}:
        raise ValueError("Wait until the agent is idle before changing Docker access")
    if state.get("state_change_seq") is None:
        raise ValueError("Could not verify the agent's state; refresh before sending")
    record = {
        "pane_id": target["pane"],
        "terminal_id": info["terminal_id"],
        "shell_pid": procs["shell_pid"],
        "agent_pid": proc["pid"],
        "agent_argv": proc["argv"],
        "agent": info["agent"],
        "cwd": proc["cwd"],
        "session_id": session,
        "marker": "",
        "seq": state["state_change_seq"],
    }
    if not Path(record["cwd"]).is_dir():
        raise ValueError("The agent's directory no longer exists")
    record["launcher"] = launcher(procs, proc, enabled)
    record["options"] = resume_options(proc["argv"], info["agent"], session)
    handoff.verify_screen(record, info, handoff.read_screen(target["pane"]))
    return record


def quit_agent(record):
    """Exit once, only after identity, state and the empty composer are rechecked."""
    pane = record["pane_id"]
    info, procs, state = handoff.inspect(pane)
    handoff.verify_identity(record, info, procs)
    if (
        (info.get("agent_session") or {}).get("value") != record["session_id"]
        or info.get("agent_status") not in {"idle", "done"}
        or state.get("state_change_seq") != record["seq"]
    ):
        raise ValueError("The agent changed before restart; nothing was sent")
    handoff.verify_screen(record, info, handoff.read_screen(pane))
    key = (pane, record["agent_pid"])
    if key in _exiting:
        raise ValueError("An exit was already attempted for this process; check the pane in Collie")
    _exiting.add(key)
    if record["agent"] == "claude":
        # send-text joins all arguments after the pane ID, including a literal --.
        handoff.herdr("pane", "send-text", pane, "/exit")
        handoff.herdr("agent", "send-keys", pane, "enter")
    else:
        handoff.herdr("agent", "send-keys", pane, "ctrl+d")
    deadline = time.monotonic() + EXIT_WAIT
    while True:
        info = handoff.result("pane", "get", pane)["pane"]
        procs = handoff.result("pane", "process-info", "--pane", pane)["process_info"]
        if (info["terminal_id"], procs["shell_pid"]) != (
            record["terminal_id"],
            record["shell_pid"],
        ):
            raise ValueError("Shell or terminal changed during exit; check the pane in Collie")
        foreground = procs["foreground_processes"]
        if foreground and all(p["pid"] == record["shell_pid"] for p in foreground):
            try:
                os.kill(record["agent_pid"], 0)
            except ProcessLookupError:
                return
        if time.monotonic() >= deadline:
            raise ValueError("Exit unconfirmed; no message sent or exit key repeated; check Collie")
        time.sleep(0.2)
