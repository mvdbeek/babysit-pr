"""Subscription usage display and default account choice, with stubbed usage readings."""

import time

import pytest
from playwright.sync_api import expect
from test_dashboard_browser import dashboard_site as dashboard_site
from test_dashboard_browser import viewer_routes as viewer_routes
from test_dashboard_browser import workspace_routes as workspace_routes

pytestmark = pytest.mark.browser

SOON = "2999-01-01T00:00:00Z"


def window(name, used, agent="claude"):
    return {"agent": agent, "name": name, "used_percent": used, "resets_at": SOON}


def account(id_, label, used, agent="claude", account=None, error=None):
    windows = [window("five_hour", used, agent), window("seven_day", used / 2, agent)]
    return {
        "id": id_,
        "agent": agent,
        "account": account,
        "label": label,
        "windows": windows,
        "left_percent": 100 - used,
        "error": error,
        "checked_at": time.time() - 600,
    }


@pytest.fixture
def usage(page):
    value = {
        "accounts": [
            account("codex", "Codex", 70, agent="codex"),
            account("claude:default", "Claude · Default", 50),
            account("claude:work", "Claude · work", 15, account="work"),
        ],
        "best": "claude:work",
        "attempted_at": time.time() - 120,
        "reader": "running",
    }
    asked = []

    def reply(route):
        asked.append(route.request.url.partition("?")[2])
        route.fulfill(json=value)

    page.route("**/api/llm-usage*", reply)
    return value, asked


@pytest.fixture
def new_task(page, dashboard_site, viewer_routes):
    snapshot = {
        "prs": {},
        "issues": {},
        "new": {},
        "error": None,
        "synced_at": 1234,
        "agent_choices": {
            "codex": {"models": [], "efforts": []},
            "claude": {
                "accounts": [
                    {"id": "default", "label": "Default"},
                    {"id": "work", "label": "work"},
                ],
                "models": [{"id": "opus", "efforts": ["high"]}],
                "efforts": ["high"],
            },
        },
    }
    page.route("**/api/workspaces", lambda route: route.fulfill(json=snapshot))
    page.route(
        "**/api/workspace-repos*",
        lambda route: route.fulfill(
            json={
                "repos": [
                    {"repo": "up/alpha", "clone": "/fixture/alpha", "remote": "origin", "active": 1}
                ],
                "idle": 0,
            }
        ),
    )
    sent = []

    def start(route):
        sent.append(route.request.post_data_json)
        route.fulfill(json={"error": "stopped by the test"}, status=400)

    page.route("**/api/workspace-new", start)
    return dashboard_site[0], sent


def test_header_shows_usage_and_dialog_lists_each_login(page, usage, new_task):
    value, asked = usage
    url, _ = new_task
    page.goto(url)
    toggle = page.get_by_role("button", name="Usage: Claude · work has the most quota left, 85%")
    expect(toggle).to_have_text("85%")
    toggle.click()
    dialog = page.locator("#usage-dialog")
    expect(dialog.locator(".usage-account strong")).to_have_text(
        ["Codex", "Claude · Default", "Claude · work"]
    )
    work = dialog.locator(".usage-account").nth(2)
    expect(work.locator(".badge")).to_have_text("Most left")
    expect(work.locator(".usage-window").first).to_contain_text("5-hour")
    expect(work.locator(".usage-window").first).to_contain_text("85% left · resets in")
    expect(dialog.locator("#usage-status")).to_have_text("Checked 2m ago.")
    value["reader"] = "offline"
    value["accounts"][1]["error"] = "usage endpoint answered HTTP 429"
    dialog.get_by_role("button", name="Refresh").click()
    expect(dialog.locator("#usage-status")).to_contain_text("watcher daemon reads usage")
    expect(dialog.locator(".usage-error")).to_have_text(
        "Last reading 10m old: usage endpoint answered HTTP 429"
    )
    assert "refresh=1" in asked
    page.screenshot(path="reports/usage-dialog.png")
    page.keyboard.press("Escape")
    for width in (390, 320):
        page.set_viewport_size({"width": width, "height": 844})
        page.screenshot(path=f"reports/usage-header-{width}.png")
        assert page.evaluate("document.documentElement.scrollWidth") <= width
        expect(toggle).to_be_in_viewport(ratio=1)
        toggle.click()
        expect(dialog).to_be_in_viewport(ratio=1)
        page.screenshot(path=f"reports/usage-dialog-{width}.png")
        page.keyboard.press("Escape")


def test_new_task_defaults_to_the_login_with_most_quota_left(page, usage, new_task):
    value, _ = usage
    url, sent = new_task
    page.goto(url)
    expect(page.locator("#usage-value")).to_have_text("85%")
    page.get_by_role("button", name="New task").click()
    dialog = page.locator("#workspace-dialog")
    agent = dialog.get_by_label("Agent", exact=True)
    expect(agent).to_have_value("claude")
    expect(agent.locator("option")).to_have_text(["Codex · 30% left", "Claude · 85% left"])
    accounts = dialog.get_by_label("Claude account", exact=True)
    expect(accounts).to_have_value("work")
    expect(accounts.locator("option")).to_have_text(["Default · 50% left", "work · 85% left"])
    expect(dialog.locator(".usage-choice")).to_have_text(
        "Claude · work has the most quota left (85%)."
    )
    page.screenshot(path="reports/usage-new-task.png")
    # A person's choice is described and kept.
    accounts.select_option("")
    expect(dialog.locator(".usage-choice")).to_have_text(
        "Claude · work has the most quota left (85%); Claude · Default has 50%."
    )
    dialog.get_by_label("New branch").fill("try-parser")
    dialog.get_by_label("Task", exact=True).fill("Explore")
    dialog.get_by_role("button", name="Start task").click()
    for _ in range(50):
        if sent:
            break
        page.wait_for_timeout(100)
    assert sent[0]["agent"] == "claude" and "claude_account" not in sent[0]
    page.keyboard.press("Escape")
    # When Codex has the most left, a new dialog starts on Codex.
    value["accounts"][0]["left_percent"] = 95
    value["accounts"][0]["windows"] = [window("five_hour", 5, "codex")]
    page.evaluate("window.dashboardUsage.load()")
    expect(page.locator("#usage-value")).to_have_text("95%")
    page.get_by_role("button", name="New task").click()
    expect(agent).to_have_value("codex")
    expect(accounts).to_be_disabled()
    # A later reading never switches the open dialog.
    value["accounts"][0]["left_percent"] = 5
    value["accounts"][0]["windows"] = [window("five_hour", 95, "codex")]
    page.evaluate("window.dashboardUsage.load()")
    expect(agent.locator("option").first).to_have_text("Codex · 5% left")
    expect(agent).to_have_value("codex")
    # Choosing Claude picks its login with the most quota left.
    agent.select_option("claude")
    expect(accounts).to_have_value("work")


def test_hidden_default_login_leaves_only_named_logins(page, usage, new_task):
    value, _ = usage
    url, sent = new_task
    # The server leaves Default out when a named login is the same one.
    value["accounts"].pop(1)
    page.unroute("**/api/workspaces")
    page.route(
        "**/api/workspaces",
        lambda route: route.fulfill(
            json={
                "prs": {},
                "issues": {},
                "new": {},
                "error": None,
                "synced_at": 1234,
                "agent_choices": {
                    "codex": {"models": [], "efforts": []},
                    "claude": {
                        "accounts": [
                            {"id": "psu", "label": "psu"},
                            {"id": "work", "label": "work"},
                        ],
                        "models": [],
                        "efforts": [],
                    },
                },
            }
        ),
    )
    page.goto(url)
    page.get_by_role("button", name="New task").click()
    dialog = page.locator("#workspace-dialog")
    accounts = dialog.get_by_label("Claude account", exact=True)
    expect(accounts.locator("option")).to_have_text(["psu", "work · 85% left"])
    expect(accounts).to_have_value("work")
    accounts.select_option("psu")
    dialog.get_by_label("New branch").fill("try-parser")
    dialog.get_by_label("Task", exact=True).fill("Explore")
    dialog.get_by_role("button", name="Start task").click()
    for _ in range(50):
        if sent:
            break
        page.wait_for_timeout(100)
    assert sent[0]["claude_account"] == "psu"


def test_no_usage_keeps_the_previous_defaults(page, new_task):
    page.route(
        "**/api/llm-usage*",
        lambda route: route.fulfill(
            json={"accounts": [], "best": None, "attempted_at": None, "reader": "offline"}
        ),
    )
    url, _ = new_task
    page.goto(url)
    expect(page.locator("#usage-value")).to_have_text("–")
    page.get_by_role("button", name="New task").click()
    dialog = page.locator("#workspace-dialog")
    expect(dialog.get_by_label("Agent", exact=True)).to_have_value("codex")
    expect(dialog.locator(".usage-choice")).to_have_text("")
