"""The Needs you tab: the landing view, its groups, tab badges and the agents chip."""

import attention
import pytest
from playwright.sync_api import Page, expect
from test_dashboard_browser import dashboard_site as dashboard_site  # noqa: F401 - fixture

pytestmark = pytest.mark.browser


@pytest.fixture(autouse=True)
def nothing_is_parked(monkeypatch):
    """The fixture's GitHub dates are fixed; the server runs in-process, so age is too."""
    monkeypatch.setattr(attention, "PARKED_AFTER", 10**9)


EMPTY = {
    "time": 0,
    "login": "fixture",
    "items": [],
    "groups": [
        {"key": key, "label": label, "count": 0}
        for key, label in (
            ("answer", "Answer an agent"),
            ("review", "Review or merge"),
            ("unblock", "Unblock"),
            ("tidy", "Tidy up"),
            ("start", "Pick up"),
            ("parked", "Parked"),
        )
    ],
    "pages": {"watcher": 0, "prs": 0, "issues": 0, "workspaces": 0, "scheduled": 0, "cron": 0},
    "agents_waiting": 0,
    "sources": {},
}


def feed_with(items, **changes):
    groups = [
        {**group, "count": sum(item["group"] == group["key"] for item in items)}
        for group in EMPTY["groups"]
    ]
    return {**EMPTY, "items": items, "groups": groups, **changes}


def item(key, group, reasons, **fields):
    return {
        "key": key,
        "kind": "pr",
        "repo": "test/alpha",
        "number": 8,
        "title": "Test PR",
        "url": key,
        "reasons": [{"code": code, "group": group, "text": text} for code, text in reasons],
        "pages": ["prs"],
        "since": 1_800_000_000,
        "workspace_url": None,
        "workspace_id": None,
        "agent": None,
        "watch": None,
        "group": group,
        **fields,
    }


def test_the_dashboard_opens_on_what_needs_you(page: Page, dashboard_site) -> None:
    url, _ = dashboard_site
    page.goto(url)
    tab = page.get_by_role("tab", name="Needs you")
    expect(tab).to_have_attribute("aria-selected", "true")
    expect(page.locator("#attention-panel")).to_be_visible()
    expect(page.locator("#watcher-panel")).to_be_hidden()
    # The fixture: one watch awaiting feedback approval (its merged twin is ready to clean
    # up) and the user's own PR with failing CI. Without a workspace inventory the
    # assigned issue is not called "nothing started".
    expect(page.locator("#attention-count")).to_have_text("2")
    expect(page.locator("#attention-tab-count")).to_have_text("2")
    groups = page.locator(".attention-group")
    expect(groups).to_have_count(2)
    expect(groups.nth(0).locator("h3")).to_have_text("Answer an agent 1")
    expect(groups.nth(1).locator("h3")).to_have_text("Unblock 1")
    expect(
        page.get_by_role("tab", name="Pull requests", exact=True)
    ).to_have_accessible_description("1 item needs you")
    # The three fixture watches share one pull request: one row, both reasons.
    answer = groups.nth(0).locator(".attention-item")
    expect(answer).to_contain_text("#1")
    expect(answer.locator(".attention-title")).to_have_text("feature")
    expect(answer.locator(".badge")).to_have_count(2)
    expect(answer.locator(".badge", has_text="1 feedback item awaits approval")).to_be_visible()
    expect(
        answer.locator(".badge", has_text="Merged: its checkout is ready to clean up")
    ).to_be_visible()
    expect(answer.get_by_role("link", name="Show in Watcher")).to_be_visible()
    unblock = groups.nth(1).locator(".attention-item")
    expect(unblock.locator(".attention-title")).to_contain_text("Test PR")
    expect(unblock.locator(".badge")).to_have_text(["CI failing"])
    # Titles come from GitHub and render as text, never as markup.
    assert page.evaluate("window.injected") is None
    # Tab badges count what needs the user on each tab; quiet tabs stay unbadged.
    expect(page.locator("#watcher-tab-count")).to_have_text("1")
    expect(page.locator("#prs-tab-count")).to_have_text("1")
    expect(page.locator("#issues-tab-count")).to_be_hidden()
    expect(page.locator("#workspaces-tab-count")).to_be_hidden()
    expect(page.locator("#agents-waiting")).to_be_hidden()
    expect(page.locator("#attention-empty")).to_be_hidden()
    expect(page.locator("#attention-status")).to_contain_text("Merged from the watcher")


def test_show_in_tab_reveals_the_item_on_its_overview(page: Page, dashboard_site) -> None:
    url, _ = dashboard_site
    page.goto(url)
    page.get_by_role("link", name="Show in Pull requests").click()
    expect(page.locator("#prs-panel")).to_be_visible()
    expect(page.locator("#attention-panel")).to_be_hidden()
    expect(page).to_have_url(f"{url}/?item=https%3A%2F%2Fgithub.com%2Ftest%2Falpha%2Fpull%2F8#prs")
    expect(page.locator("#pr-list tr").filter(has_text="Test PR")).to_be_visible()
    expect(page.locator("#navigation-status")).to_be_hidden()
    page.go_back()
    expect(page.locator("#attention-panel")).to_be_visible()
    page.get_by_role("link", name="Show in Watcher").click()
    expect(page.locator("#watcher-panel")).to_be_visible()
    expect(page.locator("#detail")).to_contain_text("Feedback awaits approval")


def test_agents_chip_counts_waiting_agents_and_opens_the_tab(page: Page, dashboard_site) -> None:
    url, _ = dashboard_site
    waiting = feed_with(
        [
            item(
                "https://github.com/test/alpha/pull/8",
                "answer",
                [("agent_blocked", "An agent is waiting for your answer")],
                pages=["prs", "workspaces"],
                workspace_url="http://127.0.0.1:8787/space/w1",
                agent={"status": "blocked"},
            ),
            {
                **item("cron:1", "answer", [("cron_attention", "Its agent needs you: asked")]),
                "kind": "cron",
                "repo": None,
                "number": None,
                "title": "Nightly",
                "url": None,
                "pages": ["cron"],
            },
        ],
        pages={**EMPTY["pages"], "prs": 1, "workspaces": 1, "cron": 1},
        agents_waiting=2,
    )
    page.route("**/api/attention*", lambda route: route.fulfill(json=waiting))
    page.goto(url + "/#prs")
    chip = page.locator("#agents-waiting")
    expect(chip).to_be_visible()
    expect(chip).to_have_attribute("aria-label", "2 agents waiting for you")
    expect(page.locator("#agents-waiting-count")).to_have_text("2")
    expect(page.locator("#workspaces-tab-count")).to_have_text("1")
    chip.click()
    expect(page.locator("#attention-panel")).to_be_visible()
    expect(page.get_by_role("tab", name="Needs you")).to_have_attribute("aria-selected", "true")
    first = page.locator(".attention-item").first
    expect(first).to_contain_text("Agent: blocked")
    expect(first.get_by_role("link", name="Open in Collie")).to_have_attribute(
        "href", "http://127.0.0.1:8787/space/w1"
    )
    expect(first.get_by_role("link", name="GitHub")).to_have_attribute(
        "href", "https://github.com/test/alpha/pull/8"
    )
    cron = page.locator(".attention-item").nth(1)
    expect(cron).to_contain_text("Cron job")
    expect(cron.locator(".attention-title")).to_have_text("Nightly")
    expect(cron.get_by_role("link", name="Show in Cron jobs")).to_have_attribute("href", "#cron")
    expect(cron.get_by_role("link", name="GitHub")).to_have_count(0)


def test_groups_collapse_and_stay_collapsed_across_polls(page: Page, dashboard_site) -> None:
    url, _ = dashboard_site
    page.goto(url)
    toggle = page.locator(".attention-group[data-group=unblock] .attention-toggle")
    expect(toggle).to_have_attribute("aria-expanded", "true")
    toggle.click()
    expect(toggle).to_have_attribute("aria-expanded", "false")
    expect(toggle).to_be_focused()
    expect(page.locator(".attention-group[data-group=unblock] .attention-list")).to_be_hidden()
    expect(page.locator(".attention-group[data-group=answer] .attention-list")).to_be_visible()
    with page.expect_response("**/api/attention?refresh=1"):
        page.locator("#attention-panel").get_by_role("button", name="Refresh").click()
    expect(toggle).to_have_attribute("aria-expanded", "false")
    expect(page.locator(".attention-group[data-group=unblock] .attention-list")).to_be_hidden()
    # A changed feed rebuilds the cards; the focused control of the same card keeps focus.
    show = page.locator(".attention-group[data-group=answer] .attention-item").first.get_by_role(
        "link", name="Show in Watcher"
    )
    show.focus()
    show.evaluate("element => { element.dataset.marker = 'before'; }")
    changed = page.request.get(url + "/api/attention?refresh=1").json()
    changed["items"][0]["reasons"].append(
        {"code": "extra", "group": "answer", "text": "A new reason"}
    )
    page.route("**/api/attention*", lambda route: route.fulfill(json=changed))
    page.evaluate("window.dashboardAttention.refresh(true)")
    expect(page.locator(".attention-item", has_text="A new reason")).to_be_visible()
    expect(page.locator("[data-marker]")).to_have_count(0)
    expect(
        page.locator(".attention-group[data-group=answer] .attention-item").first.get_by_role(
            "link", name="Show in Watcher"
        )
    ).to_be_focused()


def test_quiet_feeds_and_broken_sources_are_said_plainly(page: Page, dashboard_site) -> None:
    url, _ = dashboard_site
    quiet = feed_with([], sources={"prs": {"available": True, "error": "GitHub is down"}})
    page.route("**/api/attention*", lambda route: route.fulfill(json=quiet))
    page.goto(url)
    expect(page.locator("#attention-empty")).to_be_visible()
    expect(page.locator("#attention-empty")).to_contain_text("Nothing needs you right now")
    expect(page.locator("#attention-status")).to_contain_text(
        "Some sources are unavailable: GitHub is down"
    )
    expect(page.locator("#attention-tab-count")).to_be_hidden()
    expect(page.locator("#attention-count")).to_have_text("0")
    for tab in ("watcher", "prs", "issues", "workspaces"):
        expect(page.locator(f"#{tab}-tab-count")).to_be_hidden()
    page.unroute("**/api/attention*")
    page.route("**/api/attention*", lambda route: route.fulfill(status=500, json={"error": "boom"}))
    page.locator("#attention-panel").get_by_role("button", name="Refresh").click()
    expect(page.locator("#attention-error")).to_have_text("Cannot load what needs you: boom")
    # The last good feed stays on screen.
    expect(page.locator("#attention-empty")).to_be_visible()


def test_wait_for_activity_sets_an_item_aside_until_it_changes(page: Page, dashboard_site) -> None:
    url, _ = dashboard_site
    page.goto(url)
    unblock = page.locator(".attention-group[data-group=unblock] .attention-item").first
    unblock.get_by_role("button", name="Wait for activity").click()
    expect(page.locator(".attention-group[data-group=unblock]")).to_have_count(0)
    expect(page.locator("#attention-count")).to_have_text("1")
    expect(page.locator("#prs-tab-count")).to_be_hidden()
    waiting = page.locator(".attention-group[data-group=waiting]")
    expect(waiting.locator("h3")).to_have_text("Waiting on others 1")
    # Set-aside items start folded and are shared by every device through the server.
    expect(waiting.locator(".attention-list")).to_be_hidden()
    assert page.request.get(url + "/api/attention?refresh=1").json()["waiting"][0]["key"] == (
        "https://github.com/test/alpha/pull/8"
    )
    waiting.get_by_role("button", name="Waiting on others").click()
    card = waiting.locator(".attention-item")
    expect(card).to_contain_text("Test PR")
    card.get_by_role("button", name="Resume").click()
    expect(page.locator(".attention-group[data-group=waiting]")).to_have_count(0)
    expect(page.locator(".attention-group[data-group=unblock] .attention-item")).to_contain_text(
        "Test PR"
    )
    expect(page.locator("#prs-tab-count")).to_have_text("1")


def test_parked_items_sit_folded_at_the_end(page: Page, dashboard_site) -> None:
    url, _ = dashboard_site
    parked = feed_with(
        [
            item(
                "https://github.com/test/alpha/pull/8",
                "parked",
                [("approved", "Approved with green CI: ready to merge")],
                parked=True,
            )
        ],
        pages=EMPTY["pages"],
    )
    page.route("**/api/attention*", lambda route: route.fulfill(json=parked))
    page.goto(url)
    group = page.locator(".attention-group[data-group=parked]")
    expect(group.locator("h3")).to_have_text("Parked 1")
    expect(group.locator(".attention-list")).to_be_hidden()
    expect(page.locator("#attention-count")).to_have_text("0")
    expect(page.locator("#attention-tab-count")).to_be_hidden()
    expect(page.locator("#prs-tab-count")).to_be_hidden()


def test_notification_focus_preference_is_shown_and_changed(page: Page, dashboard_site) -> None:
    url, _ = dashboard_site
    page.route(
        "**/api/notification-preferences",
        lambda route: route.fulfill(json={"login": "fixture", "silenced": [], "focus": False}),
    )
    posted = []

    def focus(route):
        posted.append(route.request.post_data_json)
        route.fulfill(json={"login": "fixture", "silenced": [], "focus": True})

    page.route("**/api/notification-focus", focus)
    page.goto(url)
    page.locator("#notifications-toggle").click()
    box = page.locator("#notifications-focus")
    expect(box).not_to_be_checked()
    box.check()
    expect(box).to_be_checked()
    assert posted == [{"login": "fixture", "focus": True}]
