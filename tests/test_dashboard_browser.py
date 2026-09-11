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
from pr_overview import Overview

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
    overview = Overview(tmp_path)
    overview.next_poll = float("inf")  # Browser fixtures never call real GitHub.
    overview.value.update(
        login="fixture",
        synced_at=time.time(),
        prs=[
            {
                "id": "pr-one",
                "repo": "test/alpha",
                "number": 8,
                "title": '<img src=x onerror="window.injected=true"> Test PR',
                "url": "https://github.com/test/alpha/pull/8",
                "author": "fixture",
                "roles": ["author", "reviewer"],
                "ci": "FAILURE",
                "draft": False,
                "updated_at": "2026-09-11T10:00:00Z",
                "opened_at": "2025-01-01T10:00:00Z",
            },
            {
                "id": "pr-two",
                "repo": "test/beta",
                "number": 9,
                "title": "Assigned PR",
                "url": "https://github.com/test/beta/pull/9",
                "author": "colleague",
                "roles": ["assignee"],
                "ci": "SUCCESS",
                "draft": True,
                "updated_at": "2026-09-10T10:00:00Z",
                "opened_at": "2026-08-01T10:00:00Z",
            },
        ],
    )
    with dashboard.DashboardServer(tmp_path, 0, overview=overview) as server:
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


def test_pr_overview_filters_ci_roles_times_and_safe_titles(page: Page, dashboard_site) -> None:
    url, home = dashboard_site
    page.goto(url)
    page.get_by_role("tab", name="Pull requests", exact=True).click()
    rows = page.locator("#pr-list tr")
    expect(rows).to_have_count(2)
    expect(page.get_by_role("columnheader", name="Repository", exact=True)).to_be_visible()
    expect(rows.first.locator("td").nth(0)).to_have_text("test/alpha")
    expect(rows.first.locator("td").nth(1)).to_contain_text("#8")
    expect(rows.first.locator("img")).to_have_count(0)
    assert page.evaluate("window.injected === undefined")
    expect(rows.first.locator(".pr-updated time")).to_have_attribute(
        "datetime", "2026-09-11T10:00:00Z"
    )
    expect(page.locator("#pr-sync")).to_contain_text("@fixture")
    expect(page.locator("#pr-sync")).to_contain_text("Synced")
    expect(page.get_by_role("link", name="CI Failed for test/alpha #8")).to_have_attribute(
        "href", "https://github.com/test/alpha/pull/8/checks"
    )
    page.get_by_label("Filter pull requests by role").select_option("reviewer")
    expect(rows).to_have_count(1)
    page.get_by_label("Filter pull requests by role").select_option("assignee")
    expect(rows).to_contain_text(["Assigned PR"])
    page.get_by_label("Filter pull requests by role").select_option("all")
    page.get_by_label("Filter pull requests by CI").select_option("FAILURE")
    expect(rows).to_have_count(1)
    page.get_by_label("Filter pull requests by CI").select_option("all")
    page.get_by_label("Search pull requests").fill("colleague")
    expect(rows).to_contain_text(["Assigned PR"])
    page.get_by_label("Search pull requests").fill("missing")
    expect(page.locator("#pr-empty")).to_have_text("No matching pull requests.")
    assert len(dashboard.read_jobs(home)) == 3  # Discovery creates no repair watches.
    page.set_viewport_size({"width": 390, "height": 844})
    page.get_by_label("Search pull requests").fill("")
    expect(rows).to_have_count(2)
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


def test_pr_sync_failure_and_empty_state_are_distinct(page: Page, dashboard_site) -> None:
    url, _ = dashboard_site
    page.route(
        "**/api/prs",
        lambda route: route.fulfill(
            json={
                "prs": [],
                "synced_at": None,
                "error": "GitHub authentication failed",
            }
        ),
    )
    page.goto(url)
    page.get_by_role("tab", name="Pull requests", exact=True).click()
    expect(page.locator("#pr-alert")).to_contain_text("GitHub authentication failed")
    expect(page.locator("#pr-empty")).to_contain_text("PR data is unavailable")
    page.unroute("**/api/prs")
    page.route(
        "**/api/prs",
        lambda route: route.fulfill(
            json={
                "prs": [],
                "synced_at": 1234,
                "error": None,
            }
        ),
    )
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(page.locator("#pr-empty")).to_have_text("No open pull requests for your roles.")
    expect(page.locator("#pr-alert")).to_be_hidden()


def test_dashboard_tabs_default_to_watcher_and_support_navigation(
    page: Page, dashboard_site
) -> None:
    url, _ = dashboard_site
    page.goto(url)
    watcher = page.get_by_role("tab", name="Watcher", exact=True)
    prs = page.get_by_role("tab", name="Pull requests", exact=True)
    expect(watcher).to_have_attribute("aria-selected", "true")
    expect(page.locator("#watcher-panel")).to_be_visible()
    expect(page.locator("#prs-panel")).to_be_hidden()
    expect(page.locator(".page-tabs [role=tab]").first).to_have_text("Watcher")
    watcher.focus()
    watcher.press("ArrowRight")
    expect(prs).to_be_focused()
    expect(prs).to_have_attribute("aria-selected", "true")
    expect(page.locator("#watcher-panel")).to_be_hidden()
    expect(page.locator("#prs-panel")).to_be_visible()
    expect(page).to_have_url(url + "/#prs")
    page.get_by_label("Search pull requests").fill("alpha")
    watcher.click()
    page.go_back()
    expect(prs).to_have_attribute("aria-selected", "true")
    expect(page.get_by_label("Search pull requests")).to_have_value("alpha")
    page.reload()
    expect(page.locator("#prs-panel")).to_be_visible()
    prs.focus()
    prs.press("Home")
    expect(watcher).to_be_focused()
    expect(page.locator("#watcher-panel")).to_be_visible()


@pytest.mark.parametrize(
    "column,first",
    [
        ("Repository", "test/alpha"),
        ("Pull request", "test/alpha"),
        ("Author", "test/beta"),
        ("Review status", "test/beta"),
        ("Your role", "test/beta"),
        ("CI", "test/alpha"),
        ("Opened", "test/beta"),
        ("Last updated", "test/beta"),
    ],
)
def test_each_pr_column_sorts_both_directions(page: Page, dashboard_site, column, first) -> None:
    url, _ = dashboard_site
    page.goto(url + "/#prs")
    rows = page.locator("#pr-list tr")
    expect(rows).to_have_count(2)
    button = page.get_by_role("button", name=f"Sort by {column}", exact=True)
    button.click()
    expect(rows.first.locator(".pr-repo")).to_have_text(first)
    button.click()
    expect(rows.first.locator(".pr-repo")).to_have_text(
        "test/beta" if first == "test/alpha" else "test/alpha"
    )
    expect(page.get_by_role("columnheader", name=column, exact=True)).to_have_attribute(
        "aria-sort",
        "descending"
        if column not in {"Opened", "Last updated"}
        else "ascending"
        if column == "Opened"
        else "descending",
    )


def test_pr_metadata_and_mobile_sort_survive_refresh_and_filtering(
    page: Page, dashboard_site
) -> None:
    url, _ = dashboard_site
    page.goto(url + "/#prs")
    expect(page.locator(".pr-author").first).to_have_text("fixture")
    expect(page.locator(".pr-readiness").first).to_have_text("Ready for review")
    expect(page.locator(".pr-readiness").last).to_have_text("Draft")
    expect(page.locator(".pr-opened time").first).to_have_attribute(
        "datetime", "2025-01-01T10:00:00Z"
    )
    page.set_viewport_size({"width": 390, "height": 844})
    page.get_by_label("Sort by", exact=True).select_option("author")
    expect(page.locator(".pr-author").first).to_have_text("colleague")
    page.get_by_role("button", name="Ascending", exact=True).click()
    expect(page.locator(".pr-author").first).to_have_text("fixture")
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(page.locator(".pr-author").first).to_have_text("fixture")
    page.get_by_label("Search pull requests").fill("colleague")
    expect(page.locator("#pr-list tr")).to_have_count(1)
    page.get_by_label("Search pull requests").fill("")
    expect(page.locator(".pr-author").first).to_have_text("fixture")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
