"""``wt --exit-when-done`` watches followed by the dashboard, against fake herdr and agents."""

import json
import os
import subprocess
import sys
import time
import uuid

import exit_watch
import pr_workspaces as pw
import pytest
import workspace_exit
from test_pr_workspaces import local  # noqa: F401 (fixture)


def herdr_state(state):
    return json.loads(state.read_text())


def change(state, **values):
    data = herdr_state(state)
    data.update(values)
    state.write_text(json.dumps(data))


def watch_row(manager, key):
    with manager.db() as db:
        (data,) = db.execute("SELECT data FROM exit_watches WHERE id=?", (key,)).fetchone()
    return json.loads(data)


def register(manager, path, created_at=None, **values):
    now = time.time() if created_at is None else created_at
    watch = {
        "id": uuid.uuid4().hex,
        "label": "review-pr-7",
        "path": str(path),
        "workspace_id": "w1",
        "pane_id": "w1:p1",
        "agent": "codex",
        "claude_account": None,
        "marker": exit_watch.new_marker(),
        "created_at": now,
        "exit": exit_watch.watching(now),
        **values,
    }
    exit_watch.register(manager.home, watch)
    return watch


def test_a_wt_launch_is_exited_and_its_workspace_closed(local, monkeypatch):  # noqa: F811
    manager, _, _, state, _ = local
    brief = manager.home.parent / "brief.md"
    brief.write_text("Review the PR; write the report.")
    subprocess.run(
        [sys.executable, pw.WORKTREE_HELPER, "wtpr", "-r", "repo", "--codex"]
        + ["--name", "review-pr-7", "-F", str(brief), "--exit-when-done", "7"],
        env={**os.environ, "WT_MULTIPLEXER": "herdr", "BABYSIT_HOME": str(manager.home)},
        check=True,
        capture_output=True,
    )
    [launched] = manager.scheduled_tasks()["launched"]
    assert launched["state"] == "watching" and launched["label"] == "review-pr-7"
    path = manager.src / "worktrees/repo/review-pr-7"
    assert launched["path"] == str(path.resolve())
    assert launched["url"].endswith("/space/w1")
    # The agent wt started, as herdr reports it; the pane holds nothing else.
    [agent] = herdr_state(state)["agents"]
    procs = {"shell_pid": 100, "foreground_processes": []}
    procs["foreground_processes"].append(
        {"pid": 200, "argv0": "codex", "argv": ["codex"], "cwd": agent["cwd"]}
    )
    change(
        state,
        process_info={agent["pane_id"]: procs},
        panes=[{"pane_id": agent["pane_id"], "workspace_id": agent["workspace_id"]}],
    )
    seen = []

    def step(record, op, persist, now=None):
        # What wt recorded is enough to find the agent's pane and process.
        record["target"] = workspace_exit.capture(op)
        seen.append(op)
        persist({**record, "state": "exiting"})
        # Ctrl-D: only the shell is left, in the foreground of its pane.
        shell = {"shell_pid": 100, "foreground_processes": [{"pid": 100, "argv0": "zsh"}]}
        change(state, agents=[], process_info={agent["pane_id"]: shell})
        return {**record, "state": "exited", "message": "Exited"}

    monkeypatch.setattr(pw.workspace_exit, "step", step)
    manager.exit_launched()
    [op] = seen
    assert op["status"] == "complete" and op["agent"] == "codex"
    assert op["exit_marker"] in agent["task"]
    row = watch_row(manager, launched["id"])
    assert row["exit"]["state"] == "exited"
    assert row["exit"]["message"] == "Exited; its workspace was closed"
    assert row["exit"]["target"]["pane_id"] == "w1:p1"
    calls = herdr_state(state)["calls"]
    assert ["workspace", "close", "w1"] in calls
    assert not any(c[:2] in (["pane", "close"], ["worktree", "remove"]) for c in calls)
    # The checkout and its branch stay for a later resume.
    assert (path / ".git").exists()
    manager.exit_launched()  # A finished watch is never stepped again.
    assert len(seen) == 1


def test_a_watch_waits_for_its_agent_to_show_up(local, monkeypatch):  # noqa: F811
    manager, _, _, state, _ = local
    path = manager.src / "repo"
    watch = register(manager, path, created_at=1000)
    statuses = []

    def step(record, op, persist, now=None):
        statuses.append(op["status"])
        return record

    monkeypatch.setattr(pw.workspace_exit, "step", step)
    manager.exit_launched(now=1010)
    agent = {"workspace_id": "w1", "agent": "codex", "cwd": str(path), "pane_id": "w1:p1"}
    change(state, agents=[{**agent, "workspace_id": "w2"}, {**agent, "agent": "claude"}])
    manager.exit_launched(now=1020)
    change(state, agents=[agent])
    manager.exit_launched(now=1030)
    change(state, agents=[])
    manager.exit_launched(now=1000 + pw.LAUNCH_START_SECONDS + 1)
    # Once the watch has its target, herdr's agent list is not asked again.
    with manager.db() as db:
        row = {**watch, "exit": {**watch["exit"], "target": {"pane_id": "w1:p1"}}}
        db.execute("UPDATE exit_watches SET data=? WHERE id=?", (json.dumps(row), watch["id"]))
    asked = len(herdr_state(state)["calls"])
    manager.exit_launched(now=1040)
    assert len(herdr_state(state)["calls"]) == asked
    assert statuses == ["running", "running", "complete", "complete", "complete"]


SHELL = [{"pid": 100, "argv0": "zsh"}]


@pytest.mark.parametrize(
    ("panes", "shell", "procs", "closed", "message"),
    [
        (["w1:p1"], 100, SHELL, ["workspace", "close", "w1"], "; its workspace was closed"),
        (["w1:p1", "w1:p2"], 100, [], ["pane", "close", "w1:p1"], "; its pane was closed"),
        (["w1:p1"], 100, [*SHELL, {"pid": 300, "argv0": "vim"}], None, "; its pane is still open"),
        # The pane id now names another terminal: its shell is not the agent's.
        (["w1:p1"], 400, [{"pid": 400, "argv0": "zsh"}], None, "; its pane is still open"),
    ],
)
def test_only_the_agents_emptied_pane_is_closed(
    local,  # noqa: F811
    panes,
    shell,
    procs,
    closed,
    message,
):
    manager, _, _, state, _ = local
    change(
        state,
        panes=[{"pane_id": p, "workspace_id": "w1"} for p in panes],
        process_info={"w1:p1": {"shell_pid": shell, "foreground_processes": procs}},
    )
    watch = register(manager, manager.src / "repo")
    target = {"pane_id": "w1:p1", "shell_pid": 100}
    assert pw.close_launched(watch, {"target": target}) == message
    # A pane other than the one wt typed into is never closed.
    other = {"target": {**target, "pane_id": "w1:p2"}}
    assert pw.close_launched(watch, other) == "; its pane is still open"
    calls = herdr_state(state)["calls"]
    assert [c for c in calls if c[1:2] == ["close"]] == ([closed] if closed else [])


def test_nothing_is_closed_unless_the_exit_is_confirmed(local, monkeypatch):  # noqa: F811
    manager, _, _, state, _ = local
    change(state, panes=[{"pane_id": "w1:p1", "workspace_id": "w1"}])
    unconfirmed = register(manager, manager.src / "repo")
    failing = register(manager, manager.src / "repo")

    def by_watch(record, op, persist, now=None):
        if op["exit_marker"] == unconfirmed["marker"]:
            return {**record, "state": "unconfirmed", "message": "Exit unconfirmed"}
        return {**record, "state": "exited", "message": "Exited", "target": {"pane_id": "w1:p1"}}

    def broken(watch, record):
        raise TypeError("odd herdr reply")

    monkeypatch.setattr(pw.workspace_exit, "step", by_watch)
    monkeypatch.setattr(pw, "close_launched", broken)
    manager.exit_launched(now=time.time() + pw.LAUNCH_START_SECONDS + 1)
    assert watch_row(manager, unconfirmed["id"])["exit"]["message"] == "Exit unconfirmed"
    # A surprise while tidying up still records the exit, so the watch ends.
    after = watch_row(manager, failing["id"])["exit"]
    assert after["state"] == "exited"
    assert after["message"] == "Exited; its pane is still open (odd herdr reply)"
    assert not any(c[1:2] == ["close"] for c in herdr_state(state)["calls"])


def test_the_real_watch_finds_the_agent_or_leaves_it(local):  # noqa: F811
    manager, _, _, state, _ = local
    path = (manager.src / "repo").resolve()
    start = time.time()
    missing = register(manager, path, created_at=start - pw.LAUNCH_START_SECONDS - 1)
    found = register(manager, path, created_at=start, pane_id="w1:p1")
    agent = {"workspace_id": "w1", "agent": "codex", "cwd": str(path), "pane_id": "w1:p1"}
    procs = {"shell_pid": 100, "foreground_processes": []}
    procs["foreground_processes"].append(
        {"pid": 200, "argv0": "codex", "argv": ["codex"], "cwd": str(path)}
    )
    # The first watch's agent never showed up; the second one's is in its pane.
    change(state, agents=[agent], process_info={"w1:p1": procs})
    with manager.db() as db:
        row = {**missing, "workspace_id": "w9", "pane_id": "w9:p1"}
        db.execute("UPDATE exit_watches SET data=? WHERE id=?", (json.dumps(row), missing["id"]))
    manager.exit_launched(now=start + 5)
    left = watch_row(manager, missing["id"])["exit"]
    assert left["state"] == "left_open"
    assert left["message"] == "Could not identify the agent's pane; left open"
    watching = watch_row(manager, found["id"])["exit"]
    assert watching["state"] == "watching"
    assert watching["message"] == "Waiting for the agent's session transcript"
    assert watching["target"]["pane_id"] == "w1:p1" and watching["target"]["agent_pid"] == 200
    assert watching["target"]["marker"] == found["marker"]


def test_a_claim_fails_when_another_dashboard_changed_the_watch(local, monkeypatch):  # noqa: F811
    manager, _, _, state, _ = local
    watch = register(manager, manager.src / "repo")
    claims = []

    def step(record, op, persist, now=None):
        row = watch_row(manager, watch["id"])
        row["exit"]["message"] = "Other dashboard"
        with manager.db() as db:
            db.execute("UPDATE exit_watches SET data=? WHERE id=?", (json.dumps(row), watch["id"]))
        try:
            persist({**record, "state": "exiting"})
        except pw.workspace_exit.Leave:
            claims.append("refused")
            raise
        return record

    monkeypatch.setattr(pw.workspace_exit, "step", step)
    manager.exit_launched(now=watch["created_at"] + pw.LAUNCH_START_SECONDS + 1)
    assert claims == ["refused"]
    assert watch_row(manager, watch["id"])["exit"]["message"] == "Other dashboard"


def test_a_restart_never_repeats_an_exit_it_may_have_begun(local, monkeypatch):  # noqa: F811
    manager, _, _, state, jobs = local
    watch = register(manager, manager.src / "repo")

    def step(record, op, persist, now=None):
        persist({**record, "state": "exiting"})
        raise SystemExit  # The dashboard stops once "exiting" is durable.

    monkeypatch.setattr(pw.workspace_exit, "step", step)
    with pytest.raises(SystemExit):
        manager.exit_launch(watch, now=watch["created_at"] + pw.LAUNCH_START_SECONDS + 1)
    restarted = pw.Workspaces(
        manager.home, manager.overview, lambda: jobs, manager.src, issues=manager.issues
    )
    [listed] = restarted.scheduled_tasks()["launched"]
    assert listed["state"] == "unconfirmed" and "check its pane" in listed["message"]


def test_finished_watches_are_pruned_and_watching_ones_kept(local, monkeypatch):  # noqa: F811
    manager, _, _, state, _ = local
    monkeypatch.setattr(pw, "LAUNCH_HISTORY", 2)
    now = time.time()
    old = now - pw.HISTORY_SECONDS - 1
    path = manager.src / "repo"
    kept = [register(manager, path, created_at=old)["id"]]  # Still watching.
    done = {"state": "exited", "message": "Exited"}
    register(manager, path, created_at=old, exit=done)
    register(manager, path, created_at=now - 30, exit=done)
    kept += [register(manager, path, created_at=now - t, exit=done)["id"] for t in (20, 10)]
    manager.prune_history(now=now)
    assert sorted(w["id"] for w in manager.launched_exits()) == sorted(kept)
    # The month limit applies on its own, within the count.
    monkeypatch.setattr(pw, "LAUNCH_HISTORY", 100)
    register(manager, path, created_at=old, exit=done)
    manager.prune_history(now=now + pw.HISTORY_PRUNE_SECONDS)
    assert sorted(w["id"] for w in manager.launched_exits()) == sorted(kept)


def test_an_unreadable_watch_hides_no_others(local):  # noqa: F811
    manager, _, _, _, _ = local
    good = register(manager, manager.src / "repo")
    with manager.db() as db:
        db.execute("INSERT INTO exit_watches VALUES ('odd', ?)", (json.dumps({"id": "odd"}),))
    assert [w["id"] for w in manager.scheduled_tasks()["launched"]] == [good["id"]]


def test_a_message_s_watch_follows_its_running_agent_at_once(local, monkeypatch):  # noqa: F811
    manager, _, _, state, _ = local
    path = manager.src / "repo"
    # No agent listed yet would keep a launch "running"; a message's agent already runs.
    watch = register(manager, path, created_at=1000, follow_up=True, session_id="s1")
    plain = register(manager, path, created_at=1000)
    seen = {}

    def step(record, op, persist, now=None):
        seen[op["created_at"], op.get("follow_up")] = op
        return record

    monkeypatch.setattr(pw.workspace_exit, "step", step)
    manager.exit_launched(now=1010)
    message, launch = seen[1000, True], seen[1000, None]
    assert message["status"] == "complete"
    assert (message["pane_id"], message["session_id"]) == (watch["pane_id"], "s1")
    # A wt launch is unchanged: it waits for its agent, and capture searches its workspace.
    assert launch["status"] == "running" and "pane_id" not in launch
    assert plain["id"] != watch["id"]
