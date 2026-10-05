"""Temporary clones, fake herdr and fake GitHub; never touches live workspaces."""

import http.client
import json
import os
import subprocess
import threading
import time
from pathlib import Path
from unittest.mock import ANY
from urllib.parse import quote

import dashboard
import pytest
import workspace_overview as wso
import workspace_viewer as wso_viewer
from conftest import install_fakes

BASE = "base/repo"


def read(state):
    return json.loads(Path(state).read_text())


def calls(state, prefix):
    return [call for call in read(state)["calls"] if call[: len(prefix)] == prefix]


class FakeGitHub:
    """Answers only the endpoints this experiment is allowed to ask for."""

    def __init__(self):
        self.calls = []
        self.fail = set()
        self.repos = {
            "repos/fork/repo": {
                "full_name": "fork/repo",
                "fork": True,
                "parent": {"full_name": BASE},
            }
        }
        self.issues = {
            "12": {
                "number": 12,
                "title": "Crash on start",
                "html_url": f"https://github.com/{BASE}/issues/12",
                "state": "closed",
            },
            "44": {
                "number": 44,
                "title": "Still open",
                "html_url": f"https://github.com/{BASE}/issues/44",
                "state": "open",
            },
        }
        self.pulls = {
            "9": {
                "number": 9,
                "title": "Named pull request",
                "html_url": f"https://github.com/{BASE}/pull/9",
                "state": "open",
                "draft": True,
            }
        }
        self.heads = {
            "fork:merged-work": [
                {
                    "number": 7,
                    "title": "Merged work",
                    "html_url": f"https://github.com/{BASE}/pull/7",
                    "state": "closed",
                    "merged_at": "2026-09-01T00:00:00Z",
                }
            ],
            "fork:dirty-work": [
                {
                    "number": 8,
                    "title": "Dirty work",
                    "html_url": f"https://github.com/{BASE}/pull/8",
                    "state": "closed",
                }
            ],
        }

    def __call__(self, endpoint):
        self.calls.append(endpoint)
        if endpoint in self.fail:
            raise ValueError("gh: Not Found (HTTP 404)")
        if endpoint in self.repos:
            return self.repos[endpoint]
        if "/pulls?" in endpoint:
            head = endpoint.split("head=")[1]
            return self.heads.get(head, [])
        for kind, table in (("issues", self.issues), ("pulls", self.pulls)):
            prefix = f"repos/{BASE}/{kind}/"
            if endpoint.startswith(prefix):
                number = endpoint[len(prefix) :]
                if number not in table:
                    raise ValueError("gh: Not Found (HTTP 404)")
                return table[number]
        raise ValueError(f"gh: unexpected endpoint {endpoint}")


@pytest.fixture
def site(tmp_path, monkeypatch):
    """A clone with four worktrees covering clean, dirty, unpushed and issue checkouts."""
    src = tmp_path / "src"
    clone = src / "repo"
    clone.mkdir(parents=True)

    def git(*args, cwd=clone):
        return subprocess.check_output(["git", "-C", str(cwd), *args], text=True).strip()

    git("init", "-b", "main")
    git("config", "user.name", "Fixture")
    git("config", "user.email", "fixture@example.test")
    git("commit", "--allow-empty", "-m", "base")
    git("remote", "add", "origin", "https://github.com/fork/repo.git")
    git("remote", "add", "upstream", f"https://github.com/{BASE}.git")
    git("update-ref", "refs/remotes/origin/main", "HEAD")
    worktrees = src / "worktrees" / "repo"
    for branch in ("merged-work", "dirty-work", "unpushed-work", "issue-12-crash"):
        git("worktree", "add", "-b", branch, str(worktrees / branch))
        git("update-ref", f"refs/remotes/origin/{branch}", "HEAD")
    (worktrees / "dirty-work" / "scratch.txt").write_text("left behind")
    git("commit", "--allow-empty", "-m", "local only", cwd=worktrees / "unpushed-work")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    install_fakes(bin_dir)
    state = tmp_path / "herdr.json"
    state.write_text(
        json.dumps(
            {
                "workspaces": [
                    {
                        "workspace_id": "w1",
                        "label": "merged-work",
                        "agent_status": "idle",
                        "worktree": {
                            "repo_root": str(clone),
                            "checkout_path": str(worktrees / "merged-work"),
                        },
                    },
                    {
                        "workspace_id": "w2",
                        "label": "gone",
                        "agent_status": "unknown",
                        "worktree": {
                            "repo_root": str(clone),
                            "checkout_path": str(src / "worktrees/repo/removed"),
                        },
                    },
                ],
                "agents": [
                    {
                        "workspace_id": "w1",
                        "agent": "codex",
                        "agent_status": "idle",
                        "pane_id": "w1:p1",
                        "cwd": str(worktrees / "merged-work"),
                    }
                ],
                "calls": [],
            }
        )
    )
    monkeypatch.setenv("FAKE_HERDR", str(state))
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("HOME", str(tmp_path))
    home = tmp_path / "state"
    directory = home / "experiments" / "workspaces"
    directory.mkdir(parents=True)
    (directory / "config.json").write_text(json.dumps({"enabled": True, "src": str(src)}))
    fake = FakeGitHub()
    plugin = wso.WorkspaceOverview(home, fake)
    return plugin, fake, clone, worktrees, state, git


def texts(row):
    return [item["text"] for item in row["blockers"]]


def rows(plugin):
    return {row["name"]: row for row in plugin.collect()["workspaces"]}


def test_lists_worktrees_with_links_agents_and_local_state(site):
    plugin, fake, clone, worktrees, _, _ = site
    found = rows(plugin)
    assert set(found) == {"merged-work", "dirty-work", "unpushed-work", "issue-12-crash", "gone"}
    merged = found["merged-work"]
    assert merged["status"] == "ready"
    assert merged["repo"] == BASE and merged["branch"] == "merged-work"
    assert merged["links"] == [
        {
            "kind": "pr",
            "repo": BASE,
            "number": 7,
            "title": "Merged work",
            "url": f"https://github.com/{BASE}/pull/7",
            "state": "merged",
            "draft": False,
        }
    ]
    assert merged["agents"] == [{"agent": "codex", "status": "idle", "pane": "w1:p1"}]
    assert merged["workspace_ids"] == ["w1"] and merged["removable"]
    assert found["dirty-work"]["status"] == "blocked"
    assert texts(found["dirty-work"]) == ["1 uncommitted change(s)"]
    assert texts(found["unpushed-work"]) == ["1 commit(s) only on this branch"]
    assert found["issue-12-crash"]["links"][0]["number"] == 12
    assert found["issue-12-crash"]["links"][0]["state"] == "closed"
    # The clone's own checkout has no herdr workspace, so it is not a listed workspace.
    assert "repo" not in found
    assert found["gone"]["missing"] and found["gone"]["removable"]
    assert f"repos/{BASE}/pulls/9" not in fake.calls


def test_checkout_times_follow_edits_and_head_without_advancing_on_inspection(site):
    _, _, _, worktrees, _, git = site
    path = worktrees / "merged-work"
    tracked = path / 'tracked "file\n.txt'
    tracked.write_text("original")
    git("add", ".", cwd=path)
    git("commit", "-m", "tracked file", cwd=path)
    tracked.write_text("edited in place")
    untracked = path / "untracked file.txt"
    untracked.write_text("new")
    log = Path(git("rev-parse", "--absolute-git-dir", cwd=path)) / "logs" / "HEAD"
    created = float(log.read_text().splitlines()[0].split("\t", 1)[0].split()[-2])
    baseline = created + 10
    for file in (path, tracked, untracked, log):
        os.utime(file, (baseline, baseline))
    os.utime(tracked, (baseline + 20, baseline + 20))
    first = wso.local_state(str(path), "merged-work")
    assert first["created_at"] == created
    assert first["updated_at"] == baseline + 20
    assert wso.local_state(str(path), "merged-work") == first
    git("add", ".", cwd=path)
    assert wso.local_state(str(path), "merged-work") == first
    os.utime(untracked, (baseline + 30, baseline + 30))
    assert wso.local_state(str(path), "merged-work")["updated_at"] == baseline + 30
    os.utime(log, (baseline + 40, baseline + 40))
    assert wso.checkout_times(path) == {"created_at": created, "updated_at": baseline + 40}


def test_checkout_times_tolerate_missing_reflogs_and_checkouts(tmp_path):
    path = tmp_path / "checkout"
    (path / ".git").mkdir(parents=True)
    (path / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    value = wso.checkout_times(path)
    assert value["created_at"] == getattr(path.stat(), "st_birthtime", None)
    assert value["updated_at"] >= path.stat().st_mtime
    assert wso.checkout_times(tmp_path / "gone") == {"created_at": None, "updated_at": None}


def test_inventory_uses_transcript_activity_and_sorts_newest_first(site, tmp_path, monkeypatch):
    plugin, _, _, worktrees, _, _ = site
    day = tmp_path / "codex" / "sessions" / "2026" / "10" / "05"
    day.mkdir(parents=True)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    latest = time.time() + 100
    file = day / "rollout-recent.jsonl"
    file.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {"id": "01234567-1234", "cwd": str(worktrees / "merged-work/lib")},
            }
        )
        + "\n"
    )
    os.utime(file, (latest, latest))
    unrelated = day / "rollout-unrelated.jsonl"
    unrelated.write_text(
        json.dumps({"type": "session_meta", "payload": {"id": "01234567-9999", "cwd": "/else"}})
        + "\n"
    )
    os.utime(unrelated, (latest + 100, latest + 100))
    value = plugin.collect()["workspaces"]
    assert value[0]["name"] == "merged-work" and value[0]["updated_at"] == latest
    assert value[0]["created_at"] is not None
    assert value[-1]["name"] == "gone" and value[-1]["updated_at"] is None


@pytest.mark.parametrize("outside", [False, True])
def test_claude_activity_matches_the_nearest_checkout(tmp_path, monkeypatch, outside):
    src = tmp_path / "src"
    root = tmp_path / "external" if outside else src / "checkout"
    nested = root / "nested"
    nested.mkdir(parents=True)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    config = tmp_path / "claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    cwd = nested / "lib"
    folder = config / "projects" / "".join(c if c.isalnum() else "-" for c in str(cwd))
    folder.mkdir(parents=True)
    file = folder / "session.jsonl"
    file.write_text(
        json.dumps({"type": "user", "cwd": str(cwd), "sessionId": "01234567-1234"}) + "\n"
    )
    os.utime(file, (1000, 1000))
    assert wso.transcript_updates({str(root), str(nested)}, src) == {str(nested): 1000}


def test_base_tracking_and_foreign_names_never_become_links():
    item = {"path": "/w/topic", "branch": "topic", "upstream": ["fork/repo", "dev"]}
    assert wso.candidates(item, BASE, "fork") == [("head", "fork:topic")]
    release = {"path": "/w/release_26.1", "branch": "release_26.1", "upstream": None}
    assert wso.candidates(release, BASE, "fork") == []
    named = {"path": "/w/pr-other-4", "branch": "pr-other-4", "upstream": None}
    assert wso.candidates(named, BASE, "fork") == [("head", "fork:pr-other-4")]
    mine = {"path": "/w/pr-base-4-2", "branch": "pr-base-4-2", "upstream": None}
    assert ("pr", "4") in wso.candidates(mine, BASE, "fork")
    fork_head = {"path": "/w/fix", "branch": "fix", "upstream": ["other/repo", "fix"]}
    assert wso.candidates(fork_head, BASE, "fork")[0] == ("head", "other:fix")


def test_links_are_cached_reused_and_bounded_by_the_request_budget(site, monkeypatch):
    plugin, fake, *_ = site
    plugin.collect()
    first = len(fake.calls)
    assert first and rows(plugin)["merged-work"]["links"]
    assert len(fake.calls) == first  # Fresh cache entries are reused, not refetched.
    monkeypatch.setattr(wso, "CALL_BUDGET", 0)
    plugin.links.clear()
    value = plugin.collect()
    assert value["calls"] == 0
    assert any("budget exhausted" in warning for warning in value["warnings"])
    worktrees = [row for row in value["workspaces"] if row["branch"]]
    assert worktrees and all(not row["links"] and row["stale_links"] for row in worktrees)


def test_base_repository_resolves_through_the_fork_parent_and_is_cached(site):
    plugin, fake, clone, *_ = site
    subprocess.check_call(["git", "-C", str(clone), "remote", "remove", "upstream"])
    assert rows(plugin)["merged-work"]["repo"] == BASE
    assert "repos/fork/repo" in fake.calls
    fake.calls.clear()
    assert rows(plugin)["merged-work"]["repo"] == BASE
    assert "repos/fork/repo" not in fake.calls


def test_a_failed_repository_lookup_is_not_cached_as_an_answer(site):
    plugin, fake, clone, *_ = site
    subprocess.check_call(["git", "-C", str(clone), "remote", "remove", "upstream"])
    fake.fail.add("repos/fork/repo")
    value = plugin.collect()
    assert any("repository lookup failed" in warning for warning in value["warnings"])
    fake.fail.clear()
    assert rows(plugin)["merged-work"]["repo"] == BASE


def test_refresh_only_fetches_expired_github_links(site):
    plugin, fake, *_ = site
    plugin.collect()
    fake.calls.clear()
    plugin.links[f"{BASE}#head#fork:merged-work"]["checked_at"] -= wso.SETTLED_TTL
    plugin.collect()
    assert fake.calls == [
        f"repos/{BASE}/pulls?state=all&per_page=5&sort=created&direction=desc&head=fork:merged-work"
    ]


def finish(plugin, started=None):
    assert started is None or started["cleanup"]["status"] in {"running", "complete"}
    deadline = time.monotonic() + 30
    while plugin.job["status"] == "running" and time.monotonic() < deadline:
        time.sleep(0.02)
    assert plugin.job["status"] == "complete"
    return {result["key"]: result for result in plugin.job["results"]}


def test_cleanup_exits_agents_closes_the_workspace_and_removes_worktree_and_branch(site):
    plugin, _, clone, worktrees, state, git = site
    target = str(worktrees / "merged-work")
    results = finish(plugin, plugin.cleanup({"targets": [{"key": target}]}))
    assert results[target]["status"] == "done"
    assert calls(state, ["agent", "send-keys", "w1:p1"])
    assert calls(state, ["workspace", "close", "w1"])
    assert not Path(target).exists()
    assert "merged-work" not in git("branch", "--format=%(refname:short)").split()
    assert "worktree" in " ".join(results[target]["steps"]).lower()


def test_cleanup_skips_local_only_work_until_it_is_explicitly_forced(site):
    plugin, _, clone, worktrees, state, git = site
    dirty, unpushed = str(worktrees / "dirty-work"), str(worktrees / "unpushed-work")
    results = finish(plugin, plugin.cleanup({"targets": [{"key": dirty}, {"key": unpushed}]}))
    assert results[dirty]["status"] == "skipped"
    assert "uncommitted" in results[dirty]["message"]
    assert results[unpushed]["status"] == "skipped"
    assert Path(dirty).exists() and Path(unpushed).exists()
    results = finish(
        plugin,
        plugin.cleanup(
            {"targets": [{"key": dirty, "approve": ["dirty", "unpushed"]}, {"key": unpushed}]}
        ),
    )
    assert results[dirty]["status"] == "done"
    assert not Path(dirty).exists()
    assert "dirty-work" not in git("branch", "--format=%(refname:short)").split()
    assert results[unpushed]["status"] == "skipped" and Path(unpushed).exists()


def test_a_branch_git_refuses_to_delete_is_kept_and_reported(site, monkeypatch):
    plugin, _, clone, worktrees, _, git = site
    target = str(worktrees / "unpushed-work")
    # Local state cannot be verified, so only Git's own safe delete decides — and it
    # refuses, leaving the branch behind with its worktree already gone.
    monkeypatch.setattr(
        wso,
        "local_state",
        lambda path, branch=None: {
            "changes": None,
            "unpushed": None,
            "error": "Local state unavailable: fixture",
        },
    )
    results = finish(plugin, plugin.cleanup({"targets": [{"key": target, "approve": ["unknown"]}]}))
    assert results[target]["status"] == "done", results[target]["message"]
    assert not Path(target).exists()
    assert "Kept local branch unpushed-work" in results[target]["message"]
    assert "not fully merged" in results[target]["message"]
    assert "unpushed-work" in git("branch", "--format=%(refname:short)").split()


def test_a_branch_no_commit_can_be_lost_from_is_deleted_even_when_unmerged(site):
    plugin, _, clone, worktrees, _, git = site
    target = str(worktrees / "unpushed-work")
    git("update-ref", "refs/remotes/origin/unpushed-work", "unpushed-work")
    results = finish(plugin, plugin.cleanup({"targets": [{"key": target}]}))
    assert results[target]["status"] == "done" and results[target]["message"] == "Cleaned up"
    assert "unpushed-work" not in git("branch", "--format=%(refname:short)").split()


def test_a_blocker_that_appears_after_selection_stops_the_cleanup(site, monkeypatch):
    """An override approves the blockers the user saw, never one that arrived later."""
    plugin, _, _, worktrees, _, _ = site
    target = str(worktrees / "unpushed-work")
    original = wso.WorkspaceOverview.resolve

    def resolve(self, key):
        row = original(self, key)
        if row:
            row["blockers"].append(wso.blocker("dirty", "9 uncommitted change(s)"))
        return row

    monkeypatch.setattr(wso.WorkspaceOverview, "resolve", resolve)
    results = finish(
        plugin, plugin.cleanup({"targets": [{"key": target, "approve": ["unpushed"]}]})
    )
    assert results[target]["status"] == "skipped"
    assert results[target]["message"] == "Skipped: 9 uncommitted change(s)"
    assert Path(target).exists()


def test_a_checkout_outside_the_scanned_clones_is_never_removed(tmp_path, site):
    plugin, _, _, _, _, _ = site
    outside = tmp_path / "elsewhere"
    outside.mkdir()

    def git(*args, cwd=outside):
        return subprocess.check_output(["git", "-C", str(cwd), *args], text=True).strip()

    git("init", "-b", "main")
    git("config", "user.name", "Fixture")
    git("config", "user.email", "fixture@example.test")
    git("commit", "--allow-empty", "-m", "base")
    victim = tmp_path / "victim"
    git("worktree", "add", "-b", "secret", str(victim))
    key = str(victim)
    results = finish(
        plugin, plugin.cleanup({"targets": [{"key": key, "approve": sorted(wso.OVERRIDABLE)}]})
    )
    assert results[key]["status"] == "failed"
    assert "outside the scanned clones" in results[key]["message"]
    assert victim.exists()


def test_a_main_checkout_with_a_workspace_only_loses_that_workspace(site):
    plugin, _, clone, _, state, git = site
    data = read(state)
    data["workspaces"].append(
        {
            "workspace_id": "w3",
            "label": "repo",
            "agent_status": "idle",
            "worktree": {"repo_root": str(clone), "checkout_path": str(clone)},
        }
    )
    Path(state).write_text(json.dumps(data))
    key = str(clone)
    results = finish(plugin, plugin.cleanup({"targets": [{"key": key, "approve": ["main"]}]}))
    assert results[key]["status"] == "done"
    assert calls(state, ["workspace", "close", "w3"])
    assert Path(clone, ".git").exists()
    assert "main" in git("branch", "--format=%(refname:short)").split()


def test_an_unexpected_failure_stops_only_its_own_target(site, monkeypatch):
    plugin, _, _, worktrees, _, _ = site
    first, second = str(worktrees / "merged-work"), str(worktrees / "issue-12-crash")
    original = wso.WorkspaceOverview.remove

    def remove(self, job, index, target):
        if target["key"] == first:
            raise RuntimeError("herdr returned nonsense")
        return original(self, job, index, target)

    monkeypatch.setattr(wso.WorkspaceOverview, "remove", remove)
    results = finish(plugin, plugin.cleanup({"targets": [{"key": first}, {"key": second}]}))
    assert results[first]["status"] == "failed"
    assert "nonsense" in results[first]["message"]
    assert results[second]["status"] == "done" and not Path(second).exists()


def test_cleanup_never_removes_a_main_checkout_or_a_watched_checkout(site):
    plugin, _, clone, worktrees, state, _ = site
    results = finish(
        plugin, plugin.cleanup({"targets": [{"key": str(clone), "approve": ["main"]}]})
    )
    assert results[str(clone)]["status"] == "skipped"
    assert Path(clone, ".git").exists()
    target = str(worktrees / "merged-work")
    plugin.jobs = lambda: [{"cwd": target}]
    results = finish(
        plugin, plugin.cleanup({"targets": [{"key": target, "approve": sorted(wso.OVERRIDABLE)}]})
    )
    assert "watch" in results[target]["message"]
    assert Path(target).exists()


@pytest.mark.parametrize(
    "status",
    ["closed", "stopped", "watching", "paused", "blocked", "running", "handoff", None, "unknown"],
)
def test_only_ended_watches_release_checkout_protection(site, status):
    plugin, _, _, worktrees, _, _ = site
    target = str(worktrees / "merged-work")
    plugin.jobs = lambda: [{"cwd": target, "status": status}]
    ended = status in {"closed", "stopped"}
    for row in (rows(plugin)["merged-work"], plugin.resolve(target)):
        assert row["removable"] == ended
        assert any(item["kind"] == "watch" for item in row["blockers"]) != ended


@pytest.mark.parametrize("status", ["closed", "stopped"])
def test_cleanup_rechecks_watch_status_after_inventory(site, status):
    plugin, _, _, worktrees, state, _ = site
    target = str(worktrees / "merged-work")
    jobs = [{"cwd": target, "status": status}]
    plugin.jobs = lambda: jobs
    assert rows(plugin)["merged-work"]["removable"]

    # A second watch can reserve the same checkout after the list was rendered.
    jobs.append({"cwd": target, "status": "running", "stop_after_run": True})
    results = finish(plugin, plugin.cleanup({"targets": [{"key": target}]}))
    assert results[target]["status"] == "skipped"
    assert "watch" in results[target]["message"]
    assert Path(target).exists()
    assert not calls(state, ["workspace", "close", "w1"])

    jobs[-1]["status"] = "stopped"
    results = finish(plugin, plugin.cleanup({"targets": [{"key": target}]}))
    assert results[target]["status"] == "done"
    assert not Path(target).exists()
    assert calls(state, ["workspace", "close", "w1"])


def test_a_missing_checkout_only_closes_its_workspace(site):
    plugin, _, _, _, state, _ = site
    results = finish(plugin, plugin.cleanup({"targets": [{"key": "workspace:w2"}]}))
    assert results["workspace:w2"]["status"] == "done"
    assert calls(state, ["workspace", "close", "w2"])
    assert not read(state)["workspaces"] or all(
        space["workspace_id"] != "w2" for space in read(state)["workspaces"]
    )


def test_an_unknown_target_reports_that_nothing_remains(site):
    plugin, _, _, worktrees, _, _ = site
    key = str(worktrees / "never-existed")
    assert finish(plugin, plugin.cleanup({"targets": [{"key": key}]}))[key]["status"] == "done"


def test_a_running_batch_is_returned_instead_of_starting_a_second(site, monkeypatch):
    plugin, _, _, worktrees, _, _ = site
    gate = threading.Event()
    monkeypatch.setattr(
        wso.WorkspaceOverview, "remove", lambda *args, **kwargs: gate.wait(timeout=10)
    )
    started = plugin.cleanup({"targets": [{"key": str(worktrees / "merged-work")}]})
    refused = plugin.cleanup({"targets": [{"key": str(worktrees / "dirty-work")}]})
    assert started["accepted"] and not refused["accepted"]
    assert refused["cleanup"]["id"] == started["cleanup"]["id"]
    gate.set()
    finish(plugin)


@pytest.mark.parametrize(
    "request_body",
    [
        {},
        {"targets": []},
        {"targets": [{"key": ""}]},
        {"targets": [{"key": "/a"}, {"key": "/a"}]},
        {"targets": [{"key": "/a", "approve": "dirty"}]},
        {"targets": [{"key": "/a", "other": 1}]},
        {"targets": "/a"},
        {"targets": [{"key": "/a"}], "unexpected": 1},
        {"targets": [{"key": f"/{'a' * 2000}"}]},
    ],
)
def test_cleanup_rejects_invalid_requests(site, request_body):
    plugin, *_ = site
    with pytest.raises(ValueError):
        plugin.cleanup(request_body)
    assert plugin.job is None


def test_too_many_targets_are_refused(site):
    plugin, *_ = site
    with pytest.raises(ValueError):
        plugin.cleanup({"targets": [{"key": f"/w/{n}"} for n in range(wso.MAX_TARGETS + 1)]})


def test_disabled_and_missing_configuration_do_no_work(tmp_path):
    plugin = wso.WorkspaceOverview(tmp_path, lambda endpoint: pytest.fail("no GitHub call"))
    assert plugin.snapshot() == {
        "workspaces": [],
        "warnings": [],
        "synced_at": None,
        "enabled": False,
        "loading": False,
        "error": None,
        "cleanup": None,
        "stale": False,
    }
    with pytest.raises(ValueError):
        plugin.cleanup({"targets": [{"key": "/w/one"}]})
    directory = tmp_path / "experiments" / "workspaces"
    directory.mkdir(parents=True)
    (directory / "config.json").write_text(json.dumps({"enabled": False}))
    assert not wso.WorkspaceOverview(tmp_path).enabled


def test_a_broken_configuration_is_reported_without_raising(tmp_path):
    directory = tmp_path / "experiments" / "workspaces"
    directory.mkdir(parents=True)
    (directory / "config.json").write_text("{not json")
    plugin = wso.WorkspaceOverview(tmp_path)
    assert not plugin.enabled and "unavailable" in plugin.error


def test_the_cache_survives_a_restart_and_an_old_format_is_discarded(site):
    plugin, fake, *_ = site
    plugin._refresh()
    assert plugin.value["workspaces"] and (plugin.directory / "cache.json").exists()
    assert (plugin.directory / "cache.json").stat().st_mode & 0o777 == 0o600
    restarted = wso.WorkspaceOverview(plugin.home, fake)
    assert restarted.links == plugin.links and restarted.bases == plugin.bases
    assert restarted.value == plugin.value
    assert restarted.next_poll == plugin.value["synced_at"] + wso.INTERVAL
    snapshot = restarted.snapshot()
    assert snapshot["workspaces"] == plugin.value["workspaces"]
    assert not snapshot["loading"] and not snapshot["stale"]
    saved = json.loads((plugin.directory / "cache.json").read_text())
    (plugin.directory / "cache.json").write_text(json.dumps({**saved, "version": 0}))
    outdated = wso.WorkspaceOverview(plugin.home, fake)
    assert not outdated.links and not outdated.value["workspaces"]


def test_restart_shows_inventory_while_one_background_refresh_runs(site, monkeypatch):
    plugin, fake, *_ = site
    plugin._refresh()
    plugin.value["synced_at"] -= wso.INTERVAL
    plugin.save()
    restarted = wso.WorkspaceOverview(plugin.home, fake)
    gate = threading.Event()
    finished = threading.Event()
    scans = []

    def collect(publish=None):
        scans.append(True)
        assert gate.wait(timeout=10)
        return {"workspaces": [], "warnings": []}

    refresh = restarted._refresh

    def complete_refresh():
        try:
            refresh()
        finally:
            finished.set()

    monkeypatch.setattr(restarted, "collect", collect)
    monkeypatch.setattr(restarted, "_refresh", complete_refresh)
    try:
        for _ in range(3):
            snapshot = restarted.snapshot()
            assert snapshot["workspaces"] == plugin.value["workspaces"]
            assert snapshot["synced_at"] == plugin.value["synced_at"]
            assert snapshot["loading"] and snapshot["stale"]
            assert snapshot["cleanup"] is None
    finally:
        gate.set()
        assert finished.wait(timeout=10)
    assert len(scans) == 1
    snapshot = restarted.snapshot()
    assert not snapshot["workspaces"] and not snapshot["loading"] and not snapshot["stale"]
    assert snapshot["synced_at"] >= plugin.value["synced_at"]
    assert wso.WorkspaceOverview(plugin.home, fake).value == restarted.value


def test_finished_rows_appear_before_a_slow_checkout_and_only_complete_scans_are_saved(
    site, monkeypatch
):
    plugin, fake, _, worktrees, _, _ = site
    plugin._refresh()
    previous = plugin.value
    changed = worktrees / "issue-12-crash"
    (changed / "new-work.txt").write_text("new edit")
    gate = threading.Event()
    published = threading.Event()
    inspect = wso.local_state
    publish = plugin.publish_row

    def local_state(path, branch=None):
        if Path(path).name == "merged-work":
            assert gate.wait(timeout=10)
        return inspect(path, branch)

    def publish_row(row):
        publish(row)
        if row["path"] == str(changed):
            published.set()

    monkeypatch.setattr(wso, "local_state", local_state)
    monkeypatch.setattr(plugin, "publish_row", publish_row)
    plugin.loading = True
    plugin.next_poll = time.time() + wso.INTERVAL
    worker = threading.Thread(target=plugin._refresh)
    worker.start()
    try:
        assert published.wait(timeout=10)
        snapshot = plugin.snapshot()
        assert snapshot["loading"] and snapshot["synced_at"] == previous["synced_at"]
        row = next(row for row in snapshot["workspaces"] if row["path"] == str(changed))
        assert row["changes"] == 1
        assert len(snapshot["workspaces"]) == len(previous["workspaces"])
        assert wso.WorkspaceOverview(plugin.home, fake).value == previous
        # A cleanup finishing during the scan must still trigger its requested rescan.
        plugin.next_poll = 0
    finally:
        gate.set()
        worker.join(timeout=10)
    assert not worker.is_alive() and not plugin.loading and plugin.error is None
    assert plugin.next_poll == 0
    assert wso.WorkspaceOverview(plugin.home, fake).value == plugin.value


@pytest.mark.parametrize("change", ["src", "roots", "override", "legacy"])
def test_cached_inventory_is_only_restored_for_the_same_scan_scope(site, tmp_path, change):
    plugin, fake, *_ = site
    plugin._refresh()
    config = dict(plugin.config)
    options = {}
    if change == "override":
        options["src"] = tmp_path / "other"
    elif change == "legacy":
        saved = json.loads((plugin.directory / "cache.json").read_text())
        saved.pop("scope")
        saved.pop("value")
        (plugin.directory / "cache.json").write_text(json.dumps(saved))
    else:
        config[change] = str(tmp_path / "other") if change == "src" else [str(tmp_path / "other")]
        (plugin.directory / "config.json").write_text(json.dumps(config))
    restarted = wso.WorkspaceOverview(plugin.home, fake, **options)
    assert restarted.links == plugin.links
    assert not restarted.value["workspaces"] and restarted.next_poll == 0


def test_cleanup_revalidates_a_checkout_from_restored_inventory(site):
    plugin, fake, _, worktrees, state, _ = site
    plugin._refresh()
    restarted = wso.WorkspaceOverview(plugin.home, fake)
    target = worktrees / "merged-work"
    row = next(row for row in restarted.value["workspaces"] if row["path"] == str(target))
    assert row["status"] == "ready"
    (target / "new-work.txt").write_text("Created after the cached scan")
    results = finish(restarted, restarted.cleanup({"targets": [{"key": str(target)}]}))
    assert results[str(target)]["status"] == "skipped"
    assert "uncommitted" in results[str(target)]["message"]
    assert (target / "new-work.txt").exists()
    assert not calls(state, ["workspace", "close"])


def test_a_refresh_failure_keeps_the_previous_inventory_and_reports_it(site, monkeypatch):
    plugin, *_ = site
    plugin._refresh()
    kept = plugin.value["workspaces"]
    plugin = wso.WorkspaceOverview(plugin.home, plugin.fetch)
    monkeypatch.setattr(
        wso.WorkspaceOverview, "inventory", lambda *args: (_ for _ in ()).throw(OSError("no herdr"))
    )
    plugin._refresh()
    assert plugin.value["workspaces"] == kept
    assert "no herdr" in plugin.error
    assert plugin.snapshot()["stale"]


def test_an_unavailable_herdr_still_lists_worktrees(site, monkeypatch):
    plugin, *_ = site
    monkeypatch.setenv("PATH", "/usr/bin:/bin")  # Git remains; herdr does not.
    value = plugin.collect()
    assert any("herdr" in warning for warning in value["warnings"])
    assert {row["name"] for row in value["workspaces"]} >= {"merged-work", "dirty-work"}
    assert all(not row["workspace_ids"] for row in value["workspaces"])


def test_requested_refresh_respects_the_floor(site, monkeypatch):
    plugin, *_ = site
    plugin.value["synced_at"] = time.time()
    plugin.next_poll = time.time() + wso.INTERVAL
    plugin.request_refresh()
    assert plugin.next_poll > time.time()
    plugin.value["synced_at"] = time.time() - wso.MIN_REFRESH - 1
    plugin.request_refresh()
    assert plugin.next_poll == 0.0


def request(port, path, body=None, action="workspace-cleanup"):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        if body is None:
            connection.request("GET", path, headers={"Host": f"127.0.0.1:{port}"})
        else:
            connection.request(
                "POST",
                path,
                json.dumps(body),
                {
                    "Host": f"127.0.0.1:{port}",
                    "Content-Type": "application/json",
                    "X-Babysit-Action": action,
                },
            )
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


@pytest.fixture
def server(site):
    plugin = site[0]
    plugin.next_poll = float("inf")
    with dashboard.DashboardServer(plugin.home, 0, workspace_overview=plugin) as httpd:
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        yield httpd.server_port, plugin
        httpd.shutdown()
        thread.join(5)


def test_the_endpoint_serves_the_snapshot_and_starts_a_cleanup(server, site):
    port, plugin = server
    worktrees = site[3]
    status, value = request(port, "/api/workspace-overview")
    assert status == 200 and value["enabled"] and value["cleanup"] is None
    status, value = request(
        port, "/api/workspace-cleanup", {"targets": [{"key": str(worktrees / "merged-work")}]}
    )
    assert status == 200 and value["cleanup"]["status"] in {"running", "complete"}
    finish(plugin)
    status, value = request(port, "/api/workspace-overview")
    assert value["cleanup"]["results"][0]["status"] == "done"


def test_the_endpoint_rejects_an_invalid_cleanup_and_a_disabled_experiment(server, tmp_path):
    port, _ = server
    status, value = request(port, "/api/workspace-cleanup", {"targets": []})
    assert status == 400 and "Select between" in value["error"]
    with dashboard.DashboardServer(tmp_path, 0) as httpd:
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            assert request(httpd.server_port, "/api/workspace-overview")[1] == {"enabled": False}
            status, value = request(
                httpd.server_port, "/api/workspace-cleanup", {"targets": [{"key": "/w/one"}]}
            )
            assert status == 400 and "disabled" in value["error"]
        finally:
            httpd.shutdown()
            thread.join(5)


def test_a_failing_snapshot_stays_inside_the_experiment(server, monkeypatch):
    port, plugin = server
    monkeypatch.setattr(
        type(plugin), "snapshot", lambda self: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    assert request(port, "/api/workspace-overview") == (
        200,
        {"enabled": True, "error": "Workspace experiment unavailable"},
    )


@pytest.mark.parametrize("name,workspace_id", [("merged-work", "w1"), ("dirty-work", "w3")])
def test_open_workspace_reuses_or_creates_without_touching_agents(server, site, name, workspace_id):
    port, plugin = server
    _, _, _, worktrees, state, _ = site
    plugin.value = plugin.collect()
    before = read(state)["agents"]
    for _ in range(2):
        status, value = request(
            port, "/api/workspace-open", {"key": str(worktrees / name)}, action="workspace-open"
        )
        assert status == 200 and value == {"url": f"{wso.COLLIE_URL}/space/{workspace_id}"}
    opened = calls(state, ["worktree", "open"])
    assert len(opened) == (0 if name == "merged-work" else 1)
    if opened:
        assert "--no-focus" in opened[0]
    assert read(state)["agents"] == before
    assert not calls(state, ["workspace", "focus"])
    assert (worktrees / "dirty-work" / "scratch.txt").read_text() == "left behind"


def test_open_workspace_rejects_unknown_removed_and_disabled(site):
    plugin, _, _, worktrees, _, git = site
    plugin.value = plugin.collect()
    with pytest.raises(ValueError, match="Unknown workspace"):
        plugin.open_workspace({"key": "/not-listed"})
    key = str(worktrees / "issue-12-crash")
    git("worktree", "remove", key)
    with pytest.raises(ValueError, match="changed"):
        plugin.open_workspace({"key": key})
    plugin.enabled = False
    with pytest.raises(ValueError, match="disabled"):
        plugin.open_workspace({"key": str(worktrees / "merged-work")})


def test_diff_and_transcript_views_read_only_listed_checkouts(server, site, monkeypatch):
    port, plugin = server
    worktrees = site[3]
    monkeypatch.setenv("CODEX_HOME", str(plugin.home / "codex"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(plugin.home / "claude"))
    plugin.value = plugin.collect()
    key = quote(str(worktrees / "dirty-work"), safe="")
    status, value = request(port, f"/api/workspace-diff?key={key}")
    assert status == 200 and value["base"] == "origin/main"
    assert value["untracked"] == ["scratch.txt"] and value["files"] == []
    status, value = request(port, f"/api/workspace-diff?key={key}&scope=uncommitted")
    assert status == 200 and value["scope"] == "uncommitted"
    status, value = request(port, f"/api/workspace-transcript?key={key}")
    assert status == 200 and value["sessions"] == [] and value["session"] is None
    for path, error in [
        ("/api/workspace-diff?key=%2Fetc", "Unknown workspace"),
        ("/api/workspace-diff?key=workspace%3Aw2", "Unknown workspace"),
        ("/api/workspace-diff", "Supply a workspace or a workspace key"),
        (f"/api/workspace-diff?key={key}&workspace=w1", "Supply a workspace or"),
        (f"/api/workspace-diff?key={key}&key={key}", "once"),
        (f"/api/workspace-diff?key={key}&path=%2Fetc", "Unknown diff parameter"),
        (f"/api/workspace-diff?key={key}&base=HEAD", "listed base"),
        (f"/api/workspace-transcript?key={key}&before=-1", "numeric"),
        (f"/api/workspace-transcript?key={key}&session=..%2Fx", "listed session"),
        (f"/api/workspace-transcript?key={key}&file=x", "Unknown transcript parameter"),
    ]:
        status, value = request(port, path)
        assert status == 400 and error in value["error"], path


def test_a_failing_view_stays_inside_the_experiment(server, site, monkeypatch):
    port, plugin = server
    plugin.value = plugin.collect()
    monkeypatch.setattr(
        wso_viewer, "diff", lambda *a: (_ for _ in ()).throw(RuntimeError("git exploded"))
    )
    key = quote(str(site[3] / "merged-work"), safe="")
    assert request(port, f"/api/workspace-diff?key={key}") == (
        200,
        {"error": "Workspace view unavailable: git exploded"},
    )
    plugin.enabled = False
    status, value = request(port, f"/api/workspace-diff?key={key}")
    assert status == 400 and "disabled" in value["error"]


def test_views_need_the_experiment(tmp_path):
    with dashboard.DashboardServer(tmp_path, 0) as httpd:
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            status, value = request(httpd.server_port, "/api/workspace-transcript?key=%2Fw")
            assert status == 400 and "disabled" in value["error"]
        finally:
            httpd.shutdown()
            thread.join(5)


def test_views_of_a_herdr_workspace_need_no_experiment(site, tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    with dashboard.DashboardServer(tmp_path / "plain", 0) as httpd:
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            port = httpd.server_port
            status, value = request(port, "/api/workspace-diff?workspace=w1")
            assert status == 200 and value["base"] == "origin/main"
            status, value = request(port, "/api/workspace-transcript?workspace=w1")
            assert status == 200 and value["session"] is None
            for path, error in [
                ("/api/workspace-diff?workspace=w2", "no checkout"),
                ("/api/workspace-diff?workspace=w9", "not open"),
                ("/api/workspace-diff?workspace=..%2Fw1", "Supply a herdr workspace"),
                ("/api/workspace-diff?key=%2Fw", "disabled"),
            ]:
                status, value = request(port, path)
                assert status == 400 and error in value["error"], path
        finally:
            httpd.shutdown()
            thread.join(5)


def test_a_herdr_workspace_inside_a_checkout_is_refused(site, tmp_path, monkeypatch):
    plugin, _, clone, worktrees, state, _ = site
    data = read(state)
    nested = worktrees / "merged-work" / "sub"
    nested.mkdir()
    data["workspaces"][0]["worktree"]["checkout_path"] = str(nested)
    state.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="top of a Git checkout"):
        wso_viewer.workspace_checkout("w1")


def test_a_workspace_resolution_is_remembered_briefly(site, monkeypatch):
    state = site[4]
    first = wso_viewer.workspace_checkout("w1")
    lookups = len(calls(state, ["workspace", "list"]))
    assert wso_viewer.workspace_checkout("w1") == first
    assert len(calls(state, ["workspace", "list"])) == lookups
    monkeypatch.setattr(wso_viewer, "CHECKOUT_TTL", 0)
    wso_viewer.workspace_checkout("w1")
    assert len(calls(state, ["workspace", "list"])) == lookups + 1


def test_a_failing_herdr_stays_inside_the_view_boundary(server, monkeypatch):
    port, _ = server
    monkeypatch.setattr(
        wso_viewer, "herdr", lambda *a: (_ for _ in ()).throw(KeyError("workspaces"))
    )
    status, value = request(port, "/api/workspace-diff?workspace=w1")
    assert status == 200 and value["error"].startswith("Workspace view unavailable")


def test_messages_reach_one_agent_of_the_workspace(site, tmp_path, monkeypatch):
    state = site[4]
    with dashboard.DashboardServer(tmp_path / "plain", 0) as httpd:
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            port = httpd.server_port
            status, value = request(port, "/api/workspace-agents?workspace=w1")
            assert status == 200 and value["agents"] == [
                {
                    "pane": "w1:p1",
                    "agent": "codex",
                    "status": "idle",
                    "title": None,
                    "session": None,
                }
            ]
            assert value["path"].endswith("merged-work") and value["sessions"] == []
            body = {"workspace": "w1", "text": "Please add a test.\r\nThen push.\x1b[201~\x15"}
            status, value = request(
                port, "/api/workspace-message", body, action="workspace-message"
            )
            assert status == 200 and value == {"sent": True, "pane": "w1:p1", "warning": None}
            # Control characters never reach the terminal; the rest is plain text.
            assert read(state)["prompts"] == [["w1:p1", "Please add a test.\nThen push.[201~"]]
            for body, error in [
                ({"workspace": "w1", "text": " "}, "Write a message"),
                ({"workspace": "w1", "text": "x", "pane": "w9:p1"}, "no longer running"),
                ({"workspace": "w1", "text": "x", "extra": 1}, "Invalid message"),
                ({"workspace": "w9", "text": "x"}, "not open"),
                ({"workspace": "w2", "text": "x"}, "no checkout"),
            ]:
                status, value = request(
                    port, "/api/workspace-message", body, action="workspace-message"
                )
                assert status == 400 and error in value["error"], body
            assert len(read(state)["prompts"]) == 1
        finally:
            httpd.shutdown()
            thread.join(5)


def test_messages_need_a_single_unblocked_agent(site):
    import agent_messages

    state = site[4]
    data = read(state)
    data["agents"].append({**data["agents"][0], "pane_id": "w1:p2", "agent": "claude"})
    state.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="choose one"):
        agent_messages.send({"workspace": "w1", "text": "go"})
    assert (
        agent_messages.send({"workspace": "w1", "pane": "w1:p2", "text": "go"})["pane"] == "w1:p2"
    )
    data = read(state)
    data["agents"] = [{**data["agents"][0], "agent_status": "blocked"}]
    state.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="answer it in Collie"):
        agent_messages.send({"workspace": "w1", "text": "go"})
    data["agents"] = []
    state.write_text(json.dumps({**read(state), "agents": []}))
    with pytest.raises(ValueError, match="No agent is running"):
        agent_messages.send({"workspace": "w1", "text": "go"})


@pytest.mark.parametrize(
    ("code", "outcome"),
    [
        ("agent_blocked", "answer it in Collie"),
        ("agent_prompt_stalled", None),
        ("other", "could not deliver"),
    ],
)
def test_herdr_refusals_are_explained(site, monkeypatch, code, outcome):
    import agent_messages

    monkeypatch.setenv("FAKE_HERDR_PROMPT_ERROR", code)
    if outcome is None:
        value = agent_messages.send({"workspace": "w1", "text": "go"})
        assert value["sent"] and "no reaction" in value["warning"]
    else:
        with pytest.raises(ValueError, match=outcome):
            agent_messages.send({"workspace": "w1", "text": "go"})


def test_dash_leading_messages_are_text(site):
    import agent_messages

    agent_messages.send({"workspace": "w1", "text": "- rename foo\n- add a test"})
    assert read(site[4])["prompts"][-1] == ["w1:p1", "- rename foo\n- add a test"]
    prompt = calls(site[4], ["agent", "prompt"])[-1]
    assert prompt[-7:] == [
        "--wait",
        "--until",
        "working",
        "--until",
        "blocked",
        "--timeout",
        "8000",
    ]


@pytest.fixture
def exited(site, tmp_path, monkeypatch):
    """The workspace's agent has exited; its Claude session is recorded in the checkout."""
    plugin, _, _, worktrees, state, _ = site
    checkout = worktrees / "merged-work"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    folder = (
        tmp_path / "claude" / "projects" / "".join(c if c.isalnum() else "-" for c in str(checkout))
    )
    folder.mkdir(parents=True)
    sid = "8f2c6f0e-3b7a-4d34-9a8f-2b8d6c1e9a10"
    (folder / f"{sid}.jsonl").write_text(
        json.dumps(
            {
                "type": "user",
                "cwd": str(checkout),
                "sessionId": sid,
                "message": {"content": "Fix it"},
            }
        )
        + "\n"
    )
    data = read(state)
    data["agents"] = []
    data["panes"] = [
        {"pane_id": "w1:p1", "workspace_id": "w1"},
        {"pane_id": "w1:p2", "workspace_id": "w1"},
    ]
    data["process_info"] = {
        # p1 runs a server in the foreground; p2 is the shell at its prompt.
        "w1:p1": {"shell_pid": 10, "foreground_processes": [{"pid": 11, "cwd": str(checkout)}]},
        "w1:p2": {"shell_pid": 20, "foreground_processes": [{"pid": 20, "cwd": str(checkout)}]},
    }
    state.write_text(json.dumps(data))
    return state, sid, checkout, plugin.home


def test_a_message_resumes_an_exited_session_in_a_new_pane(exited):
    import agent_messages

    state, sid, checkout, home = exited
    assert agent_messages.sessions("w1", home) == [
        {"id": sid, "agent": "claude", "title": "Fix it", "updated": ANY, "watched": False}
    ]
    value = agent_messages.send({"workspace": "w1", "resume": sid, "text": "Now add docs"}, home)
    data = read(state)
    # Never the idle-looking shell in p2: a fresh split in the session's directory.
    assert data["splits"] == [
        ["w1:p1", "--direction", "right", "--cwd", str(checkout.resolve()), "--no-focus"]
    ]
    pane = data["panes"][-1]["pane_id"]
    assert value == {"sent": True, "pane": pane, "resumed": sid, "warning": None}
    assert [run[0] for run in data["runs"]] == [pane]
    assert data["runs"][0][1].startswith(f'claude --resume {sid} -- "$(cat ')
    agent = data["agents"][0]
    assert agent["argv"] == ["--resume", sid, "--", "Now add docs"]
    assert Path(agent["cwd"]).resolve() == checkout.resolve()
    # The staged prompt is removed once the agent has read it.
    assert not list((home / "message-prompts").iterdir())
    assert agent_messages._recent == {}
    # The agent now runs: a second resume is refused, a message goes to it instead.
    with pytest.raises(ValueError, match="already running"):
        agent_messages.send({"workspace": "w1", "resume": sid, "text": "again"}, home)


def test_a_session_open_elsewhere_gets_the_message_there(exited):
    import agent_messages

    state, sid, _, home = exited
    data = read(state)
    data["panes"].append(
        {"pane_id": "w7:p1", "workspace_id": "w7", "agent_session": {"value": sid}}
    )
    data["agents"] = [{"pane_id": "w7:p1", "workspace_id": "w7", "agent_status": "idle"}]
    state.write_text(json.dumps(data))
    value = agent_messages.send({"workspace": "w1", "resume": sid, "text": "go"}, home)
    assert value == {"sent": True, "pane": "w7:p1", "warning": None}
    data = read(state)
    assert data["prompts"] == [["w7:p1", "go"]]
    assert "splits" not in data and "runs" not in data
    data["agents"][0]["agent_status"] = "blocked"
    state.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="waiting on a question"):
        agent_messages.send({"workspace": "w1", "resume": sid, "text": "go"}, home)


def test_a_session_left_on_an_exited_pane_is_resumed(exited, monkeypatch):
    import agent_messages

    state, sid, _, home = exited
    data = read(state)
    # herdr keeps the session on a pane whose agent has exited back to the shell.
    data["panes"].append(
        {"pane_id": "w1:p3", "workspace_id": "w1", "agent_session": {"value": sid}}
    )
    state.write_text(json.dumps(data))
    monkeypatch.setattr(agent_messages, "RESUME_WAIT", 0)
    value = agent_messages.send({"workspace": "w1", "resume": sid, "text": "go"}, home)
    assert value["resumed"] == sid
    assert len(read(state)["runs"]) == 1


def test_a_just_resumed_session_is_not_resumed_again(exited, monkeypatch):
    import agent_messages

    state, sid, _, home = exited
    monkeypatch.setattr(agent_messages, "RESUME_WAIT", 0)
    first = agent_messages.send({"workspace": "w1", "resume": sid, "text": "go"}, home)
    assert "no agent appeared yet" in first["warning"]
    data = read(state)
    data["agents"] = []  # Not recognized yet: only the recent-resume record stops a repeat.
    state.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="just resumed"):
        agent_messages.send({"workspace": "w1", "resume": sid, "text": "go"}, home)
    assert len(read(state)["runs"]) == 1


def test_a_watched_session_is_left_to_the_watcher(exited):
    import fcntl

    import agent_messages
    import claude_runner
    import pr_supervisor as supervisor

    state, sid, _, home = exited
    db = supervisor.open_db(home)
    with db:
        supervisor.save_job(db, {"id": "w-1", "status": "watching", "session_id": sid})
    db.close()
    assert agent_messages.sessions("w1", home)[0]["watched"] is True
    with pytest.raises(ValueError, match="babysit watch w-1 \\(watching\\)"):
        agent_messages.send({"workspace": "w1", "resume": sid, "text": "go"}, home)
    db = supervisor.open_db(home)
    with db:
        supervisor.save_job(db, {"id": "w-1", "status": "stopped", "session_id": sid})
    db.close()
    # A repair holding the session lock right now, from any supervisor home.
    lock = claude_runner.config_home() / "babysit-pr-locks" / f"{sid}.lock"
    lock.parent.mkdir(parents=True)
    with lock.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        with pytest.raises(ValueError, match="repair is running"):
            agent_messages.send({"workspace": "w1", "resume": sid, "text": "go"}, home)
    assert not read(state).get("runs")


def test_resume_refuses_unknown_sessions_and_mixed_targets(exited):
    import agent_messages

    _, sid, _, home = exited
    for request, error in [
        (
            {"workspace": "w1", "resume": "1f2c6f0e-3b7a-4d34-9a8f-2b8d6c1e9a10", "text": "x"},
            "not recorded",
        ),
        ({"workspace": "w1", "resume": "../x", "text": "x"}, "Choose a recorded session"),
        ({"workspace": "w1", "resume": sid, "pane": "w1:p2", "text": "x"}, "not both"),
    ]:
        with pytest.raises(ValueError, match=error):
            agent_messages.send(request, home)


def test_old_resume_prompts_and_records_are_pruned(tmp_path, monkeypatch):
    import os
    import time

    import agent_messages

    old = tmp_path / "resume-old"
    new = tmp_path / "resume-new"
    for staged in (old, new):
        staged.write_text("x")
    os.utime(old, (time.time() - 90000, time.time() - 90000))
    recent = {"a": ("w1:p1", time.monotonic() - 999), "b": ("w1:p2", time.monotonic())}
    monkeypatch.setattr(agent_messages, "_recent", recent)
    agent_messages.prune(tmp_path)
    assert [p.name for p in tmp_path.iterdir()] == ["resume-new"]
    assert list(agent_messages._recent) == ["b"]


RULE = "─" * 40
QUESTIONS = [
    {
        "question": "Pick a color",
        "header": "Color",
        "multiSelect": False,
        "options": [{"label": "Red", "description": "Warm"}, {"label": "Blue"}],
    },
    {
        "question": "Pick toppings",
        "header": "Toppings",
        "multiSelect": True,
        "options": [{"label": "Cheese"}, {"label": "Olives"}, {"label": "Basil"}],
    },
]


def dialog_screen(question):
    """Claude's question dialog as herdr reads it from the pane."""
    options = [f"  {i}. {o['label']}" for i, o in enumerate(question["options"], 1)]
    return "\n".join(
        [
            "❯ earlier prompt that mentioned Pick a color",
            RULE,
            "←  ☐ Color  ☐ Toppings  ✔ Submit  →",
            question["question"],
            *options,
            f"  {len(options) + 1}. Type something.",
            RULE,
            f"  {len(options) + 2}. Chat about this",
            "Enter to select · Tab/Arrow keys to navigate · Esc to cancel",
        ]
    )


REVIEW = "\n".join(
    [
        RULE,
        "Review your answers",
        "Ready to submit your answers?",
        "❯ 1. Submit answers",
        "2. Cancel",
    ]
)


@pytest.fixture
def asking(exited, monkeypatch):
    """A running Claude agent waits on a two-question dialog."""
    state, sid, checkout, home = exited
    monkeypatch.setenv("FAKE_HERDR_KEEP_AGENT", "1")
    folder = next(p for p in (checkout.parents[3] / "claude" / "projects").iterdir())
    transcript = folder / f"{sid}.jsonl"
    asked = {
        "type": "assistant",
        "cwd": str(checkout),
        "sessionId": sid,
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_q1",
                    "name": "AskUserQuestion",
                    "input": {"questions": QUESTIONS},
                }
            ]
        },
    }
    transcript.write_text(transcript.read_text() + json.dumps(asked) + "\n")
    data = read(state)
    data["agents"] = [
        {
            "workspace_id": "w1",
            "pane_id": "w1:p1",
            "agent": "claude",
            "agent_status": "blocked",
            "agent_session": {"value": sid},
        }
    ]
    data["screens"] = {
        "w1:p1": [
            dialog_screen(QUESTIONS[0]),
            dialog_screen(QUESTIONS[1]),
            dialog_screen(QUESTIONS[1]),
            dialog_screen(QUESTIONS[1]),
            REVIEW,
            "done",
        ]
    }
    state.write_text(json.dumps(data))
    monkeypatch.setattr(__import__("agent_messages"), "DIALOG_WAIT", 0.3)
    return state, sid, transcript


def answer_request(sid, answers, **extra):
    return {
        "workspace": "w1",
        "pane": "w1:p1",
        "session": sid,
        "tool": "toolu_q1",
        "answers": answers,
        **extra,
    }


def keys_sent(state):
    return [
        c[3:]
        for c in read(state)["calls"]
        if c[:2] in (["agent", "send-keys"], ["pane", "send-text"])
    ]


def test_a_waiting_claude_dialog_is_answered_one_question_at_a_time(asking):
    import agent_messages

    state, sid, _ = asking
    assert agent_messages.agents("w1")[0]["session"] == sid
    value = agent_messages.answer(answer_request(sid, [{"options": [1]}, {"options": [2, 0]}]))
    assert value == {"answered": True, "pane": "w1:p1"}
    # Blue picks and moves on; toppings toggle, Right moves on; 1 submits the review.
    assert keys_sent(state) == [["2"], ["1"], ["3"], ["right"], ["1"]]


def test_a_typed_answer_goes_through_the_type_something_option(asking):
    import agent_messages

    state, sid, transcript = asking
    data = read(state)
    lone = [QUESTIONS[0]]
    lines = transcript.read_text().splitlines()
    record = json.loads(lines[-1])
    record["message"]["content"][0]["input"]["questions"] = lone
    transcript.write_text("\n".join(lines[:-1] + [json.dumps(record)]) + "\n")
    typing = dialog_screen(QUESTIONS[0]).replace("Esc to", "ctrl+g to edit in Vim · Esc to")
    data["screens"]["w1:p1"] = [dialog_screen(QUESTIONS[0]), typing, "done"]
    state.write_text(json.dumps(data))
    text = "Teal,\n\x1b[201~ please"
    agent_messages.answer(answer_request(sid, [{"text": text}]))
    # One line, with no control characters; a single question needs no review step.
    assert keys_sent(state) == [["3"], ["--", "Teal, [201~ please"], ["enter"]]


@pytest.mark.parametrize(
    ("change", "answers", "error"),
    [
        ({}, [{"options": [1]}], "Answer every question"),
        ({}, [{"options": [0, 1]}, {"options": [0]}], "Choose one listed option"),
        ({}, [{"options": [5]}, {"options": [0]}], "Choose one listed option"),
        ({}, [{"options": [True]}, {"options": [0]}], "Choose one listed option"),
        ({}, [{"options": [0]}, {"text": "x"}], "Write an answer"),
        ({}, [{"text": "  "}, {"options": [0]}], "Write an answer"),
        ({}, [{"options": [0], "text": "x"}, {"options": [0]}], "Choose options or write"),
        ({"agent_status": "working"}, [{"options": [0]}, {"options": [0]}], "not waiting"),
        ({"agent": "codex"}, [{"options": [0]}, {"options": [0]}], "no longer running"),
        ({"agent_session": {"value": "other"}}, [{"options": [0]}, {"options": [0]}], "no longer"),
    ],
)
def test_answers_are_refused_before_anything_is_typed(asking, change, answers, error):
    import agent_messages

    state, sid, _ = asking
    data = read(state)
    data["agents"][0].update(change)
    state.write_text(json.dumps(data))
    with pytest.raises(ValueError, match=error):
        agent_messages.answer(answer_request(sid, answers))
    assert keys_sent(state) == []


def test_nothing_is_typed_unless_the_dialog_shows_the_question(asking):
    import agent_messages

    state, sid, transcript = asking
    good = [{"options": [0]}, {"options": [0]}]
    with pytest.raises(ValueError, match="Invalid answer"):
        agent_messages.answer(answer_request(sid, good, extra=1))
    data = read(state)
    data["screens"]["w1:p1"] = [dialog_screen(QUESTIONS[1])]  # Moved on in the terminal.
    state.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="not on the agent's screen"):
        agent_messages.answer(answer_request(sid, good))
    assert keys_sent(state) == []
    # A dialog that stops following stops the answer, saying it is partly given.
    data["screens"]["w1:p1"] = [dialog_screen(QUESTIONS[0]), "something else"]
    state.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="Answered in part"):
        agent_messages.answer(answer_request(sid, good))
    assert keys_sent(state) == [["1"]]
    # An answered question is not answered again.
    result = {
        "type": "user",
        "cwd": "/",
        "sessionId": sid,
        "message": {
            "content": [{"type": "tool_result", "tool_use_id": "toolu_q1", "content": "done"}]
        },
        "toolUseResult": {"answers": {"Pick a color": "Red", "Pick toppings": "Cheese"}},
    }
    transcript.write_text(transcript.read_text() + json.dumps(result) + "\n")
    with pytest.raises(ValueError, match="already answered"):
        agent_messages.answer(answer_request(sid, good))


def test_the_dashboard_relays_answers(asking, tmp_path):
    state, sid, _ = asking
    with dashboard.DashboardServer(tmp_path / "plain", 0) as httpd:
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            body = answer_request(sid, [{"options": [0]}, {"options": [0, 1]}])
            status, value = request(
                httpd.server_port, "/api/workspace-answer", body, action="workspace-answer"
            )
            assert status == 200 and value["answered"], value
            status, value = request(
                httpd.server_port,
                "/api/workspace-answer",
                {**body, "answers": []},
                action="workspace-answer",
            )
            assert status == 400 and "Answer every question" in value["error"]
        finally:
            httpd.shutdown()
            thread.join(5)


def test_a_similar_question_or_a_bare_screen_is_not_taken_for_the_dialog(asking):
    import agent_messages

    state, sid, transcript = asking
    good = [{"options": [0]}, {"options": [0]}]
    data = read(state)
    # The next question starts with this one's words and repeats its options.
    longer = {**QUESTIONS[0], "question": "Pick a color for the border"}
    shell = "\n".join(["Pick a color", "  1. Red", "  2. Blue", "% "])
    for screen in (dialog_screen(longer), shell):
        data["screens"]["w1:p1"] = [screen]
        state.write_text(json.dumps(data))
        with pytest.raises(ValueError, match="not on the agent's screen"):
            agent_messages.answer(answer_request(sid, good))
    assert keys_sent(state) == []


def test_questions_with_too_many_options_for_digit_keys_are_left_to_the_terminal(asking):
    import agent_messages

    state, sid, transcript = asking
    lines = transcript.read_text().splitlines()
    record = json.loads(lines[-1])
    many = {**QUESTIONS[0], "options": [{"label": f"Option {i}"} for i in range(9)]}
    record["message"]["content"][0]["input"]["questions"] = [many]
    transcript.write_text("\n".join(lines[:-1] + [json.dumps(record)]) + "\n")
    for answer in ({"text": "mine"}, {"options": [0]}):
        with pytest.raises(ValueError, match="too many options"):
            agent_messages.answer(answer_request(sid, [answer]))
    assert keys_sent(state) == []


def test_an_agent_that_stops_waiting_mid_answer_stops_the_typing(asking, monkeypatch):
    import agent_messages

    state, sid, _ = asking
    real = agent_messages.herdr

    def herdr(*args):
        value = real(*args)
        if args[:2] == ("agent", "send-keys"):
            # The first key lands, then the agent is no longer waiting.
            data = read(state)
            data["agents"][0]["agent_status"] = "working"
            state.write_text(json.dumps(data))
        return value

    monkeypatch.setattr(agent_messages, "herdr", herdr)
    with pytest.raises(ValueError, match=r"Answered in part \(the agent stopped waiting\)"):
        agent_messages.answer(answer_request(sid, [{"options": [1]}, {"options": [0]}]))
    assert keys_sent(state) == [["2"]]
