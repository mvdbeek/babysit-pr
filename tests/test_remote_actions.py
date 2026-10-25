"""Acting on a worker's workspaces: cleanup there, and messages to its agents.

The fake workers are directories under ``<tmp>/hosts/<ssh target>`` that the fake ssh
uses as HOME, with their herdr workspaces and agents in the fake herdr's per-label state.
"""

import hashlib
import json
import time

import pytest
import remote_probe
import workspace_overview as wso
from test_remote_inventory import BOX, MINI, fleet, git, make_clone, read, register, short_tmp
from test_workspace_overview import FakeGitHub

__all__ = ["fleet", "short_tmp"]  # Fixtures, used by name.


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
