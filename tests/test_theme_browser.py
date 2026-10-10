"""Colour scheme, safe areas and touch targets of the dashboard shell, in real Chromium."""

import pytest
from playwright.sync_api import Browser, Page, Playwright, expect
from test_dashboard_browser import dashboard_site as dashboard_site

pytestmark = pytest.mark.browser

# Effective text and background colour of the first match, backgrounds composited up the tree.
COLOURS = """
selector => {
    const element = document.querySelector(selector);
    if (!element) return null;
    const parse = value => {
        const [r, g, b, a = 1] = value.match(/[\\d.]+/g).map(Number);
        return [r, g, b, a];
    };
    let background = [255, 255, 255];
    const chain = [];
    for (let node = element; node; node = node.parentElement) chain.unshift(node);
    for (const node of chain) {
        const [r, g, b, a] = parse(getComputedStyle(node).backgroundColor);
        background = [r, g, b].map((channel, i) => channel * a + background[i] * (1 - a));
    }
    const [r, g, b, a] = parse(getComputedStyle(element).color);
    const text = [r, g, b].map((channel, i) => channel * a + background[i] * (1 - a));
    return [text, background];
}
"""


def luminance(rgb) -> float:
    def linear(channel: float) -> float:
        channel /= 255
        return channel / 12.92 if channel <= 0.03928 else ((channel + 0.055) / 1.055) ** 2.4

    r, g, b = (linear(channel) for channel in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(first, second) -> float:
    high, low = sorted((luminance(first), luminance(second)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def to_hex(rgb) -> str:
    return "#" + "".join(f"{round(channel):02x}" for channel in rgb)


def open_page(browser: Browser, url: str, **options) -> tuple:
    context = browser.new_context(**options)
    page = context.new_page()
    page.goto(url)
    expect(page.locator("#updated")).to_contain_text("Refreshed")
    return context, page


def test_dark_scheme_is_dark_and_every_main_surface_is_readable(
    browser: Browser, dashboard_site
) -> None:
    url, _ = dashboard_site
    context, page = open_page(
        browser, url, color_scheme="dark", viewport={"width": 1280, "height": 900}
    )
    try:
        assert page.evaluate("getComputedStyle(document.documentElement).colorScheme") == (
            "light dark"
        )
        body = page.evaluate(COLOURS, "body")
        assert luminance(body[1]) < 0.05, f"page background {to_hex(body[1])} is not dark"
        assert luminance(body[0]) > 0.5, f"page text {to_hex(body[0])} is not light"

        # Text of the watcher tab: header, metrics, watch cards, badges, tabs and fields.
        watcher = {
            "body": 4.5,
            "header h1": 4.5,
            "header p": 4.5,
            "#updated": 4.5,
            "#health": 4.5,
            ".metrics span": 4.5,
            ".metrics strong": 4.5,
            "#list .watch-title": 4.5,
            "#list .branch": 4.5,
            "#list .watch-summary": 4.5,
            "#list .watch-bottom": 4.5,
            "#list .badge": 4.5,
            ".page-tabs [aria-selected=true]": 4.5,
            ".page-tabs [aria-selected=false]": 4.5,
            "#search": 4.5,
            "#new-task": 4.5,
            "footer": 4.5,
        }
        failures = {}
        for selector, minimum in watcher.items():
            colours = page.evaluate(COLOURS, selector)
            assert colours, f"{selector} is not on the page"
            ratio = contrast(*colours)
            if ratio < minimum:
                failures[selector] = round(ratio, 2)
        page.goto(f"{url}/#prs")
        expect(page.locator("#pr-list tr")).to_have_count(2)
        for selector in (
            ".pr-table th",
            "#pr-list .pr-title",
            "#pr-list .pr-meta",
            "#pr-list .pr-readiness .badge",
            "#pr-list .pr-actions button",
            "#pr-search",
        ):
            ratio = contrast(*page.evaluate(COLOURS, selector))
            if ratio < 4.5:
                failures[selector] = round(ratio, 2)
        assert not failures, f"text below 4.5:1 in dark mode: {failures}"

        # The keyboard focus ring and a field's edge are user-interface components: 3:1.
        paper = page.evaluate(COLOURS, "#pr-list tr")[1]
        for token in ("--focus", "--field-line", "--brand"):
            value = page.evaluate(
                "token => { const probe = document.createElement('i');"
                " probe.style.color = `var(${token})`; document.body.append(probe);"
                " const color = getComputedStyle(probe).color; probe.remove(); return color; }",
                token,
            )
            rgb = [float(part) for part in value.removeprefix("rgb(").removesuffix(")").split(",")]
            assert contrast(rgb, paper) >= 3, f"{token} {value} on {to_hex(paper)}"
    finally:
        context.close()


def test_theme_colour_matches_the_header_in_both_schemes(browser: Browser, dashboard_site) -> None:
    url, _ = dashboard_site
    for scheme in ("light", "dark"):
        context, page = open_page(browser, url, color_scheme=scheme)
        try:
            theme = page.evaluate(
                """() => [...document.querySelectorAll('meta[name=theme-color]')]
                    .find(meta => matchMedia(meta.media).matches).content"""
            )
            body, header = (
                page.evaluate(COLOURS + "", selector)[1] for selector in ("body", "header")
            )
            # The browser's own chrome continues the page: the page in light, the header in dark.
            assert theme == to_hex(body if scheme == "light" else header), scheme
        finally:
            context.close()


def test_light_scheme_keeps_its_palette(browser: Browser, dashboard_site) -> None:
    url, _ = dashboard_site
    context, page = open_page(browser, url, color_scheme="light")
    try:
        tokens = page.evaluate(
            """() => Object.fromEntries(
                ["--bg", "--paper", "--ink", "--muted", "--line", "--green", "--red", "--amber",
                 "--blue", "--raised", "--field-line", "--alert-bg", "--code-bg"]
                    .map(name => [name, getComputedStyle(document.documentElement)
                        .getPropertyValue(name).trim()])
            )"""
        )
        assert tokens == {
            "--bg": "#f4f5f1",
            "--paper": "#fff",
            "--ink": "#25342f",
            "--muted": "#5b695f",
            "--line": "#e2e7df",
            "--green": "#28775a",
            "--red": "#ae4941",
            "--amber": "#966927",
            "--blue": "#386d9f",
            "--raised": "#fcfdf9",
            "--field-line": "#d6ddd3",
            "--alert-bg": "#fff3df",
            "--code-bg": "#202b25",
        }
        body, header = (page.evaluate(COLOURS, selector)[1] for selector in ("body", "header"))
        assert to_hex(body) == "#f4f5f1"
        assert to_hex(header) == "#fcfdf9"
    finally:
        context.close()


def test_viewport_covers_the_notch_and_the_status_bar_is_not_translucent(
    page: Page, dashboard_site
) -> None:
    url, _ = dashboard_site
    page.goto(url)
    assert "viewport-fit=cover" in page.locator("meta[name=viewport]").get_attribute("content")
    # Translucent would put the header under the clock; the header does handle the inset, but the
    # light header would make the status bar's white text unreadable.
    status = page.locator("meta[name=apple-mobile-web-app-status-bar-style]")
    assert status.get_attribute("content") == "default"


@pytest.mark.parametrize("width", [320, 375, 390])
def test_nothing_floats_over_the_content_on_small_phones(
    browser: Browser, dashboard_site, width: int
) -> None:
    url, _ = dashboard_site
    context, page = open_page(
        browser,
        url + "/#watcher",
        viewport={"width": width, "height": 700},
        is_mobile=True,
        has_touch=True,
    )
    try:
        # Usage stays in the header, in flow, on every phone.
        usage = page.locator("#usage-toggle")
        assert usage.evaluate("el => getComputedStyle(el).position") != "fixed"
        assert usage.evaluate(
            """el => {
                const box = el.getBoundingClientRect();
                const header = document.querySelector('header').getBoundingClientRect();
                return box.left >= header.left && box.right <= header.right
                    && box.top >= header.top && box.bottom <= header.bottom;
            }"""
        )
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")

        page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
        fixed = page.evaluate(
            """() => [...document.querySelectorAll('body *')]
                .filter(el => getComputedStyle(el).position === 'fixed'
                    && el.getClientRects().length && !el.closest('dialog'))
                .map(el => el.id)"""
        )
        assert fixed == ["new-task"], fixed
        button = page.locator("#new-task").bounding_box()
        assert button
        for selector in ("footer", "#list > :last-child"):
            box = page.locator(selector).bounding_box()
            assert box
            overlaps = (
                box["x"] < button["x"] + button["width"]
                and button["x"] < box["x"] + box["width"]
                and box["y"] < button["y"] + button["height"]
                and button["y"] < box["y"] + box["height"]
            )
            assert not overlaps, f"{selector} sits under the New task button at {width}px"
    finally:
        context.close()


def test_row_checkbox_is_a_real_checkbox_with_a_44px_hit_area_on_touch(
    browser: Browser, dashboard_site
) -> None:
    url, _ = dashboard_site
    context, page = open_page(
        browser,
        url + "/#prs",
        viewport={"width": 390, "height": 844},
        is_mobile=True,
        has_touch=True,
    )
    try:
        row = page.locator("#pr-list tr").first
        expect(row).to_be_visible()
        box = row.locator(".item-select")
        assert box.evaluate("el => el.matches('input[type=checkbox]')")
        bounds = box.bounding_box()
        assert bounds and bounds["width"] >= 44 and bounds["height"] >= 44
        # It does not crowd its neighbours: pin, checkbox and notification toggle are separate.
        controls = [
            row.locator(selector).bounding_box()
            for selector in (".item-pin", ".item-select", ".notification-silence")
        ]
        for first, second in zip(controls, controls[1:], strict=False):
            assert first and second
            assert first["x"] + first["width"] <= second["x"] + 0.5
        # The edges of the hit area, well outside the 20px square, toggle it.
        box.click(position={"x": 22, "y": 2})
        expect(box).to_be_checked()
        assert box.evaluate("el => getComputedStyle(el).backgroundImage").startswith("url(")
        box.click(position={"x": 2, "y": 22})
        expect(box).not_to_be_checked()
        assert box.evaluate("el => getComputedStyle(el).backgroundImage") == "none"
        box.focus()
        page.keyboard.press("Space")
        expect(box).to_be_checked()
    finally:
        context.close()


def test_row_checkbox_stays_native_with_a_mouse(page: Page, dashboard_site) -> None:
    url, _ = dashboard_site
    page.set_viewport_size({"width": 1400, "height": 900})
    page.goto(f"{url}/#prs")
    box = page.locator("#pr-list tr").first.locator(".item-select")
    expect(box).to_be_visible()
    assert box.evaluate("el => getComputedStyle(el).appearance") == "auto"
    assert box.bounding_box()["width"] == 18


def test_a_dialog_does_not_shift_the_page_when_scrollbars_take_room(
    playwright: Playwright, dashboard_site
) -> None:
    url, _ = dashboard_site
    # Headless Chromium hides scrollbars by default; classic ones are what Windows and Linux show.
    browser = playwright.chromium.launch(ignore_default_args=["--hide-scrollbars"])
    try:
        context, page = open_page(browser, url, viewport={"width": 1280, "height": 400})
        gutter = page.evaluate("innerWidth - document.documentElement.clientWidth")
        assert gutter > 0, "this browser has no classic scrollbar to lose"
        edge = page.locator("#notifications-toggle")
        before = edge.bounding_box()
        assert before
        for name in ("usage-dialog", "ci-dialog"):
            page.evaluate("id => document.getElementById(id).showModal()", name)
            assert page.evaluate("document.documentElement.scrollHeight > innerHeight")
            assert edge.bounding_box() == before, f"page moved when {name} opened"
            page.evaluate("id => document.getElementById(id).close()", name)
        assert edge.bounding_box() == before

        # The full-screen viewer spans the window; the scrollbar's room is not held back from it.
        page.evaluate(
            """() => { const viewer = document.getElementById('ws-viewer');
                viewer.classList.add('ws-viewer-full'); viewer.showModal(); }"""
        )
        assert page.locator("#ws-viewer").bounding_box() == {
            "x": 0,
            "y": 0,
            "width": 1280,
            "height": 400,
        }
        context.close()
    finally:
        browser.close()


def test_safe_areas_keep_everything_clear_of_a_notch(browser: Browser, dashboard_site) -> None:
    url, _ = dashboard_site

    def pixels(page: Page, selector: str, *properties: str) -> list[float]:
        return page.evaluate(
            """([selector, properties]) => {
                const style = getComputedStyle(document.querySelector(selector));
                return properties.map(name => parseFloat(style[name]));
            }""",
            [selector, list(properties)],
        )

    def inset_page(width: int, height: int, **insets: int) -> tuple:
        context, page = open_page(
            browser,
            url,
            viewport={"width": width, "height": height},
            is_mobile=True,
            has_touch=True,
        )
        try:
            page.context.new_cdp_session(page).send(
                "Emulation.setSafeAreaInsetsOverride", {"insets": insets}
            )
        except Exception:  # pragma: no cover - an older Chromium
            context.close()
            pytest.skip("this Chromium cannot emulate safe-area insets")
        page.wait_for_timeout(100)
        return context, page

    # A landscape phone: the content column, the dialogs and the full-screen viewer.
    context, page = inset_page(844, 390, top=47, left=47, right=47, bottom=34)
    try:
        sides = ("paddingTop", "paddingLeft", "paddingRight")
        assert all(value >= 47 for value in pixels(page, "header", *sides))
        assert all(value >= 47 for value in pixels(page, "main", "paddingLeft", "paddingRight"))
        assert pixels(page, "main", "paddingBottom")[0] >= 34
        for dialog in ("ci-dialog", "usage-dialog", "notifications-dialog"):
            page.evaluate("id => document.getElementById(id).showModal()", dialog)
            box = page.locator(f"#{dialog}").bounding_box()
            assert box
            assert box["x"] >= 47 and box["x"] + box["width"] <= 844 - 47, dialog
            assert box["y"] >= 47 and box["y"] + box["height"] <= 390 - 34, dialog
            page.evaluate("id => document.getElementById(id).close()", dialog)
        page.evaluate("document.getElementById('ws-viewer').showModal()")
        padding = pixels(
            page, "#ws-viewer", "paddingTop", "paddingRight", "paddingBottom", "paddingLeft"
        )
        assert [value >= inset for value, inset in zip(padding, (47, 47, 34, 47), strict=True)] == [
            True
        ] * 4, padding
    finally:
        context.close()

    # A portrait phone: the header and the floating New task button.
    context, page = inset_page(390, 844, top=47, left=20, right=24, bottom=34)
    try:
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert pixels(page, "header", "paddingLeft", "paddingRight", "paddingTop") == [20, 24, 47]
        button = page.locator("#new-task").bounding_box()
        assert button
        assert button["x"] + button["width"] <= 390 - 24
        assert button["y"] + button["height"] <= 844 - 34
    finally:
        context.close()
