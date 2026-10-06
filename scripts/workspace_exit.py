"""Exit a scheduled task's agent once it confirms the task is finished.

A scheduled brief asks the agent to end its final response with a marker line. The
scheduler then follows the agent's first turn. Once that turn completes with the
marker, and the TUI shows the same idle, empty-composer state on two consecutive
passes, it presses Ctrl-D the way an automatic handoff does. The pane, its shell and
the worktree stay. Anything else leaves the agent open and stops watching it: a turn
that ends without the marker (usually a question), a follow-up turn, a changed
process. A draft in the composer or a scrolled terminal only delays the exit.
"""

import datetime
import json
import os
import re
import time
import uuid
from pathlib import Path

import claude_accounts
import herdr_handoff as handoff

BRIEF = (
    "\n\nThis task was scheduled to run unattended. When it is finished and nothing more "
    "is needed from the user, end your final response with this line:\n{marker}\n"
    "Leave the line out if you stop to ask a question or cannot finish; the session then "
    "stays open for the user.\n"
)
# A session transcript appears once the agent records its first prompt.
LOCATE_LIMIT = 15 * 60
# An agent still working after this long is left to the user.
WATCH_LIMIT = 7 * 86400
# How long a finished agent must stay idle and unchanged before it is exited.
SETTLE_SECONDS = 20


class Wait(Exception):
    """Not ready yet; check again on the next pass."""


class Leave(Exception):
    """Stop watching and leave the agent open."""


def new_marker():
    return f"[babysit-done:{uuid.uuid4().hex[:16]}]"


def brief(marker):
    return BRIEF.format(marker=marker)


def watching(now=None):
    return {
        "state": "watching",
        "since": time.time() if now is None else now,
        "message": "Waiting for the agent to finish",
    }


def capture(op):
    """The pane, shell and process of the agent this launch started."""
    path = Path(op["path"]).resolve()
    panes = [
        a["pane_id"]
        for a in handoff.result("agent", "list")["agents"]
        if a.get("workspace_id") == op["result"]["workspace_id"]
        and a.get("agent") == op["agent"]
        and Path(a.get("cwd") or "/missing").resolve() == path
    ]
    if len(panes) != 1:
        raise Leave("Could not identify the agent's pane; left open")
    info, procs, _ = handoff.inspect(panes[0])
    matches = [
        p
        for p in procs["foreground_processes"]
        if Path(p.get("argv0", "")).name == op["agent"] and Path(p.get("cwd", "")).resolve() == path
    ]
    if info.get("agent") != op["agent"] or len(matches) != 1:
        raise Leave("Could not identify the agent's process; left open")
    if matches[0]["pid"] == procs["shell_pid"]:
        raise Leave("The agent is the pane's root process; exiting it could close the pane")
    return {
        "agent": op["agent"],
        "claude_account": op.get("claude_account"),
        "cwd": str(path),
        "pane_id": panes[0],
        "terminal_id": info["terminal_id"],
        "shell_pid": procs["shell_pid"],
        "agent_pid": matches[0]["pid"],
        "agent_argv": matches[0]["argv"],
        "marker": op["exit_marker"],
    }


def first_json(path):
    try:
        with path.open() as stream:
            return json.loads(stream.readline())
    except (OSError, ValueError):
        return {}


def claude_cwd(path):
    """The working directory of the first conversation record in a Claude transcript."""
    try:
        with path.open() as stream:
            for line in stream:
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if item.get("type") in {"user", "assistant"} and item.get("cwd"):
                    return Path(item["cwd"]).resolve()
    except OSError:
        pass
    return None


def is_uuid(text):
    try:
        uuid.UUID(text)
    except ValueError:
        return False
    return True


def written_since(path, since):
    try:
        return path.stat().st_mtime >= since
    except OSError:
        return False


def locate(target, since):
    """The one session transcript written in this worktree since the launch.

    A worktree path can be reused once an earlier one is removed, and its old
    transcripts stay behind; only files written since the launch count.
    """
    cwd = Path(target["cwd"])
    found = []
    if target["agent"] == "claude":
        # Claude names a project folder after its cwd, every other character a dash.
        found = [
            (path, path.stem)
            for home in (
                [claude_accounts.account_home(target["claude_account"])]
                if target.get("claude_account")
                else claude_accounts.homes()
            )
            for path in (home / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(cwd))).glob("*.jsonl")
            if is_uuid(path.stem) and written_since(path, since) and claude_cwd(path) == cwd
        ]
    else:
        sessions = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "sessions"
        # Day folders may be local or UTC dates; a day either side covers both.
        day = datetime.date.fromtimestamp(since) - datetime.timedelta(days=1)
        last = datetime.date.today() + datetime.timedelta(days=1)
        while day <= last:
            for path in (sessions / f"{day:%Y/%m/%d}").glob("rollout-*.jsonl"):
                if not written_since(path, since):
                    continue
                meta = first_json(path)
                payload = meta.get("payload") or {}
                if (
                    meta.get("type") == "session_meta"
                    and payload.get("id")
                    and Path(payload.get("cwd") or "/missing").resolve() == cwd
                ):
                    found.append((path, payload["id"]))
            day += datetime.timedelta(days=1)
    if len(found) > 1:
        raise Leave("Several sessions ran in this worktree; left open")
    if not found:
        raise Wait("Waiting for the agent's session transcript")
    path, session_id = found[0]
    return {"rollout": str(path.resolve()), "session_id": session_id}


def first_turn(rollout, agent):
    """The transcript's first prompt turn, which is the scheduled brief."""
    with Path(rollout).open() as stream:
        for line in stream:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = item.get("payload") or {}
            if agent != "claude":
                if item.get("type") == "event_msg" and payload.get("type") == "task_started":
                    return payload.get("turn_id")
                continue
            # The same prompt test as latest_claude_turn uses.
            if item.get("type") != "user" or item.get("isSidechain") or item.get("isMeta"):
                continue
            content = (item.get("message") or {}).get("content")
            blocks = content if isinstance(content, list) else []
            if not any(b.get("type") == "tool_result" for b in blocks):
                return item.get("uuid")
    return None


def confirms(response, marker):
    """Whether a final response ends with the marker line, not merely mentions it."""
    lines = [line.strip().strip("`*").strip() for line in response.splitlines()]
    lines = [line for line in lines if line]  # Also drops the fence around a fenced marker.
    return bool(lines) and lines[-1] == marker


def advance(record, op, persist, now):
    if now - record["since"] > WATCH_LIMIT:
        raise Leave("Still not finished after a week; stopped watching")
    if op is None or op.get("status") in {"failed", "uncertain"}:
        raise Leave("The launch did not finish, so its agent is not watched")
    if op.get("status") != "complete":
        raise Wait("Waiting for the launch to finish")
    if not op.get("exit_marker"):
        raise Leave("This launch did not ask its agent to confirm completion")
    if "target" not in record:
        record["target"] = capture(op)
    target = record["target"]
    if "session_id" not in target:
        try:
            target.update(locate(target, op.get("created_at", record["since"])))
        except Wait:
            # Measured from when the launch finished: a long clone is not the agent's fault.
            if now - op.get("updated_at", record["since"]) > LOCATE_LIMIT:
                raise Leave("Could not find the agent's session transcript; left open") from None
            raise
    info, procs, state = handoff.inspect(target["pane_id"])
    try:
        handoff.verify_identity(target, info, procs)
    except RuntimeError as exc:
        raise Leave(f"The agent exited or changed ({exc}); nothing was sent") from exc
    try:
        turn, complete = handoff.latest_turn(
            target["rollout"], target["session_id"], target["agent"]
        )
    except RuntimeError as exc:
        raise Wait(str(exc)) from exc
    # Only the transcript's first turn is the scheduled task; any later one is the user's.
    if turn != first_turn(target["rollout"], target["agent"]):
        raise Leave("The session moved on to another turn; left open")
    if complete is None:
        raise Wait("The agent is working")
    if not confirms(complete, target["marker"]):
        raise Leave("The agent ended its turn without confirming the task was done; left open")
    try:
        if info.get("agent_status") not in {"idle", "done"}:
            raise Wait("Waiting for the agent to go idle")
        try:
            handoff.verify_screen(target, info, handoff.read_screen(target["pane_id"]))
        except RuntimeError as exc:
            raise Wait(f"Finished, but not exiting yet: {exc}") from exc
    except Wait:
        # Someone is using the pane: the settle restarts once they stop.
        record.pop("ready", None)
        record.pop("ready_at", None)
        raise
    # Exit only once later passes see the same session and transcript for a while. The
    # brief echo keeps the marker on screen, so the screen alone proves no final display.
    seq = state.get("state_change_seq")
    ready = [seq, list(handoff.fingerprint(target["rollout"]))]
    if seq is None or record.get("ready") != ready:
        record.update(ready=ready, ready_at=now)
        raise Wait("Finished; exiting once the agent stays idle")
    if now - record["ready_at"] < SETTLE_SECONDS:
        raise Wait("Finished; exiting once the agent stays idle")
    record.update(state="exiting", message="Exiting the agent")
    # Claims the exit; raises, sending nothing, if another process changed this watch.
    persist(record)
    # Recheck just before the key: anything that moved since the pass began cancels it.
    try:
        info, procs, state = handoff.inspect(target["pane_id"])
        handoff.verify_identity(target, info, procs)
        handoff.verify_screen(target, info, handoff.read_screen(target["pane_id"]))
        unchanged = (
            info.get("agent_status") in {"idle", "done"}
            and state.get("state_change_seq") == seq
            and list(handoff.fingerprint(target["rollout"])) == ready[1]
            and handoff.latest_turn(target["rollout"], target["session_id"], target["agent"])
            == (turn, complete)
        )
    except (OSError, RuntimeError):
        unchanged = False
    if not unchanged:
        del record["ready"], record["ready_at"]
        record.update(state="watching", message="The agent changed just before exiting")
        return
    try:
        handoff.send_exit(
            target, (turn, complete), tuple(ready[1]), lambda: None, lambda *a, **k: None
        )
    except Exception as exc:
        record.update(state="unconfirmed", message=f"Exit unconfirmed ({exc}); check its pane")
        return
    record.update(state="exited", message="The agent confirmed the task was done and exited")


def step(record, op, persist, now=None):
    """Advance one watch by a pass; ``persist`` saves the record before any key is sent."""
    if record.get("state") != "watching":
        return record
    try:
        advance(record, op, persist, time.time() if now is None else now)
    except Wait as exc:
        record["message"] = str(exc)
    except Leave as exc:
        record.update(state="left_open", message=str(exc))
    except Exception as exc:
        # Any surprise before a key is sent leaves the agent alone.
        record.update(state="left_open", message=f"Stopped watching: {exc}")
    return record
