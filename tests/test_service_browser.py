"""The Services dialog behind the health badge, with stubbed service endpoints."""

import json
import time

import pytest
from playwright.sync_api import expect
from test_dashboard_browser import dashboard_site as dashboard_site

pytestmark = pytest.mark.browser


@pytest.fixture
def services(page):
    now = time.time()
    state = {
        "daemon": {
            "health": "healthy",
            "heartbeat_age": 0.5,
            "max_workers": 2,
            "pid": 4321,
            "started_at": now - 7200,
        },
        "dashboard": {"pid": 9876, "started_at": now - 120, "service": "net.example.dashboard"},
    }
    posted = []

    def reply(route):
        if route.request.method != "POST":
            route.fulfill(json=state)
            return
        posted.append((route.request.headers, json.loads(route.request.post_data)))
        if json.loads(route.request.post_data)["service"] == "daemon":
            state["daemon"] = {**state["daemon"], "pid": 5555, "started_at": time.time()}
            route.fulfill(json={"daemon": state["daemon"]})
            return
        route.fulfill(json={"dashboard": state["dashboard"]})

    page.route("**/api/service", reply)
    page.route("**/api/service-restart", reply)
    return state, posted


def open_services(page):
    page.locator("#health").click()
    dialog = page.locator("#service-dialog")
    expect(dialog).to_be_visible()
    return dialog


def test_the_health_badge_opens_both_services(page, dashboard_site, services):
    page.goto(dashboard_site[0])
    dialog = open_services(page)
    watcher = dialog.get_by_role("listitem").filter(has_text="Watcher daemon")
    expect(watcher).to_contain_text("Online")
    expect(watcher).to_contain_text("pid 4321 · started 2h ago · 2 repairs at a time")
    expect(watcher).to_contain_text("Repairs already running keep going")
    dashboard = dialog.get_by_role("listitem").filter(has_text="Dashboard")
    expect(dashboard).to_contain_text("pid 9876 · started 2m ago · net.example.dashboard starts it")


def test_a_stopped_watcher_still_offers_a_restart(page, dashboard_site, services):
    state, _ = services
    state["daemon"] = {
        "health": "offline",
        "heartbeat_age": None,
        "max_workers": None,
        "pid": None,
        "started_at": None,
    }
    page.goto(dashboard_site[0])
    dialog = open_services(page)
    watcher = dialog.get_by_role("listitem").filter(has_text="Watcher daemon")
    expect(watcher).to_contain_text("Offline")
    expect(watcher).to_contain_text("no pid recorded")
    expect(watcher.get_by_role("button", name="Restart the watcher daemon")).to_be_enabled()


def test_restarting_the_watcher_shows_the_process_that_replaced_it(page, dashboard_site, services):
    _, posted = services
    page.goto(dashboard_site[0])
    dialog = open_services(page)
    watcher = dialog.get_by_role("listitem").filter(has_text="Watcher daemon")
    watcher.get_by_role("button", name="Restart the watcher daemon").click()
    expect(dialog.locator("#service-status")).to_have_text(
        "The watcher restarted. Repairs already running kept going."
    )
    expect(watcher).to_contain_text("pid 5555 · started just now")
    [(headers, body)] = posted
    assert headers["x-babysit-action"] == "service-restart"
    assert headers["content-type"] == "application/json"
    assert body == {"service": "daemon"}


def test_a_dashboard_nothing_would_restart_says_so(page, dashboard_site, services):
    state, posted = services
    state["dashboard"] = {**state["dashboard"], "service": None}
    page.goto(dashboard_site[0])
    dialog = open_services(page)
    dashboard = dialog.get_by_role("listitem").filter(has_text="Dashboard")
    expect(dashboard).to_contain_text("started by hand")
    expect(dashboard).to_contain_text("Nothing would start this dashboard again")
    expect(dashboard.get_by_role("button", name="Restart the dashboard")).to_be_disabled()
    assert posted == []


def test_restarting_the_dashboard_waits_for_the_new_one_and_reloads(page, dashboard_site, services):
    state, posted = services
    page.goto(dashboard_site[0])
    page.evaluate("window.beforeRestart = true")
    dialog = open_services(page)
    dashboard = dialog.get_by_role("listitem").filter(has_text="Dashboard")
    dashboard.get_by_role("button", name="Restart the dashboard").click()
    expect(dialog.locator("#service-status")).to_have_text("Restarting the dashboard…")
    # While the same process answers, the page keeps waiting rather than reloading.
    expect(dashboard.get_by_role("button", name="Restart the dashboard")).to_be_disabled()
    assert page.evaluate("window.beforeRestart") is True
    with page.expect_navigation(wait_until="domcontentloaded", timeout=20000):
        state["dashboard"] = {
            "pid": 1234,
            "started_at": time.time(),
            "service": "net.example.dashboard",
        }
    assert page.evaluate("window.beforeRestart") is None
    expect(page.locator("#prs-tab")).to_be_visible()
    assert [body for _, body in posted] == [{"service": "dashboard"}]
