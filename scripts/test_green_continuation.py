import json
import time

import pr_supervisor as supervisor
import pytest
from test_pr_supervisor import job, release_fixture, snapshot, start_service, wait_until


def green_job():
    j = job()
    s = snapshot()
    s.update(failed_runs=[], failed_jobs=[], new_review_items=[])
    s["checks"].update(failed_count=0, pending_count=0, passed_count=5, all_terminal=True)
    j.update(snapshot=s, on_green="Open the already requested draft PR", on_green_completed=False)
    return j


def test_green_is_opt_in_and_consumed_once_even_after_new_commits():
    j = green_job()
    assert supervisor.actionable(j)
    j.pop("on_green")
    assert not supervisor.actionable(j)
    j["on_green"] = "Open draft"
    j.update(dispatched_keys=supervisor.wake_keys(j), dispatched_reviews=[])
    supervisor.finish_repair(j, {"status": "waiting", "summary": "Draft opened: URL"})
    assert j["on_green_completed"]
    assert j["on_green_result"] == "Draft opened: URL"
    j["snapshot"]["pr"]["head_sha"] = "next"
    assert not supervisor.actionable(j)


@pytest.mark.parametrize(
    "condition", ["empty", "pending", "failed", "closed", "conflict", "review", "failed_job"]
)
def test_continuation_waits_for_success_without_other_work(condition):
    j = green_job()
    if condition == "empty":
        j["snapshot"]["checks"]["passed_count"] = 0
    if condition == "pending":
        j["snapshot"]["checks"].update(pending_count=1, all_terminal=False)
    if condition == "failed":
        j["snapshot"]["checks"]["failed_count"] = 1
    if condition == "closed":
        j["snapshot"]["pr"]["closed"] = True
    if condition == "conflict":
        j["snapshot"]["pr"]["mergeable"] = "CONFLICTING"
    if condition == "review":
        j["pending_reviews"] = [{"kind": "review", "id": "1"}]
    if condition == "failed_job":
        j["snapshot"]["failed_jobs"] = [{"job_id": 1}]
    assert not supervisor.green_ready(j)
    assert not any(key.startswith("green:") for key in supervisor.wake_keys(j))


@pytest.mark.parametrize("outcome", ["blocked", "deferred"])
def test_incomplete_continuation_remains_pending(outcome):
    j = green_job()
    j.update(dispatched_keys=supervisor.wake_keys(j), dispatched_reviews=[])
    supervisor.finish_repair(j, {"status": outcome, "summary": "Wait"})
    assert not j["on_green_completed"]
    assert supervisor.green_ready(j)


def test_configure_preserves_paused_ownership(tmp_path):
    db = supervisor.open_db(tmp_path)
    j = green_job()
    j.update(status="paused", epoch=4)
    with db:
        supervisor.save_job(db, j)
    configured = supervisor.configure_on_green(db, j["id"], "Finish the authorized task")
    assert configured["status"] == "paused"
    assert configured["epoch"] == 5
    assert not configured["dispatch_ready"]
    db.close()


def test_green_resumes_once_and_survives_service_restart(harness):
    h = harness
    state = json.loads(h["state"].read_text())
    state["failed"] = False
    h["state"].write_text(json.dumps(state))
    supervisor.configure_on_green(
        h["db"], h["job"]["id"], "Open the already authorized draft PR; avoid duplicates"
    )
    release_fixture(h)
    proc = start_service(h)
    try:
        wait_until(lambda: supervisor.get_job(h["db"], h["job"]["id"]).get("on_green_completed"))
        calls = [json.loads(line) for line in h["calls"].read_text().splitlines()]
        assert len(calls) == 1
        assert "successful-CI continuation" in calls[0]["prompt"]
        assert "already authorized draft PR" in calls[0]["prompt"]
        proc.terminate()
        proc.wait(timeout=5)
        proc = start_service(h)
        time.sleep(3)
        assert len(h["calls"].read_text().splitlines()) == 1
    finally:
        proc.terminate()
        proc.wait(timeout=5)
