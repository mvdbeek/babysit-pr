"""Diff and Transcript views of a worker's checkout: probe ops in-process and over fake ssh.

The fake worker is ``<tmp>/hosts/mini.test``, which the fake ssh uses as HOME: its clone,
its Claude and Codex session stores, and its herdr workspaces in the fake herdr's state
for the label ``Mac mini``. Its paths exist on this filesystem too, so each remote answer
can be compared with what this machine's own viewer reads from the same checkout.
"""

import copy
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

import dashboard
import machines
import pytest
import remote_probe
import workspace_overview as wso
import workspace_viewer as wv
from playwright.sync_api import expect
from test_remote_inventory import MINI, git, make_clone, read, register
from test_workspace_overview import FakeGitHub, request
from test_workspace_overview_browser import row

CLAUDE_SID = str(uuid.UUID(int=1))
CODEX_SID = str(uuid.UUID(int=2))
SIBLING_SID = str(uuid.UUID(int=3))
ELSEWHERE_SID = str(uuid.UUID(int=4))
# Git quotes a name with a double quote or backslash in it whatever core.quotePath says.
HOSTILE = "it's $(touch PWNED) `touch PWNED`; y & z"


def claude_turn(kind, cwd, text):
    content = text if kind == "user" else [{"type": "text", "text": text}]
    return {"type": kind, "cwd": str(cwd), "message": {"role": kind, "content": content}}


def jsonl(*records):
    return "".join(json.dumps(record) + "\n" for record in records)


def claude_file(host, cwd, sid):
    folder = host / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(cwd))
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{sid}.jsonl"


def codex_file(host, cwd, sid, prompt):
    day = host / ".codex" / "sessions" / "2026" / "10" / "25"
    day.mkdir(parents=True, exist_ok=True)
    path = day / f"rollout-2026-10-25T10-00-00-{sid}.jsonl"
    path.write_text(
        jsonl(
            {"type": "session_meta", "payload": {"id": sid, "cwd": str(cwd)}},
            {"type": "event_msg", "payload": {"type": "user_message", "message": prompt}},
        )
    )
    return path


@pytest.fixture
def short_tmp(monkeypatch):
    """ssh control sockets need a short directory; never the real /tmp's shared one."""
    path = tempfile.mkdtemp(prefix="bs", dir="/tmp")
    monkeypatch.setattr(tempfile, "tempdir", path)
    machines.control_dir.cache_clear()
    yield Path(path)
    machines.control_dir.cache_clear()
    shutil.rmtree(path)


@pytest.fixture
def worker(tmp_path, fake_tools, short_tmp, monkeypatch):
    """This machine with one clone, and the worker Mac mini with a checkout and sessions."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    local = make_clone(tmp_path / "src" / "repo", ["local-work"]) / "local-work"
    (local / "local.txt").write_text("here\n")
    host = tmp_path / "hosts" / "mini.test"
    worktrees = make_clone(host / "src" / "repo", ["feature", "feature-2"])
    checkout = worktrees / "feature"
    (checkout / HOSTILE).write_text("one\ntwo\n")
    git("add", "-A", cwd=checkout)
    git("commit", "-q", "-m", "Hostile name", cwd=checkout)
    (checkout / "notes.txt").write_text("not yet added\n")
    claude = claude_file(host, checkout, CLAUDE_SID)
    claude.write_text(
        jsonl(claude_turn("user", checkout, "Fix it"), claude_turn("assistant", checkout, "Done"))
    )
    # Listed by folder name beside the checkout, but recorded in another checkout.
    sibling = worktrees / "feature-2"
    claude_file(host, sibling, SIBLING_SID).write_text(jsonl(claude_turn("user", sibling, "No")))
    codex_file(host, checkout / "sub", CODEX_SID, "Codex task")
    codex_file(host, host / "elsewhere", ELSEWHERE_SID, "Not here")
    store = read(fake_tools)
    store["machines"] = {
        "Mac mini": {
            "workspaces": [
                {
                    "workspace_id": "w1",
                    "label": "feature",
                    "agent_status": "idle",
                    "worktree": {
                        "repo_root": str(host / "src" / "repo"),
                        "checkout_path": str(checkout),
                    },
                }
            ],
            "agents": [],
            "calls": [],
        }
    }
    fake_tools.write_text(json.dumps(store))
    home = short_tmp / "state"
    home.mkdir()
    register(home, MINI)
    return SimpleNamespace(
        home=home,
        host=host,
        checkout=checkout,
        claude=claude,
        local=local,
        machine=machines.load(home)["mini"],
        state=fake_tools,
    )


def ssh_calls(worker):
    return len(read(worker.state).get("ssh", []))


# The probe ops in this process --------------------------------------------------


def test_probe_ops_answer_what_the_local_viewer_reads(worker, monkeypatch):
    path = str(worker.checkout)
    for scope in ("branch", "uncommitted"):
        assert remote_probe._host_diff(path, scope, None) == wv.diff(worker.checkout, scope)
    assert remote_probe._host_bases(path) == wv.bases(worker.checkout)
    found = remote_probe._host_diff(path, "branch", None)
    assert [f["path"] for f in found["files"]] == [HOSTILE]
    assert found["untracked"] == ["notes.txt"] and found["commits"][0]["subject"] == "Hostile name"
    (worker.checkout / "deep").mkdir()
    with pytest.raises(ValueError, match="top of a Git checkout"):
        remote_probe._host_diff(str(worker.checkout / "deep"), "branch", None)
    with pytest.raises(ValueError, match="absolute"):
        remote_probe._host_diff("feature", "branch", None)
    with pytest.raises(ValueError, match="listed base"):
        remote_probe._host_diff(path, "branch", "HEAD")
    monkeypatch.setenv("HOME", str(worker.host))
    listed = remote_probe._host_sessions(path)
    assert {s["id"] for s in listed} == {CLAUDE_SID, CODEX_SID}
    claude = next(s for s in listed if s["agent"] == "claude")
    assert claude["file"] == str(worker.claude.resolve()) and claude["title"] == "Fix it"
    assert claude["cwd_exists"] and claude["account"] == "default"
    assert claude["home"] == str((worker.host / ".claude").resolve())
    codex = next(s for s in listed if s["agent"] == "codex")
    assert codex["cwd"] == str(worker.checkout / "sub") and not codex["cwd_exists"]
    assert codex["home"] is None and codex["account"] is None
    assert remote_probe.validate_sessions(listed) == listed


def test_probe_tail_reads_complete_lines_and_refuses_other_files(worker, monkeypatch):
    monkeypatch.setenv("HOME", str(worker.host))
    file = str(worker.claude)
    first = len(worker.claude.read_text().split("\n", 1)[0]) + 1
    with worker.claude.open("a") as stream:
        stream.write('{"type": "user", "partial')  # Still being written.
    answer = remote_probe.validate_tail(remote_probe._host_tail(file, None, 0, 1 << 20))
    inode = worker.claude.stat().st_ino
    assert answer["inode"] == inode and answer["offset"] == 0
    assert answer["data"] == worker.claude.read_bytes()[: answer["data"].rfind(b"\n") + 1]
    assert answer["data"].endswith(b'"Done"}]}}\n')
    # From a known position; a stale inode or a shrunk file starts over.
    assert remote_probe._host_tail(file, inode, first, 1 << 20)["offset"] == first
    assert remote_probe._host_tail(file, inode + 1, first, 1 << 20)["offset"] == 0
    assert remote_probe._host_tail(file, inode, 1 << 30, 1 << 20)["offset"] == 0
    # A full read inside one long line comes back whole, to be joined with the next.
    piece = remote_probe.validate_tail(remote_probe._host_tail(file, inode, 0, 10))
    assert piece["data"] == worker.claude.read_bytes()[:10]
    outside = worker.host / "outside.jsonl"
    outside.write_text("{}\n")
    text = worker.claude.with_suffix(".txt")
    text.write_text("{}\n")
    link = worker.claude.with_name(f"{SIBLING_SID}x.jsonl")
    link.symlink_to(outside)
    for path in ("/etc/passwd", str(outside), str(text), str(link)):
        with pytest.raises(ValueError, match="Not a session transcript"):
            remote_probe._host_tail(path, None, 0, 100)
    for args in [("rel.jsonl", None, 0, 1), (file, -1, 0, 1), (file, None, 0, 0)]:
        with pytest.raises(ValueError):
            remote_probe._host_tail(*args)
    with pytest.raises(ValueError):
        remote_probe._host_tail(file, None, 0, remote_probe.MAX_TAIL + 1)


@pytest.mark.parametrize(
    "answer",
    [
        {"inode": 1, "size": 2, "offset": 0, "data": "not base64!"},
        {"inode": 1, "size": 2, "offset": -1, "data": ""},
        {"inode": 1, "size": 2, "offset": 0, "data": "", "extra": 1},
    ],
)
def test_malformed_tail_answers_are_refused(answer):
    with pytest.raises(ValueError, match="Malformed probe answer"):
        remote_probe.validate_tail(answer)


def test_malformed_diff_and_session_answers_are_refused(worker):
    good = wv.diff(worker.checkout)
    assert remote_probe.validate_diff(copy.deepcopy(good)) == good
    for change in (
        {"scope": "everything"},
        {"commits": [{"sha": 1}]},
        {"files": [{**good["files"][0], "status": "exploded"}]},
        {"files": [{**good["files"][0], "lines": ["x"] * (remote_probe.MAX_FILE_LINES + 1)}]},
        {"untracked": ["x"] * (remote_probe.MAX_UNTRACKED + 1)},
        {"added": -1},
    ):
        with pytest.raises(ValueError, match="Malformed probe answer"):
            remote_probe.validate_diff({**good, **change})
    session = {
        "id": CLAUDE_SID,
        "agent": "claude",
        "cwd": "/w",
        "cwd_exists": True,
        "title": None,
        "updated": 1.0,
        "size": 1,
        "file": "/h/.claude/projects/-w/a.jsonl",
        "home": "/h/.claude",
        "account": None,
    }
    assert remote_probe.validate_sessions([session])
    for change in ({"id": "../x"}, {"file": "relative.jsonl"}, {"file": "/h/a.txt"}):
        with pytest.raises(ValueError, match="Malformed probe answer"):
            remote_probe.validate_sessions([{**session, **change}])


# Over the fake ssh ---------------------------------------------------------------


def test_remote_diff_reads_hostile_names_verbatim_without_a_shell(worker):
    for scope in ("uncommitted", "branch"):
        found = wv.remote_diff(worker.home, worker.machine, str(worker.checkout), scope)
        assert found == wv.diff(worker.checkout, scope)
    assert [f["path"] for f in found["files"]] == [HOSTILE]
    commands = [call["command"] for call in read(worker.state)["ssh"]]
    assert commands and all(HOSTILE not in command for command in commands)
    assert not list(worker.host.parent.parent.rglob("PWNED"))
    bases = remote_probe.call(worker.home, worker.machine, "bases", 30, path=str(worker.checkout))
    assert bases == wv.bases(worker.checkout)


def test_remote_diff_keeps_the_viewer_caps(worker):
    checkout = worker.checkout
    (checkout / "big.txt").write_text("".join(f"line {i}\n" for i in range(4100)))
    (checkout / "huge.txt").write_text(("x" * 99 + "\n") * 33000)
    git("add", "big.txt", "huge.txt", cwd=checkout)
    # 205 more commits on the branch, made in one fast-import rather than 205 commits.
    stream = "".join(
        f"commit refs/heads/feature\ncommitter F <f@example.test> {1700000000 + i} +0000\n"
        f"data 4\nc{i:03}\n" + ("from refs/heads/feature^0\n" if i == 0 else "") + "\n"
        for i in range(205)
    )
    subprocess.run(
        ["git", "-C", str(checkout), "fast-import", "--quiet"],
        input=stream,
        text=True,
        check=True,
    )
    found = wv.remote_diff(worker.home, worker.machine, str(checkout))
    assert len(found["commits"]) == remote_probe.MAX_COMMITS
    big = next(f for f in found["files"] if f["path"] == "big.txt")
    assert len(big["lines"]) == remote_probe.MAX_FILE_LINES and big["truncated"]
    assert found["truncated"] and found["files"][-1]["truncated"]
    assert sum(len(line) + 1 for f in found["files"] for line in f["lines"]) < remote_probe.MAX_DIFF


def test_remote_sessions_are_scoped_to_the_checkout_and_cached(worker):
    listed = wv.remote_sessions(worker.home, worker.machine, str(worker.checkout))
    assert [s["id"] for s in listed] == sorted(
        [CLAUDE_SID, CODEX_SID],
        key=lambda sid: next(s["updated"] for s in listed if s["id"] == sid),
        reverse=True,
    )
    claude = next(s for s in listed if s["id"] == CLAUDE_SID)
    assert claude["_file"] == str(worker.claude.resolve()) and claude["_cwd_exists"]
    assert claude["claude_account"] == "default" and claude["title"] == "Fix it"
    codex = next(s for s in listed if s["id"] == CODEX_SID)
    assert codex["title"] == "Codex task" and not codex["_cwd_exists"]
    assert "claude_account" not in codex
    before = ssh_calls(worker)
    assert wv.remote_sessions(worker.home, worker.machine, str(worker.checkout)) == listed
    assert ssh_calls(worker) == before


def entries(value):
    return [entry["text"] for entry in value["entries"]]


def test_remote_transcript_follows_appends_truncation_and_replacement(worker, monkeypatch):
    # Small reads: several tail calls per update, and one line longer than any of them.
    monkeypatch.setattr(wv, "CHUNK", 64)
    root = str(worker.checkout)
    value = wv.remote_transcript(worker.home, worker.machine, root, CLAUDE_SID)
    assert entries(value) == ["Fix it", "Done"] and value["session"]["id"] == CLAUDE_SID
    assert "_file" not in value["session"] and all("_file" not in s for s in value["sessions"])
    long = "y" * 300
    with worker.claude.open("a") as stream:
        stream.write(json.dumps(claude_turn("assistant", worker.checkout, long)) + "\n")
        stream.write('{"type": "assistant", "unfinished')
    value = wv.remote_transcript(worker.home, worker.machine, root, CLAUDE_SID, after=2)
    assert value["start"] == 2 and entries(value) == [long] and value["total"] == 3
    with worker.claude.open("a") as stream:
        stream.write('": 1}\n' + json.dumps(claude_turn("user", worker.checkout, "More")) + "\n")
    value = wv.remote_transcript(worker.home, worker.machine, root, CLAUDE_SID, after=3)
    assert entries(value) == ["More"]
    worker.claude.write_text(jsonl(claude_turn("user", worker.checkout, "Fix it")))
    value = wv.remote_transcript(worker.home, worker.machine, root, CLAUDE_SID)
    assert entries(value) == ["Fix it"]
    replacement = worker.claude.with_name("replacement")
    replacement.write_text(
        jsonl(*(claude_turn("user", worker.checkout, f"New {i}") for i in range(3)))
    )
    os.replace(replacement, worker.claude)
    value = wv.remote_transcript(worker.home, worker.machine, root, CLAUDE_SID)
    assert entries(value) == ["New 0", "New 1", "New 2"]
    codex = wv.remote_transcript(worker.home, worker.machine, root, CODEX_SID)
    assert entries(codex) == ["Codex task"]
    newest = wv.remote_transcript(worker.home, worker.machine, root)
    assert newest["session"]["id"] == newest["sessions"][0]["id"]
    for session, error in [
        (SIBLING_SID, "not recorded"),
        (ELSEWHERE_SID, "not recorded"),
        ("../x", "listed session"),
    ]:
        with pytest.raises(ValueError, match=error):
            wv.remote_transcript(worker.home, worker.machine, root, session)


def test_remote_tail_refuses_files_outside_the_session_stores(worker):
    outside = worker.host / "outside.jsonl"
    outside.write_text("{}\n")
    for file in ("/etc/passwd", str(outside), str(worker.checkout / HOSTILE)):
        with pytest.raises(ValueError, match="Mac mini probe failed.*Not a session transcript"):
            remote_probe.call(
                worker.home, worker.machine, "tail", 30, file=file, inode=None, offset=0, limit=10
            )


def test_an_unreachable_worker_names_itself_and_leaves_local_views_alone(worker, monkeypatch):
    monkeypatch.setenv("FAKE_SSH_DOWN", "mini.test")
    with pytest.raises(ValueError, match=r"Mac mini probe failed \(unreachable\)"):
        wv.remote_diff(worker.home, worker.machine, str(worker.checkout))
    with pytest.raises(ValueError, match="Mac mini"):
        wv.remote_transcript(worker.home, worker.machine, str(worker.checkout))
    assert wv.diff(worker.local)["untracked"] == ["local.txt"]
    monkeypatch.setenv("FAKE_HERDR_DOWN", "Mac mini")
    with pytest.raises(ValueError, match="Mac mini did not list its workspaces"):
        wv.remote_checkout(worker.machine, "w1")


# Through the dashboard -------------------------------------------------------------


@pytest.fixture
def dashboard_site(worker):
    """A dashboard listing this machine's row and the worker's, with its views live."""
    directory = worker.home / "experiments" / "workspaces"
    directory.mkdir(parents=True)
    (directory / "config.json").write_text(json.dumps({"enabled": True}))
    plugin = wso.WorkspaceOverview(worker.home, FakeGitHub())
    plugin.next_poll = float("inf")
    remote = row(
        "feature",
        key=f"m:mini:{worker.checkout}",
        path=str(worker.checkout),
        machine="mini",
        machine_label="Mac mini",
        machine_stale=False,
        workspace_ids=["mini/w1"],
        removable=False,
    )
    local = row("local-work", key=str(worker.local), path=str(worker.local))
    plugin.value = {"workspaces": [local, remote], "warnings": [], "synced_at": time.time()}
    plugin.collect = lambda **kwargs: copy.deepcopy(plugin.value)
    with dashboard.DashboardServer(worker.home, 0, workspace_overview=plugin) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        yield server.server_port, remote
        server.shutdown()
        thread.join(3)


def test_views_resolve_worker_rows_and_ids_on_the_server(worker, dashboard_site, monkeypatch):
    port, remote = dashboard_site
    key = quote(remote["key"], safe="")
    status, value = request(port, f"/api/workspace-diff?key={key}")
    assert status == 200 and [f["path"] for f in value["files"]] == [HOSTILE]
    status, value = request(port, "/api/workspace-transcript?workspace=mini%2Fw1")
    assert status == 200 and value["session"]["id"] in {CLAUDE_SID, CODEX_SID}
    status, value = request(port, f"/api/workspace-transcript?key={key}&session={CLAUDE_SID}")
    assert status == 200 and entries(value) == ["Fix it", "Done"]
    assert ("mini", "w1") in wv._checkouts
    for path, error in [
        (f"/api/workspace-diff?key={quote('m:mini:/etc', safe='')}", "Unknown workspace"),
        ("/api/workspace-diff?key=m%3Abox%3A%2Fsrc", "Unknown workspace"),
        ("/api/workspace-diff?workspace=box%2Fw1", "Unknown machine box"),
        ("/api/workspace-diff?workspace=mini%2Fw9", "not open"),
        (f"/api/workspace-agents?key={key}", "Not available for workspaces on Mac mini yet"),
        ("/api/workspace-agents?workspace=mini%2Fw1", "cannot be opened"),
    ]:
        status, value = request(port, path)
        assert status == 400 and error in value["error"], path
    monkeypatch.setenv("FAKE_SSH_DOWN", "mini.test")
    status, value = request(port, f"/api/workspace-diff?key={key}")
    assert status == 400 and "Mac mini probe failed (unreachable)" in value["error"]
    status, value = request(port, f"/api/workspace-diff?key={quote(str(worker.local), safe='')}")
    assert status == 200 and value["untracked"] == ["local.txt"]


@pytest.mark.browser
def test_a_worker_row_opens_its_diff_and_transcript_read_only(page, worker, dashboard_site):
    port, _ = dashboard_site
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(f"http://127.0.0.1:{port}/#workspaces")
    line = page.locator("#ws-list tr").filter(has=page.get_by_text("feature", exact=True))
    expect(line.get_by_role("button", name="Open workspace", exact=True)).to_be_disabled()
    line.get_by_role("button", name="Diff", exact=True).click()
    viewer = page.locator("#ws-viewer")
    expect(viewer.locator("#ws-viewer-title")).to_have_text("feature")
    expect(viewer.locator("#ws-viewer-content")).to_contain_text(HOSTILE)
    notice = viewer.locator("#ws-message-readonly")
    expect(notice).to_have_text("Not available for workspaces on Mac mini yet")
    expect(viewer.locator("#ws-message")).to_be_hidden()
    viewer.get_by_role("tab", name="Transcript").click()
    # The newest session the worker recorded here, the Codex one.
    expect(viewer.locator("#ws-viewer-content")).to_contain_text("Codex task")
    expect(viewer.locator("#ws-viewer-error")).to_have_text("")
    viewer.get_by_role("button", name="Close workspace viewer").click()
    # This machine's row keeps its composer.
    local = page.locator("#ws-list tr").filter(has=page.get_by_text("local-work", exact=True))
    local.get_by_role("button", name="Diff", exact=True).click()
    expect(viewer.locator("#ws-viewer-content")).to_contain_text("local.txt")
    expect(notice).to_be_hidden()
    expect(viewer.locator("#ws-message")).to_be_visible()
    assert not errors
