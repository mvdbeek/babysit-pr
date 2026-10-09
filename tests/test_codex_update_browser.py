"""The Codex update notice and its update button, with a stubbed update endpoint."""

import pytest
from playwright.sync_api import expect
from test_dashboard_browser import dashboard_site as dashboard_site

pytestmark = pytest.mark.browser


@pytest.fixture
def codex(page):
    state = {
        "installed": "0.160.1",
        "latest": "0.161.0",
        "available": True,
        "updatable": True,
        "checked_at": 1234,
        "error": None,
        "update": None,
    }
    posted = []

    def reply(route):
        if route.request.method == "POST":
            posted.append((route.request.headers, route.request.post_data))
            state["update"] = {
                "running": True,
                "version": "0.161.0",
                "ok": None,
                "error": None,
                "finished_at": None,
            }
        route.fulfill(json=state)

    page.route("**/api/codex-update*", reply)
    return state, posted


def test_no_notice_without_a_newer_codex(page, dashboard_site, codex):
    state, _ = codex
    state.update(latest="0.160.1", available=False)
    page.goto(dashboard_site[0])
    expect(page.locator("#prs-tab")).to_be_visible()
    expect(page.locator("#codex-update")).to_be_hidden()


def test_update_button_installs_and_reports_the_new_version(page, dashboard_site, codex):
    state, posted = codex
    page.goto(dashboard_site[0])
    notice = page.locator("#codex-update")
    expect(notice).to_contain_text("Codex 0.160.1 → 0.161.0 is available.")
    button = notice.get_by_role("button", name="Update Codex")
    button.click()
    expect(notice).to_contain_text("Updating Codex to 0.161.0…")
    expect(button).to_be_disabled()
    [(headers, body)] = posted
    assert headers["x-babysit-action"] == "codex-update" and body == "{}"
    assert headers["content-type"] == "application/json"
    state.update(installed="0.161.0", available=False)
    state["update"] = {**state["update"], "running": False, "ok": True, "finished_at": 1300}
    expect(page.locator("#codex-update-text")).to_have_text(
        "Codex updated to 0.161.0. New and resumed agents use it; running agents keep their version.",
        timeout=10000,
    )
    expect(button).to_be_hidden()


def test_a_failed_update_shows_its_error_and_allows_a_retry(page, dashboard_site, codex):
    state, _ = codex
    state["update"] = {
        "running": False,
        "version": "0.161.0",
        "ok": False,
        "error": "npm error code EACCES",
        "finished_at": 1300,
    }
    page.goto(dashboard_site[0])
    notice = page.locator("#codex-update")
    expect(notice).to_contain_text("is available. The last update failed: npm error code EACCES")
    expect(notice.get_by_role("button", name="Update Codex")).to_be_enabled()


def test_codex_installed_another_way_gets_no_button(page, dashboard_site, codex):
    state, _ = codex
    state["updatable"] = False
    page.goto(dashboard_site[0])
    expect(page.locator("#codex-update")).to_be_visible()
    expect(page.locator("#codex-update-text")).to_have_text(
        "Codex 0.160.1 → 0.161.0 is available. Update it the way it was installed."
    )
    expect(page.locator("#codex-update-button")).to_be_hidden()
