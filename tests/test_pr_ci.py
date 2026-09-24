"""CI detail reads use fake GitHub data and private temporary state."""

import http.client
import json
import subprocess
import threading
from unittest.mock import Mock

import dashboard
import owned_process
import pr_ci as ci
import pytest
from pr_overview import Overview

SHA = "a" * 40


def run(key="run1", state="FAILURE", **extra):
    return {
        "id": key,
        "__typename": "CheckRun",
        "name": f"pytest ({key})",
        "status": "COMPLETED",
        "conclusion": state,
        "detailsUrl": f"https://github.com/test/repo/runs/{key}",
        "checkSuite": {"workflowRun": {"workflow": {"name": "Tests"}}},
        **extra,
    }


def response(checks=(), cursor=None, sha=SHA):
    return {
        "headRefOid": sha,
        "statusCheckRollup": {
            "state": "FAILURE",
            "commit": {"oid": sha},
            "contexts": {
                "nodes": list(checks),
                "pageInfo": {"hasNextPage": bool(cursor), "endCursor": cursor},
            },
        },
    }


@pytest.fixture
def cache(tmp_path):
    overview = Overview(tmp_path)
    overview.next_poll = float("inf")
    overview.value.update(login="test", prs=[{"id": "pr1", "head_sha": SHA}])
    return ci.CiDetails(tmp_path, overview)


def wait(cache):
    with cache.lock:
        workers = list(cache.workers.values())
    for worker in workers:
        worker.join(5)
        assert not worker.is_alive()


def test_collect_checks_fork_head_pagination_states_and_failure_order(monkeypatch):
    fetch = Mock(
        side_effect=[
            response([run("green", "SUCCESS"), run("pending", None, status="IN_PROGRESS")], "next"),
            response(
                [
                    run("failed"),
                    run("timeout", "TIMED_OUT"),
                    run("cancel", "CANCELLED"),
                    run("skip", "NEUTRAL"),
                    {
                        "id": "external",
                        "__typename": "StatusContext",
                        "context": "External tests",
                        "state": "ERROR",
                        "description": "2 tests failed",
                        "targetUrl": "https://ci.example/test",
                    },
                ]
            ),
        ]
    )
    monkeypatch.setattr(ci, "graphql", fetch)
    result = ci.collect_checks({"id": "fork-pr", "head_sha": SHA})
    assert result["checks"][0]["name"] == "External tests"
    assert [c["bucket"] for c in result["checks"]] == [
        "fail",
        "fail",
        "fail",
        "pending",
        "cancel",
        "pass",
        "skipping",
    ]
    assert result["checks"][1]["workflow"] == "Tests"
    assert result["checks"][1]["has_details"]
    assert not result["checks"][0]["has_details"]
    assert fetch.call_args_list[1].args[1] == {"id": "fork-pr", "cursor": "next"}
    assert not result["truncated"]


def test_pagination_is_bounded_and_partial_is_explicit(monkeypatch):
    fetch = Mock(side_effect=[response([run(str(i))], str(i)) for i in range(3)])
    monkeypatch.setattr(ci, "graphql", fetch)
    result = ci.collect_checks({"id": "pr1", "head_sha": SHA})
    assert fetch.call_count == 3 and result["truncated"]
    fetch.side_effect = [response([], "same"), response([], "same")]
    with pytest.raises(ValueError, match="pagination"):
        ci.collect_checks({"id": "pr1", "head_sha": SHA})


def test_actions_job_provenance_keeps_large_database_ids(monkeypatch):
    check = run(
        databaseId=102894622268,
        repository={"nameWithOwner": "fork/repo"},
        checkSuite={"workflowRun": {"databaseId": 34437189920, "workflow": {"name": "Tests"}}},
    )
    monkeypatch.setattr(ci, "graphql", lambda *args: response([check]))
    value = ci.collect_checks({"id": "pr1", "head_sha": SHA})["checks"][0]
    assert value["database_id"] == 102894622268
    assert value["run_id"] == 34437189920 and value["repository"] == "fork/repo"


def test_head_changes_and_empty_ci(monkeypatch):
    monkeypatch.setattr(ci, "graphql", lambda *args: response(sha="b" * 40))
    with pytest.raises(ValueError, match="head changed"):
        ci.collect_checks({"id": "pr1", "head_sha": SHA})
    monkeypatch.setattr(ci, "graphql", lambda *args: {"headRefOid": SHA, "statusCheckRollup": None})
    result = ci.collect_checks({"id": "pr1", "head_sha": SHA})
    assert result["checks"] == [] and result["state"] == "NONE"


def test_failure_details_are_bounded_and_bound_to_the_commit(monkeypatch):
    value = {
        "id": "run1",
        "checkSuite": {"commit": {"oid": SHA}},
        "title": "Failing tests",
        "summary": "test_parser failed",
        "text": "x" * 15000,
        "annotations": {
            "totalCount": 40,
            "nodes": [
                {
                    "annotationLevel": "FAILURE",
                    "title": "test_foo",
                    "message": "assert 1 == 2",
                    "path": "tests/test_foo.py",
                    "location": {"start": {"line": 10}},
                }
            ],
        },
    }
    monkeypatch.setattr(ci, "graphql", lambda *args: value)
    detail = ci.collect_failure("run1", SHA)
    assert detail["summary"] == "test_parser failed"
    assert detail["annotations"][0]["line"] == 10
    assert len(detail["text"]) == 12000 and detail["truncated"]
    with pytest.raises(ValueError, match="selected commit"):
        ci.collect_failure("run1", "b" * 40)


def test_no_prefetch_single_flight_cache_restart_expiry_and_new_head(cache, monkeypatch):
    started, release = threading.Event(), threading.Event()
    calls = []

    def load(pr):
        calls.append(pr["head_sha"])
        started.set()
        assert release.wait(5)
        return {"checks": [], "sha": pr["head_sha"]}

    monkeypatch.setattr(ci, "collect_checks", load)
    assert not calls and not cache.path.exists()
    assert cache.snapshot("pr1")["refreshing"]
    assert started.wait(5)
    assert cache.snapshot("pr1")["refreshing"] and len(calls) == 1
    release.set()
    wait(cache)
    assert not cache.snapshot("pr1")["refreshing"] and len(calls) == 1
    assert cache.path.stat().st_mode & 0o777 == 0o600
    restarted = ci.CiDetails(cache.path.parent, cache.overview)
    assert restarted.snapshot("pr1")["value"]["sha"] == SHA and len(calls) == 1
    with restarted.lock:
        next(iter(restarted.entries.values()))["expires_at"] = 0
    restarted.snapshot("pr1")
    wait(restarted)
    assert len(calls) == 2
    cache.overview.value["prs"][0]["head_sha"] = "b" * 40
    assert restarted.snapshot("pr1")["value"] is None
    wait(restarted)
    assert calls[-1] == "b" * 40
    cache.overview.value["login"] = "different-account"
    assert restarted.snapshot("pr1")["value"] is None
    wait(restarted)
    assert len(calls) == 4


def test_error_backoff_keeps_stale_data(cache, monkeypatch):
    fetch = Mock(return_value={"checks": [], "sha": SHA})
    monkeypatch.setattr(ci, "collect_checks", fetch)
    cache.snapshot("pr1")
    wait(cache)
    with cache.lock:
        next(iter(cache.entries.values()))["expires_at"] = 0
    fetch.side_effect = ValueError("rate limited")
    cache.snapshot("pr1")
    wait(cache)
    result = cache.snapshot("pr1")
    assert result["error"] == "rate limited" and result["value"]["sha"] == SHA
    assert not result["refreshing"] and fetch.call_count == 2


def test_global_concurrency_cap_and_lru(cache, monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(ci, "MAX_ENTRIES", 2)
    cache.overview.value["prs"] = [{"id": f"pr{i}", "head_sha": SHA} for i in range(3)]

    def load(pr):
        assert release.wait(5)
        return {"checks": [], "sha": SHA}

    monkeypatch.setattr(ci, "collect_checks", load)
    cache.snapshot("pr0")
    cache.snapshot("pr1")
    assert cache.snapshot("pr2")["busy"]
    assert len(cache.workers) == 2
    release.set()
    wait(cache)
    assert not cache.snapshot("pr2")["busy"]
    wait(cache)
    assert len(cache.entries) == 2


def test_arbitrary_check_ids_rejected_and_failure_loading_is_lazy(cache, monkeypatch):
    checks = {
        "sha": SHA,
        "checks": [{"id": "run1", "has_details": True}, {"id": "green", "has_details": False}],
    }
    fetch = Mock(return_value=checks)
    failure = Mock(return_value={"title": "test_foo failed"})
    monkeypatch.setattr(ci, "collect_checks", fetch)
    monkeypatch.setattr(ci, "collect_failure", failure)
    with pytest.raises(ValueError):
        cache.snapshot("unknown")
    with pytest.raises(ValueError):
        cache.snapshot("pr1", "run1")
    cache.snapshot("pr1")
    wait(cache)
    assert not failure.called
    for key in ["arbitrary", "green"]:
        with pytest.raises(ValueError):
            cache.snapshot("pr1", key)
    cache.snapshot("pr1", "run1")
    wait(cache)
    assert cache.snapshot("pr1", "run1")["value"]["title"] == "test_foo failed"
    failure.assert_called_once_with("run1", SHA)


def test_github_query_is_json_read_only_and_errors_are_visible(monkeypatch):
    command = Mock(
        return_value=subprocess.CompletedProcess(
            [], 0, json.dumps({"data": {"node": {"id": "run1"}}}), ""
        )
    )
    monkeypatch.setattr(owned_process, "run", command)
    assert ci.graphql(ci.FAILURE_QUERY, {"id": "run1"}) == {"id": "run1"}
    args, kwargs = command.call_args
    assert args[0] == ["gh", "api", "--hostname", "github.com", "graphql", "--input", "-"]
    assert kwargs["timeout"] == 45
    assert json.loads(kwargs["input"])["variables"] == {"id": "run1"}
    command.return_value = subprocess.CompletedProcess([], 1, "", "authentication failed")
    with pytest.raises(ValueError, match="authentication"):
        ci.graphql(ci.CHECKS_QUERY, {})
    command.return_value = subprocess.CompletedProcess([], 0, '{"errors":[{}]}', "")
    with pytest.raises(ValueError, match="complete CI details"):
        ci.graphql(ci.CHECKS_QUERY, {})


def test_ci_http_protection_and_no_prefetch(cache, monkeypatch):
    fetch = Mock(return_value={"checks": [], "sha": SHA})
    monkeypatch.setattr(ci, "collect_checks", fetch)
    with dashboard.DashboardServer(
        cache.path.parent, 0, overview=cache.overview, ci=cache
    ) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:

            def request(path, headers=None):
                conn = http.client.HTTPConnection("127.0.0.1", server.server_port)
                conn.request("GET", path, headers=headers or {})
                response = conn.getresponse()
                value = response.status, json.loads(response.read())
                conn.close()
                return value

            assert request("/api/prs")[0] == 200 and not fetch.called
            assert request("/api/pr-ci?id=pr1", {"Host": "evil.test"})[0] == 403
            assert request("/api/pr-ci?id=pr1", {"Sec-Fetch-Site": "cross-site"})[0] == 403
            assert not fetch.called
            assert request("/api/pr-ci?id=pr1")[0] == 200
            wait(cache)
            assert request("/api/pr-ci?id=pr1")[1]["value"]["sha"] == SHA
            assert fetch.call_count == 1
            assert request("/api/pr-ci?id=pr1&check=arbitrary")[0] == 400
        finally:
            server.shutdown()
            thread.join()


def comment(text, author="reviewer"):
    return {
        "body": text,
        "url": f"https://github.com/c/{len(text)}",
        "createdAt": "2026-09-24T10:00:00Z",
        "author": {"login": author},
    }


def test_collect_reviews_keeps_only_feedback_and_unresolved_threads(monkeypatch):
    node = {
        "updatedAt": "2026-09-24T10:00:00Z",
        "reviews": {
            "nodes": [
                {
                    "state": "COMMENTED",
                    "body": "",
                    "url": "u1",
                    "submittedAt": "t1",
                    "author": None,
                },
                {
                    "state": "CHANGES_REQUESTED",
                    "body": "",
                    "url": "u2",
                    "submittedAt": "t2",
                    "author": {"login": "a"},
                },
                {
                    "state": "APPROVED",
                    "body": "x" * 5000,
                    "url": "u3",
                    "submittedAt": "t3",
                    "author": {"login": "b"},
                },
                None,
            ]
        },
        "reviewThreads": {
            "totalCount": 150,
            "nodes": [
                {
                    "isResolved": True,
                    "isOutdated": False,
                    "path": "done.py",
                    "line": 1,
                    "comments": {"totalCount": 1, "nodes": [comment("resolved")]},
                },
                {
                    "isResolved": False,
                    "isOutdated": True,
                    "path": "a.py",
                    "line": None,
                    "originalLine": 7,
                    "comments": {"totalCount": 25, "nodes": [comment("please fix"), None]},
                },
            ],
        },
    }
    calls = []
    monkeypatch.setattr(ci, "graphql", lambda query, variables: calls.append(variables) or node)
    value = ci.collect_reviews({"id": "pr1"})
    assert calls == [{"id": "pr1"}]
    # Newest first; an inline-only COMMENTED review has nothing of its own to show.
    assert [r["url"] for r in value["reviews"]] == ["u3", "u2"]
    assert value["reviews"][0]["body"] == "x" * ci.MAX_BODY + "…"
    assert value["for_updated_at"] is None
    assert value["reviews"][1]["author"] == "a"
    [thread] = value["threads"]
    assert (thread["path"], thread["line"], thread["outdated"]) == ("a.py", 7, True)
    assert [c["body"] for c in thread["comments"]] == ["please fix"]
    assert thread["more_comments"] == 24 and value["truncated"] is True
    assert "mutation" not in ci.REVIEWS_QUERY


def test_review_details_are_lazy_and_refetch_after_new_activity(cache, monkeypatch):
    fetch = Mock(
        side_effect=lambda pr: {"threads": [], "reviews": [], "for_updated_at": pr["updated_at"]}
    )
    monkeypatch.setattr(ci, "collect_reviews", fetch)
    cache.overview.value["prs"][0]["updated_at"] = "2026-09-24T10:00:00Z"
    assert cache.snapshot("pr1", reviews=True)["refreshing"]
    wait(cache)
    assert cache.snapshot("pr1", reviews=True)["value"]["threads"] == []
    assert not cache.snapshot("pr1", reviews=True)["refreshing"] and fetch.call_count == 1
    # CI details for the same PR are a separate cache entry.
    monkeypatch.setattr(ci, "collect_checks", Mock(return_value={"checks": [], "sha": SHA}))
    cache.snapshot("pr1")
    wait(cache)
    assert fetch.call_count == 1
    entries = len(cache.entries)
    # New activity refetches before the TTL, keeps showing the old feedback meanwhile,
    # and replaces the PR's one review entry instead of adding another.
    cache.overview.value["prs"][0]["updated_at"] = "2026-09-24T11:00:00Z"
    stale = cache.snapshot("pr1", reviews=True)
    assert stale["refreshing"] and stale["stale"] and stale["value"] is not None
    wait(cache)
    current = cache.snapshot("pr1", reviews=True)
    assert current["value"]["for_updated_at"] == "2026-09-24T11:00:00Z"
    assert not current["refreshing"] and fetch.call_count == 2
    assert len(cache.entries) == entries


def test_review_text_is_bounded_per_entry(monkeypatch):
    thread = {
        "isResolved": False,
        "path": "a.py",
        "line": 1,
        "comments": {"totalCount": 20, "nodes": [comment("y" * 5000) for _ in range(20)]},
    }
    node = {"reviews": {"nodes": []}, "reviewThreads": {"totalCount": 100, "nodes": [thread] * 100}}
    monkeypatch.setattr(ci, "graphql", lambda query, variables: node)
    value = ci.collect_reviews({"id": "pr1", "updated_at": "t"})
    bodies = [c["body"] for t in value["threads"] for c in t["comments"]]
    assert len("".join(bodies)) < ci.MAX_REVIEW_TEXT + 60 * len(bodies)
    assert bodies[-1] == "(Not shown here; open it on GitHub.)"
    assert value["for_updated_at"] == "t"


def test_review_http_route_is_protected(cache, monkeypatch):
    fetch = Mock(return_value={"reviews": [], "threads": [], "truncated": False})
    monkeypatch.setattr(ci, "collect_reviews", fetch)
    with dashboard.DashboardServer(
        cache.path.parent, 0, overview=cache.overview, ci=cache
    ) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:

            def request(path, headers=None):
                conn = http.client.HTTPConnection("127.0.0.1", server.server_port)
                conn.request("GET", path, headers=headers or {})
                response = conn.getresponse()
                value = response.status, json.loads(response.read())
                conn.close()
                return value

            assert request("/api/pr-reviews?id=pr1", {"Host": "evil.test"})[0] == 403
            assert not fetch.called
            assert request("/api/pr-reviews?id=pr1")[0] == 200
            wait(cache)
            assert request("/api/pr-reviews?id=pr1")[1]["value"]["threads"] == []
            assert request("/api/pr-reviews?id=unknown")[0] == 400
        finally:
            server.shutdown()
            thread.join()
