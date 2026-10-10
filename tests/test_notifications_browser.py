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
    page.goto(url + "/#watcher")
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
    entries = page.locator("#notifications-list a[href*='test%2Falpha%2Fpull%2F8']")
    expect(entries).to_have_count(1)
    expect(entries).to_have_attribute(
        "href", "/?item=https%3A%2F%2Fgithub.com%2Ftest%2Falpha%2Fpull%2F8#watcher"
    )
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
    page.goto(url + "/#watcher")
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
    page.goto(url + "/#watcher")
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
    page.goto(url + "/#watcher")
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
    page.goto(url + "/#watcher")
    count(page, 0)
    packets["prs"]["prs"][0]["title"] = '<img src=x onerror="window.injected=true"> A changed PR'
    packets["prs"]["synced_at"] += 1
    refresh(page)
    count(page, 1)
    page.locator("#notifications-toggle").focus()
    page.keyboard.press("Enter")
    expect(page.get_by_role("dialog", name="Recent updates")).to_be_visible()
    assert (
        page.locator("#notifications-seen").bounding_box()["y"]
        < page.locator("#notifications-list").bounding_box()["y"]
    )
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
    page.goto(url + "/#watcher")
    # The changed PR and the other PR absent from the old visit are both unseen.
    count(page, 2)
    page.get_by_role("tab", name="Pull requests", exact=True).click()
    count(page, 0)


def test_notifications_isolate_accounts(page, inbox):
    url, packets = inbox
    page.goto(url + "/#watcher")
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
    page.goto(url + "/#watcher")
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
    page.goto(url + "/#watcher")
    count(page, 0)
    packets["prs"]["prs"][0]["title"] += " changed"
    packets["issues"]["issues"][0]["title"] += " changed"
    packets["prs"]["synced_at"] += 1
    packets["issues"]["synced_at"] += 1
    refresh(page)
    count(page, 2)
    page.locator("#notifications-toggle").click()
    link = page.locator("#notifications-list a[href*='test%2Falpha%2Fpull%2F8']")
    link.click()
    count(page, 1)
    expect(page.locator("#notifications-dialog")).to_be_hidden()
    expect(page.get_by_role("tab", name="Pull requests", exact=True)).to_have_attribute(
        "aria-selected", "true"
    )
    expect(page.locator("#pr-list tr.notification-target")).to_be_focused()
    page.locator("#notifications-toggle").click()
    expect(page.locator("#notifications-list .notification-unseen")).to_have_count(1)


@pytest.mark.parametrize("source", ["prs", "issues", "watcher"])
def test_notification_deep_link_reveals_source_after_reload(page, inbox, source):
    from urllib.parse import quote

    url, packets = inbox
    item = packets["status"]["jobs"][0] if source == "watcher" else packets[source][source][0]
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(f"{url}/?item={quote(item['url'], safe='')}#{source}")
    panel = "Pull requests" if source == "prs" else "Issues" if source == "issues" else "Watcher"
    expect(page.get_by_role("tab", name=panel, exact=True)).to_have_attribute(
        "aria-selected", "true"
    )
    target = page.locator(
        "#detail"
        if source == "watcher"
        else f"#{'pr' if source == 'prs' else 'issue'}-list tr.notification-target"
    )
    expect(target).to_be_focused()
    expect(page.locator("#navigation-status")).to_be_hidden()
    assert page.url.startswith(url)


def test_notification_target_overrides_search_and_missing_item_is_explained(page, inbox):
    url, _ = inbox
    page.goto(url + "/#prs")
    expect(page.locator("#pr-list tr")).to_have_count(2)
    page.locator("#pr-search").fill("no matching PR")
    expect(page.locator("#pr-list tr")).to_have_count(0)
    page.evaluate(
        "dashboardNavigation.open('/?item=https%3A%2F%2Fgithub.com%2Ftest%2Falpha%2Fpull%2F8#prs')"
    )
    expect(page.locator("#pr-search")).to_have_value("")
    expect(page.locator("#pr-list tr.notification-target")).to_be_focused()
    page.evaluate(
        "dashboardNavigation.open('/?item=https%3A%2F%2Fgithub.com%2Ftest%2Falpha%2Fpull%2F999#prs')"
    )
    expect(page.locator("#navigation-status")).to_contain_text("no longer")


def test_notification_reveals_item_beyond_first_page(page, inbox):
    from urllib.parse import quote

    url, packets = inbox
    template = packets["prs"]["prs"][0]
    packets["prs"]["prs"] = [
        {
            **template,
            "id": f"pr-{i}",
            "number": i,
            "title": f"PR {i}",
            "url": f"https://github.com/test/alpha/pull/{i}",
        }
        for i in range(120)
    ]
    target = packets["prs"]["prs"][-1]
    page.goto(f"{url}/?item={quote(target['url'], safe='')}#prs")
    row = page.locator("#pr-list tr.notification-target")
    expect(row).to_contain_text("PR 119")
    expect(row).to_be_focused()


def test_seen_state_syncs_between_browser_tabs(page, inbox):
    url, packets = inbox
    page.goto(url + "/#watcher")
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
    page.goto(url + "/#watcher")
    count(page, 0)
    packets["prs"]["prs"][0]["title"] = "Recovered update"
    packets["prs"]["synced_at"] += 1
    refresh(page)
    count(page, 1)


def test_silence_button_persists_and_mutes_watcher_and_pr_updates(page, inbox):
    url, packets = inbox
    preferences = {"login": "fixture", "silenced": []}
    page.route("**/api/notification-preferences", lambda route: route.fulfill(json=preferences))

    def toggle(route):
        request = route.request.post_data_json
        assert request["login"] == "fixture"
        preferences["silenced"] = [request["url"]] if request["silenced"] else []
        route.fulfill(json=preferences)

    page.route("**/api/notification-silence", toggle)
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(url + "/#prs")
    button = page.get_by_role("button", name="Silence notifications for test/alpha #8", exact=True)
    button.click()
    button = page.get_by_role(
        "button", name="Unsilence notifications for test/alpha #8", exact=True
    )
    expect(button).to_have_attribute("aria-pressed", "true")
    expect(button).to_be_focused()
    page.screenshot(path="reports/notification-silence-mobile.png")
    page.get_by_role("tab", name="Watcher", exact=True).click()
    packets["prs"]["prs"][0]["title"] = "Muted update"
    packets["prs"]["synced_at"] += 1
    packets["status"]["jobs"][0].update(
        status="blocked", updated_at=packets["status"]["jobs"][0]["updated_at"] + 1
    )
    refresh(page)
    count(page, 0)
    page.reload()
    count(page, 0)
    page.get_by_role("tab", name="Pull requests", exact=True).click()
    button.click()
    expect(
        page.get_by_role("button", name="Silence notifications for test/alpha #8", exact=True)
    ).to_have_attribute("aria-pressed", "false")
    page.get_by_role("tab", name="Watcher", exact=True).click()
    refresh(page)
    count(page, 0)
    packets["prs"]["prs"][0]["title"] = "Notify again"
    packets["prs"]["synced_at"] += 1
    refresh(page)
    count(page, 1)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


def test_own_actions_and_progress_are_quiet_but_outcomes_notify(page, inbox):
    url, packets = inbox
    page.goto(url + "/#watcher")
    count(page, 0)
    job = packets["status"]["jobs"][0]
    before = job["status"]
    job.update(status="stopped", updated_at=job["updated_at"] + 1)
    job["notification_actions"] = [
        {"at": job["updated_at"], "before": {"status": before}, "after": {"status": "stopped"}}
    ]
    refresh(page)
    count(page, 0)
    job.update(
        status="running",
        attempts=job.get("attempts", 0) + 1,
        updated_at=job["updated_at"] + 1,
        check_details=[{"name": "a", "bucket": "pass"}, {"name": "b", "bucket": "pending"}],
        checks_result="PENDING",
    )
    packets["prs"]["prs"][0]["ci"] = "PENDING"
    packets["prs"]["synced_at"] += 1
    refresh(page)
    count(page, 0)
    job.update(
        status="blocked",
        updated_at=job["updated_at"] + 1,
        check_details=[{"name": "a", "bucket": "pass"}, {"name": "b", "bucket": "fail"}],
        checks_result="FAILURE",
    )
    refresh(page)
    count(page, 1)


def test_repair_completed_between_browser_polls_notifies(page, inbox):
    url, packets = inbox
    page.goto(url + "/#watcher")
    count(page, 0)
    job = packets["status"]["jobs"][0]
    job.update(attempts=job.get("attempts", 0) + 1, updated_at=job["updated_at"] + 1)
    refresh(page)
    count(page, 1)


def test_review_thread_count_added_after_baselines_is_not_a_change(page, inbox):
    url, packets = inbox
    # Visit the PR tab once so both notification and visit baselines exist.
    page.goto(url + "/#prs")
    count(page, 0)
    expect(page.locator("#pr-changes")).to_contain_text("from this visit onward")
    page.get_by_role("tab", name="Watcher").click()
    # Rewrite stored notification and visit baselines as they were before the field existed.
    page.evaluate(
        """() => {
          const strip = (value) => {
            if (!value || typeof value !== "object") return;
            delete value.unresolved_threads;
            Object.values(value).forEach(strip);
          };
          for (const key of Object.keys(localStorage)) {
            try {
              const value = JSON.parse(localStorage.getItem(key));
              strip(value);
              localStorage.setItem(key, JSON.stringify(value));
            } catch {}
          }
        }"""
    )
    assert "unresolved_threads" not in json.dumps(
        page.evaluate("() => Object.values(localStorage)")
    )
    first, second = packets["prs"]["prs"]
    first["unresolved_threads"] = 0
    packets["prs"]["synced_at"] += 1
    page.goto(url + "/#watcher")
    count(page, 0)
    second["unresolved_threads"] = 2
    packets["prs"]["synced_at"] += 1
    refresh(page)
    count(page, 1)
    page.locator("#notifications-toggle").click()
    expect(page.locator("#notifications-list")).to_contain_text("Review threads changed")
    page.keyboard.press("Escape")
    page.get_by_role("tab", name="Pull requests").click()
    expect(page.locator(".pr-new")).to_have_count(0)
    expect(page.locator(".pr-changed .pr-change-note")).to_have_text(["Review threads"])
