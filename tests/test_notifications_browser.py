"""Notification behavior against isolated dashboard fixtures, with no live GitHub."""

import copy
import json

import pytest
from playwright.sync_api import Page, expect
from test_dashboard_browser import dashboard_site as dashboard_site

pytestmark = pytest.mark.browser


@pytest.fixture
def inbox(page: Page, dashboard_site):
    url, _ = dashboard_site
    packets = {
        name: page.request.get(f"{url}/api/{name}").json() for name in ("prs", "issues", "status")
    }
    watch = packets["status"]["jobs"][0]
    watch.update(url=packets["prs"]["prs"][0]["url"], repo="test/alpha", number=8)
    packets["status"]["jobs"] = [watch]
    for name in packets:
        page.route(
            f"**/api/{name}",
            lambda route: route.fulfill(json=packets[route.request.url.rsplit("/", 1)[1]]),
        )
    return url, packets


def refresh(page):
    # Wait for both overview requests, including the chained issue refresh.
    with page.expect_response("**/api/issues"):
        page.get_by_role("button", name="Refresh", exact=True).click()


def count(page, value):
    expect(page.locator("#issue-count")).to_have_text("2 / 2")
    expect(page.locator("#notifications-count")).to_have_text(str(value))
    if not value:
        expect(page.locator("#notifications-count")).to_be_hidden()


def test_notifications_group_sources_and_keep_unread_until_acknowledged(page, inbox):
    url, packets = inbox
    page.goto(url)
    count(page, 0)
    packets["prs"]["prs"][0]["ci"] = "SUCCESS"
    packets["prs"]["synced_at"] += 1
    refresh(page)
    count(page, 1)
    packets["status"]["jobs"][0].update(status="blocked", summary="Operator input needed")
    packets["status"]["jobs"][0]["updated_at"] += 1
    refresh(page)
    count(page, 1)
    page.locator("#notifications-toggle").click()
    count(page, 1)
    entries = page.locator("#notifications-list a[href='https://github.com/test/alpha/pull/8']")
    expect(entries).to_have_count(1)
    expect(entries).to_contain_text("CI changed")
    expect(entries).to_contain_text("Watch status changed")
    page.get_by_role("button", name="Mark all seen", exact=True).click()
    count(page, 0)
    page.keyboard.press("Escape")
    expect(page.locator("#notifications-toggle")).to_be_focused()
    refresh(page)
    count(page, 0)
    packets["prs"]["prs"][0]["ci"] = "FAILURE"
    packets["prs"]["synced_at"] += 1
    refresh(page)
    count(page, 1)


def test_notifications_limit_log_to_ten_distinct_items_and_survive_reload(page, inbox):
    url, packets = inbox
    page.goto(url)
    count(page, 0)
    template = packets["prs"]["prs"][0]
    for number in range(100, 112):
        item = copy.deepcopy(template)
        item.update(
            id=f"new-{number}",
            number=number,
            url=f"https://github.com/test/alpha/pull/{number}",
            title=f"Update {number}",
        )
        packets["prs"]["prs"].append(item)
    packets["prs"]["synced_at"] += 1
    refresh(page)
    count(page, 12)
    page.reload()
    count(page, 12)
    packets["prs"]["prs"][-1]["title"] = "Latest update"
    packets["prs"]["synced_at"] += 1
    refresh(page)
    count(page, 12)
    page.locator("#notifications-toggle").click()
    expect(page.locator("#notifications-list li")).to_have_count(10)
    expect(page.locator("#notifications-list li").first).to_contain_text("Latest update")
    assert page.locator("#notifications-list a").evaluate_all(
        "links => new Set(links.map(a => a.href)).size === 10"
    )
    page.get_by_role("button", name="Mark all seen", exact=True).click()
    count(page, 0)
    expect(page.locator("#notifications-list li")).to_have_count(10)


def test_visiting_lists_acknowledges_items_and_issues_remain_distinct(page, inbox):
    url, packets = inbox
    issue = packets["issues"]["issues"][0]
    issue.update(number=8, url="https://github.com/test/alpha/issues/8")
    page.goto(url)
    count(page, 0)
    packets["prs"]["prs"][0]["title"] = "Changed PR"
    issue["title"] = "Changed issue"
    packets["prs"]["synced_at"] += 1
    packets["issues"]["synced_at"] += 1
    refresh(page)
    count(page, 2)
    page.get_by_role("tab", name="Pull requests", exact=True).click()
    count(page, 1)
    page.get_by_role("tab", name="Issues", exact=True).click()
    count(page, 0)


def test_notifications_ignore_stale_and_failed_snapshots_and_quiet_polls(page, inbox):
    url, packets = inbox
    page.goto(url)
    count(page, 0)
    packets["status"]["jobs"][0]["updated_at"] += 10
    packets["status"]["jobs"][0]["next_poll"] = (
        packets["status"]["jobs"][0]["next_poll"] or 0
    ) + 10
    packets["prs"]["synced_at"] += 10
    refresh(page)
    count(page, 0)
    packets["prs"]["synced_at"] -= 20
    packets["prs"]["prs"][0]["title"] = "Stale title"
    refresh(page)
    count(page, 0)
    packets["prs"]["synced_at"] += 30
    packets["prs"]["error"] = "GitHub unavailable"
    refresh(page)
    count(page, 0)


@pytest.mark.parametrize("width", [320, 390, 1280])
def test_notification_layout_keyboard_and_plain_text(page, inbox, width):
    url, packets = inbox
    page.set_viewport_size({"width": width, "height": 844})
    page.goto(url)
    count(page, 0)
    packets["prs"]["prs"][0]["title"] = '<img src=x onerror="window.injected=true"> A changed PR'
    packets["prs"]["synced_at"] += 1
    refresh(page)
    count(page, 1)
    page.locator("#notifications-toggle").focus()
    page.keyboard.press("Enter")
    expect(page.get_by_role("dialog", name="Recent updates")).to_be_visible()
    expect(page.locator("#notifications-list img")).to_have_count(0)
    assert page.evaluate("window.injected === undefined")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert page.locator("#notifications-dialog").evaluate("el => el.scrollWidth <= el.clientWidth")
    page.screenshot(path=f"reports/notifications-{width}.png")
    page.keyboard.press("Escape")
    expect(page.locator("#notifications-toggle")).to_be_focused()


def test_notifications_use_existing_visit_history(page, inbox):
    url, packets = inbox
    pr = packets["prs"]["prs"][0]
    old = {
        field: pr.get(field)
        for field in (
            "repo",
            "title",
            "author",
            "ci",
            "review_decision",
            "draft",
            "head_sha",
            "updated_at",
        )
    }
    old["roles"] = sorted(pr["roles"])
    visit = {"synced_at": packets["prs"]["synced_at"] - 10, "prs": {pr["id"]: old}}
    page.add_init_script(
        f"localStorage.setItem('babysit-pr:seen-prs:v1:fixture', {json.dumps(json.dumps(visit))})"
    )
    pr["title"] = "Changed since last visit"
    page.goto(url)
    # The changed PR and the other PR absent from the old visit are both unseen.
    count(page, 2)
    page.get_by_role("tab", name="Pull requests", exact=True).click()
    count(page, 0)


def test_notifications_isolate_accounts(page, inbox):
    url, packets = inbox
    page.goto(url)
    count(page, 0)
    packets["prs"]["prs"][0]["title"] = "Fixture account update"
    packets["prs"]["synced_at"] += 1
    refresh(page)
    count(page, 1)
    packets["prs"]["login"] = "second-account"
    packets["prs"]["prs"] = []
    packets["status"]["jobs"] = []
    refresh(page)
    count(page, 0)
    page.locator("#notifications-toggle").click()
    expect(page.locator("#notifications-list")).not_to_contain_text("Fixture account update")
    page.keyboard.press("Escape")
    packets["prs"]["login"] = "fixture"
    refresh(page)
    count(page, 1)


def test_notifications_work_when_storage_is_unavailable(page, inbox):
    url, packets = inbox
    page.add_init_script(
        "Object.defineProperty(window, 'localStorage', { get() { throw new Error('Blocked'); } });"
    )
    page.goto(url)
    count(page, 0)
    packets["prs"]["prs"][0]["title"] = "In-memory update"
    packets["prs"]["synced_at"] += 1
    refresh(page)
    count(page, 1)
    page.locator("#notifications-toggle").click()
    expect(page.locator("#notifications-status")).to_contain_text("this tab only")
    page.get_by_role("button", name="Mark all seen", exact=True).click()
    count(page, 0)


def test_opening_one_notification_marks_only_that_item_seen(page, inbox):
    url, packets = inbox
    page.goto(url)
    count(page, 0)
    for item in packets["prs"]["prs"]:
        item["title"] += " changed"
    packets["prs"]["synced_at"] += 1
    refresh(page)
    count(page, 2)
    page.locator("#notifications-toggle").click()
    link = page.locator("#notifications-list a[href='https://github.com/test/alpha/pull/8']")
    # Exercise the click handler without navigating to live GitHub.
    link.evaluate("a => a.addEventListener('click', event => event.preventDefault())")
    link.click()
    count(page, 1)
    expect(page.locator("#notifications-list .notification-unseen")).to_have_count(1)


def test_seen_state_syncs_between_browser_tabs(page, inbox):
    url, packets = inbox
    page.goto(url)
    count(page, 0)
    packets["prs"]["prs"][0]["title"] = "Shared update"
    packets["prs"]["synced_at"] += 1
    refresh(page)
    count(page, 1)
    other = page.context.new_page()
    try:
        for name in packets:
            other.route(
                f"**/api/{name}",
                lambda route: route.fulfill(json=packets[route.request.url.rsplit("/", 1)[1]]),
            )
        other.goto(url)
        count(other, 1)
        other.locator("#notifications-toggle").click()
        other.get_by_role("button", name="Mark all seen", exact=True).click()
        count(other, 0)
        count(page, 0)
    finally:
        other.close()


def test_corrupt_notification_storage_recovers(page, inbox):
    url, packets = inbox
    page.add_init_script(
        "localStorage.setItem('babysit-pr:notifications:v1:fixture', '{bad json');"
    )
    page.goto(url)
    count(page, 0)
    packets["prs"]["prs"][0]["title"] = "Recovered update"
    packets["prs"]["synced_at"] += 1
    refresh(page)
    count(page, 1)
