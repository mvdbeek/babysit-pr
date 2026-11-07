"""The Agents tab and the viewer's Terminal view in real Chromium, against mocked APIs."""

import time
from urllib.parse import parse_qs, urlparse

import pytest
from playwright.sync_api import Page, expect
from test_dashboard_browser import choose_option
from test_dashboard_browser import dashboard_site as dashboard_site
from test_dashboard_browser import viewer_routes as viewer_routes
from test_dashboard_browser import workspace_routes as workspace_routes

pytestmark = pytest.mark.browser

NOW = time.time()


def agent(id_, status, **fields):
    machine = fields.get("machine")
    pane = id_.split("/")[-1]
    return {
        "id": id_,
        "pane": pane,
        "workspace": f"{machine}/w1" if machine else pane.split(":")[0],
        "agent": "claude",
        "status": status,
        "title": f"{status} task",
        "session": f"s-{pane}",
        "origin": "you",
        "origin_label": None,
        "machine": None,
        "machine_label": None,
        "name": f"{status}-work",
        "repo": "repo",
        "branch": f"{status}-branch",
        "links": [],
        "since": NOW - 300,
        **fields,
    }


AGENTS = [
    agent("w2:p1", "idle", origin="cron", origin_label="Cron: nightly"),
    agent("w1:p1", "working"),
    agent(
        "mini/w1:p1",
        "blocked",
        agent="codex",
        machine="mini",
        machine_label="Mac mini",
        origin="watch",
        origin_label="Watch w-7",
        origin_url="https://github.com/o/r/pull/7",
    ),
]
MACHINES = [
    {"id": None, "label": "This machine", "local": True, "pending": False, "error": None},
    {"id": "mini", "label": "Mac mini", "local": False, "pending": False, "error": None},
]


@pytest.fixture
def agents_routes(page, viewer_routes):
    sent = []
    page.route(
        "**/api/agents",
        lambda route: route.fulfill(json={"agents": AGENTS, "machines": MACHINES, "now": NOW}),
    )

    def terminal(route):
        query = {k: v[0] for k, v in parse_qs(urlparse(route.request.url).query).items()}
        pane = query.get("pane") or "w1:p1"
        route.fulfill(
            json={
                "workspace": query["workspace"],
                "pane": pane,
                "panes": [
                    {"pane": "w1:p1", "label": None, "agent": "claude", "status": "working"},
                    {"pane": "w1:p9", "label": "babysit shell", "agent": None, "status": None},
                ],
                "screen": f"\x1b[1;31mbold red\x1b[0m on {pane}\n  indented",
                "truncated": False,
            }
        )

    def post(route):
        action = route.request.headers.get("x-babysit-action")
        sent.append((action, route.request.post_data_json))
        route.fulfill(json={"pane": "w1:p9"} if action == "workspace-shell" else {"sent": True})

    page.route("**/api/workspace-terminal?*", terminal)
    page.route("**/api/workspace-keys", post)
    page.route("**/api/workspace-shell", post)
    return sent


def typed(sent):
    """What reached the pane, one key or character at a time."""
    tokens = []
    for action, body in sent:
        if action != "workspace-keys":
            continue
        tokens += [("key", key) for key in body.get("keys", [])]
        tokens += [("text", c) for c in body.get("text", "")]
        if body.get("enter"):
            tokens.append(("key", "enter"))
    return tokens


def rows(page: Page):
    return page.locator("#agents-list .agents-row")


def test_agents_are_listed_by_state_with_a_badge_and_filters(page, dashboard_site, agents_routes):
    url, _ = dashboard_site
    page.set_viewport_size({"width": 1280, "height": 900})
    page.goto(url + "/#agents")
    expect(rows(page)).to_have_count(3)
    # Waiting for an answer first, then working, then settled.
    expect(rows(page).nth(0)).to_contain_text("Waiting for you")
    expect(rows(page).nth(0)).to_contain_text("Mac mini")
    expect(rows(page).nth(0)).to_contain_text("Watch")
    expect(rows(page).nth(1)).to_contain_text("Working")
    expect(rows(page).nth(2)).to_contain_text("Cron")
    expect(rows(page).nth(1)).to_contain_text("5m")
    expect(page.locator("#agents-tab-count")).to_have_text("3")
    expect(page.locator("#agents-tab-count")).to_have_class("count attention-count")
    expect(page.locator("#agents-tab-needs")).to_have_text("1 waiting for you")
    choose_option(page.get_by_role("combobox", name="Filter agents by state"), "blocked")
    expect(rows(page)).to_have_count(1)
    choose_option(page.get_by_role("combobox", name="Filter agents by state"), "")
    choose_option(page.get_by_role("combobox", name="Filter agents by who started them"), "cron")
    expect(rows(page)).to_have_count(1)
    expect(rows(page).first).to_contain_text("idle-work")
    choose_option(page.get_by_role("combobox", name="Filter agents by who started them"), "")
    choose_option(page.get_by_role("combobox", name="Filter agents by machine"), "mini")
    expect(rows(page)).to_have_count(1)
    choose_option(page.get_by_role("combobox", name="Filter agents by machine"), "")
    page.locator("#agents-search").fill("working-branch")
    expect(rows(page)).to_have_count(1)
    page.locator("#agents-search").fill("nothing like it")
    expect(page.locator("#agents-list")).to_have_text("No agent matches these filters.")


def test_a_wide_screen_docks_the_viewer_beside_the_list(page, dashboard_site, agents_routes):
    url, _ = dashboard_site
    page.set_viewport_size({"width": 1400, "height": 900})
    page.goto(url + "/#agents")
    rows(page).filter(has_text="working-work").locator(".agents-open").click()
    viewer = page.locator("#agents-detail #ws-viewer")
    expect(viewer).to_be_visible()
    assert not viewer.evaluate("el => el.matches(':modal')")
    expect(page.locator("#agents-placeholder")).to_be_hidden()
    expect(viewer.locator("#ws-viewer-title")).to_have_text("working-work")
    expect(viewer.locator("#ws-viewer-full")).to_be_hidden()
    expect(viewer.locator("#ws-viewer-minimize")).to_be_hidden()
    current = rows(page).filter(has_text="working-work").locator(".agents-open")
    expect(current).to_have_attribute("aria-current", "true")
    # The page stays usable beside it: another agent replaces it in place.
    rows(page).filter(has_text="idle-work").locator(".agents-open").click()
    expect(viewer.locator("#ws-viewer-title")).to_have_text("idle-work")
    expect(current).to_have_attribute("aria-current", "false")
    viewer.locator("#ws-viewer-close").click()
    expect(page.locator("#agents-placeholder")).to_be_visible()
    rows(page).filter(has_text="working-work").locator(".agents-open").click()
    expect(viewer).to_be_visible()
    # Another tab takes the viewer back: it opens as a dialog there again.
    page.locator("#prs-tab").click()
    expect(page.locator("#agents-detail #ws-viewer")).to_have_count(0)
    expect(page.locator("#ws-viewer")).to_be_hidden()
    page.locator("#pr-list tr").first.get_by_role("button", name="Transcript").click()
    assert page.locator("#ws-viewer").evaluate("el => el.matches(':modal')")


def test_a_narrow_screen_opens_the_viewer_as_a_dialog(page, dashboard_site, agents_routes):
    url, _ = dashboard_site
    page.set_viewport_size({"width": 390, "height": 800})
    page.goto(url + "/#agents")
    rows(page).filter(has_text="working-work").locator(".agents-open").click()
    viewer = page.locator("#ws-viewer")
    expect(viewer).to_be_visible()
    assert viewer.evaluate("el => el.matches(':modal')")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


def test_the_terminal_mirrors_the_pane_and_sends_keys_text_and_commands(
    page, dashboard_site, agents_routes
):
    url, _ = dashboard_site
    page.set_viewport_size({"width": 1400, "height": 900})
    page.goto(url + "/#agents")
    rows(page).filter(has_text="working-work").locator(".agents-open").click()
    viewer = page.locator("#ws-viewer")
    viewer.get_by_role("tab", name="Terminal").click()
    screen = viewer.locator(".ws-terminal")
    expect(screen).to_contain_text("bold red on w1:p1")
    # Colours and weight come through; the first line's indentation is kept.
    red = screen.locator("span", has_text="bold red")
    assert red.evaluate("el => getComputedStyle(el).fontWeight") == "700"
    assert "\n  indented" in screen.inner_text()
    expect(viewer.locator("#ws-message")).to_be_hidden()
    viewer.get_by_role("button", name="Send Esc").click()
    viewer.get_by_role("button", name="Send Shift+Tab").click()
    viewer.locator("#ws-terminal-text").fill("yes please")
    viewer.get_by_role("button", name="Type", exact=True).click()
    expect(viewer.locator("#ws-terminal-text")).to_have_value("")
    # Typing into the screen sends what is pressed, until Ctrl+] gives the keyboard back.
    viewer.locator("#ws-terminal-capture").check()
    expect(screen).to_be_focused()
    page.keyboard.type("ab")
    page.keyboard.press("Tab")
    page.keyboard.press("Control+c")
    page.keyboard.press("Control+]")
    expect(viewer.locator("#ws-terminal-capture")).not_to_be_checked()
    page.keyboard.press("x")
    # Keystrokes typed while one is on its way go together; the order is what counts.
    expected = [
        ("key", "esc"),
        ("key", "shift+tab"),
        *[("text", c) for c in "yes please"],
        ("key", "enter"),
        ("text", "a"),
        ("text", "b"),
        ("key", "tab"),
        ("key", "ctrl+c"),
    ]
    for _ in range(50):
        if len(typed(agents_routes)) >= len(expected):
            break
        page.wait_for_timeout(100)
    page.wait_for_timeout(300)  # Nothing after Ctrl+] arrives late.
    assert typed(agents_routes) == expected
    keys = [body for action, body in agents_routes if action == "workspace-keys"]
    assert all(body["workspace"] == "w1" and body["pane"] == "w1:p1" for body in keys)
    viewer.locator("#ws-terminal-command").fill("git status")
    viewer.get_by_role("button", name="Run in new pane").click()
    expect(screen).to_contain_text("bold red on w1:p9")
    expect(viewer.locator("select[aria-label=Pane]")).to_have_value("w1:p9")
    assert ("workspace-shell", {"workspace": "w1", "command": "git status"}) in agents_routes
