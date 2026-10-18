"""The Needs you tab: the landing view, its groups, tab badges and the agents chip."""

import json
import re
import time

import attention
import pytest
from playwright.sync_api import Page, expect
from test_dashboard_browser import dashboard_site as dashboard_site  # noqa: F401 - fixture
from test_dashboard_browser import workspace_routes as workspace_routes  # noqa: F401 - fixture
from test_workspace_overview_browser import link as overview_link
from test_workspace_overview_browser import row as overview_row

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
                workspace_id="w1",
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
    # Beside Open in Collie, the agent's transcript is read in place.
    seen = []

    def transcript(route):
        seen.append(route.request.url)
        route.fulfill(json={"sessions": [], "session": None, "entries": [], "start": 0, "total": 0})

    page.route("**/api/workspace-transcript?*", transcript)
    first.get_by_role("button", name="Transcript", exact=True).click()
    viewer = page.locator("#ws-viewer")
    expect(viewer.get_by_text("No Claude or Codex session")).to_be_visible()
    assert "workspace-transcript?workspace=w1" in seen[-1]
    expect(viewer.locator("#ws-viewer-links").get_by_role("link", name="PR #8")).to_have_attribute(
        "href", "https://github.com/test/alpha/pull/8"
    )
    page.keyboard.press("Escape")
    expect(viewer).to_be_hidden()
    cron = page.locator(".attention-item").nth(1)
    expect(cron.get_by_role("button", name="Transcript", exact=True)).to_have_count(0)
    expect(cron).to_contain_text("Cron job")
    expect(cron.locator(".attention-title")).to_have_text("Nightly")
    expect(cron.get_by_role("link", name="Show in Cron jobs")).to_have_attribute("href", "#cron")
    expect(cron.get_by_role("link", name="GitHub")).to_have_count(0)


def test_a_blocked_repair_offers_its_transcript_without_a_workspace(
    page: Page, dashboard_site
) -> None:
    url, _ = dashboard_site
    blocked = feed_with(
        [
            item(
                "https://github.com/test/alpha/pull/8",
                "answer",
                [("watch_blocked", "Repair blocked: needs a decision")],
                pages=["watcher"],
                watch={"id": "abc123", "status": "blocked"},
            )
        ],
        pages={**EMPTY["pages"], "watcher": 1},
    )
    page.route("**/api/attention*", lambda route: route.fulfill(json=blocked))
    seen = []

    def transcript(route):
        seen.append(route.request.url)
        route.fulfill(json={"sessions": [], "session": None, "entries": [], "start": 0, "total": 0})

    page.route("**/api/workspace-transcript?*", transcript)
    watch = {"id": "abc123", "status": "blocked", "repo": "test/alpha", "number": 8}
    session = {"id": "s1", "agent": "claude", "title": "Fix it", "updated": 1, "watch": watch}
    agents = {"agents": [], "sessions": [session], "path": "/c", "workspace": None, "url": None}

    def workspace_agents(route):
        seen.append(route.request.url)
        route.fulfill(json=agents)

    page.route("**/api/workspace-agents?*", workspace_agents)
    page.goto(url + "/#attention")
    card = page.locator(".attention-item").first
    expect(card.get_by_role("link", name="Open in Collie")).to_have_count(0)
    card.get_by_role("button", name="Transcript", exact=True).click()
    viewer = page.locator("#ws-viewer")
    expect(viewer.get_by_text("No Claude or Codex session")).to_be_visible()
    assert any("workspace-transcript?watch=abc123" in u for u in seen)
    # The repair's session can be taken over from here: cancel the watch, then resume it.
    expect(viewer.get_by_role("button", name="Cancel watch and reopen…")).to_be_visible()
    expect(viewer.get_by_role("button", name="Cancel watch and send…")).to_be_visible()
    assert any("workspace-agents?watch=abc123" in u for u in seen)


def test_a_requested_review_starts_from_its_card(
    page: Page, dashboard_site, workspace_routes
) -> None:
    url, _ = dashboard_site
    info, _, requests = workspace_routes
    info["matches"] = []
    pr = "https://github.com/test/alpha/pull/8"
    requested = feed_with(
        [
            item(pr, "review", [("review_requested", "Your review is requested")]),
            item(
                "https://github.com/test/alpha/pull/9",
                "review",
                [("review_requested", "Your review is requested")],
                number=9,
                workspace_id="w1",
                workspace_url="https://collie.example.ts.net/space/w1",
            ),
        ]
    )
    page.route("**/api/attention*", lambda route: route.fulfill(json=requested))
    page.goto(url + "/#attention")
    cards = page.locator(".attention-item")
    # A review already under way links its workspace instead of offering another.
    expect(cards.nth(1).get_by_role("link", name="Open in Collie")).to_be_visible()
    expect(cards.nth(1).get_by_role("button", name="Review pull request")).to_have_count(0)
    cards.first.get_by_role("button", name="Review pull request").click()
    dialog = page.locator("#workspace-dialog")
    expect(dialog).to_be_visible()
    task = dialog.get_by_role("textbox", name="Task", exact=True)
    expect(task).to_have_value(re.compile(rf"^Review {re.escape(pr)}\. "))
    dialog.get_by_role("button", name="Handle", exact=True).click()
    expect(page.locator("#workspace-progress")).to_contain_text("running")
    assert requests[-1]["id"] == "pr-one"
    assert requests[-1]["action"] == "handle"
    assert requests[-1]["task"].startswith(f"Review {pr}.")


def test_a_review_from_needs_you_opens_from_the_background_snapshot(
    page: Page, dashboard_site, workspace_routes
) -> None:
    url, _ = dashboard_site
    info, snapshot, _ = workspace_routes
    info["matches"] = []
    snapshot["synced_at"] = 1
    loads = []

    def workspaces(route):
        loads.append(route.request.url)
        route.fulfill(json=snapshot)

    page.route("**/api/workspaces", workspaces)
    pr = "https://github.com/test/alpha/pull/8"
    requested = feed_with([item(pr, "review", [("review_requested", "Your review is requested")])])
    page.route("**/api/attention*", lambda route: route.fulfill(json=requested))
    # Needs you is the default tab; it keeps the snapshot loaded before any dialog asks.
    with page.expect_response("**/api/workspaces"):
        page.goto(url + "/#attention")
    before = len(loads)
    page.locator(".attention-item").first.get_by_role("button", name="Review pull request").click()
    expect(
        page.locator("#workspace-dialog").get_by_role("textbox", name="Task", exact=True)
    ).to_be_visible()
    assert len(loads) == before


def test_a_requested_review_also_offers_the_agent_that_is_not_the_main_reviewer(
    page: Page, dashboard_site, workspace_routes
) -> None:
    url, _ = dashboard_site
    info, _, requests = workspace_routes
    info["matches"] = []
    pr = "https://github.com/test/alpha/pull/8"
    requested = feed_with([item(pr, "review", [("review_requested", "Your review is requested")])])
    page.route("**/api/attention*", lambda route: route.fulfill(json=requested))
    page.goto(url + "/#attention")
    card = page.locator(".attention-item").first
    # Codex reviews by default, so the second opinion comes from Claude.
    expect(card.get_by_role("button", name="Review with Codex")).to_have_count(0)
    card.get_by_role("button", name="Review with Claude").click()
    dialog = page.locator("#workspace-dialog")
    expect(dialog.get_by_label("Agent", exact=True)).to_have_value("claude")
    expect(dialog.get_by_role("textbox", name="Task", exact=True)).to_have_value(
        re.compile(rf"^Review {re.escape(pr)}\. ")
    )
    dialog.get_by_role("button", name="Handle", exact=True).click()
    expect(page.locator("#workspace-progress")).to_contain_text("running")
    assert requests[-1]["agent"] == "claude"


def test_the_main_reviewer_in_settings_is_the_one_a_card_starts(
    page: Page, dashboard_site, workspace_routes
) -> None:
    url, home = dashboard_site
    info, _, requests = workspace_routes
    info["matches"] = []
    (home / "dashboard-settings.json").write_text(json.dumps({"reviewer": "claude"}))
    requested = feed_with(
        [
            item(
                "https://github.com/test/alpha/pull/8",
                "review",
                [("review_requested", "Your review is requested")],
            )
        ]
    )
    page.route("**/api/attention*", lambda route: route.fulfill(json=requested))
    page.goto(url + "/#attention")
    card = page.locator(".attention-item").first
    expect(card.get_by_role("button", name="Review with Codex")).to_be_visible()
    card.get_by_role("button", name="Review pull request").click()
    dialog = page.locator("#workspace-dialog")
    expect(dialog.get_by_label("Agent", exact=True)).to_have_value("claude")
    dialog.get_by_role("button", name="Handle", exact=True).click()
    expect(page.locator("#workspace-progress")).to_contain_text("running")
    assert requests[-1]["agent"] == "claude"


@pytest.fixture
def tidy(page: Page):
    """Two closed checkouts on the Tidy up list, and a stubbed Workspaces cleanup."""
    ready = overview_row("merged-work", status="ready", links=[overview_link("pr", 7, "merged")])
    dirty = overview_row(
        "dirty-work",
        status="blocked",
        links=[overview_link("issue", 12, "closed")],
        blockers=[{"kind": "dirty", "text": "3 uncommitted change(s)"}],
    )
    snapshot = {"enabled": True, "workspaces": [ready, dirty], "synced_at": time.time()}
    page.route("**/api/workspace-overview*", lambda route: route.fulfill(json=snapshot))
    posted = []

    def cleanup(route):
        posted.append(route.request.post_data_json)
        # Like the server, later scans report the cleanup that is running.
        snapshot["cleanup"] = {"status": "running", "results": []}
        route.fulfill(json={"accepted": True, "cleanup": snapshot["cleanup"]})

    page.route("**/api/workspace-cleanup", cleanup)
    tidy_up = feed_with(
        [
            item(
                "https://github.com/base/repo/pull/7",
                "tidy",
                [("workspace_ready", "Its items are closed: ready to clean up")],
                repo="base/repo",
                number=7,
                cleanup=[ready["key"]],
            ),
            item(
                "https://github.com/base/repo/issues/12",
                "tidy",
                [("workspace_blocked", "Its items are closed, but: 3 uncommitted change(s)")],
                kind="issue",
                repo="base/repo",
                number=12,
                cleanup=[dirty["key"]],
            ),
        ]
    )
    page.route("**/api/attention*", lambda route: route.fulfill(json=tidy_up))
    return ready, dirty, posted


def test_a_tidy_card_cleans_up_its_checkout_after_the_same_confirmation(
    page: Page, dashboard_site, tidy
) -> None:
    url, _ = dashboard_site
    ready, _, posted = tidy
    page.goto(url + "/#attention")
    page.locator(".attention-item").first.get_by_role("button", name="Clean up", exact=True).click()
    dialog = page.locator("#ws-dialog")
    expect(page.locator("#ws-dialog-title")).to_have_text("Clean up 1 workspace(s)")
    expect(dialog).to_contain_text("merged-work")
    expect(dialog).not_to_contain_text("dirty-work")
    assert posted == []  # Nothing is removed before the confirmation.
    page.locator("#ws-confirm").click()
    expect(page.locator("#ws-dialog-title")).to_have_text("Cleaning up…")
    assert posted == [{"targets": [{"key": ready["key"], "approve": []}]}]


def test_tidy_up_cleans_up_every_listed_checkout_at_once(page: Page, dashboard_site, tidy) -> None:
    url, _ = dashboard_site
    ready, dirty, posted = tidy
    page.goto(url + "/#attention")
    group = page.locator(".attention-group[data-group=tidy]")
    group.get_by_role("button", name="Clean up 2 workspaces…").click()
    dialog = page.locator("#ws-dialog")
    expect(page.locator("#ws-dialog-title")).to_have_text("Clean up 2 workspace(s)")
    # What would be lost is still offered row by row, and never pre-ticked.
    override = dialog.get_by_label("Remove anyway: 3 uncommitted change(s)")
    expect(override).not_to_be_checked()
    override.check()
    page.locator("#ws-confirm").click()
    expect(page.locator("#ws-dialog-title")).to_have_text("Cleaning up…")
    assert posted == [
        {
            "targets": [
                {"key": ready["key"], "approve": []},
                {"key": dirty["key"], "approve": ["dirty"]},
            ]
        }
    ]


def test_several_requested_reviews_start_from_one_dialog(
    page: Page, dashboard_site, workspace_routes
) -> None:
    url, home = dashboard_site
    (home / "dashboard-settings.json").write_text(json.dumps({"reviewer": "claude"}))
    info, snapshot, _ = workspace_routes
    for entry in snapshot["prs"].values():
        entry["matches"] = []
    batches = []

    def batch(route):
        body = route.request.post_data_json
        batches.append(body)
        route.fulfill(
            json={
                "results": [
                    {"id": entry["id"], "scheduled": {"start_at": time.time()}}
                    for entry in body["items"]
                ]
            }
        )

    page.route("**/api/workspace-batch", batch)
    requested = [("review_requested", "Your review is requested")]
    review = feed_with(
        [
            item("https://github.com/test/alpha/pull/8", "review", requested),
            item(
                "https://github.com/test/beta/pull/9",
                "review",
                requested,
                repo="test/beta",
                number=9,
            ),
            item(
                "https://github.com/test/alpha/pull/10",
                "review",
                [("ready_to_merge", "Approved with green CI")],
                number=10,
            ),
        ]
    )
    page.route("**/api/attention*", lambda route: route.fulfill(json=review))
    page.goto(url + "/#attention")
    group = page.locator(".attention-group[data-group=review]")
    # Only requested reviews can be picked; nothing is picked to begin with.
    picks = group.get_by_role("checkbox")
    expect(picks).to_have_count(2)
    expect(group.get_by_role("button", name="Review selected…")).to_be_disabled()
    group.get_by_role("checkbox", name="Select PR test/beta #9 for a batch review").check()
    expect(group.get_by_role("button", name="Review 1 selected…")).to_be_enabled()
    group.get_by_role("button", name="Select all").click()
    expect(picks.first).to_be_checked()
    expect(group.get_by_role("button", name="Clear selection")).to_be_visible()
    group.get_by_role("button", name="Review 2 selected…").click()
    dialog = page.locator("#workspace-dialog")
    expect(page.locator("#workspace-title")).to_have_text("Handle 2 pull requests")
    # The main reviewer from Settings starts them; the dialog can still pick the other.
    expect(dialog.get_by_label("Agent", exact=True)).to_have_value("claude")
    expect(page.locator("#workspace-task")).to_have_value(re.compile(r"^Review \{url\}\. "))
    dialog.get_by_role("button", name="Schedule 2 tasks").click()
    expect(page.locator("#workspace-progress")).to_contain_text("Scheduled 2 of 2")
    assert batches[-1]["agent"] == "claude"
    assert [entry["id"] for entry in batches[-1]["items"]] == ["pr-one", "pr-two"]
    # Scheduled reviews leave the selection.
    expect(picks.first).not_to_be_checked()
    expect(picks.last).not_to_be_checked()


def test_a_review_clicked_before_the_prs_load_waits_for_them(
    page: Page, dashboard_site, workspace_routes
) -> None:
    url, _ = dashboard_site
    workspace_routes[0]["matches"] = []
    requested = feed_with(
        [
            item(
                "https://github.com/Test/Alpha/pull/8",
                "review",
                [("review_requested", "Your review is requested")],
                repo="Test/Alpha",
            )
        ]
    )
    page.route("**/api/attention*", lambda route: route.fulfill(json=requested))
    held = []
    page.route("**/api/prs*", lambda route: held.append(route))
    page.goto(url + "/#attention")
    button = page.get_by_role("button", name="Review pull request")
    expect(button).to_be_visible()
    for _ in range(100):
        if held:
            break
        page.wait_for_timeout(20)
    button.click()
    expect(button).to_be_disabled()
    expect(page.locator("#workspace-dialog")).to_be_hidden()
    page.unroute("**/api/prs*")
    for route in held:
        route.continue_()
    # The card's link differs in case from the overview's; the PR is the same one.
    expect(page.locator("#workspace-dialog")).to_be_visible()
    expect(page.locator("#workspace-task")).to_have_value(re.compile(r"^Review https://"))
    expect(page.locator("#attention-error")).to_be_hidden()


def test_a_review_of_a_pr_gone_from_the_overview_says_so(page: Page, dashboard_site) -> None:
    url, _ = dashboard_site
    gone = feed_with(
        [
            item(
                "https://github.com/test/alpha/pull/404",
                "review",
                [("review_requested", "Your review is requested")],
                number=404,
            )
        ]
    )
    page.route("**/api/attention*", lambda route: route.fulfill(json=gone))
    page.goto(url + "/#attention")
    page.get_by_role("button", name="Review pull request").click()
    expect(page.locator("#attention-error")).to_have_text(
        "Could not start the review: The pull request is no longer in the overview."
    )
    expect(page.locator("#workspace-dialog")).to_be_hidden()


def test_a_review_whose_prs_cannot_load_says_why(page: Page, dashboard_site) -> None:
    url, _ = dashboard_site
    requested = feed_with(
        [item("https://github.com/test/alpha/pull/8", "review", [("review_requested", "Yours")])]
    )
    page.route("**/api/attention*", lambda route: route.fulfill(json=requested))
    page.route(
        "**/api/prs*", lambda route: route.fulfill(status=503, json={"error": "GitHub is down"})
    )
    page.goto(url + "/#attention")
    page.get_by_role("button", name="Review pull request").click()
    expect(page.locator("#attention-error")).to_contain_text(
        "Could not start the review: Cannot load pull requests:"
    )
    expect(page.locator("#workspace-dialog")).to_be_hidden()


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
