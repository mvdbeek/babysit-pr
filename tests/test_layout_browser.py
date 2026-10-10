"""Layout between the phone and desktop layouts, and on touch screens, in real Chromium."""

import pr_supervisor as supervisor
import pytest
from playwright.sync_api import Browser, Page, expect
from test_dashboard_browser import dashboard_site as dashboard_site

pytestmark = pytest.mark.browser

OVERFLOW = "document.documentElement.scrollWidth - window.innerWidth"


@pytest.mark.parametrize("width", [560, 620, 700, 910, 1024])
def test_pages_do_not_scroll_sideways_between_phone_and_desktop(
    page: Page, dashboard_site, width: int
) -> None:
    url, _ = dashboard_site
    page.set_viewport_size({"width": width, "height": 900})
    for tab in ("watcher", "prs"):
        page.goto(f"{url}/#{tab}")
        expect(page.locator(f"#{tab}-panel")).to_be_visible()
        expect(page.locator("#updated")).to_contain_text("Refreshed")
        assert page.evaluate(OVERFLOW) <= 0, f"{tab} overflows at {width}px"


def test_pr_rows_stack_into_cards_until_the_table_fits(page: Page, dashboard_site) -> None:
    url, _ = dashboard_site
    page.goto(f"{url}/#prs")
    rows = page.locator("#pr-list tr")
    expect(rows).to_have_count(2)

    page.set_viewport_size({"width": 820, "height": 900})
    expect(page.locator("#prs-panel .pr-table thead")).to_be_hidden()
    assert rows.first.evaluate("row => getComputedStyle(row).display") == "grid"
    # Every column, CI, dates and actions included, is on screen without sideways scrolling.
    edges = rows.first.locator("td").evaluate_all(
        "cells => cells.map(cell => { const r = cell.getBoundingClientRect(); return [r.left, r.right]; })"
    )
    assert len(edges) == 9
    assert all(left >= 0 and right <= 820 for left, right in edges), edges
    wrap = page.locator("#prs-panel .pr-table-wrap")
    assert wrap.evaluate("el => el.scrollWidth <= el.clientWidth")

    page.set_viewport_size({"width": 1280, "height": 900})
    expect(page.locator("#prs-panel .pr-table thead")).to_be_visible()
    assert rows.first.evaluate("row => getComputedStyle(row).display") == "table-row"


def test_main_leaves_room_above_the_floating_new_task_button(page: Page, dashboard_site) -> None:
    url, _ = dashboard_site
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(url)
    expect(page.locator("#new-task")).to_be_visible()
    padding = page.locator("main").evaluate("el => parseFloat(getComputedStyle(el).paddingBottom)")
    assert padding >= 80
    # The footer, the last thing on the page, ends above the button's top edge.
    page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
    footer = page.locator("footer").bounding_box()
    button = page.locator("#new-task").bounding_box()
    assert footer and button and footer["y"] + footer["height"] <= button["y"]


def test_touch_screens_never_zoom_into_fields(browser: Browser, dashboard_site) -> None:
    url, _ = dashboard_site
    context = browser.new_context(viewport={"width": 1024, "height": 800}, has_touch=True)
    try:
        page = context.new_page()
        page.goto(url)
        assert page.evaluate("matchMedia('(pointer: coarse)').matches")
        fields = page.locator("#watcher-panel input, #watcher-panel select")
        expect(page.locator("#search")).to_be_visible()
        sizes = fields.evaluate_all(
            "els => els.map(el => [el.id, parseFloat(getComputedStyle(el).fontSize)])"
        )
        assert sizes
        assert all(size >= 16 for _, size in sizes), sizes
        # The searchable replacement of the filter is the field a finger actually focuses.
        combobox = page.locator("#watcher-panel [role=combobox]")
        expect(combobox).to_have_count(1)
        assert combobox.evaluate("el => parseFloat(getComputedStyle(el).fontSize)") >= 16
    finally:
        context.close()


def test_modal_dialogs_lock_the_page_behind_them(page: Page, dashboard_site) -> None:
    url, _ = dashboard_site
    page.goto(url)
    overflow = "getComputedStyle(document.documentElement).overflowY"
    assert page.evaluate(overflow) == "visible"
    page.locator("#notifications-toggle").click()
    expect(page.locator("#notifications-dialog")).to_be_visible()
    assert page.evaluate(overflow) == "hidden"
    page.keyboard.press("Escape")
    expect(page.locator("#notifications-dialog")).to_be_hidden()
    assert page.evaluate(overflow) == "visible"


def test_selected_watch_details_stay_in_view_beside_a_long_list(page: Page, dashboard_site) -> None:
    url, home = dashboard_site
    db = supervisor.open_db(home)
    with db:
        for number in range(8):
            supervisor.save_job(
                db,
                {
                    "id": f"extra-{number}",
                    "repo": f"test/extra-{number}",
                    "url": f"https://github.com/test/extra-{number}/pull/{100 + number}",
                    "cwd": "/fixture/worktree",
                    "epoch": 0,
                    "attempts": 0,
                    "max_repairs": 5,
                    "status": "watching",
                    "summary": "Waiting",
                    "branch": None,
                    "pending_reviews": [],
                    "snapshot": {"pr": {"number": 100 + number, "merged": False, "closed": False}},
                },
            )
    db.close()
    page.set_viewport_size({"width": 1440, "height": 500})
    page.goto(url)
    rows = page.locator("#list > *")
    expect(rows).to_have_count(10)
    rows.nth(7).scroll_into_view_if_needed()
    rows.nth(7).click()
    expect(page.locator("#detail .detail-head")).to_be_visible()
    top, bottom = page.locator("#detail").evaluate(
        "el => { const r = el.getBoundingClientRect(); return [r.top, r.bottom]; }"
    )
    assert top >= 0 and bottom <= 500


def test_a_new_sort_in_the_card_layout_returns_to_the_top_of_the_list(
    page: Page, dashboard_site
) -> None:
    url, _ = dashboard_site
    prs = [
        {
            "id": f"pr-{index}",
            "repo": "test/repo",
            "title": f"Change {index}",
            "number": index,
            "url": f"https://github.com/test/repo/pull/{index}",
            "roles": ["author"],
            "ci": "SUCCESS",
            "draft": False,
            "updated_at": f"2026-01-01T00:00:{index:02d}Z",
        }
        for index in range(1, 31)
    ]
    page.route("**/api/prs", lambda route: route.fulfill(json={"prs": prs, "synced_at": 1234}))
    page.emulate_media(reduced_motion="reduce")
    page.set_viewport_size({"width": 820, "height": 900})
    page.goto(f"{url}/#prs")
    rows = page.locator("#pr-list tr")
    expect(rows).to_have_count(30)
    expect(page.locator("#prs-panel .pr-table thead")).to_be_hidden()
    top = "document.querySelector('#prs-panel .pr-table-wrap').getBoundingClientRect().top"
    # With the list's top on screen, a search typed above it leaves the page where it is.
    search = page.get_by_label("Search pull requests")
    page.evaluate("y => window.scrollBy(0, y - 50)", search.bounding_box()["y"])
    scrolled = page.evaluate("window.scrollY")
    assert scrolled > 0
    assert 0 < page.evaluate(top) < 900
    search.fill("Change")
    page.wait_for_timeout(200)
    assert page.evaluate("window.scrollY") == scrolled
    # Read far down the cards, then reverse the sort from the keyboard (focus stayed on
    # the button). The wrapper has nothing to scroll back: the page itself returns.
    direction = page.locator("#pr-sort-direction")
    direction.focus()
    rows.nth(25).scroll_into_view_if_needed()
    expect(rows.first).not_to_be_in_viewport()
    page.keyboard.press("Enter")
    expect(direction).to_have_attribute("aria-label", "Ascending")
    expect(rows.first).to_be_in_viewport()
    expect(rows.first.locator(".pr-title")).to_have_text("Change 1")
    assert -1 < page.evaluate(top) < 900  # Scrolled to its top, give or take a subpixel.
