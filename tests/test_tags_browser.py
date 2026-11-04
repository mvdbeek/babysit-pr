"""Tag editing and shared focus in a real browser, against an isolated server."""

import copy

import pytest
import work_tags
from playwright.sync_api import expect
from test_dashboard_browser import choose_option, dashboard_site  # noqa: F401
from test_workspace_overview_browser import WORKSPACES

pytestmark = pytest.mark.browser


@pytest.mark.parametrize("width", [1440, 390])
def test_tag_editing_and_focus_across_repositories_and_tabs(page, dashboard_site, width):  # noqa: F811
    url, home = dashboard_site
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.set_viewport_size({"width": width, "height": 1000})
    workspaces = copy.deepcopy(WORKSPACES)
    page.route(
        "**/api/workspace-overview*",
        lambda route: route.fulfill(json={"enabled": True, "workspaces": workspaces}),
    )
    tags = work_tags.Tags(home)
    workspace = f"workspace:{workspaces[0]['key']}"
    tags.link(workspace, "pr:test/alpha#8")
    page.goto(url + "/#prs")
    expect(page.locator("#pr-list tr")).to_have_count(2)

    def add_tag(label):
        page.get_by_role("button", name=f"Edit tags for {label}", exact=True).click()
        page.get_by_label("Add tag", exact=True).fill("release")
        page.get_by_role("button", name="Add", exact=True).click()
        expect(page.get_by_role("button", name="Remove tag release", exact=True)).to_be_enabled()
        page.get_by_role("button", name="Close tags", exact=True).click()

    add_tag("test/alpha #8")
    choose_option(page.get_by_role("combobox", name="Focus tag", exact=True), "release")
    expect(page.locator("#pr-list tr")).to_have_count(1)
    expect(page.locator("#pr-list")).to_contain_text("test/alpha")
    page.get_by_role("tab", name="Issues", exact=True).click()
    expect(page.locator("#issue-list tr")).to_have_count(0)
    page.get_by_role("button", name="Clear tag focus").click()
    expect(page.locator("#issue-list tr")).to_have_count(2)
    add_tag("test/beta #31")
    page.locator("#issue-list .tag-chip").click()
    expect(page.locator("#issue-list tr")).to_have_count(1)
    expect(page.locator("#issue-list")).to_contain_text("test/beta")

    page.get_by_role("tab", name="Workspaces", exact=True).click()
    expect(page.locator("#ws-list tr")).to_have_count(1)
    expect(page.locator("#ws-list")).to_contain_text("merged-work")
    page.get_by_role("button", name="Edit tags for merged-work", exact=True).click()
    expect(page.locator("#tag-inherited")).to_contain_text("From linked work: release")
    expect(page.get_by_role("button", name="Remove tag release", exact=True)).to_have_count(0)
    page.get_by_role("button", name="Close tags", exact=True).click()
    page.get_by_role("button", name="Clear tag focus").click()
    add_tag("other-repo-work")
    choose_option(page.get_by_role("combobox", name="Focus tag", exact=True), "release")
    expect(page.locator("#ws-list tr")).to_have_count(2)
    page.reload()
    expect(page.locator("#ws-list tr")).to_have_count(2)
    assert "tag=release" in page.url
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")

    page.get_by_role("tab", name="Pull requests", exact=True).click()
    expect(page.locator("#pr-list tr")).to_have_count(1)
    page.get_by_role("tab", name="Issues", exact=True).click()
    expect(page.locator("#issue-list tr")).to_have_count(1)
    page.get_by_role("button", name="Edit tags for test/beta #31", exact=True).click()
    page.get_by_role("button", name="Remove tag release", exact=True).click()
    expect(page.get_by_role("button", name="Remove tag release", exact=True)).to_have_count(0)
    page.get_by_role("button", name="Close tags", exact=True).click()
    expect(page.locator("#issue-list tr")).to_have_count(0)
    page.get_by_role("button", name="Clear tag focus").click()
    expect(page.locator("#issue-list tr")).to_have_count(2)
    assert not errors


def test_unknown_url_tag_stays_focused_and_cli_edits_refresh(page, dashboard_site):  # noqa: F811
    url, home = dashboard_site
    page.goto(url + "/?tag=not-yet-tagged#prs")
    expect(page.locator("#pr-list tr")).to_have_count(0)
    expect(page.get_by_role("combobox", name="Focus tag", exact=True)).to_have_value(
        "not-yet-tagged"
    )
    work_tags.Tags(home).change({"target": "pr:test/beta#9", "add": ["not-yet-tagged"]})
    page.evaluate("window.dashboardTags.refresh()")
    expect(page.locator("#pr-list tr")).to_have_count(1)
    page.reload()
    expect(page.locator("#pr-list tr")).to_have_count(1)
    page.get_by_role("button", name="Clear tag focus").click()
    expect(page.locator("#pr-list tr")).to_have_count(2)


def test_failed_tag_edit_is_visible_and_can_retry(page, dashboard_site):  # noqa: F811
    url, _ = dashboard_site
    page.goto(url + "/#prs")
    page.get_by_role("button", name="Edit tags for test/alpha #8", exact=True).click()
    page.route(
        "**/api/tags",
        lambda route: (
            route.fulfill(status=400, json={"error": "Could not save tag"})
            if route.request.method == "POST"
            else route.continue_()
        ),
    )
    page.get_by_label("Add tag", exact=True).fill("release")
    page.get_by_role("button", name="Add", exact=True).click()
    expect(page.locator("#tag-error")).to_have_text("Could not save tag")
    expect(page.get_by_role("button", name="Remove tag release", exact=True)).to_have_count(0)
    page.unroute("**/api/tags")
    page.get_by_role("button", name="Add", exact=True).click()
    expect(page.get_by_role("button", name="Remove tag release", exact=True)).to_be_enabled()


def test_long_and_html_like_tags_are_plain_text_and_fit_on_mobile(page, dashboard_site):  # noqa: F811
    url, home = dashboard_site
    names = ["n" * 64, '<img src=x onerror="window.tagInjected=true">']
    work_tags.Tags(home).change({"target": "pr:test/alpha#8", "add": names})
    page.set_viewport_size({"width": 390, "height": 1000})
    page.goto(url + "/#prs")
    expect(page.locator("#pr-list .tag-chip")).to_have_text(sorted(names))
    expect(page.locator(".work-tags img")).to_have_count(0)
    assert page.evaluate("window.tagInjected") is None
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
