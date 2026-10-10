"""The attention feed: one row per item, merged across sources, grouped by what to do."""

import attention
import pytest

NOW = 1_800_000_000.0


def pr(**fields):
    return {
        "id": "PR_1",
        "number": 1,
        "title": "Fix the flaky test",
        "url": "https://github.com/base/repo/pull/1",
        "repo": "base/repo",
        "roles": ["author"],
        "ci": "SUCCESS",
        "draft": False,
        "review_decision": None,
        "unresolved_threads": 0,
        "review_requests": [],
        "updated_at": "2027-01-15T10:00:00Z",
        **fields,
    }


def watch(**fields):
    return {
        "id": "job-1",
        "url": "https://github.com/base/repo/pull/1",
        "repo": "base/repo",
        "kind": "pr",
        "number": 1,
        "title": "Fix the flaky test",
        "status": "watching",
        "summary": "",
        "pending_reviews": 0,
        "cleanup_ready": False,
        "pr_outcome": None,
        "updated_at": NOW - 100,
        **fields,
    }


def workspace_row(**fields):
    return {
        "key": "/src/worktrees/repo/feature",
        "name": "feature",
        "repo": "base/repo",
        "status": "active",
        "missing": False,
        "agents": [],
        "agent_status": None,
        "workspace_ids": [],
        "workspace_url": None,
        "links": [],
        "blockers": [],
        "updated_at": NOW - 50,
        **fields,
    }


def by_key(feed):
    return {item["key"]: item for item in feed["items"]}


def codes(item):
    return [reason["code"] for reason in item["reasons"]]


def test_empty_sources_give_an_empty_feed():
    feed = attention.collect(now=NOW)
    assert feed["items"] == []
    assert feed["pages"] == {page: 0 for page in attention.PAGES}
    assert [g["count"] for g in feed["groups"]] == [0] * 6
    assert feed["agents_waiting"] == 0
    assert feed["sources"]["prs"] == {"available": False}


def test_subject_keys_ignore_owner_case_and_trailing_slashes():
    assert attention.subject_key("https://github.com/Base/Repo/pull/1/") == (
        "https://github.com/base/repo/pull/1"
    )
    assert attention.subject_key("https://github.com/base/repo/pull/Keep") == (
        "https://github.com/base/repo/pull/Keep"
    )
    assert attention.subject_key("cron:1") == "cron:1"
    assert attention.subject_key("https://example.com/Base/Repo") == "https://example.com/Base/Repo"


def test_a_watched_pr_appears_once_with_reasons_from_every_source():
    feed = attention.collect(
        watcher={"jobs": [watch(status="blocked", summary="Needs a decision")]},
        prs={
            "login": "alice",
            "prs": [
                pr(ci="FAILURE", unresolved_threads=2, url="https://github.com/Base/Repo/pull/1/")
            ],
        },
        workspaces={
            "prs": {
                "PR_1": {
                    "matches": [
                        {
                            "workspace_id": "w1",
                            "agent_status": "blocked",
                            "url": "http://127.0.0.1:8787/space/w1",
                        }
                    ]
                }
            },
            "watches": {"watch:job-1": {"matches": []}},
        },
        now=NOW,
    )
    [item] = feed["items"]
    assert item["key"] == "https://github.com/base/repo/pull/1"
    assert item["url"] == "https://github.com/base/repo/pull/1"
    assert item["group"] == "answer"
    # A live watch repairs CI itself, so its failing CI is not a separate reason.
    assert codes(item) == ["watch_blocked", "threads", "agent_blocked"]
    assert item["pages"] == ["watcher", "prs"]
    assert item["watch"] == {"id": "job-1", "status": "blocked"}
    assert item["workspace_url"] == "http://127.0.0.1:8787/space/w1"
    assert item["agent"] == {"status": "blocked"}
    assert item["since"] == pytest.approx(attention.epoch("2027-01-15T10:00:00Z"))
    assert feed["pages"]["watcher"] == 1 and feed["pages"]["prs"] == 1
    assert feed["agents_waiting"] == 1


def test_a_watch_is_active_when_it_starts_or_changes_status_not_when_it_polls():
    job = watch(status="blocked", summary="S", started_at=NOW - 500, updated_at=NOW)
    assert attention.collect(watcher={"jobs": [job]}, now=NOW)["items"][0]["since"] == NOW - 500
    job["notification_actions"] = [{"at": NOW - 100, "before": {}, "after": {}}]
    assert attention.collect(watcher={"jobs": [job]}, now=NOW)["items"][0]["since"] == NOW - 100
    job.update(started_at=None, notification_actions=[])
    assert attention.collect(watcher={"jobs": [job]}, now=NOW)["items"][0]["since"] is None


def test_a_paused_watch_does_not_hide_failing_ci():
    feed = attention.collect(
        watcher={"jobs": [watch(status="paused")]},
        prs={"login": "me", "prs": [pr(ci="FAILURE")]},
        now=NOW,
    )
    [item] = feed["items"]
    assert codes(item) == ["ci_failing"] and item["pages"] == ["prs"]


def test_pr_reasons_follow_authorship_review_state_and_ci():
    prs = [
        pr(id="mine", url="u/mine", ci="FAILURE"),
        pr(id="errored", url="u/errored", ci="ERROR"),
        pr(id="changes", url="u/changes", review_decision="CHANGES_REQUESTED"),
        pr(id="ready", url="u/ready", review_decision="APPROVED"),
        pr(id="ready-draft", url="u/ready-draft", review_decision="APPROVED", draft=True),
        pr(id="ready-red", url="u/ready-red", review_decision="APPROVED", ci="FAILURE"),
        pr(id="theirs", url="u/theirs", roles=["reviewer"], ci="FAILURE", review_requests=["me"]),
        pr(id="team", url="u/team", roles=["reviewer"], review_requests=["team:core"]),
        pr(id="quiet", url="u/quiet"),
    ]
    feed = attention.collect(prs={"login": "me", "prs": prs}, now=NOW)
    found = by_key(feed)
    assert codes(found["u/mine"]) == ["ci_failing"]
    assert found["u/mine"]["reasons"][0]["text"] == "CI failing"
    assert found["u/errored"]["reasons"][0]["text"] == "CI errored"
    assert codes(found["u/changes"]) == ["changes_requested"]
    assert codes(found["u/ready"]) == ["approved"] and found["u/ready"]["group"] == "review"
    assert "u/ready-draft" not in found
    assert codes(found["u/ready-red"]) == ["ci_failing"]
    # Someone else's failing CI is theirs to fix; a requested review is mine to give.
    assert codes(found["u/theirs"]) == ["review_requested"]
    assert "u/team" not in found
    assert "u/quiet" not in found


def test_groups_order_items_and_a_merged_item_takes_its_first_group():
    feed = attention.collect(
        prs={
            "login": "me",
            "prs": [
                pr(
                    id="a", url="u/a", review_decision="APPROVED", updated_at="2027-01-11T00:00:00Z"
                ),
                pr(id="b", url="u/b", ci="FAILURE", updated_at="2027-01-13T00:00:00Z"),
                pr(id="c", url="u/c", ci="FAILURE", updated_at="2027-01-12T00:00:00Z"),
            ],
        },
        workspaces={"prs": {"c": {"matches": [{"workspace_id": "w", "agent_status": "blocked"}]}}},
        now=NOW,
    )
    assert [item["key"] for item in feed["items"]] == ["u/c", "u/a", "u/b"]
    assert [item["group"] for item in feed["items"]] == ["answer", "review", "unblock"]
    assert [g["count"] for g in feed["groups"]] == [1, 1, 1, 0, 0, 0]


def test_watch_reasons_cover_feedback_release_and_cleanup():
    jobs = [
        watch(id="fb", url="u/fb", pending_reviews=2),
        watch(id="queued", url="u/queued", pending_reviews=2, feedback_approved=2),
        watch(id="partly", url="u/partly", pending_reviews=2, feedback_approved=1),
        watch(id="release", url="u/release", status="awaiting_release"),
        watch(id="hand", url="u/hand", status="handoff"),
        watch(id="done", url="u/done", status="closed", cleanup_ready=True, pr_outcome="merged"),
        watch(id="stale", url="u/stale", status="stopped", pending_reviews=3),
        watch(id="branch", url=None, kind="branch", title=None, branch="ci-fix", status="blocked"),
    ]
    feed = attention.collect(watcher={"jobs": jobs}, now=NOW)
    found = by_key(feed)
    assert found["u/fb"]["reasons"][0]["text"] == "2 feedback items await approval"
    assert "u/queued" not in found
    assert found["u/partly"]["reasons"][0]["text"] == "1 feedback item awaits approval"
    assert codes(found["u/release"]) == ["watch_release"]
    # The automatic handoff is running; nothing waits on the user.
    assert "u/hand" not in found
    assert found["u/done"]["group"] == "tidy"
    assert found["u/done"]["reasons"][0]["text"] == "Merged: its checkout is ready to clean up"
    assert "u/stale" not in found
    assert found["watch:branch"]["title"] == "ci-fix" and found["watch:branch"]["kind"] == "branch"


def test_issues_need_picking_up_only_when_nothing_has_started():
    issue = {
        "id": "I1",
        "number": 7,
        "title": "Crash",
        "url": "u/i1",
        "repo": "base/repo",
        "roles": ["assignee"],
        "linked_prs": [],
        "updated_at": "2027-01-15T10:00:00Z",
    }
    cases = {
        "bare": ({}, issue),
        "linked": ({}, {**issue, "linked_prs": [{"state": "OPEN"}]}),
        "merged-link": ({}, {**issue, "linked_prs": [{"state": "MERGED"}]}),
        "checkout": ({"I1": {"matches": [{"workspace_id": None, "agent_status": "x"}]}}, issue),
        "scheduled": ({"I1": {"scheduled": [{"start_at": NOW + 60}]}}, issue),
        "mentioned": ({}, {**issue, "roles": ["mentioned"]}),
    }
    results = {
        name: codes(by_key(feed).get("u/i1", {"reasons": []}))
        for name, (targets, record) in cases.items()
        for feed in [
            attention.collect(issues={"issues": [record]}, workspaces={"issues": targets}, now=NOW)
        ]
    }
    assert results == {
        "bare": ["assigned"],
        "linked": [],
        "merged-link": ["assigned"],
        "checkout": [],
        "scheduled": [],
        "mentioned": [],
    }
    # Without the workspace inventory, nobody can say nothing was started.
    assert attention.collect(issues={"issues": [issue]}, now=NOW)["items"] == []

    def broken():
        raise RuntimeError("no inventory")

    assert attention.collect(issues={"issues": [issue]}, workspaces=broken, now=NOW)["items"] == []


def test_agents_waiting_or_finished_in_issue_checkouts_are_listed():
    issue = {"id": "I1", "number": 7, "title": "Crash", "url": "u/i1", "roles": ["author"]}
    feed = attention.collect(
        issues={"issues": [issue]},
        workspaces={
            "issues": {
                "I1": {
                    "matches": [
                        {"workspace_id": "w1", "agent_status": "done", "url": "c/w1"},
                        {"workspace_id": None, "agent_status": "No workspace"},
                    ],
                    "operation": {"status": "uncertain", "message": "Check its workspace"},
                }
            }
        },
        now=NOW,
    )
    [item] = feed["items"]
    assert codes(item) == ["agent_finished", "launch_uncertain"]
    assert item["group"] == "review"
    assert item["workspace_url"] == "c/w1"
    assert feed["agents_waiting"] == 0


def test_idle_agents_and_old_failed_launches_need_nobody():
    issue = {"id": "I1", "number": 7, "title": "Crash", "url": "u/i1", "roles": ["author"]}
    targets = {
        "I1": {
            "matches": [{"workspace_id": "w1", "agent_status": "idle", "url": "c/w1"}],
            "operation": {"status": "failed", "message": "Old", "updated_at": NOW - 2 * 86400},
        }
    }
    collect = lambda: attention.collect(  # noqa: E731
        issues={"issues": [issue]}, workspaces={"issues": targets}, now=NOW
    )
    assert collect()["items"] == []
    targets["I1"]["operation"]["updated_at"] = NOW - 60
    [item] = collect()["items"]
    assert codes(item) == ["launch_failed"]
    assert item["reasons"][0]["text"] == "Workspace launch failed: Old"


def test_workspace_rows_merge_with_their_pull_request():
    link = {
        "kind": "pr",
        "repo": "base/repo",
        "number": 1,
        "title": "Fix the flaky test",
        "url": "https://github.com/base/repo/pull/1",
        "state": "merged",
        "draft": False,
    }
    rows = [
        workspace_row(
            status="ready",
            links=[link],
            workspace_ids=["w1"],
            workspace_url="http://127.0.0.1:8787/space/w1",
        ),
        workspace_row(
            key="/w/dirty",
            name="dirty",
            status="blocked",
            links=[{**link, "url": "u/2", "number": 2}],
            blockers=[{"kind": "dirty", "text": "3 uncommitted change(s)"}],
        ),
        workspace_row(
            key="/w/open",
            name="open",
            status="blocked",
            links=[{**link, "url": "u/3", "state": "open"}],
            blockers=[{"kind": "dirty", "text": "1 uncommitted change(s)"}],
        ),
        workspace_row(key="/w/agent", name="agent", agents=[{"status": "blocked"}]),
        workspace_row(key="/w/busy", name="busy", agents=[{"status": "working"}]),
        workspace_row(key="workspace:w9", name="gone", missing=True, workspace_ids=["w9"]),
        # A branch watch's checkout: no link of its own, joined through its herdr workspace.
        workspace_row(
            key="/w/branch",
            name="ci-fix",
            agents=[{"status": "blocked"}],
            workspace_ids=["w7"],
            workspace_url="c/w7",
        ),
    ]
    feed = attention.collect(
        watcher={
            "jobs": [
                watch(
                    id="branch",
                    url="https://github.com/base/repo/tree/ci-fix",
                    kind="branch",
                    status="blocked",
                    summary="S",
                )
            ]
        },
        prs={"login": "me", "prs": [pr(review_decision="APPROVED")]},
        workspaces={
            "watches": {
                "watch:branch": {
                    "matches": [{"workspace_id": "w7", "agent_status": "blocked", "url": "c/w7"}]
                }
            }
        },
        workspace_overview={"enabled": True, "workspaces": rows},
        now=NOW,
    )
    found = by_key(feed)
    merged = found["https://github.com/base/repo/pull/1"]
    assert codes(merged) == ["approved", "workspace_ready"]
    assert merged["pages"] == ["prs", "workspaces"]
    assert merged["workspace_url"] == "http://127.0.0.1:8787/space/w1"
    assert found["u/2"]["reasons"][0]["text"] == (
        "Its items are closed, but: 3 uncommitted change(s)"
    )
    assert "u/3" not in found
    assert found["workspace:/w/agent"]["group"] == "answer"
    assert found["workspace:/w/agent"]["title"] == "agent"
    assert "workspace:/w/busy" not in found
    assert found["workspace:w9"]["reasons"][0]["code"] == "workspace_missing"
    branch = found["https://github.com/base/repo/tree/ci-fix"]
    assert codes(branch) == ["watch_blocked", "agent_blocked"]
    assert branch["pages"] == ["watcher", "workspaces"]
    assert "workspace:/w/branch" not in found
    assert feed["pages"]["workspaces"] == 5
    assert feed["agents_waiting"] == 2


def test_disabled_workspace_experiment_adds_nothing():
    feed = attention.collect(
        workspace_overview={"enabled": False, "workspaces": [workspace_row(status="ready")]},
        now=NOW,
    )
    assert feed["items"] == []


def test_scheduled_failures_are_recent_or_uncertain():
    subject = {"kind": "pr", "repo": "base/repo", "number": 4, "title": "T", "url": "u/4"}
    tasks = [
        {"id": "a", "status": "missed", "message": "Not started", "updated_at": NOW - 60},
        {"id": "b", "status": "failed", "message": "Old", "updated_at": NOW - 2 * 86400},
        {"id": "c", "status": "uncertain", "message": "Check", "updated_at": NOW - 3 * 86400},
        {"id": "d", "status": "scheduled", "message": "Scheduled", "updated_at": NOW},
        {"id": "e", "status": "started", "message": "Started", "updated_at": NOW},
        {"id": "f", "status": "failed", "message": "No", "updated_at": NOW, "subject": subject},
    ]
    tasks.append(
        {"id": "g", "status": "missed", "message": "Late", "updated_at": NOW, "subject": subject}
    )
    feed = attention.collect(scheduled={"tasks": tasks}, now=NOW)
    found = by_key(feed)
    assert set(found) == {"scheduled:a", "scheduled:c", "u/4"}
    assert found["u/4"]["title"] == "T" and found["u/4"]["pages"] == ["scheduled"]
    # Each failed launch of one item keeps its own message.
    assert [r["text"] for r in found["u/4"]["reasons"]] == ["No", "Late"]
    assert found["scheduled:a"]["title"] == "Scheduled task"
    assert all(item["group"] == "unblock" for item in feed["items"])


def test_cron_jobs_report_their_last_run():
    jobs = [
        {
            "id": "1",
            "name": "Nightly",
            "enabled": True,
            "runs": [
                {"status": "failed", "message": "Exited with status 2", "finished_at": NOW - 10}
            ],
        },
        {
            "id": "2",
            "name": "Agent",
            "enabled": True,
            "runs": [{"status": "attention", "message": "Asked a question"}],
        },
        {
            "id": "3",
            "name": "Paused",
            "enabled": False,
            "runs": [{"status": "failed", "message": "x"}],
        },
        {
            "id": "4",
            "name": "Fine",
            "enabled": True,
            "runs": [{"status": "succeeded", "message": "ok"}],
        },
        {"id": "5", "name": "New", "enabled": True, "runs": []},
        {
            "id": "6",
            "name": "Paused agent",
            "enabled": False,
            "runs": [{"status": "attention", "message": "y"}],
        },
    ]
    feed = attention.collect(cron={"enabled": True, "jobs": jobs}, now=NOW)
    found = by_key(feed)
    assert set(found) == {"cron:1", "cron:2", "cron:6"}
    assert found["cron:1"]["reasons"][0]["text"] == "Last run failed: Exited with status 2"
    assert found["cron:1"]["kind"] == "cron" and found["cron:1"]["title"] == "Nightly"
    assert found["cron:2"]["group"] == "answer"
    assert feed["agents_waiting"] == 2
    assert attention.collect(cron={"enabled": False, "jobs": jobs}, now=NOW)["items"] == []


def test_the_cached_feed_is_shared_for_a_few_seconds_and_refreshed_on_demand(monkeypatch):
    calls = []

    def compute():
        calls.append(1)
        return {"items": [], "cached": False}

    clock = [100.0]
    monkeypatch.setattr(attention.time, "monotonic", lambda: clock[0])
    cached = attention.Cached(compute)
    assert cached.get()["cached"] is False
    assert cached.get()["cached"] is True
    clock[0] += attention.CACHE_SECONDS
    assert cached.get()["cached"] is False
    assert cached.get(fresh=True)["cached"] is False
    assert len(calls) == 3


def test_a_failing_source_is_reported_without_emptying_the_feed():
    def broken():
        raise RuntimeError("boom")

    feed = attention.collect(
        watcher=lambda: {"jobs": [watch(status="blocked", summary="S")]},
        prs=broken,
        cron=lambda: {"enabled": True, "jobs": [], "error": "quiet"},
        now=NOW,
    )
    assert [item["key"] for item in feed["items"]] == ["https://github.com/base/repo/pull/1"]
    assert feed["sources"]["prs"] == {"available": True, "error": "prs unavailable: boom"}
    assert feed["sources"]["cron"]["error"] == "quiet"
    assert feed["sources"]["watcher"]["error"] is None


def test_long_summaries_are_clipped():
    feed = attention.collect(
        watcher={"jobs": [watch(status="blocked", summary="x " * 200)]}, now=NOW
    )
    text = feed["items"][0]["reasons"][0]["text"]
    assert text.startswith("Repair blocked: x x") and text.endswith("…") and len(text) <= 180


@pytest.mark.parametrize(
    "value,expected",
    [
        ("2027-01-15T10:00:00Z", 1800007200.0),
        (12.5, 12.5),
        (True, None),
        ("soon", None),
        (None, None),
    ],
)
def test_epoch_accepts_github_timestamps_and_numbers(value, expected):
    assert attention.epoch(value) == expected


def test_old_items_are_parked_unless_something_new_happens():
    old = "2026-12-01T00:00:00Z"  # Well over two weeks before NOW.
    feed = attention.collect(
        prs={
            "login": "me",
            "prs": [
                pr(id="stale", url="u/stale", review_decision="APPROVED", updated_at=old),
                pr(id="fresh", url="u/fresh", review_decision="APPROVED"),
            ],
        },
        workspaces={
            "prs": {"stale": {"matches": [{"workspace_id": "w1", "agent_status": "blocked"}]}}
        },
        now=NOW,
    )
    found = by_key(feed)
    # An agent waiting for an answer is never parked, however old the pull request.
    assert found["u/stale"]["parked"] is False and found["u/stale"]["group"] == "answer"
    feed = attention.collect(
        prs={
            "login": "me",
            "prs": [
                pr(id="stale", url="u/stale", review_decision="APPROVED", updated_at=old),
                pr(id="fresh", url="u/fresh", review_decision="APPROVED"),
            ],
        },
        now=NOW,
    )
    found = by_key(feed)
    assert found["u/stale"]["parked"] is True and found["u/stale"]["group"] == "parked"
    assert [item["key"] for item in feed["items"]] == ["u/fresh", "u/stale"]
    assert feed["groups"][-1] == {"key": "parked", "label": "Parked", "count": 1}
    # Parked items are listed but not counted on tabs or in the chip.
    assert feed["pages"]["prs"] == 1


def test_items_set_aside_wait_until_they_change(tmp_path):
    triage = attention.Triage(tmp_path)
    prs = {"login": "me", "prs": [pr(ci="FAILURE"), pr(id="other", url="u/other", ci="FAILURE")]}
    feed = attention.collect(prs=prs, now=NOW, triage=triage)
    assert len(feed["items"]) == 2 and feed["waiting"] == []
    with pytest.raises(ValueError, match="syncing"):
        triage.set({"login": "someone", "key": "u/other", "action": "wait"}, feed)
    with pytest.raises(ValueError, match="syncing"):
        triage.set({"login": "", "key": "u/other", "action": "wait"}, {**feed, "login": None})
    with pytest.raises(ValueError, match="no longer listed"):
        triage.set({"login": "me", "key": "u/missing", "action": "wait"}, feed)
    with pytest.raises(ValueError, match="Supply"):
        triage.set({"login": "me", "key": "u/other", "action": "snooze"}, feed)
    assert triage.set({"login": "me", "key": "u/other", "action": "wait"}, feed) == {
        "login": "me",
        "waiting": ["u/other"],
    }
    feed = attention.collect(prs=prs, now=NOW, triage=triage)
    assert [item["key"] for item in feed["items"]] == ["https://github.com/base/repo/pull/1"]
    [waiting] = feed["waiting"]
    assert waiting["key"] == "u/other" and waiting["waiting_since"] == pytest.approx(
        waiting["waiting_since"]
    )
    assert feed["pages"]["prs"] == 1
    # Every device sees the same list: it is saved privately in the state directory.
    saved = attention.Triage(tmp_path)
    assert (tmp_path / attention.TRIAGE_FILE).stat().st_mode & 0o777 == 0o600
    assert set(saved.value["me"]) == {"u/other"}
    # New activity on the item brings it back on its own.
    prs["prs"][1]["updated_at"] = "2027-01-16T10:00:00Z"
    feed = attention.collect(prs=prs, now=NOW, triage=saved)
    assert len(feed["items"]) == 2 and feed["waiting"] == []
    assert saved.value["me"] == {}
    # Resume lists it at once.
    saved.set({"login": "me", "key": "u/other", "action": "wait"}, feed)
    assert saved.set({"login": "me", "key": "u/other", "action": "clear"}, feed)["waiting"] == []
    assert len(attention.collect(prs=prs, now=NOW, triage=saved)["items"]) == 2


def test_a_new_reason_brings_a_set_aside_item_back_but_polls_do_not(tmp_path):
    triage = attention.Triage(tmp_path)
    job = watch(status="watching", pending_reviews=1, started_at=NOW - 500, updated_at=NOW)
    feed = attention.collect(watcher={"jobs": [job]}, now=NOW, triage=triage)
    key = feed["items"][0]["key"]
    triage.set({"login": "me", "key": key, "action": "wait"}, {**feed, "login": "me"})
    # Routine polls move `updated_at`; the item stays aside.
    job["updated_at"] = NOW + 120
    feed = attention.collect(
        watcher={"jobs": [job]}, prs={"login": "me", "prs": []}, now=NOW + 120, triage=triage
    )
    assert feed["items"] == [] and len(feed["waiting"]) == 1
    # A blocked repair is a reason it did not have: it is listed again.
    job.update(status="blocked", summary="Needs you")
    feed = attention.collect(
        watcher={"jobs": [job]}, prs={"login": "me", "prs": []}, now=NOW + 121, triage=triage
    )
    assert len(feed["items"]) == 1 and feed["waiting"] == []
    # Losing a reason is not activity.
    triage.set({"login": "me", "key": key, "action": "wait"}, feed)
    job.update(pending_reviews=0)
    feed = attention.collect(
        watcher={"jobs": [job]}, prs={"login": "me", "prs": []}, now=NOW + 122, triage=triage
    )
    assert feed["items"] == [] and len(feed["waiting"]) == 1


def test_set_aside_entries_need_an_activity_time_and_are_forgotten_when_gone(tmp_path):
    triage = attention.Triage(tmp_path)
    job = watch(status="blocked", summary="S", started_at=None)
    feed = {**attention.collect(watcher={"jobs": [job]}, now=NOW), "login": "me"}
    assert feed["items"][0]["since"] is None
    with pytest.raises(ValueError, match="activity time"):
        triage.set({"login": "me", "key": feed["items"][0]["key"], "action": "wait"}, feed)
    prs = {"login": "me", "prs": [pr(ci="FAILURE")]}
    feed = attention.collect(prs=prs, now=NOW, triage=triage)
    key = feed["items"][0]["key"]
    triage.set({"login": "me", "key": key, "action": "wait"}, feed)
    # The pull request merged: nothing lists it, and the entry lingers for a month.
    attention.collect(prs={"login": "me", "prs": []}, now=NOW + 10 * 86400, triage=triage)
    assert key in triage.value["me"]
    attention.collect(prs={"login": "me", "prs": []}, now=NOW + 31 * 86400, triage=triage)
    assert triage.value["me"] == {}


def test_corrupt_triage_state_is_reported_not_overwritten(tmp_path):
    (tmp_path / attention.TRIAGE_FILE).write_text("[]")
    triage = attention.Triage(tmp_path)
    assert triage.error
    feed = attention.collect(prs={"login": "me", "prs": [pr(ci="FAILURE")]}, now=NOW, triage=triage)
    assert len(feed["items"]) == 1
    with pytest.raises(ValueError, match="repair"):
        triage.set({"login": "me", "key": feed["items"][0]["key"], "action": "wait"}, feed)
    assert (tmp_path / attention.TRIAGE_FILE).read_text() == "[]"
