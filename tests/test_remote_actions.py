"""Acting on a worker's workspaces: cleanup there, and messages to its agents.

The fake workers are directories under ``<tmp>/hosts/<ssh target>`` that the fake ssh
uses as HOME, with their herdr workspaces and agents in the fake herdr's per-label state.
"""

import copy
import hashlib
import json
import stat
import threading
import time
from pathlib import Path
from urllib.parse import quote

import agent_messages
import dashboard
import machines
import pytest
import remote_probe
import session_locks
import workspace_overview as wso
import workspace_viewer as wv
from playwright.sync_api import expect
from test_remote_inventory import BOX, MINI, fleet, git, make_clone, read, register, short_tmp
from test_remote_viewer import CLAUDE_SID, CODEX_SID, worker
from test_workspace_overview import FakeGitHub, request
from test_workspace_overview_browser import row

__all__ = ["fleet", "short_tmp", "worker"]  # Fixtures, used by name.


def cleanup(plugin, *targets):
    """Run one cleanup batch to its end; its results by key."""
    started = plugin.cleanup({"targets": [{"key": k, "approve": a} for k, a in targets]})
    assert started["accepted"]
    deadline = time.time() + 30
    while plugin.snapshot(start=False)["cleanup"]["status"] == "running":
        assert time.time() < deadline
        time.sleep(0.05)
    return {r["key"]: r for r in plugin.snapshot(start=False)["cleanup"]["results"]}


def branches(clone):
    return git("branch", "--format=%(refname:short)", cwd=clone).split()


def worker_calls(state, label="Mac mini"):
    return read(state)["machines"][label]["calls"]


@pytest.fixture
def plugin(fleet):
    register(fleet["home"], MINI, BOX)
    plugin = wso.WorkspaceOverview(fleet["home"], FakeGitHub())
    plugin.value = plugin.collect()
    return plugin


# Cleanup -----------------------------------------------------------------------------


def test_a_worker_checkout_is_removed_there_with_its_branch(fleet, plugin):
    clone = fleet["hosts"] / "mini.test" / "src" / "repo"
    key = f"m:mini:{fleet['mini'] / 'dirty-work'}"
    result = cleanup(plugin, (key, ["dirty"]))[key]
    assert result["status"] == "done" and result["message"] == "Cleaned up", result
    assert result["steps"] == [
        f"Removing worktree {fleet['mini'] / 'dirty-work'} on Mac mini",
        "Deleted local branch dirty-work",
    ]
    assert not (fleet["mini"] / "dirty-work").exists()
    assert "dirty-work" not in branches(clone) and "merged-work" in branches(clone)
    # Nothing on this machine was touched, and only the worker's herdr was asked.
    assert (fleet["local"] / "local-work").exists()
    assert not any(call[:2] == ["workspace", "close"] for call in read(fleet["state"])["calls"])


def test_an_unapproved_blocker_on_a_worker_is_skipped(fleet, plugin):
    key = f"m:mini:{fleet['mini'] / 'dirty-work'}"
    result = cleanup(plugin, (key, []))[key]
    assert result["status"] == "skipped" and "uncommitted" in result["message"]
    assert (fleet["mini"] / "dirty-work" / "scratch.txt").exists()


def test_a_worker_agent_is_stopped_and_its_workspace_closed_there(fleet, plugin):
    key = f"m:mini:{fleet['mini'] / 'merged-work'}"
    result = cleanup(plugin, (key, ["agent"]))[key]
    assert result["status"] == "done", result
    assert "Closing herdr workspace mini/w1" in result["steps"]
    calls = worker_calls(fleet["state"])
    assert ["workspace", "close", "w1"] in calls
    # The working agent got no keys; its workspace's close ended it.
    assert not any(call[:2] == ["agent", "send-keys"] for call in calls)
    assert not (fleet["mini"] / "merged-work").exists()
    workspaces = read(fleet["state"])["machines"]["Mac mini"]["workspaces"]
    assert [space["workspace_id"] for space in workspaces] == ["w2"]


def test_a_missing_worker_checkouts_workspace_is_closed(fleet, plugin):
    result = cleanup(plugin, ("workspace:mini:w2", []))["workspace:mini:w2"]
    assert result["status"] == "done" and result["steps"] == ["Closing herdr workspace mini/w2"]
    assert ["workspace", "close", "w2"] in worker_calls(fleet["state"])


def test_a_worker_checkout_outside_its_scanned_clones_is_refused(fleet, plugin):
    other = make_clone(fleet["hosts"] / "mini.test" / "elsewhere" / "repo", ["stray"])
    key = f"m:mini:{other / 'stray'}"
    result = cleanup(plugin, (key, ["dirty"]))[key]
    assert result["status"] == "failed" and "outside the scanned clones" in result["message"]
    assert (other / "stray").exists()


def test_cleanup_refuses_unknown_and_disabled_workers(fleet, plugin):
    results = cleanup(plugin, ("m:ghost:/src/x", []), ("workspace:ghost:w1", []))
    assert all("Unknown machine ghost" in r["message"] for r in results.values())
    register(fleet["home"], {**MINI, "enabled": False})
    key = f"m:mini:{fleet['mini'] / 'dirty-work'}"
    result = cleanup(plugin, (key, ["dirty"]))[key]
    assert result["status"] == "failed" and "Mac mini is disabled" in result["message"]
    assert (fleet["mini"] / "dirty-work").exists()


def test_an_unreachable_worker_fails_its_target_and_leaves_the_rest(fleet, plugin, monkeypatch):
    monkeypatch.setenv("FAKE_HERDR_DOWN", "Mac mini")
    key = f"m:mini:{fleet['mini'] / 'dirty-work'}"
    box = f"m:box:{fleet['box'] / 'box-work'}"
    results = cleanup(plugin, (key, ["dirty"]), (box, []))
    assert results[key]["status"] == "failed" and "cannot reach Mac mini" in results[key]["message"]
    assert (fleet["mini"] / "dirty-work").exists()
    assert results[box]["status"] == "done" and not (fleet["box"] / "box-work").exists()


def test_a_worker_clone_is_locked_apart_from_this_machines(fleet, plugin):
    clone = "/src/repo"
    local = plugin.lock_file(clone)
    assert local.name == hashlib.sha256(clone.encode()).hexdigest()
    mini = plugin.key_machine("m:mini:/src/repo/x")
    assert plugin.lock_file(clone, mini).name == hashlib.sha256(b"mini\0/src/repo").hexdigest()
    # The lock a launch on that worker takes for the same clone.
    assert plugin.lock_file(clone, mini) == machines.clone_lock(fleet["home"], mini, clone)


def stale_cleanup(plugin, monkeypatch, key, approve):
    """Clean up ``key`` as resolved now, with an untracked file written after that."""
    row = plugin.resolve(key)
    path = row["path"]
    monkeypatch.setattr(plugin, "resolve", lambda wanted: row)
    (Path(path) / "written-meanwhile.txt").write_text("work")
    return cleanup(plugin, (key, approve))[key], Path(path)


def test_approving_only_a_working_agent_keeps_the_worker_clean_check(fleet, plugin, monkeypatch):
    key = f"m:mini:{fleet['mini'] / 'merged-work'}"
    result, path = stale_cleanup(plugin, monkeypatch, key, ["agent"])
    assert result["status"] == "failed" and "untracked files; kept" in result["message"]
    assert (path / "written-meanwhile.txt").exists()


def test_approving_only_a_working_agent_keeps_the_local_clean_check(fleet, plugin, monkeypatch):
    key = str(fleet["local"] / "local-work")
    result, path = stale_cleanup(plugin, monkeypatch, key, ["agent"])
    assert result["status"] == "failed" and "untracked files; kept" in result["message"]
    assert (path / "written-meanwhile.txt").exists()


def test_a_worker_workspace_without_its_clone_is_checked_as_the_scan_shows_it(fleet):
    elsewhere = fleet["hosts"] / "mini.test" / "elsewhere"
    elsewhere.mkdir()
    store = read(fleet["state"])
    store["machines"]["Mac mini"]["workspaces"].append(
        {
            "workspace_id": "w5",
            "label": "odd",
            "worktree": {"repo_root": "relative", "checkout_path": str(elsewhere)},
        }
    )
    fleet["state"].write_text(json.dumps(store))
    register(fleet["home"], MINI)
    plugin = wso.WorkspaceOverview(fleet["home"], FakeGitHub())
    plugin.value = plugin.collect()
    listed = next(r for r in plugin.value["workspaces"] if r["key"] == "workspace:mini:w5")
    row = plugin.resolve("workspace:mini:w5")
    assert not row["missing"] and not row["removable"]
    assert [b["kind"] for b in row["blockers"]] == [b["kind"] for b in listed["blockers"]]
    assert row["removable"] == listed["removable"]


def test_a_cleanup_reads_its_row_again_once_it_holds_the_clones_lock(fleet, plugin, monkeypatch):
    """A launch holding the lock may start an agent there meanwhile; that is never removed."""
    key = f"m:mini:{fleet['mini'] / 'dirty-work'}"
    real = plugin.resolve
    calls = []

    def resolve(wanted):
        row = real(wanted)
        if not calls:
            # What the launch the cleanup waited for leaves behind.
            store = read(fleet["state"])
            mini = store["machines"]["Mac mini"]
            mini["workspaces"].append(
                {
                    "workspace_id": "w7",
                    "label": "dirty-work",
                    "worktree": {
                        "repo_root": str(fleet["hosts"] / "mini.test" / "src" / "repo"),
                        "checkout_path": str(fleet["mini"] / "dirty-work"),
                    },
                }
            )
            mini["agents"].append(
                {
                    "workspace_id": "w7",
                    "agent": "claude",
                    "agent_status": "working",
                    "pane_id": "w7:p1",
                }
            )
            fleet["state"].write_text(json.dumps(store))
        calls.append(wanted)
        return row

    monkeypatch.setattr(plugin, "resolve", resolve)
    result = cleanup(plugin, (key, ["dirty"]))[key]
    assert len(calls) == 2
    assert result["status"] == "skipped" and "agent" in result["message"], result
    assert (fleet["mini"] / "dirty-work" / "scratch.txt").exists()
    assert ["workspace", "close", "w7"] not in worker_calls(fleet["state"])


def test_worker_pane_ids_are_checked_before_herdr_gets_them(fleet, plugin, monkeypatch):
    agent = {"workspace_id": "w1", "agent": "claude", "agent_status": "idle"}
    assert plugin.remote_agent({**agent, "pane_id": "--machine=box"})["pane_id"] is None
    assert plugin.remote_agent({**agent, "pane_id": "w1:p1"})["pane_id"] == "w1:p1"
    monkeypatch.setattr(wso, "herdr", lambda *args, **kwargs: {"agents": [1, "x", None]})
    monkeypatch.setattr(wso, "EXIT_WAIT", 0.3)
    plugin.await_exit({"w1:p1"}, plugin.key_machine("m:mini:/x"))


def test_a_disabled_workers_row_is_not_messaged(fleet, plugin):
    key = f"m:mini:{fleet['mini'] / 'dirty-work'}"
    assert plugin.worker_checkout(key)[1] == str(fleet["mini"] / "dirty-work")
    register(fleet["home"], {**MINI, "enabled": False})
    with pytest.raises(ValueError, match="Mac mini is disabled"):
        plugin.worker_checkout(key)


def test_the_removal_probe_outlasts_its_git_calls():
    # The clean check's status and listing, the removal, and its quick Git reads.
    assert wso.REMOVE_TIMEOUT >= 3 * 300 + 3 * 60


# The probe's remove op, in this process ----------------------------------------------


@pytest.fixture
def clone(tmp_path):
    clone = tmp_path / "src" / "repo"
    make_clone(clone, ["topic", "other"])
    return clone


def test_remove_refuses_the_main_checkout_and_unlisted_paths(clone, tmp_path):
    worktrees = clone.parent / "worktrees" / "repo"
    with pytest.raises(ValueError, match="own checkout"):
        remote_probe._host_remove(str(clone), str(clone), True, None, True)
    stray = tmp_path / "stray"
    stray.mkdir()
    with pytest.raises(ValueError, match="not a worktree of"):
        remote_probe._host_remove(str(clone), str(stray), True, None, True)
    (clone / "sub").mkdir()
    with pytest.raises(ValueError, match="not the top of a clone"):
        remote_probe._host_remove(str(clone / "sub"), str(worktrees / "topic"), True, None, True)
    for branch in ("-D", "--force", "a b", "x:y", ""):
        with pytest.raises(ValueError, match="Expected a branch name"):
            remote_probe._host_remove(str(clone), str(worktrees / "topic"), True, branch, True)
    with pytest.raises(ValueError, match="Expected an? "):
        remote_probe._host_remove("relative", str(worktrees / "topic"), True, None, True)
    assert (worktrees / "topic").is_dir() and "topic" in branches(clone)


def test_remove_runs_the_hardened_clean_check_unless_forced(clone):
    topic = clone.parent / "worktrees" / "repo" / "topic"
    (topic / "new.txt").write_text("untracked")
    with pytest.raises(ValueError, match="uncommitted changes or untracked files; kept"):
        remote_probe._host_remove(str(clone), str(topic), False, "topic", True)
    assert topic.is_dir()
    done = remote_probe._host_remove(str(clone), str(topic), True, "topic", True)
    assert done == {"worktree": "removed", "branch": "deleted", "detail": None}
    assert remote_probe.validate_remove(done) == done
    assert not topic.exists() and "topic" not in branches(clone)


def test_remove_deletes_the_tree_without_following_symlinks_then_unregisters_it(
    clone, tmp_path, monkeypatch
):
    topic = clone.parent / "worktrees" / "repo" / "topic"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    (topic / "link").symlink_to(outside)
    deleted = []
    real = remote_probe.shutil.rmtree

    def rmtree(path, **kwargs):
        deleted.append(path)
        return real(path, **kwargs)

    rmtree.avoids_symlink_attacks = real.avoids_symlink_attacks
    assert rmtree.avoids_symlink_attacks  # Linux and macOS both have the fd-based delete.
    monkeypatch.setattr(remote_probe.shutil, "rmtree", rmtree)
    remote_probe._host_remove(str(clone), str(topic), True, None, True)
    assert deleted == [str(topic.resolve())]
    assert (outside / "keep.txt").exists() and not topic.exists()
    assert str(topic) not in git("worktree", "list", cwd=clone)


def test_remove_deletes_nothing_of_a_locked_worktree(clone):
    topic = clone.parent / "worktrees" / "repo" / "topic"
    (topic / "build.out").write_text("ignored output")
    git("worktree", "lock", "--reason", "keep it", str(topic), cwd=clone)
    with pytest.raises(ValueError, match=r"is locked \(keep it\); kept"):
        remote_probe._host_remove(str(clone), str(topic), True, "topic", True)
    assert (topic / "build.out").exists() and "topic" in branches(clone)


def test_remove_refuses_the_main_checkout_named_from_a_linked_worktree(clone):
    topic = clone.parent / "worktrees" / "repo" / "topic"
    with pytest.raises(ValueError, match="not the top of a clone|own checkout"):
        remote_probe._host_remove(str(topic), str(clone), True, None, True)
    assert (clone / ".git").is_dir()


def test_remove_keeps_a_branch_not_checked_out_there_or_not_merged(clone):
    worktrees = clone.parent / "worktrees" / "repo"
    done = remote_probe._host_remove(str(clone), str(worktrees / "topic"), False, "other", True)
    assert done["branch"] == "kept" and "not checked out there" in done["detail"]
    assert "other" in branches(clone)
    other = worktrees / "other"
    (other / "work.txt").write_text("work")
    git("add", "work.txt", cwd=other)
    git("commit", "-q", "-m", "Unmerged", cwd=other)
    done = remote_probe._host_remove(str(clone), str(other), False, "other", False)
    assert done["worktree"] == "removed" and done["branch"] == "kept"
    assert "not fully merged" in done["detail"] and "other" in branches(clone)


def test_resolve_answers_pass_no_unvalidated_fields(clone):
    topic = str(clone.parent / "worktrees" / "repo" / "topic")
    found = remote_probe._host_resolve(str(clone.parent), [], [], path=topic)
    assert "updates" not in remote_probe.validate_resolve({**found, "updates": {}})


def test_resolve_reads_only_a_scanned_clone(clone, tmp_path):
    topic = str(clone.parent / "worktrees" / "repo" / "topic")
    found = remote_probe._host_resolve(str(clone.parent), [], [], path=topic)
    assert remote_probe.validate_resolve(found) == found
    assert found["root"] == str(clone) and found["roots"] == [str(clone)]
    assert [item["path"] for item in found["clones"][0]["items"]][-1] in found["states"]
    assert found["states"][topic]["changes"] == 0
    outside = remote_probe._host_resolve(str(tmp_path / "nothing"), [], [], path=topic)
    assert outside["root"] == str(clone) and outside["clones"] == [] and outside["states"] == {}
    gone = remote_probe._host_resolve(str(clone.parent), [], [], path=str(tmp_path / "gone"))
    assert gone["root"] is None
    with pytest.raises(ValueError, match="checkout or a clone"):
        remote_probe._host_resolve("~", [], [], path=topic, root=str(clone))


@pytest.mark.parametrize(
    "change",
    [
        lambda v: v.update(root="relative"),
        lambda v: v.update(clones=v["clones"] * 2),
        lambda v: v.update(extra=1),
        lambda v: v.pop("root"),
        lambda v: v["states"].update({"/elsewhere": {"changes": 0, "unpushed": 0, "error": None}}),
    ],
)
def test_malformed_resolve_answers_are_refused(clone, change):
    topic = str(clone.parent / "worktrees" / "repo" / "topic")
    found = json.loads(
        json.dumps(remote_probe._host_resolve(str(clone.parent), [], [], path=topic))
    )
    change(found)
    with pytest.raises(ValueError, match="Malformed probe answer"):
        remote_probe.validate_resolve(found)


@pytest.mark.parametrize(
    "answer",
    [
        {"worktree": "removed", "branch": None},
        {"worktree": "gone", "branch": None, "detail": None},
        {"worktree": "removed", "branch": "maybe", "detail": None},
        {"worktree": "removed", "branch": "kept", "detail": 3},
    ],
)
def test_malformed_remove_answers_are_refused(answer):
    with pytest.raises(ValueError, match="Malformed probe answer"):
        remote_probe.validate_remove(answer)


def test_local_cleanup_refuses_a_branch_named_like_an_option():
    assert not wso.BRANCH_NAME.fullmatch("-D")
    assert wso.BRANCH_NAME.fullmatch("feature/x-1")


# Messages and resumes -----------------------------------------------------------------


def mini_store(worker, **changes):
    store = read(worker.state)
    store["machines"]["Mac mini"].update(changes)
    worker.state.write_text(json.dumps(store))


def mini(worker):
    return read(worker.state)["machines"]["Mac mini"]


@pytest.fixture
def idle(worker):
    """A Claude agent idle in the worker's w1, and this machine's own w1 beside it."""
    agent = {
        "workspace_id": "w1",
        "agent": "claude",
        "agent_status": "idle",
        "pane_id": "w1:p1",
        "agent_session": {"value": CLAUDE_SID},
    }
    mini_store(worker, agents=[agent])
    store = read(worker.state)
    store["workspaces"] = [
        {
            "workspace_id": "w1",
            "label": "local",
            "worktree": {"repo_root": str(worker.local), "checkout_path": str(worker.local)},
        }
    ]
    store["agents"] = [{**agent, "agent_session": {"value": "local"}}]
    worker.state.write_text(json.dumps(store))
    return worker


def test_a_worker_agent_is_listed_and_messaged_through_its_herdr(idle):
    found = agent_messages.agents("w1", machine=idle.machine)
    assert [(a["pane"], a["session"]) for a in found] == [("w1:p1", CLAUDE_SID)]
    sent = agent_messages.send(
        {"workspace": "w1", "text": "Add docs"}, idle.home, machine=idle.machine
    )
    assert sent == {"sent": True, "pane": "w1:p1", "warning": None}
    assert mini(idle)["prompts"] == [["w1:p1", "Add docs"]]
    assert "prompts" not in read(idle.state)
    with pytest.raises(ValueError, match="not available for agents on Mac mini"):
        agent_messages.send(
            {"workspace": "w1", "text": "x", "exit_when_done": True},
            idle.home,
            machine=idle.machine,
        )


def test_a_worker_agent_is_stopped_through_its_herdr(idle):
    mini_store(idle, agents=[{**mini(idle)["agents"][0], "agent_status": "working"}])
    request = {"workspace": "w1", "pane": "w1:p1", "session": CLAUDE_SID}
    assert agent_messages.interrupt(request, machine=idle.machine)["pane"] == "w1:p1"
    assert ["agent", "send-keys", "w1:p1", "esc"] in mini(idle)["calls"]
    assert not any(c[:2] == ["agent", "send-keys"] for c in read(idle.state)["calls"])


def test_a_worker_agents_dialog_is_read_and_answered_there(idle):
    screen = "\n".join(
        ["─" * 40, " Allow this command?", " ❯ 1. Yes", "   2. No", "", " Esc to cancel"]
    )
    blocked = {**mini(idle)["agents"][0], "agent_status": "blocked"}
    mini_store(idle, agents=[blocked], screens={"w1:p1": [screen]})
    [agent] = agent_messages.agents("w1", interactions=True, machine=idle.machine)
    choices = agent["interaction"]["choices"]
    assert [o["label"] for o in choices["options"]] == ["Yes", "No"]
    request = {
        "workspace": "w1",
        "pane": "w1:p1",
        "session": CLAUDE_SID,
        "dialog": choices["id"],
        "option": "2",
    }
    assert agent_messages.choose(request, machine=idle.machine) == {"chosen": True, "pane": "w1:p1"}
    assert ["agent", "send-keys", "w1:p1", "2"] in mini(idle)["calls"]
    assert not any(c[:2] == ["agent", "read"] for c in read(idle.state)["calls"])


@pytest.fixture
def exited(worker):
    """The worker's w1 with a shell pane and no agent: its sessions can be resumed."""
    mini_store(worker, panes=[{"pane_id": "w1:p1", "workspace_id": "w1"}])
    return worker


def staged_prompts(worker):
    return list((worker.host / ".local/state/babysit-pr/message-prompts").glob("resume-*"))


def test_a_worker_session_resumes_there_with_its_prompt_staged_there(exited):
    sessions = agent_messages.sessions("w1", exited.home, machine=exited.machine)
    assert {s["id"] for s in sessions} == {CLAUDE_SID, CODEX_SID}
    value = agent_messages.send(
        {"workspace": "w1", "resume": CLAUDE_SID, "text": "Now add docs"},
        exited.home,
        machine=exited.machine,
    )
    store = mini(exited)
    pane = store["panes"][-1]["pane_id"]
    assert value == {"sent": True, "pane": pane, "resumed": CLAUDE_SID, "warning": None}
    assert store["splits"] == [
        ["w1:p1", "--direction", "right", "--cwd", str(exited.checkout), "--no-focus"]
    ]
    [staged] = staged_prompts(exited)
    assert stat.S_IMODE(staged.stat().st_mode) == 0o600
    assert staged.read_text() == "Now add docs\n"
    # The worker's default login, under its Safehouse: no CLAUDE_CONFIG_DIR is typed.
    assert store["runs"] == [
        [
            pane,
            f'(unset CLAUDE_CONFIG_DIR; claude --resume {CLAUDE_SID} -- "$(cat {staged})")',
        ]
    ]
    assert store["agents"][0]["argv"] == ["--resume", CLAUDE_SID, "--", "Now add docs"]
    # Nothing was staged, split or typed on this machine.
    assert not (exited.home / "message-prompts").exists()
    local = read(exited.state)
    assert "runs" not in local and "splits" not in local
    with pytest.raises(ValueError, match="already running"):
        agent_messages.send(
            {"workspace": "w1", "resume": CLAUDE_SID, "text": "again"},
            exited.home,
            machine=exited.machine,
        )


def test_a_worker_resume_is_refused_on_its_own_facts(exited):
    def resume(sid):
        return agent_messages.send(
            {"workspace": "w1", "resume": sid, "text": "go"}, exited.home, machine=exited.machine
        )

    # The Codex session was recorded in a directory that is gone on the worker.
    with pytest.raises(ValueError, match="directory no longer exists"):
        resume(CODEX_SID)
    # The worker's repair lease, which this machine holds for it.
    with session_locks.acquire("claude", CLAUDE_SID, None, "mini"):
        with pytest.raises(agent_messages.Refused, match="repair is running"):
            resume(CLAUDE_SID)
    # The session open in a pane there.
    mini_store(
        exited,
        agents=[
            {
                "workspace_id": "w9",
                "agent": "claude",
                "agent_status": "idle",
                "pane_id": "w9:p1",
                "agent_session": {"value": CLAUDE_SID},
            }
        ],
        panes=[
            *mini(exited)["panes"],
            {"pane_id": "w9:p1", "agent_session": {"value": CLAUDE_SID}},
        ],
    )
    with pytest.raises(ValueError, match="already running in w9:p1"):
        agent_messages.send(
            {"workspace": "w1", "resume": CLAUDE_SID, "text": ""},
            exited.home,
            machine=exited.machine,
        )
    assert "runs" not in mini(exited) and not staged_prompts(exited)


def test_a_worker_resume_locks_its_checkout_by_the_workers_real_path(exited, monkeypatch):
    link = exited.host / "linked"
    link.symlink_to(exited.checkout)
    store = mini(exited)
    store["workspaces"][0]["worktree"]["checkout_path"] = str(link)
    mini_store(exited, workspaces=store["workspaces"])
    taken = []
    real = agent_messages.locked

    def locked(*keys):
        taken.extend(keys)
        return real(*keys)

    monkeypatch.setattr(agent_messages, "locked", locked)
    agent_messages.send(
        {"workspace": "w1", "resume": CLAUDE_SID, "text": ""}, exited.home, machine=exited.machine
    )
    assert ("checkout", f"mini:{exited.checkout.resolve()}") in taken


def test_a_closed_worker_workspace_is_reopened_there_and_followed(exited):
    assert wv.remote_checkout(exited.machine, "w1") == str(exited.checkout)
    # Another checkout's workspace stays open there, so the reopened one gets a new ID.
    other = {"workspace_id": "w2", "label": "other", "worktree": {"checkout_path": "/elsewhere"}}
    mini_store(exited, workspaces=[other], panes=[])
    assert wv.live_workspace("w1", exited.machine) is None
    value = agent_messages.send(
        {"workspace": "w1", "resume": CLAUDE_SID, "text": ""}, exited.home, machine=exited.machine
    )
    store = mini(exited)
    clone = exited.host / "src" / "repo"
    assert [c for c in store["calls"] if c[:2] == ["worktree", "open"]] == [
        ["worktree", "open", "--cwd", str(clone), "--path", str(exited.checkout), "--no-focus"]
    ]
    [space] = [w for w in store["workspaces"] if w["workspace_id"] != "w2"]
    assert space["workspace_id"] == "w3"
    assert value["pane"] == f"{space['workspace_id']}:p1" and value["resumed"] == CLAUDE_SID
    assert store["runs"][0][1] == f"(unset CLAUDE_CONFIG_DIR; claude --resume {CLAUDE_SID})"
    # The viewer left on w1 follows the workspace opened on its checkout since.
    assert wv.live_workspace("w1", exited.machine) == space["workspace_id"]
    assert "calls" not in read(exited.state) or not any(
        c[:2] == ["worktree", "open"] for c in read(exited.state)["calls"]
    )


def test_a_linux_workers_resume_line_runs_its_babysit_bwrap():
    box = machines.Machine(
        "box", "Box", "box", "linux", "~/src", "~/b", "python3", 4, ("gpg",), None, True
    )
    found = {
        "path": "/home/b/src/worktrees/repo/n",
        "common": "/home/b/src/repo/.git",
        "cwd": "/home/b/src/worktrees/repo/n",
        "cwd_exists": True,
        "claude_home": "/home/b/.claude",
        "claude_env": "/home/b/.claude",
        "codex_home": "/home/b/.codex",
        "launcher": "/home/b/b/scripts/babysit-bwrap",
    }
    chosen = {"id": CLAUDE_SID, "agent": "claude", "claude_account": "default"}
    assert agent_messages.remote_command(box, chosen, found, "go", "/home/b/p") == (
        "CLAUDE_CONFIG_DIR=/home/b/.claude /home/b/b/scripts/babysit-bwrap --enable=gpg "
        "--claude-config-dir=/home/b/.claude "
        "--add-dirs=/home/b/src/worktrees/repo/n:/home/b/src/repo/.git -- "
        f'claude --dangerously-skip-permissions --resume {CLAUDE_SID} -- "$(cat /home/b/p)"'
    )
    with pytest.raises(ValueError, match="babysit-bwrap is not installed under ~/b"):
        agent_messages.remote_command(box, chosen, {**found, "launcher": None}, "", None)


def test_the_resume_probe_reports_the_workers_own_paths(worker, monkeypatch):
    monkeypatch.setenv("HOME", str(worker.host))
    args = dict(agent="claude", account="default", babysit_root="~/b", sandbox="bwrap")
    found = remote_probe._host_resume(str(worker.checkout), str(worker.checkout), **args)
    assert remote_probe.validate_resume(found) == found
    assert found["path"] == found["cwd"] == str(worker.checkout.resolve())
    assert found["common"] == str((worker.host / "src" / "repo" / ".git").resolve())
    assert found["cwd_exists"] and found["launcher"] is None
    # Literal: Claude names its Keychain item after the path it is given.
    assert found["claude_env"] == str(worker.host / ".claude")
    gone = remote_probe._host_resume(str(worker.checkout), str(worker.checkout / "sub"), **args)
    assert not gone["cwd_exists"]
    with pytest.raises(ValueError, match="outside its checkout"):
        remote_probe._host_resume(str(worker.checkout), str(worker.host), **args)
    (worker.checkout / "deep").mkdir()
    deep = str(worker.checkout / "deep")
    with pytest.raises(ValueError, match="top of a Git checkout"):
        remote_probe._host_resume(deep, deep, **args)
    with pytest.raises(ValueError, match="Unknown Claude account"):
        remote_probe._host_resume(
            str(worker.checkout), str(worker.checkout), **{**args, "account": "nobody"}
        )
    # The folder is trusted in the config the sandboxed Claude reads, as launches trust it.
    monkeypatch.delenv("BABYSIT_CLAUDE_TRUST")
    config = worker.host / ".claude" / ".claude.json"
    config.write_text("{}")
    remote_probe._host_resume(str(worker.checkout), str(worker.checkout), **args)
    projects = json.loads(config.read_text())["projects"]
    assert projects[found["path"]] == {"hasTrustDialogAccepted": True}
    # A config the agent swapped for a symlink is never written through.
    target = worker.host / "babysit-state.json"
    target.write_text('{"version": 1}')
    config.unlink()
    config.symlink_to(target)
    (worker.checkout / "other").mkdir()
    remote_probe._host_resume(str(worker.checkout), str(worker.checkout / "other"), **args)
    assert target.read_text() == '{"version": 1}' and config.is_symlink()
    for bad in ({"cwd": "/w\x1b[2J"}, {"cwd_exists": "yes"}, {"launcher": "relative"}):
        with pytest.raises(ValueError, match="Malformed probe answer"):
            remote_probe.validate_resume({**found, **bad})


# Through the dashboard -----------------------------------------------------------------


@pytest.fixture
def site(exited):
    """A dashboard listing the worker's row, with its composer's routes live."""
    directory = exited.home / "experiments" / "workspaces"
    directory.mkdir(parents=True)
    (directory / "config.json").write_text(json.dumps({"enabled": True}))
    plugin = wso.WorkspaceOverview(exited.home, FakeGitHub())
    plugin.next_poll = float("inf")
    remote = row(
        "feature",
        key=f"m:mini:{exited.checkout}",
        path=str(exited.checkout),
        machine="mini",
        machine_label="Mac mini",
        machine_stale=False,
        workspace_ids=["mini/w1"],
    )
    plugin.value = {"workspaces": [remote], "warnings": [], "synced_at": time.time()}
    plugin.collect = lambda **kwargs: copy.deepcopy(plugin.value)
    with dashboard.DashboardServer(exited.home, 0, workspace_overview=plugin) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        yield server.server_port, remote
        server.shutdown()
        thread.join(3)


def test_the_composer_routes_reach_the_worker(exited, site):
    port, remote = site
    key = quote(remote["key"], safe="")
    for path in (f"/api/workspace-agents?key={key}", "/api/workspace-agents?workspace=mini%2Fw1"):
        status, value = request(port, path)
        assert status == 200, value
        assert value["workspace"] == "mini/w1" and value["path"] == str(exited.checkout)
        assert value["agents"] == [] and value["url"] is None
        assert {s["id"] for s in value["sessions"]} == {CLAUDE_SID, CODEX_SID}
    body = {"workspace": "mini/w1", "resume": CLAUDE_SID, "text": "Go on"}
    status, value = request(port, "/api/workspace-message", body, "workspace-message")
    assert status == 200 and value["resumed"] == CLAUDE_SID, value
    status, value = request(port, "/api/workspace-agents?workspace=mini%2Fw1")
    [agent] = value["agents"]
    # Docker access is a Safehouse grant on this Mac's own agents.
    assert agent["docker"] is None and agent["docker_supported"] is False
    attached = "2026-10-25-0000000a-a.txt"
    (exited.home / "attachments").mkdir()
    (exited.home / "attachments" / attached).write_text("notes")
    docker = {"workspace": "mini/w1", "pane": "w1:p1", "session": CLAUDE_SID, "enabled": True}
    for action, payload, error in [
        ("workspace-docker", docker, "only for agents on this Mac"),
        (
            "workspace-message",
            {"workspace": "mini/w1", "text": "x", "attachments": [attached]},
            "cannot be attached for agents on Mac mini",
        ),
        ("workspace-message", {"workspace": "ghost/w1", "text": "x"}, "Unknown machine ghost"),
    ]:
        status, value = request(port, f"/api/{action}", payload, action)
        assert status == 400 and error in value["error"], (action, value)
    assert "prompts" not in mini(exited)


@pytest.mark.browser
def test_a_worker_agent_is_messaged_from_its_row_without_docker_access(page, exited, site):
    port, _ = site
    agent = {
        "workspace_id": "w1",
        "agent": "claude",
        "agent_status": "idle",
        "pane_id": "w1:p1",
        "agent_session": {"value": CLAUDE_SID},
    }
    mini_store(exited, agents=[agent])
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(f"http://127.0.0.1:{port}/#workspaces")
    line = page.locator("#ws-list tr").filter(has=page.get_by_text("feature", exact=True))
    expect(line.get_by_role("checkbox")).to_be_enabled()
    expect(line.get_by_role("button", name="Open workspace", exact=True)).to_be_disabled()
    line.get_by_role("button", name="Transcript", exact=True).click()
    viewer = page.locator("#ws-viewer")
    expect(viewer.locator("#ws-message-agent")).to_contain_text("To claude · idle")
    expect(viewer.locator("#ws-docker")).to_be_hidden()
    viewer.locator("#ws-message-text").fill("Add docs")
    viewer.get_by_role("button", name="Send to agent").click()
    expect(viewer.locator("#ws-message-status")).to_contain_text("Sent to the agent in w1:p1")
    assert mini(exited)["prompts"] == [["w1:p1", "Add docs"]]
    assert "prompts" not in read(exited.state)
    assert not errors
