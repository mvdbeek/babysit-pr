"""Minimizing the workspace viewer to a tray, and restoring it, in real Chromium."""

import copy
import json
from urllib.parse import parse_qs, urlparse

import pytest
from playwright.sync_api import Page, expect
from test_dashboard_browser import COMMENT_DIFF, choose_option
from test_dashboard_browser import dashboard_site as dashboard_site
from test_dashboard_browser import viewer_routes as viewer_routes
from test_dashboard_browser import workspace_routes as workspace_routes
from test_theme_browser import COLOURS, contrast

pytestmark = pytest.mark.browser

SESSIONS = [
    {"id": "s-new", "agent": "claude", "updated": 2, "size": 1, "title": "Newer"},
    {"id": "s-old", "agent": "codex", "updated": 1, "size": 1, "title": "Older"},
]

# The first visible transcript entry and its offset below the sticky header, as the
# reader sees it.
READING = """
dialog => {
    const top = Math.max(
        dialog.querySelector('.ws-viewer-header').getBoundingClientRect().bottom,
        dialog.getBoundingClientRect().top,
    );
    const items = [...dialog.querySelectorAll('.ws-transcript > *')];
    const index = items.findIndex(item => item.getBoundingClientRect().bottom > top);
    return [items[index].textContent, Math.round(items[index].getBoundingClientRect().top - top)];
}
"""


@pytest.fixture
def tray_routes(page, viewer_routes, workspace_routes):
    """Two workspaces, w1 and w2, each with two long sessions and a diff on two bases."""
    _, snapshot, _ = workspace_routes
    second = snapshot["prs"]["pr-two"]["matches"][0]
    second.update(workspace_id="w2", name="Second workspace", path="/fixture/second")
    seen = []

    def transcript(route):
        query = {
            name: values[0] for name, values in parse_qs(urlparse(route.request.url).query).items()
        }
        seen.append(query)
        space = query.get("workspace", "?")
        session = next((s for s in SESSIONS if s["id"] == query.get("session")), SESSIONS[0])
        entries = [
            {"role": "assistant", "text": f"{space} {session['id']} entry {index}"}
            for index in range(60)
        ]
        route.fulfill(
            json={
                "sessions": SESSIONS,
                "session": session,
                "entries": entries,
                "start": 0,
                "total": len(entries),
            }
        )

    def diff(route):
        query = {
            name: values[0] for name, values in parse_qs(urlparse(route.request.url).query).items()
        }
        seen.append(query)
        data = copy.deepcopy(COMMENT_DIFF)
        data["base"] = query.get("base", "origin/main")
        data["bases"] = [{"ref": "origin/main", "ahead": 1}, {"ref": "origin/dev", "ahead": 3}]
        data["files"][0]["lines"] += [f" line {index}" for index in range(200)]
        route.fulfill(json=data)

    page.route("**/api/workspace-transcript?*", transcript)
    page.route("**/api/workspace-diff?*", diff)
    return seen


def row(page: Page, repo: str):
    return page.locator("#pr-list tr").filter(has_text=repo)


def open_view(page: Page, repo: str, tab: str):
    row(page, repo).get_by_role("button", name=tab, exact=True).click()
    viewer = page.locator("#ws-viewer")
    expect(viewer).to_be_visible()
    return viewer


def chips(page: Page):
    return page.get_by_role("region", name="Minimized viewers").locator(".ws-tray-restore")


def scroll_to_entry(viewer, text: str) -> None:
    viewer.get_by_text(text, exact=True).evaluate("el => el.scrollIntoView({block: 'center'})")


def test_minimize_and_restore_keep_tab_session_and_reading_position(
    page, dashboard_site, tray_routes
):
    url, _ = dashboard_site
    page.goto(url + "/#prs")
    viewer = open_view(page, "test/alpha", "Transcript")
    expect(viewer.get_by_text("w1 s-new entry 59", exact=True)).to_be_visible()
    choose_option(viewer.locator("select[aria-label=Session]"), "s-old")
    expect(viewer.get_by_text("w1 s-old entry 0", exact=True)).to_be_attached()
    scroll_to_entry(viewer, "w1 s-old entry 20")
    reading = viewer.evaluate(READING)
    assert "s-old" in reading[0]
    tray = page.get_by_role("region", name="Minimized viewers")
    expect(tray).to_be_hidden()
    viewer.get_by_role("button", name="Minimize workspace viewer").click()
    expect(viewer).to_be_hidden()
    expect(tray).to_be_visible()
    expect(chips(page)).to_have_text(["Transcript Renamed workspace"])
    expect(chips(page).first).to_be_focused()  # The keyboard follows the viewer to its chip.
    page.screenshot(path="reports/viewer-tray-1280.png")
    chips(page).first.click()
    expect(viewer).to_be_visible()
    expect(tray).to_be_hidden()  # The restored viewer left the tray.
    expect(viewer.get_by_role("tab", name="Transcript")).to_have_attribute("aria-selected", "true")
    expect(viewer.locator("select[aria-label=Session]")).to_have_value("s-old")
    assert tray_routes[-1].get("session") == "s-old"
    expect(viewer.get_by_text("w1 s-old entry 20", exact=True)).to_be_in_viewport()
    text, offset = viewer.evaluate(READING)
    assert text == reading[0] and abs(offset - reading[1]) <= 2
    # Following the end is restored as following it, not as a fixed offset.
    viewer.evaluate("el => { el.scrollTop = el.scrollHeight; }")
    viewer.get_by_role("button", name="Minimize workspace viewer").click()
    stored = json.loads(page.evaluate("localStorage.getItem('ws-viewer-tray')"))
    assert stored[0]["position"] == {"end": True}
    chips(page).first.click()
    expect(viewer.get_by_text("w1 s-old entry 59", exact=True)).to_be_in_viewport()


def test_two_minimized_viewers_restore_to_their_own_state(page, dashboard_site, tray_routes):
    url, _ = dashboard_site
    page.goto(url + "/#prs")
    viewer = open_view(page, "test/alpha", "Diff")
    choose_option(viewer.locator("select[aria-label=Base]"), "origin/dev")
    expect(viewer.locator("select[aria-label=Base]")).to_have_value("origin/dev")
    expect(viewer.get_by_text("line 199")).to_be_attached()
    saved = viewer.evaluate("el => { el.scrollTop = el.scrollHeight / 2; return el.scrollTop; }")
    assert saved > 100
    viewer.get_by_role("button", name="Minimize workspace viewer").click()
    viewer = open_view(page, "test/beta", "Transcript")
    expect(viewer.locator("#ws-viewer-title")).to_have_text("Second workspace")
    scroll_to_entry(viewer, "w2 s-new entry 30")
    reading = viewer.evaluate(READING)
    viewer.get_by_role("button", name="Minimize workspace viewer").click()
    # Most recently minimized first.
    expect(chips(page)).to_have_text(["Transcript Second workspace", "Diff Renamed workspace"])
    chips(page).filter(has_text="Renamed workspace").click()
    expect(viewer.locator("#ws-viewer-title")).to_have_text("Renamed workspace")
    expect(viewer.get_by_role("tab", name="Diff")).to_have_attribute("aria-selected", "true")
    expect(viewer.locator("select[aria-label=Base]")).to_have_value("origin/dev")
    assert tray_routes[-1] == {"workspace": "w1", "scope": "branch", "base": "origin/dev"}
    assert viewer.evaluate("el => el.scrollTop") == saved
    expect(chips(page)).to_have_text(["Transcript Second workspace"])
    page.keyboard.press("Escape")  # Escape still closes, without minimizing.
    expect(viewer).to_be_hidden()
    expect(chips(page)).to_have_text(["Transcript Second workspace"])
    chips(page).first.click()
    expect(viewer.locator("#ws-viewer-title")).to_have_text("Second workspace")
    expect(viewer.get_by_role("tab", name="Transcript")).to_have_attribute("aria-selected", "true")
    assert viewer.evaluate(READING)[0] == reading[0]
    expect(page.get_by_role("region", name="Minimized viewers")).to_be_hidden()


def test_minimizing_a_workspace_again_replaces_its_chip_and_dismiss_keeps_the_rest(
    page, dashboard_site, tray_routes
):
    url, _ = dashboard_site
    page.goto(url + "/#prs")
    viewer = open_view(page, "test/alpha", "Diff")
    viewer.get_by_role("button", name="Minimize workspace viewer").click()
    viewer = open_view(page, "test/beta", "Diff")
    viewer.get_by_role("button", name="Minimize workspace viewer").click()
    viewer = open_view(page, "test/alpha", "Transcript")
    viewer.get_by_role("button", name="Minimize workspace viewer").click()
    expect(chips(page)).to_have_text(["Transcript Renamed workspace", "Diff Second workspace"])
    tray = page.get_by_role("region", name="Minimized viewers")
    tray.get_by_role("button", name="Remove Transcript Renamed workspace from the tray").click()
    expect(chips(page)).to_have_text(["Diff Second workspace"])
    expect(chips(page).first).to_be_focused()
    stored = json.loads(page.evaluate("localStorage.getItem('ws-viewer-tray')"))
    assert [item["entry"]["workspace"] for item in stored] == ["w2"]


def test_the_tray_survives_a_reload(page, dashboard_site, tray_routes):
    url, _ = dashboard_site
    page.goto(url + "/#prs")
    viewer = open_view(page, "test/alpha", "Transcript")
    choose_option(viewer.locator("select[aria-label=Session]"), "s-old")
    expect(viewer.get_by_text("w1 s-old entry 0", exact=True)).to_be_attached()
    viewer.get_by_role("button", name="Minimize workspace viewer").click()
    viewer = open_view(page, "test/beta", "Diff")
    viewer.get_by_role("button", name="Minimize workspace viewer").click()
    page.reload()
    expect(chips(page)).to_have_text(["Diff Second workspace", "Transcript Renamed workspace"])
    chips(page).filter(has_text="Renamed workspace").click()
    expect(viewer.locator("#ws-viewer-title")).to_have_text("Renamed workspace")
    expect(viewer.locator("select[aria-label=Session]")).to_have_value("s-old")
    page.reload()
    expect(chips(page)).to_have_text(["Diff Second workspace"])


def test_a_stale_entry_is_dropped_with_a_note(page, dashboard_site, tray_routes):
    url, _ = dashboard_site
    page.goto(url + "/#prs")
    viewer = open_view(page, "test/alpha", "Transcript")
    viewer.get_by_role("button", name="Minimize workspace viewer").click()
    viewer = open_view(page, "test/beta", "Transcript")
    choose_option(viewer.locator("select[aria-label=Session]"), "s-old")
    expect(viewer.get_by_text("w2 s-old entry 0", exact=True)).to_be_attached()
    viewer.get_by_role("button", name="Minimize workspace viewer").click()
    # w1 closed meanwhile; w2 is still open, but its saved session is no longer recorded.
    page.route(
        "**/api/workspace-transcript?workspace=w1*",
        lambda route: route.fulfill(
            status=400, json={"error": "That herdr workspace is not open; refresh"}
        ),
    )
    page.route(
        "**/api/workspace-transcript?workspace=w2*",
        lambda route: route.fulfill(
            json={
                "sessions": SESSIONS[:1],
                "session": SESSIONS[0],
                "entries": [{"role": "assistant", "text": "w2 s-new only"}],
                "start": 0,
                "total": 1,
            }
        ),
    )
    page.reload()
    tray = page.get_by_role("region", name="Minimized viewers")
    chips(page).filter(has_text="Renamed workspace").click()
    expect(tray.get_by_role("status")).to_have_text(
        "Renamed workspace is no longer available; removed it from the tray."
    )
    expect(viewer).to_be_hidden()
    expect(page.get_by_role("alert").filter(has_text="not open")).to_have_count(0)
    expect(chips(page)).to_have_text(["Transcript Second workspace"])
    chips(page).first.click()
    expect(viewer.get_by_text("w2 s-new only", exact=True)).to_be_visible()
    expect(page.locator("#ws-viewer-error")).to_be_empty()
    expect(viewer.locator("select[aria-label=Session]")).to_have_value("s-new")


def test_the_composer_draft_is_intact_after_restore(page, dashboard_site, tray_routes):
    url, _ = dashboard_site
    page.goto(url + "/#prs")
    viewer = open_view(page, "test/alpha", "Transcript")
    viewer.get_by_label("Message the agent").fill("Keep the deprecation alias.")
    viewer.get_by_role("button", name="Minimize workspace viewer").click()
    page.reload()
    chips(page).first.click()
    expect(viewer.get_by_label("Message the agent")).to_have_value("Keep the deprecation alias.")
    drafts = page.evaluate(
        "Object.keys(localStorage).filter(key => key.startsWith('ws-viewer-draft:'))"
    )
    assert drafts == ["ws-viewer-draft:w1"]
    stored = json.loads(page.evaluate("localStorage.getItem('ws-viewer-draft:w1')"))
    assert stored["message"] == "Keep the deprecation alias."


def test_polling_stops_while_minimized_and_resumes_on_restore(page, dashboard_site, tray_routes):
    url, _ = dashboard_site
    agents = []
    page.on(
        "request",
        lambda request: "/api/workspace-agents" in request.url and agents.append(request.url),
    )
    page.clock.install()
    page.goto(url + "/#prs")
    viewer = open_view(page, "test/alpha", "Transcript")
    expect(viewer.get_by_text("w1 s-new entry 59", exact=True)).to_be_visible()
    page.clock.run_for(11000)
    page.wait_for_timeout(100)
    assert any("after" in query for query in tray_routes)  # It polls while open.
    viewer.get_by_role("button", name="Minimize workspace viewer").click()
    page.wait_for_timeout(100)
    views, polled = len(tray_routes), len(agents)
    assert polled
    page.clock.run_for(65000)
    page.evaluate("window.dispatchEvent(new Event('focus'))")
    page.wait_for_timeout(200)
    assert len(tray_routes) == views and len(agents) == polled
    chips(page).first.click()
    expect(viewer.get_by_text("w1 s-new entry 59", exact=True)).to_be_visible()
    restored = len(tray_routes)
    page.clock.run_for(11000)
    page.wait_for_timeout(200)
    assert any("after" in query for query in tray_routes[restored:])


@pytest.mark.parametrize("width", [320, 390, 1280])
def test_the_tray_fits_a_phone_and_both_schemes(page, dashboard_site, tray_routes, width):
    url, _ = dashboard_site
    page.set_viewport_size({"width": width, "height": 844})
    page.goto(url + "/#prs")
    viewer = open_view(page, "test/alpha", "Transcript")
    viewer.get_by_label("Message the agent").fill("Rerun the tests.")
    viewer.get_by_role("button", name="Minimize workspace viewer").click()
    # A crowded tray: more chips than fit, each another (made-up) watch.
    stored = json.loads(page.evaluate("localStorage.getItem('ws-viewer-tray')"))
    for index in range(7):
        stored.append(
            {
                "id": f"watch-{index}",
                "entry": {"watch": f"watch-{index}", "name": f"Parked watch number {index}"},
                "mode": "diff",
                "scope": "branch",
                "base": None,
                "session": None,
                "position": None,
            }
        )
    page.evaluate("value => localStorage.setItem('ws-viewer-tray', value)", json.dumps(stored))
    page.reload()
    tray = page.get_by_role("region", name="Minimized viewers")
    expect(chips(page)).to_have_count(8)
    assert page.evaluate(
        "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
    )
    box = tray.bounding_box()
    assert box["x"] >= 16 and box["x"] + box["width"] <= width - 16
    assert box["y"] + box["height"] <= 844 - 16
    holder = tray.locator(".ws-tray-chips")
    assert holder.evaluate("el => el.scrollWidth > el.clientWidth")  # Scrolls sideways.
    new_task = page.locator("#new-task")
    if width <= 540:
        expect(new_task).to_be_visible()
        fab = new_task.bounding_box()
        assert box["x"] + box["width"] <= fab["x"] or box["y"] + box["height"] <= fab["y"]
    # The end of the page can still be scrolled clear of the tray.
    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    footer = page.locator("footer").bounding_box()
    assert footer["y"] + footer["height"] <= tray.bounding_box()["y"]
    page.screenshot(path=f"reports/viewer-tray-crowded-{width}.png")
    chips(page).filter(has_text="Renamed workspace").click()
    send = viewer.get_by_role("button", name="Send", exact=True)
    viewer.locator("#ws-message").scroll_into_view_if_needed()
    expect(send).to_be_in_viewport()
    assert send.evaluate("""el => {
        const r = el.getBoundingClientRect();
        return el.contains(document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2));
    }""")
    expect(viewer.get_by_label("Message the agent")).to_have_value("Rerun the tests.")
    header = viewer.locator(".ws-viewer-actions")
    assert header.evaluate("el => el.scrollWidth <= el.clientWidth")
    expect(viewer.get_by_role("button", name="Minimize workspace viewer")).to_be_in_viewport()
    page.screenshot(path=f"reports/viewer-minimize-{width}.png")
    page.keyboard.press("Escape")
    for scheme in ("light", "dark"):
        page.emulate_media(color_scheme=scheme)
        for selector in (".ws-tray-name", ".ws-tray-tab", ".ws-tray-dismiss"):
            text, background = page.evaluate(COLOURS, selector)
            assert contrast(text, background) >= 4.5, (scheme, selector)
    page.screenshot(path=f"reports/viewer-tray-dark-{width}.png")


def test_a_worker_entry_round_trips(page, dashboard_site, tray_routes):
    url, _ = dashboard_site
    parked = {
        "id": "m:chonk:/srv/w5",
        "entry": {
            "id": "m:chonk:/srv/w5",
            "key": "m:chonk:/srv/w5",
            "workspace": "chonk/w5",
            "machineLabel": "chonk",
            "name": "Worker checkout",
            "links": [],
        },
        "mode": "transcript",
        "scope": "branch",
        "base": None,
        "session": "s-old",
        "position": None,
    }
    page.goto(url + "/#prs")
    page.evaluate("value => localStorage.setItem('ws-viewer-tray', value)", json.dumps([parked]))
    page.reload()
    # A worker not answering is not a checkout gone: the chip stays, saying why.
    offline = {"down": True}

    def worker(route):
        if offline["down"]:
            route.fulfill(status=400, json={"error": "chonk did not answer within 20 seconds"})
        else:
            route.fallback()

    page.route("**/api/workspace-transcript?key=*", worker)
    tray = page.get_by_role("region", name="Minimized viewers")
    viewer = page.locator("#ws-viewer")
    chips(page).first.click()
    expect(tray.get_by_role("status")).to_have_text(
        "Could not open Worker checkout: chonk did not answer within 20 seconds"
    )
    expect(viewer).to_be_hidden()
    expect(chips(page)).to_have_text(["Transcript Worker checkout"])
    expect(chips(page).first).to_be_focused()
    offline["down"] = False
    chips(page).first.click()
    expect(viewer.locator("#ws-viewer-title")).to_have_text("Worker checkout")
    expect(page.locator("#ws-message-readonly")).to_have_text(
        "Not available for workspaces on chonk yet"
    )
    expect(chips(page)).to_have_count(0)
    assert tray_routes[-1] == {"key": "m:chonk:/srv/w5", "session": "s-old"}
    viewer.get_by_role("button", name="Minimize workspace viewer").click()
    stored = json.loads(page.evaluate("localStorage.getItem('ws-viewer-tray')"))
    assert stored[0]["entry"] == parked["entry"] and stored[0]["id"] == parked["id"]


def test_another_tab_sees_the_tray_change(page, dashboard_site, tray_routes):
    url, _ = dashboard_site
    page.goto(url + "/#prs")
    other = page.context.new_page()
    # Routes belong to a page: the other tab is answered the same way.
    session = SESSIONS[0]
    transcript = {"sessions": [session], "session": session, "entries": [], "start": 0, "total": 0}
    other.route("**/api/workspace-transcript?*", lambda route: route.fulfill(json=transcript))
    other.route("**/api/workspace-diff?*", lambda route: route.fulfill(json=COMMENT_DIFF))
    other.route(
        "**/api/workspace-agents?*",
        lambda route: route.fulfill(
            json={"agents": [], "sessions": [], "path": "/fixture/checkout"}
        ),
    )
    other.goto(url + "/#prs")
    viewer = open_view(page, "test/alpha", "Diff")
    viewer.get_by_role("button", name="Minimize workspace viewer").click()
    expect(chips(other)).to_have_text(["Diff Renamed workspace"])
    # Restored there, it leaves this tab's tray too.
    chips(other).first.click()
    expect(other.locator("#ws-viewer-meta")).to_contain_text("1 file changed")
    expect(page.get_by_role("region", name="Minimized viewers")).to_be_hidden()
    other.close()


def test_a_chip_whose_workspace_id_now_names_another_checkout_is_dropped(
    page, dashboard_site, tray_routes, viewer_routes
):
    url, _ = dashboard_site
    page.goto(url + "/#prs")
    viewer = open_view(page, "test/alpha", "Transcript")
    expect(page.locator("#ws-message-agent")).not_to_have_text("Looking for agents…")
    viewer.get_by_role("button", name="Minimize workspace viewer").click()
    stored = json.loads(page.evaluate("localStorage.getItem('ws-viewer-tray')"))
    assert stored[0]["path"] == "/fixture/checkout"
    viewer = open_view(page, "test/beta", "Transcript")
    viewer.get_by_role("button", name="Minimize workspace viewer").click()
    # herdr restarted: w1 is now another checkout. While its old one is remembered, the
    # dashboard says so; once forgotten, only the path tells.
    viewer_routes["path"] = "/fixture/elsewhere"
    page.route(
        "**/api/workspace-transcript?workspace=w2*",
        lambda route: route.fulfill(
            status=400, json={"error": "That herdr workspace now shows another checkout; refresh"}
        ),
    )
    tray = page.get_by_role("region", name="Minimized viewers")
    chips(page).filter(has_text="Renamed workspace").click()
    expect(tray.get_by_role("status")).to_have_text(
        "Renamed workspace now names another checkout; removed it from the tray."
    )
    expect(viewer).to_be_hidden()
    chips(page).filter(has_text="Second workspace").click()
    expect(tray.get_by_role("status")).to_have_text(
        "Second workspace is no longer available; removed it from the tray."
    )
    expect(chips(page)).to_have_count(0)
    expect(viewer).to_be_hidden()


def test_saved_chips_are_checked_and_capped_on_load(page, dashboard_site, tray_routes):
    url, _ = dashboard_site

    def chip(index, **changes):
        item = {
            "id": f"watch:{index}",
            "entry": {"watch": f"watch-{index}", "name": f"Watch {index}", "links": []},
            "mode": "diff",
            "scope": "branch",
            "base": None,
            "session": None,
            "position": None,
        }
        item.update(changes)
        return item

    malformed = [
        chip(100, entry="watch-100"),
        chip(101, entry={"name": "No target"}),
        chip(102, entry={"watch": "w", "name": "Bad links", "links": "nope"}),
        chip(103, entry={"watch": ["w"], "name": "Bad watch"}),
        chip(104, mode="bogus"),
        chip(105, scope="everything"),
        chip(106, base=7),
        chip(107, session={"id": "s"}),
        chip(108, id=108),
        "not a chip",
    ]
    saved = malformed + [chip(index) for index in range(15)]
    page.goto(url + "/#prs")
    page.evaluate("value => localStorage.setItem('ws-viewer-tray', value)", json.dumps(saved))
    page.reload()
    expect(chips(page)).to_have_count(12)
    expect(chips(page)).to_have_text([f"Diff Watch {index}" for index in range(12)])


def test_a_restored_diff_whose_base_is_gone_falls_back_to_the_default(
    page, dashboard_site, tray_routes
):
    url, _ = dashboard_site
    page.goto(url + "/#prs")
    viewer = open_view(page, "test/alpha", "Diff")
    choose_option(viewer.locator("select[aria-label=Base]"), "origin/dev")
    expect(viewer.locator("select[aria-label=Base]")).to_have_value("origin/dev")
    viewer.get_by_role("button", name="Minimize workspace viewer").click()

    def gone(route):
        if "base=origin%2Fdev" in route.request.url:
            route.fulfill(status=400, json={"error": "Choose one of the listed base branches"})
        else:
            route.fallback()

    page.route("**/api/workspace-diff?*", gone)
    chips(page).first.click()
    expect(viewer.locator("select[aria-label=Base]")).to_have_value("origin/main")
    expect(page.locator("#ws-viewer-error")).to_be_empty()
    expect(page.locator("#ws-viewer-meta")).to_contain_text("1 file changed")


def test_a_followed_reopen_replaces_its_chip(page, dashboard_site, tray_routes, viewer_routes):
    url, _ = dashboard_site
    reopened = {"workspace": None}

    def agents(route):
        route.fulfill(
            json={
                "agents": viewer_routes["agents"],
                "sessions": viewer_routes["sessions"],
                "path": viewer_routes["path"],
                "workspace": reopened["workspace"],
            }
        )

    page.route("**/api/workspace-agents?*", agents)
    page.goto(url + "/#prs")
    viewer = open_view(page, "test/alpha", "Transcript")
    expect(page.locator("#ws-message-agent")).not_to_have_text("Looking for agents…")
    viewer.get_by_role("button", name="Minimize workspace viewer").click()
    # The agent was reopened in w9: the viewer follows it there.
    reopened["workspace"] = "w9"
    viewer = open_view(page, "test/alpha", "Diff")
    expect(page.locator("#ws-message-agent")).not_to_have_text("Looking for agents…")
    viewer.get_by_role("button", name="Minimize workspace viewer").click()
    expect(chips(page)).to_have_text(["Diff Renamed workspace"])
