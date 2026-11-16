"""The Agents tab and the viewer's Terminal view in real Chromium, against mocked APIs."""

import re
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
    agent(
        "mini/w1:p1",
        "blocked",
        agent="codex",
        machine="mini",
        machine_label="Mac mini",
        origin="watch",
        origin_label="Watch w-7",
        origin_url="https://github.com/o/r/pull/7",
        activity=NOW - 60,
    ),
    agent("w2:p1", "idle", origin="cron", origin_label="Cron: nightly", activity=NOW - 7200),
    # Its transcript's time moves with every poll; it is at work, which says enough.
    agent("w1:p1", "working", activity=NOW - 9000),
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


def test_agents_are_listed_by_last_activity_with_a_badge_and_filters(
    page, dashboard_site, agents_routes
):
    url, _ = dashboard_site
    page.set_viewport_size({"width": 1280, "height": 900})
    page.goto(url + "/#agents")
    expect(rows(page)).to_have_count(3)
    # The one at work first, then the most recently active, whichever machine it is on.
    expect(rows(page).nth(0)).to_contain_text("Working")
    expect(rows(page).nth(0).locator(".agents-age")).to_have_text("5m")
    expect(rows(page).nth(0).locator(".agents-age")).to_have_attribute(
        "title", re.compile("^In this state since ")
    )
    expect(rows(page).nth(1)).to_contain_text("Waiting for you")
    expect(rows(page).nth(1)).to_contain_text("Mac mini")
    expect(rows(page).nth(1)).to_contain_text("Watch")
    expect(rows(page).nth(1).locator(".agents-age")).to_have_text("1m")
    expect(rows(page).nth(1).locator(".agents-age")).to_have_attribute(
        "title", re.compile("^Last active ")
    )
    expect(rows(page).nth(2)).to_contain_text("Cron")
    expect(rows(page).nth(2).locator(".agents-age")).to_have_text("2h")
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


@pytest.mark.parametrize("width", [390, 768, 950, 1100, 1280, 1440])
def test_each_filter_keeps_its_name_beside_it_and_stays_inside_the_panel(
    page, dashboard_site, agents_routes, width
):
    url, _ = dashboard_site
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(url + "/#agents")
    expect(rows(page)).to_have_count(3)

    def middle(locator):
        box = locator.bounding_box()
        return box["y"] + box["height"] / 2

    search = page.locator("#agents-search")
    panel = page.locator("#agents-panel .toolbar").bounding_box()
    controls = {
        "Machine": page.get_by_role("combobox", name="Filter agents by machine"),
        "State": page.locator("#agents-state"),
        "Started by": page.locator("#agents-origin"),
    }
    for name, control in controls.items():
        box = control.bounding_box()
        label = page.locator("#agents-panel .filters label", has_text=name)
        # The name is on the control's line, before it, not stacked above it.
        text_middle = label.evaluate(
            "(label, name) => { const range = document.createRange();"
            " range.selectNodeContents([...label.childNodes].find(n => n.nodeType === 3"
            " && n.textContent.trim() === name)); const r = range.getBoundingClientRect();"
            " return r.y + r.height / 2; }",
            name,
        )
        assert text_middle == pytest.approx(middle(control), abs=3), name
        assert label.bounding_box()["x"] < box["x"], name
        assert box["x"] + box["width"] <= panel["x"] + panel["width"] + 1, name
        if width >= 1100:  # Room for all four on one line.
            assert middle(control) == pytest.approx(middle(search), abs=2), name
            assert box["height"] == pytest.approx(search.bounding_box()["height"], abs=2), name
    page.screenshot(path=f"reports/layout-agent-filters-{width}.png")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


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


def long_transcript(page):
    session = {"id": "s1", "agent": "claude", "updated": 1, "size": 1, "title": "Go"}
    entries = [{"role": "assistant", "text": f"Step {n} is done."} for n in range(60)]
    page.route(
        "**/api/workspace-transcript?*",
        lambda route: route.fulfill(
            json={
                "sessions": [session],
                "session": session,
                "entries": entries,
                "start": 0,
                "total": len(entries),
            }
        ),
    )


def bottom(locator):
    box = locator.bounding_box()
    return box["y"] + box["height"]


def test_the_docked_viewer_keeps_its_composer_in_the_window(page, dashboard_site, agents_routes):
    url, _ = dashboard_site
    long_transcript(page)
    # Full screen is a choice about the dialog: docked, the viewer is still a pane.
    page.add_init_script("localStorage.setItem('ws-viewer-full', '1')")
    page.set_viewport_size({"width": 1400, "height": 800})
    page.goto(url + "/#agents")
    rows(page).filter(has_text="working-work").locator(".agents-open").click()
    viewer = page.locator("#agents-detail #ws-viewer")
    composer = viewer.locator("#ws-message")
    expect(viewer.get_by_text("Step 59 is done.")).to_be_visible()
    expect(composer).to_be_visible()

    def shows_newest():
        viewer.evaluate(
            "el => new Promise(done => { const check = () =>"
            " el.scrollHeight - el.scrollTop - el.clientHeight < 2"
            " ? done() : requestAnimationFrame(check); check(); })"
        )

    def wheel_over(locator, delta):
        box = locator.bounding_box()
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        page.mouse.wheel(0, delta)

    # Before the page has scrolled, the viewer starts under the tabs and filters and ends at
    # the window's lower edge, with the composer in it.
    assert page.evaluate("scrollY") == 0
    start = viewer.bounding_box()
    assert start["y"] > 100
    assert bottom(viewer) == pytest.approx(792, abs=1)
    assert bottom(composer) <= 792
    shows_newest()
    page.screenshot(path="reports/layout-agent-docked-top.png")
    # Scrolling on past the transcript's end scrolls the page, which gives the viewer the
    # rest of the window; it still shows the newest entries.
    wheel_over(viewer, 2000)
    page.wait_for_function("() => scrollY + innerHeight >= document.documentElement.scrollHeight")
    page.wait_for_function(
        "() => document.getElementById('ws-viewer').getBoundingClientRect().top > 0"
    )
    grown = viewer.bounding_box()
    assert grown["y"] == pytest.approx(8, abs=1)
    assert grown["height"] > start["height"] + 50
    assert bottom(viewer) <= 792
    assert bottom(composer) <= 792
    shows_newest()
    # Back at the top of the page the viewer is as it was, and has not lost its place.
    wheel_over(page.locator("#agents-list"), -2000)
    page.wait_for_function("() => scrollY === 0")
    expect(viewer).to_have_js_property("offsetHeight", round(start["height"]))
    assert viewer.bounding_box() == pytest.approx(start, abs=1)
    assert bottom(composer) <= 792
    shows_newest()


def test_the_docked_viewer_leaves_a_laptop_window_room_for_the_transcript(
    page, dashboard_site, agents_routes, viewer_routes
):
    url, _ = dashboard_site
    long_transcript(page)
    page.route(
        "**/api/workspace-agents?*",
        lambda route: route.fulfill(
            json={
                "agents": viewer_routes["agents"],
                "sessions": viewer_routes["sessions"],
                "path": viewer_routes["path"],
                "url": "https://collie.example/w1",
            }
        ),
    )
    page.set_viewport_size({"width": 1280, "height": 720})
    page.goto(url + "/#agents")
    # With nothing to report, the list starts under its filters: the page's header already
    # says when the dashboard last refreshed.
    expect(rows(page)).to_have_count(3)
    expect(page.locator("#agents-status")).to_be_hidden()
    rows(page).filter(has_text="working-work").locator(".agents-open").click()
    viewer = page.locator("#agents-detail #ws-viewer")
    header = viewer.locator(".ws-viewer-header")
    composer = viewer.locator("#ws-message")
    expect(viewer.get_by_text("Step 59 is done.")).to_be_visible()
    expect(viewer.get_by_label("Agent", exact=True)).to_be_visible()
    expect(viewer.locator(".ws-live-state")).to_have_attribute("data-live", "unknown")
    # The name and its links share the buttons' line, and the tabs the agent's status.
    title = viewer.locator("#ws-viewer-title").bounding_box()
    link = viewer.locator("#ws-viewer-links a").first.bounding_box()
    close = viewer.locator("#ws-viewer-close").bounding_box()
    assert title["y"] < link["y"] + link["height"] and link["y"] < title["y"] + title["height"]
    assert close["y"] < title["y"] + title["height"] and title["y"] < close["y"] + close["height"]
    tabs = viewer.get_by_role("tab", name="Transcript").bounding_box()
    strip = viewer.locator("#ws-live").bounding_box()
    assert strip["x"] > tabs["x"] + tabs["width"]
    assert strip["y"] == pytest.approx(tabs["y"], abs=2)
    # Before the page has scrolled, the pinned header and composer leave the transcript
    # several entries of the window, with every option of the composer still in it.
    assert page.evaluate("scrollY") == 0
    assert bottom(composer) <= 712
    for option in ("#ws-message-send", ".attach-button", "#ws-message-quit"):
        assert bottom(viewer.locator(option)) <= bottom(composer)
    room = composer.bounding_box()["y"] - bottom(header)
    assert room >= 220, room
    page.screenshot(path="reports/layout-agent-docked-laptop.png")


def test_typing_a_message_leaves_the_transcript_where_the_reader_scrolled_it(
    page, dashboard_site, agents_routes
):
    url, _ = dashboard_site
    long_transcript(page)
    page.set_viewport_size({"width": 1280, "height": 720})
    page.goto(url + "/#agents")
    rows(page).filter(has_text="working-work").locator(".agents-open").click()
    viewer = page.locator("#agents-detail #ws-viewer")
    composer = viewer.locator("#ws-message")
    expect(viewer.get_by_text("Step 59 is done.")).to_be_visible()
    # Reading further up, the reader starts a message: the field is pinned to the viewer's
    # end, and neither a key nor the field growing by a line moves what is being read.
    read = viewer.get_by_text("Step 30 is done.")
    read.evaluate("el => el.scrollIntoView({ block: 'center' })")
    expect(read).to_be_in_viewport()
    at = viewer.evaluate("el => el.scrollTop")
    place = read.bounding_box()
    field = viewer.get_by_label("Message the agent")
    field.click()
    height = field.bounding_box()["height"]
    for line in ("Please", "rerun", "the", "tests"):
        page.keyboard.type(line)
        page.keyboard.press("Enter")
    assert field.bounding_box()["height"] > height + 30
    assert viewer.evaluate("el => el.scrollTop") == at
    assert read.bounding_box() == place
    # What takes the focus under the composer is still scrolled clear of it.
    covered = viewer.evaluate(
        "el => { const edge = el.querySelector('#ws-message').getBoundingClientRect().top;"
        " return [...el.querySelectorAll('.ws-msg')].findIndex("
        "entry => entry.getBoundingClientRect().bottom > edge + 10); }"
    )
    behind = viewer.locator(".ws-msg").nth(covered)
    behind.evaluate("el => { el.tabIndex = 0; }")
    assert bottom(behind) > composer.bounding_box()["y"]
    behind.focus()
    assert bottom(behind) <= composer.bounding_box()["y"]


def test_the_docked_viewer_ends_above_the_tray_of_minimized_viewers(
    page, dashboard_site, agents_routes, workspace_routes
):
    url, _ = dashboard_site
    long_transcript(page)
    _, snapshot, _ = workspace_routes
    snapshot["prs"]["pr-two"]["matches"][0].update(workspace_id="w2", name="Second workspace")
    page.set_viewport_size({"width": 1000, "height": 1000})
    page.goto(url + "/#prs")
    tray = page.locator("#ws-tray")
    for count, repo in enumerate(["test/alpha", "test/beta"], 1):
        row = page.locator("#pr-list tr").filter(has_text=repo)
        row.get_by_role("button", name="Transcript", exact=True).click()
        page.locator("#ws-viewer-minimize").click()
        expect(tray.locator(".ws-tray-chip")).to_have_count(count)
    page.locator("#agents-tab").click()
    rows(page).filter(has_text="working-work").locator(".agents-open").click()
    viewer = page.locator("#agents-detail #ws-viewer")
    composer = viewer.locator("#ws-message")
    expect(composer).to_be_visible()
    # The tray floats over the viewer's corner: the viewer, composer included, ends above it.
    over = tray.bounding_box()
    assert over["x"] + over["width"] > viewer.bounding_box()["x"]
    ends_at = (
        "edge => Math.abs("
        "document.getElementById('ws-viewer').getBoundingClientRect().bottom - edge) < 1"
    )
    page.wait_for_function(ends_at, arg=over["y"] - 8)
    assert bottom(composer) <= over["y"] - 8
    page.screenshot(path="reports/layout-agent-docked-tray.png")
    # Without the tray it has the window to its lower edge again.
    for dismiss in tray.locator(".ws-tray-dismiss").all()[::-1]:
        dismiss.click()
    expect(tray).to_be_hidden()
    page.wait_for_function(ends_at, arg=992)


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
