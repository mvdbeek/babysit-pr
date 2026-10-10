"""Polled panels keep the user's place: no double submits, open sections, focus and storage."""

import copy
import json
from datetime import UTC, datetime, timedelta

import pytest
import test_sentry_browser
import test_workspace_overview_browser
from playwright.sync_api import expect
from test_dashboard_browser import choose_option
from test_dashboard_browser import dashboard_site as dashboard_site  # noqa: F401 - fixture
from test_notifications_browser import count, refresh
from test_notifications_browser import inbox as inbox  # noqa: F401 - fixture
from test_sentry_browser import GTN
from test_sentry_browser import row as sentry_row
from test_sentry_browser import rows as sentry_rows
from test_upstream_tests_browser import upstream_site as upstream_site  # noqa: F401 - fixture
from test_workspace_overview_browser import WORKSPACES

pytestmark = pytest.mark.browser

# Both modules call their fixture "site"; pytest registers these under the names below.
sentry_site = test_sentry_browser.site
workspace_site = test_workspace_overview_browser.site

# On a minute boundary, so a few 5-second polls never move relative times ("3h ago").
START = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
NOTIFICATIONS = "babysit-pr:notifications:v1:fixture"


def workspace_row(page, name):
    return page.locator("#ws-list tr").filter(has=page.get_by_text(name, exact=True))


def test_cleanup_confirm_cannot_submit_twice_across_a_poll(page, workspace_site):
    url, plugin, removed = workspace_site
    held = []
    # Held unanswered until a poll has run.
    page.route("**/api/workspace-cleanup", lambda route: held.append(route))
    page.clock.install(time=START)
    page.goto(url + "/#workspaces")
    page.get_by_role("checkbox", name="Select merged-work").check()
    page.get_by_role("button", name="Clean up selected").click()
    confirm = page.get_by_role("button", name="Clean up 1 workspace(s)")
    confirm.click()
    expect(confirm).to_be_disabled()
    plugin.value["warnings"] = ["Poll marker"]
    page.clock.fast_forward(5000)
    expect(page.locator("#ws-notices")).to_contain_text("Poll marker")
    expect(confirm).to_be_disabled()
    confirm.click(force=True)
    assert len(held) == 1
    held[0].continue_()
    expect(page.locator("#ws-dialog")).to_contain_text("Cleanup results")
    assert len(held) == 1
    assert [target["key"] for target in removed] == ["/src/worktrees/repo/merged-work"]


def test_focused_workspace_controls_survive_polls_and_rebuilds(page, workspace_site):
    url, plugin, _ = workspace_site
    plugin.value["workspaces"] = copy.deepcopy(WORKSPACES)
    page.clock.install(time=START)
    page.goto(url + "/#workspaces")
    line = workspace_row(page, "open-work")
    button = line.get_by_role("button", name="Create workspace", exact=True)
    button.focus()
    button.evaluate("element => { element.dataset.marker = 'kept'; }")
    plugin.value["warnings"] = ["Poll marker"]
    page.clock.fast_forward(5000)
    expect(page.locator("#ws-notices")).to_contain_text("Poll marker")
    # Unchanged rows are left alone: the very same element still has focus.
    expect(page.locator("#ws-list [data-marker=kept]")).to_be_focused()
    plugin.value["workspaces"][1]["changes"] = 2
    page.clock.fast_forward(5000)
    expect(line).to_contain_text("2 uncommitted")
    expect(page.locator("#ws-list [data-marker]")).to_have_count(0)
    expect(button).to_be_focused()
    # Toggling a row from the keyboard rebuilds it and keeps the checkbox focused.
    box = page.get_by_role("checkbox", name="Select open-work")
    box.focus()
    page.keyboard.press("Space")
    expect(page.locator("#ws-selected")).to_have_text("1 selected")
    expect(box).to_be_checked()
    expect(box).to_be_focused()
    page.clock.fast_forward(5000)
    expect(box).to_be_checked()
    expect(box).to_be_focused()


def test_workspace_repository_picker_follows_a_vanished_repository(page, workspace_site):
    url, plugin, _ = workspace_site
    page.clock.install(time=START)
    page.goto(url + "/#workspaces")
    picker = page.get_by_role("combobox", name="Filter workspaces by repository")
    choose_option(picker, "other/repo")
    expect(page.locator("#ws-list tr")).to_have_count(1)
    plugin.value["workspaces"] = [entry for entry in WORKSPACES if entry["repo"] != "other/repo"]
    page.clock.fast_forward(5000)
    expect(page.locator("#ws-list tr")).to_have_count(4)
    expect(picker).to_have_value("All repositories")


def test_focused_sentry_controls_survive_polls_and_times_still_advance(page, sentry_site):
    url, fake = sentry_site
    page.clock.install(time=START)
    page.goto(url + "/#sentry")
    line = sentry_row(page, "GALAXY-MAIN-2B")
    expect(line).to_contain_text("Last 3h ago")
    handle = line.get_by_role("button", name="Handle", exact=True)
    handle.focus()
    handle.evaluate("element => { element.dataset.marker = 'kept'; }")
    fake.value["warnings"] = ["Poll marker"]
    page.clock.fast_forward(5000)
    expect(page.locator("#sentry-notices")).to_contain_text("Poll marker")
    expect(page.locator("#sentry-list [data-marker=kept]")).to_be_focused()
    fake.find("b2")["events_24h"] = 99
    page.clock.fast_forward(5000)
    expect(line).to_contain_text("99/24h")
    expect(page.locator("#sentry-list [data-marker]")).to_have_count(0)
    expect(handle).to_be_focused()
    # Relative times are redrawn at most once a minute, even when nothing else changed.
    page.clock.set_system_time(START + timedelta(hours=1))
    page.clock.fast_forward(5000)
    expect(line).to_contain_text("Last 4h ago")
    expect(handle).to_be_focused()


def test_sentry_repository_picker_follows_a_vanished_repository(page, sentry_site):
    url, fake = sentry_site
    page.clock.install(time=START)
    page.goto(url + "/#sentry")
    picker = page.get_by_role("combobox", name="Filter Sentry issues by repository")
    choose_option(picker, f"repo:{GTN}")
    expect(sentry_rows(page)).to_have_count(1)
    fake.value["groups"] = [entry for entry in fake.value["groups"] if entry["repo"] != GTN]
    page.clock.fast_forward(5000)
    expect(sentry_rows(page)).to_have_count(4)
    expect(picker).to_have_value("All repositories")


def test_open_upstream_sections_stay_open_across_polls(page, upstream_site):
    url, plugin = upstream_site
    page.clock.install(time=START)
    page.goto(url + "/#upstream")
    card = page.locator(".upstream-card").first
    evidence = card.locator(":scope > details")
    evidence.locator(":scope > summary").click()
    jobs = evidence.locator("details").first
    jobs.locator("summary").click()
    expect(evidence).to_have_attribute("open", "")
    expect(jobs).to_have_attribute("open", "")
    evidence.evaluate("element => { element.dataset.marker = 'kept'; }")
    with page.expect_request_finished(lambda request: "/api/upstream-tests" in request.url):
        page.clock.fast_forward(30000)
    plugin.value["warnings"] = ["Poll marker"]
    page.clock.fast_forward(30000)
    expect(page.locator("#upstream-notices")).to_contain_text("Poll marker")
    # The changed snapshot rebuilt the card, and its sections were opened again.
    expect(card.locator("[data-marker]")).to_have_count(0)
    expect(evidence).to_have_attribute("open", "")
    expect(jobs).to_have_attribute("open", "")


def test_notification_storage_forgets_items_missing_from_snapshots_for_a_day(page, inbox):
    url, packets = inbox
    first, second = packets["prs"]["prs"]
    second["ci"] = "SUCCESS"
    job = packets["status"]["jobs"][0]
    job["feedback"] = [
        {
            "kind": "issue_comment",
            "id": "1",
            "author": "reviewer",
            "body": "A long review comment body",
            "url": first["url"] + "#issuecomment-1",
        }
    ]

    def stored():
        return json.loads(page.evaluate(f"localStorage.getItem('{NOTIFICATIONS}')"))

    def key(item):
        return item["url"].lower()

    page.goto(url + "/#watcher")
    count(page, 0)
    page.wait_for_function(
        "name => JSON.parse(localStorage.getItem(name))?.baselines?.watcher", arg=NOTIFICATIONS
    )
    state = stored()
    assert key(second) in state["baselines"]["prs"]
    assert key(second) in state["outcomes"]
    # Watcher baselines keep a digest of each feedback body, not the body itself.
    assert "A long review comment body" not in json.dumps(state)
    # A baseline saved with whole bodies (before digests) is not a change.
    page.evaluate(
        """([name, url, body]) => {
          const state = JSON.parse(localStorage.getItem(name));
          const values = state.baselines.watcher[url].values;
          values.feedback = JSON.stringify(
            JSON.parse(values.feedback).map(({ kind, id }) => ({ kind, id, body })),
          );
          localStorage.setItem(name, JSON.stringify(state));
        }""",
        [NOTIFICATIONS, key(first), "A long review comment body"],
    )
    page.reload()
    count(page, 0)
    # A PR one snapshot omits (search results vary) keeps its baseline, marked missing...
    packets["prs"]["prs"] = [first]
    packets["prs"]["synced_at"] += 1
    refresh(page)
    count(page, 0)
    state = stored()
    assert isinstance(state["baselines"]["prs"][key(second)]["missing"], int)
    assert key(second) in state["outcomes"]
    assert "missing" not in state["baselines"]["prs"][key(first)]
    # ...so its return compares with it: unchanged, it is not announced as a new PR.
    packets["prs"]["prs"] = [first, second]
    packets["prs"]["synced_at"] += 1
    refresh(page)
    count(page, 0)
    assert "missing" not in stored()["baselines"]["prs"][key(second)]
    # Missing for over a day, it is forgotten, with the CI outcome no source still lists.
    packets["prs"]["prs"] = [first]
    packets["prs"]["synced_at"] += 1
    refresh(page)
    count(page, 0)
    page.evaluate(
        """([name, url]) => {
          const state = JSON.parse(localStorage.getItem(name));
          state.baselines.prs[url].missing = Date.now() - 25 * 60 * 60 * 1000;
          localStorage.setItem(name, JSON.stringify(state));
        }""",
        [NOTIFICATIONS, key(second)],
    )
    packets["prs"]["synced_at"] += 1
    refresh(page)
    count(page, 0)
    state = stored()
    assert key(first) in state["baselines"]["prs"]
    assert key(second) not in state["baselines"]["prs"]
    assert key(second) not in state["outcomes"]
    assert key(first) in state["baselines"]["watcher"]
    # A changed comment body is still a change.
    job["feedback"][0]["body"] = "An edited review comment body"
    job["updated_at"] += 1
    refresh(page)
    count(page, 1)
    page.locator("#notifications-toggle").click()
    expect(page.locator("#notifications-list")).to_contain_text("Feedback updated")
