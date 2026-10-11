"""The Settings panel: the wording each Handle button prefills, kept with the watcher."""

import json
import re

import pytest
from playwright.sync_api import Page, expect
from test_dashboard_browser import choose_option
from test_dashboard_browser import dashboard_site as dashboard_site
from test_dashboard_browser import workspace_routes as workspace_routes

pytestmark = pytest.mark.browser


def open_settings(page: Page):
    page.locator("#settings-toggle").click()
    dialog = page.locator("#settings-dialog")
    expect(dialog).to_be_visible()
    return dialog


def test_the_panel_opens_on_the_review_task_the_page_starts_from(page: Page, dashboard_site):
    page.goto(dashboard_site[0])
    dialog = open_settings(page)
    expect(dialog.locator("#settings-task")).to_have_value("prReview")
    prompt = dialog.locator("#settings-prompt")
    expect(prompt).to_have_value(
        "Review {url}. Read its description, linked issues, and the full diff against its base "
        "branch, then check out the branch and read the code the change touches. Look for "
        "correctness bugs, regressions, missing tests, and unclear or unnecessarily complex "
        "code. Do not push, comment, or submit a review on GitHub. Report each finding ranked "
        "by severity with file and line references, and say what you checked."
    )
    choose_option(dialog.get_by_role("combobox", name="Task prompt"), "ci")
    expect(prompt).to_have_value(re.compile(r"^Investigate and fix the failing tests"))


def test_a_saved_prompt_is_what_the_handle_button_prefills(
    page: Page, dashboard_site, workspace_routes
):
    url, home = dashboard_site
    page.goto(url + "/#prs")
    dialog = open_settings(page)
    choose_option(dialog.get_by_role("combobox", name="Task prompt"), "ci")
    dialog.locator("#settings-prompt").fill("Make {url} green again, and say how.")
    dialog.get_by_role("button", name="Save prompt").click()
    expect(dialog.locator("#settings-status")).to_have_text("Saved.")
    # The task the person changed is marked, so the default is not mistaken for it.
    expect(dialog.locator("#settings-task")).to_contain_text("Handle failing tests (edited)")
    expect(dialog.get_by_role("combobox", name="Task prompt")).to_have_value(
        "Handle failing tests (edited)"
    )
    dialog.get_by_role("button", name="Close settings").click()
    page.locator("#pr-list tr").first.get_by_role("button", name="Handle failing tests").click()
    expect(page.locator("#workspace-task")).to_have_value(
        "Make https://github.com/test/alpha/pull/8 green again, and say how."
    )
    assert json.loads((home / "dashboard-settings.json").read_text())["task_prompts"] == {
        "ci": "Make {url} green again, and say how."
    }


def test_a_saved_prompt_survives_a_reload_and_the_default_comes_back(page: Page, dashboard_site):
    url, _ = dashboard_site
    page.goto(url)
    dialog = open_settings(page)
    # The dialog reads the saved settings as it opens; what is typed meanwhile stands.
    dialog.locator("#settings-prompt").fill("Review {url} and report what you find.")
    dialog.get_by_role("button", name="Save prompt").click()
    expect(dialog.locator("#settings-status")).to_have_text("Saved.")
    page.reload()
    dialog = open_settings(page)
    expect(dialog.locator("#settings-prompt")).to_have_value(
        "Review {url} and report what you find."
    )
    dialog.get_by_role("button", name="Use the default").click()
    expect(dialog.locator("#settings-status")).to_have_text("Back to the default.")
    expect(dialog.locator("#settings-prompt")).to_have_value(
        re.compile(r"^Review \{url\}\. Read its description")
    )
    expect(dialog.get_by_role("button", name="Use the default")).to_be_disabled()
    expect(dialog.locator("#settings-task")).not_to_contain_text("(edited)")


def test_a_prompt_the_watcher_refuses_says_why_and_is_not_kept(page: Page, dashboard_site):
    url, home = dashboard_site
    page.goto(url)
    dialog = open_settings(page)
    dialog.locator("#settings-prompt").fill("Review the pull request.")
    dialog.get_by_role("button", name="Save prompt").click()
    expect(dialog.locator("#settings-status")).to_have_text(
        "Could not save: Include {url} where the link belongs"
    )
    assert not (home / "dashboard-settings.json").exists()


def test_the_main_reviewer_is_saved_as_it_is_chosen(page: Page, dashboard_site):
    url, home = dashboard_site
    page.goto(url)
    dialog = open_settings(page)
    reviewer = dialog.get_by_role("combobox", name="Main reviewer")
    expect(dialog.locator("#settings-reviewer")).to_have_value("codex")
    expect(dialog.locator("#settings-reviewer-note")).to_have_text(
        "Review pull request starts Codex. A card that needs you also offers the same "
        "review from Claude."
    )
    choose_option(reviewer, "claude")
    expect(dialog.locator("#settings-status")).to_have_text("Saved.")
    expect(dialog.locator("#settings-reviewer-note")).to_contain_text(
        "starts Claude. A card that needs you also offers the same review from Codex."
    )
    assert json.loads((home / "dashboard-settings.json").read_text())["reviewer"] == "claude"


def test_settings_that_cannot_be_read_leave_the_pages_own_wording(page: Page, dashboard_site):
    url, _ = dashboard_site
    page.goto(url)
    page.route("**/api/settings", lambda route: route.fulfill(status=503, json={"error": "gone"}))
    dialog = open_settings(page)
    expect(dialog.locator("#settings-status")).to_have_text("Showing this page’s defaults: gone")
    expect(dialog.locator("#settings-prompt")).to_have_value(
        re.compile(r"^Review \{url\}\. Read its description")
    )
