"""Experiment enabled/disabled browser coverage against temporary servers only."""

import threading
import time

import dashboard
import pytest
from playwright.sync_api import expect
from test_dashboard_browser import choose_option
from test_upstream_tests import FakeGitHub, collect, configured

pytestmark = pytest.mark.browser


@pytest.fixture
def upstream_site(tmp_path):
    fake = FakeGitHub()
    plugin = configured(tmp_path, fake)
    plugin.next_poll = float("inf")
    plugin.value, _ = collect(fake)
    plugin.value["synced_at"] = time.time()
    with dashboard.DashboardServer(tmp_path, 0, upstream_tests=plugin) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        yield f"http://127.0.0.1:{server.server_port}", plugin
        server.shutdown()
        thread.join(3)


@pytest.mark.parametrize("width", [1440, 390])
def test_upstream_grouping_routes_keyboard_and_responsive_screenshots(page, upstream_site, width):
    url, plugin = upstream_site
    page.set_viewport_size({"width": width, "height": 1000})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(url + "/#upstream")
    expect(page.get_by_role("tab", name="Upstream tests")).to_be_visible()
    expect(page.locator("#upstream-panel")).to_be_visible()
    expect(page.locator(".upstream-card")).to_have_count(4)
    expect(page.locator(".upstream-card").first).to_contain_text("2 branches")
    expect(page.locator("#upstream-scope")).to_contain_text("dev, release_26.1")
    expect(page.locator(".upstream-card").first).to_contain_text("Likely flaky")
    page.locator(".upstream-card").first.get_by_text("Pass / fail evidence", exact=True).click()
    expect(page.locator(".upstream-card").first.get_by_role("link").first).to_have_attribute(
        "href", "https://github.com/galaxyproject/galaxy/actions/runs/10"
    )
    page.screenshot(path=f"reports/upstream-tests-{width}.png", full_page=True)
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    choose_option(page.get_by_role("combobox", name="Group by", exact=True), "branch")
    expect(page.locator("#upstream-results")).to_contain_text("API tests · failure")
    page.locator(".upstream-card details").first.locator("summary").click()
    expect(
        page.locator(".upstream-card").first.get_by_role("link", name="Test (3.10, 0): failure")
    ).to_have_attribute("href", "https://github.com/galaxyproject/galaxy/actions/runs/10/job/42")
    page.screenshot(path=f"reports/upstream-tests-branches-{width}.png", full_page=True)
    page.get_by_role("tab", name="Upstream tests").focus()
    page.keyboard.press("ArrowLeft")
    expect(page.locator("#issues-panel")).to_be_visible()
    page.keyboard.press("End")
    expect(page.locator("#upstream-panel")).to_be_visible()
    page.go_back()
    expect(page.locator("#issues-panel")).to_be_visible()
    assert not errors
    assert plugin.next_poll == float("inf")


def test_loading_stale_empty_incomplete_errors_and_untrusted_text(page, upstream_site):
    url, plugin = upstream_site
    plugin.loading = True
    page.goto(url + "/#upstream")
    expect(page.locator("#upstream-status")).to_contain_text("Loading")
    plugin.loading = False
    plugin.error = "API rate limit"
    page.locator("#upstream-refresh").click()
    expect(page.locator("#upstream-status")).to_contain_text("Stale saved results")
    expect(page.locator("#upstream-notices")).to_contain_text("API rate limit")
    plugin.error = None
    plugin.value["groups"] = []
    plugin.value["incomplete"] = True
    plugin.value["warnings"] = ["dev: no eligible runs"]
    page.locator("#upstream-refresh").click()
    expect(page.locator("#upstream-results")).to_contain_text("available reports")
    expect(page.locator("#upstream-notices")).to_contain_text("no eligible runs")
    plugin.value["incomplete"] = False
    page.locator("#upstream-refresh").click()
    expect(page.locator("#upstream-results")).to_contain_text(
        "No failing or likely flaky tests in the selected runs"
    )
    plugin.value["groups"] = [
        {
            "test": '<img src=x onerror="window.injected=true">',
            "occurrences": [
                {
                    "branch": "dev",
                    "workflow": "<script>oops</script>",
                    "url": "javascript:window.injected=true",
                    "run_id": 1,
                    "attempt": 1,
                }
            ],
        }
    ]
    page.locator("#upstream-refresh").click()
    expect(page.locator(".upstream-card")).to_contain_text("<img src=x")
    expect(
        page.locator(".upstream-card img, .upstream-card script, .upstream-card a[href]")
    ).to_have_count(0)
    assert page.evaluate("window.injected") is None
    page.route("**/api/upstream-tests", lambda route: route.fulfill(status=503, body="unavailable"))
    page.locator("#upstream-refresh").click()
    expect(page.locator("#upstream-status")).to_contain_text("API unavailable")
    assert page.request.get(url + "/api/status").ok
    assert page.request.get(url + "/api/prs").ok
    assert page.request.get(url + "/api/issues").ok


def test_classification_filter_and_retry_evidence(page, upstream_site):
    url, _ = upstream_site
    page.goto(url + "/#upstream")
    choose_option(page.get_by_role("combobox", name="Show", exact=True), "likely_flaky")
    expect(page.locator(".upstream-card")).to_have_count(1)
    card = page.locator(".upstream-card")
    expect(card).to_contain_text("Passed after a test retry")
    expect(card).to_contain_text("0 failed / 1 passed")
    card.get_by_text("Pass / fail evidence", exact=True).click()
    expect(card).to_contain_text("Passed after retry")
    expect(card).to_contain_text("Commit aaaaaaaaaaaa")
    choose_option(page.get_by_role("combobox", name="Show", exact=True), "likely_broken")
    expect(page.locator("#upstream-results")).to_contain_text("No findings match this filter")
    choose_option(page.get_by_role("combobox", name="Show", exact=True), "insufficient")
    expect(page.locator(".upstream-card")).to_have_count(3)


def test_disabled_and_broken_plugin_do_not_affect_other_views(page, tmp_path):
    with dashboard.DashboardServer(tmp_path, 0) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.server_port}"
        try:
            page.goto(url + "/#upstream")
            expect(page.locator("#upstream-status")).to_contain_text("disabled")
            expect(page.locator("#upstream-tab")).to_be_hidden()
            page.get_by_role("tab", name="Watcher", exact=True).click()
            page.keyboard.press("End")
            expect(page.locator("#issues-panel")).to_be_visible()

            class Broken:
                def snapshot(self):
                    raise RuntimeError("experimental failure")

            server.upstream_tests = Broken()
            data = page.request.get(url + "/api/upstream-tests").json()
            assert "unavailable" in data["error"]
            for endpoint in ("status", "prs", "issues"):
                assert page.request.get(url + "/api/" + endpoint).ok
        finally:
            server.shutdown()
            thread.join(3)
    assert not (tmp_path / "queue.sqlite").exists()
