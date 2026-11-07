"""The Agents tab's roster: every machine's herdr agents, cached, with who started each."""

import json
import threading
import time

import agent_roster
import dashboard
import pytest
from test_machine_loads import MINI, register
from test_workspace_overview import request

BOX = {"id": "box", "label": "Linux box", "ssh": "box", "os": "linux"}


def settle(roster):
    for _ in range(400):
        with roster.lock:
            if not roster.refreshing:
                return
        time.sleep(0.05)
    pytest.fail("the roster never settled")


def listing(roster):
    roster.snapshot()
    settle(roster)
    return roster.snapshot()


def agent(pane, status, session=None, kind="claude", **fields):
    return {
        "pane_id": pane,
        "workspace_id": pane.split(":")[0],
        "agent": kind,
        "agent_status": status,
        "agent_session": {"value": session} if session else None,
        "terminal_title_stripped": f"{kind} in {pane}",
        **fields,
    }


def space(workspace_id, checkout, label="label"):
    return {
        "workspace_id": workspace_id,
        "label": label,
        "agent_status": "idle",
        "worktree": {"checkout_path": checkout, "repo_name": "repo", "repo_root": "/src/repo"},
    }


@pytest.fixture
def herdr_state(tmp_path, fake_tools, monkeypatch):
    register(tmp_path, MINI, BOX)
    data = json.loads(fake_tools.read_text())
    data["workspaces"] = [space("w1", "/src/worktrees/repo/feature"), space("w2", "/src/x")]
    data["agents"] = [
        agent("w1:p1", "working", "s-mine"),
        agent("w1:p2", "blocked", "s-watched", kind="codex"),
        agent("w2:p4", "idle", "s-cron"),
        {"pane_id": "bad pane", "workspace_id": "w1"},
    ]
    data["machines"] = {
        "Mac mini": {
            "workspaces": [space("w1", "/Users/me/src/worktrees/repo/remote")],
            "agents": [agent("w1:p1", "done", "s-remote")],
            "calls": [],
        }
    }
    fake_tools.write_text(json.dumps(data))
    monkeypatch.setenv("FAKE_HERDR_DOWN", "Linux box")
    return fake_tools


JOBS = [
    {
        "id": "w-7",
        "session_id": "s-watched",
        "status": "watching",
        "url": "https://github.com/o/r/pull/7",
    },
    {
        "id": "w-8",
        "session_id": "s-mine",
        "status": "closed",
        "url": "https://github.com/o/r/pull/8",
    },
]
CRON = {
    "jobs": [
        {
            "name": "nightly sweep",
            "runs": [{"status": "running", "agent_run": {"machine": None, "pane": "w2:p4"}}],
        }
    ]
}


def test_lists_every_machines_agents_with_origins_and_isolates_a_failing_worker(
    tmp_path, herdr_state
):
    roster = agent_roster.Roster(tmp_path, jobs=lambda: JOBS, cron=lambda: CRON)
    first = roster.snapshot()
    # The answer never waits for herdr: nothing is known yet.
    assert first["agents"] == []
    assert [m["pending"] for m in first["machines"]] == [True, True, True]
    settle(roster)
    value = roster.snapshot()
    by_id = {a["id"]: a for a in value["agents"]}
    assert set(by_id) == {"w1:p1", "w1:p2", "w2:p4", "mini/w1:p1"}
    mine = by_id["w1:p1"]
    assert mine["origin"] == "you" and mine["status"] == "working"
    assert mine["workspace"] == "w1" and mine["machine"] is None
    assert mine["name"] == "feature" and mine["repo"] == "repo"
    assert mine["session"] == "s-mine" and mine["since"] is not None
    # A watch owning the session started it; an ended watch no longer counts.
    assert by_id["w1:p2"]["origin"] == "watch"
    assert by_id["w1:p2"]["origin_url"] == "https://github.com/o/r/pull/7"
    assert by_id["w2:p4"]["origin"] == "cron"
    assert by_id["w2:p4"]["origin_label"] == "Cron: nightly sweep"
    remote = by_id["mini/w1:p1"]
    assert remote["workspace"] == "mini/w1" and remote["machine_label"] == "Mac mini"
    assert remote["name"] == "remote" and remote["status"] == "done"
    machines = {m["id"]: m for m in value["machines"]}
    assert machines[None]["error"] is None and machines["mini"]["error"] is None
    assert "cannot reach Linux box" in machines["box"]["error"]


def test_a_cached_listing_is_reused_until_it_expires(tmp_path, herdr_state):
    now = [1000.0]
    roster = agent_roster.Roster(tmp_path, clock=lambda: now[0])
    listing(roster)
    asked = len(json.loads(herdr_state.read_text())["calls"])
    roster.snapshot()
    settle(roster)
    assert len(json.loads(herdr_state.read_text())["calls"]) == asked
    now[0] += agent_roster.LOCAL_TTL
    listing(roster)
    # This machine's agents and workspaces again; the workers' live longer.
    assert len(json.loads(herdr_state.read_text())["calls"]) == asked + 2


def test_since_follows_status_and_session_changes(tmp_path, herdr_state):
    now = [1000.0]
    roster = agent_roster.Roster(tmp_path, clock=lambda: now[0])
    listing(roster)
    data = json.loads(herdr_state.read_text())
    data["agents"][0]["agent_status"] = "idle"
    herdr_state.write_text(json.dumps(data))
    now[0] = 2000.0
    value = {a["id"]: a for a in listing(roster)["agents"]}
    assert value["w1:p1"]["since"] == 2000.0
    assert value["w1:p2"]["since"] == 1000.0


def test_a_failed_listing_keeps_the_last_agents_with_its_error(tmp_path, herdr_state, monkeypatch):
    now = [1000.0]
    roster = agent_roster.Roster(tmp_path, clock=lambda: now[0])
    listing(roster)
    monkeypatch.setenv("FAKE_HERDR_DOWN", "Linux box,Mac mini")
    now[0] += agent_roster.WORKER_TTL
    value = listing(roster)
    assert "mini/w1:p1" in {a["id"] for a in value["agents"]}
    mini = next(m for m in value["machines"] if m["id"] == "mini")
    assert "cannot reach Mac mini" in mini["error"] and mini["synced_at"] == 1000.0


def test_workspace_links_come_from_the_inventory_without_scanning(tmp_path, herdr_state):
    class Overview:
        def snapshot(self, start=True):
            assert start is False
            return {
                "workspaces": [
                    {
                        "workspace_ids": ["w1"],
                        "branch": "feature",
                        "links": [{"label": "PR #7", "url": "https://github.com/o/r/pull/7"}],
                    }
                ]
            }

    roster = agent_roster.Roster(tmp_path, overview=Overview())
    value = {a["id"]: a for a in listing(roster)["agents"]}
    assert value["w1:p1"]["branch"] == "feature"
    assert value["w1:p1"]["links"][0]["label"] == "PR #7"
    assert value["w2:p4"]["links"] == []


def test_the_dashboard_serves_the_roster(tmp_path, herdr_state):
    with dashboard.DashboardServer(tmp_path, 0) as httpd:
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            status, value = request(httpd.server_port, "/api/agents")
            assert status == 200 and {"agents", "machines", "now"} <= set(value)
            settle(httpd.roster)
            status, value = request(httpd.server_port, "/api/agents")
            assert "w1:p1" in {a["id"] for a in value["agents"]}
        finally:
            httpd.shutdown()
            thread.join(5)
