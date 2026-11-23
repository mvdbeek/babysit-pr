"""Keyboard shortcuts: the default keys, the list that shows them, and changing them."""

import json

import pytest
from playwright.sync_api import Page, expect
from test_dashboard_browser import dashboard_site as dashboard_site

pytestmark = pytest.mark.browser


def shortcut_row(page: Page, label: str):
    return page.locator("#shortcuts-list tr").filter(
        has=page.get_by_role("rowheader", name=label, exact=True)
    )


def test_question_mark_lists_the_keys_and_each_key_does_its_job(page: Page, dashboard_site):
    page.goto(dashboard_site[0])
    page.keyboard.press("?")
    dialog = page.locator("#shortcuts-dialog")
    expect(dialog).to_be_visible()
    expect(shortcut_row(page, "New task").locator("kbd")).to_have_text("N")
    expect(shortcut_row(page, "Search this view").locator("kbd")).to_have_text("/")
    expect(shortcut_row(page, "Voice assistant").locator("kbd")).to_have_text("Alt+V")
    # Keys wait while a dialog is open, this one included.
    page.keyboard.press("n")
    expect(page.locator("#workspace-dialog")).not_to_be_visible()
    page.keyboard.press("Escape")
    expect(dialog).not_to_be_visible()

    page.keyboard.press("]")
    expect(page.locator("#agents-tab")).to_have_attribute("aria-selected", "true")
    page.keyboard.press("[")
    expect(page.locator("#attention-tab")).to_have_attribute("aria-selected", "true")
    page.keyboard.press("[")
    # Back past the first view is the last one shown.
    expect(page.locator("#attention-tab")).to_have_attribute("aria-selected", "false")

    page.locator("#prs-tab").click()
    page.keyboard.press("/")
    expect(page.locator("#pr-search")).to_be_focused()
    # Typed into the search, the keys are letters.
    page.keyboard.type("n]")
    expect(page.locator("#pr-search")).to_have_value("n]")
    expect(page.locator("#workspace-dialog")).not_to_be_visible()
    expect(page.locator("#prs-tab")).to_have_attribute("aria-selected", "true")

    page.locator("#pr-search").blur()
    page.keyboard.press("n")
    expect(page.locator("#workspace-dialog")).to_be_visible()
    expect(page.locator("#new-task")).to_have_attribute("title", "New task (N)")


def test_a_changed_key_is_kept_in_this_browser_and_can_go_back(page: Page, dashboard_site):
    page.goto(dashboard_site[0])
    page.locator("#settings-toggle").click()
    settings = page.locator("#settings-dialog")
    expect(settings.locator("#settings-shortcuts-note")).to_have_text(
        "Press ? anywhere to see them."
    )
    settings.get_by_role("button", name="Keyboard shortcuts…").click()
    row = shortcut_row(page, "New task")
    row.get_by_role("button", name="Change New task").click()
    expect(row.locator("kbd")).to_have_text("Press a key…")
    # A key another shortcut has is refused, and the choice goes on.
    page.keyboard.press("b")
    expect(page.locator("#shortcuts-status")).to_have_text(
        "B is already Recent updates. Choose another key, or Escape to cancel."
    )
    page.keyboard.press("Tab")
    expect(page.locator("#shortcuts-status")).to_contain_text("Tab stays with the page")
    page.keyboard.press("Shift+T")
    expect(row.locator("kbd")).to_have_text("Shift+T")
    expect(page.locator("#shortcuts-status")).to_have_text("New task: Shift+T.")
    assert json.loads(page.evaluate("localStorage.getItem('babysit-shortcuts')")) == {
        "newTask": "T"
    }
    # Escape while choosing cancels the choice, and leaves the list open.
    shortcut_row(page, "Settings").get_by_role("button", name="Change Settings").click()
    page.keyboard.press("Escape")
    expect(page.locator("#shortcuts-dialog")).to_be_visible()
    expect(shortcut_row(page, "Settings").locator("kbd")).to_have_text(",")
    shortcut_row(page, "Recent updates").get_by_role(
        "button", name="Turn off Recent updates"
    ).click()
    expect(shortcut_row(page, "Recent updates").locator("kbd")).to_have_text("Off")

    page.reload()
    expect(page.locator("#new-task")).to_have_attribute("title", "New task (Shift+T)")
    page.keyboard.press("b")
    expect(page.locator("#notifications-dialog")).not_to_be_visible()
    page.keyboard.press("n")
    expect(page.locator("#workspace-dialog")).not_to_be_visible()
    page.keyboard.press("Shift+T")
    expect(page.locator("#workspace-dialog")).to_be_visible()
    page.keyboard.press("Escape")
    expect(page.locator("#workspace-dialog")).not_to_be_visible()

    page.keyboard.press("?")
    page.get_by_role("button", name="Restore the default keys").click()
    expect(shortcut_row(page, "New task").locator("kbd")).to_have_text("N")
    expect(shortcut_row(page, "Recent updates").locator("kbd")).to_have_text("B")
    assert page.evaluate("localStorage.getItem('babysit-shortcuts')") is None
    page.get_by_role("button", name="Close keyboard shortcuts").click()
    page.keyboard.press("b")
    expect(page.locator("#notifications-dialog")).to_be_visible()
