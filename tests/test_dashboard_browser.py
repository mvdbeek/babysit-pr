"""Real Chromium tests: isolated SQLite queue, real HTTP server, no watcher or agents."""

import copy
import json
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import dashboard
import pr_supervisor as supervisor
import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.browser


@pytest.fixture
def dashboard_site(tmp_path: Path) -> Iterator[tuple[str, Path]]:
    db = supervisor.open_db(tmp_path)
    base = {
        "id": "feedback",
        "repo": "test/repo",
        "url": "https://github.com/test/repo/pull/1",
        "cwd": "/fixture/worktree",
        "epoch": 0,
        "attempts": 0,
        "max_repairs": 5,
        "status": "watching",
        "summary": "Feedback awaits approval",
        "branch": None,
        "pending_reviews": [
            {
                "kind": "issue_comment",
                "id": "1",
                "author": "reviewer",
                "body": '<img src=x onerror="window.injected=true"> Fix the assertion.',
                "url": "https://github.com/test/repo/issues/1#issuecomment-1",
            }
        ],
        "snapshot": {
            "pr": {"head_branch": "feature", "number": 1, "merged": False, "closed": False}
        },
    }
    with db:
        supervisor.save_job(db, base)
        running = copy.deepcopy(base)
        running.update(id="running", repo="test/running", status="running", pending_reviews=[])
        supervisor.save_job(db, running)
        closed = copy.deepcopy(base)
        closed.update(
            id="closed", repo="test/merged", status="closed", pending_reviews=[], cleanup_ready=True
        )
        closed["snapshot"]["pr"].update(merged=True, closed=True)
        supervisor.save_job(db, closed)
    db.close()
    (tmp_path / "heartbeat.json").write_text(json.dumps({"time": time.time()}))
    with dashboard.DashboardServer(tmp_path, 0) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}", tmp_path
        finally:
            server.shutdown()
            thread.join(timeout=5)


def read_job(home: Path, key: str) -> dict:
    db = supervisor.open_db(home)
    try:
        return supervisor.get_job(db, key)
    finally:
        db.close()


def open_watch(page: Page, url: str, repo: str) -> None:
    page.goto(url)
    page.get_by_role("button", name=f"{repo}, feature,", exact=False).click()


def test_feedback_is_text_and_requires_an_explicit_click(page: Page, dashboard_site) -> None:
    url, home = dashboard_site
    open_watch(page, url, "test/repo")
    expect(page.locator(".feedback-item")).to_contain_text(
        '<img src=x onerror="window.injected=true">'
    )
    expect(page.locator(".feedback-item img")).to_have_count(0)
    assert page.evaluate("window.injected === undefined")
    assert not read_job(home, "feedback").get("approved_reviews")
    page.get_by_role("button", name="Handle feedback", exact=True).click()
    expect(page.get_by_role("button", name="Feedback queued", exact=True)).to_be_disabled()
    assert len(read_job(home, "feedback")["approved_reviews"]) == 1


def test_stale_feedback_click_is_rejected_without_approving_new_text(
    page: Page, dashboard_site
) -> None:
    url, home = dashboard_site
    open_watch(page, url, "test/repo")
    db = supervisor.open_db(home)
    with db:
        job = supervisor.get_job(db, "feedback")
        job["pending_reviews"][0]["body"] = "A later comment revision"
        supervisor.save_job(db, job)
    db.close()
    page.get_by_role("button", name="Handle feedback", exact=True).click()
    expect(page.locator("#detail [role=alert]")).to_contain_text("Feedback changed")
    assert not read_job(home, "feedback").get("approved_reviews")
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(page.locator(".feedback-item")).to_contain_text("A later comment revision")


def test_cancel_keeps_history_and_running_repairs_finish_first(page: Page, dashboard_site) -> None:
    url, home = dashboard_site
    open_watch(page, url, "test/repo")
    page.get_by_role("button", name="Cancel watch", exact=True).click()
    expect(page.locator("#ended")).to_have_text("2")
    page.locator("#show-ended").click()
    expect(page.locator("#list")).to_contain_text("test/repo")
    assert read_job(home, "feedback")["status"] == "stopped"
    page.get_by_label("Filter watches").select_option("active")
    page.get_by_role("button", name="test/running, feature,", exact=False).click()
    page.get_by_role("button", name="Cancel watch", exact=True).click()
    expect(page.get_by_role("button", name="Cancellation pending", exact=True)).to_be_disabled()
    job = read_job(home, "running")
    assert job["status"] == "running" and job["stop_after_run"]


def test_mobile_cleanup_attention_and_offline_state(page: Page, dashboard_site) -> None:
    url, home = dashboard_site
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(url)
    page.locator("#show-attention").click()
    page.get_by_role("button", name="test/merged, feature,", exact=False).click()
    expect(page.locator(".cleanup-note")).to_contain_text("PR merged · ready for cleanup")
    expect(page.get_by_role("button", name="Handle feedback", exact=True)).to_have_count(0)
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    (home / "heartbeat.json").unlink()
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(page.locator("#health")).to_have_text("Watcher offline")
