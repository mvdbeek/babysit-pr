"""Temporary clones, fake herdr and fake GitHub; never touches live workspaces."""

import http.client
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import ANY
from urllib.parse import quote

import dashboard
import pr_workspaces
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
        self.graphql_error = "gh: Something went wrong"
        self.owners = {}  # Head -> login as GitHub spells it, when it differs.
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

    def __call__(self, endpoint, payload=None):
        if endpoint == "graphql":
            return self.graphql(payload)
        self.calls.append(endpoint)
        if endpoint in self.fail:
            raise ValueError("gh: Not Found (HTTP 404)")
        if endpoint in self.repos:
            return self.repos[endpoint]
        if "/pulls?" in endpoint:
            return self.heads.get(endpoint.split("head=")[1], [])
        raise ValueError(f"gh: unexpected endpoint {endpoint}")

    @staticmethod
    def node(value, typename, owner=None):
        merged = value.get("merged_at")
        node = {
            "__typename": typename,
            "number": value["number"],
            "title": value["title"],
            "url": value["html_url"],
            "state": "MERGED" if merged else value["state"].upper(),
        }
        if typename == "PullRequest":
            node["isDraft"] = bool(value.get("draft"))
            node["headRepositoryOwner"] = {"login": owner} if owner else None
        return node

    def graphql(self, payload):
        """Answers aliased lookups from the same tables, recording one call per request."""
        variables = payload["variables"]
        repo = f"{variables['owner']}/{variables['name']}"
        fields = re.findall(r"(a\d+): (pullRequests|issueOrPullRequest)", payload["query"])
        lookups = [("head" if f == "pullRequests" else "number", variables[a]) for a, f in fields]
        self.calls.append(("graphql", repo, tuple(lookups)))
        if "graphql" in self.fail:
            raise ValueError(self.graphql_error)
        if repo != BASE:
            missing = {"type": "NOT_FOUND", "path": ["repository"], "message": "Not found"}
            return {"data": {"repository": None}, "errors": [missing]}
        first = (
            int(re.search(r"first: (\d+)", payload["query"])[1])
            if "first:" in payload["query"]
            else 0
        )
        data, errors = {}, []
        for alias, (kind, value) in zip((a for a, _ in fields), lookups, strict=True):
            if kind == "head":
                nodes = [
                    self.node(pull, "PullRequest", self.owners.get(head, head.split(":")[0]))
                    for head, pulls in self.heads.items()
                    if head.split(":", 1)[1] == value
                    for pull in pulls
                ]
                nodes.sort(key=lambda node: -node["number"])
                data[alias] = {"nodes": nodes[:first]}
            elif str(value) in self.pulls:
                data[alias] = self.node(self.pulls[str(value)], "PullRequest", "fork")
            elif str(value) in self.issues:
                data[alias] = self.node(self.issues[str(value)], "Issue")
            else:
                data[alias] = None
                errors.append(
                    {
                        "type": "NOT_FOUND",
                        "path": ["repository", alias],
                        "message": f"Could not resolve to an issue or pull request with the number of {value}.",
                    }
                )
        return {"data": {"repository": data}, **({"errors": errors} if errors else {})}

    def lookups(self):
        return [lookup for call in self.calls if call[0] == "graphql" for lookup in call[2]]


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
    assert ("number", 9) not in fake.lookups()
    # Every checkout's lookups share one request, and both fork owners of a branch one alias.
    assert [call[:2] for call in fake.calls] == [("graphql", BASE)]
    assert fake.lookups().count(("head", "merged-work")) == 1


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
    huge = {"path": "/w/issue-1234567890", "branch": None, "upstream": None}
    assert wso.candidates(huge, BASE, "fork") == []
    named = {"path": "/w/pr-other-4", "branch": "pr-other-4", "upstream": None}
    assert wso.candidates(named, BASE, "fork") == [("head", "fork:pr-other-4")]
    mine = {"path": "/w/pr-base-4-2", "branch": "pr-base-4-2", "upstream": None}
    assert ("pr", "4") in wso.candidates(mine, BASE, "fork")
    fork_head = {"path": "/w/fix", "branch": "fix", "upstream": ["other/repo", "fix"]}
    assert wso.candidates(fork_head, BASE, "fork")[0] == ("head", "other:fix")


@pytest.mark.parametrize("base_branch", ["main", "master", "dev", "release_26.1", "25.0"])
def test_renamed_checkout_does_not_link_its_base_branch(base_branch):
    item = {
        "path": "/w/review-pr-9",
        "branch": "review-pr-9",
        "upstream": [BASE, base_branch],
    }
    assert wso.candidates(item, BASE, "fork") == [("head", "fork:review-pr-9")]


def test_renamed_checkout_links_release_prefixed_topic_branch():
    item = {
        "path": "/w/review-pr-9",
        "branch": "review-pr-9",
        "upstream": ["contributor/repo", "release_26.0_vtk_download"],
    }
    assert wso.candidates(item, BASE, "fork")[0] == (
        "head",
        "contributor:release_26.0_vtk_download",
    )


@pytest.mark.parametrize("head_repo", [BASE, "contributor/repo"])
def test_review_checkout_links_renamed_tracking_branch_after_cached_miss(site, head_repo):
    plugin, fake, _, worktrees, _, git = site
    branch = "review-pr-9"
    head_branch = "dependabot/npm_and_yarn/client/markdown-it-14.3.3"
    git("worktree", "add", "-b", branch, str(worktrees / branch))
    # A previous scan missed the link by looking up the local name in origin.
    assert rows(plugin)[branch]["links"] == []
    assert plugin.links[f"{BASE}#head#fork:{branch}"]["link"]["state"] == "none"
    fake.calls.clear()
    git("remote", "add", "wtpr-review", f"https://github.com/{head_repo}.git")
    git("config", f"branch.{branch}.remote", "wtpr-review")
    git("config", f"branch.{branch}.merge", f"refs/heads/{head_branch}")
    fake.heads[f"{head_repo.split('/')[0]}:{head_branch}"] = [fake.pulls["9"]]

    row = rows(plugin)[branch]

    assert row["links"] == [
        {
            "kind": "pr",
            "repo": BASE,
            "number": 9,
            "title": "Named pull request",
            "url": f"https://github.com/{BASE}/pull/9",
            "state": "open",
            "draft": True,
        }
    ]
    assert row["status"] == "active" and not row["stale_links"]
    assert fake.lookups() == [("head", head_branch)]


def test_saving_keeps_every_live_link_and_only_the_newest_others(site, monkeypatch):
    plugin, *_ = site
    plugin.collect()
    live = set(plugin.links)
    assert live == plugin.live
    for index in range(3):
        plugin.links[f"{BASE}#issue#{900 + index}"] = {"link": {}, "checked_at": index + 1}
    for key in live:
        plugin.links[key]["checked_at"] = 0  # Older than every leftover.
    monkeypatch.setattr(wso, "MAX_LINKS", 1)
    plugin.save()
    assert set(plugin.links) == live | {f"{BASE}#issue#902"}


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
    assert fake.calls == [("graphql", BASE, (("head", "merged-work"),))]


def test_one_head_lookup_answers_every_fork_owner_of_a_branch(site):
    plugin, fake, _, worktrees, _, git = site
    git("remote", "add", "other", "https://github.com/other/repo.git")
    git("config", "branch.merged-work.remote", "other")
    git("config", "branch.merged-work.merge", "refs/heads/merged-work")
    fake.heads["other:merged-work"] = [
        {
            "number": 30,
            "title": "Other fork",
            "html_url": f"https://github.com/{BASE}/pull/30",
            "state": "open",
        }
    ]
    links = rows(plugin)["merged-work"]["links"]
    assert [link["number"] for link in links] == [30]
    assert fake.lookups().count(("head", "merged-work")) == 1
    assert plugin.links[f"{BASE}#head#fork:merged-work"]["link"]["number"] == 7


def test_an_unknown_number_fails_only_its_own_lookup(site):
    plugin, fake, _, worktrees, _, git = site
    git("worktree", "add", "-b", "issue-404-gone", str(worktrees / "issue-404-gone"))
    value = plugin.collect()
    found = {row["name"]: row for row in value["workspaces"]}
    assert found["issue-404-gone"]["stale_links"] and not found["issue-404-gone"]["links"]
    assert found["issue-12-crash"]["links"][0]["number"] == 12
    assert not found["issue-12-crash"]["stale_links"]
    assert any("issue 404: Could not resolve" in warning for warning in value["warnings"])
    assert f"{BASE}#issue#404" not in plugin.links


def test_an_exhausted_budget_resolves_never_seen_lookups_before_aged_ones(site, monkeypatch):
    plugin, fake, _, worktrees, _, git = site
    plugin.collect()
    for entry in plugin.links.values():
        entry["checked_at"] -= wso.SETTLED_TTL
    git("worktree", "add", "-b", "issue-44-new", str(worktrees / "issue-44-new"))
    fake.calls.clear()
    monkeypatch.setattr(wso, "LOOKUP_BATCH", 1)
    monkeypatch.setattr(wso, "CALL_BUDGET", 2)
    value = plugin.collect()
    assert sorted(fake.lookups()) == [("head", "issue-44-new"), ("number", 44)]
    found = {row["name"]: row for row in value["workspaces"]}
    assert found["issue-44-new"]["links"][0]["state"] == "open"
    # Aged entries keep their last answer, marked stale, without a warning.
    assert found["merged-work"]["links"][0]["number"] == 7 and found["merged-work"]["stale_links"]
    assert not any("budget" in warning for warning in value["warnings"])


def test_fork_owners_match_whatever_case_github_spells_them_in(site):
    plugin, fake, *_ = site
    fake.owners["fork:merged-work"] = "Fork"
    assert rows(plugin)["merged-work"]["links"][0]["number"] == 7


def test_a_crowded_branch_name_is_settled_by_the_exact_head_query(site, monkeypatch):
    plugin, fake, *_ = site
    monkeypatch.setattr(wso, "HEAD_PULLS", 1)
    fake.heads["other:merged-work"] = [
        {
            "number": 99,
            "title": "Another fork's newer branch",
            "html_url": f"https://github.com/{BASE}/pull/99",
            "state": "open",
        }
    ]
    links = rows(plugin)["merged-work"]["links"]
    assert [link["number"] for link in links] == [7]
    assert (
        f"repos/{BASE}/pulls?state=all&per_page=5&sort=created&direction=desc&head=fork:merged-work"
        in fake.calls
    )


def test_a_failed_request_is_reported_once_and_a_rate_limit_stops_the_scan(site, monkeypatch):
    plugin, fake, *_ = site
    fake.fail.add("graphql")
    value = plugin.collect()
    unresolved = [w for w in value["warnings"] if "unresolved" in w]
    assert len(unresolved) == 1
    assert re.fullmatch(r"\d+ GitHub lookups unresolved: gh: Something went wrong", unresolved[0])
    assert all(row["stale_links"] for row in value["workspaces"] if row["branch"])
    fake.calls.clear()
    fake.graphql_error = "gh: API rate limit exceeded for user ID 1"
    monkeypatch.setattr(wso, "LOOKUP_BATCH", 1)
    plugin.collect()
    assert len([call for call in fake.calls if call[0] == "graphql"]) == 1


def test_an_unknown_repository_fails_its_lookups_without_caching(site):
    plugin, fake, *_ = site
    plugin.collect()
    plugin.links.clear()
    for entry in plugin.bases.values():
        entry["repo"] = "gone/repo"
    value = plugin.collect()
    assert any("unresolved: Not found" in warning for warning in value["warnings"])
    assert not plugin.links


def test_rows_with_fresh_links_do_not_wait_for_github(site, monkeypatch):
    plugin, fake, _, worktrees, *_ = site
    plugin.collect()
    plugin.links[f"{BASE}#head#fork:merged-work"]["checked_at"] -= wso.SETTLED_TTL
    gate = threading.Event()
    published = threading.Event()
    answer = fake.graphql

    def slow(payload):
        assert gate.wait(timeout=10)
        return answer(payload)

    def publish(row):
        if row["name"] == "dirty-work":
            published.set()

    monkeypatch.setattr(fake, "graphql", slow)
    worker = threading.Thread(target=plugin.collect, kwargs={"publish": publish})
    worker.start()
    try:
        assert published.wait(timeout=10)
    finally:
        gate.set()
        worker.join(timeout=10)
    assert not worker.is_alive()


def test_partial_graphql_answers_never_look_like_lost_credentials():
    def result(stdout, stderr=""):
        return subprocess.CompletedProcess(["gh"], 1, stdout, stderr)

    titles = '{"data":{"repository":{"a0":{"title":"Fix gh auth login"}}}}'
    assert not wso.github_cli.unauthenticated(result(titles, "gh: Could not resolve"))
    assert not wso.github_cli.unauthenticated(result(titles.encode(), b""))
    assert wso.github_cli.unauthenticated(result(titles, "gh: HTTP 401"))
    assert wso.github_cli.unauthenticated(result("Requires authentication"))


def test_gh_keeps_a_partial_graphql_answer_and_rejects_other_failures(monkeypatch):
    answer = {"data": {"repository": {"a0": None}}, "errors": [{"path": ["repository", "a0"]}]}
    seen = []

    def run(args, **kwargs):
        seen.append((args, kwargs.get("input")))
        stdout = json.dumps(answer) if "graphql" in args else ""
        return subprocess.CompletedProcess(args, 1, stdout, "gh: Not Found")

    monkeypatch.setattr(wso.github_cli, "run", run)
    assert wso.gh_json("graphql", {"query": "q"}) == answer
    assert seen[0][0][-2:] == ["--input", "-"] and json.loads(seen[0][1]) == {"query": "q"}
    with pytest.raises(ValueError, match="Not Found"):
        wso.gh_json("repos/fork/repo")
    assert seen[1] == (["gh", "api", "--hostname", "github.com", "repos/fork/repo"], None)


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

    def collect(publish=None, local_done=None):
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
        assert status == 200 and value == {
            "url": f"{pr_workspaces.COLLIE_URL}/space/{workspace_id}"
        }
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
        ("/api/workspace-diff", "Supply a workspace, a workspace key or a watch"),
        (f"/api/workspace-diff?key={key}&workspace=w1", "Supply a workspace,"),
        (f"/api/workspace-diff?key={key}&watch=w1", "Supply a workspace,"),
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


def test_qualified_workspace_ids_name_a_registered_machine(site, tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    home = tmp_path / "plain"
    home.mkdir()
    mini = {"id": "mini", "label": "Mac mini", "ssh": "mini", "os": "darwin"}
    (home / "machines.json").write_text(json.dumps({"version": 1, "machines": [mini]}))
    queue = sqlite3.connect(home / "queue.sqlite")
    queue.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, data TEXT NOT NULL)")
    job = {"id": "abc123", "machine": "mini", "cwd": str(site[3] / "dirty-work")}
    queue.execute("INSERT INTO jobs VALUES ('abc123', ?)", (json.dumps(job),))
    queue.commit()
    queue.close()
    with dashboard.DashboardServer(home, 0) as httpd:
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            port = httpd.server_port
            status, value = request(port, "/api/workspace-diff?workspace=w1")
            assert status == 200 and value["base"] == "origin/main"
            for route in ("workspace-diff", "workspace-transcript", "workspace-agents"):
                # Views read a worker's workspace there; nothing else acts on it yet.
                remote = "not open" if route != "workspace-agents" else "cannot be opened"
                for workspace, error in [
                    ("unknown%2Fw1", "Unknown machine unknown"),
                    ("mini%2Fw1", remote),
                ]:
                    path = f"/api/{route}?workspace={workspace}"
                    status, value = request(port, path)
                    assert status == 400 and error in value["error"], path
            # The same path here is another checkout than the watch's on mini.
            status, value = request(port, "/api/workspace-diff?watch=abc123")
            assert status == 400 and "on machine mini" in value["error"]
            body = {"workspace": "unknown/w1", "pane": "w1:p1", "message": "hi"}
            status, value = request(port, "/api/workspace-message", body, "workspace-message")
            assert status == 400 and "Unknown machine unknown" in value["error"]
            assert not calls(site[4], ["agent", "prompt"])
        finally:
            httpd.shutdown()
            thread.join(5)


def test_views_of_a_watch_read_its_checkout_without_a_workspace(site, tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    home = tmp_path / "plain"
    home.mkdir()
    queue = sqlite3.connect(home / "queue.sqlite")
    queue.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, data TEXT NOT NULL)")
    job = {"id": "abc123", "cwd": str(site[3] / "dirty-work"), "session_id": None}
    queue.execute("INSERT INTO jobs VALUES ('abc123', ?)", (json.dumps(job),))
    queue.commit()
    queue.close()
    with dashboard.DashboardServer(home, 0) as httpd:
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            port = httpd.server_port
            status, value = request(port, "/api/workspace-diff?watch=abc123")
            assert status == 200 and value["untracked"] == ["scratch.txt"]
            status, value = request(port, "/api/workspace-transcript?watch=abc123")
            assert status == 200 and value["session"] is None
            status, value = request(port, "/api/workspace-diff?watch=other")
            assert status == 400 and "Unknown watch" in value["error"]
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
                    "docker": True,
                }
            ]
            assert value["path"].endswith("merged-work") and value["sessions"] == []
            # Text that sends the reader to Collie links this workspace there.
            assert value["url"] == f"{pr_workspaces.COLLIE_URL}/space/w1"
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
        ("agent_not_running", "exited instead of taking the message"),
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


@pytest.fixture
def working(site, monkeypatch):
    """A Claude agent in the workspace is busy with a turn of session s1."""
    state = site[4]
    monkeypatch.setenv("FAKE_HERDR_KEEP_AGENT", "1")
    data = read(state)
    data["agents"] = [
        {**data["agents"][0], "agent": "claude", "agent_status": "working"},
        {
            **data["agents"][0],
            "pane_id": "w1:p2",
            "agent_status": "working",
            "agent_session": {"value": "s1"},
        },
    ]
    data["agents"][0]["agent_session"] = {"value": "s1-other"}
    state.write_text(json.dumps(data))
    return state


def stop_request(**extra):
    return {"workspace": "w1", "pane": "w1:p2", "session": "s1", **extra}


def test_stop_sends_esc_to_the_working_agent_and_waits_for_it(working):
    import agent_messages

    assert agent_messages.interrupt(stop_request()) == {
        "stopped": True,
        "pane": "w1:p2",
        "warning": None,
    }
    assert calls(working, ["agent", "send-keys"]) == [["agent", "send-keys", "w1:p2", "esc"]]
    assert calls(working, ["agent", "wait"]) == [
        [
            "agent",
            "wait",
            "w1:p2",
            "--until",
            "idle",
            "--until",
            "done",
            "--until",
            "blocked",
            "--timeout",
            "5000",
        ]
    ]


@pytest.mark.parametrize("exits", [False, True])
def test_a_failed_wait_reports_only_an_agent_still_working(working, monkeypatch, exits):
    import agent_messages

    real = agent_messages.herdr

    def herdr(*args):
        if args[:2] == ("agent", "wait"):
            if exits:
                # The agent quit on Esc, so herdr has nothing left to wait on.
                data = read(working)
                data["agents"] = data["agents"][:1]
                working.write_text(json.dumps(data))
            raise ValueError('{"code":"timeout","message":"timed out waiting for agent status"}')
        return real(*args)

    monkeypatch.setattr(agent_messages, "herdr", herdr)
    value = agent_messages.interrupt(stop_request())
    if exits:
        assert value == {"stopped": True, "pane": "w1:p2", "warning": None}
    else:
        assert not value["stopped"] and "still shows as working" in value["warning"]
    assert calls(working, ["agent", "send-keys"]) == [["agent", "send-keys", "w1:p2", "esc"]]


@pytest.mark.parametrize(
    ("change", "request_", "error"),
    [
        (None, stop_request(session="s1-other"), "no longer running"),
        (None, stop_request(pane="w1:p9"), "no longer running"),
        (None, stop_request(session=None), "no longer running"),
        ({"agent_status": "idle"}, stop_request(), "not working"),
        ({"agent_status": "blocked"}, stop_request(), "not working"),
        (None, {"workspace": "w1", "pane": "w1:p2"}, "Invalid stop"),
        (None, stop_request(extra=1), "Invalid stop"),
        (None, stop_request(session=5), "Invalid stop"),
        (None, stop_request(workspace="w9"), "not open"),
    ],
)
def test_stop_is_refused_before_any_key_is_sent(working, change, request_, error):
    import agent_messages

    if change:
        data = read(working)
        data["agents"][1].update(change)
        working.write_text(json.dumps(data))
    with pytest.raises(ValueError, match=error):
        agent_messages.interrupt(request_)
    assert calls(working, ["agent", "send-keys"]) == []


def test_an_agent_without_a_session_can_be_stopped(working):
    import agent_messages

    data = read(working)
    del data["agents"][1]["agent_session"]
    working.write_text(json.dumps(data))
    assert agent_messages.interrupt(stop_request(session=None))["stopped"]


def test_the_dashboard_relays_stops(working, tmp_path):
    with dashboard.DashboardServer(tmp_path / "plain", 0) as httpd:
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            port = httpd.server_port
            status, value = request(
                port, "/api/workspace-interrupt", stop_request(), action="workspace-interrupt"
            )
            assert status == 200 and value["stopped"], value
            status, value = request(
                port,
                "/api/workspace-interrupt",
                stop_request(session="s9"),
                action="workspace-interrupt",
            )
            assert status == 400 and "no longer running" in value["error"]
            # The action needs its own header, as every dashboard action does.
            status, _ = request(port, "/api/workspace-interrupt", stop_request(), action="cancel")
            assert status == 404
            assert len(calls(working, ["agent", "send-keys"])) == 1
        finally:
            httpd.shutdown()
            thread.join(5)


QUIT_RULE = "─" * 40
CLAUDE_READY = "\n".join(["⏺ Done.", "", QUIT_RULE, "❯ ", QUIT_RULE, "  ? for shortcuts"])
CLAUDE_EXIT_TYPED = CLAUDE_READY.replace("❯ ", "❯ /exit")
CODEX_READY = "› \x1b[2mAsk Codex to do anything\x1b[0m"
# ps naming a pane's root process: an interactive login shell, unless the state's
# "commands" (by pid) say otherwise.
PS = """import json,os,sys
from pathlib import Path
data=json.loads(Path(os.environ['FAKE_HERDR']).read_text()); a=sys.argv[1:]
assert a[:3]==['-o','args=','-p'], a
print(data.get('commands',{}).get(a[3],'-zsh'))
"""


def quit_agent_state(kind="claude", status="idle", session="s1"):
    """Pane w1:p2 runs one agent its shell started, on ``session``."""
    return (
        {
            "pane_id": "w1:p2",
            "agent": kind,
            "agent_status": status,
            "agent_session": {"value": session},
        },
        {
            "shell_pid": 100,
            "foreground_processes": [
                {"pid": 4242, "argv0": f"/opt/bin/{kind}", "argv": [kind], "cwd": "/src/w"}
            ],
        },
        [CLAUDE_READY, CLAUDE_EXIT_TYPED] if kind == "claude" else [CODEX_READY],
    )


@pytest.fixture
def quitting(site, monkeypatch):
    """An idle Claude on session s1 in pane w1:p2, beside the workspace's Codex. Esc leaves
    an agent idle; any other key exits it, leaving only the pane's shell."""
    import agent_messages
    import herdr_handoff

    state = site[4]
    ps = state.parent / "bin" / "ps"
    ps.write_text(f"#!{sys.executable} -S\n" + PS)
    ps.chmod(0o700)
    monkeypatch.setattr(agent_messages, "PS", str(ps))
    data = read(state)
    agent, procs, screens = quit_agent_state()
    data["agents"] = [data["agents"][0], {**data["agents"][0], **agent}]
    data["process_info"] = {"w1:p2": procs}
    data["screens"] = {"w1:p2": screens}
    state.write_text(json.dumps(data))
    real = herdr_handoff.herdr

    def herdr(*args, machine=None):
        if args[:2] == ("agent", "send-keys") and args[3] == "esc":
            # Stops the turn, or dismisses the dialog; the agent stays, now idle.
            data = read(state)
            data["calls"].append(list(args))
            for agent in data["agents"]:
                if agent.get("pane_id") == args[2]:
                    agent["agent_status"] = "idle"
            state.write_text(json.dumps(data))
            return "{}"
        return real(*args, machine=machine)

    def kill(pid, signal):
        assert (pid, signal) == (4242, 0)
        if not any(a.get("pane_id") == "w1:p2" for a in read(state)["agents"]):
            raise ProcessLookupError()

    monkeypatch.setattr(herdr_handoff, "herdr", herdr)
    monkeypatch.setattr(herdr_handoff.os, "kill", kill)
    return state


def set_quit_agent(state, *args, **changes):
    agent, procs, screens = quit_agent_state(*args)
    data = read(state)
    data["agents"][1].update(agent, **changes)
    data["process_info"]["w1:p2"] = procs
    data["screens"]["w1:p2"] = screens
    state.write_text(json.dumps(data))
    return data


def keys(state):
    return [call[3] for call in calls(state, ["agent", "send-keys", "w1:p2"])]


def test_quit_types_exit_into_an_idle_claude_and_sees_it_leave(quitting):
    import agent_messages

    assert agent_messages.exit_agent(stop_request()) == {
        "quit": True,
        "pane": "w1:p2",
        "warning": None,
    }
    assert read(quitting)["typed_text"] == [["w1:p2", "/exit"]]
    assert keys(quitting) == ["enter"]
    assert calls(quitting, ["agent", "wait"]) == []
    # The pane stays, its shell alone in the foreground.
    assert read(quitting)["process_info"]["w1:p2"]["foreground_processes"] == [{"pid": 100}]


def test_an_idle_codex_gets_ctrl_d(quitting):
    import agent_messages

    set_quit_agent(quitting, "codex")
    assert agent_messages.exit_agent(stop_request())["quit"]
    assert keys(quitting) == ["ctrl+d"]
    assert "typed_text" not in read(quitting)


@pytest.mark.parametrize("kind", ["claude", "codex"])
@pytest.mark.parametrize("status", ["working", "blocked"])
def test_a_busy_agent_is_stopped_with_esc_before_it_is_quit(quitting, kind, status):
    import agent_messages

    data = set_quit_agent(quitting, kind, status)
    if kind == "claude":
        # A dialog on screen; Esc dismisses it, and the composer shows again.
        data["screens"]["w1:p2"] = [
            "Allow?\n❯ 1. Yes\nEsc to cancel",
            CLAUDE_READY,
            CLAUDE_EXIT_TYPED,
        ]
        quitting.write_text(json.dumps(data))
    assert agent_messages.exit_agent(stop_request())["quit"]
    assert keys(quitting) == ["esc", "enter" if kind == "claude" else "ctrl+d"]
    # A stopped turn may leave herdr calling Claude blocked; the settle decides on its screen.
    # A dialog's Esc must leave blocked behind.
    until = ["idle", "done", *(["blocked"] if status == "working" else [])]
    assert calls(quitting, ["agent", "wait"]) == [
        ["agent", "wait", "w1:p2", *(x for u in until for x in ("--until", u)), "--timeout", "5000"]
    ]


def test_a_stopped_claude_herdr_wrongly_calls_blocked_is_quit(quitting, monkeypatch):
    import agent_messages
    import herdr_handoff

    data = set_quit_agent(quitting, "claude", "working")
    data["screens"]["w1:p2"] = [CLAUDE_READY, CLAUDE_READY, CLAUDE_EXIT_TYPED]
    quitting.write_text(json.dumps(data))
    stop = herdr_handoff.herdr

    def herdr(*args, machine=None):
        value = stop(*args, machine=machine)
        if args[3:4] == ("esc",):
            data = read(quitting)
            data["agents"][1]["agent_status"] = "blocked"
            quitting.write_text(json.dumps(data))
        return value

    monkeypatch.setattr(herdr_handoff, "herdr", herdr)
    assert agent_messages.exit_agent(stop_request())["quit"]
    assert keys(quitting) == ["esc", "enter"]


def test_a_claude_herdr_wrongly_calls_blocked_is_quit_without_esc(quitting):
    import agent_messages

    set_quit_agent(quitting, "claude", "blocked")
    assert agent_messages.exit_agent(stop_request())["quit"]
    assert keys(quitting) == ["enter"]


def test_an_agent_still_busy_after_esc_is_not_quit(quitting, monkeypatch):
    import agent_messages
    import herdr_handoff

    set_quit_agent(quitting, "codex", "working")
    real = herdr_handoff.herdr

    def herdr(*args, machine=None):
        if args[:2] == ("agent", "wait"):
            raise RuntimeError('{"code":"timeout"}')
        return real(*args, machine=machine)

    monkeypatch.setattr(herdr_handoff, "herdr", herdr)
    with pytest.raises(ValueError, match="did not settle; it was not quit"):
        agent_messages.exit_agent(stop_request())
    assert keys(quitting) == ["esc"]


@pytest.mark.parametrize(
    ("change", "request_", "error"),
    [
        (None, stop_request(session="s1-other"), "no longer running"),
        (None, stop_request(pane="w1:p9"), "no longer running"),
        (None, stop_request(session=None), "no longer running"),
        (None, {"workspace": "w1", "pane": "w1:p2"}, "Invalid quit"),
        (None, stop_request(extra=1), "Invalid quit"),
        (None, stop_request(pane=""), "Invalid quit"),
        (None, stop_request(workspace="w9"), "not open"),
        ({"agent": "aider"}, stop_request(), "Only Claude and Codex"),
        ({"root": True}, stop_request(), "root process; quitting it would close the pane"),
        ({"twice": True}, stop_request(), "Could not identify the agent's process"),
        ({"draft": True}, stop_request(), "composer holds a draft"),
        ({"scrolled": True}, stop_request(), "terminal is scrolled"),
    ],
)
def test_quit_is_refused_before_any_key_is_sent(quitting, monkeypatch, change, request_, error):
    import agent_messages
    import herdr_handoff

    data = read(quitting)
    procs = data["process_info"]["w1:p2"]
    change = change or {}
    if "agent" in change:
        data["agents"][1]["agent"] = change["agent"]
    if change.get("root"):
        procs["foreground_processes"][0]["pid"] = procs["shell_pid"]
    if change.get("twice"):
        procs["foreground_processes"].append({**procs["foreground_processes"][0], "pid": 4243})
    if change.get("draft"):
        data["screens"]["w1:p2"] = [CLAUDE_READY.replace("❯ ", "❯ half a thought")]
    quitting.write_text(json.dumps(data))
    if change.get("scrolled"):
        real = herdr_handoff.result

        def result(*args, machine=None):
            value = real(*args, machine=machine)
            if args[:2] == ("pane", "get"):
                value["pane"]["scroll"] = {"offset_from_bottom": 3}
            return value

        monkeypatch.setattr(herdr_handoff, "result", result)
    with pytest.raises(ValueError, match=error):
        agent_messages.exit_agent(request_)
    assert keys(quitting) == []
    assert "typed_text" not in read(quitting)


def test_an_unconfirmed_exit_is_reported_without_repeating_a_key(quitting, monkeypatch):
    import agent_messages
    import herdr_handoff

    monkeypatch.setenv("FAKE_HERDR_KEEP_AGENT", "1")
    monkeypatch.setattr(herdr_handoff, "EXIT_WAIT", 0)
    value = agent_messages.exit_agent(stop_request())
    assert not value["quit"] and value["warning"].startswith("Exit unconfirmed")
    assert keys(quitting) == ["enter"]


def test_an_exit_is_attempted_once_per_process(quitting, monkeypatch):
    import agent_messages
    import herdr_handoff

    monkeypatch.setenv("FAKE_HERDR_KEEP_AGENT", "1")
    monkeypatch.setattr(herdr_handoff, "EXIT_WAIT", 0)
    assert not agent_messages.exit_agent(stop_request())["quit"]
    # The first exit may still be taking: a second could reach the shell.
    with pytest.raises(ValueError, match="already attempted"):
        agent_messages.exit_agent(stop_request())
    assert keys(quitting) == ["enter"]
    assert read(quitting)["typed_text"] == [["w1:p2", "/exit"]]


def test_a_working_codex_with_a_draft_is_refused_before_esc(quitting):
    import agent_messages

    data = set_quit_agent(quitting, "codex", "working")
    data["screens"]["w1:p2"] = ["• Working (3s • esc to interrupt)\n\n› also check the docs"]
    quitting.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="holds a draft"):
        agent_messages.exit_agent(stop_request())
    assert keys(quitting) == []


@pytest.mark.parametrize(
    "command",
    [
        "bash -c claude",
        "bash /opt/start-claude.sh",
        "zsh -lc claude",
        "python3 run.py",
        "fish --command=claude",
        "fish --command claude",
        "fish -i --command=claude",
    ],
)
def test_an_agent_whose_pane_root_is_no_interactive_shell_is_not_quit(quitting, command):
    import agent_messages

    data = read(quitting)
    data["commands"] = {"100": command}
    quitting.write_text(json.dumps(data))
    # The root would exit with the agent, closing the pane.
    with pytest.raises(ValueError, match="not an interactive shell.*nothing was sent"):
        agent_messages.exit_agent(stop_request())
    assert keys(quitting) == [] and "typed_text" not in read(quitting)


def test_the_root_shell_is_named_by_the_system_ps_never_one_on_path(fake_tools):
    import agent_messages

    # A ps earlier on PATH would call any process an interactive shell.
    ps = fake_tools.parent / "bin" / "ps"
    ps.write_text(f"#!{sys.executable} -S\n" + PS)
    ps.chmod(0o700)
    assert not agent_messages.interactive_shell(os.getpid())


@pytest.mark.parametrize(
    "command", ["-zsh", "/bin/bash -l", "fish", "zsh --login -i", "bash --norc -i"]
)
def test_an_agent_under_an_interactive_shell_is_quit(quitting, command):
    import agent_messages

    data = read(quitting)
    data["commands"] = {"100": command}
    quitting.write_text(json.dumps(data))
    assert agent_messages.exit_agent(stop_request())["quit"]


@pytest.mark.parametrize("status", ["watching", "handoff", "awaiting_release"])
def test_a_session_a_babysit_watch_owns_is_not_quit(quitting, tmp_path, status):
    import agent_messages

    home = tmp_path / "home"
    save_watch(home, "s1", status=status)
    with pytest.raises(ValueError, match=f"Not quit: babysit watch w-1 \\({status}\\)"):
        agent_messages.exit_agent(stop_request(), home)
    assert keys(quitting) == []
    save_watch(home, "s1", status="stopped")
    assert agent_messages.exit_agent(stop_request(), home)["quit"]


def save_exit_watch(home, where, state="watching", **row):
    """An exit watch of each kind the dashboard follows, on pane w1:p2 unless ``row`` says
    otherwise: a message's, a ``wt`` launch's, a scheduled task's or a cron run's."""
    import exit_watch

    followed = {"state": state, "since": 1, "message": "Waiting for the agent to finish"}
    if where == "message":
        exit_watch.register(
            home,
            {
                "id": "m1",
                "label": "Message: go",
                "machine": None,
                "pane_id": "w1:p2",
                "session_id": "s1",
                "exit": followed,
                **row,
            },
        )
    elif where == "launch":
        exit_watch.register(
            home,
            {
                "id": "l1",
                "label": "wt task",
                "machine": None,
                "exit": {**followed, "target": {"pane_id": "w1:p2", "session_id": "s1"}},
                **row,
            },
        )
    elif where == "scheduled":
        db = sqlite3.connect(home / "pr-workspaces.sqlite")
        with db:
            db.execute("CREATE TABLE IF NOT EXISTS scheduled (id TEXT PRIMARY KEY, data TEXT)")
            data = {"id": "t1", "exit": {**followed, "target": {"pane_id": "w1:p2"}}, **row}
            db.execute("INSERT INTO scheduled VALUES (?,?)", ("t1", json.dumps(data)))
        db.close()
    else:
        db = sqlite3.connect(home / "cron.sqlite")
        with db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS runs "
                "(id INTEGER PRIMARY KEY AUTOINCREMENT, job TEXT, data TEXT)"
            )
            data = {
                "kind": "agent",
                "status": "running",
                "agent_run": {"pane": "w1:p2", "workspace_id": "w1"},
                "exit": followed,
                **row,
            }
            db.execute("INSERT INTO runs (job, data) VALUES (?,?)", ("j1", json.dumps(data)))
        db.close()


@pytest.mark.parametrize("where", ["message", "launch", "scheduled", "cron"])
@pytest.mark.parametrize("state", ["watching", "exiting"])
def test_an_agent_an_exit_watch_will_quit_is_not_quit(quitting, tmp_path, where, state):
    import agent_messages

    home = tmp_path / "home"
    home.mkdir()
    save_exit_watch(home, where, state)
    # Two exits at once could send the second key to the pane's shell.
    with pytest.raises(ValueError, match="quits by itself once its task is done"):
        agent_messages.exit_agent(stop_request(), home)
    assert keys(quitting) == [] and "typed_text" not in read(quitting)


@pytest.mark.parametrize(
    ("where", "state", "row"),
    [
        ("message", "left_open", {}),
        ("launch", "exited", {}),
        ("message", "watching", {"pane_id": "w1:p9", "session_id": "s9"}),
        ("message", "watching", {"machine": "mini"}),
        ("cron", "watching", {"agent_run": {"pane": "w1:p2", "machine": "mini"}}),
        ("cron", "watching", {"status": "done"}),
    ],
)
def test_ended_and_other_exit_watches_leave_the_quit_alone(quitting, tmp_path, where, state, row):
    import agent_messages

    home = tmp_path / "home"
    home.mkdir()
    save_exit_watch(home, where, state, **row)
    assert agent_messages.exit_agent(stop_request(), home)["quit"]


def test_unreadable_exit_watches_refuse_the_quit(quitting, tmp_path):
    import agent_messages

    home = tmp_path / "home"
    # A database that cannot even be opened.
    (home / "pr-workspaces.sqlite").mkdir(parents=True)
    with pytest.raises(ValueError, match="Could not read the exit watches"):
        agent_messages.exit_agent(stop_request(), home)
    assert keys(quitting) == []


def test_an_exit_watch_row_this_version_cannot_read_is_skipped(quitting, tmp_path):
    import agent_messages

    home = tmp_path / "home"
    home.mkdir()
    save_exit_watch(home, "message", pane_id="w1:p9", session_id="s9")
    db = sqlite3.connect(home / "pr-workspaces.sqlite")
    with db:
        db.execute("INSERT INTO exit_watches VALUES (?,?)", ("odd", "[1]"))
    db.close()
    assert agent_messages.exit_agent(stop_request(), home)["quit"]


def test_a_quit_never_overlaps_another_exit_of_the_pane(quitting, tmp_path, monkeypatch):
    import agent_messages
    import herdr_handoff

    home = tmp_path / "home"
    # An exit watch or handoff, in another process, sending its exit keys right now.
    with herdr_handoff.pane_lock(home, "w1:p2"):
        with pytest.raises(ValueError, match="Another exit of this agent is under way"):
            agent_messages.exit_agent(stop_request(), home)
    assert keys(quitting) == [] and "typed_text" not in read(quitting)
    sent = []
    send_exit = herdr_handoff.send_exit

    def held(*args, **kwargs):
        # The quit's own keys go out under the same lock.
        with pytest.raises(BlockingIOError):
            with herdr_handoff.pane_lock(home, "w1:p2", wait=False):
                pass
        sent.append(True)
        return send_exit(*args, **kwargs)

    monkeypatch.setattr(herdr_handoff, "send_exit", held)
    assert agent_messages.exit_agent(stop_request(), home)["quit"]
    assert sent == [True]


@pytest.mark.parametrize("failure", ["enter", "kill"])
def test_a_failed_call_after_the_exit_key_is_reported_as_uncertain(quitting, monkeypatch, failure):
    import agent_messages
    import herdr_handoff

    typed = herdr_handoff.herdr

    def herdr(*args, machine=None):
        if args[:2] == ("agent", "send-keys") and failure == "enter":
            raise subprocess.TimeoutExpired(["herdr", *args], 15)
        return typed(*args, machine=machine)

    def kill(pid, signal):
        raise PermissionError("Operation not permitted")

    monkeypatch.setattr(herdr_handoff, "herdr", herdr)
    if failure == "kill":
        monkeypatch.setattr(herdr_handoff.os, "kill", kill)
    value = agent_messages.exit_agent(stop_request())
    assert value["quit"] is False and "may have been sent" in value["warning"]


def test_a_failed_call_before_any_key_stays_an_error(quitting, monkeypatch):
    import agent_messages
    import herdr_handoff

    set_quit_agent(quitting, "codex")
    looked = herdr_handoff.inspect
    seen = []

    def inspect(pane, machine=None):
        seen.append(pane)
        if len(seen) > 1:
            raise subprocess.TimeoutExpired(["herdr"], 15)
        return looked(pane, machine)

    monkeypatch.setattr(herdr_handoff, "inspect", inspect)
    # The dashboard reports it as an error: nothing was sent.
    with pytest.raises(subprocess.TimeoutExpired):
        agent_messages.exit_agent(stop_request())
    assert keys(quitting) == []


def test_a_failed_esc_call_is_reported_as_uncertain(quitting, monkeypatch):
    import agent_messages
    import herdr_handoff

    set_quit_agent(quitting, "codex", "working")
    sent = herdr_handoff.herdr

    def herdr(*args, machine=None):
        if args[3:4] == ("esc",):
            raise subprocess.TimeoutExpired(["herdr", *args], 15)
        return sent(*args, machine=machine)

    monkeypatch.setattr(herdr_handoff, "herdr", herdr)
    value = agent_messages.exit_agent(stop_request())
    assert value["quit"] is False and "may have been sent" in value["warning"]


def test_a_working_claude_with_a_draft_is_refused_before_esc(quitting):
    import agent_messages

    data = set_quit_agent(quitting, "claude", "working")
    data["screens"]["w1:p2"] = [
        "✻ Thinking… (esc to interrupt)\n" + CLAUDE_READY.replace("❯ ", "❯ also check the docs")
    ]
    quitting.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="holds a draft"):
        agent_messages.exit_agent(stop_request())
    assert keys(quitting) == []


@pytest.mark.parametrize(
    ("after_esc", "error"),
    [
        ({"pid": 5151}, "changed while its turn was stopped"),
        ({"agent_status": "working"}, "busy again"),
        ({"agent_session": {"value": "s2"}}, "no longer running"),
        ({"gone": True}, "no longer running"),
    ],
)
def test_after_esc_only_the_same_idle_agent_is_quit(quitting, monkeypatch, after_esc, error):
    import agent_messages
    import herdr_handoff

    set_quit_agent(quitting, "codex", "working")
    stop = herdr_handoff.herdr

    def herdr(*args, machine=None):
        value = stop(*args, machine=machine)
        if args[3:4] == ("esc",):
            data = read(quitting)
            if "pid" in after_esc:
                data["process_info"]["w1:p2"]["foreground_processes"][0]["pid"] = 5151
            elif after_esc.get("gone"):
                data["agents"] = data["agents"][:1]
            else:
                data["agents"][1].update(after_esc)
            quitting.write_text(json.dumps(data))
        return value

    monkeypatch.setattr(herdr_handoff, "herdr", herdr)
    with pytest.raises(ValueError, match=error):
        agent_messages.exit_agent(stop_request())
    assert keys(quitting) == ["esc"]


def test_a_stopped_turn_gets_a_moment_to_redraw_its_composer(quitting, monkeypatch):
    import agent_messages
    import herdr_handoff

    set_quit_agent(quitting, "codex", "working")
    # Codex's screen is read only after Esc: blank at first, then its empty composer.
    screens = iter(["", CODEX_READY])
    monkeypatch.setattr(herdr_handoff, "read_screen", lambda pane, machine=None: next(screens))
    monkeypatch.setattr(agent_messages.time, "sleep", lambda seconds: None)
    assert agent_messages.exit_agent(stop_request())["quit"]
    assert keys(quitting) == ["esc", "ctrl+d"]


def test_a_typed_exit_that_never_shows_gets_no_enter(quitting, monkeypatch):
    import agent_messages
    import herdr_handoff

    data = read(quitting)
    data["screens"]["w1:p2"] = [CLAUDE_READY]
    quitting.write_text(json.dumps(data))
    monkeypatch.setattr(herdr_handoff, "TYPED_EXIT_WAIT", 0)
    value = agent_messages.exit_agent(stop_request())
    assert not value["quit"] and "Enter not sent" in value["warning"]
    assert keys(quitting) == []


def test_the_dashboard_relays_quits_and_names_this_machine_beside_workers(quitting, tmp_path):
    home = tmp_path / "plain"
    with dashboard.DashboardServer(home, 0) as httpd:
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            port = httpd.server_port
            status, value = request(port, "/api/workspace-agents?workspace=w1")
            assert status == 200 and value["machine_label"] is None
            home.mkdir(exist_ok=True)
            (home / "machines.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "machines": [
                            {"id": "mini", "label": "Mac mini", "ssh": "mini.test", "os": "darwin"}
                        ],
                    }
                )
            )
            status, value = request(port, "/api/workspace-agents?workspace=w1")
            assert value["machine_label"] in {"this Mac", "this machine"}
            status, value = request(
                port, "/api/workspace-quit", stop_request(session="s9"), action="workspace-quit"
            )
            assert status == 400 and "no longer running" in value["error"]
            # A worker's agents are not quit from here yet.
            status, value = request(
                port,
                "/api/workspace-quit",
                stop_request(workspace="mini/w1"),
                action="workspace-quit",
            )
            assert status == 400 and "cannot be opened" in value["error"]
            status, _ = request(port, "/api/workspace-quit", stop_request(), action="cancel")
            assert status == 404
            assert keys(quitting) == []
            status, value = request(
                port, "/api/workspace-quit", stop_request(), action="workspace-quit"
            )
            assert status == 200 and value["quit"], value
            assert keys(quitting) == ["enter"]
        finally:
            httpd.shutdown()
            thread.join(5)


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


def test_a_resume_without_a_message_leaves_the_session_waiting(exited):
    import agent_messages

    state, sid, checkout, home = exited
    value = agent_messages.send({"workspace": "w1", "resume": sid, "text": "  "}, home)
    assert value["resumed"] == sid and value["warning"] is None
    assert read(state)["agents"][0]["argv"] == ["--resume", sid]
    # A message to a running agent still needs text.
    with pytest.raises(ValueError, match="Write a message"):
        agent_messages.send({"workspace": "w1", "text": ""}, home)


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
    assert not agent.get("safe_enable")
    assert Path(agent["cwd"]).resolve() == checkout.resolve()
    # The staged prompt is removed once the agent has read it.
    assert not list((home / "message-prompts").iterdir())
    assert agent_messages._recent == {}
    # The agent now runs: a second resume is refused, a message goes to it instead.
    with pytest.raises(ValueError, match="already running"):
        agent_messages.send({"workspace": "w1", "resume": sid, "text": "again"}, home)


def test_a_codex_session_resumes_without_the_shared_background_server(exited):
    import agent_messages

    state, _, checkout, home = exited
    sid = "c0ffee00-0000-4000-8000-000000000009"
    folder = Path(os.environ["CODEX_HOME"]) / "sessions" / "2026" / "10" / "03"
    folder.mkdir(parents=True)
    (folder / f"rollout-{sid}.jsonl").write_text(
        json.dumps({"type": "session_meta", "payload": {"id": sid, "cwd": str(checkout)}}) + "\n"
    )
    value = agent_messages.send({"workspace": "w1", "resume": sid, "text": "go on"}, home)
    assert value["resumed"] == sid and value["warning"] is None
    # On the shared server a session with other feature settings opens a dialog whose
    # default, Cancel, the message's Enter would pick.
    assert read(state)["runs"][0][1].startswith(
        f'codex -c check_for_update_on_startup=false --no-daemon resume {sid} -- "$(cat '
    )
    assert read(state)["agents"][0]["argv"] == [
        "-c",
        "check_for_update_on_startup=false",
        "--no-daemon",
        "resume",
        sid,
        "--",
        "go on",
    ]


def test_a_session_open_elsewhere_gets_the_message_there(exited):
    import agent_messages

    state, sid, _, home = exited
    data = read(state)
    data["panes"].append(
        {"pane_id": "w7:p1", "workspace_id": "w7", "agent_session": {"value": sid}}
    )
    data["agents"] = [
        {"pane_id": "w7:p1", "workspace_id": "w7", "agent": "claude", "agent_status": "idle"}
    ]
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


@pytest.mark.parametrize("name", ["default", "work"])
def test_named_account_resume_checks_its_lock_and_restores_its_store(
    exited, tmp_path, monkeypatch, name
):
    import fcntl
    import shutil

    import agent_messages
    import claude_accounts

    state, sid, _, home = exited
    original = Path(os.environ["CLAUDE_CONFIG_DIR"])
    monkeypatch.setenv("HOME", str(tmp_path / "account-home"))
    account = claude_accounts.account_home(name, create=True)
    shutil.move(original / "projects", account / "projects")
    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    assert agent_messages.sessions("w1", home)[0]["claude_account"] == name
    lock = account / "babysit-pr-locks" / f"{sid}.lock"
    lock.parent.mkdir()
    with lock.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        with pytest.raises(ValueError, match="repair is running"):
            agent_messages.send({"workspace": "w1", "resume": sid, "text": "go"}, home)
    assert not read(state).get("runs")
    result = agent_messages.send({"workspace": "w1", "resume": sid, "text": "go"}, home)
    assert result["sent"] and result["warning"] is None
    if name == "work":
        assert read(state)["agents"][0]["claude_config_dir"] == str(account)
    else:
        assert "claude_config_dir" not in read(state)["agents"][0]
        assert "unset CLAUDE_CONFIG_DIR" in read(state)["runs"][0][1]


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
    assert "session is not confirmed yet" in first["warning"]
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


def save_watch(home, sid, watch_id="w-1", **changes):
    import pr_supervisor as supervisor

    db = supervisor.open_db(home)
    with db:
        supervisor.save_job(
            db,
            {
                "id": watch_id,
                "status": "blocked",
                "session_id": sid,
                "epoch": 0,
                "repo": BASE,
                "url": f"https://github.com/{BASE}/pull/7",
                "snapshot": {"pr": {"number": 7, "title": "Merged work"}},
                **changes,
            },
        )
    db.close()


def load_watch(home, watch_id="w-1"):
    import pr_supervisor as supervisor

    db = supervisor.open_db(home)
    try:
        return supervisor.get_job(db, watch_id)
    finally:
        db.close()


WATCH = {
    "id": "w-1",
    "status": "blocked",
    "stop_after_run": False,
    "repo": BASE,
    "number": 7,
    "title": "Merged work",
    "branch": None,
    "url": f"https://github.com/{BASE}/pull/7",
}


def test_a_refusal_names_the_watch_that_owns_the_session(exited):
    import agent_messages

    state, sid, _, home = exited
    save_watch(home, sid)
    # Named before any click, so the composer can offer to cancel it up front.
    assert agent_messages.sessions("w1", home)[0]["watch"] == WATCH
    with pytest.raises(agent_messages.Refused) as refused:
        agent_messages.send({"workspace": "w1", "resume": sid, "text": "go"}, home)
    assert str(refused.value) == (
        "Not resumed: babysit watch w-1 (blocked) resumes this session itself; stop it first"
    )
    assert refused.value.details == {"code": "watch_owns_session", "watch": WATCH}
    # Only the watch the reader confirmed is cancelled; another one is named instead.
    with pytest.raises(agent_messages.Refused) as refused:
        agent_messages.send(
            {"workspace": "w1", "resume": sid, "text": "go", "stop_watch": "w-9"}, home
        )
    assert refused.value.details["code"] == "watch_owns_session"
    assert load_watch(home)["status"] == "blocked"
    with pytest.raises(ValueError, match="Only a resume can cancel"):
        agent_messages.send({"workspace": "w1", "pane": "w1:p1", "text": "go", "stop_watch": "w-1"})
    assert not read(state).get("runs")


def test_cancelling_the_owning_watch_resumes_its_session(exited):
    import agent_messages

    state, sid, _, home = exited
    save_watch(home, sid)
    value = agent_messages.send(
        {"workspace": "w1", "resume": sid, "text": "Address review comment", "stop_watch": "w-1"},
        home,
    )
    assert value["resumed"] == sid and value["warning"] is None
    job = load_watch(home)
    assert job["status"] == "stopped" and job["summary"] == "Watch cancelled"
    assert read(state)["agents"][0]["argv"] == ["--resume", sid, "--", "Address review comment"]
    assert "watch" not in agent_messages.sessions("w1", home)[0]


def test_a_watch_in_a_repair_is_never_resumed_over(exited):
    import fcntl

    import agent_messages
    import claude_runner

    state, sid, _, home = exited
    save_watch(home, sid, status="running")
    assert agent_messages.sessions("w1", home)[0]["watch"]["status"] == "running"
    # The cancellation waits for the repair; the session is left to it meanwhile.
    with pytest.raises(agent_messages.Refused) as refused:
        agent_messages.send(
            {"workspace": "w1", "resume": sid, "text": "go", "stop_watch": "w-1"}, home
        )
    assert refused.value.details["code"] == "watch_finishing_repair"
    assert refused.value.details["watch"]["stop_after_run"] is True
    job = load_watch(home)
    assert job["status"] == "running" and job["stop_after_run"]
    # A repair that holds the session lock refuses the resume even once the watch ended.
    save_watch(home, sid, status="stopped")
    lock = claude_runner.config_home() / "babysit-pr-locks" / f"{sid}.lock"
    lock.parent.mkdir(parents=True)
    with lock.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        with pytest.raises(agent_messages.Refused) as refused:
            agent_messages.send(
                {"workspace": "w1", "resume": sid, "text": "go", "stop_watch": "w-1"}, home
            )
    assert refused.value.details == {"code": "repair_running"}
    assert "repair is running" in str(refused.value)
    assert not read(state).get("runs")


def test_an_ended_watch_does_not_block_a_resume(exited):
    import agent_messages

    state, sid, _, home = exited
    save_watch(home, sid, status="closed")
    session = agent_messages.sessions("w1", home)[0]
    assert session["watched"] is False and "watch" not in session
    # A confirmation that arrives after the watch ended just resumes.
    value = agent_messages.send(
        {"workspace": "w1", "resume": sid, "text": "go", "stop_watch": "w-1"}, home
    )
    assert value["resumed"] == sid
    assert load_watch(home)["status"] == "closed"
    assert len(read(state)["runs"]) == 1


def test_the_dashboard_relays_a_watch_refusal_and_its_cancellation(exited, server):
    port, _ = server
    state, sid, _, home = exited
    save_watch(home, sid)
    body = {"workspace": "w1", "resume": sid, "text": "go"}
    status, value = request(port, "/api/workspace-message", body, action="workspace-message")
    assert status == 400
    assert value == {
        "error": "Not resumed: babysit watch w-1 (blocked) resumes this session itself; stop it first",
        "code": "watch_owns_session",
        "watch": WATCH,
    }
    status, value = request(
        port, "/api/workspace-message", {**body, "stop_watch": "w-1"}, action="workspace-message"
    )
    assert status == 200 and value["resumed"] == sid, value
    assert load_watch(home)["status"] == "stopped"
    assert len(read(state)["runs"]) == 1


def close_workspace(state, workspace_id):
    data = read(state)
    data["workspaces"] = [w for w in data["workspaces"] if w["workspace_id"] != workspace_id]
    data["agents"] = [a for a in data["agents"] if a["workspace_id"] != workspace_id]
    data["panes"] = [p for p in data.get("panes", []) if p["workspace_id"] != workspace_id]
    state.write_text(json.dumps(data))


def test_a_closed_workspace_is_reopened_to_resume_its_session(exited, site, monkeypatch):
    import agent_messages

    state, sid, checkout, home = exited
    clone = site[2]
    assert wso_viewer.workspace_checkout("w1")  # The viewer saw it open.
    close_workspace(state, "w1")
    monkeypatch.setattr(wso_viewer, "CHECKOUT_TTL", 0)
    # Its checkout is still shown, with no agent but the sessions recorded there.
    assert wso_viewer.live_workspace("w1") is None
    assert agent_messages.agents("w1") == []
    assert [s["id"] for s in agent_messages.sessions("w1", home)] == [sid]
    value = agent_messages.send({"workspace": "w1", "resume": sid, "text": "Now add docs"}, home)
    assert calls(state, ["worktree", "open"]) == [
        [
            "worktree",
            "open",
            "--cwd",
            str(clone.resolve()),
            "--path",
            str(checkout.resolve()),
            "--no-focus",
        ]
    ]
    data = read(state)
    reopened = data["workspaces"][-1]["workspace_id"]
    # The reopened workspace's own new shell runs the resume; nothing is split.
    assert value == {"sent": True, "pane": f"{reopened}:p1", "resumed": sid, "warning": None}
    assert "splits" not in data
    assert data["agents"][0]["argv"] == ["--resume", sid, "--", "Now add docs"]
    assert wso_viewer.live_workspace("w1") == reopened
    with pytest.raises(ValueError, match="already running"):
        agent_messages.send({"workspace": "w1", "resume": sid, "text": "again"}, home)


def test_a_row_listed_without_a_workspace_resumes_its_session_in_a_new_one(exited, server, site):
    port, plugin = server
    state, sid, checkout, home = exited
    clone = site[2]
    close_workspace(state, "w1")
    plugin.value = plugin.collect()
    key = str(checkout.resolve())
    assert rows(plugin)["merged-work"]["workspace_ids"] == []
    query = f"/api/workspace-agents?key={quote(key, safe='')}"
    status, value = request(port, query)
    assert status == 200 and value["workspace"] is None and value["url"] is None
    assert value["agents"] == [] and [s["id"] for s in value["sessions"]] == [sid]
    assert value["path"] == key
    for body, error in [
        ({"key": key, "text": "x"}, "only a recorded session"),
        ({"key": key, "workspace": "w1", "resume": sid, "text": "x"}, "only a recorded session"),
        ({"key": "/etc", "resume": sid, "text": "x"}, "Unknown workspace"),
    ]:
        status, value = request(port, "/api/workspace-message", body, action="workspace-message")
        assert status == 400 and error in value["error"], body
    body = {"key": key, "resume": sid, "text": "Now add docs"}
    status, value = request(port, "/api/workspace-message", body, action="workspace-message")
    assert calls(state, ["worktree", "open"]) == [
        ["worktree", "open", "--cwd", str(clone.resolve()), "--path", key, "--no-focus"]
    ]
    reopened = read(state)["workspaces"][-1]["workspace_id"]
    assert status == 200
    assert value == {"sent": True, "pane": f"{reopened}:p1", "resumed": sid, "warning": None}
    assert read(state)["agents"][0]["argv"] == ["--resume", sid, "--", "Now add docs"]
    # The row is still listed without it; the viewer finds and follows the new one.
    status, value = request(port, query)
    assert status == 200 and value["workspace"] == reopened
    assert [agent["pane"] for agent in value["agents"]] == [f"{reopened}:p1"]
    status, value = request(port, "/api/workspace-message", body, action="workspace-message")
    assert status == 400 and "already running" in value["error"]
    status, value = request(port, f"{query}&workspace=w1")
    assert status == 400 and "Supply a workspace, a workspace key or a watch" in value["error"]


def test_the_viewer_follows_a_workspace_reopened_under_a_new_id(site, tmp_path, monkeypatch):
    _, _, _, worktrees, state, _ = site
    assert wso_viewer.workspace_checkout("w1")
    data = read(state)
    data["workspaces"][0]["workspace_id"] = "w7"
    data["agents"][0].update(workspace_id="w7", pane_id="w7:p1")
    state.write_text(json.dumps(data))
    monkeypatch.setattr(wso_viewer, "CHECKOUT_TTL", 0)
    with dashboard.DashboardServer(tmp_path / "plain", 0) as httpd:
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            port = httpd.server_port
            status, value = request(port, "/api/workspace-agents?workspace=w1")
            assert status == 200 and value["workspace"] == "w7"
            assert value["url"] == f"{pr_workspaces.COLLIE_URL}/space/w7"
            assert [agent["pane"] for agent in value["agents"]] == ["w7:p1"]
            assert value["path"] == str((worktrees / "merged-work").resolve())
            body = {"workspace": "w1", "text": "Carry on"}
            status, value = request(
                port, "/api/workspace-message", body, action="workspace-message"
            )
            assert status == 200 and value["pane"] == "w7:p1"
            # Closed for good: the checkout is still shown, but nobody can be messaged.
            close_workspace(state, "w7")
            status, value = request(port, "/api/workspace-agents?workspace=w1")
            assert status == 200 and value["workspace"] is None and value["url"] is None
            assert value["agents"] == [] and value["path"].endswith("merged-work")
        finally:
            httpd.shutdown()
            thread.join(5)


def test_a_closed_workspace_never_follows_one_that_was_open_beside_it(site, monkeypatch):
    _, _, _, worktrees, state, _ = site
    checkout = str(worktrees / "merged-work")
    data = read(state)
    data["workspaces"].append(
        {"workspace_id": "w5", "label": "monitor", "worktree": {"checkout_path": checkout}}
    )
    state.write_text(json.dumps(data))
    assert wso_viewer.live_workspace("w1") == "w1"
    close_workspace(state, "w1")
    monkeypatch.setattr(wso_viewer, "CHECKOUT_TTL", 0)
    # w5 was another workspace on the same checkout all along, not w1 reopened.
    assert wso_viewer.live_workspace("w1") is None
    data = read(state)
    data["workspaces"].append(
        {"workspace_id": "w6", "label": "merged-work", "worktree": {"checkout_path": checkout}}
    )
    state.write_text(json.dumps(data))
    assert wso_viewer.live_workspace("w1") == "w6"
    data["workspaces"].append(
        {"workspace_id": "w8", "label": "other", "worktree": {"checkout_path": checkout}}
    )
    state.write_text(json.dumps(data))
    # Two opened since: neither is known to be the one.
    assert wso_viewer.live_workspace("w1") is None


def test_an_id_reused_for_another_checkout_is_refused(site):
    _, _, _, worktrees, state, _ = site
    assert wso_viewer.workspace_checkout("w1")
    data = read(state)
    data["workspaces"][0]["worktree"]["checkout_path"] = str(worktrees / "dirty-work")
    state.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="another checkout"):
        wso_viewer.live_workspace("w1")


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


@pytest.fixture
def missing_question(asking):
    state, sid, transcript = asking
    # Claude may be blocked before the tool call reaches the JSONL transcript.
    transcript.write_text(transcript.read_text().splitlines()[0] + "\n")
    screen = (
        dialog_screen(QUESTIONS[0])
        .replace("←  ☐ Color  ☐ Toppings  ✔ Submit  →", " ☐ Color")
        .replace("Pick a color\n", "Pick a\ncolor\n")
        .replace("  1. Red", "❯ 1. Red\n     Warm")
    )
    data = read(state)
    data["screens"]["w1:p1"] = [screen, "done"]
    state.write_text(json.dumps(data))
    return state, sid, screen


@pytest.mark.parametrize("typed", [False, True])
def test_missing_transcript_question_can_be_answered_from_the_screen(missing_question, typed):
    import agent_messages

    state, sid, screen = missing_question
    target = agent_messages.docker_status("w1")[0]
    assert "docker" in target
    interaction = target["interaction"]
    question = interaction["question"]
    assert interaction["screen"] == screen
    assert question["questions"][0] == {
        "header": "Color",
        "question": "Pick a color",
        "multi": False,
        "options": [{"label": "Red", "description": "Warm"}, {"label": "Blue", "description": ""}],
    }
    if typed:
        data = read(state)
        data["screens"]["w1:p1"] = [
            screen,
            screen.replace("Esc to", "ctrl+g to edit · Esc to"),
            "done",
        ]
        state.write_text(json.dumps(data))
    value = agent_messages.answer(
        answer_request(
            sid, [{"text": "Teal"}] if typed else [{"options": [1]}], tool=question["id"]
        )
    )
    assert value["answered"]
    assert keys_sent(state) == ([["3"], ["Teal"], ["enter"]] if typed else [["2"]])
    if typed:
        assert read(state)["typed_text"] == [["w1:p1", "Teal"]]


def test_changed_live_question_is_not_answered(missing_question):
    import agent_messages

    state, sid, screen = missing_question
    question = agent_messages.agents("w1", interactions=True)[0]["interaction"]["question"]
    data = read(state)
    data["screens"]["w1:p1"] = [screen.replace("Warm", "Different meaning")]
    state.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="screen changed"):
        agent_messages.answer(answer_request(sid, [{"options": [0]}], tool=question["id"]))
    assert keys_sent(state) == []


@pytest.mark.parametrize("mode", ["shell", "multiple", "editing", "truncated", "approval"])
def test_unsupported_live_dialogs_are_visible_without_answer_controls(missing_question, mode):
    import agent_messages

    state, _, screen = missing_question
    screen = {
        "shell": "Pick a color\n1. Red\n2. Blue\n%",
        "multiple": screen.replace("☐ Color", "☐ Color  ☐ Toppings  ✔ Submit"),
        "editing": screen.replace("Esc to", "ctrl+g to edit · Esc to"),
        "truncated": screen.replace("3. Type something.", ""),
        "approval": screen.replace("☐ Color", "Allow this command?"),
    }[mode]
    data = read(state)
    data["screens"]["w1:p1"] = [screen]
    state.write_text(json.dumps(data))
    interaction = agent_messages.agents("w1", interactions=True)[0]["interaction"]
    assert (interaction["screen"], interaction["question"], interaction["idle"]) == (
        screen,
        None,
        False,
    )
    # Its numbered options can still be picked one key at a time, unless typing is open.
    labels = [o["label"] for o in (interaction["choices"] or {}).get("options", [])]
    assert labels == (
        ["Red", "Blue", "Chat about this"] if mode in {"multiple", "approval"} else []
    )
    assert keys_sent(state) == []


PERMISSION = "\n".join(
    [
        "⏺ Bash(rm -rf build)",
        RULE,
        " Bash command",
        "",
        "   rm -rf build",
        "   Remove the build directory",
        "",
        " Do you want to proceed?",
        " ❯ 1. Yes",
        "   2. Yes, and don't ask again for rm commands in /work",
        "   3. No, and tell Claude what to do differently (esc)",
        "",
        " Esc to cancel · Tab to amend · ctrl+e to explain",
    ]
)


@pytest.fixture
def approving(asking):
    state, sid, transcript = asking
    transcript.write_text(transcript.read_text().splitlines()[0] + "\n")
    data = read(state)
    data["screens"]["w1:p1"] = [PERMISSION, "done"]
    state.write_text(json.dumps(data))
    return state, sid


def choice_request(sid, dialog, option="1", **extra):
    return {
        "workspace": "w1",
        "pane": "w1:p1",
        "session": sid,
        "dialog": dialog,
        "option": option,
        **extra,
    }


def test_a_permission_prompt_is_answered_with_the_chosen_number(approving):
    import agent_messages

    state, sid = approving
    interaction = agent_messages.agents("w1", interactions=True)[0]["interaction"]
    assert interaction["question"] is None
    choices = interaction["choices"]
    assert (
        choices["text"]
        == "Bash command\n\n  rm -rf build\n  Remove the build directory\n\nDo you want to proceed?"
    )
    assert [(o["key"], o["label"]) for o in choices["options"]] == [
        ("1", "Yes"),
        ("2", "Yes, and don't ask again for rm commands in /work"),
        ("3", "No, and tell Claude what to do differently (esc)"),
    ]
    # Moving the cursor in the terminal leaves it the same dialog.
    moved = PERMISSION.replace(" ❯ 1. Yes", "   1. Yes").replace("   3. No", " ❯ 3. No")
    data = read(state)
    data["screens"]["w1:p1"] = [moved, "done"]
    state.write_text(json.dumps(data))
    assert agent_messages.choose(choice_request(sid, choices["id"], "3")) == {
        "chosen": True,
        "pane": "w1:p1",
    }
    assert keys_sent(state) == [["3"]]


@pytest.mark.parametrize(
    "change, request_extra, error",
    [
        ("dialog", {}, "dialog on the agent's screen changed"),
        (None, {"option": "4"}, "one of the dialog's options"),
        (None, {"session": "other"}, "no longer running"),
        (None, {"extra": "x"}, "Invalid choice"),
        ("idle", {}, "not waiting on a dialog"),
    ],
)
def test_choices_are_refused_before_any_key(approving, change, request_extra, error):
    import agent_messages

    state, sid = approving
    dialog = agent_messages.agents("w1", interactions=True)[0]["interaction"]["choices"]["id"]
    data = read(state)
    if change == "dialog":
        data["screens"]["w1:p1"] = [PERMISSION.replace("rm -rf build", "rm -rf /"), "done"]
    if change == "idle":
        data["agents"][0]["agent_status"] = "idle"
    state.write_text(json.dumps(data))
    with pytest.raises(ValueError, match=error):
        agent_messages.choose(choice_request(sid, dialog, **request_extra))
    assert keys_sent(state) == []


def test_the_dashboard_relays_choices(approving, tmp_path):
    import agent_messages

    state, sid = approving
    dialog = agent_messages.agents("w1", interactions=True)[0]["interaction"]["choices"]["id"]
    with dashboard.DashboardServer(tmp_path / "plain", 0) as httpd:
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            status, value = request(
                httpd.server_port,
                "/api/workspace-choose",
                choice_request(sid, dialog, "2"),
                action="workspace-choose",
            )
        finally:
            httpd.shutdown()
            thread.join(5)
    assert (status, value) == (200, {"chosen": True, "pane": "w1:p1"})
    assert keys_sent(state) == [["2"]]


TRUST = "\n".join(
    [
        RULE,
        " Accessing workspace:",
        "",
        " /work/checkout",
        "",
        " These will apply without asking. Only proceed if you trust this configuration.",
        "",
        " ❯ No, exit",
        "   Yes, I trust this folder",
        "",
        " Enter to confirm · Esc to cancel",
    ]
)


@pytest.mark.parametrize("lands", [True, False])
def test_an_unnumbered_dialog_moves_its_cursor_before_enter(approving, lands):
    import agent_messages

    state, sid = approving
    moved = TRUST.replace(" ❯ No, exit", "   No, exit").replace("   Yes, I", " ❯ Yes, I")
    data = read(state)
    data["screens"]["w1:p1"] = [TRUST, moved if lands else TRUST, "done"]
    state.write_text(json.dumps(data))
    choices = agent_messages.agents("w1", interactions=True)[0]["interaction"]["choices"]
    assert choices["text"].startswith("Accessing workspace:\n\n/work/checkout")
    assert [(o["key"], o["label"]) for o in choices["options"]] == [
        ("1", "No, exit"),
        ("2", "Yes, I trust this folder"),
    ]
    if lands:
        agent_messages.choose(choice_request(sid, choices["id"], "2"))
        assert keys_sent(state) == [["down"], ["enter"]]
    else:
        with pytest.raises(ValueError, match="nothing was confirmed"):
            agent_messages.choose(choice_request(sid, choices["id"], "2"))
        assert keys_sent(state) == [["down"]]


IDLE = "\n".join(["⏺ All checks passed.", "", RULE, "❯ ", RULE, "  ? for shortcuts"])


@pytest.mark.parametrize("appears", [True, False])
def test_a_misdetected_blocked_agent_gets_the_message_pasted(asking, appears):
    import agent_messages

    state, sid, _ = asking
    data = read(state)
    typed = IDLE.replace("❯ ", "❯ [Pasted text #1 +2 lines]")
    data["screens"]["w1:p1"] = [IDLE, typed if appears else IDLE, "done"]
    state.write_text(json.dumps(data))
    interaction = agent_messages.agents("w1", interactions=True)[0]["interaction"]
    assert (interaction["idle"], interaction["choices"], interaction["question"]) == (
        True,
        None,
        None,
    )
    request = {"workspace": "w1", "pane": "w1:p1", "text": "one\ntwo"}
    if appears:
        assert agent_messages.send(request) == {"sent": True, "pane": "w1:p1", "warning": None}
        assert keys_sent(state) == [["\x1b[200~one\ntwo\x1b[201~"], ["enter"]]
    else:
        with pytest.raises(ValueError, match="did not appear"):
            agent_messages.send(request)
        assert keys_sent(state) == [["\x1b[200~one\ntwo\x1b[201~"]]
    assert "prompts" not in read(state)


def test_a_dialog_that_opens_after_the_paste_gets_no_enter(asking, monkeypatch):
    import agent_messages

    state, _, _ = asking
    monkeypatch.setattr(agent_messages, "DIALOG_WAIT", 0.3)
    data = read(state)
    data["screens"]["w1:p1"] = [IDLE, PERMISSION.replace("rm -rf build", "rm one\ntwo")]
    state.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="did not appear"):
        agent_messages.send({"workspace": "w1", "pane": "w1:p1", "text": "one\ntwo"})
    assert keys_sent(state) == [["\x1b[200~one\ntwo\x1b[201~"]]


def test_a_numbered_list_in_the_question_is_not_taken_for_its_options(missing_question):
    import agent_messages

    state, sid, screen = missing_question
    listed = screen.replace("Pick a\ncolor\n", "Pick a color from\n1. the logo\n2. the site\n")
    data = read(state)
    data["screens"]["w1:p1"] = [listed, "done"]
    state.write_text(json.dumps(data))
    question = agent_messages.agents("w1", interactions=True)[0]["interaction"]["question"]
    asked = question["questions"][0]
    assert asked["question"] == "Pick a color from 1. the logo 2. the site"
    assert [o["label"] for o in asked["options"]] == ["Red", "Blue"]


def test_a_blocked_agent_with_a_draft_or_dialog_is_not_pasted_into(asking):
    import agent_messages

    state, _, _ = asking
    working = IDLE.replace("? for shortcuts", "⏵⏵ bypass permissions on · esc to interrupt")
    for screen in (IDLE.replace("❯ ", "❯ half-typed draft"), PERMISSION, working):
        data = read(state)
        data["screens"]["w1:p1"] = [screen]
        state.write_text(json.dumps(data))
        with pytest.raises(ValueError, match="waiting on a question"):
            agent_messages.send({"workspace": "w1", "pane": "w1:p1", "text": "hi"})
    assert keys_sent(state) == []


def test_quote_bars_are_not_part_of_a_live_question(missing_question):
    import agent_messages

    state, sid, screen = missing_question
    quoted = screen.replace("Pick a\ncolor\n", "│ Pick a\n│ color\n")
    data = read(state)
    data["screens"]["w1:p1"] = [quoted, "done"]
    state.write_text(json.dumps(data))
    question = agent_messages.agents("w1", interactions=True)[0]["interaction"]["question"]
    assert question["questions"][0]["question"] == "Pick a color"
    agent_messages.answer(answer_request(sid, [{"options": [1]}], tool=question["id"]))
    assert keys_sent(state) == [["2"]]


def test_a_waiting_claude_dialog_is_answered_one_question_at_a_time(asking):
    import agent_messages

    state, sid, _ = asking
    assert agent_messages.agents("w1")[0]["session"] == sid
    value = agent_messages.answer(answer_request(sid, [{"options": [1]}, {"options": [2, 0]}]))
    assert value == {"answered": True, "pane": "w1:p1"}
    # Blue picks and moves on; toppings toggle, Right moves on; 1 submits the review.
    assert keys_sent(state) == [["2"], ["1"], ["3"], ["right"], ["1"]]


@pytest.mark.parametrize("prefix", ["", "-- "])
def test_a_typed_answer_goes_through_the_type_something_option(asking, prefix):
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
    text = prefix + "Teal,\n\x1b[201~ please"
    agent_messages.answer(answer_request(sid, [{"text": text}]))
    # One line, with no control characters; a single question needs no review step.
    expected = prefix + "Teal, [201~ please"
    assert read(state)["typed_text"] == [["w1:p1", expected]]
    assert keys_sent(state) == [["3"], [expected], ["enter"]]


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


def test_a_message_with_an_attached_file_gives_the_agent_its_path(site, tmp_path):
    state = site[4]
    with dashboard.DashboardServer(tmp_path / "plain", 0) as httpd:
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            port = httpd.server_port
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            connection.request(
                "POST",
                "/api/attachment-upload",
                b"log line",
                {
                    "Host": f"127.0.0.1:{port}",
                    "Content-Type": "application/octet-stream",
                    "X-Babysit-Action": "attachment-upload",
                    "X-Filename": "ci.log",
                },
            )
            uploaded = json.loads(connection.getresponse().read())
            connection.close()
            body = {"workspace": "w1", "text": "See the log", "attachments": [uploaded["id"]]}
            status, value = request(
                port, "/api/workspace-message", body, action="workspace-message"
            )
            assert status == 200 and value["sent"]
            path = tmp_path / "plain" / "attachments" / uploaded["id"]
            assert read(state)["prompts"] == [
                ["w1:p1", f"See the log\n\nAttached files (read them from these paths):\n- {path}"]
            ]
            status, value = request(
                port,
                "/api/workspace-message",
                {**body, "attachments": ["2026-01-01-00000000-gone.txt"]},
                action="workspace-message",
            )
            assert status == 400 and "missing" in value["error"]
        finally:
            httpd.shutdown()
            thread.join(5)


def test_docker_action_uses_dashboard_post_protections(server, monkeypatch):
    import agent_messages

    port, _ = server
    calls = []

    def change(body, home):
        calls.append(body)
        return {"pane": body["pane"], "docker": body["enabled"], "warning": None}

    monkeypatch.setattr(agent_messages, "set_docker", change)
    body = {"workspace": "w1", "pane": "w1:p1", "session": "s1", "enabled": False}
    status, _ = request(port, "/api/workspace-docker", body, action="")
    assert status == 403 and not calls
    status, value = request(port, "/api/workspace-docker", body, action="workspace-docker")
    assert status == 200 and value["docker"] is False
    assert calls == [body]


# Layout and digit/notes behavior from openai/codex rust-v0.160.0,
# tui/src/bottom_pane/request_user_input/{mod.rs,snapshots/}.
def codex_screen(question, index=0, total=1, selected=1, notes=None):
    options = question.get("options", [])
    labels = [o if isinstance(o, str) else o["label"] for o in options]
    if labels:
        labels.append("None of the above")
    lines = [f"Question {index + 1}/{total} ({total - index} unanswered)", question["title"], ""]
    lines += [f"{'›' if i == selected else ' '} {i}. {label}" for i, label in enumerate(labels, 1)]
    if not labels or notes is not None:
        lines += [
            "",
            "› " + (notes or ("Add notes" if labels else "Type your answer (optional)")),
            "",
        ]
    footer = (
        "tab to clear notes | " if notes is not None else "tab to add notes | " if labels else ""
    )
    return "\n".join([*lines, footer + "enter to submit answer | esc to interrupt"])


@pytest.fixture
def codex_asking(exited, monkeypatch):
    import agent_messages

    state, sid, checkout, _ = exited
    sid = "c0ffee00-0000-4000-8000-000000000003"
    folder = Path(os.environ["CODEX_HOME"]) / "sessions" / "2026" / "10" / "06"
    folder.mkdir(parents=True)
    transcript = folder / f"rollout-{sid}.jsonl"
    question = {
        "title": "Should Docker access be automatic?",
        "options": ["Automatic", "Optional checkbox"],
    }
    records = [
        {"type": "session_meta", "payload": {"id": sid, "cwd": str(checkout)}},
        {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "request_user_input_async",
                "call_id": "toolu_q1",
                "arguments": json.dumps({"questions": [question]}),
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "toolu_q1",
                "output": '{"accepted":true}',
            },
        },
    ]
    transcript.write_text("".join(json.dumps(r) + "\n" for r in records))
    data = read(state)
    data["agents"] = [
        {
            "workspace_id": "w1",
            "pane_id": "w1:p1",
            "agent": "codex",
            "agent_status": "blocked",
            "agent_session": {"value": sid},
        }
    ]
    data["screens"] = {"w1:p1": [codex_screen(question), "done"]}
    state.write_text(json.dumps(data))
    monkeypatch.setenv("FAKE_HERDR_KEEP_AGENT", "1")
    monkeypatch.setattr(agent_messages, "DIALOG_WAIT", 0.01)
    monkeypatch.setattr(agent_messages, "_codex_answered", set())
    return state, sid, transcript, question


def test_codex_async_question_is_answered_via_the_dashboard(codex_asking, tmp_path):
    state, sid, _, _ = codex_asking
    with dashboard.DashboardServer(tmp_path / "plain", 0) as httpd:
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            body = answer_request(sid, [{"options": [1]}])
            status, value = request(
                httpd.server_port, "/api/workspace-answer", body, action="workspace-answer"
            )
            assert status == 200 and value["answered"], value
            assert keys_sent(state) == [["2"]]
            # A stale async acknowledgment must never replay a completed answer.
            status, value = request(
                httpd.server_port, "/api/workspace-answer", body, action="workspace-answer"
            )
            assert status == 400 and "already attempted" in value["error"]
            assert keys_sent(state) == [["2"]]
        finally:
            httpd.shutdown()
            thread.join(5)


@pytest.mark.parametrize("freeform", [False, True])
@pytest.mark.parametrize("prefix", ["", "-- "])
def test_codex_typed_answer_uses_other_or_a_freeform_composer(codex_asking, freeform, prefix):
    import agent_messages

    state, sid, transcript, question = codex_asking
    text = prefix + "Use a checkbox"
    if freeform:
        question = {"title": "Share details."}
        records = [json.loads(line) for line in transcript.read_text().splitlines()]
        records[1]["payload"]["arguments"] = json.dumps({"questions": [question]})
        transcript.write_text("".join(json.dumps(r) + "\n" for r in records))
        screens = [codex_screen(question), codex_screen(question, notes=text), "done"]
        expected = [[text], ["enter"]]
    else:
        screens = [codex_screen(question, selected=i) for i in (1, 2, 3)] + [
            codex_screen(question, selected=3, notes=""),
            codex_screen(question, selected=3, notes=text),
            "done",
        ]
        expected = [["down"], ["down"], ["tab"], [text], ["enter"]]
    data = read(state)
    data["screens"]["w1:p1"] = screens
    state.write_text(json.dumps(data))
    agent_messages.answer(answer_request(sid, [{"text": prefix + "Use a\ncheckbox"}]))
    assert keys_sent(state) == expected
    assert read(state)["typed_text"] == [["w1:p1", text]]


def test_codex_synchronous_questions_advance_without_a_claude_review(codex_asking):
    import agent_messages

    state, sid, transcript, question = codex_asking
    second = {"title": "Which environment?", "options": ["Test", "Production"]}
    records = [json.loads(line) for line in transcript.read_text().splitlines()][:2]
    records[1]["payload"].update(
        name="request_user_input", arguments=json.dumps({"questions": [question, second]})
    )
    transcript.write_text("".join(json.dumps(r) + "\n" for r in records))
    data = read(state)
    data["screens"]["w1:p1"] = [
        codex_screen(question, total=2),
        codex_screen(second, index=1, total=2),
        "done",
    ]
    state.write_text(json.dumps(data))
    agent_messages.answer(answer_request(sid, [{"options": [1]}, {"options": [0]}]))
    assert keys_sent(state) == [["2"], ["1"]]


@pytest.mark.parametrize(
    "mode",
    [
        "approval",
        "similar",
        "missing_header",
        "notes",
        "other_session",
        "working",
        "answered",
        "remapped_submit",
    ],
)
def test_codex_answers_require_the_matching_live_question(codex_asking, mode):
    import agent_messages

    state, sid, transcript, question = codex_asking
    data = read(state)
    screen = codex_screen(question)
    if mode == "approval":
        screen = (
            "Would you like to run the following command?\n1. Yes\n2. No\nPress enter to confirm"
        )
    elif mode == "similar":
        screen = codex_screen({**question, "title": question["title"] + " For all agents?"})
    elif mode == "missing_header":
        screen = screen.split("\n", 1)[1]
    elif mode == "notes":
        screen = codex_screen(question, notes="An unfinished answer")
    elif mode == "other_session":
        data["agents"][0]["agent_session"]["value"] = "different"
    elif mode == "working":
        data["agents"][0]["agent_status"] = "working"
    elif mode == "answered":
        transcript.write_text(
            transcript.read_text().replace('{\\"accepted\\":true}', '{\\"answers\\":{}}')
        )
    elif mode == "remapped_submit":
        screen = screen.replace("enter to", "ctrl+enter to")
    data["screens"]["w1:p1"] = [screen]
    state.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        agent_messages.answer(answer_request(sid, [{"options": [1]}]))
    assert keys_sent(state) == []


def test_codex_partial_answer_stops_and_cannot_be_retried(codex_asking):
    import agent_messages

    state, sid, _, question = codex_asking
    data = read(state)
    data["screens"]["w1:p1"] = [codex_screen(question), "approval or exited shell"]
    state.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="Answer attempted"):
        agent_messages.answer(answer_request(sid, [{"text": "Something else"}]))
    assert keys_sent(state) == [["down"]]
    with pytest.raises(ValueError, match="already attempted"):
        agent_messages.answer(answer_request(sid, [{"text": "Something else"}]))
    assert keys_sent(state) == [["down"]]


@pytest.mark.parametrize("failure", ["exit", "timeout"])
def test_codex_delivery_stops_when_the_agent_exits_or_io_is_uncertain(
    codex_asking, monkeypatch, failure
):
    import agent_messages

    state, sid, _, _ = codex_asking
    if failure == "exit":
        real = agent_messages.agents
        count = 0

        def agents(workspace):
            nonlocal count
            count += 1
            return real(workspace) if count == 1 else []

        monkeypatch.setattr(agent_messages, "agents", agents)
        error = "agent stopped waiting"
    else:
        real_herdr = agent_messages.herdr

        def herdr(*args):
            if args[:2] == ("agent", "send-keys"):
                raise subprocess.TimeoutExpired("herdr", 1)
            return real_herdr(*args)

        monkeypatch.setattr(agent_messages, "herdr", herdr)
        error = "Answer attempted"
    with pytest.raises(ValueError, match=error):
        agent_messages.answer(answer_request(sid, [{"options": [1]}]))
    assert keys_sent(state) == []
    if failure == "timeout":
        with pytest.raises(ValueError, match="already attempted"):
            agent_messages.answer(answer_request(sid, [{"options": [1]}]))


def test_codex_answer_is_not_replayed_after_its_session_moves_panes(codex_asking):
    import agent_messages

    state, sid, _, question = codex_asking
    body = answer_request(sid, [{"options": [1]}])
    agent_messages.answer(body)
    data = read(state)
    data["agents"][0]["pane_id"] = "w1:p2"
    data["screens"]["w1:p2"] = [codex_screen(question)]
    state.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="already attempted"):
        agent_messages.answer({**body, "pane": "w1:p2"})
    assert keys_sent(state) == [["2"]]


@pytest.fixture
def rewritten(tmp_path):
    """A clone whose branch was rebased upstream: same changes under different names."""
    clone = tmp_path / "repo"
    clone.mkdir()

    def git(*args, cwd=clone):
        return subprocess.check_output(["git", "-C", str(cwd), *args], text=True).strip()

    git("init", "-b", "main")
    git("config", "user.name", "Fixture")
    git("config", "user.email", "fixture@example.test")
    git("config", "commit.gpgsign", "false")
    (clone / "base.txt").write_text("base\n")
    git("add", ".")
    git("commit", "-m", "base")
    git("branch", "work")
    for name in ("one", "two"):
        (clone / f"{name}.txt").write_text(f"{name}\n")
        git("add", ".")
        git("commit", "-m", f"add {name}")
    # What the maintainer merged: the same two patches, replayed under new names.
    git("update-ref", "refs/remotes/origin/main", "HEAD")
    git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    git("reset", "--hard", "work")
    return clone, git


def test_a_rebased_branch_keeps_no_commits_of_its_own(rewritten, tmp_path):
    clone, git = rewritten
    work = tmp_path / "work"
    git("worktree", "add", str(work), "work")
    for name in ("one", "two"):
        (work / f"{name}.txt").write_text(f"{name}\n")
        git("add", ".", cwd=work)
        git("commit", "-m", f"add {name} (local)", cwd=work)
    # The same two changes under local names: no remote carries the SHAs, all the work.
    assert (
        git(
            "rev-list",
            "--count",
            "HEAD",
            "--not",
            "--remotes",
            "--exclude=work",
            "--branches",
            cwd=work,
        )
        == "2"
    )
    assert wso.local_state(str(work), "work")["unpushed"] == 0


def test_an_unlanded_commit_still_counts_on_a_rebased_branch(rewritten, tmp_path):
    clone, git = rewritten
    work = tmp_path / "work"
    git("worktree", "add", str(work), "work")
    for name in ("one", "two"):
        (work / f"{name}.txt").write_text(f"{name}\n")
        git("add", ".", cwd=work)
        git("commit", "-m", f"add {name} (local)", cwd=work)
    (work / "three.txt").write_text("three\n")
    git("add", ".", cwd=work)
    git("commit", "-m", "add three", cwd=work)
    assert wso.local_state(str(work), "work")["unpushed"] == 1


def test_without_a_base_branch_every_commit_is_kept(rewritten, tmp_path):
    clone, git = rewritten
    git("symbolic-ref", "--delete", "refs/remotes/origin/HEAD")
    work = tmp_path / "work"
    git("worktree", "add", str(work), "work")
    (work / "one.txt").write_text("one\n")
    git("add", ".", cwd=work)
    git("commit", "-m", "add one (local)", cwd=work)
    assert wso.local_state(str(work), "work")["unpushed"] == 1


def test_a_merge_counts_only_when_it_resolved_something_itself(rewritten, tmp_path):
    clone, git = rewritten
    work = tmp_path / "work"
    git("worktree", "add", str(work), "work")
    git("merge", "--no-ff", "-m", "merge main", "main", cwd=work)
    # Nothing but the join of two upstream histories: the merge carries no work.
    assert wso.local_state(str(work), "work")["unpushed"] == 0
    (work / "one.txt").write_text("resolved differently\n")
    git("add", ".", cwd=work)
    git("commit", "--amend", "--no-edit", cwd=work)
    assert wso.local_state(str(work), "work")["unpushed"] == 1


def exit_watches(home):
    import exit_watch

    with exit_watch.connect(home) as db:
        return [json.loads(data) for (data,) in db.execute("SELECT data FROM exit_watches")]


def test_a_message_can_ask_its_agent_to_quit_when_done(site):
    import agent_messages
    import exit_watch

    plugin, _, _, worktrees, state, _ = site
    body = {"workspace": "w1", "text": "Approve and merge", "exit_when_done": True}
    value = agent_messages.send(body, plugin.home)
    assert value["sent"] and value["warning"] is None
    ((pane, typed),) = read(state)["prompts"]
    marker = re.search(r"\[babysit-done:[0-9a-f]{16}\]", typed)[0]
    assert typed == "Approve and merge" + exit_watch.follow_up(marker)
    (watch,) = exit_watches(plugin.home)
    assert watch["id"] == value["exit_watch"]
    assert (watch["pane_id"], watch["workspace_id"], watch["agent"]) == (pane, "w1", "codex")
    assert watch["follow_up"] and watch["marker"] == marker
    assert watch["path"] == str((worktrees / "merged-work").resolve())
    assert watch["label"] == "Message: Approve and merge"
    assert watch["exit"]["state"] == "watching"
    # Without the flag a message is typed as written and nothing is watched.
    agent_messages.send({"workspace": "w1", "text": "Thanks"}, plugin.home)
    assert read(state)["prompts"][-1] == [pane, "Thanks"]
    assert len(exit_watches(plugin.home)) == 1


def test_a_resumed_session_can_quit_when_done(exited):
    import agent_messages

    state, sid, checkout, home = exited
    body = {"workspace": "w1", "resume": sid, "text": "Approve and merge", "exit_when_done": True}
    value = agent_messages.send(body, home)
    (watch,) = exit_watches(home)
    assert value["exit_watch"] == watch["id"] and watch["pane_id"] == value["pane"]
    assert watch["session_id"] == sid and watch["agent"] == "claude"
    assert watch["marker"] in read(state)["agents"][0]["argv"][-1]


def test_a_message_whose_agent_cannot_be_found_says_it_will_not_quit(site, monkeypatch):
    import agent_messages

    plugin, *_ = site
    # Typed, but herdr no longer lists the agent in that pane.
    monkeypatch.setattr(
        agent_messages,
        "prompt",
        lambda target, text: {"sent": True, "pane": "w1:p9", "warning": None},
    )
    body = {"workspace": "w1", "text": "Approve and merge", "exit_when_done": True}
    value = agent_messages.send(body, plugin.home)
    assert value["sent"] and value["warning"] == agent_messages.QUIT_UNWATCHED
    assert "exit_watch" not in value and exit_watches(plugin.home) == []


@pytest.mark.parametrize(
    "body, error",
    [
        ({"workspace": "w1", "text": "x", "exit_when_done": "yes"}, "exit_when_done flag"),
        ({"workspace": "w1", "resume": "s", "text": "", "exit_when_done": True}, "Write a message"),
    ],
)
def test_quitting_after_a_message_needs_a_flag_and_a_message(site, body, error):
    import agent_messages

    plugin, _, _, _, state, _ = site
    with pytest.raises(ValueError, match=error):
        agent_messages.send(body, plugin.home)
    assert not read(state).get("prompts")


def test_a_typed_message_is_never_reported_unsent_when_its_watch_fails(site, monkeypatch):
    import agent_messages

    plugin, *_ = site
    real_herdr, real_prompt, typed = agent_messages.herdr, agent_messages.prompt, []

    def prompt(target, text):
        typed.append(text)
        return real_prompt(target, text)

    def herdr(*args):
        value = real_herdr(*args)
        if typed and args[:2] == ("agent", "list"):
            # After delivery herdr lists the agent without its workspace.
            value["agents"] = [
                {k: v for k, v in a.items() if k != "workspace_id"} for a in value["agents"]
            ]
        return value

    monkeypatch.setattr(agent_messages, "prompt", prompt)
    monkeypatch.setattr(agent_messages, "herdr", herdr)
    body = {"workspace": "w1", "text": "Approve and merge", "exit_when_done": True}
    value = agent_messages.send(body, plugin.home)
    assert value["sent"] and value["warning"] == agent_messages.QUIT_UNWATCHED
    assert len(typed) == 1 and exit_watches(plugin.home) == []
