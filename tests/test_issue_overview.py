"""Issue discovery reuses the PR search machinery with fake GitHub responses only."""

from unittest.mock import Mock

import issue_overview
import owned_process
import pr_overview
import pytest
from test_pr_overview import page


def issue(key="one", updated="2026-09-11T12:00:00Z", linked=(), **extra):
    return {
        "id": key,
        "number": 12,
        "title": "An issue",
        "url": f"https://github.com/test/repo/issues/{key}",
        "repository": {"nameWithOwner": "test/repo"},
        "author": {"login": "alice"},
        "labels": {"nodes": [{"name": "kind/bug", "color": "d73a4a"}, None]},
        "assignees": {"nodes": [{"login": "bob"}]},
        "comments": {"totalCount": 3},
        "updatedAt": updated,
        "createdAt": "2025-01-02T09:00:00Z",
        "state": "OPEN",
        "closedByPullRequestsReferences": {"nodes": list(linked)},
        **extra,
    }


def linked(number=40, state="OPEN"):
    return {
        "id": f"PR_{number}",
        "number": number,
        "title": "Fix it",
        "url": f"https://github.com/test/repo/pull/{number}",
        "state": state,
        "isDraft": False,
        "repository": {"nameWithOwner": "test/repo"},
        "headRepository": {"nameWithOwner": "fork/repo"},
        "headRefName": "fix-12",
        "headRefOid": "b" * 40,
    }


def test_collects_every_role_merges_duplicates_and_maps_linked_prs(monkeypatch):
    fetch = Mock(
        side_effect=[
            page([issue(), {}, issue("closed", state="CLOSED")], "next"),
            page([issue("two", author=None, labels=None, assignees=None)]),
            page([issue(updated="2026-09-11T13:00:00Z", linked=[linked(), None])]),
            page([issue(updated="2026-09-11T11:00:00Z")]),
            page([issue(updated="2026-09-11T11:00:00Z"), issue("three")]),
        ]
    )
    monkeypatch.setattr(pr_overview, "github_page", fetch)
    result = issue_overview.collect()
    assert "prs" not in result and len(result["issues"]) == 3
    first = result["issues"][0]
    assert first["roles"] == ["author", "assignee", "mentioned", "participant"]
    assert first["updated_at"] == "2026-09-11T13:00:00Z"
    assert first["labels"] == [{"name": "kind/bug", "color": "d73a4a"}]
    assert first["assignees"] == ["bob"] and first["comments"] == 3
    assert first["linked_prs"] == [
        {
            "id": "PR_40",
            "number": 40,
            "title": "Fix it",
            "url": "https://github.com/test/repo/pull/40",
            "repo": "test/repo",
            "state": "OPEN",
            "draft": False,
            "head_repo": "fork/repo",
            "head_branch": "fix-12",
            "head_sha": "b" * 40,
        }
    ]
    assert "head_sha" not in first and "ci" not in first
    second = result["issues"][1]
    assert second["author"] is None and second["labels"] == [] and second["assignees"] == []
    queries = [call.args[0] for call in fetch.call_args_list]
    assert queries[0] == "is:issue is:open author:@me sort:updated-desc"
    assert queries[-1] == "is:issue is:open commenter:@me sort:updated-desc"
    assert "mentions:@me" in queries[3]
    assert fetch.call_args_list[1].args[1] == "next"
    assert all(call.args[2] == issue_overview.ISSUE_FRAGMENT for call in fetch.call_args_list)
    assert result["login"] == "alice" and not result["warnings"]


def test_issue_mentions_follow_the_shared_setting(tmp_path, monkeypatch):
    (tmp_path / "overview-config.json").write_text('{"mentions": false}')
    fetch = Mock(return_value=page([issue()]))
    monkeypatch.setattr(pr_overview, "github_page", fetch)
    issue_overview.Overview(tmp_path).refresh()
    queries = [call.args[0] for call in fetch.call_args_list]
    assert len(queries) == 3 and not any("mentions:" in query for query in queries)
    assert issue_overview.Overview(tmp_path).value["roles"] == [
        "author",
        "assignee",
        "participant",
    ]


def test_search_limit_names_issues(monkeypatch):
    fetch = Mock(side_effect=[page([], str(i), 1001) for i in range(20)] + [page()] * 3)
    monkeypatch.setattr(pr_overview, "github_page", fetch)
    result = issue_overview.collect()
    assert result["warnings"] == ["Only the most recently updated 1,000 author issues are shown."]
    assert fetch.call_count == 23


def test_fragment_is_sent_in_the_graphql_query(monkeypatch):
    run = Mock(return_value=Mock(returncode=0, stdout='{"data": {"ok": true}}'))
    monkeypatch.setattr(owned_process, "run", run)
    pr_overview.github_page("is:issue", None, issue_overview.ISSUE_FRAGMENT)
    sent = run.call_args.kwargs["input"]
    assert "closedByPullRequestsReferences" in sent and "on PullRequest" not in sent
    pr_overview.github_page("is:pr")
    assert "on PullRequest" in run.call_args.kwargs["input"]


def test_issue_cache_is_separate_and_names_its_errors(tmp_path, monkeypatch):
    cache = issue_overview.Overview(tmp_path)
    assert cache.value["issues"] == [] and "prs" not in cache.value
    good = {"issues": [{"id": "one"}], "login": "alice", "warnings": []}
    monkeypatch.setattr(issue_overview, "collect", Mock(side_effect=[good, ValueError("nope")]))
    cache.refresh()
    assert cache.value["issues"] == good["issues"]
    assert (tmp_path / "issue-overview.json").stat().st_mode & 0o777 == 0o600
    assert not (tmp_path / "pr-overview.json").exists()
    assert issue_overview.Overview(tmp_path).value["issues"] == good["issues"]
    assert pr_overview.Overview(tmp_path).value["prs"] == []
    cache.refresh()
    assert cache.value["error"] == "Cannot sync GitHub issues: nope"
    assert cache.value["issues"] == good["issues"]


@pytest.mark.parametrize(
    "branch,number",
    [
        ("issue-12", 12),
        ("issue-12-fix-the-crash", 12),
        ("issue-12-2", 12),
        ("issue-120", 120),
        ("issue-", None),
        ("issues-12", None),
        ("fix-issue-12", None),
        (None, None),
    ],
)
def test_branch_number(branch, number):
    assert issue_overview.branch_number(branch) == number
