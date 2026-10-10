"""PR discovery and caching use fake GitHub responses, never the live account."""

import json
import subprocess
import threading
from unittest.mock import Mock

import owned_process
import pr_overview as overview
import pytest


def pr(key="one", updated="2026-09-11T12:00:00Z", ci="SUCCESS", **extra):
    return {
        "id": key,
        "number": 1,
        "title": "A PR",
        "url": f"https://github.com/test/repo/pull/{key}",
        "repository": {"nameWithOwner": "test/repo"},
        "headRepository": {"nameWithOwner": "fork/repo"},
        "headRefName": "feature",
        "headRefOid": "a" * 40,
        "author": {"login": "alice"},
        "updatedAt": updated,
        "createdAt": "2025-01-02T09:00:00Z",
        "state": "OPEN",
        "isDraft": False,
        "reviewDecision": None,
        "statusCheckRollup": {"state": ci} if ci else None,
        **extra,
    }


def page(nodes=(), next_cursor=None, count=1, login="alice"):
    return {
        "viewer": {"login": login},
        "search": {
            "nodes": list(nodes),
            "issueCount": count,
            "pageInfo": {"hasNextPage": bool(next_cursor), "endCursor": next_cursor},
        },
    }


def test_discovers_all_roles_paginates_deduplicates_and_sorts(monkeypatch):
    fetch = Mock(
        side_effect=[
            page([pr(), None, pr("closed", state="CLOSED")], "next"),
            page([pr("two", ci=None, author=None)]),
            page([pr(updated="2026-09-11T13:00:00Z", ci="FAILURE")]),
            page([pr(updated="2026-09-11T11:00:00Z"), pr("three", ci="PENDING")]),
            page([pr(updated="2026-09-11T10:00:00Z")]),
        ]
    )
    monkeypatch.setattr(overview, "github_page", fetch)
    result = overview.collect()
    assert len(result["prs"]) == 3
    first = result["prs"][0]
    assert first["roles"] == ["author", "assignee", "reviewer", "mentioned"]
    assert result["roles"] == ["author", "assignee", "reviewer", "mentioned"]
    assert first["ci"] == "FAILURE"
    assert (first["head_repo"], first["head_branch"], first["head_sha"]) == (
        "fork/repo",
        "feature",
        "a" * 40,
    )
    assert first["opened_at"] == "2025-01-02T09:00:00Z"
    assert first["updated_at"] == "2026-09-11T13:00:00Z"
    assert result["prs"][1]["ci"] == "NONE"
    assert result["prs"][1]["author"] is None
    assert fetch.call_args_list[1].args[1] == "next"
    assert "review-involves:@me" in fetch.call_args_list[-2].args[0]
    assert fetch.call_args_list[-1].args[0] == "is:pr is:open mentions:@me sort:updated-desc"
    assert result["login"] == "alice" and not result["warnings"]


def test_mentions_can_be_left_out(monkeypatch):
    fetch = Mock(return_value=page([pr()]))
    monkeypatch.setattr(overview, "github_page", fetch)
    result = overview.collect(mentions=False)
    assert result["roles"] == result["prs"][0]["roles"] == ["author", "assignee", "reviewer"]
    assert not any("mentions:" in call.args[0] for call in fetch.call_args_list)


@pytest.mark.parametrize(
    "content, expected",
    [(None, True), ("{}", True), ('{"mentions": true}', True), ('{"mentions": false}', False)],
)
def test_mentions_setting_defaults_on(tmp_path, content, expected):
    if content is not None:
        (tmp_path / "overview-config.json").write_text(content)
    assert overview.include_mentions(tmp_path) is expected


@pytest.mark.parametrize("content", ["not json", "[]", '{"mentions": "no"}'])
def test_invalid_mentions_setting_is_a_visible_sync_error(tmp_path, monkeypatch, content):
    (tmp_path / "overview-config.json").write_text(content)
    fetch = Mock()
    monkeypatch.setattr(overview, "collect", fetch)
    cache = overview.Overview(tmp_path)
    cache.refresh()
    assert "overview-config.json" in cache.value["error"]
    assert not fetch.called


def test_overview_passes_the_mentions_setting(tmp_path, monkeypatch):
    (tmp_path / "overview-config.json").write_text('{"mentions": false}')
    fetch = Mock(return_value={"prs": [], "login": "alice", "warnings": []})
    monkeypatch.setattr(overview, "collect", fetch)
    overview.Overview(tmp_path).refresh()
    fetch.assert_called_once_with(mentions=False)


def test_counts_unresolved_review_threads_including_outdated(monkeypatch):
    threads = {"nodes": [{"isResolved": False}, {"isResolved": True}, {"isResolved": False}]}
    nodes = [pr(reviewThreads=threads, reviewDecision="CHANGES_REQUESTED"), pr("bare")]
    monkeypatch.setattr(overview, "github_page", lambda *args: page(nodes))
    found = {item["id"]: item for item in overview.collect()["prs"]}
    assert found["one"]["unresolved_threads"] == 2
    assert found["one"]["review_decision"] == "CHANGES_REQUESTED"
    assert found["bare"]["unresolved_threads"] == 0
    assert "reviewThreads(last: 100) { nodes { isResolved } }" in overview.PR_FRAGMENT


def test_empty_search_still_lists_the_searched_roles(monkeypatch):
    monkeypatch.setattr(overview, "github_page", lambda *args: page([]))
    result = overview.collect()
    assert result["prs"] == []
    assert result["roles"] == ["author", "assignee", "reviewer", "mentioned"]


def test_search_limit_is_visible(monkeypatch):
    fetch = Mock(side_effect=[page([], str(i), 1001) for i in range(20)] + [page()] * 3)
    monkeypatch.setattr(overview, "github_page", fetch)
    result = overview.collect()
    assert result["warnings"] == ["Only the most recently updated 1,000 author PRs are shown."]
    assert fetch.call_count == 23


@pytest.mark.parametrize(
    "responses, message",
    [
        ([page([], "same"), page([], "same")], "pagination"),
        ([page(), page(login="bob")], "account changed"),
    ],
)
def test_invalid_refresh_is_rejected(monkeypatch, responses, message):
    monkeypatch.setattr(overview, "github_page", Mock(side_effect=responses))
    with pytest.raises(ValueError, match=message):
        overview.collect()


def test_github_command_uses_json_stdin_and_reports_api_failures(monkeypatch):
    run = Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps({"data": page()}), ""))
    monkeypatch.setattr(owned_process, "run", run)
    assert overview.github_page("is:open", "cursor") == page()
    args, kwargs = run.call_args
    assert args[0] == ["gh", "api", "--hostname", "github.com", "graphql", "--input", "-"]
    assert json.loads(kwargs["input"])["variables"] == {"query": "is:open", "cursor": "cursor"}
    assert kwargs["timeout"] == 45
    run.return_value = subprocess.CompletedProcess([], 1, "", "rate limit exceeded")
    with pytest.raises(ValueError, match="rate limit"):
        overview.github_page("query")
    run.return_value = subprocess.CompletedProcess([], 0, '{"errors": [{"message": "denied"}]}', "")
    with pytest.raises(ValueError, match="incomplete"):
        overview.github_page("query")


def test_cache_serves_stale_data_without_duplicate_workers_and_persists(tmp_path, monkeypatch):
    cache = overview.Overview(tmp_path)
    entered, finish = threading.Event(), threading.Event()
    result = {"prs": [{"id": "one"}], "login": "alice", "warnings": []}

    def collect(**_):
        entered.set()
        assert finish.wait(5)
        return result

    fetch = Mock(side_effect=collect)
    monkeypatch.setattr(overview, "collect", fetch)
    assert cache.snapshot()["refreshing"]
    assert entered.wait(5)
    try:
        for _ in range(10):
            assert cache.snapshot()["prs"] == []
        assert fetch.call_count == 1
    finally:
        finish.set()
        cache.worker.join(5)
    snapshot = cache.snapshot()
    assert snapshot["prs"] == result["prs"]
    assert snapshot["synced_at"] and not snapshot["refreshing"]
    # A new dict per call; the items it shares with the cache are read-only.
    snapshot["prs"] = []
    assert cache.snapshot()["prs"] == result["prs"] == [{"id": "one"}]
    assert fetch.call_count == 1
    assert overview.Overview(tmp_path).value["prs"] == result["prs"]
    assert (tmp_path / "pr-overview.json").stat().st_mode & 0o777 == 0o600
    assert not (tmp_path / "queue.sqlite").exists()


@pytest.mark.parametrize(
    "error", [ValueError("rate limit"), subprocess.TimeoutExpired("gh", 45), OSError("missing gh")]
)
def test_failure_keeps_last_complete_snapshot_and_recovers(tmp_path, monkeypatch, error):
    cache = overview.Overview(tmp_path)
    good = {"prs": [{"id": "one"}], "login": "alice", "warnings": []}
    monkeypatch.setattr(overview, "collect", Mock(side_effect=[good, error, {**good, "prs": []}]))
    cache.refresh()
    saved = cache.path.read_bytes()
    timestamp = cache.value["synced_at"]
    cache.refresh()
    assert cache.value["error"]
    assert cache.value["prs"] == good["prs"]
    assert cache.value["synced_at"] == timestamp
    assert cache.path.read_bytes() == saved
    cache.refresh()
    assert cache.value["error"] is None and cache.value["prs"] == []


def test_corrupt_cache_and_write_failure_are_recoverable(tmp_path, monkeypatch):
    (tmp_path / "pr-overview.json").write_text("not json")
    cache = overview.Overview(tmp_path)
    assert cache.value["prs"] == []
    monkeypatch.setattr(
        overview, "collect", lambda **_: {"prs": [], "login": "alice", "warnings": []}
    )
    monkeypatch.setattr(overview.os, "open", Mock(side_effect=OSError("read only")))
    cache.refresh()
    assert "read only" in cache.value["error"]
