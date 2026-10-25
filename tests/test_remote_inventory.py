"""Worker machines in the workspace inventory: the probe in-process and over the fake ssh.

Each fake host is a directory under ``<tmp>/hosts/<ssh target>`` that the fake ssh uses as
HOME, with its own clones; its herdr workspaces live in the fake herdr's per-label state.
"""

import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import attention
import machines
import pr_workspaces
import pytest
import remote_probe
import workspace_overview as wso
from test_workspace_overview import BASE, FakeGitHub

MINI = {"id": "mini", "label": "Mac mini", "ssh": "mini.test", "os": "darwin"}
BOX = {"id": "box", "label": "Linux box", "ssh": "box.test", "os": "linux"}
HOSTILE = 'it\'s $(touch PWNED) `touch PWNED` "x"; y'


def git(*args, cwd):
    return subprocess.check_output(["git", "-C", str(cwd), *args], text=True).strip()


def make_clone(clone, branches):
    """A clone tracking fork/repo with base/repo upstream, and one worktree per branch."""
    clone.mkdir(parents=True)
    git("init", "-q", "-b", "main", cwd=clone)
    git("config", "user.name", "Fixture", cwd=clone)
    git("config", "user.email", "fixture@example.test", cwd=clone)
    git("commit", "-q", "--allow-empty", "-m", "base", cwd=clone)
    git("remote", "add", "origin", "https://github.com/fork/repo.git", cwd=clone)
    git("remote", "add", "upstream", f"https://github.com/{BASE}.git", cwd=clone)
    git("update-ref", "refs/remotes/origin/main", "HEAD", cwd=clone)
    worktrees = clone.parent / "worktrees" / clone.name
    for branch in branches:
        git("worktree", "add", "-q", "-b", branch, str(worktrees / branch), cwd=clone)
        git("update-ref", f"refs/remotes/origin/{branch}", "HEAD", cwd=clone)
    return worktrees


def read(state):
    return json.loads(Path(state).read_text())


@pytest.fixture
def short_tmp(tmp_path, monkeypatch):
    """ssh control sockets need a short directory; never the real /tmp's shared one."""
    path = tempfile.mkdtemp(prefix="bs", dir="/tmp")
    monkeypatch.setattr(tempfile, "tempdir", path)
    machines.control_dir.cache_clear()
    yield Path(path)
    machines.control_dir.cache_clear()
    shutil.rmtree(path)


@pytest.fixture
def fleet(tmp_path, fake_tools, short_tmp, monkeypatch):
    """This machine with one clone, and two workers with their own clones and workspaces."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    src = tmp_path / "src"
    local = make_clone(src / "repo", ["local-work"])
    hosts = tmp_path / "hosts"
    mini = make_clone(hosts / "mini.test" / "src" / "repo", ["merged-work", "dirty-work"])
    (mini / "dirty-work" / "scratch.txt").write_text("left behind")
    box = make_clone(hosts / "box.test" / "src" / "tool", ["box-work"])
    store = read(fake_tools)
    store["machines"] = {
        "Mac mini": {
            "workspaces": [
                {
                    "workspace_id": "w1",
                    "label": "merged-work",
                    "agent_status": "idle",
                    "worktree": {
                        "repo_root": str(hosts / "mini.test" / "src" / "repo"),
                        "checkout_path": str(mini / "merged-work"),
                    },
                },
                {
                    "workspace_id": "w2",
                    "label": "gone",
                    "agent_status": "unknown",
                    "worktree": {
                        "repo_root": str(hosts / "mini.test" / "src" / "repo"),
                        "checkout_path": str(mini / "removed"),
                    },
                },
            ],
            "agents": [
                {
                    "workspace_id": "w1",
                    "agent": "codex",
                    "agent_status": "working",
                    "pane_id": "w1:p1",
                }
            ],
            "calls": [],
        },
        "Linux box": {"workspaces": [], "agents": [], "calls": []},
    }
    fake_tools.write_text(json.dumps(store))
    home = short_tmp / "state"
    directory = home / "experiments" / "workspaces"
    directory.mkdir(parents=True)
    (directory / "config.json").write_text(json.dumps({"enabled": True, "src": str(src)}))
    calls = []
    real = machines.run

    def counted(home, m, argv, **kwargs):
        if m is not None:
            calls.append(m.id)
        return real(home, m, argv, **kwargs)

    monkeypatch.setattr(machines, "run", counted)
    # Counted here: the fakes' shared log may drop an entry when two hosts write at once.
    listings = []
    herdr = wso.herdr

    def listed(*args, machine=None, **kwargs):
        if machine is not None:
            listings.append((machine.id, args))
        return herdr(*args, machine=machine, **kwargs)

    monkeypatch.setattr(wso, "herdr", listed)
    return {
        "home": home,
        "state": fake_tools,
        "local": local,
        "mini": mini,
        "box": box,
        "hosts": hosts,
        "ssh": calls,
        "herdr": listings,
    }


def register(home, *entries):
    (home / "machines.json").write_text(json.dumps({"version": 1, "machines": list(entries)}))


def by_key(value):
    return {row["key"]: row for row in value["workspaces"]}


def test_without_a_registry_no_ssh_runs_and_the_snapshot_is_unchanged(fleet):
    plugin = wso.WorkspaceOverview(fleet["home"], FakeGitHub())
    value = plugin.collect()
    assert set(value) == {"workspaces", "warnings", "calls", "src"}
    assert [row["key"] for row in value["workspaces"]] == [str(fleet["local"] / "local-work")]
    assert not any(key.startswith("machine") for row in value["workspaces"] for key in row)
    assert set(plugin.inventory_scope()) == {"src", "roots"}
    assert fleet["ssh"] == [] and "ssh" not in read(fleet["state"])
    assert fleet["herdr"] == []


def test_each_worker_is_scanned_with_one_ssh_call_and_listed_read_only(fleet):
    register(fleet["home"], MINI, BOX)
    plugin = wso.WorkspaceOverview(fleet["home"], FakeGitHub())
    value = plugin.collect()
    assert sorted(fleet["ssh"]) == ["box", "mini"]
    assert sorted(fleet["herdr"]) == [
        ("box", ("agent", "list")),
        ("box", ("workspace", "list")),
        ("mini", ("agent", "list")),
        ("mini", ("workspace", "list")),
    ]
    rows = by_key(value)
    local = str(fleet["local"] / "local-work")
    assert "machine" not in rows[local] and rows[local]["removable"]
    merged = rows[f"m:mini:{fleet['mini'] / 'merged-work'}"]
    assert merged["path"] == str(fleet["mini"] / "merged-work")
    assert (merged["machine"], merged["machine_label"], merged["machine_stale"]) == (
        "mini",
        "Mac mini",
        False,
    )
    # GitHub matching reads the worker's clone config its probe returned.
    assert merged["repo"] == BASE and [link["number"] for link in merged["links"]] == [7]
    assert merged["workspace_ids"] == ["mini/w1"] and merged["workspace_url"] is None
    assert merged["agents"] == [{"agent": "codex", "status": "working", "pane": "w1:p1"}]
    assert merged["removable"] is False and merged["status"] == "blocked"
    dirty = rows[f"m:mini:{fleet['mini'] / 'dirty-work'}"]
    assert dirty["changes"] == 1 and dirty["created_at"] is not None
    gone = rows["workspace:mini:w2"]
    assert gone["missing"] and gone["status"] == "missing" and gone["removable"] is False
    assert f"m:box:{fleet['box'] / 'box-work'}" in rows
    assert value["machines"] == {
        "mini": {
            "label": "Mac mini",
            "status": "ok",
            "error": None,
            "synced_at": pytest.approx(time.time(), abs=60),
        },
        "box": {
            "label": "Linux box",
            "status": "ok",
            "error": None,
            "synced_at": pytest.approx(time.time(), abs=60),
        },
    }
    assert plugin.inventory_scope()["machines"] == ["box", "mini"]
    plugin.collect()
    assert sorted(fleet["ssh"]) == ["box", "box", "mini", "mini"]


def test_worker_transcripts_date_its_rows(fleet):
    register(fleet["home"], MINI)
    day = fleet["hosts"] / "mini.test" / ".codex" / "sessions" / "2026" / "10" / "05"
    day.mkdir(parents=True)
    rollout = day / "rollout-recent.jsonl"
    meta = {"type": "session_meta", "payload": {"id": "01234567-1234"}}
    meta["payload"]["cwd"] = str(fleet["mini"] / "dirty-work" / "lib")
    rollout.write_text(json.dumps(meta) + "\n")
    latest = time.time() + 1000
    os.utime(rollout, (latest, latest))
    rows = by_key(wso.WorkspaceOverview(fleet["home"], FakeGitHub()).collect())
    assert rows[f"m:mini:{fleet['mini'] / 'dirty-work'}"]["updated_at"] == latest


def test_one_unreachable_worker_keeps_its_last_rows_and_backs_off(fleet, monkeypatch):
    register(fleet["home"], MINI, BOX)
    plugin = wso.WorkspaceOverview(fleet["home"], FakeGitHub())
    plugin.value = plugin.collect()
    monkeypatch.setenv("FAKE_SSH_DOWN", "mini.test")
    value = plugin.collect()
    rows = by_key(value)
    assert rows[str(fleet["local"] / "local-work")]["changes"] == 0
    assert rows[f"m:box:{fleet['box'] / 'box-work'}"]["machine_stale"] is False
    kept = [row for row in rows.values() if row.get("machine") == "mini"]
    assert len(kept) == 3 and all(row["machine_stale"] for row in kept)
    status = value["machines"]["mini"]
    assert status["status"] == "stale" and "unreachable" in status["error"]
    assert status["synced_at"] == plugin.value["machines"]["mini"]["synced_at"]
    assert any(w.startswith("Mac mini unavailable:") for w in value["warnings"])
    assert value["machines"]["box"]["status"] == "ok"
    plugin.value = value
    fleet["ssh"].clear()
    # Within the back-off the worker is not asked again; its rows stay stale.
    again = plugin.collect()
    assert fleet["ssh"] == ["box"]
    assert again["machines"]["mini"]["status"] == "stale"
    assert len([row for row in again["workspaces"] if row.get("machine") == "mini"]) == 3


def test_a_worker_never_reached_is_reported_unreachable_without_rows(fleet, monkeypatch):
    register(fleet["home"], MINI)
    monkeypatch.setenv("FAKE_SSH_DOWN", "mini.test")
    value = wso.WorkspaceOverview(fleet["home"], FakeGitHub()).collect()
    assert [row["key"] for row in value["workspaces"]] == [str(fleet["local"] / "local-work")]
    assert value["machines"]["mini"]["status"] == "unreachable"
    assert value["machines"]["mini"]["synced_at"] is None


def test_a_hung_worker_is_given_up_on_without_delaying_the_scan_past_its_timeout(
    fleet, monkeypatch
):
    register(fleet["home"], MINI)
    release = threading.Event()
    monkeypatch.setattr(wso, "REMOTE_TIMEOUT", 0.2)
    monkeypatch.setattr(wso.WorkspaceOverview, "scan_machine", lambda self, *args: release.wait(10))
    value = wso.WorkspaceOverview(fleet["home"], FakeGitHub()).collect()
    release.set()
    assert value["machines"]["mini"]["status"] == "unreachable"
    assert "No answer within" in value["machines"]["mini"]["error"]
    assert str(fleet["local"] / "local-work") in by_key(value)


def test_a_malformed_registry_scans_this_machine_and_says_so(fleet):
    (fleet["home"] / "machines.json").write_text("{")
    value = wso.WorkspaceOverview(fleet["home"], FakeGitHub()).collect()
    assert "machines" not in value and fleet["ssh"] == []
    assert any("Machine registry unavailable" in w for w in value["warnings"])


def test_hostile_worker_paths_round_trip_verbatim(fleet):
    register(fleet["home"], MINI)
    clone = fleet["hosts"] / "mini.test" / "src" / HOSTILE
    worktrees = make_clone(clone, [])
    path = worktrees.parent / f"{HOSTILE} wt"
    git("worktree", "add", "-q", "-b", "odd", str(path), cwd=clone)
    value = wso.WorkspaceOverview(fleet["home"], FakeGitHub()).collect()
    row = by_key(value)[f"m:mini:{path}"]
    assert row["path"] == str(path) and row["name"] == f"{HOSTILE} wt"
    assert row["repo_root"] == str(clone)
    assert not list(fleet["hosts"].rglob("PWNED")) and not list(Path.cwd().glob("PWNED"))


def test_worker_rows_offer_no_actions_and_cleanup_refuses_them(fleet):
    register(fleet["home"], MINI)
    plugin = wso.WorkspaceOverview(fleet["home"], FakeGitHub())
    plugin.value = plugin.collect()
    key = f"m:mini:{fleet['mini'] / 'dirty-work'}"
    for refused in (key, "workspace:mini:w2"):
        with pytest.raises(ValueError, match="Not available for workspaces on Mac mini yet"):
            plugin.resolve(refused)
        with pytest.raises(ValueError, match="Not available for workspaces on Mac mini yet"):
            plugin.open_workspace({"key": refused})
    with pytest.raises(ValueError, match="Not available for workspaces on Mac mini yet"):
        plugin.checkout(key)
    with pytest.raises(ValueError, match="on ghost yet"):
        plugin.resolve("m:ghost:/src/x")
    started = plugin.cleanup({"targets": [{"key": key, "approve": ["dirty"]}]})
    assert started["accepted"]
    deadline = time.time() + 10
    while plugin.snapshot(start=False)["cleanup"]["status"] == "running":
        assert time.time() < deadline
        time.sleep(0.05)
    result = plugin.snapshot(start=False)["cleanup"]["results"][0]
    assert result["status"] == "failed" and "Not available" in result["message"]
    assert (fleet["mini"] / "dirty-work" / "scratch.txt").exists()
    assert not any(
        call[0].startswith("--machine") and "close" in call
        for call in read(fleet["state"])["calls"]
    )


def test_worker_rows_stay_out_of_needs_you(fleet):
    register(fleet["home"], MINI)
    value = {**wso.WorkspaceOverview(fleet["home"], FakeGitHub()).collect(), "enabled": True}
    assert "mini" not in json.dumps(attention.collect(workspace_overview=value)["items"])
    # The same rows from this machine would be cards: a working agent, a missing checkout.
    local = [{k: v for k, v in row.items() if k != "machine"} for row in value["workspaces"]]
    items = attention.collect(workspace_overview={**value, "workspaces": local})["items"]
    assert "workspace:mini:w2" in json.dumps(items)


# The probe in-process -----------------------------------------------------------


def test_probe_inventory_matches_the_local_scan(fleet):
    clone = fleet["local"].parent.parent / "repo"
    opened = str(fleet["local"] / "local-work")
    found = remote_probe.inventory(
        remote_probe.host_git,
        remote_probe.host_session_files,
        remote_probe.host_cwd,
        str(fleet["local"].parent.parent),
        [],
        [],
        [opened, "/nowhere/at/all"],
    )
    assert remote_probe.validate_inventory(json.loads(json.dumps(found))) == found
    assert found["roots"] == [str(clone)]
    [entry] = found["clones"]
    assert (entry["main"], entry["items"]) == pr_workspaces.clone_worktrees(clone)
    assert entry["config"] == pr_workspaces.repo_config(clone)
    state = dict(found["states"][opened])
    assert state == wso.local_state(opened, "local-work")
    assert list(found["states"]) == [opened]  # The clone's own checkout is not opened.
    assert found["paths"] == {
        opened: {"path": opened, "exists": True},
        "/nowhere/at/all": {"path": "/nowhere/at/all", "exists": False},
    }


def test_probe_git_never_runs_repository_hooks_or_fsmonitor(tmp_path):
    clone = tmp_path / "clone"
    make_clone(clone, [])
    marker = tmp_path / "ran"
    script = tmp_path / "monitor.sh"
    script.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
    script.chmod(0o700)
    git("config", "core.fsmonitor", str(script), cwd=clone)
    remote_probe.host_git(clone, "status", "--porcelain")
    assert not marker.exists()
    state = remote_probe.local_state(remote_probe.host_git, str(clone), "main")
    assert state["error"] is None and not marker.exists()


def test_probe_reads_session_working_directories(tmp_path, monkeypatch):
    claude = tmp_path / "claude.jsonl"
    claude.write_text(
        "not json\n"
        + json.dumps({"type": "summary", "cwd": "/no"})
        + "\n"
        + json.dumps({"type": "assistant", "isSidechain": True, "cwd": "/side"})
        + "\n"
        + json.dumps({"type": "user", "cwd": "/w/checkout"})
        + "\n"
    )
    assert remote_probe.host_cwd(claude, "claude") == "/w/checkout"
    codex = tmp_path / "rollout.jsonl"
    codex.write_text(json.dumps({"type": "session_meta", "payload": {"id": "x", "cwd": "/c"}}))
    assert remote_probe.host_cwd(codex, "codex") is None  # Its first line is still being written.
    codex.write_text(codex.read_text() + "\n")
    assert remote_probe.host_cwd(codex, "codex") == "/c"
    assert remote_probe.host_cwd(tmp_path / "missing.jsonl", "codex") is None
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    folder = tmp_path / ".claude" / "accounts" / "work" / "projects" / "-w-checkout"
    folder.mkdir(parents=True)
    (folder / "s.jsonl").write_text("")
    found = list(remote_probe.host_session_files(Path("/w/checkout")))
    assert found == [("claude", (folder / "s.jsonl").resolve())]


def run_main(capsys, *argv):
    remote_probe.main(["-", *argv])
    return json.loads(capsys.readouterr().out)


def encoded(value):
    return base64.b64encode(json.dumps(value).encode()).decode()


def test_probe_entry_checks_protocol_op_and_arguments(capsys, tmp_path):
    assert "protocol mismatch" in run_main(capsys, "babysit-probe-0", "e30=")["error"]
    assert (
        "Unknown probe op"
        in run_main(capsys, remote_probe.PROTOCOL, encoded({"op": "rm"}))["error"]
    )
    bad = {
        "op": "inventory",
        "args": {"src": "~/src", "roots": [], "repo_roots": ["rel"], "opened": []},
    }
    assert "absolute paths" in run_main(capsys, remote_probe.PROTOCOL, encoded(bad))["error"]
    good = {
        "op": "inventory",
        "args": {"src": str(tmp_path), "roots": [], "repo_roots": [], "opened": []},
    }
    answer = run_main(capsys, remote_probe.PROTOCOL, encoded(good))
    assert answer == {
        "protocol": remote_probe.PROTOCOL,
        "result": {"roots": [], "clones": [], "states": {}, "updates": {}, "paths": {}},
    }


@pytest.mark.parametrize(
    "change",
    [
        lambda v: v["roots"].append("relative"),
        lambda v: v["roots"].append("/a\0b"),
        lambda v: v["paths"].update({"/x": {"path": "x", "exists": True}}),
        lambda v: v["states"].update({"/unlisted": {"changes": 0, "unpushed": 0, "error": None}}),
        lambda v: v.update(extra=1),
    ],
)
def test_malformed_probe_answers_are_rejected(change):
    value = {"roots": [], "clones": [], "states": {}, "updates": {}, "paths": {}}
    change(value)
    with pytest.raises(ValueError, match="Malformed probe answer"):
        remote_probe.validate_inventory(value)


@pytest.mark.parametrize(
    "returncode,stdout,message",
    [
        (255, b"", "unreachable"),
        (0, b"x" * (remote_probe.MAX_STDOUT + 1), "exceeded"),
        (0, b"not json", "no JSON"),
        (0, json.dumps({"protocol": "other", "result": {}}).encode(), "another protocol"),
        (0, json.dumps({"protocol": remote_probe.PROTOCOL, "error": "boom"}).encode(), "boom"),
    ],
)
def test_call_bounds_and_checks_the_worker_answer(
    tmp_path, monkeypatch, returncode, stdout, message
):
    register(tmp_path, MINI)
    sent = []

    def run(home, machine, argv, **kwargs):
        sent.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, returncode, stdout, b"e" * 10000)

    monkeypatch.setattr(machines, "run", run)
    with pytest.raises(ValueError, match=message) as error:
        remote_probe.call(tmp_path, machines.resolve(tmp_path, "mini"), "inventory", 5, src="~/s")
    assert len(str(error.value)) < remote_probe.MAX_STDERR + 200
    [(argv, kwargs)] = sent
    assert argv[:5] == ["python3", "-I", "-S", "-", remote_probe.PROTOCOL]
    assert json.loads(base64.b64decode(argv[5])) == {
        "op": "inventory",
        "args": {"src": "~/s"},
        "deadline": 5,
    }
    assert kwargs["max_output"] == remote_probe.MAX_STDOUT
    assert kwargs["input"] == Path(remote_probe.__file__).read_bytes()


# A worker that is buggy, version-skewed or compromised -------------------------


def test_malformed_worker_herdr_never_stops_this_machines_rows(fleet):
    register(fleet["home"], MINI)
    store = read(fleet["state"])
    store["machines"]["Mac mini"] = {
        "workspaces": [
            "not a workspace",
            {"workspace_id": ["w0"]},
            {
                "workspace_id": "w1",
                "label": 5,
                "agent_status": ["unhashable"],
                "worktree": "not a worktree",
            },
            {
                "workspace_id": "w3",
                "label": {"x": 1},
                "agent_status": {"working": True},
                "worktree": {
                    "repo_root": 7,
                    "checkout_path": str(fleet["mini"] / "merged-work"),
                    "repo_name": ["repo"],
                },
            },
        ],
        "agents": [
            42,
            {"workspace_id": "w3", "agent": [1], "agent_status": {}, "pane_id": None},
        ],
        "calls": [],
    }
    fleet["state"].write_text(json.dumps(store))
    plugin = wso.WorkspaceOverview(fleet["home"], FakeGitHub())
    plugin._refresh()
    assert plugin.error is None
    rows = by_key(plugin.value)
    assert rows[str(fleet["local"] / "local-work")]["changes"] == 0
    assert plugin.value["machines"]["mini"]["status"] == "ok"
    detached = rows["workspace:mini:w1"]
    assert (detached["name"], detached["label"], detached["agent_status"]) == ("w1", None, None)
    merged = rows[f"m:mini:{fleet['mini'] / 'merged-work'}"]
    assert merged["workspace_ids"] == ["mini/w3"] and merged["label"] is None
    assert merged["agents"] == [{"agent": None, "status": None, "pane": None}]
    assert "workspace:mini:w0" not in rows


def test_a_worker_whose_rows_cannot_be_built_is_left_stale_alone(fleet, monkeypatch):
    register(fleet["home"], MINI, BOX)
    plugin = wso.WorkspaceOverview(fleet["home"], FakeGitHub())
    plugin._refresh()
    original = wso.WorkspaceOverview.on_machine

    def broken(row, machine):
        if machine.id == "mini":
            raise TypeError("unexpected worker data")
        return original(row, machine)

    monkeypatch.setattr(wso.WorkspaceOverview, "on_machine", staticmethod(broken))
    (fleet["local"] / "local-work" / "new.txt").write_text("changed")
    plugin.next_poll = 0
    plugin._refresh()
    assert plugin.error is None
    rows = by_key(plugin.value)
    assert rows[str(fleet["local"] / "local-work")]["changes"] == 1
    status = plugin.value["machines"]["mini"]
    assert status["status"] == "stale" and "unexpected worker data" in status["error"]
    kept = [row for row in rows.values() if row.get("machine") == "mini"]
    assert len(kept) == 3 and all(row["machine_stale"] for row in kept)
    assert plugin.value["machines"]["box"]["status"] == "ok"
    assert "mini" in plugin.retry


def test_a_worker_scan_runs_within_its_deadline(fleet, monkeypatch):
    register(fleet["home"], MINI)
    machine = machines.resolve(fleet["home"], "mini")
    plugin = wso.WorkspaceOverview(fleet["home"], FakeGitHub())
    timeouts = []
    herdr = wso.herdr
    call = remote_probe.call

    def listed(*args, timeout, **kwargs):
        timeouts.append(("herdr", timeout))
        return herdr(*args, timeout=timeout, **kwargs)

    def probe(home, machine, op, timeout, **args):
        timeouts.append(("probe", timeout))
        return call(home, machine, op, timeout, **args)

    monkeypatch.setattr(wso, "herdr", listed)
    monkeypatch.setattr(remote_probe, "call", probe)
    plugin.scan_machine(machine, time.monotonic() + 50)
    assert [kind for kind, _ in timeouts] == ["herdr", "herdr", "probe"]
    assert all(timeout <= 30 for _, timeout in timeouts[:2]) and timeouts[2][1] <= 50
    timeouts.clear()
    with pytest.raises(subprocess.TimeoutExpired):
        plugin.scan_machine(machine, time.monotonic() - 1)
    assert timeouts == []


def test_each_worker_spends_its_share_of_the_github_budget(fleet, monkeypatch):
    register(fleet["home"], MINI, BOX)
    monkeypatch.setattr(wso, "CALL_BUDGET", 9)
    shares = []
    original = wso.WorkspaceOverview.machine_rows

    def counted(self, machine, spaces, agents, found, jobs, budget, warnings):
        limit = budget.limit
        try:
            return original(self, machine, spaces, agents, found, jobs, budget, warnings)
        finally:
            shares.append((machine.id, limit, budget.calls))

    monkeypatch.setattr(wso.WorkspaceOverview, "machine_rows", counted)
    value = wso.WorkspaceOverview(fleet["home"], FakeGitHub()).collect()
    local = value["calls"] - sum(calls for _, _, calls in shares)
    (first, first_limit, first_calls), (_, second_limit, _) = shares
    assert first == "mini" and first_limit == (9 - local) // 2
    assert second_limit == 9 - local - first_calls


def test_worker_inventories_are_capped_and_say_so(monkeypatch):
    monkeypatch.setattr(wso, "MAX_REMOTE_CLONES", 2)
    monkeypatch.setattr(wso, "MAX_REMOTE_ITEMS", 3)
    monkeypatch.setattr(wso, "MAX_REMOTE_ROWS", 1)

    def item(path):
        return {"path": path, "common": "/c/.git", "branch": "b", "sha": "", "remotes": []}

    clones = [
        {"main": item(f"/c{n}"), "items": [item(f"/c{n}"), item(f"/c{n}/a"), item(f"/c{n}/b")]}
        for n in range(3)
    ]
    found = {
        "roots": [],
        "clones": clones,
        "states": {"/c0/a": {}, "/c1": {}, "/c2/a": {}},
        "updates": {"/c0/a": 1.0, "/c2/a": 2.0},
        "paths": {},
    }
    notes = []
    capped = wso.WorkspaceOverview.capped(found, notes, "Mac mini")
    assert [[i["path"] for i in clone["items"]] for clone in capped["clones"]] == [
        ["/c0", "/c0/a", "/c0/b"]
    ]
    assert capped["states"] == {"/c0/a": {}} and capped["updates"] == {"/c0/a": 1.0}
    assert notes == ["Mac mini: inventory truncated to its first 1 checkouts"]


def test_an_oversized_worker_never_blocks_saving_this_machines_cache(fleet, monkeypatch):
    register(fleet["home"], MINI)
    plugin = wso.WorkspaceOverview(fleet["home"], FakeGitHub())
    plugin.value = {**plugin.collect(), "synced_at": time.time()}
    local = [row for row in plugin.value["workspaces"] if "machine" not in row]
    worker = {**plugin.value["workspaces"][-1], "machine": "mini", "label": "x" * 100000}
    plugin.value["workspaces"].append(worker)
    monkeypatch.setattr(wso, "MAX_STATE", 50000)
    plugin.save()
    assert plugin.error is None
    cache = json.loads((fleet["home"] / "experiments" / "workspaces" / "cache.json").read_text())
    assert cache["value"]["workspaces"] == local


def test_base_repositories_of_gone_workers_and_clones_are_pruned(fleet):
    register(fleet["home"], MINI)
    plugin = wso.WorkspaceOverview(fleet["home"], FakeGitHub())
    root = str(fleet["hosts"] / "mini.test" / "src" / "repo")
    entry = {"repo": BASE, "origin_owner": "fork", "checked_at": time.time()}
    plugin.bases = {"m:ghost:/src/x": entry, "m:mini:/src/gone": entry, "/local/clone": entry}
    plugin.collect()
    assert set(plugin.bases) >= {"/local/clone", f"m:mini:{root}"}
    assert not {"m:ghost:/src/x", "m:mini:/src/gone"} & set(plugin.bases)


def test_remote_slugs_never_name_dot_repositories():
    assert remote_probe.remote_slug("https://github.com/owner/..") is None
    assert remote_probe.remote_slug("git@github.com:owner/..git") is None
    assert remote_probe.remote_slug("https://github.com/owner/.") is None
    assert remote_probe.remote_slug("https://github.com/Owner/.dots.git") == "owner/.dots"


@pytest.mark.parametrize(
    "state",
    [
        {"changes": -1, "unpushed": 0, "error": None},
        {"changes": 0.5, "unpushed": 0, "error": None},
        {"changes": 0, "unpushed": remote_probe.MAX_UNPUSHED + 1, "error": None},
        {"changes": 0, "unpushed": 0, "error": None, "created_at": float("nan")},
        {"changes": 0, "unpushed": 0, "error": None, "updated_at": float("inf")},
        {"changes": 0, "unpushed": 0, "error": None, "updated_at": -5},
        {"changes": 0, "unpushed": 0, "error": None, "updated_at": 1e300},
        {"changes": 0, "unpushed": 0, "error": "x" * (remote_probe.MAX_TEXT + 1)},
    ],
)
def test_implausible_worker_states_are_rejected(state):
    item = {"path": "/c", "common": "/c/.git", "branch": "main", "sha": "", "remotes": []}
    value = {
        "roots": [],
        "clones": [{"main": item, "items": [item], "config": {}}],
        "states": {"/c": state},
        "updates": {},
        "paths": {},
    }
    with pytest.raises(ValueError, match="Malformed probe answer"):
        remote_probe.validate_inventory(value)


def test_an_item_listed_by_two_clones_is_rejected():
    item = {"path": "/c", "common": "/c/.git", "branch": "main", "sha": "", "remotes": []}
    other = {**item, "path": "/d"}
    value = {
        "roots": [],
        "clones": [
            {"main": item, "items": [item], "config": {}},
            {"main": other, "items": [other, item], "config": {}},
        ],
        "states": {},
        "updates": {},
        "paths": {},
    }
    with pytest.raises(ValueError, match="item listed twice"):
        remote_probe.validate_inventory(value)


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_numbers_in_an_answer_are_refused(tmp_path, monkeypatch, constant):
    register(tmp_path, MINI)
    result = '{"roots": [], "clones": [], "states": {}, "updates": {"/x": %}, "paths": {}}'
    answer = f'{{"protocol": "{remote_probe.PROTOCOL}", "result": {result}}}'
    stdout = answer.replace("%", constant).encode()
    monkeypatch.setattr(
        machines,
        "run",
        lambda home, m, argv, **kw: subprocess.CompletedProcess(argv, 0, stdout, b""),
    )
    with pytest.raises(ValueError, match="no JSON.*non-finite"):
        remote_probe.call(tmp_path, machines.resolve(tmp_path, "mini"), "inventory", 5)


def test_a_streaming_worker_is_cut_off_at_the_answer_cap(fleet, monkeypatch):
    pidfile = fleet["hosts"] / "streamer.pid"
    streamer = fleet["hosts"] / "streamer"
    streamer.write_text(f"#!/bin/sh\necho $$ > '{pidfile}'\nexec yes babysit\n")
    streamer.chmod(0o700)
    register(fleet["home"], {**MINI, "python": str(streamer)})
    monkeypatch.setattr(remote_probe, "MAX_STDOUT", 1024 * 1024)
    started = time.monotonic()
    with pytest.raises(ValueError, match="exceeded 1048576 bytes"):
        remote_probe.call(fleet["home"], machines.resolve(fleet["home"], "mini"), "inventory", 60)
    assert time.monotonic() - started < 30
    pid = int(pidfile.read_text())
    deadline = time.monotonic() + 5
    while True:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        assert time.monotonic() < deadline, "the streaming worker command survived"
        time.sleep(0.05)


def test_the_probe_stops_at_its_deadline(capsys, monkeypatch):
    monkeypatch.setitem(remote_probe.OPS, "inventory", lambda **args: time.sleep(10))
    request = {"op": "inventory", "args": {}, "deadline": 0.2}
    started = time.monotonic()
    code = remote_probe.main(["-", remote_probe.PROTOCOL, encoded(request)])
    assert code == remote_probe.EXPIRED and time.monotonic() - started < 5
    assert "deadline expired" in json.loads(capsys.readouterr().out)["error"]
    assert remote_probe.DEADLINE is None
    for bad in (0, -1, True, "5", remote_probe.MAX_DEADLINE + 1):
        answer = run_main(capsys, remote_probe.PROTOCOL, encoded({**request, "deadline": bad}))
        assert "deadline must be" in answer["error"]


def test_expiry_while_an_error_is_being_reported_still_exits_expired(capsys, monkeypatch):
    class Slow(Exception):
        def __str__(self):
            time.sleep(10)  # The alarm fires while the probe formats this error.
            return "slow"

    def failing(**args):
        raise Slow

    monkeypatch.setitem(remote_probe.OPS, "inventory", failing)
    request = {"op": "inventory", "args": {}, "deadline": 0.2}
    code = remote_probe.main(["-", remote_probe.PROTOCOL, encoded(request)])
    assert code == remote_probe.EXPIRED
    assert json.loads(capsys.readouterr().out)["error"] == "Probe deadline expired"
    # The next request starts with no memory of that expiry.
    answer = run_main(capsys, remote_probe.PROTOCOL, encoded({**request, "deadline": 0}))
    assert "deadline must be" in answer["error"]


def test_an_expired_probe_stops_its_git_and_exits_non_zero(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    pids = tmp_path / "git.pids"
    git_stub = bin_dir / "git"
    git_stub.write_text(f"#!/bin/sh\necho $$ >> '{pids}'\nexec sleep 60\n")
    git_stub.chmod(0o700)
    (tmp_path / "src" / "repo" / ".git").mkdir(parents=True)
    request = {
        "op": "inventory",
        "args": {"src": str(tmp_path / "src"), "roots": [], "repo_roots": [], "opened": []},
        "deadline": 3,
    }
    started = time.monotonic()
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-", remote_probe.PROTOCOL, encoded(request)],
        input=Path(remote_probe.__file__).read_bytes(),
        capture_output=True,
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"},
        timeout=30,
    )
    assert result.returncode == remote_probe.EXPIRED, result.stderr
    assert time.monotonic() - started < 15
    assert "deadline expired" in json.loads(result.stdout)["error"]
    for pid in map(int, pids.read_text().split()):
        deadline = time.monotonic() + 5
        while True:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            assert time.monotonic() < deadline, "the probe's Git survived its deadline"
            time.sleep(0.05)


def test_a_git_timeout_at_the_deadline_is_reported_as_expiry(tmp_path, monkeypatch):
    """subprocess's own timeout can fire before the alarm does; it is the same expiry."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "git").write_text("#!/bin/sh\nexec sleep 60\n")
    (bin_dir / "git").chmod(0o700)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setattr(remote_probe, "DEADLINE", time.monotonic() + 0.3)
    started = time.monotonic()
    with pytest.raises(remote_probe.Expired):
        remote_probe.host_git(tmp_path, "status")
    assert time.monotonic() - started < 5


def fifo_reflog(clone):
    log = clone / ".git" / "logs" / "HEAD"
    log.unlink()
    os.mkfifo(log)


def test_a_fifo_reflog_never_holds_a_scan(tmp_path):
    clone = tmp_path / "src" / "repo"
    make_clone(clone, [])
    fifo_reflog(clone)
    done = []
    thread = threading.Thread(target=lambda: done.append(remote_probe.checkout_times(clone)))
    thread.daemon = True
    thread.start()
    thread.join(5)
    assert done and done[0]["updated_at"]
    # A worker's probe answers within its deadline, not when the FIFO is written.
    request = {
        "op": "inventory",
        "args": {
            "src": str(tmp_path / "src"),
            "roots": [],
            "repo_roots": [],
            "opened": [str(clone)],
        },
        "deadline": 20,
    }
    started = time.monotonic()
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-", remote_probe.PROTOCOL, encoded(request)],
        input=Path(remote_probe.__file__).read_bytes(),
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert time.monotonic() - started < 15
    assert json.loads(result.stdout)["result"]["states"][str(clone.resolve())]["error"] is None


def test_a_reflog_without_newlines_is_read_only_in_part(tmp_path):
    import tracemalloc

    clone = tmp_path / "repo"
    make_clone(clone, [])
    (clone / ".git" / "logs" / "HEAD").write_bytes(b"x" * (32 * 1024 * 1024))
    tracemalloc.start()
    try:
        times = remote_probe.checkout_times(clone)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert times["updated_at"] and peak < 4 * 1024 * 1024


def test_a_stuck_thread_never_keeps_the_probe_alive(tmp_path):
    wrapper = (
        "import runpy, sys, threading, time\n"
        "threading.Thread(target=time.sleep, args=(600,)).start()\n"
        "sys.argv = ['-', *sys.argv[1:]]\n"
        f"runpy.run_path({remote_probe.__file__!r}, run_name='__main__')\n"
    )
    started = time.monotonic()
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c", wrapper, "babysit-probe-0", "e30="],
        capture_output=True,
        timeout=30,
    )
    assert time.monotonic() - started < 15
    assert "protocol mismatch" in json.loads(result.stdout)["error"]


def test_the_workers_python_never_runs_site_packages_hooks(fleet, tmp_path):
    venv = tmp_path / "venv"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True)
    marker = tmp_path / "site-ran"
    [site] = venv.glob("lib/python*/site-packages")
    (site / "planted.pth").write_text(f"import os; open({str(marker)!r}, 'w').close()\n")
    register(fleet["home"], {**MINI, "python": str(venv / "bin" / "python")})
    machine = machines.resolve(fleet["home"], "mini")
    found = remote_probe.call(
        fleet["home"], machine, "inventory", 30, src="~/src", roots=[], repo_roots=[], opened=[]
    )
    assert found["clones"] and not marker.exists()


def test_this_machines_rows_are_published_before_a_slow_worker_answers(fleet, monkeypatch):
    register(fleet["home"], MINI)
    release = threading.Event()

    def slow(self, machine, deadline=None):
        release.wait(10)
        raise ValueError("worker gone")

    monkeypatch.setattr(wso.WorkspaceOverview, "scan_machine", slow)
    plugin = wso.WorkspaceOverview(fleet["home"], FakeGitHub())
    plugin.loading = True
    thread = threading.Thread(target=plugin._refresh, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 8
        while not plugin.value.get("synced_at") and time.monotonic() < deadline:
            time.sleep(0.05)
        assert str(fleet["local"] / "local-work") in by_key(plugin.value)
        assert plugin.loading
    finally:
        release.set()
        thread.join(10)
    assert plugin.value["machines"]["mini"]["status"] == "unreachable"
    assert not plugin.loading


def test_a_failing_worker_backs_off_longer_each_time(fleet, monkeypatch):
    register(fleet["home"], MINI)
    monkeypatch.setenv("FAKE_SSH_DOWN", "mini.test")
    plugin = wso.WorkspaceOverview(fleet["home"], FakeGitHub())

    def backoff():
        """How long after its failed scan the worker waits, in rescan intervals."""
        plugin.retry["mini"] = 0  # Due again, as once its back-off has passed.
        before = time.time()
        plugin.value = plugin.collect()
        after = time.time()
        waits = [(plugin.retry["mini"] - at) / wso.INTERVAL for at in (after, before)]
        return next(n for n in (2, 4, 8, 16) if waits[0] <= n <= waits[1])

    assert [backoff() for _ in range(5)] == [2, 4, 8, 16, 16]
    monkeypatch.delenv("FAKE_SSH_DOWN")
    plugin.retry["mini"] = 0
    plugin.value = plugin.collect()
    assert "mini" not in plugin.retry and plugin.value["machines"]["mini"]["status"] == "ok"
    monkeypatch.setenv("FAKE_SSH_DOWN", "mini.test")
    assert backoff() == 2


def test_a_worker_row_links_its_own_collie_when_the_registry_names_one(fleet):
    register(fleet["home"], {**MINI, "collie_url": "https://mini.example.ts.net:8787"})
    value = wso.WorkspaceOverview(fleet["home"], FakeGitHub()).collect()
    merged = by_key(value)[f"m:mini:{fleet['mini'] / 'merged-work'}"]
    assert merged["workspace_url"] == "https://mini.example.ts.net:8787/space/w1"
    assert by_key(value)["workspace:mini:w2"]["workspace_url"] == (
        "https://mini.example.ts.net:8787/space/w2"
    )
