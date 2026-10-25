"""Worker machines in the workspace inventory: the probe in-process and over the fake ssh.

Each fake host is a directory under ``<tmp>/hosts/<ssh target>`` that the fake ssh uses as
HOME, with its own clones; its herdr workspaces live in the fake herdr's per-label state.
"""

import base64
import json
import os
import shutil
import subprocess
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
    monkeypatch.setattr(
        wso.WorkspaceOverview, "scan_machine", lambda self, machine: release.wait(10)
    )
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
    assert argv[:4] == ["python3", "-I", "-", remote_probe.PROTOCOL]
    assert json.loads(base64.b64decode(argv[4])) == {"op": "inventory", "args": {"src": "~/s"}}
    assert kwargs["input"] == Path(remote_probe.__file__).read_bytes()
