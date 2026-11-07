"""A workspace pane's terminal from the viewer: its screen, keys, text and shell commands."""

import json
import threading

import agent_terminal
import dashboard
import pytest
from test_workspace_overview import calls, read, request
from test_workspace_overview import site as site  # noqa: F401 (fixture)

SCREEN = "  \x1b[1m> \x1b[0mhello"


@pytest.fixture
def panes(site):  # noqa: F811
    """Workspace w1 with its agent's pane and a plain shell pane."""
    state = site[4]
    data = read(state)
    data["panes"] = [
        {"pane_id": "w1:p1", "workspace_id": "w1"},
        {"pane_id": "w1:p2", "workspace_id": "w1", "label": "notes"},
        {"pane_id": "w2:p1", "workspace_id": "w2"},
    ]
    data["screens"] = {"w1:p1": [SCREEN], "w1:p2": ["$ "]}
    state.write_text(json.dumps(data))
    return site


def test_the_screen_defaults_to_the_agents_pane_and_lists_the_others(panes):
    value = agent_terminal.screen("w1")
    assert value["pane"] == "w1:p1" and value["screen"] == SCREEN
    assert value["panes"] == [
        {"pane": "w1:p1", "label": None, "agent": "codex", "status": "idle", "session": None},
        {"pane": "w1:p2", "label": "notes", "agent": None, "status": None, "session": None},
    ]
    assert calls(panes[4], ["pane", "read"]) == [
        ["pane", "read", "w1:p1", "--source", "visible", "--format", "ansi"]
    ]
    assert agent_terminal.screen("w1", "w1:p2")["screen"] == "$ "
    # Another workspace's pane is never read through this one.
    with pytest.raises(ValueError, match="no longer in this workspace"):
        agent_terminal.screen("w1", "w2:p1")


def test_keys_and_text_go_to_the_named_pane(panes):
    state = panes[4]
    agent_terminal.send({"workspace": "w1", "pane": "w1:p2", "keys": ["ctrl+c", "up", "shift+tab"]})
    agent_terminal.send({"workspace": "w1", "pane": "w1:p2", "text": "ls -la", "enter": True})
    agent_terminal.send({"workspace": "w1", "pane": "w1:p1", "text": "", "enter": True})
    assert read(state)["pane_keys"] == [
        ["w1:p2", "ctrl+c", "up", "shift+tab"],
        ["w1:p2", "enter"],
        ["w1:p1", "enter"],
    ]
    assert read(state)["typed_text"] == [["w1:p2", "ls -la"]]


@pytest.mark.parametrize(
    "request_,error",
    [
        ({"workspace": "w1", "pane": "w1:p1", "keys": ["rm -rf"]}, "Unknown key"),
        ({"workspace": "w1", "pane": "w1:p1", "keys": []}, "Unknown key"),
        ({"workspace": "w1", "pane": "w1:p1", "text": "a\x1b[201~b", "enter": False}, "control"),
        ({"workspace": "w1", "pane": "w1:p1", "text": "", "enter": False}, "Invalid text"),
        ({"workspace": "w1", "pane": "w2:p1", "keys": ["esc"]}, "no longer in this workspace"),
        ({"workspace": "w1", "pane": "w1:p1"}, "Send keys"),
    ],
)
def test_refused_input_types_nothing(panes, request_, error):
    with pytest.raises(ValueError, match=error):
        agent_terminal.send(request_)
    data = read(panes[4])
    assert "pane_keys" not in data and "typed_text" not in data


def test_a_shell_command_runs_in_a_new_labelled_split_of_the_checkout(panes):
    _, _, _, worktrees, state, _ = panes
    value = agent_terminal.shell({"workspace": "w1", "command": "echo ran > ran.txt"})
    pane = value["pane"]
    assert read(state)["splits"] == [
        ["w1:p1", "--direction", "down", "--cwd", str(worktrees / "merged-work"), "--no-focus"]
    ]
    assert read(state)["runs"] == [[pane, "echo ran > ran.txt"]]
    assert next(p for p in read(state)["panes"] if p["pane_id"] == pane)["label"] == "babysit shell"
    assert (worktrees / "merged-work" / "ran.txt").read_text() == "ran\n"
    with pytest.raises(ValueError, match="control character"):
        agent_terminal.shell({"workspace": "w1", "command": "echo\x15 hi"})


def test_the_dashboard_relays_the_terminal(panes, tmp_path):
    state = panes[4]
    with dashboard.DashboardServer(tmp_path / "plain", 0) as httpd:
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            port = httpd.server_port
            status, value = request(port, "/api/workspace-terminal?workspace=w1&pane=w1:p2")
            assert status == 200 and value["pane"] == "w1:p2", value
            status, value = request(port, "/api/workspace-terminal?workspace=w1&other=1")
            assert status == 400
            body = {"workspace": "w1", "pane": "w1:p1", "keys": ["esc"]}
            status, value = request(port, "/api/workspace-keys", body, action="workspace-keys")
            assert status == 200 and value["sent"], value
            # Each action needs its own header.
            status, _ = request(port, "/api/workspace-keys", body, action="workspace-shell")
            assert status == 404
            body = {"workspace": "w1", "command": "true"}
            status, value = request(port, "/api/workspace-shell", body, action="workspace-shell")
            assert status == 200 and value["pane"], value
            assert read(state)["pane_keys"] == [["w1:p1", "esc"]]
        finally:
            httpd.shutdown()
            thread.join(5)
