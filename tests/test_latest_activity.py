"""Activity attribution uses bounded fake timelines, never live GitHub items."""

import json
from unittest.mock import Mock

import issue_overview
import pr_overview
import pytest
from latest_activity import latest_activity, timeline_activity
from test_issue_overview import issue
from test_pr_overview import page, pr

EARLY = "2026-09-11T10:00:00Z"
LATE = "2026-09-11T11:00:00Z"
URL = "https://github.com/test/repo/pull/1#issuecomment-2"


def comment(**extra):
    return {
        "__typename": "IssueComment",
        "author": {"login": "alice"},
        "createdAt": EARLY,
        "url": URL,
        **extra,
    }


def test_comment_edit_uses_editor_and_edit_time_not_original_author():
    node = pr(
        timelineItems={
            "nodes": [
                comment(lastEditedAt=LATE, editor={"login": "bob"}),
                {"__typename": "LabeledEvent", "actor": {"login": "alice"}, "createdAt": EARLY},
            ]
        }
    )
    result = latest_activity(node)
    assert result == {
        "type": "ContentEdited",
        "actor": "bob",
        "action": "edited a comment",
        "at": LATE,
        "url": URL,
    }
    assert node["updatedAt"] != result["at"]


@pytest.mark.parametrize("factory", [pr, issue])
def test_description_edit_competes_with_timeline_activity(factory):
    node = factory(timelineItems={"nodes": [comment()]}, lastEditedAt=LATE, editor={"login": "bob"})
    result = latest_activity(node)
    assert (result["actor"], result["action"], result["at"]) == (
        "bob",
        "edited the description",
        LATE,
    )
    node["lastEditedAt"] = "2026-09-10T10:00:00Z"
    assert latest_activity(node)["action"] == "commented"


@pytest.mark.parametrize("bot", ["dependabot", "dependabot[bot]"])
def test_bot_label_event_has_label_and_explicit_bot_identity(bot):
    event = {
        "__typename": "LabeledEvent",
        "actor": {"login": bot, "__typename": "Bot"},
        "createdAt": LATE,
        "label": {"name": "dependencies"},
    }
    assert timeline_activity(event) == {
        "type": "LabeledEvent",
        "actor": "dependabot[bot]",
        "action": "added a label: dependencies",
        "at": LATE,
        "url": None,
    }


@pytest.mark.parametrize(
    "state,action",
    [
        ("APPROVED", "approved"),
        ("CHANGES_REQUESTED", "requested changes"),
        ("COMMENTED", "reviewed"),
        ("DISMISSED", "submitted a review (now dismissed)"),
    ],
)
def test_reviews_use_submission_time_and_state(state, action):
    event = {
        "__typename": "PullRequestReview",
        "state": state,
        "createdAt": EARLY,
        "submittedAt": LATE,
        "author": {"login": "reviewer"},
        "url": URL,
    }
    result = timeline_activity(event)
    assert (result["actor"], result["action"], result["at"], result["url"]) == (
        "reviewer",
        action,
        LATE,
        URL,
    )
    event.update(lastEditedAt="2026-09-12T10:00:00Z", editor=None)
    assert timeline_activity(event)["action"] == "edited a review"
    assert timeline_activity(event)["actor"] is None
    event["state"] = "PENDING"
    assert timeline_activity(event) is None
    event.update(state="COMMENTED", submittedAt=None)
    assert timeline_activity(event) is None


@pytest.mark.parametrize(
    "user,expected", [({"login": "committer"}, "committer"), (None, "Git Name")]
)
def test_commits_never_claim_to_identify_the_pusher(user, expected):
    result = timeline_activity(
        {
            "__typename": "PullRequestCommit",
            "commit": {
                "committedDate": EARLY,
                "committer": {"name": "Git Name", "user": user},
                "url": "https://github.com/test/repo/commit/abc",
            },
        }
    )
    assert (result["actor"], result["action"], result["at"]) == (expected, "committed", EARLY)
    assert result["url"].endswith("/commit/abc")


def test_force_push_identifies_the_actual_event_actor():
    result = timeline_activity(
        {
            "__typename": "HeadRefForcePushedEvent",
            "actor": {"login": "pusher"},
            "createdAt": LATE,
        }
    )
    assert (result["actor"], result["action"], result["at"]) == (
        "pusher",
        "force-pushed the branch",
        LATE,
    )


@pytest.mark.parametrize(
    "tail", [None, {"__typename": "FutureEvent"}, {"__typename": "IssueComment"}]
)
def test_unknown_final_event_does_not_misattribute_older_activity(tail):
    assert latest_activity(pr(timelineItems={"nodes": [comment(), tail]})) is None


def test_missing_actor_and_empty_or_missing_timeline():
    assert timeline_activity(comment(author=None))["actor"] is None
    assert latest_activity(pr()) is None
    assert latest_activity(pr(timelineItems=None)) is None
    opened = latest_activity(issue(timelineItems={"nodes": []}))
    assert (opened["actor"], opened["action"], opened["at"]) == (
        "alice",
        "opened this",
        "2025-01-02T09:00:00Z",
    )
    assert latest_activity(issue(timelineItems={"nodes": []}, createdAt=None)) is None


@pytest.mark.parametrize("module,factory,calls", [(pr_overview, pr, 3), (issue_overview, issue, 4)])
def test_collect_includes_activity_in_existing_requests(monkeypatch, module, factory, calls):
    node = factory(timelineItems={"nodes": [comment()]})
    run = Mock(return_value=Mock(returncode=0, stdout=json.dumps({"data": page([node])})))
    monkeypatch.setattr(pr_overview.subprocess, "run", run)
    result = module.collect()
    key = "prs" if module is pr_overview else "issues"
    assert result[key][0]["latest_activity"]["actor"] == "alice"
    assert result[key][0]["latest_activity"]["url"] == URL
    assert run.call_count == calls
    for call in run.call_args_list:
        query = json.loads(call.kwargs["input"])["query"]
        assert "timelineItems(last: 5)" in query
        assert "lastEditedAt editor" in query
        assert ("... on PullRequestReview" in query) == (key == "prs")


def test_activity_survives_cache_reload(tmp_path, monkeypatch):
    node = pr(timelineItems={"nodes": [comment()]})
    record = pr_overview.pr_record(node, ["author"])
    monkeypatch.setattr(pr_overview, "collect", lambda: {"prs": [record], "login": "alice"})
    pr_overview.Overview(tmp_path).refresh()
    assert (
        pr_overview.Overview(tmp_path).value["prs"][0]["latest_activity"]
        == record["latest_activity"]
    )
