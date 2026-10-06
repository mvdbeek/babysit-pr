"""Persistent controls with long content on isolated dashboard servers."""

import copy

import pytest
from playwright.sync_api import expect
from test_dashboard_browser import ci_routes as ci_routes
from test_dashboard_browser import dashboard_site as dashboard_site
from test_dashboard_browser import issue_workspace_routes as issue_workspace_routes
from test_dashboard_browser import workspace_routes as workspace_routes
from test_notifications_browser import count, refresh
from test_notifications_browser import inbox as inbox
from test_sentry_browser import row
from test_sentry_browser import site as site

pytestmark = pytest.mark.browser


def reachable(control):
    expect(control).to_be_in_viewport(ratio=1)
    assert control.evaluate("""el => {
        const r = el.getBoundingClientRect();
        return el.contains(document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2));
    }""")


def scroll_dialog(dialog, controls):
    for fraction in (0.5, 1):
        dialog.evaluate("(el, f) => { el.scrollTop = el.scrollHeight * f; }", fraction)
        assert dialog.evaluate("el => el.scrollTop > 100")
        assert dialog.evaluate("el => el.scrollWidth <= el.clientWidth")
        for control in controls:
            reachable(control)


@pytest.mark.parametrize("width", [320, 390, 1280])
def test_ci_header_stays_reachable(page, dashboard_site, ci_routes, width):
    url, _ = dashboard_site
    checks, _, _ = ci_routes
    template = checks["value"]["checks"][0]
    checks["value"]["checks"] = [
        {**template, "id": str(i), "name": f"Test job {i}"} for i in range(40)
    ]
    page.set_viewport_size({"width": width, "height": 844})
    page.goto(url + "/#prs")
    page.get_by_role("link", name="CI Failed for test/alpha #8", exact=True).click()
    dialog = page.locator("#ci-dialog")
    expect(dialog.locator(".ci-item")).to_have_count(40)
    close = dialog.get_by_role("button", name="Close CI details")
    link = dialog.get_by_role("link", name="All checks on GitHub")
    scroll_dialog(dialog, [close, link])
    expect(link).to_have_attribute("href", "https://github.com/test/alpha/pull/8/checks")
    page.screenshot(path=f"reports/sticky-ci-{width}.png")
    close.click()
    expect(dialog).to_be_hidden()


@pytest.mark.parametrize("width", [320, 390, 1280])
def test_workspace_header_stays_reachable(page, dashboard_site, workspace_routes, width):
    url, _ = dashboard_site
    info, _, requests = workspace_routes
    template = info["matches"][0]
    info["matches"] = [
        {**template, "path": f"/fixture/checkout-{i}", "name": f"Checkout {i}"} for i in range(30)
    ]
    page.set_viewport_size({"width": width, "height": 844})
    page.goto(url + "/#prs")
    page.locator("#pr-list tr").first.get_by_role(
        "button", name="Open workspace", exact=True
    ).click()
    dialog = page.locator("#workspace-dialog")
    expect(dialog.get_by_role("button", name="Open workspace", exact=True)).to_have_count(30)
    close = dialog.get_by_role("button", name="Close workspace actions")
    scroll_dialog(dialog, [close, dialog.locator("#workspace-title")])
    page.screenshot(path=f"reports/sticky-workspace-{width}.png")
    close.click()
    expect(dialog).to_be_hidden()
    assert not requests


@pytest.mark.parametrize("width", [320, 390, 1280])
def test_publish_header_stays_reachable(page, site, width):
    url, fake = site
    fake.sanitized = True
    fake.findings = [{"kind": "email", "excerpt": f"reporter-{i}@example.org"} for i in range(50)]
    page.set_viewport_size({"width": width, "height": 844})
    page.goto(url + "/#sentry")
    row(page, "GALAXY-MAIN-1A").get_by_role("button", name="Publish to GitHub").click()
    dialog = page.locator("#sentry-publish-dialog")
    expect(dialog.locator("#sentry-publish-findings")).to_contain_text("reporter-49@example.org")
    close = dialog.get_by_role("button", name="Close GitHub issue draft")
    scroll_dialog(dialog, [close, dialog.locator("#sentry-publish-heading")])
    expect(dialog.get_by_role("button", name="Create GitHub issue")).to_be_disabled()
    page.screenshot(path=f"reports/sticky-publish-{width}.png")
    close.click()
    expect(dialog).to_be_hidden()
    assert [r["action"] for r in fake.requests] == ["publish-draft"]


@pytest.mark.parametrize("width", [320, 390, 1280])
def test_notification_controls_stay_reachable(page, inbox, width):
    url, packets = inbox
    page.set_viewport_size({"width": width, "height": 844})
    page.goto(url)
    count(page, 0)
    template = packets["prs"]["prs"][0]
    for number in range(100, 110):
        packets["prs"]["prs"].append(
            {
                **template,
                "id": f"new-{number}",
                "number": number,
                "url": f"https://github.com/test/alpha/pull/{number}",
                "title": f"Update {number}: a long title describing the pull request",
            }
        )
    packets["prs"]["synced_at"] += 1
    refresh(page)
    count(page, 10)
    page.locator("#notifications-toggle").click()
    dialog = page.locator("#notifications-dialog")
    expect(dialog.locator("li")).to_have_count(10)
    close = dialog.get_by_role("button", name="Close notifications")
    seen = dialog.get_by_role("button", name="Mark all seen")
    scroll_dialog(dialog, [close, seen])
    page.screenshot(path=f"reports/sticky-notifications-{width}.png")
    seen.click()
    count(page, 0)
    reachable(close)
    close.click()
    expect(dialog).to_be_hidden()


@pytest.mark.parametrize("kind,prefix", [("prs", "pr"), ("issues", "issue")])
@pytest.mark.parametrize("width", [320, 390])
def test_mobile_selection_controls_stay_reachable(
    page, dashboard_site, issue_workspace_routes, kind, prefix, width
):
    url, _ = dashboard_site
    info, snapshot, _ = issue_workspace_routes
    packet = page.request.get(f"{url}/api/{kind}").json()
    template = packet[kind][0]
    packet[kind] = [
        {
            **template,
            "id": f"item-{i}",
            "number": i,
            "title": f"Item {i}",
            "url": f"https://github.com/test/alpha/{'pull' if kind == 'prs' else 'issues'}/{i}",
        }
        for i in range(30)
    ]
    snapshot[kind] = {item["id"]: copy.deepcopy(info) for item in packet[kind]}
    page.route(f"**/api/{kind}", lambda route: route.fulfill(json=packet))
    page.set_viewport_size({"width": width, "height": 844})
    page.goto(f"{url}/#{kind}")
    rows = page.locator(f"#{prefix}-list tr")
    expect(rows).to_have_count(30)
    bar = page.locator(f"#{prefix}-batch")
    expect(bar).to_be_hidden()
    # The first selection is far down the page, after the bar has scrolled offscreen.
    rows.last.locator(".item-select").check()
    expect(bar).to_contain_text("1 selected")
    assert page.evaluate("scrollY > 1000")
    for control in bar.locator("button").all():
        reachable(control)
    reachable(page.locator(f"#{prefix}-batch-count"))
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path=f"reports/sticky-{kind}-selection-{width}.png")
    bar.get_by_role("button", name="Handle selected…").click()
    dialog = page.locator("#workspace-dialog")
    noun = "pull request" if kind == "prs" else "issue"
    expect(dialog.locator("#workspace-title")).to_have_text(f"Handle 1 {noun}")
    dialog.get_by_role("button", name="Close workspace actions").click()
    bar.get_by_role("button", name="Clear", exact=True).click()
    expect(bar).to_be_hidden()
