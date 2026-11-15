"""herdr-core as the terminal runtime: the ``hc`` client, herdr's commands answered from
it (``hc_panes``), and dashboard launches that end up in hc terminals.

Everything runs against the fake ``hc`` in ``conftest``; no real core is touched.
"""

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import agent_messages
import agent_terminal
import exit_watch
import hc
import hc_panes
import herdr_handoff
import machines
import pane_runner
import pr_workspaces as pw
import pytest
import workspace_exit
from test_pr_workspaces import finish, local  # noqa: F401 (fixture)

SESSION = "0b6f3c1e-2d4a-4e5f-9a7b-8c9d0e1f2a3b"
# The reader of this machine's process table, before the fixtures put an empty one in.
REAL_PROCESSES = hc_panes.processes


def store(state):
    return json.loads(state.read_text())["hc"]


def change(state, edit):
    data = json.loads(state.read_text())
    edit(data.setdefault("hc", {"terminals": [], "machines": [], "calls": []}))
    state.write_text(json.dumps(data))


def terminal(state, address="t1"):
    return next(t for t in store(state)["terminals"] if t["address"] == address)


def report(path, address, **words):
    """What the terminal's program reports about itself, as hc keeps it."""

    def edit(data):
        found = next(t for t in data["terminals"] if t["address"] == address)
        found["status"] = words or None
        found["status_ms"] = (found.get("status_ms") or 0) + 1

    change(path, edit)


def opened(path, clone="/clone", title="pr-repo-feature", machine=None):
    return hc_panes.run(
        ["worktree", "open", "--cwd", clone, "--path", str(path), "--label", title, "--no-focus"],
        machine,
    )


# --- the client -------------------------------------------------------------------------


def test_refusals_carry_hcs_kind_and_a_missing_hc_is_unreachable(fake_tools, monkeypatch):
    with pytest.raises(hc.Error) as refused:
        hc.get("t9")
    assert refused.value.kind == "not_found" and not isinstance(refused.value, hc.Unreachable)
    with pytest.raises(hc.Error, match="not a terminal address"):
        hc.get("../t1")
    monkeypatch.setenv("BABYSIT_HC", str(fake_tools.parent / "missing"))
    with pytest.raises(hc.Unreachable, match="not installed"):
        hc.terminals()


def test_each_machines_terminals_are_listed_as_that_machine_names_them(fake_tools, monkeypatch):
    change(fake_tools, lambda data: data["machines"].append({"name": "Box"}))
    here = hc.start("/src/a", name="a")
    there = hc.start("/src/b", machine="Box", labels={"k": "v"})
    assert (here["address"], there["address"]) == ("t1", "t2")
    assert [t["address"] for t in store(fake_tools)["terminals"]] == ["t1", "Box/t2"]
    assert [t["address"] for t in hc.terminals()] == ["t1"]
    assert [(t["address"], t["labels"]) for t in hc.terminals("Box")] == [("t2", {"k": "v"})]
    assert hc.get("t2", "Box")["cwd"] == "/src/b"
    # Kept shells with no parent: they outlive both their shell and whoever asked.
    runs = [q for method, q in store(fake_tools)["calls"] if method == "terminal.run"]
    assert all(q["keep"] and q["no_parent"] and "argv" not in q for q in runs)
    assert runs[1]["on"] == "Box"
    # A worker whose line is down is unknown, never a worker without terminals.
    monkeypatch.setenv("FAKE_HC_DOWN", "Box")
    with pytest.raises(hc.Unreachable, match="line to Box is reconnecting"):
        hc.terminals("Box")
    assert [t["address"] for t in hc.terminals()] == ["t1"]
    with pytest.raises(hc.Unreachable, match="hc machine add <ssh target> --name Mini"):
        hc.terminals("Mini")


def test_closing_ends_and_forgets_a_terminal_and_tolerates_one_already_gone(fake_tools):
    hc.start("/src/a")
    hc.close("t1")
    assert store(fake_tools)["terminals"] == []
    hc.close("t1")


@pytest.mark.parametrize(
    "text,taken,expected",
    [
        ("PR galaxy/Foo #12", set(), "pr-galaxy-foo-12"),
        ("review", {"review"}, "review-2"),
        ("x" * 40, {"x" * 32}, "x" * 30 + "-2"),
        ("///", set(), None),
    ],
)
def test_terminal_names_fit_hcs_rules_and_stay_unique(text, taken, expected):
    assert hc.terminal_name(text, taken) == expected


# --- herdr's commands, answered by hc ---------------------------------------------------


def test_a_checkout_gets_one_shell_terminal_and_is_one_workspace(fake_tools, tmp_path):
    first = opened(tmp_path)
    wid = hc_panes.workspace_id(str(tmp_path))
    assert first == {
        "already_open": False,
        "root_pane": {"pane_id": "t1"},
        "pane": {"pane_id": "t1"},
        "workspace": {"workspace_id": wid},
    }
    assert machines.WORKSPACE.fullmatch(wid) and pw.PANE.fullmatch("t1")
    record = terminal(fake_tools)
    assert record["name"] == "pr-repo-feature" and record["keep"]
    assert record["labels"] == {
        hc_panes.CHECKOUT: str(tmp_path),
        hc_panes.CLONE: "/clone",
        hc_panes.TITLE: "pr-repo-feature",
    }
    # Opening it again starts nothing: the checkout keeps its terminal and its id.
    again = opened(tmp_path)
    assert again["already_open"] and again["root_pane"] == {"pane_id": "t1"}
    assert len(store(fake_tools)["terminals"]) == 1
    assert hc_panes.run(["workspace", "list"], None) == {
        "workspaces": [
            {
                "workspace_id": wid,
                "label": "pr-repo-feature",
                "agent_status": "unknown",
                "pane_count": 1,
                "focused": False,
                "worktree": {
                    "checkout_path": str(tmp_path),
                    "repo_root": "/clone",
                    "repo_name": "clone",
                },
            }
        ]
    }
    # A shell at its prompt is a pane, not an agent.
    assert hc_panes.run(["agent", "list"], None) == {"agents": []}
    [pane] = hc_panes.run(["pane", "list", "--workspace", wid], None)["panes"]
    assert (pane["pane_id"], pane["terminal_id"], pane["workspace_id"]) == ("t1", "t1", wid)
    assert "agent" not in pane


def test_a_terminal_started_by_hand_is_a_workspace_of_its_folder(fake_tools, tmp_path):
    hc.start(str(tmp_path), name="scratch")
    [space] = hc_panes.run(["workspace", "list"], None)["workspaces"]
    assert space["label"] == tmp_path.name
    assert space["worktree"] == {
        "checkout_path": str(tmp_path),
        "repo_root": str(tmp_path),
        "repo_name": tmp_path.name,
    }


def test_an_agent_typed_into_the_terminal_reports_its_own_report(fake_tools, tmp_path):
    opened(tmp_path)
    assert hc_panes.run(["pane", "run", "t1", "codex 'Fix it'"], None) is None
    [agent] = hc_panes.run(["agent", "list"], None)["agents"]
    assert agent["pane_id"] == "t1" and agent["agent"] == "codex"
    assert agent["agent_status"] == "working" and agent["cwd"] == str(tmp_path)
    assert agent["workspace_id"] == hc_panes.workspace_id(str(tmp_path))
    assert terminal(fake_tools)["task"] == "Fix it"
    # The line was typed whatever ran there, as herdr's pane run is.
    assert store(fake_tools)["sent"] == [["t1", "codex 'Fix it'", True]]
    info = hc_panes.run(["pane", "get", "t1"], None)["pane"]
    assert (info["agent"], info["agent_status"], info["terminal_id"]) == ("codex", "working", "t1")
    assert info["scroll"]["offset_from_bottom"] == 0
    for words, expected in [
        ({"state": "idle"}, "idle"),
        ({"state": "done"}, "done"),
        ({"state": "blocked", "kind": "permission"}, "blocked"),
        ({"state": "error"}, "blocked"),
        ({"state": "compacting"}, "unknown"),
    ]:
        report(fake_tools, "t1", app="codex", **words)
        [agent] = hc_panes.run(["agent", "list"], None)["agents"]
        assert agent["agent_status"] == expected
        [space] = hc_panes.run(["workspace", "list"], None)["workspaces"]
        assert space["agent_status"] == expected
    # Its change counter moves with every report, which is all it is compared for.
    before = hc_panes.run(["agent", "get", "t1"], None)["agent"]["state_change_seq"]
    report(fake_tools, "t1", app="codex", state="idle")
    assert hc_panes.run(["agent", "get", "t1"], None)["agent"]["state_change_seq"] != before


def test_a_finished_turn_is_done_until_a_herdr_client_marks_it_seen(fake_tools, tmp_path):
    opened(tmp_path)
    hc_panes.run(["pane", "run", "t1", "claude"], None)
    report(fake_tools, "t1", app="claude-code", state="idle")

    def turn(number, seen):
        def edit(data):
            data["terminals"][0]["turn"] = number
            data["terminals"][0]["labels"][hc_panes.SEEN] = seen

        change(fake_tools, edit)
        return hc_panes.run(["agent", "list"], None)["agents"][0]["agent_status"]

    assert turn(3, "2") == "done"
    assert turn(3, "3") == "idle"


def test_an_agent_that_returned_to_the_shell_is_no_longer_listed(fake_tools, tmp_path):
    opened(tmp_path)
    hc_panes.run(["pane", "run", "t1", "codex"], None)
    hc_panes.run(["agent", "send-keys", "t1", "ctrl+d"], None)
    assert store(fake_tools)["keys"] == [["t1", ["ctrl+d"]]]
    assert hc_panes.run(["agent", "list"], None) == {"agents": []}
    info = hc_panes.run(["pane", "process-info", "--pane", "t1"], None)["process_info"]
    assert [p["pid"] for p in info["foreground_processes"]] == [info["shell_pid"]]


def test_a_message_goes_only_to_an_agent_that_can_take_it(fake_tools, tmp_path, monkeypatch):
    opened(tmp_path)
    with pytest.raises(hc_panes.Refused) as shell:
        hc_panes.run(["agent", "prompt", "t1", "hello", "--wait", "--timeout", "8000"], None)
    assert shell.value.code == "agent_not_running"
    hc_panes.run(["pane", "run", "t1", "claude"], None)
    assert hc_panes.run(["agent", "prompt", "t1", "hello"], None) == {}
    # hc may refuse it: a message never answers a dialog.
    assert store(fake_tools)["sent"][-1] == ["t1", "hello", False]
    report(fake_tools, "t1", app="claude-code", state="blocked", kind="question")
    with pytest.raises(hc_panes.Refused) as blocked:
        hc_panes.run(["agent", "prompt", "t1", "again"], None)
    assert blocked.value.code == "agent_blocked"
    report(fake_tools, "t1", app="claude-code", state="idle")
    monkeypatch.setenv("FAKE_HC_STARTED", "false")
    with pytest.raises(hc_panes.Refused) as stalled:
        hc_panes.run(["agent", "prompt", "t1", "anyone?"], None)
    assert stalled.value.code == "agent_prompt_stalled"


def test_unsubmitted_text_is_pressed_as_keys_or_refused(fake_tools, tmp_path):
    opened(tmp_path)
    hc_panes.run(["pane", "send-text", "t1", "/exit Ab"], None)
    assert store(fake_tools)["keys"] == [["t1", ["/", "e", "x", "i", "t", "space", "shift+a", "b"]]]
    with pytest.raises(hc_panes.Refused, match="cannot type '\"' '\\$' without submitting"):
        hc_panes.run(["pane", "send-text", "t1", 'say "$HOME"'], None)
    assert len(store(fake_tools)["keys"]) == 1


def test_an_agent_that_replaced_its_launcher_is_the_foreground_program(fake_tools, tmp_path):
    opened(tmp_path)
    hc_panes.run(["pane", "run", "t1", "codex resume " + SESSION], None)
    record = terminal(fake_tools)
    info = hc_panes.run(["pane", "process-info", "--pane", "t1"], None)["process_info"]
    assert info["shell_pid"] == record["pid"] != record["foreground"]["pid"]
    assert info["foreground_process_group_id"] == record["foreground"]["pid"]
    assert info["foreground_processes"] == [
        {
            "pid": record["foreground"]["pid"],
            "name": "codex",
            "argv": ["codex", "resume", SESSION],
            "cwd": str(tmp_path),
            "argv0": "codex",
        }
    ]
    assert herdr_handoff.program(info["foreground_processes"][0]) == "codex"


def test_a_terminal_running_a_program_of_its_own_has_no_shell_to_bind(
    fake_tools, tmp_path, hc_terminals
):
    opened(tmp_path)

    def own(data):  # As ``hc run claude``: the terminal ends when the agent does.
        found = data["terminals"][0]
        found["argv"] = found["foreground"]["argv"] = ["claude"]
        found["status"] = {"state": "idle", "app": "claude-code"}

    change(fake_tools, own)
    info = hc_panes.run(["pane", "process-info", "--pane", "t1"], None)["process_info"]
    assert info["shell_pid"] is None
    with pytest.raises(RuntimeError, match="Pane has no identifiable shell"):
        pane_runner.capture("t1", str(tmp_path), "claude")


def test_an_agent_beside_its_launcher_is_found_in_the_process_group(
    fake_tools, tmp_path, monkeypatch
):
    opened(tmp_path)
    launcher = ["bash", "/home/u/.local/bin/safehouse", "codex"]

    def edit(data):
        found = data["terminals"][0]
        found["foreground"] = {"pid": 500, "name": "bash", "argv": launcher, "cwd": str(tmp_path)}

    change(fake_tools, edit)
    codex = ["/opt/codex/bin/codex", "resume", SESSION]
    shell = terminal(fake_tools)["pid"]

    def process(pid, ppid, pgid, argv, tpgid=500):
        return {"pid": pid, "ppid": ppid, "pgid": pgid, "tpgid": tpgid, "start": str(pid)} | {
            "argv": argv
        }

    listed = [
        process(shell, 1, shell, ["/bin/zsh", "-l"]),
        process(500, shell, 500, launcher),
        process(501, 500, 500, codex),
        # What the agent started in groups of its own is not the pane's foreground.
        process(502, 501, 502, [codex[0], "exec"]),
        process(900, 1, 900, ["codex"], tpgid=900),
        # Neither is what the shell keeps running in the background.
        process(950, shell, 950, ["gitstatusd"]),
    ]
    monkeypatch.setattr(hc_panes, "processes", lambda: listed)
    monkeypatch.setattr(hc_panes, "process_cwd", lambda pid: f"/cwd/{pid}")
    monkeypatch.setattr(
        hc_panes, "codex_session", lambda pid, argv: SESSION if pid == 501 else None
    )
    # Not reporting yet: the agent is told by its program, under a launcher that is not one.
    [agent] = hc_panes.run(["agent", "list"], None)["agents"]
    assert (agent["agent"], agent["agent_status"]) == ("codex", "unknown")
    assert agent["agent_session"]["value"] == SESSION
    assert terminal(fake_tools)["labels"][hc_panes.SESSION_OF] == "501:0"
    info = hc_panes.run(["pane", "process-info", "--pane", "t1"], None)["process_info"]
    assert info["foreground_process_group_id"] == 500
    assert info["foreground_processes"] == [
        {"pid": 500, "name": "bash", "argv": launcher, "cwd": str(tmp_path)}
        | {"argv0": "bash", "start": "500"},
        {"pid": 501, "name": "codex", "argv": codex, "cwd": "/cwd/501"}
        | {"argv0": "codex", "start": "501"},
    ]
    # Under bwrap the agent has a session of its own: it is found below the group, with
    # the launchers between, and nothing else those started.
    apart = [
        listed[0],
        process(500, shell, 500, ["bwrap", "--clearenv"]),
        process(510, 500, 510, ["bwrap", "--clearenv"]),
        process(501, 510, 501, codex),
        process(520, 500, 520, ["gpg-agent-proxy"]),
    ]
    monkeypatch.setattr(hc_panes, "processes", lambda: apart)
    info = hc_panes.run(["pane", "process-info", "--pane", "t1"], None)["process_info"]
    assert [p["pid"] for p in info["foreground_processes"]] == [500, 501, 510]
    # Whatever asks from inside the terminal is not part of its foreground.
    asking = [*listed[:3], process(os.getpid(), 500, 500, ["python", "hc_panes.py"])]
    asking.append(process(960, os.getpid(), 500, ["ps"]))
    monkeypatch.setattr(hc_panes, "processes", lambda: asking)
    info = hc_panes.run(["pane", "process-info", "--pane", "t1"], None)["process_info"]
    assert [p["pid"] for p in info["foreground_processes"]] == [500, 501]
    # Back at its prompt, the shell stands alone whatever it runs in the background. The
    # system says so before hc's next sample of the terminal does.
    back = [dict(p, tpgid=shell) if p["tpgid"] == 500 else p for p in listed]
    monkeypatch.setattr(hc_panes, "processes", lambda: back)
    info = hc_panes.run(["pane", "process-info", "--pane", "t1"], None)["process_info"]
    assert [p["pid"] for p in info["foreground_processes"]] == [shell]
    assert info["foreground_process_group_id"] == shell
    assert hc_panes.run(["agent", "list"], None) == {"agents": []}
    # And a command that checks itself as it starts is in front before hc has sampled it.
    hc_panes.run(["agent", "send-keys", "t1", "ctrl+d"], None)
    runner = [dict(listed[0], tpgid=700), process(700, shell, 700, ["python", "run.py"], 700)]
    monkeypatch.setattr(hc_panes, "processes", lambda: runner)
    info = hc_panes.run(["pane", "process-info", "--pane", "t1"], None)["process_info"]
    assert [(p["pid"], p["argv"]) for p in info["foreground_processes"]] == [
        (700, ["python", "run.py"])
    ]
    assert info["foreground_processes"][0]["cwd"] == "/cwd/700"
    monkeypatch.setattr(hc_panes, "processes", lambda: listed)
    change(fake_tools, edit)
    # Without a process table (a sandbox that hides it) the foreground program stands alone.
    monkeypatch.setattr(hc_panes, "processes", lambda: (_ for _ in ()).throw(OSError("denied")))
    info = hc_panes.run(["pane", "process-info", "--pane", "t1"], None)["process_info"]
    assert [p["pid"] for p in info["foreground_processes"]] == [500]
    assert hc_panes.run(["agent", "list"], None) == {"agents": []}


def test_the_process_table_is_read_from_ps_on_macos(monkeypatch):
    listing = (
        "  501     1   501      0 Mon Oct  5 19:42:48 2026 /bin/zsh -l\n"
        "24067   501 24067  24067 Sun Nov 15 09:35:58 2026 claude --resume abc\n"
        "  777     1   777     -1 Sun Nov 15 09:35:58 2026 /usr/sbin/daemon\n"
        "garbage line\n"
    )
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(
        hc_panes.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, listing, "")
    )
    found = REAL_PROCESSES()
    assert found == [
        {"pid": 501, "ppid": 1, "pgid": 501, "tpgid": 0}
        | {"start": "Mon Oct 5 19:42:48 2026", "argv": ["/bin/zsh", "-l"]},
        {"pid": 24067, "ppid": 501, "pgid": 24067, "tpgid": 24067}
        | {"start": "Sun Nov 15 09:35:58 2026", "argv": ["claude", "--resume", "abc"]},
        {"pid": 777, "ppid": 1, "pgid": 777, "tpgid": -1}
        | {"start": "Sun Nov 15 09:35:58 2026", "argv": ["/usr/sbin/daemon"]},
    ]


def test_a_workers_agent_is_found_under_its_sandbox_launcher(fake_tools, tmp_path):
    change(fake_tools, lambda data: data["machines"].append({"name": "Box"}))
    opened(tmp_path, machine="Box")
    assert terminal(fake_tools, "Box/t1")["cwd"] == str(tmp_path)
    tree = [
        {"pid": 4240, "name": "bwrap", "argv": ["bwrap", "--clearenv"], "cwd": str(tmp_path)},
        {"pid": 4242, "name": "claude", "argv": ["claude"], "cwd": str(tmp_path)},
    ]

    def edit(data):
        found = data["terminals"][0]
        found["foreground"] = dict(tree[0])
        found["status"] = {"state": "idle", "app": "claude-code"}
        found["labels"][hc_panes.SESSION] = SESSION
        data["trees"] = {"4240": tree}

    change(fake_tools, edit)
    info = hc_panes.run(["pane", "process-info", "--pane", "t1"], "Box")["process_info"]
    assert info["foreground_processes"] == [
        dict(tree[0], argv0="bwrap"),
        dict(tree[1], argv0="claude"),
    ]
    [executed] = store(fake_tools)["executed"]
    assert executed["on"] == "Box" and executed["argv"][:3] == ["python3", "-I", "-c"]
    # The broker labelled the session; the status names the agent the launcher hides.
    [agent] = hc_panes.run(["agent", "list"], "Box")["agents"]
    assert (agent["agent"], agent["agent_status"]) == ("claude", "idle")
    assert agent["agent_session"]["value"] == SESSION


def test_this_machines_sessions_are_read_from_the_agents_themselves(
    fake_tools, tmp_path, monkeypatch
):
    for name in ("a", "b"):
        (tmp_path / name).mkdir()
    opened(tmp_path / "a")
    hc_panes.run(["pane", "run", "t1", "codex resume " + SESSION], None)
    [agent] = hc_panes.run(["agent", "list"], None)["agents"]
    assert agent["agent_session"] == {"agent": "codex", "kind": "id", "value": SESSION}
    # Found once per turn of that Codex process, then kept on the terminal.
    pid = terminal(fake_tools)["foreground"]["pid"]
    assert terminal(fake_tools)["labels"][hc_panes.SESSION_OF] == f"{pid}:0"
    monkeypatch.setattr(hc_panes, "codex_session", lambda *_: pytest.fail("read again"))
    assert hc_panes.run(["agent", "list"], None)["agents"][0]["agent_session"]["value"] == SESSION
    # A later turn may follow a /new, and another process is another conversation: the
    # label is not theirs, and one that cannot be read yet has no session.
    monkeypatch.setattr(hc_panes, "codex_session", lambda *_: None)
    for field, value in (("turn", 1), ("pid", pid + 1)):

        def moved(data, field=field, value=value):
            found = data["terminals"][0]
            if field == "turn":
                found["turn"] = value
            else:
                found["foreground"]["pid"] = value

        change(fake_tools, moved)
        assert "agent_session" not in hc_panes.run(["pane", "get", "t1"], None)["pane"]
    # Claude records its session per process, in whichever account it runs.
    opened(tmp_path / "b", title="other")
    hc_panes.run(["pane", "run", "t2", "claude"], None)
    assert "agent_session" not in hc_panes.run(["pane", "get", "t2"], None)["pane"]
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    sessions = tmp_path / ".claude/accounts/oss/sessions"
    sessions.mkdir(parents=True)
    claude = terminal(fake_tools, "t2")["foreground"]["pid"]
    (sessions / f"{claude}.json").write_text(json.dumps({"pid": claude, "sessionId": SESSION}))
    assert hc_panes.run(["pane", "get", "t2"], None)["pane"]["agent_session"]["value"] == SESSION
    # A record for another process, or a malformed one, names nothing.
    (sessions / f"{claude}.json").write_text(json.dumps({"pid": 1, "sessionId": SESSION}))
    assert "agent_session" not in hc_panes.run(["pane", "get", "t2"], None)["pane"]
    # Neither does one an earlier process with the same id left behind.
    assert hc_panes.claude_session(claude, "Sun Nov 15 09:35:58 2026") is None
    left = {"pid": claude, "sessionId": SESSION, "procStart": "Sat Nov 14 08:00:00 2026"}
    (sessions / f"{claude}.json").write_text(json.dumps(left))
    assert hc_panes.claude_session(claude, "Sun Nov 15 09:35:58 2026") is None
    assert hc_panes.claude_session(claude, "Sat Nov 14 08:00:00 2026") == SESSION
    # ps pads a one-digit day; the same moment still matches.
    left["procStart"] = "Mon Oct 5 19:42:48 2026"
    (sessions / f"{claude}.json").write_text(json.dumps(left))
    assert hc_panes.claude_session(claude, "Mon Oct  5 19:42:48 2026") == SESSION
    # A sandbox broker's label, which carries no mark of ours, is the word of last resort.
    (sessions / f"{claude}.json").unlink()
    hc.label("t2", {hc_panes.SESSION: SESSION})
    assert hc_panes.run(["pane", "get", "t2"], None)["pane"]["agent_session"]["value"] == SESSION


def test_further_terminals_join_the_checkouts_workspace_and_close_one_by_one(fake_tools, tmp_path):
    opened(tmp_path)
    wid = hc_panes.workspace_id(str(tmp_path))
    split = hc_panes.run(
        ["pane", "split", "t1", "--direction", "right", "--cwd", str(tmp_path), "--no-focus"], None
    )
    assert split == {"pane": {"pane_id": "t2"}}
    hc_panes.run(["pane", "rename", "t2", "babysit", "agent"], None)
    panes = hc_panes.run(["pane", "list", "--workspace", wid], None)["panes"]
    assert [(p["pane_id"], p["label"]) for p in panes] == [
        ("t1", "pr-repo-feature"),
        ("t2", "babysit agent"),
    ]
    second = terminal(fake_tools, "t2")
    assert second["name"] == "pr-repo-feature-2"
    assert second["labels"][hc_panes.CLONE] == "/clone"
    assert len(hc_panes.run(["workspace", "list"], None)["workspaces"]) == 1
    hc_panes.run(["pane", "close", "t2"], None)
    assert [t["address"] for t in store(fake_tools)["terminals"]] == ["t1"]
    hc_panes.run(["workspace", "close", wid], None)
    assert store(fake_tools)["terminals"] == []


def test_the_command_line_never_reads_this_machines_processes_in_tests(
    fake_tools, tmp_path, hc_terminals
):
    # The fake's terminals name made-up process ids: a real process that has one must
    # not be taken for what runs there, in the command line any more than in-process.
    opened(tmp_path)

    def real(data):  # This test's own process, which really runs and has children.
        found = data["terminals"][0]
        found["foreground"] = {**found["foreground"], "pid": os.getppid(), "argv": ["launcher"]}

    change(fake_tools, real)
    info = pw.herdr("pane", "process-info", "--pane", "t1")["process_info"]
    assert [p["pid"] for p in info["foreground_processes"]] == [os.getppid()]


def test_the_command_line_answers_in_herdrs_envelope(fake_tools, tmp_path, hc_terminals):
    argv = machines.herdr_argv(None, "workspace", "focus", "hc-0123456789ab")
    assert argv[:2] == [sys.executable, str(machines.HC_PANES)]
    refused = subprocess.run(argv, capture_output=True, text=True, check=False)
    assert refused.returncode == 1
    assert json.loads(refused.stdout)["error"]["code"] == "unsupported"
    assert pw.herdr("worktree", "open", "--cwd", "/clone", "--path", str(tmp_path))[
        "root_pane"
    ] == {"pane_id": "t1"}
    change(fake_tools, lambda data: data.update(screens={"t1": ["  first line\nsecond"]}))
    read = subprocess.run(
        machines.herdr_argv(None, "agent", "read", "t1", "--source", "visible", "--format", "ansi"),
        capture_output=True,
        text=True,
        check=True,
    )
    assert read.stdout == "  first line\nsecond\n"
    # A worker is addressed by its label, as herdr's is.
    worker = machines._machine({"id": "box", "label": "Box", "ssh": "user@box", "os": "linux"})
    assert machines.herdr_argv(worker, "agent", "list")[2:] == ["--machine=Box", "agent", "list"]
    with pytest.raises(ValueError, match="hc does not know Box"):
        pw.herdr("agent", "list", machine=worker)


# --- which runtime ----------------------------------------------------------------------


def test_the_runtime_is_the_setting_then_the_surrounding_terminal(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("BABYSIT_TERMINALS")
    assert machines.terminals() == "herdr"
    assert machines.herdr_argv(None, "agent", "list") == ["herdr", "agent", "list"]
    # Inside an hc terminal here, the setting still decides: no file keeps herdr.
    monkeypatch.setenv("HC_TERMINAL_ID", "t7")
    assert machines.terminals() == "herdr"
    setting = tmp_path / ".config/babysit-pr/terminals"
    setting.parent.mkdir(parents=True)
    # A line typed into a worker's hc terminal needs no setting there.
    (setting.parent / "worker.json").write_text("{}")
    assert machines.terminals() == "hc"
    monkeypatch.delenv("HC_TERMINAL_ID")
    assert machines.terminals() == "herdr"
    monkeypatch.setenv("HC_TERMINAL_ID", "t7")
    setting.write_text("herdr\n")
    assert machines.terminals() == "herdr"
    setting.write_text("hc  # since November\n")
    monkeypatch.delenv("HC_TERMINAL_ID")
    assert machines.terminals() == "hc"
    monkeypatch.setenv("BABYSIT_TERMINALS", "herdr")
    assert machines.terminals() == "herdr"
    monkeypatch.setenv("BABYSIT_TERMINALS", "tmux")
    with pytest.raises(ValueError, match="Unknown terminal runtime 'tmux'"):
        machines.terminals()


def test_collie_has_no_page_for_a_herdr_core_workspace():
    assert pw.collie_url(None, "w1").endswith("/space/w1")
    assert pw.collie_url(None, hc_panes.workspace_id("/src/a")) is None


# --- where herdr-core does a step whole -------------------------------------------------


def test_text_and_enter_become_one_submitted_line_on_herdr_core(monkeypatch):
    steps = [("key", "3"), ("text", "Because"), ("key", "enter"), ("key", "1")]
    assert agent_messages.whole_lines(steps) == steps
    monkeypatch.setenv("BABYSIT_TERMINALS", "hc")
    assert agent_messages.whole_lines(steps) == [("key", "3"), ("line", "Because"), ("key", "1")]
    assert agent_messages.whole_lines([("key", "enter")]) == [("key", "enter")]


def test_plain_screens_are_never_read_for_a_draft_or_an_idle_box(monkeypatch):
    placeholder = '──────\n❯ Try "fix lint errors"\n──────\n'
    target = {"agent": "claude", "marker": "[babysit-done:0123456789abcdef]"}
    shown = placeholder + target["marker"]
    # herdr's styles would show the placeholder faint; as plain text it reads as input.
    with pytest.raises(RuntimeError, match="Composer contains input"):
        herdr_handoff.verify_screen(target, {}, shown)
    assert agent_messages.has_draft(placeholder, "claude")
    monkeypatch.setenv("BABYSIT_TERMINALS", "hc")
    herdr_handoff.verify_screen(target, {}, shown)
    assert not agent_messages.has_draft(placeholder, "claude")
    assert not agent_messages.idle_screen("t1", "──────\n❯ \n──────\n")
    # What plain text can show still stops an exit.
    with pytest.raises(RuntimeError, match="Queued input or background work"):
        herdr_handoff.verify_screen(target, {}, shown + "\nesc to interrupt")
    with pytest.raises(herdr_handoff.ScreenNotReady, match="Final receipt"):
        herdr_handoff.verify_screen(target, {}, placeholder)


def exit_target(state, agent, tmp_path):
    record = terminal(state)
    return {
        "agent": agent,
        "pane_id": "t1",
        "terminal_id": "t1",
        "shell_pid": record["pid"],
        "agent_pid": record["foreground"]["pid"],
        "agent_argv": record["foreground"]["argv"],
        "cwd": str(tmp_path),
        "marker": "",
    }


def gone(pid, signal):
    raise ProcessLookupError


def test_a_claude_on_herdr_core_is_asked_to_exit_with_one_submitted_line(
    fake_tools, tmp_path, hc_terminals, monkeypatch
):
    opened(tmp_path)
    hc_panes.run(["pane", "run", "t1", "claude"], None)
    report(fake_tools, "t1", app="claude-code", state="idle")
    target = exit_target(fake_tools, "claude", tmp_path)
    monkeypatch.setattr(herdr_handoff.os, "kill", gone)
    owned = []
    herdr_handoff.send_exit(target, None, None, lambda: owned.append(1), None)
    # One /exit that hc may hold or refuse; no key typed, no composer read back.
    assert store(fake_tools)["sent"][-1] == ["t1", "/exit", False]
    assert "keys" not in store(fake_tools) and owned
    assert not any(method == "terminal.read" for method, _ in store(fake_tools)["calls"])
    assert hc_panes.run(["agent", "list"], None) == {"agents": []}


def test_an_exit_hc_refuses_presses_nothing(fake_tools, tmp_path, hc_terminals):
    opened(tmp_path)
    hc_panes.run(["pane", "run", "t1", "claude"], None)
    report(fake_tools, "t1", app="claude-code", state="blocked", kind="permission")
    target = exit_target(fake_tools, "claude", tmp_path)
    with pytest.raises(RuntimeError, match="The agent is blocked; /exit not sent"):
        herdr_handoff.send_exit(target, None, None, lambda: None, None)
    assert [text for _, text, _ in store(fake_tools)["sent"]] == ["claude"]
    assert hc_panes.run(["agent", "list"], None)["agents"][0]["agent"] == "claude"


def after(seconds, edit, path):
    """Change the fake's state while a send is being held there."""
    timer = threading.Timer(seconds, lambda: change(path, edit))
    timer.start()
    return timer


@pytest.mark.parametrize(
    "meanwhile,refusal",
    [
        ({"state": "working", "app": "claude-code"}, "t1 is working, not idle"),
        # It left for its shell: the line must never run there as a command.
        (None, "t1 is silent, not idle"),
    ],
)
def test_an_exit_is_not_typed_into_an_agent_that_changed_while_hc_held_it(
    fake_tools, tmp_path, hc_terminals, monkeypatch, meanwhile, refusal
):
    opened(tmp_path)
    hc_panes.run(["pane", "run", "t1", "claude"], None)
    report(fake_tools, "t1", app="claude-code", state="idle")
    target = exit_target(fake_tools, "claude", tmp_path)
    monkeypatch.setenv("FAKE_HC_HOLD", "1.5")  # A person is typing in the terminal.

    def moved_on(data):
        data["terminals"][0]["status"] = meanwhile

    timer = after(0.5, moved_on, fake_tools)
    with pytest.raises(RuntimeError, match=f"hc did not submit /exit: {refusal}"):
        herdr_handoff.send_exit(target, None, None, lambda: None, None)
    timer.join()
    # hc was asked to type only into a still idle agent, and typed nothing.
    [asked] = [q for method, q in store(fake_tools)["calls"] if method == "terminal.send"][-1:]
    assert asked["text"] == "/exit" and asked["if"] == "idle"
    assert [text for _, text, _ in store(fake_tools)["sent"]] == ["claude"]


def test_an_exit_held_too_long_is_taken_back(fake_tools, tmp_path, hc_terminals, monkeypatch):
    opened(tmp_path)
    hc_panes.run(["pane", "run", "t1", "claude"], None)
    report(fake_tools, "t1", app="claude-code", state="done")
    target = exit_target(fake_tools, "claude", tmp_path)
    monkeypatch.setattr(hc, "BRIEF_HOLD", 0.3)
    monkeypatch.setattr(hc, "TYPING", 0.2)
    monkeypatch.setenv("FAKE_HC_HOLD", "3")
    with pytest.raises(RuntimeError, match="a person is typing; it was taken back"):
        herdr_handoff.send_exit(target, None, None, lambda: None, None)
    # Its caller gone, the held line is never typed.
    time.sleep(3.2)
    assert [text for _, text, _ in store(fake_tools)["sent"]] == ["claude"]


def test_a_message_is_never_run_by_the_shell_an_agent_left(
    fake_tools, tmp_path, hc_terminals, monkeypatch
):
    opened(tmp_path)
    hc_panes.run(["pane", "run", "t1", "claude"], None)
    report(fake_tools, "t1", app="claude-code", state="working")
    monkeypatch.setenv("FAKE_HC_HOLD", "1.5")

    def left(data):
        found = data["terminals"][0]
        found["status"] = None
        found["foreground"] = {**found["foreground"], "pid": found["pid"], "argv": found["argv"]}

    timer = after(0.5, left, fake_tools)
    with pytest.raises(hc_panes.Refused, match="t1 is silent, not working; nothing was typed"):
        hc_panes.run(["agent", "prompt", "t1", "rm -rf build\nmake"], None)
    timer.join()
    assert [text for _, text, _ in store(fake_tools)["sent"]] == ["claude"]


def test_a_codex_on_herdr_core_still_gets_ctrl_d(fake_tools, tmp_path, hc_terminals, monkeypatch):
    opened(tmp_path)
    hc_panes.run(["pane", "run", "t1", "codex"], None)
    report(fake_tools, "t1", app="codex", state="idle")
    monkeypatch.setattr(herdr_handoff.os, "kill", gone)
    herdr_handoff.send_exit(
        exit_target(fake_tools, "codex", tmp_path), None, None, lambda: None, None
    )
    assert store(fake_tools)["keys"] == [["t1", ["ctrl+d"]]]


def test_the_dashboard_quits_an_agent_on_herdr_core(
    fake_tools, tmp_path, hc_terminals, monkeypatch
):
    opened(tmp_path)
    wid = hc_panes.workspace_id(str(tmp_path))
    hc_panes.run(["pane", "run", "t1", "claude"], None)
    report(fake_tools, "t1", app="claude-code", state="idle")
    # A placeholder, which hc's plain text cannot tell from a draft, is on screen.
    change(fake_tools, lambda data: data.update(screens={"t1": ['─────\n❯ Try "fix it"\n─────']}))
    monkeypatch.setattr(
        agent_messages.workspace_viewer, "live_workspace", lambda workspace, *a, **k: workspace
    )
    monkeypatch.setattr(agent_messages, "watch_owner", lambda *a: None)
    monkeypatch.setattr(agent_messages, "exit_watcher", lambda *a: None)
    monkeypatch.setattr(agent_messages, "interactive_shell", lambda *a: True)
    monkeypatch.setattr(herdr_handoff.os, "kill", gone)
    request = {"workspace": wid, "pane": "t1", "session": None}
    quit_ = agent_messages.exit_agent(request, tmp_path / "state")
    assert quit_ == {"quit": True, "pane": "t1", "warning": None}
    # The agent's own process was identified, and asked to leave only while still idle.
    [asked] = [q for method, q in store(fake_tools)["calls"] if method == "terminal.send"][-1:]
    assert (asked["text"], asked["if"]) == ("/exit", "idle")
    assert "keys" not in store(fake_tools)
    assert hc_panes.run(["agent", "list"], None) == {"agents": []}


def test_the_terminal_view_submits_a_line_whole_on_herdr_core(
    fake_tools, tmp_path, hc_terminals, monkeypatch
):
    opened(tmp_path)
    wid = hc_panes.workspace_id(str(tmp_path))
    monkeypatch.setattr(agent_terminal, "live", lambda workspace, machine=None: workspace)
    agent_terminal.send({"workspace": wid, "pane": "t1", "text": 'echo "$HOME"', "enter": True})
    assert store(fake_tools)["sent"] == [["t1", 'echo "$HOME"', True]]
    agent_terminal.send({"workspace": wid, "pane": "t1", "text": "ls", "enter": False})
    agent_terminal.send({"workspace": wid, "pane": "t1", "keys": ["ctrl+c"]})
    assert store(fake_tools)["keys"] == [["t1", ["l", "s"]], ["t1", ["ctrl+c"]]]
    with pytest.raises(ValueError, match="cannot type"):
        agent_terminal.send({"workspace": wid, "pane": "t1", "text": "~", "enter": False})


# --- the watcher's pane binding ---------------------------------------------------------


def test_a_watch_binds_its_hc_terminal_and_types_a_repair_into_its_shell(
    fake_tools, tmp_path, hc_terminals
):
    opened(tmp_path)
    hc_panes.run(["pane", "run", "t1", "codex"], None)
    # The agent registers from the terminal it runs in: its address is $HC_TERMINAL_ID.
    binding = pane_runner.capture("t1", str(tmp_path), "codex")
    assert binding == {
        "pane_id": "t1",
        "terminal_id": "t1",
        "shell_pid": terminal(fake_tools)["pid"],
    }
    with pytest.raises(RuntimeError, match="Bind the original claude pane"):
        pane_runner.capture("t1", str(tmp_path), "claude")
    with pytest.raises(RuntimeError, match="Pane worktree does not match"):
        pane_runner.capture("t1", str(tmp_path / "elsewhere"), "codex")
    # While the agent runs, nothing is typed over it.
    with pytest.raises(RuntimeError, match="Original pane is busy"):
        pane_runner.locate(binding, str(tmp_path), require_shell=True)
    hc_panes.run(["agent", "send-keys", "t1", "ctrl+d"], None)
    assert pane_runner.locate(binding, str(tmp_path), require_shell=True) == "t1"
    runner = tmp_path / "runner.py"
    runner.write_text(
        "import json, os, sys\n"
        "open('ran.json', 'w').write(json.dumps([sys.argv[1:], os.environ['HC_TERMINAL_ID']]))\n"
    )
    job = {"id": "abc", "attempt": "run-1", "cwd": str(tmp_path), "pane": binding}
    pane_runner.launch(job, tmp_path / "state", runner)
    ran, inside = json.loads((tmp_path / "ran.json").read_text())
    assert ran == ["--home", str(tmp_path / "state"), "_repair", "abc", "run-1"]
    assert inside == "t1"
    # A terminal that ended is never replaced by another in the same checkout.
    hc_panes.run(["pane", "close", "t1"], None)
    opened(tmp_path)
    with pytest.raises(RuntimeError, match="Original herdr terminal is missing"):
        pane_runner.locate(binding, str(tmp_path), require_shell=True)


def test_a_restarted_shell_is_not_the_bound_one(fake_tools, tmp_path, hc_terminals):
    opened(tmp_path)
    binding = pane_runner.capture("t1", str(tmp_path), "codex")

    def restart(data):  # hc gave the kept terminal a new shell after losing the old one.
        found = data["terminals"][0]
        found["pid"] = found["foreground"]["pid"] = found["pid"] + 7

    change(fake_tools, restart)
    with pytest.raises(RuntimeError, match="terminal or shell was replaced"):
        pane_runner.locate(binding, str(tmp_path), require_shell=True)


# --- dashboard launches -----------------------------------------------------------------


def test_the_copied_command_attaches_a_herdr_core_terminal(local, hc_terminals):  # noqa: F811
    manager, pr, _, state, _ = local
    manager.action({"id": pr["id"], "action": "create", "agent": "codex", "task": "Fix"})
    op = finish(manager, pr["id"])
    assert op["status"] == "complete", op
    target = {"id": pr["id"], "workspace_id": op["result"]["workspace_id"], "path": op["path"]}
    assert manager.action({**target, "action": "copy"}) == {"command": "hc attach t1"}
    # There is no window to focus: the action says so instead of doing nothing.
    with pytest.raises(ValueError, match="no window to focus"):
        manager.action({**target, "action": "focus"})


def test_a_dashboard_launch_starts_its_agent_in_an_hc_terminal(local, hc_terminals):  # noqa: F811
    manager, pr, _, state, _ = local
    task = "Fix quotes ' \" $() `touch SHOULD_NOT_EXIST`\nsecond line; echo no"
    request = {"id": pr["id"], "action": "create", "agent": "claude", "task": task}
    manager.action(request)
    op = finish(manager, pr["id"])
    assert op["status"] == "complete", op
    [record] = store(state)["terminals"]
    assert record["labels"] == {
        hc_panes.CHECKOUT: op["path"],
        hc_panes.CLONE: str(manager.src / "repo"),
        hc_panes.TITLE: "pr-repo-feature",
    }
    assert record["name"] == "pr-repo-feature" and record["cwd"] == op["path"]
    assert record["task"] == task + "\n\nPull request: " + pr["url"]
    assert not (Path(op["path"]) / "SHOULD_NOT_EXIST").exists()
    # The workspace the dashboard follows is the checkout's, and Collie has no page for it.
    assert op["result"]["workspace_id"] == hc_panes.workspace_id(op["path"])
    assert op["result"]["url"] is None and op["result"]["agent_status"] == "working"
    # Nothing went to herdr, and wt was told which runtime to use.
    assert json.loads(state.read_text())["calls"] == []
    assert json.loads(state.read_text())["workspaces"] == []
    manager.scan()  # The snapshot serves the cached inventory and refreshes lazily.
    [match] = manager.snapshot()["prs"][pr["id"]]["matches"]
    assert match["workspace_id"] == op["result"]["workspace_id"] and match["url"] is None
    # A second launch for the same pull request finds the workspace open.
    again = manager.action(request)["operation"]
    assert again["id"] == op["id"]
    assert len(store(state)["terminals"]) == 1


def test_an_exit_watch_on_herdr_core_closes_the_agents_terminal(
    local,  # noqa: F811
    hc_terminals,
    monkeypatch,
):
    manager, _, _, state, _ = local
    brief = manager.home.parent / "brief.md"
    brief.write_text("Review the PR; write the report.")
    subprocess.run(
        [sys.executable, pw.WORKTREE_HELPER, "wtpr", "-r", "repo", "--codex"]
        + ["--name", "review-pr-7", "-F", str(brief), "--exit-when-done", "7"],
        env={**os.environ, "BABYSIT_HOME": str(manager.home)},
        check=True,
        capture_output=True,
    )
    [launched] = manager.scheduled_tasks()["launched"]
    path = manager.src / "worktrees/repo/review-pr-7"
    assert launched["state"] == "watching" and launched["path"] == str(path.resolve())
    assert launched["url"] is None
    with exit_watch.connect(manager.home) as db:
        [watch] = [json.loads(data) for (data,) in db.execute("SELECT data FROM exit_watches")]
    assert watch["pane_id"] == "t1"
    assert watch["workspace_id"] == hc_panes.workspace_id(str(path.resolve()))
    record = terminal(state)
    assert watch["marker"] in record["task"]
    seen = []

    def step(record, op, persist, now=None, *_):
        # What wt recorded is enough to find the agent's terminal and process.
        record["target"] = workspace_exit.capture(op)
        seen.append(record["target"])
        persist({**record, "state": "exiting"})
        hc_panes.run(["agent", "send-keys", "t1", "ctrl+d"], None)
        return {**record, "state": "exited", "message": "Exited"}

    monkeypatch.setattr(pw.workspace_exit, "step", step)
    manager.exit_launched()
    [target] = seen
    assert (target["pane_id"], target["terminal_id"]) == ("t1", "t1")
    assert target["shell_pid"] == record["pid"]
    assert target["agent_pid"] == record["foreground"]["pid"]
    with manager.db() as db:
        (data,) = db.execute("SELECT data FROM exit_watches").fetchone()
    assert json.loads(data)["exit"]["message"] == "Exited; its workspace was closed"
    # The checkout's only terminal went with it; the checkout stays for a later resume.
    assert store(state)["terminals"] == []
    assert (path / ".git").exists()
