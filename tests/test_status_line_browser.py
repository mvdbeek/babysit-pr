"""Overview rows carry one status line, and compact rows fold the other columns away."""

import pytest
from playwright.sync_api import Page, expect
from test_dashboard_browser import dashboard_site as dashboard_site  # noqa: F401 - fixture
from test_notifications_browser import inbox as inbox  # noqa: F401 - fixture

pytestmark = pytest.mark.browser


def test_rows_show_watch_and_agent_state_on_one_line(page: Page, inbox) -> None:
    url, packets = inbox
    # The inbox fixture binds the newest watch, a closed one, to this pull request.
    packets["status"]["jobs"][0]["status"] = "watching"
    page.goto(url + "/#prs")
    row = page.locator("#pr-list tr").filter(has_text="Test PR")
    line = row.locator(".pr-status-line")
    # The watch on this pull request is shown beside the title; CI and review state are
    # columns of their own, so their chips stay for compact rows.
    expect(line.locator(".badge:visible")).to_have_text(["Watch: Watching"])
    packets["status"]["jobs"][0]["status"] = "blocked"
    # The watcher poll, every 5 seconds, redraws the rows whose watch changed.
    page.evaluate("refresh(true)")
    expect(line.locator(".badge:visible")).to_have_text(["Watch: Blocked"])
    expect(line.locator(".badge:visible")).to_have_class(["badge red"])
    # Issues show the same line; this one has nothing running against it.
    page.get_by_role("tab", name="Issues", exact=True).click()
    issue = page.locator("#issue-list tr").filter(has_text="Assigned issue")
    expect(issue.locator(".pr-status-line .badge:visible")).to_have_count(0)


def test_compact_rows_fold_columns_and_persist(page: Page, dashboard_site) -> None:
    url, _ = dashboard_site
    page.goto(url + "/#prs")
    toggle = page.locator("#pr-compact")
    expect(toggle).to_have_attribute("aria-pressed", "false")
    row = page.locator("#pr-list tr").filter(has_text="Test PR")
    expect(row.locator(".pr-author")).to_be_visible()
    expect(row.locator(".pr-status-line .badge:visible")).to_have_count(0)
    toggle.click()
    expect(toggle).to_have_attribute("aria-pressed", "true")
    expect(row.locator(".pr-author")).to_be_hidden()
    expect(row.locator(".pr-readiness")).to_be_hidden()
    expect(row.locator(".pr-dates")).to_be_hidden()
    # Pin, repository, pull request and actions remain.
    expect(page.locator("#prs-panel th:visible")).to_have_count(4)
    expect(row.locator(".pr-status-line .badge:visible")).to_have_text(
        ["CI failed", "Ready for review"]
    )
    expect(row.locator(".pr-repo")).to_be_visible()
    expect(row.locator(".workspace-actions")).to_be_visible()
    # One choice for both overviews, kept across reloads.
    page.get_by_role("tab", name="Issues", exact=True).click()
    expect(page.locator("#issue-compact")).to_have_attribute("aria-pressed", "true")
    expect(page.locator("#issue-list tr").first.locator(".pr-author")).to_be_hidden()
    page.reload()
    expect(page.locator("#issue-compact")).to_have_attribute("aria-pressed", "true")
    page.locator("#issue-compact").click()
    expect(page.locator("#issue-list tr").first.locator(".pr-author")).to_be_visible()
    page.get_by_role("tab", name="Pull requests", exact=True).click()
    expect(row.locator(".pr-author")).to_be_visible()
