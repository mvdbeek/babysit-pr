"""Feedback lifecycle checks with isolated queues and fake GitHub responses."""

import copy
import hashlib
import json

import gh_pr_watch as watch
import pr_supervisor as supervisor
import pytest
from test_feedback_gate import feedback_job, persist


def thread_page(root, resolved=False, cursor=None):
    return {
        "data": {
            "repository": {
                "pullRequest": {
                    "reviewThreads": {
                        "nodes": [
                            {"isResolved": resolved, "comments": {"nodes": [{"databaseId": root}]}}
                        ],
                        "pageInfo": {"hasNextPage": cursor is not None, "endCursor": cursor},
                    }
                }
            }
        }
    }


@pytest.fixture
def feedback_source(monkeypatch):
    comment = {
        "id": 1,
        "user": {"login": "reviewer"},
        "author_association": "MEMBER",
        "body": "Fix the assertion",
        "path": "test.py",
        "line": 10,
    }
    source = {"comment": comment, "resolved": False}
    monkeypatch.setattr(
        watch,
        "gh_api_list_paginated",
        lambda endpoint, **kw: (
            [source["comment"]] if "/pulls/" in endpoint and "/comments" in endpoint else []
        ),
    )
    monkeypatch.setattr(watch, "gh_json", lambda *a, **kw: thread_page(1, source["resolved"]))
    return source


def test_handled_feedback_does_not_return_when_lines_move(feedback_source):
    pr, state = {"repo": "test/repo", "number": 1}, {}
    items = watch.fetch_new_review_items(pr, state, True)
    j = feedback_job()
    j.update(
        pending_reviews=items,
        watcher_state=state,
        dispatched_review_items=copy.deepcopy(items),
        dispatched_reviews=["review_comment:1"],
        dispatched_keys=[],
    )
    supervisor.finish_repair(j, {"status": "waiting", "summary": "Fixed"})
    assert not j["pending_reviews"]
    feedback_source["comment"]["line"] = 30
    assert watch.fetch_new_review_items(pr, state, False) == []
    feedback_source["comment"]["body"] = "This still fails for empty inputs"
    assert watch.fetch_new_review_items(pr, state, False)[0]["body"].endswith("empty inputs")


def test_legacy_feedback_versions_upgrade_without_replaying_comments(feedback_source):
    item = watch.normalize_review_comments([feedback_source["comment"]], {})[0]
    state = {
        "seen_review_comment_ids": ["1"],
        "seen_feedback_versions": {
            "review_comment:1": hashlib.sha256(
                json.dumps(item, sort_keys=True).encode()
            ).hexdigest()
        },
    }
    pr = {"repo": "test/repo", "number": 1}
    assert watch.fetch_new_review_items(pr, state, False) == []
    feedback_source["comment"]["line"] = 50
    assert watch.fetch_new_review_items(pr, state, False) == []


def test_resolved_feedback_is_removed_and_reopening_needs_approval(feedback_source):
    pr, state = {"repo": "test/repo", "number": 1}, {}
    items = watch.fetch_new_review_items(pr, state, True)
    j = feedback_job()
    j.update(pending_reviews=items, approved_reviews=[supervisor.feedback_token(items[0])])
    feedback_source["resolved"] = True
    assert watch.fetch_new_review_items(pr, state, False) == []
    s = {**j["snapshot"], "inactive_review_keys": state["inactive_review_keys"]}
    supervisor.ingest(j, s, state)
    assert not j["pending_reviews"] and not j["approved_reviews"]
    feedback_source["resolved"] = False
    reopened = watch.fetch_new_review_items(pr, state, False)
    supervisor.ingest(j, {**s, "inactive_review_keys": [], "new_review_items": reopened}, state)
    assert len(j["pending_reviews"]) == 1 and not supervisor.approved_feedback(j)


def test_resolved_thread_pagination_includes_replies(monkeypatch):
    calls = []

    def fetch(args, **kw):
        calls.append(args)
        return thread_page(2, True) if "cursor=next" in args else thread_page(1, False, "next")

    monkeypatch.setattr(watch, "gh_json", fetch)
    result = watch.resolved_review_comment_ids(
        {"repo": "test/repo", "number": 1},
        [{"id": 1}, {"id": 2}, {"id": 3, "in_reply_to_id": 2}],
    )
    assert result == {"2", "3"} and len(calls) == 2


@pytest.mark.parametrize(
    "payload", [{"errors": [{"message": "Denied"}]}, {}, thread_page(1, True, "repeated")]
)
def test_resolution_errors_do_not_consume_feedback(feedback_source, monkeypatch, payload):
    state = {"seen_review_comment_ids": ["old"]}
    before = copy.deepcopy(state)
    monkeypatch.setattr(watch, "gh_json", lambda *a, **kw: payload)
    with pytest.raises(watch.GhCommandError):
        watch.fetch_new_review_items({"repo": "test/repo", "number": 1}, state, False)
    assert state == before


def test_empty_reviews_are_inactive_but_substantive_reviews_remain(monkeypatch):
    reviews = [
        {
            "id": n,
            "body": body,
            "state": "COMMENTED",
            "user": {"login": "reviewer"},
            "author_association": "MEMBER",
        }
        for n, body in [(1, "  "), (2, "Needs a rebase")]
    ]
    monkeypatch.setattr(
        watch,
        "gh_api_list_paginated",
        lambda endpoint, **kw: reviews if endpoint.endswith("/reviews") else [],
    )
    state = {}
    items = watch.fetch_new_review_items({"repo": "test/repo", "number": 1}, state, True)
    assert [v["id"] for v in items] == ["2"]
    assert state["inactive_review_keys"] == ["review:1"]


def test_mark_addressed_is_specific_durable_and_rejects_stale_clicks(tmp_path, feedback_source):
    pr, state = {"repo": "test/repo", "number": 1}, {}
    items = watch.fetch_new_review_items(pr, state, True)
    j = feedback_job()
    j.update(pending_reviews=items + j["pending_reviews"], watcher_state=state)
    db = supervisor.open_db(tmp_path)
    persist(db, j)
    token = supervisor.feedback_token(j["pending_reviews"])
    updated = supervisor.mark_feedback_addressed(db, j["id"], token, "review_comment:1")
    assert [v["kind"] for v in updated["pending_reviews"]] == ["issue_comment"]
    assert updated["epoch"] == 1 and not supervisor.actionable(updated)
    with pytest.raises(ValueError, match="changed"):
        supervisor.mark_feedback_addressed(db, j["id"], token, "issue_comment:1")
    db.close()
    db = supervisor.open_db(tmp_path)
    updated = supervisor.get_job(db, j["id"])
    feedback_source["comment"]["line"] = 40
    assert watch.fetch_new_review_items(pr, updated["watcher_state"], False) == []
    feedback_source["comment"]["body"] = "Edited request"
    assert len(watch.fetch_new_review_items(pr, updated["watcher_state"], False)) == 1
    db.close()


@pytest.mark.parametrize("status", ["running", "closed", "stopped", "awaiting_release"])
def test_mark_addressed_does_not_interfere_with_running_or_ended_watches(tmp_path, status):
    db = supervisor.open_db(tmp_path)
    j = feedback_job()
    j["status"] = status
    persist(db, j)
    with pytest.raises(ValueError, match="watch state"):
        supervisor.mark_feedback_addressed(
            db, j["id"], supervisor.feedback_token(j["pending_reviews"]), "issue_comment:1"
        )
    assert supervisor.get_job(db, j["id"])["pending_reviews"] == j["pending_reviews"]
    db.close()
