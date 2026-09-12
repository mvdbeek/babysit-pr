"""Real Chromium tests: isolated SQLite queue, real HTTP server, no watcher or agents."""

import copy
import json
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import dashboard
import pr_supervisor as supervisor
import pytest
from issue_overview import Overview as IssueOverview
from playwright.sync_api import Locator, Page, expect
from pr_overview import Overview

pytestmark = pytest.mark.browser


@pytest.fixture
def dashboard_site(tmp_path: Path) -> Iterator[tuple[str, Path]]:
    db = supervisor.open_db(tmp_path)
    base = {
        "id": "feedback",
        "repo": "test/repo",
        "url": "https://github.com/test/repo/pull/1",
        "cwd": "/fixture/worktree",
        "epoch": 0,
        "attempts": 0,
        "max_repairs": 5,
        "status": "watching",
        "summary": "Feedback awaits approval",
        "branch": None,
        "pending_reviews": [
            {
                "kind": "issue_comment",
                "id": "1",
                "author": "reviewer",
                "body": '<img src=x onerror="window.injected=true"> Fix the assertion.',
                "url": "https://github.com/test/repo/issues/1#issuecomment-1",
            }
        ],
        "snapshot": {
            "pr": {"head_branch": "feature", "number": 1, "merged": False, "closed": False}
        },
    }
    with db:
        supervisor.save_job(db, base)
        running = copy.deepcopy(base)
        running.update(id="running", repo="test/running", status="running", pending_reviews=[])
        supervisor.save_job(db, running)
        closed = copy.deepcopy(base)
        closed.update(
            id="closed", repo="test/merged", status="closed", pending_reviews=[], cleanup_ready=True
        )
        closed["snapshot"]["pr"].update(merged=True, closed=True)
        supervisor.save_job(db, closed)
    db.close()
    (tmp_path / "heartbeat.json").write_text(json.dumps({"time": time.time()}))
    overview = Overview(tmp_path)
    overview.next_poll = float("inf")  # Browser fixtures never call real GitHub.
    overview.value.update(
        login="fixture",
        synced_at=time.time(),
        prs=[
            {
                "id": "pr-one",
                "repo": "test/alpha",
                "number": 8,
                "title": '<img src=x onerror="window.injected=true"> Test PR',
                "url": "https://github.com/test/alpha/pull/8",
                "author": "fixture",
                "roles": ["author", "reviewer"],
                "ci": "FAILURE",
                "draft": False,
                "updated_at": "2026-09-11T10:00:00Z",
                "opened_at": "2025-01-01T10:00:00Z",
            },
            {
                "id": "pr-two",
                "repo": "test/beta",
                "number": 9,
                "title": "Assigned PR",
                "url": "https://github.com/test/beta/pull/9",
                "author": "colleague",
                "roles": ["assignee"],
                "ci": "SUCCESS",
                "draft": True,
                "updated_at": "2026-09-10T10:00:00Z",
                "opened_at": "2026-08-01T10:00:00Z",
            },
        ],
    )
    issues = IssueOverview(tmp_path)
    issues.next_poll = float("inf")
    issues.value.update(
        login="fixture",
        synced_at=time.time(),
        issues=[
            {
                "id": "issue-one",
                "repo": "test/alpha",
                "number": 30,
                "title": '<img src=x onerror="window.injected=true"> Crash on start',
                "url": "https://github.com/test/alpha/issues/30",
                "author": "fixture",
                "assignees": ["colleague"],
                "labels": [{"name": "kind/bug", "color": "d73a4a"}],
                "comments": 4,
                "roles": ["author", "mentioned"],
                "linked_prs": [
                    {
                        "id": "pr-one",
                        "number": 8,
                        "title": "Test PR",
                        "url": "https://github.com/test/alpha/pull/8",
                        "repo": "test/alpha",
                        "state": "OPEN",
                        "draft": False,
                        "head_repo": "fork/alpha",
                        "head_branch": "fix-30",
                        "head_sha": "c" * 40,
                    }
                ],
                "updated_at": "2026-09-11T09:00:00Z",
                "opened_at": "2025-02-01T10:00:00Z",
            },
            {
                "id": "issue-two",
                "repo": "test/beta",
                "number": 31,
                "title": "Assigned issue",
                "url": "https://github.com/test/beta/issues/31",
                "author": "colleague",
                "assignees": [],
                "labels": [],
                "comments": 0,
                "roles": ["assignee"],
                "linked_prs": [],
                "updated_at": "2026-09-10T09:00:00Z",
                "opened_at": "2026-08-02T10:00:00Z",
            },
        ],
    )
    with dashboard.DashboardServer(tmp_path, 0, overview=overview, issues=issues) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}", tmp_path
        finally:
            server.shutdown()
            thread.join(timeout=5)


def choose_option(control: Locator, value: str) -> None:
    """Choose through the visible picker, including its real narrowing behavior."""
    if control.evaluate("node => node.tagName") == "SELECT":
        control.select_option(value)
        return
    wrapper = control.locator("xpath=../..")
    label = wrapper.locator("select option").evaluate_all(
        "(options, value) => options.find(option => option.value === value).label", value
    )
    control.fill(label)
    wrapper.get_by_role("option", name=label, exact=True).click()


def read_job(home: Path, key: str) -> dict:
    db = supervisor.open_db(home)
    try:
        return supervisor.get_job(db, key)
    finally:
        db.close()


@pytest.mark.parametrize("width", [1440, 768, 390, 320])
def test_header_branding_and_status_fit_at_all_widths(page: Page, dashboard_site, width):
    url, home = dashboard_site
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(url)
    expect(page.locator("#updated")).to_contain_text("Refreshed")
    (home / "heartbeat.json").unlink()
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(page.locator("#health")).to_have_text("Watcher offline")
    page.screenshot(path=f"reports/header-{width}.png")

    for selector in [".mark", "header h1", "#health", "#updated"]:
        element = page.locator(selector)
        expect(element).to_be_visible()
        assert element.evaluate("node => node.scrollWidth <= node.clientWidth")
        assert element.evaluate(
            """node => {
                const rect = node.getBoundingClientRect();
                const header = node.closest('header').getBoundingClientRect();
                return rect.left >= header.left && rect.right <= header.right
                    && rect.top >= header.top && rect.bottom <= header.bottom;
            }"""
        )
    assert page.locator(".brand").evaluate(
        "node => node.getBoundingClientRect().right <= "
        "document.querySelector('.connection').getBoundingClientRect().left"
    )
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    tagline = page.locator("header p")
    if width > 900:
        expect(tagline).to_be_visible()
        assert tagline.evaluate(
            "node => node.getBoundingClientRect().left >= "
            "document.querySelector('header h1').getBoundingClientRect().right"
        )
    elif width <= 540:
        expect(tagline).to_be_hidden()


@pytest.mark.parametrize("kind,prefix", [("prs", "pr"), ("issues", "issue")])
@pytest.mark.parametrize("width", [1280, 390])
def test_item_pins_keyboard_mobile_and_reload(page, dashboard_site, kind, prefix, width):
    url, _ = dashboard_site
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(f"{url}/#{kind}")
    rows = page.locator(f"#{prefix}-list tr")
    expect(rows).to_have_count(2)
    mutations = []
    page.on(
        "request", lambda request: mutations.append(request) if request.method != "GET" else None
    )
    seen_key = f"babysit-pr:seen-{kind}:v1:fixture"
    baseline = page.evaluate("key => localStorage.getItem(key)", seen_key)
    pin = rows.nth(1).get_by_role(
        "button",
        name=f"Pin {prefix.upper() if prefix == 'pr' else prefix} test/beta #",
        exact=False,
    )
    expect(pin).to_have_attribute("aria-pressed", "false")
    pin.scroll_into_view_if_needed()
    bounds = pin.bounding_box()
    target_size = 44 if width <= 540 else 28
    assert bounds and bounds["width"] == target_size and bounds["height"] == target_size
    expect(rows.nth(1).locator("td").first.locator(".item-pin")).to_have_count(1)
    expect(pin).to_have_text("")
    icon_bounds = pin.locator("svg").bounding_box()
    assert icon_bounds and icon_bounds["width"] == 16 and icon_bounds["height"] == 16
    pin.focus()
    pin.press("Enter")
    expect(rows.first.locator(".pr-repo")).to_have_text("test/beta")
    unpin = rows.first.locator(".item-pin")
    assert unpin.get_attribute("aria-label").startswith("Unpin ")
    expect(unpin).to_have_attribute("aria-pressed", "true")
    expect(unpin).to_be_focused()
    expect(page.locator(f"#{prefix}-count")).to_have_text("2 / 2")
    expect(rows.locator(".pr-change-badge")).to_have_count(0)
    assert page.evaluate("key => localStorage.getItem(key)", seen_key) == baseline
    assert not mutations
    page.screenshot(path=f"reports/{kind}-pins-{width}.png", full_page=True)
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.reload()
    expect(rows.first.locator(".pr-repo")).to_have_text("test/beta")
    expect(rows.first.locator(".item-pin")).to_have_attribute("aria-pressed", "true")
    rows.first.locator(".item-pin").press("Space")
    expect(rows.first.locator(".pr-repo")).to_have_text("test/alpha")
    expect(rows.nth(1).locator(".item-pin")).to_be_focused()
    page.reload()
    expect(rows.first.locator(".pr-repo")).to_have_text("test/alpha")
    assert (
        page.evaluate(
            "key => JSON.parse(localStorage.getItem(key))", f"babysit-pr:pins-{kind}:v1:fixture"
        )
        == []
    )


@pytest.mark.parametrize("kind,prefix", [("prs", "pr"), ("issues", "issue")])
def test_item_pins_sort_filter_and_pagination(page, dashboard_site, kind, prefix):
    url, _ = dashboard_site
    snapshot = page.request.get(f"{url}/api/{kind}").json()
    template = snapshot[kind][0]
    snapshot[kind] = [
        dict(
            template,
            id=f"node-{index}",
            title=f"Item {index:03d}",
            number=index,
            repo="test/even" if index % 2 == 0 else "test/odd",
        )
        for index in range(1, 121)
    ]
    page.route(f"**/api/{kind}", lambda route: route.fulfill(json=snapshot))
    # Keep pagination deterministic; the explicit Show more control is tested here.
    page.add_init_script("window.IntersectionObserver = undefined;")
    page.goto(f"{url}/#{kind}")
    rows = page.locator(f"#{prefix}-list tr")
    page.locator(f"#{prefix}-sort-title").click()
    expect(rows.first.locator(".pr-title")).to_have_text("Item 001")
    page.locator(f"#{prefix}-search").fill("Item 120")
    rows.first.locator(".item-pin").click()
    page.locator(f"#{prefix}-search").fill("")
    expect(rows).to_have_count(50)
    expect(rows.first.locator(".pr-title")).to_have_text("Item 120")
    rows.filter(has=page.get_by_text("Item 002", exact=True)).locator(".item-pin").click()
    expect(rows.locator(".pr-title")).to_have_text(
        ["Item 002", "Item 120"] + [f"Item {i:03d}" for i in range(1, 50) if i != 2]
    )
    page.locator(f"#{prefix}-sort-title").click()
    expect(rows.locator(".pr-title")).to_have_text(
        ["Item 120", "Item 002"] + [f"Item {i:03d}" for i in range(119, 71, -1)]
    )
    repo_picker = page.locator(f"#{prefix}-repo").locator("..").get_by_role("combobox")
    choose_option(repo_picker, "test/odd")
    expect(rows.first.locator(".pr-title")).to_have_text("Item 119")
    expect(rows.locator('.item-pin[aria-pressed="true"]')).to_have_count(0)
    expect(page.locator(f"#{prefix}-count")).to_have_text("60 / 120")
    choose_option(repo_picker, "all")
    page.locator(f"#{prefix}-search").fill("Item 11")
    expect(rows).to_have_count(10)
    expect(rows.locator('.item-pin[aria-pressed="true"]')).to_have_count(0)
    page.locator(f"#{prefix}-search").fill("")
    page.locator(f"#{prefix}-more-button").click()
    expect(rows).to_have_count(100)
    rows.first.locator(".item-pin").click()
    expect(rows).to_have_count(100)
    expect(rows.first.locator(".pr-title")).to_have_text("Item 002")
    page.locator(f"#{prefix}-refresh").click()
    expect(rows).to_have_count(100)
    expect(page.locator(f"#{prefix}-more-button")).to_have_text("Show 20 more · 100 of 120 shown")
    # Unpinning an item whose ordinary position is outside the window moves focus
    # to Show more, and loading that page reveals its normal placement.
    rows.first.locator(".item-pin").click()
    expect(page.locator(f"#{prefix}-more-button")).to_be_focused()
    expect(rows.first.locator(".pr-title")).to_have_text("Item 120")
    page.locator(f"#{prefix}-more-button").press("Enter")
    expect(rows).to_have_count(120)
    expect(rows.nth(118).locator(".pr-title")).to_have_text("Item 002")


@pytest.mark.parametrize("kind,prefix", [("prs", "pr"), ("issues", "issue")])
def test_item_pins_accounts_kinds_and_absent_items(page, dashboard_site, kind, prefix):
    url, _ = dashboard_site
    snapshot = page.request.get(f"{url}/api/{kind}").json()
    missing = snapshot[kind][1]
    page.route(f"**/api/{kind}", lambda route: route.fulfill(json=snapshot))
    page.goto(f"{url}/#{kind}")
    rows = page.locator(f"#{prefix}-list tr")
    rows.nth(1).locator(".item-pin").click()
    key = f"babysit-pr:pins-{kind}:v1:fixture"
    assert page.evaluate("key => JSON.parse(localStorage.getItem(key))", key) == [missing["id"]]
    other_kind = "issues" if kind == "prs" else "prs"
    assert (
        page.evaluate(
            "key => localStorage.getItem(key)", f"babysit-pr:pins-{other_kind}:v1:fixture"
        )
        is None
    )
    snapshot["login"] = "second-account"
    page.locator(f"#{prefix}-refresh").click()
    expect(rows.first.locator(".pr-repo")).to_have_text("test/alpha")
    expect(rows.locator('.item-pin[aria-pressed="true"]')).to_have_count(0)
    rows.first.locator(".item-pin").click()
    assert page.evaluate("key => JSON.parse(localStorage.getItem(key))", key) == [missing["id"]]
    snapshot["login"] = "fixture"
    snapshot[kind].remove(missing)
    page.locator(f"#{prefix}-refresh").click()
    expect(rows).to_have_count(1)
    rows.first.locator(".item-pin").click()
    assert set(page.evaluate("key => JSON.parse(localStorage.getItem(key))", key)) == {
        missing["id"],
        snapshot[kind][0]["id"],
    }
    rows.first.locator(".item-pin").click()
    snapshot[kind].append(missing)
    page.locator(f"#{prefix}-refresh").click()
    expect(rows.first.locator(".pr-repo")).to_have_text("test/beta")
    assert page.evaluate(
        "key => JSON.parse(localStorage.getItem(key))", f"babysit-pr:pins-{kind}:v1:second-account"
    ) == [snapshot[kind][0]["id"]]


@pytest.mark.parametrize("kind,prefix", [("prs", "pr"), ("issues", "issue")])
@pytest.mark.parametrize("mode", ["blocked", "quota", "corrupt", "invalid", "stale"])
def test_item_pins_storage_fallback(page, dashboard_site, kind, prefix, mode):
    url, _ = dashboard_site
    key = f"babysit-pr:pins-{kind}:v1:fixture"
    if mode == "blocked":
        page.add_init_script(
            "Storage.prototype.getItem = () => { throw new DOMException('blocked', 'SecurityError'); };"
        )
    elif mode == "quota":
        page.add_init_script(
            "Storage.prototype.setItem = () => { throw new DOMException('full', 'QuotaExceededError'); };"
        )
    else:
        value = {
            "corrupt": "{broken",
            "invalid": '{"wrong":true}',
            "stale": '[null,5,"deleted-node"]',
        }[mode]
        page.add_init_script(f"localStorage.setItem({json.dumps(key)}, {json.dumps(value)});")
    page.goto(f"{url}/#{kind}")
    rows = page.locator(f"#{prefix}-list tr")
    expect(rows).to_have_count(2)
    rows.nth(1).locator(".item-pin").click()
    expect(rows.first.locator(".pr-repo")).to_have_text("test/beta")
    page.locator(f"#{prefix}-refresh").click()
    expect(rows.first.locator(".item-pin")).to_have_attribute("aria-pressed", "true")
    if mode in {"blocked", "quota"}:
        expect(page.locator(f"#{prefix}-pins-status")).to_contain_text("this tab only")
    else:
        saved = page.evaluate("key => JSON.parse(localStorage.getItem(key))", key)
        assert f"{prefix}-two" in saved
        assert all(isinstance(node, str) for node in saved)
        expect(page.locator(f"#{prefix}-pins-status")).to_be_hidden()
    rows.first.locator(".item-pin").click()
    expect(rows.first.locator(".pr-repo")).to_have_text("test/alpha")


@pytest.mark.parametrize("kind,prefix", [("prs", "pr"), ("issues", "issue")])
@pytest.mark.parametrize("width", [1280, 390])
def test_latest_activity_display_refresh_and_fallback(page, dashboard_site, kind, prefix, width):
    url, _ = dashboard_site
    page.set_viewport_size({"width": width, "height": 900})
    snapshot = page.request.get(f"{url}/api/{kind}").json()
    first = snapshot[kind][0]
    activity_url = first["url"] + "#issuecomment-123"
    first["latest_activity"] = {
        "actor": "dependabot[bot]",
        "action": "commented",
        "at": "2026-09-11T08:00:00Z",
        "url": activity_url,
    }
    page.route(f"**/api/{kind}", lambda route: route.fulfill(json=snapshot))
    page.goto(f"{url}/#{kind}")
    rows = page.locator(f"#{prefix}-list tr")
    detail = rows.first.locator(".pr-activity")
    expect(detail).to_contain_text("Latest activity")
    expect(detail.get_by_role("link", name="dependabot[bot] commented")).to_have_attribute(
        "href", activity_url
    )
    expect(detail.locator("time")).to_have_attribute("datetime", "2026-09-11T08:00:00Z")
    assert detail.locator("time").get_attribute("title")
    expect(rows.first.locator(".pr-updated > time")).to_have_attribute(
        "datetime", first["updated_at"]
    )
    expect(rows.nth(1).locator(".pr-activity")).to_contain_text("Unavailable")
    detail.scroll_into_view_if_needed()
    expect(detail).to_be_visible()
    assert detail.evaluate("node => node.scrollWidth <= node.clientWidth")
    if width == 390:
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.screenshot(path=f"reports/{kind}-activity-{width}.png", full_page=True)

    first["latest_activity"].update(actor=None, action="added a label", url=None)
    page.locator(f"#{prefix}-refresh").click()
    expect(detail).to_contain_text("Unknown actor added a label")
    expect(detail.locator("a")).to_have_count(0)

    first["latest_activity"].update(
        actor='<img src=x onerror="window.injected=true">',
        action="commented",
        url="javascript:window.injected=true",
    )
    page.locator(f"#{prefix}-refresh").click()
    expect(detail).to_contain_text('<img src=x onerror="window.injected=true"> commented')
    expect(detail.locator("a, img")).to_have_count(0)
    assert page.evaluate("window.injected") is None


@pytest.mark.parametrize("kind,prefix,columns", [("prs", "pr", 9), ("issues", "issue", 10)])
@pytest.mark.parametrize("width", [1280, 390])
def test_compact_dates_share_a_column_and_keep_both_sorts(
    page, dashboard_site, kind, prefix, columns, width
):
    url, _ = dashboard_site
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(f"{url}/#{kind}")
    rows = page.locator(f"#{prefix}-list tr")
    expect(rows).to_have_count(2)
    table = rows.first.locator("xpath=ancestor::table")
    page.screenshot(path=f"reports/{kind}-compact-dates-{width}.png", full_page=True)
    expect(table.locator("thead th")).to_have_count(columns)
    expect(rows.first.locator("td")).to_have_count(columns)
    dates = rows.first.locator("td.pr-dates")
    expect(dates.locator(".pr-opened > time")).to_have_attribute(
        "datetime", "2025-01-01T10:00:00Z" if kind == "prs" else "2025-02-01T10:00:00Z"
    )
    expect(dates.locator(".pr-updated > time")).to_have_attribute(
        "datetime", "2026-09-11T10:00:00Z" if kind == "prs" else "2026-09-11T09:00:00Z"
    )
    expect(dates.locator(".pr-opened")).to_contain_text("Opened")
    expect(dates.locator(".pr-updated")).to_contain_text("Updated")
    expect(dates.locator(".pr-activity")).to_contain_text("Unavailable")
    expect(page.locator(f"#{prefix}-sync")).to_contain_text("Sorted by Last updated (descending)")
    for field, label in [("opened_at", "Opened"), ("updated_at", "Last updated")]:
        if width == 390:
            choose_option(
                page.locator(f"#{prefix}-sort").locator("..").get_by_role("combobox"), field
            )
        else:
            button = page.get_by_role("button", name=f"Sort by {label}", exact=True)
            button.focus()
            button.press("Enter")
            expect(button).to_be_focused()
            expect(button).to_have_attribute("aria-pressed", "true")
            expect(
                table.get_by_role("columnheader", name=f"Dates / activity, sorted by {label}")
            ).to_have_attribute("aria-sort", "descending")
        expect(rows.first.locator(".pr-repo")).to_have_text(
            "test/beta" if field == "opened_at" else "test/alpha"
        )
        if width == 390:
            page.locator(f"#{prefix}-sort-direction").click()
        else:
            button.press("Space")
        expect(rows.first.locator(".pr-repo")).to_have_text(
            "test/alpha" if field == "opened_at" else "test/beta"
        )
    dates.scroll_into_view_if_needed()
    assert dates.evaluate("node => node.scrollWidth <= node.clientWidth")
    if width == 390:
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


@pytest.mark.parametrize("kind,prefix", [("prs", "pr"), ("issues", "issue")])
def test_compact_dates_keep_missing_dates_last_in_both_directions(
    page, dashboard_site, kind, prefix
):
    url, _ = dashboard_site
    snapshot = page.request.get(f"{url}/api/{kind}").json()
    snapshot[kind][1].update(opened_at=None, updated_at="invalid")
    page.route(f"**/api/{kind}", lambda route: route.fulfill(json=snapshot))
    page.goto(f"{url}/#{kind}")
    rows = page.locator(f"#{prefix}-list tr")
    expect(rows).to_have_count(2)
    for field in ["opened", "updated"]:
        detail = rows.last.locator(f".pr-{field}")
        expect(detail.locator(":scope > time")).to_have_text("Unknown")
        expect(detail.locator(":scope > time[datetime]")).to_have_count(0)
        expect(detail).to_contain_text("at an unknown time")
    for label in ["Opened", "Last updated"]:
        button = page.get_by_role("button", name=f"Sort by {label}", exact=True)
        for _ in range(2):
            button.click()
            expect(rows.last.locator(".pr-repo")).to_have_text("test/beta")
    page.locator(f"#{prefix}-refresh").click()
    expect(rows.last.locator(".pr-repo")).to_have_text("test/beta")


@pytest.mark.parametrize(
    "kind,prefix,field",
    [("prs", "pr", "updated_at"), ("prs", "pr", "head_sha"), ("issues", "issue", "updated_at")],
)
def test_compact_dates_highlight_updated_section_only(page, dashboard_site, kind, prefix, field):
    url, _ = dashboard_site
    snapshot = page.request.get(f"{url}/api/{kind}").json()
    page.route(f"**/api/{kind}", lambda route: route.fulfill(json=snapshot))
    page.goto(f"{url}/#{kind}")
    rows = page.locator(f"#{prefix}-list tr")
    expect(rows).to_have_count(2)
    snapshot[kind][0][field] = "2026-09-12T12:00:00Z" if field == "updated_at" else "b" * 40
    snapshot["synced_at"] += 1
    page.locator(f"#{prefix}-refresh").click()
    changed = page.locator(f"#{prefix}-list .pr-changed")
    expect(changed).to_have_count(1)
    expect(changed.locator(".pr-updated.pr-field-changed")).to_have_count(1)
    expect(
        changed.locator(".pr-dates.pr-field-changed, .pr-opened.pr-field-changed")
    ).to_have_count(0)
    expect(changed.locator(".pr-change-note")).to_have_text(
        "New commits"
        if field == "head_sha"
        else f"{'PR' if kind == 'prs' else 'Issue'} activity changed"
    )
    # The local highlight must not color Opened through a shared parent.
    assert changed.locator(".pr-updated").evaluate(
        "node => getComputedStyle(node).backgroundColor"
    ) != changed.locator(".pr-opened").evaluate("node => getComputedStyle(node).backgroundColor")
    page.reload()
    expect(page.locator(f"#{prefix}-list .pr-field-changed")).to_have_count(0)


def open_watch(page: Page, url: str, repo: str) -> None:
    page.goto(url)
    page.get_by_role("button", name=f"{repo}, feature,", exact=False).click()


def test_feedback_is_text_and_requires_an_explicit_click(page: Page, dashboard_site) -> None:
    url, home = dashboard_site
    open_watch(page, url, "test/repo")
    expect(page.locator(".feedback-item")).to_contain_text(
        '<img src=x onerror="window.injected=true">'
    )
    expect(page.locator(".feedback-item img")).to_have_count(0)
    assert page.evaluate("window.injected === undefined")
    assert not read_job(home, "feedback").get("approved_reviews")
    page.get_by_role("button", name="Handle feedback", exact=True).click()
    expect(page.get_by_role("button", name="Feedback queued", exact=True)).to_be_disabled()
    assert len(read_job(home, "feedback")["approved_reviews"]) == 1


def test_stale_feedback_click_is_rejected_without_approving_new_text(
    page: Page, dashboard_site
) -> None:
    url, home = dashboard_site
    open_watch(page, url, "test/repo")
    db = supervisor.open_db(home)
    with db:
        job = supervisor.get_job(db, "feedback")
        job["pending_reviews"][0]["body"] = "A later comment revision"
        supervisor.save_job(db, job)
    db.close()
    page.get_by_role("button", name="Handle feedback", exact=True).click()
    expect(page.locator("#detail [role=alert]")).to_contain_text("Feedback changed")
    assert not read_job(home, "feedback").get("approved_reviews")
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(page.locator(".feedback-item")).to_contain_text("A later comment revision")


def test_cancel_keeps_history_and_running_repairs_finish_first(page: Page, dashboard_site) -> None:
    url, home = dashboard_site
    open_watch(page, url, "test/repo")
    page.get_by_role("button", name="Cancel watch", exact=True).click()
    expect(page.locator("#ended")).to_have_text("2")
    page.locator("#show-ended").click()
    expect(page.locator("#list")).to_contain_text("test/repo")
    assert read_job(home, "feedback")["status"] == "stopped"
    choose_option(page.get_by_role("combobox", name="Filter watches", exact=True), "active")
    page.get_by_role("button", name="test/running, feature,", exact=False).click()
    page.get_by_role("button", name="Cancel watch", exact=True).click()
    expect(page.get_by_role("button", name="Cancellation pending", exact=True)).to_be_disabled()
    job = read_job(home, "running")
    assert job["status"] == "running" and job["stop_after_run"]


def test_mobile_cleanup_attention_and_offline_state(page: Page, dashboard_site) -> None:
    url, home = dashboard_site
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(url)
    page.locator("#show-attention").click()
    page.get_by_role("button", name="test/merged, feature,", exact=False).click()
    expect(page.locator(".cleanup-note")).to_contain_text("PR merged · ready for cleanup")
    expect(page.get_by_role("button", name="Handle feedback", exact=True)).to_have_count(0)
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    (home / "heartbeat.json").unlink()
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(page.locator("#health")).to_have_text("Watcher offline")


def test_pr_overview_filters_ci_roles_times_and_safe_titles(page: Page, dashboard_site) -> None:
    url, home = dashboard_site
    page.goto(url)
    page.get_by_role("tab", name="Pull requests", exact=True).click()
    rows = page.locator("#pr-list tr")
    expect(rows).to_have_count(2)
    expect(page.get_by_role("columnheader", name="Repository", exact=True)).to_be_visible()
    expect(rows.first.locator(".pr-repo")).to_have_text("test/alpha")
    expect(rows.first.locator(".pr-description")).to_contain_text("#8")
    expect(rows.first.locator("img")).to_have_count(0)
    assert page.evaluate("window.injected === undefined")
    expect(rows.first.locator(".pr-updated time")).to_have_attribute(
        "datetime", "2026-09-11T10:00:00Z"
    )
    expect(page.locator("#pr-sync")).to_contain_text("@fixture")
    expect(page.locator("#pr-sync")).to_contain_text("Synced")
    expect(page.get_by_role("link", name="CI Failed for test/alpha #8")).to_have_attribute(
        "href", "https://github.com/test/alpha/pull/8/checks"
    )
    choose_option(
        page.get_by_role("combobox", name="Filter pull requests by role", exact=True), "reviewer"
    )
    expect(rows).to_have_count(1)
    choose_option(
        page.get_by_role("combobox", name="Filter pull requests by role", exact=True), "assignee"
    )
    expect(rows).to_contain_text(["Assigned PR"])
    choose_option(
        page.get_by_role("combobox", name="Filter pull requests by role", exact=True), "all"
    )
    choose_option(
        page.get_by_role("combobox", name="Filter pull requests by CI", exact=True), "FAILURE"
    )
    expect(rows).to_have_count(1)
    choose_option(
        page.get_by_role("combobox", name="Filter pull requests by CI", exact=True), "all"
    )
    page.get_by_label("Search pull requests").fill("colleague")
    expect(rows).to_contain_text(["Assigned PR"])
    page.get_by_label("Search pull requests").fill("missing")
    expect(page.locator("#pr-empty")).to_have_text("No matching pull requests.")
    assert len(dashboard.read_jobs(home)) == 3  # Discovery creates no repair watches.
    page.set_viewport_size({"width": 390, "height": 844})
    page.get_by_label("Search pull requests").fill("")
    expect(rows).to_have_count(2)
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


def test_pr_sync_failure_and_empty_state_are_distinct(page: Page, dashboard_site) -> None:
    url, _ = dashboard_site
    page.route(
        "**/api/prs",
        lambda route: route.fulfill(
            json={
                "prs": [],
                "synced_at": None,
                "error": "GitHub authentication failed",
            }
        ),
    )
    page.goto(url)
    page.get_by_role("tab", name="Pull requests", exact=True).click()
    expect(page.locator("#pr-alert")).to_contain_text("GitHub authentication failed")
    expect(page.locator("#pr-empty")).to_contain_text("PR data is unavailable")
    page.unroute("**/api/prs")
    page.route(
        "**/api/prs",
        lambda route: route.fulfill(
            json={
                "prs": [],
                "synced_at": 1234,
                "error": None,
            }
        ),
    )
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(page.locator("#pr-empty")).to_have_text("No open pull requests for your roles.")
    expect(page.locator("#pr-alert")).to_be_hidden()


def test_dashboard_tabs_default_to_watcher_and_support_navigation(
    page: Page, dashboard_site
) -> None:
    url, _ = dashboard_site
    page.goto(url)
    watcher = page.get_by_role("tab", name="Watcher", exact=True)
    prs = page.get_by_role("tab", name="Pull requests", exact=True)
    expect(watcher).to_have_attribute("aria-selected", "true")
    expect(page.locator("#watcher-panel")).to_be_visible()
    expect(page.locator("#prs-panel")).to_be_hidden()
    expect(page.locator(".page-tabs [role=tab]").first).to_have_text("Watcher")
    watcher.focus()
    watcher.press("ArrowRight")
    expect(prs).to_be_focused()
    expect(prs).to_have_attribute("aria-selected", "true")
    expect(page.locator("#watcher-panel")).to_be_hidden()
    expect(page.locator("#prs-panel")).to_be_visible()
    expect(page).to_have_url(url + "/#prs")
    page.get_by_label("Search pull requests").fill("alpha")
    watcher.click()
    page.go_back()
    expect(prs).to_have_attribute("aria-selected", "true")
    expect(page.get_by_label("Search pull requests")).to_have_value("alpha")
    page.reload()
    expect(page.locator("#prs-panel")).to_be_visible()
    prs.focus()
    prs.press("Home")
    expect(watcher).to_be_focused()
    expect(page.locator("#watcher-panel")).to_be_visible()
    issues = page.get_by_role("tab", name="Issues", exact=True)
    watcher.press("End")
    expect(issues).to_be_focused()
    expect(issues).to_have_attribute("aria-selected", "true")
    expect(page.locator("#issues-panel")).to_be_visible()
    expect(page.locator("#prs-panel")).to_be_hidden()
    expect(page).to_have_url(url + "/#issues")
    issues.press("ArrowRight")
    expect(watcher).to_be_focused()
    watcher.press("ArrowLeft")
    expect(issues).to_be_focused()
    issues.press("ArrowLeft")
    expect(prs).to_be_focused()
    expect(page.locator("#prs-panel")).to_be_visible()
    page.goto(url + "/#issues")
    expect(page.locator("#issues-panel")).to_be_visible()
    expect(page.locator("#issue-list tr")).to_have_count(2)


@pytest.mark.parametrize(
    "column,first",
    [
        ("Repository", "test/alpha"),
        ("Pull request", "test/alpha"),
        ("Author", "test/beta"),
        ("Review status", "test/beta"),
        ("Your role", "test/beta"),
        ("CI", "test/alpha"),
        ("Opened", "test/beta"),
        ("Last updated", "test/beta"),
    ],
)
def test_each_pr_column_sorts_both_directions(page: Page, dashboard_site, column, first) -> None:
    url, _ = dashboard_site
    page.goto(url + "/#prs")
    rows = page.locator("#pr-list tr")
    expect(rows).to_have_count(2)
    button = page.get_by_role("button", name=f"Sort by {column}", exact=True)
    button.click()
    expect(rows.first.locator(".pr-repo")).to_have_text(first)
    button.click()
    expect(rows.first.locator(".pr-repo")).to_have_text(
        "test/beta" if first == "test/alpha" else "test/alpha"
    )
    expect(button.locator("xpath=ancestor::th")).to_have_attribute(
        "aria-sort",
        "descending"
        if column not in {"Opened", "Last updated"}
        else "ascending"
        if column == "Opened"
        else "descending",
    )


def test_pr_metadata_and_mobile_sort_survive_refresh_and_filtering(
    page: Page, dashboard_site
) -> None:
    url, _ = dashboard_site
    page.goto(url + "/#prs")
    expect(page.locator(".pr-author").first).to_have_text("fixture")
    expect(page.locator(".pr-readiness").first).to_have_text("Ready for review")
    expect(page.locator(".pr-readiness").last).to_have_text("Draft")
    expect(page.locator(".pr-opened time").first).to_have_attribute(
        "datetime", "2025-01-01T10:00:00Z"
    )
    page.set_viewport_size({"width": 390, "height": 844})
    choose_option(page.get_by_role("combobox", name="Sort by", exact=True), "author")
    expect(page.locator(".pr-author").first).to_have_text("colleague")
    page.get_by_role("button", name="Ascending", exact=True).click()
    expect(page.locator(".pr-author").first).to_have_text("fixture")
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(page.locator(".pr-author").first).to_have_text("fixture")
    page.get_by_label("Search pull requests").fill("colleague")
    expect(page.locator("#pr-list tr")).to_have_count(1)
    page.get_by_label("Search pull requests").fill("")
    expect(page.locator(".pr-author").first).to_have_text("fixture")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


def test_pr_filters_combine_and_keep_repository_selection_on_refresh(
    page: Page, dashboard_site
) -> None:
    url, _ = dashboard_site
    page.goto(url + "/#prs")
    repo = page.get_by_role("combobox", name="Filter pull requests by repository", exact=True)
    review = page.get_by_role("combobox", name="Filter pull requests by review status", exact=True)
    ci = page.get_by_role("combobox", name="Filter pull requests by CI", exact=True)
    rows = page.locator("#pr-list tr")
    expect(page.locator("#pr-repo option")).to_have_text(
        ["All repositories", "test/alpha", "test/beta"]
    )
    choose_option(repo, "test/beta")
    choose_option(review, "draft")
    choose_option(ci, "SUCCESS")
    expect(rows).to_have_count(1)
    expect(rows).to_contain_text(["Assigned PR"])
    choose_option(review, "ready")
    expect(rows).to_have_count(0)
    expect(page.locator("#pr-empty")).to_have_text("No matching pull requests.")
    expect(page.locator("#pr-repo option")).to_have_count(3)
    choose_option(repo, "test/alpha")
    choose_option(ci, "FAILURE")
    expect(rows).to_have_count(1)
    choose_option(
        page.get_by_role("combobox", name="Filter pull requests by role", exact=True), "assignee"
    )
    expect(rows).to_have_count(0)
    choose_option(
        page.get_by_role("combobox", name="Filter pull requests by role", exact=True), "all"
    )
    expect(rows).to_have_count(1)
    page.route("**/api/prs", lambda route: route.fulfill(json={"prs": [], "synced_at": 1234}))
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(rows).to_have_count(0)
    expect(repo).to_have_value("test/alpha")
    expect(review).to_have_value("Ready for review")
    expect(ci).to_have_value("Failed")
    page.unroute("**/api/prs")
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(rows).to_have_count(1)
    page.set_viewport_size({"width": 390, "height": 844})
    expect(repo).to_be_visible()
    expect(review).to_be_visible()
    expect(ci).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


def test_ci_filters_match_each_displayed_state(page: Page, dashboard_site) -> None:
    url, _ = dashboard_site
    states = ["SUCCESS", "FAILURE", "PENDING", "ERROR", "EXPECTED", "NONE", "UNKNOWN"]
    prs = [
        {
            "id": state,
            "repo": "test/repo",
            "title": state,
            "number": index,
            "url": f"https://github.com/test/repo/pull/{index}",
            "roles": ["author"],
            "ci": state,
            "draft": False,
            "updated_at": "2026-09-11T10:00:00Z",
        }
        for index, state in enumerate(states, 1)
    ]
    page.route("**/api/prs", lambda route: route.fulfill(json={"prs": prs, "synced_at": 1234}))
    page.goto(url + "/#prs")
    for state in states:
        choose_option(
            page.get_by_role("combobox", name="Filter pull requests by CI", exact=True), state
        )
        expect(page.locator("#pr-list tr")).to_have_count(1)
        expect(page.locator("#pr-list .pr-title")).to_have_text(state)
    choose_option(
        page.get_by_role("combobox", name="Filter pull requests by CI", exact=True), "all"
    )
    expect(page.locator("#pr-list tr")).to_have_count(7)


def test_pr_overview_pages_fifty_rows_and_loads_more_on_scroll(page: Page, dashboard_site) -> None:
    url, _home = dashboard_site
    prs = [
        {
            "id": f"pr-{index}",
            "repo": "test/odd" if index % 2 else "test/even",
            "title": f"Change {index}",
            "number": index,
            "url": f"https://github.com/test/repo/pull/{index}",
            "roles": ["author"],
            "ci": "SUCCESS",
            "draft": False,
            "updated_at": f"2026-01-01T00:{index // 60:02d}:{index % 60:02d}Z",
        }
        for index in range(1, 121)
    ]
    page.route("**/api/prs", lambda route: route.fulfill(json={"prs": prs, "synced_at": 1234}))
    page.goto(url + "/#prs")
    rows = page.locator("#pr-list tr")
    more = page.locator("#pr-more-button")
    expect(rows).to_have_count(50)
    expect(page.locator("#pr-count")).to_have_text("120 / 120")  # Counts cover the whole list.
    expect(rows.first.locator(".pr-title")).to_have_text("Change 120")
    expect(more).to_have_text("Show 50 more · 50 of 120 shown")
    # Scrolling the sentinel into view appends the next page.
    more.scroll_into_view_if_needed()
    expect(rows).to_have_count(100)
    expect(more).to_have_text("Show 20 more · 100 of 120 shown")
    more.scroll_into_view_if_needed()
    expect(rows).to_have_count(120)
    expect(page.locator("#pr-more")).to_be_hidden()
    # A filter restarts at the top of the first page; the button also appends a page.
    choose_option(
        page.get_by_role("combobox", name="Filter pull requests by repository", exact=True),
        "test/even",
    )
    expect(rows).to_have_count(50)
    assert page.evaluate("document.querySelector('#prs-panel .pr-table-wrap').scrollTop") == 0
    expect(more).to_have_text("Show 10 more · 50 of 60 shown")
    more.dispatch_event("click")
    expect(rows).to_have_count(60)
    expect(page.locator("#pr-more")).to_be_hidden()
    # The timed refresh keeps the window; a search restarts it.
    page.locator("#pr-refresh").click()
    expect(rows).to_have_count(60)
    page.get_by_label("Search pull requests").fill("Change 1")
    expect(rows).to_have_count(16)  # Even numbers containing a 1: 10-18 and 100-120.
    expect(page.locator("#pr-more")).to_be_hidden()
    page.get_by_role("tab", name="Issues", exact=True).click()
    expect(page.locator("#issue-list tr")).to_have_count(2)
    expect(page.locator("#issue-more")).to_be_hidden()


@pytest.fixture
def workspace_routes(page):
    target = {
        "path": "/fixture/checkout",
        "workspace_id": "w1",
        "name": "Renamed workspace",
        "agent_status": "working",
        "url": "https://mac-mini.tailfb45be.ts.net/space/w1",
    }
    info = {
        "matches": [target],
        "suggestions": [],
        "clones": ["/fixture/repo"],
        "preferred_clone": None,
        "destination": "/fixture/new",
        "operation": None,
    }
    snapshot = {"prs": {"pr-one": info, "pr-two": copy.deepcopy(info)}, "error": None}
    requests = []
    page.route("**/api/workspaces", lambda route: route.fulfill(json=snapshot))

    def action(route):
        body = route.request.post_data_json
        requests.append(body)
        if body["action"] == "copy":
            route.fulfill(json={"command": "herdr workspace focus w1"})
        elif body["action"] in {"open", "focus"}:
            route.fulfill(json={"result": target})
        else:
            op = {
                "id": "op1",
                "status": "running",
                "message": "Fetching PR",
                "log": "Fetch started",
            }
            info["operation"] = op
            route.fulfill(json={"operation": op})

    page.route("**/api/workspace-action", action)
    return info, snapshot, requests


def test_workspace_single_open_and_explicit_native_menu(page, dashboard_site, workspace_routes):
    url, _ = dashboard_site
    _, _, requests = workspace_routes
    page.goto(url + "/#prs")
    page.evaluate("window.open = () => { window.opened = {location: {}}; return window.opened; }")
    row = page.locator("#pr-list tr").first
    row.get_by_role("button", name="Open workspace", exact=True).click()
    expect(page.locator("#workspace-result a")).to_have_attribute(
        "href", "https://mac-mini.tailfb45be.ts.net/space/w1"
    )
    assert page.evaluate("window.opened.location.href").endswith("/space/w1")
    assert requests == [
        {"id": "pr-one", "action": "open", "path": "/fixture/checkout", "workspace_id": "w1"}
    ]
    expect(page.locator("#workspace-dialog")).not_to_be_visible()
    row.get_by_text("More actions", exact=True).click()
    with page.expect_response("**/api/workspace-action"):
        row.get_by_role("button", name="Focus in herdr", exact=True).click()
    expect(row.get_by_role("button", name="Copy command", exact=True)).to_be_visible()
    assert requests[-1]["action"] == "focus"


def test_workspace_chooser_names_status_and_reopen(page, dashboard_site, workspace_routes):
    url, _ = dashboard_site
    info, _, requests = workspace_routes
    info["matches"].append(
        {
            **info["matches"][0],
            "workspace_id": None,
            "path": "/fixture/other",
            "name": "Other checkout",
            "agent_status": "No workspace",
        }
    )
    page.goto(url + "/#prs")
    page.locator("#pr-list tr").first.get_by_role(
        "button", name="Open workspace", exact=True
    ).click()
    dialog = page.get_by_role("dialog")
    expect(dialog).to_contain_text("Renamed workspace")
    expect(dialog).to_contain_text("working")
    expect(dialog).to_contain_text("Other checkout")
    assert not requests
    dialog.get_by_role("button", name="Reopen workspace", exact=True).click()
    expect(page.locator("#workspace-progress")).to_contain_text("running")
    assert requests[-1] == {"id": "pr-one", "action": "reopen", "path": "/fixture/other"}


def test_workspace_create_required_task_agent_progress_and_errors(
    page, dashboard_site, workspace_routes
):
    url, _ = dashboard_site
    info, _, requests = workspace_routes
    info["matches"] = []
    info["clones"] = ["/fixture/one", "/fixture/two"]
    page.goto(url + "/#prs")
    page.locator("#pr-list tr").first.get_by_role(
        "button", name="Create workspace", exact=True
    ).click()
    dialog = page.get_by_role("dialog")
    expect(dialog.get_by_label("Agent", exact=True)).to_have_value("codex")
    dialog.get_by_role("button", name="Create workspace", exact=True).click()
    assert not requests
    choose_option(dialog.get_by_role("combobox", name="Local clone", exact=True), "/fixture/two")
    dialog.get_by_label("Task", exact=True).fill("Fix 'quotes'\nsecond line")
    choose_option(dialog.get_by_label("Agent", exact=True), "claude")
    page.screenshot(path="reports/workspace-desktop.png")
    dialog.get_by_role("button", name="Create workspace", exact=True).click()
    expect(page.locator("#workspace-progress")).to_contain_text("Fetching PR")
    expect(dialog.get_by_role("button", name="Create workspace", exact=True)).to_be_disabled()
    assert requests[-1]["agent"] == "claude" and requests[-1]["clone"] == "/fixture/two"
    assert requests[-1]["task"] == "Fix 'quotes'\nsecond line"
    info["operation"].update(status="failed", message="Fetch failed", log="network unavailable")
    page.evaluate("refreshWorkspaces()")
    expect(page.locator("#workspace-progress")).to_contain_text("Fetch failed")
    expect(dialog.get_by_role("button", name="Create workspace", exact=True)).to_be_enabled()
    dialog.get_by_role("button", name="Create workspace", exact=True).click()
    expect(page.locator("#workspace-progress")).to_contain_text("Fetching PR")
    assert requests[-1]["retry"] is True
    info["operation"].update(
        status="complete",
        message="Workspace ready",
        result={"url": "https://mac-mini.tailfb45be.ts.net/space/w1"},
    )
    page.evaluate("refreshWorkspaces()")
    expect(dialog.get_by_role("link", name="Open in Collie")).to_have_attribute(
        "href", "https://mac-mini.tailfb45be.ts.net/space/w1"
    )


def test_workspace_dialog_closes_on_backdrop_click_and_polls_running_operation(
    page, dashboard_site, workspace_routes
):
    url, _ = dashboard_site
    info, snapshot, _ = workspace_routes
    info["matches"] = []
    polls = []
    page.route(
        "**/api/workspaces",
        lambda route: (polls.append(time.monotonic()), route.fulfill(json=snapshot)),
    )
    page.goto(url + "/#prs")
    page.locator("#pr-list tr").first.get_by_role(
        "button", name="Create workspace", exact=True
    ).click()
    dialog = page.get_by_role("dialog")
    expect(dialog).to_be_visible()
    # Clicking inside the dialog box keeps it open; clicking the backdrop closes it.
    dialog.get_by_role("heading").click()
    expect(dialog).to_be_visible()
    page.mouse.click(2, 2)
    expect(dialog).not_to_be_visible()
    page.locator("#pr-list tr").first.get_by_role(
        "button", name="Create workspace", exact=True
    ).click()
    dialog.get_by_label("Task", exact=True).fill("Fix it")
    started = time.monotonic()
    before = len(polls)
    dialog.get_by_role("button", name="Create workspace", exact=True).click()
    expect(page.locator("#workspace-progress")).to_contain_text("running: Fetching PR")
    # A running operation polls faster than the 15-second refresh, and stops when done.
    while len(polls) < before + 2:
        assert time.monotonic() - started < 8, "expected fast polling while running"
        page.wait_for_timeout(200)
    info["operation"].update(status="complete", message="Workspace ready")
    expect(page.locator("#workspace-progress")).to_contain_text("complete: Workspace ready")
    settled = len(polls)
    page.wait_for_timeout(3000)
    assert len(polls) == settled  # No fast polling once the operation has finished.


def test_clone_destination_mobile_and_protected_action_error(
    page, dashboard_site, workspace_routes
):
    url, _ = dashboard_site
    info, _, requests = workspace_routes
    info.update(matches=[], clones=[])
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(url + "/#prs")
    page.locator("#pr-list tr").first.get_by_role(
        "button", name="Clone and create", exact=True
    ).click()
    dialog = page.get_by_role("dialog")
    expect(dialog).to_contain_text("Clone test/alpha into /fixture/new")
    assert not requests
    dialog.get_by_label("Task", exact=True).fill("Fix the issue")
    page.route(
        "**/api/workspace-action",
        lambda route: route.fulfill(
            status=403, json={"error": "This action requires the configured dashboard"}
        ),
    )
    dialog.get_by_role("button", name="Clone and create", exact=True).click()
    expect(dialog.get_by_role("alert")).to_contain_text("configured dashboard")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.screenshot(path="reports/workspace-mobile.png")
    dialog.get_by_role("button", name="Close workspace actions").click()
    expect(dialog).not_to_be_visible()


@pytest.fixture
def ci_routes(page):
    checks = {
        "value": {
            "sha": "a" * 40,
            "state": "FAILURE",
            "truncated": False,
            "checks": [
                {
                    "id": "bad",
                    "name": "pytest (Python 3.14)",
                    "state": "FAILURE",
                    "bucket": "fail",
                    "workflow": "Unit tests",
                    "url": "https://github.com/test/alpha/actions/runs/1/job/2",
                    "description": "",
                    "has_details": True,
                },
                {
                    "id": "good",
                    "name": "lint",
                    "state": "SUCCESS",
                    "bucket": "pass",
                    "workflow": "Lint",
                    "url": "https://github.com/test/alpha/runs/3",
                    "description": "",
                    "has_details": False,
                },
            ],
        },
        "synced_at": 1789146000,
        "error": None,
        "refreshing": False,
        "stale": False,
    }
    failure = {
        "value": {
            "title": "1 failed test",
            "summary": "test_cache_atomic failed",
            "text": "",
            "annotations": [
                {
                    "title": "test_cache_atomic",
                    "message": '<img src=x onerror="window.injected=true"> AssertionError',
                    "path": "tests/test_cache.py",
                    "line": 12,
                }
            ],
            "truncated": False,
        },
        "error": None,
        "refreshing": False,
    }
    requests = []

    def handler(route):
        requests.append(route.request.url)
        route.fulfill(json=failure if "check=" in route.request.url else checks)

    page.route("**/api/pr-ci?*", handler)
    return checks, failure, requests


def test_ci_details_are_lazy_show_jobs_and_reported_tests(page, dashboard_site, ci_routes):
    url, _ = dashboard_site
    _, _, requests = ci_routes
    page.goto(url + "/#prs")
    expect(page.locator("#pr-list tr")).to_have_count(2)
    assert not requests
    page.get_by_role("link", name="CI Failed for test/alpha #8", exact=True).click()
    dialog = page.get_by_role("dialog", name="test/alpha #8 · CI")
    expect(dialog).to_contain_text("Failing checks (1)")
    expect(dialog.get_by_role("link", name="pytest (Python 3.14)", exact=True)).to_have_attribute(
        "href", "https://github.com/test/alpha/actions/runs/1/job/2"
    )
    expect(dialog).to_contain_text("Unit tests")
    expect(dialog).to_contain_text("Cached for 5 minutes")
    assert len(requests) == 1
    dialog.get_by_text("Reported failure details", exact=True).click()
    expect(dialog).to_contain_text("test_cache_atomic failed")
    expect(dialog).to_contain_text("tests/test_cache.py:12")
    expect(dialog.locator("img")).to_have_count(0)
    assert page.evaluate("window.injected === undefined")
    assert len(requests) == 2
    dialog.get_by_text("Reported failure details", exact=True).click()
    dialog.get_by_text("Reported failure details", exact=True).click()
    assert len(requests) == 2
    page.screenshot(path="reports/ci-desktop.png")


def test_ci_mobile_partial_results_and_missing_failure_output(page, dashboard_site, ci_routes):
    url, _ = dashboard_site
    checks, failure, _ = ci_routes
    checks["value"]["truncated"] = True
    failure["value"].update(title="", summary="", text="", annotations=[])
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(url + "/#prs")
    page.get_by_role("link", name="CI Failed for test/alpha #8", exact=True).click()
    dialog = page.get_by_role("dialog")
    expect(dialog).to_contain_text("list is incomplete")
    dialog.get_by_text("Reported failure details", exact=True).click()
    expect(dialog).to_contain_text("did not publish failure messages")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.screenshot(path="reports/ci-mobile.png")
    dialog.get_by_role("button", name="Close CI details").click()
    expect(dialog).not_to_be_visible()


def test_ci_stale_error_and_no_checks_are_distinct(page, dashboard_site, ci_routes):
    url, _ = dashboard_site
    checks, _, _ = ci_routes
    checks.update(error="GitHub rate limited", stale=True)
    page.goto(url + "/#prs")
    page.get_by_role("link", name="CI Failed for test/alpha #8", exact=True).click()
    expect(page.locator("#ci-error")).to_contain_text("rate limited")
    expect(page.locator("#ci-meta")).to_contain_text("Saved results may be out of date")
    expect(page.locator("#ci-content")).to_contain_text("pytest (Python 3.14)")
    page.get_by_role("button", name="Close CI details").click()
    checks["value"]["checks"] = []
    checks.update(error=None, stale=False)
    page.get_by_role("link", name="CI Failed for test/alpha #8", exact=True).click()
    expect(page.locator("#ci-content")).to_contain_text("No checks reported")


def test_ci_close_cancels_loading_polls(page, dashboard_site, ci_routes):
    url, _ = dashboard_site
    checks, _, requests = ci_routes
    checks.update(value=None, refreshing=True)
    page.goto(url + "/#prs")
    page.get_by_role("link", name="CI Failed for test/alpha #8", exact=True).click()
    expect(page.locator("#ci-meta")).to_contain_text("Loading checks")
    page.get_by_role("button", name="Close CI details").click()
    page.wait_for_timeout(2200)
    assert len(requests) == 1


def test_ci_automatic_log_progress_test_names_and_stream(page, dashboard_site, ci_routes):
    url, _ = dashboard_site
    results = [
        {"state": "downloading", "message": "Downloading failed-job log", "refreshing": True},
        {
            "state": "ready",
            "message": "Cached job log",
            "refreshing": False,
            "value": {
                "tests": ["tests/test_cache.py::test_atomic"],
                "failed_steps": ["Run pytest"],
                "text": '<img src=x onerror="window.injected=true"> AssertionError: cache missing',
                "truncated": True,
                "excerpt": True,
            },
        },
    ]
    requests = []

    def handler(route):
        requests.append(route.request.url)
        if "page=" in route.request.url:
            route.fulfill(
                json={
                    "page": 1,
                    "pages": 1,
                    "text": results[1]["value"]["text"],
                    "line_start": 1,
                    "line_end": 1,
                    "truncated": True,
                }
            )
        else:
            route.fulfill(json=results[min(len(requests) - 1, 1)])

    page.route("**/api/pr-ci-log?*", handler)
    page.goto(url + "/#prs")
    assert not requests
    page.get_by_role("link", name="CI Failed for test/alpha #8", exact=True).click()
    dialog = page.get_by_role("dialog")
    assert not requests
    dialog.get_by_text("Downloaded test log", exact=True).click()
    expect(dialog).to_contain_text("Downloading failed-job log")
    expect(dialog).to_contain_text("Downloads continue with the browser closed")
    expect(dialog).to_contain_text("Detected test failures")
    expect(dialog).to_contain_text("tests/test_cache.py::test_atomic")
    expect(dialog).to_contain_text("Run pytest")
    expect(dialog).to_contain_text("this log is incomplete")
    expect(dialog.locator("img")).to_have_count(0)
    expect(dialog.locator(".ci-log-viewer pre")).to_contain_text("AssertionError: cache missing")
    expect(dialog.get_by_role("link", name="Download cached log")).to_have_count(0)
    expect(dialog.locator(".ci-log-viewer button")).to_have_count(0)
    expect(dialog.locator(".ci-log-viewer")).to_contain_text(
        "End of cached log · This log is incomplete"
    )
    assert len(requests) == 3
    page.screenshot(path="reports/ci-logs-desktop.png")


@pytest.mark.parametrize(
    "state,message",
    [
        ("queued", "Hourly download budget reached; queued for the next available slot"),
        ("unavailable", "Job log unavailable after three attempts"),
        ("unsupported", "Automatic logs are available for failed GitHub Actions jobs."),
    ],
)
def test_ci_log_mobile_budget_and_unavailable_states(
    page, dashboard_site, ci_routes, state, message
):
    url, _ = dashboard_site
    page.route(
        "**/api/pr-ci-log?*",
        lambda route: route.fulfill(
            json={
                "state": state,
                "message": message,
                "refreshing": False,
                "error": "Job logs expired" if state == "unavailable" else None,
            }
        ),
    )
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(url + "/#prs")
    page.get_by_role("link", name="CI Failed for test/alpha #8", exact=True).click()
    dialog = page.get_by_role("dialog")
    dialog.get_by_text("Downloaded test log", exact=True).click()
    expect(dialog).to_contain_text(message)
    if state == "unavailable":
        expect(dialog).to_contain_text("Job logs expired")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.screenshot(path=f"reports/ci-logs-mobile-{state}.png")


@pytest.fixture
def visit_routes(page, dashboard_site):
    url, _ = dashboard_site
    snapshot = page.request.get(url + "/api/prs").json()
    page.route("**/api/prs", lambda route: route.fulfill(json=snapshot))
    return snapshot


def saved_visit(page, login="fixture"):
    return page.evaluate(
        "login => JSON.parse(localStorage.getItem(`babysit-pr:seen-prs:v1:${login}`))", login
    )


def test_pr_visit_highlights_fields_and_new_rows_until_next_visit(
    page, dashboard_site, visit_routes
):
    url, _ = dashboard_site
    page.goto(url + "/#prs")
    expect(page.locator("#pr-list tr")).to_have_count(2)
    expect(page.locator(".pr-changed, .pr-new")).to_have_count(0)
    expect(page.locator("#pr-changes")).to_contain_text("from this visit onward")
    assert saved_visit(page)["prs"]["pr-one"]["ci"] == "FAILURE"
    visit_routes["synced_at"] += 1
    visit_routes["prs"][0].update(ci="SUCCESS", review_decision="APPROVED", head_sha="b" * 40)
    new = copy.deepcopy(visit_routes["prs"][1])
    new.update(id="pr-new", title="A newly discovered PR", number=10)
    visit_routes["prs"].append(new)
    page.reload()
    expect(page.locator("#pr-changes")).to_contain_text("1 new · 1 updated")
    changed = page.locator(".pr-changed")
    expect(changed.locator(".pr-change-note")).to_have_text("CI · Review · New commits")
    expect(changed.locator(".pr-field-changed")).to_have_count(3)
    expect(changed.locator(".pr-approved")).to_have_text("Approved")
    expect(page.locator(".pr-new .pr-change-badge")).to_have_text("New")
    assert saved_visit(page)["prs"]["pr-one"]["ci"] == "SUCCESS"
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(changed).to_have_count(1)
    page.get_by_label("Search pull requests").fill("missing")
    expect(page.locator(".pr-changed")).to_have_count(0)
    page.get_by_label("Search pull requests").fill("")
    expect(changed).to_have_count(1)
    page.screenshot(path="reports/pr-visit-desktop.png")
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.screenshot(path="reports/pr-visit-mobile.png")
    page.reload()
    expect(page.locator("#pr-changes")).to_contain_text("No changes since your last visit")
    expect(page.locator(".pr-changed, .pr-new")).to_have_count(0)


def test_watcher_tab_does_not_consume_pr_changes(page, dashboard_site, visit_routes):
    url, _ = dashboard_site
    page.goto(url)
    expect(page.locator("#pr-list tr")).to_have_count(2)
    assert saved_visit(page) is None
    page.get_by_role("tab", name="Pull requests", exact=True).click()
    expect(page.locator("#pr-changes")).to_be_visible()
    old = saved_visit(page)
    page.get_by_role("tab", name="Watcher", exact=True).click()
    visit_routes["synced_at"] += 1
    visit_routes["prs"][0]["ci"] = "SUCCESS"
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(page.locator("#pr-list tr").first).to_contain_text("Passed")
    assert saved_visit(page) == old
    page.get_by_role("tab", name="Pull requests", exact=True).click()
    expect(page.locator(".pr-changed")).to_have_count(1)
    assert saved_visit(page)["prs"]["pr-one"]["ci"] == "SUCCESS"


def test_pr_visits_ignore_role_order_and_show_other_activity(page, dashboard_site, visit_routes):
    url, _ = dashboard_site
    page.goto(url + "/#prs")
    expect(page.locator("#pr-list tr")).to_have_count(2)
    visit_routes["prs"][0]["roles"].reverse()
    visit_routes["synced_at"] += 1
    page.reload()
    expect(page.locator(".pr-changed")).to_have_count(0)
    visit_routes["prs"][0]["updated_at"] = "2026-09-12T12:00:00Z"
    visit_routes["synced_at"] += 1
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(page.locator(".pr-change-note")).to_have_text("PR activity changed")
    expect(page.locator(".pr-updated.pr-field-changed")).to_have_count(1)


def test_failed_or_inflight_sync_does_not_replace_saved_visit(page, dashboard_site, visit_routes):
    url, _ = dashboard_site
    page.goto(url + "/#prs")
    expect(page.locator("#pr-list tr")).to_have_count(2)
    old = saved_visit(page)
    visit_routes["synced_at"] += 1
    visit_routes["prs"][0]["ci"] = "SUCCESS"
    visit_routes["error"] = "Offline"
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(page.locator("#pr-alert")).to_contain_text("Offline")
    assert saved_visit(page) == old
    visit_routes.update(error=None, refreshing=True)
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(page.locator("#pr-sync")).to_contain_text("Syncing")
    assert saved_visit(page) == old
    visit_routes["refreshing"] = False
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(page.locator("#pr-sync")).not_to_contain_text("Syncing")
    assert saved_visit(page)["prs"]["pr-one"]["ci"] == "SUCCESS"


def test_visit_storage_accounts_and_older_tabs_are_isolated(page, dashboard_site, visit_routes):
    url, _ = dashboard_site
    page.goto(url + "/#prs")
    expect(page.locator("#pr-list tr")).to_have_count(2)
    old = saved_visit(page)
    newer = copy.deepcopy(old)
    newer["synced_at"] += 20
    page.evaluate(
        "v => localStorage.setItem('babysit-pr:seen-prs:v1:fixture', JSON.stringify(v))", newer
    )
    visit_routes["synced_at"] += 1
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(page.locator("#pr-list tr")).to_have_count(2)
    assert saved_visit(page) == newer
    page.reload()
    expect(page.locator("#pr-changes")).to_contain_text("Waiting for a current PR snapshot")
    expect(page.locator(".pr-changed, .pr-new")).to_have_count(0)
    assert saved_visit(page) == newer
    visit_routes["login"] = "second-account"
    visit_routes["prs"][0]["ci"] = "SUCCESS"
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(page.locator("#pr-sync")).to_contain_text("@second-account")
    expect(page.locator(".pr-changed, .pr-new")).to_have_count(0)
    assert saved_visit(page) == newer
    assert saved_visit(page, "second-account")["prs"]["pr-one"]["ci"] == "SUCCESS"


@pytest.mark.parametrize("mode", ["blocked", "corrupt"])
def test_unavailable_or_corrupt_visit_storage_keeps_dashboard_usable(
    page, dashboard_site, visit_routes, mode
):
    url, _ = dashboard_site
    if mode == "blocked":
        page.add_init_script(
            "Storage.prototype.getItem = () => { throw new DOMException('blocked', 'SecurityError'); };"
        )
    else:
        page.add_init_script("localStorage.setItem('babysit-pr:seen-prs:v1:fixture', '{broken');")
    page.goto(url + "/#prs")
    expect(page.locator("#pr-list tr")).to_have_count(2)
    expect(page.locator(".pr-changed, .pr-new")).to_have_count(0)
    if mode == "blocked":
        expect(page.locator("#pr-changes")).to_contain_text("storage is unavailable")
    else:
        assert saved_visit(page)["prs"]["pr-one"]["ci"] == "FAILURE"
    visit_routes["prs"][0]["ci"] = "SUCCESS"
    visit_routes["synced_at"] += 1
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(page.locator(".pr-changed")).to_have_count(1)


@pytest.fixture
def paged_log_routes(page, ci_routes):
    requests = []
    state = {"fail": False, "delay": False, "pending": [], "short": False}

    def handler(route):
        from urllib.parse import parse_qs, urlsplit

        params = parse_qs(urlsplit(route.request.url).query)
        requests.append(params)
        if "page" not in params:
            route.fulfill(
                json={
                    "state": "ready",
                    "message": "Cached job log",
                    "refreshing": False,
                    "value": {
                        "tests": ["test_atomic"],
                        "failed_steps": ["Run tests"],
                        "text": "Failure excerpt only",
                        "truncated": False,
                        "excerpt": True,
                    },
                }
            )
            return
        if state["delay"]:
            state["pending"].append(route)
            return
        if state["fail"]:
            route.fulfill(status=503, json={"error": "Cached log temporarily unavailable"})
            return
        number = int(params["page"][0])
        route.fulfill(
            json={
                "page": number,
                "pages": 3,
                "text": log_page_text(number, state["short"]),
                "line_start": number * 100 - 99,
                "line_end": number * 100,
                "truncated": False,
            }
        )

    page.route("**/api/pr-ci-log?*", handler)
    return requests, state


def log_page_text(number, short=False):
    return f"Full log page {number}\n<img src=x onerror=alert(1)>\n" + (
        "" if short else "ordinary log output\n" * 100
    )


def open_scrolling_log(page, url):
    page.goto(url + "/#prs")
    page.get_by_role("link", name="CI Failed for test/alpha #8", exact=True).click()
    page.get_by_text("Downloaded test log", exact=True).click()
    expect(page.locator(".ci-log-viewer pre")).to_contain_text("Full log page 1")


def scroll_log_to_end(page):
    page.locator(".ci-log-viewer pre").evaluate("node => { node.scrollTop = node.scrollHeight; }")


def test_log_scroll_appends_pages_preserves_position_and_has_no_controls(
    page, dashboard_site, paged_log_routes
):
    url, _ = dashboard_site
    requests, _ = paged_log_routes
    open_scrolling_log(page, url)
    viewer = page.locator(".ci-log-viewer")
    output = viewer.locator("pre")
    expect(viewer.locator("button, input, a, img")).to_have_count(0)
    expect(page.get_by_role("button", name="Read full cached log", exact=True)).to_have_count(0)
    expect(page.get_by_role("link", name="Download cached log", exact=True)).to_have_count(0)
    assert [q["page"][0] for q in requests if "page" in q] == ["1"]
    old_top = output.evaluate(
        "node => { node.scrollTop = node.scrollHeight; return node.scrollTop; }"
    )
    expect(output).to_contain_text("Full log page 2")
    assert abs(output.evaluate("node => node.scrollTop") - old_top) <= 1
    assert output.text_content() == log_page_text(1) + log_page_text(2)
    scroll_log_to_end(page)
    expect(viewer).to_contain_text("End of cached log")
    assert output.text_content() == "".join(log_page_text(n) for n in range(1, 4))
    scroll_log_to_end(page)
    page.wait_for_timeout(200)
    assert [q["page"][0] for q in requests if "page" in q] == ["1", "2", "3"]
    assert not any("download" in q for q in requests)
    page.screenshot(path="reports/ci-log-scroll-desktop.png")
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.screenshot(path="reports/ci-log-scroll-mobile.png")


def test_log_scroll_fills_short_pages_automatically(page, dashboard_site, paged_log_routes):
    url, _ = dashboard_site
    requests, state = paged_log_routes
    state["short"] = True
    open_scrolling_log(page, url)
    expect(page.locator(".ci-log-viewer")).to_contain_text("End of cached log")
    assert page.locator(".ci-log-viewer pre").text_content() == "".join(
        log_page_text(n, True) for n in range(1, 4)
    )
    assert [q["page"][0] for q in requests if "page" in q] == ["1", "2", "3"]


def test_log_scroll_retries_transparently_and_keeps_loaded_output(
    page, dashboard_site, paged_log_routes
):
    url, _ = dashboard_site
    requests, state = paged_log_routes
    open_scrolling_log(page, url)
    state["fail"] = True
    scroll_log_to_end(page)
    viewer = page.locator(".ci-log-viewer")
    expect(viewer).to_contain_text("Cached log temporarily unavailable")
    expect(viewer).to_contain_text("Retrying")
    assert viewer.locator("pre").text_content() == log_page_text(1)
    state["fail"] = False
    expect(viewer.locator("pre")).to_contain_text("Full log page 2")
    assert viewer.locator("pre").text_content() == log_page_text(1) + log_page_text(2)
    expect(viewer.locator("button")).to_have_count(0)
    assert [q["page"][0] for q in requests if "page" in q] == ["1", "2", "2"]


def test_log_scroll_bounds_retries_and_reopening_allows_recovery(
    page, dashboard_site, paged_log_routes
):
    url, _ = dashboard_site
    requests, state = paged_log_routes
    open_scrolling_log(page, url)
    state["fail"] = True
    scroll_log_to_end(page)
    expect(page.locator(".ci-log-viewer")).to_contain_text(
        "Collapse and reopen this log to retry", timeout=10000
    )
    assert [q["page"][0] for q in requests if "page" in q] == ["1", "2", "2", "2"]
    state["fail"] = False
    page.get_by_text("Downloaded test log", exact=True).click()
    page.get_by_text("Downloaded test log", exact=True).click()
    expect(page.locator(".ci-log-viewer pre")).to_contain_text("Full log page 2")


def test_collapsed_log_does_not_fetch_until_reopened(page, dashboard_site, paged_log_routes):
    url, _ = dashboard_site
    requests, _ = paged_log_routes
    open_scrolling_log(page, url)
    page.get_by_text("Downloaded test log", exact=True).click()
    page.locator(".ci-log-viewer pre").dispatch_event("scroll")
    page.wait_for_timeout(200)
    assert [q["page"][0] for q in requests if "page" in q] == ["1"]
    page.get_by_text("Downloaded test log", exact=True).click()
    scroll_log_to_end(page)
    expect(page.locator(".ci-log-viewer pre")).to_contain_text("Full log page 2")


def test_log_scroll_serializes_requests_and_closing_cancels_pending_read(
    page, dashboard_site, paged_log_routes
):
    url, _ = dashboard_site
    requests, state = paged_log_routes
    open_scrolling_log(page, url)
    state["delay"] = True
    scroll_log_to_end(page)
    expect(page.locator(".ci-log-viewer")).to_contain_text("Loading more")
    for _ in range(5):
        page.locator(".ci-log-viewer pre").dispatch_event("scroll")
    assert [q["page"][0] for q in requests if "page" in q] == ["1", "2"]
    with page.expect_event("requestfailed", predicate=lambda request: "page=2" in request.url):
        page.get_by_role("button", name="Close CI details", exact=True).click()
    expect(page.get_by_role("dialog")).not_to_be_visible()
    state["delay"] = False
    state["pending"][0].fulfill(
        json={
            "page": 2,
            "pages": 3,
            "text": "Late output",
            "line_start": 101,
            "line_end": 200,
            "truncated": False,
        }
    )
    page.get_by_role("link", name="CI Failed for test/alpha #8", exact=True).click()
    page.get_by_text("Downloaded test log", exact=True).click()
    expect(page.locator(".ci-log-viewer pre")).to_contain_text("Full log page 1")
    expect(page.locator(".ci-log-viewer pre")).not_to_contain_text("Late output")


def test_issue_overview_columns_filters_linked_prs_and_safe_titles(
    page: Page, dashboard_site
) -> None:
    url, home = dashboard_site
    page.goto(url + "/#issues")
    rows = page.locator("#issue-list tr")
    expect(rows).to_have_count(2)
    expect(page.get_by_role("columnheader", name="Linked PRs", exact=True)).to_be_visible()
    first = rows.first
    expect(first.locator(".pr-repo")).to_have_text("test/alpha")
    expect(first.locator(".pr-description")).to_contain_text("#30")
    expect(first.locator(".pr-description")).to_contain_text('<img src=x onerror="window.injected')
    expect(first.locator("img")).to_have_count(0)
    assert page.evaluate("window.injected === undefined")
    expect(first.locator(".pr-label")).to_have_text("kind/bug")
    expect(first.locator(".pr-assignees")).to_have_text("colleague")
    expect(first.locator(".pr-comments")).to_have_text("4")
    expect(first.locator(".pr-roles .badge").first).to_have_text("Author")
    expect(first.locator(".pr-updated time")).to_have_attribute("datetime", "2026-09-11T09:00:00Z")
    linked = first.get_by_role("link", name="Pull request test/alpha #8", exact=True)
    expect(linked).to_have_attribute("href", "https://github.com/test/alpha/pull/8")
    expect(linked).to_have_text("#8")
    # CI comes from the PR overview because #8 is already there; nothing is fetched per issue.
    expect(
        first.get_by_role("link", name="CI Failed for test/alpha #8", exact=True)
    ).to_have_attribute("href", "https://github.com/test/alpha/pull/8/checks")
    expect(rows.last.locator(".pr-linked")).to_have_text("None")
    expect(rows.last.locator(".pr-assignees")).to_have_text("Unassigned")
    expect(page.locator("#issue-sync")).to_contain_text("@fixture")
    expect(page.locator("#issue-sync")).to_contain_text("Open issues")
    expect(page.locator("#pr-sync")).to_contain_text("Open PRs")
    choose_option(
        page.get_by_role("combobox", name="Filter issues by role", exact=True), "assignee"
    )
    expect(rows).to_have_count(1)
    expect(rows).to_contain_text(["Assigned issue"])
    choose_option(page.get_by_role("combobox", name="Filter issues by role", exact=True), "all")
    label = page.get_by_role("combobox", name="Filter issues by label", exact=True)
    expect(page.locator("#issue-label option")).to_have_text(["All labels", "kind/bug"])
    choose_option(label, "kind/bug")
    expect(rows).to_have_count(1)
    expect(rows.first.locator(".pr-repo")).to_have_text("test/alpha")
    choose_option(label, "all")
    choose_option(
        page.get_by_role("combobox", name="Filter issues by linked pull requests", exact=True),
        "unlinked",
    )
    expect(rows).to_have_count(1)
    expect(rows.first.locator(".pr-repo")).to_have_text("test/beta")
    choose_option(
        page.get_by_role("combobox", name="Filter issues by linked pull requests", exact=True),
        "linked",
    )
    expect(rows.first.locator(".pr-repo")).to_have_text("test/alpha")
    choose_option(
        page.get_by_role("combobox", name="Filter issues by linked pull requests", exact=True),
        "all",
    )
    choose_option(
        page.get_by_role("combobox", name="Filter issues by repository", exact=True), "test/beta"
    )
    expect(rows).to_have_count(1)
    choose_option(
        page.get_by_role("combobox", name="Filter issues by repository", exact=True), "all"
    )
    page.get_by_label("Search issues").fill("colleague")
    expect(rows).to_have_count(2)  # Assignee on one issue, author of the other.
    page.get_by_label("Search issues").fill("kind/bug")
    expect(rows).to_have_count(1)
    page.get_by_label("Search issues").fill("missing")
    expect(page.locator("#issue-empty")).to_have_text("No matching issues.")
    page.get_by_label("Search issues").fill("")
    assert len(dashboard.read_jobs(home)) == 3  # Discovery creates no repair watches.
    expect(page.locator("#pr-list tr")).to_have_count(2)  # The PR tab is untouched.
    page.screenshot(path="reports/issues-desktop.png")
    page.set_viewport_size({"width": 390, "height": 844})
    expect(rows).to_have_count(2)
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.screenshot(path="reports/issues-mobile.png")


@pytest.mark.parametrize(
    "column,first,second",
    [
        ("Repository", "test/alpha", "test/beta"),
        ("Issue", "test/alpha", "test/beta"),
        ("Author", "test/beta", "test/alpha"),
        ("Assignees", "test/alpha", "test/alpha"),  # Unassigned stays at the bottom.
        ("Your role", "test/beta", "test/alpha"),
        ("Comments", "test/beta", "test/alpha"),
        ("Linked PRs", "test/beta", "test/alpha"),
        ("Opened", "test/beta", "test/alpha"),
        ("Last updated", "test/beta", "test/alpha"),
    ],
)
def test_each_issue_column_sorts_both_directions(
    page: Page, dashboard_site, column, first, second
) -> None:
    url, _ = dashboard_site
    page.goto(url + "/#issues")
    rows = page.locator("#issue-list tr")
    expect(rows).to_have_count(2)
    button = page.get_by_role("button", name=f"Sort by {column}", exact=True)
    button.click()
    expect(rows.first.locator(".pr-repo")).to_have_text(first)
    button.click()
    expect(rows.first.locator(".pr-repo")).to_have_text(second)
    heading = button.locator("xpath=ancestor::th")
    expect(heading).to_have_attribute(
        "aria-sort", "ascending" if column == "Opened" else "descending"
    )
    expect(page.locator("#issue-heading-dates")).to_have_attribute(
        "aria-sort",
        "descending" if column == "Last updated" else "ascending" if column == "Opened" else "none",
    )
    page.set_viewport_size({"width": 390, "height": 844})
    expect(page.get_by_role("combobox", name="Sort issues by", exact=True)).to_be_visible()
    choose_option(page.get_by_role("combobox", name="Sort issues by", exact=True), "comments")
    if column == "Comments":
        # Re-selecting the current value preserves its direction, like a native picker.
        expect(rows.first.locator(".pr-repo")).to_have_text("test/alpha")
        page.get_by_role("button", name="Descending", exact=True).click()
    expect(rows.first.locator(".pr-repo")).to_have_text("test/beta")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


def test_issue_sync_failure_and_empty_state_are_distinct(page: Page, dashboard_site) -> None:
    url, _ = dashboard_site
    page.route(
        "**/api/issues",
        lambda route: route.fulfill(
            json={"issues": [], "synced_at": None, "error": "GitHub authentication failed"}
        ),
    )
    page.goto(url + "/#issues")
    expect(page.locator("#issue-alert")).to_contain_text("GitHub authentication failed")
    expect(page.locator("#issue-empty")).to_contain_text("Issue data is unavailable")
    expect(page.locator("#pr-alert")).to_be_hidden()
    page.unroute("**/api/issues")
    page.route(
        "**/api/issues",
        lambda route: route.fulfill(json={"issues": [], "synced_at": 1234, "error": None}),
    )
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(page.locator("#issue-empty")).to_have_text("No open issues for your roles.")
    expect(page.locator("#issue-alert")).to_be_hidden()


def test_issue_visit_tracking_is_separate_from_prs(page: Page, dashboard_site) -> None:
    url, _ = dashboard_site
    snapshot = page.request.get(url + "/api/issues").json()
    page.route("**/api/issues", lambda route: route.fulfill(json=snapshot))
    page.goto(url + "/#issues")
    expect(page.locator("#issue-list tr")).to_have_count(2)
    expect(page.locator("#issue-changes")).to_contain_text("from this visit onward")
    saved = page.evaluate("JSON.parse(localStorage.getItem('babysit-pr:seen-issues:v1:fixture'))")
    assert saved["issues"]["issue-one"]["comments"] == 4
    assert saved["issues"]["issue-one"]["linked_prs"] == ["test/alpha#8"]
    assert "prs" not in saved
    assert page.evaluate("localStorage.getItem('babysit-pr:seen-prs:v1:fixture')") is None
    snapshot["synced_at"] += 1
    snapshot["issues"][0].update(comments=5, linked_prs=[], labels=[])
    new = copy.deepcopy(snapshot["issues"][1])
    new.update(id="issue-new", title="A newly discovered issue", number=32)
    snapshot["issues"].append(new)
    page.reload()
    expect(page.locator("#issue-changes")).to_contain_text("1 new · 1 updated")
    changed = page.locator("#issue-list .pr-changed")
    expect(changed.locator(".pr-change-note")).to_have_text("Labels · Comments · Linked PRs")
    expect(changed.locator(".pr-field-changed")).to_have_count(3)
    expect(page.locator("#issue-list .pr-new .pr-change-badge")).to_have_text("New")
    expect(page.locator("#pr-list .pr-changed, #pr-list .pr-new")).to_have_count(0)
    page.reload()
    expect(page.locator("#issue-changes")).to_contain_text("No changes since your last visit")


@pytest.fixture
def issue_workspace_routes(page):
    info = {
        "matches": [],
        "suggestions": ["/fixture/other-repo/issue-30"],
        "clones": ["/fixture/alpha"],
        "preferred_clone": "/fixture/alpha",
        "destination": None,
        "operation": None,
    }
    linked_match = {
        "path": "/fixture/alpha-fix-30",
        "workspace_id": "w7",
        "name": "Fix 30 workspace",
        "agent_status": "idle",
        "url": "https://mac-mini.tailfb45be.ts.net/space/w7",
        "linked_pr": 8,
    }
    snapshot = {
        "prs": {"pr-one": {**copy.deepcopy(info), "suggestions": []}},
        "issues": {
            "issue-one": info,
            "issue-two": {**copy.deepcopy(info), "matches": [linked_match], "suggestions": []},
        },
        "error": None,
        "synced_at": 1234,
    }
    requests = []
    page.route("**/api/workspaces", lambda route: route.fulfill(json=snapshot))

    def action(route):
        body = route.request.post_data_json
        requests.append(body)
        if body["action"] in {"open", "focus"}:
            route.fulfill(json={"result": linked_match})
        else:
            op = {"id": "op2", "status": "running", "message": "Fetching issue", "log": "wti"}
            info["operation"] = op
            route.fulfill(json={"operation": op})

    page.route("**/api/workspace-action", action)
    return info, snapshot, requests


def test_issue_workspace_create_and_linked_pr_match(page, dashboard_site, issue_workspace_routes):
    url, _ = dashboard_site
    info, _, requests = issue_workspace_routes
    page.goto(url + "/#issues")
    rows = page.locator("#issue-list tr")
    expect(rows).to_have_count(2)
    expect(rows.last.locator(".pr-actions")).to_contain_text("Via PR #8")
    rows.first.get_by_role("button", name="Create workspace", exact=True).click()
    dialog = page.get_by_role("dialog")
    expect(dialog).to_contain_text("test/alpha #30")
    expect(dialog).to_contain_text(
        "Unverified branch-name suggestions: /fixture/other-repo/issue-30"
    )
    expect(dialog.get_by_label("Agent", exact=True)).to_have_value("codex")
    dialog.get_by_role("button", name="Create workspace", exact=True).click()
    assert not requests
    dialog.get_by_label("Task", exact=True).fill("Reproduce the crash and fix it")
    choose_option(dialog.get_by_label("Agent", exact=True), "claude")
    dialog.get_by_role("button", name="Create workspace", exact=True).click()
    expect(page.locator("#workspace-progress")).to_contain_text("Fetching issue")
    assert requests == [
        {
            "id": "issue-one",
            "action": "create",
            "agent": "claude",
            "task": "Reproduce the crash and fix it",
            "clone": "/fixture/alpha",
            "retry": False,
        }
    ]
    info["operation"].update(
        status="complete",
        message="Workspace ready",
        result={"url": "https://mac-mini.tailfb45be.ts.net/space/w8"},
    )
    page.evaluate("refreshWorkspaces()")
    expect(dialog.get_by_role("link", name="Open in Collie")).to_have_attribute(
        "href", "https://mac-mini.tailfb45be.ts.net/space/w8"
    )
    dialog.get_by_role("button", name="Close workspace actions").click()
    page.evaluate("window.open = () => { window.opened = {location: {}}; return window.opened; }")
    rows.last.get_by_role("button", name="Open workspace", exact=True).click()
    expect(page.locator("#workspace-result a")).to_have_attribute(
        "href", "https://mac-mini.tailfb45be.ts.net/space/w7"
    )
    assert requests[-1] == {
        "id": "issue-two",
        "action": "open",
        "path": "/fixture/alpha-fix-30",
        "workspace_id": "w7",
    }
    page.screenshot(path="reports/issue-workspace-desktop.png")


@pytest.mark.parametrize("kind,prefix", [("prs", "pr"), ("issues", "issue")])
def test_searchable_picker_keyboard_clear_and_dismissal(page, dashboard_site, kind, prefix):
    url, _ = dashboard_site
    page.goto(f"{url}/#{kind}")
    native = page.locator(f"#{prefix}-repo")
    picker = page.get_by_role(
        "combobox", name=f"Filter {'pull requests' if kind == 'prs' else 'issues'} by repository"
    )
    options = page.get_by_role("listbox").get_by_role("option")
    expect(native.locator("option")).to_have_count(3)
    native.evaluate(
        "node => { window.selectionChanges = 0; node.addEventListener('change', () => window.selectionChanges++); }"
    )
    picker.click()
    expect(options).to_have_text(["All repositories", "test/alpha", "test/beta"])
    picker.fill("TEST/")
    expect(options).to_have_text(["test/alpha", "test/beta"])
    picker.press("ArrowDown")
    picker.press("ArrowUp")
    picker.press("ArrowDown")
    active = picker.get_attribute("aria-activedescendant")
    expect(page.locator(f"#{active}")).to_have_text("test/beta")
    picker.press("Enter")
    expect(native).to_have_value("test/beta")
    expect(picker).to_have_value("test/beta")
    expect(picker).to_be_focused()
    expect(picker).to_have_attribute("aria-expanded", "false")
    assert page.evaluate("window.selectionChanges") == 1
    expect(page.locator(f"#{prefix}-list tr")).to_have_count(1)

    # Typing while focus stays on the committed value must reopen the list too.
    picker.press_sequentially("missing repository")
    expect(options).to_have_count(0)
    expect(page.locator(".select-status:visible")).to_have_text("No matches")
    assert picker.get_attribute("aria-activedescendant") is None
    picker.press("Enter")
    expect(native).to_have_value("test/beta")
    page.get_by_role("button", name="Clear search for", exact=False).click()
    expect(picker).to_have_value("")
    expect(options).to_have_count(3)
    expect(native).to_have_value("test/beta")
    picker.fill("alpha")
    picker.press("Escape")
    expect(picker).to_have_value("test/beta")
    expect(picker).to_be_focused()
    picker.press("ArrowDown")
    expect(options).to_have_count(3)
    picker.fill("alpha")
    page.locator(f"#{prefix}-sync").click()  # Non-focusable outside target.
    expect(picker).to_have_attribute("aria-expanded", "false")
    expect(picker).to_have_value("test/beta")
    picker.click()
    picker.fill("alpha")
    picker.press("Tab")
    expect(picker).to_have_attribute("aria-expanded", "false")
    assert page.evaluate("window.selectionChanges") == 1
    choose_option(picker, "all")
    expect(page.locator(f"#{prefix}-list tr")).to_have_count(2)
    assert page.evaluate("window.selectionChanges") == 2


@pytest.mark.parametrize("kind,prefix", [("prs", "pr"), ("issues", "issue")])
def test_searchable_picker_refresh_preserves_query_selection_and_hidden_filters(
    page, dashboard_site, kind, prefix
):
    url, _ = dashboard_site
    snapshot = page.request.get(f"{url}/api/{kind}").json()
    page.route(f"**/api/{kind}", lambda route: route.fulfill(json=snapshot))
    page.goto(f"{url}/#{kind}")
    picker = page.locator(f"#{prefix}-repo").locator("..").get_by_role("combobox")
    choose_option(picker, "test/beta")
    picker.fill("alp")
    expect(page.get_by_role("option")).to_have_text(["test/alpha"])
    picker.press("ArrowLeft")
    caret = picker.evaluate("node => node.selectionStart")
    snapshot[kind][1]["repo"] = "test/alpine"
    page.evaluate(f"{prefix}Table.refresh()")
    expect(page.get_by_role("option")).to_have_text(["test/alpha", "test/alpine"])
    expect(picker).to_have_value("alp")
    expect(picker).to_be_focused()
    assert picker.evaluate("node => node.selectionStart") == caret
    expect(page.locator(f"#{prefix}-repo")).to_have_value("test/beta")
    expect(page.locator(f"#{prefix}-list tr")).to_have_count(0)
    picker.press("Escape")
    page.get_by_role("tab", name="Watcher", exact=True).click()
    snapshot[kind] = []
    page.evaluate(f"{prefix}Table.refresh()")
    page.get_by_role("tab", name="Pull requests" if kind == "prs" else "Issues", exact=True).click()
    expect(picker).to_have_value("test/beta")
    picker.click()
    expect(page.get_by_role("option")).to_have_text(["All repositories", "test/beta"])


@pytest.mark.parametrize("width", [1280, 390])
def test_searchable_long_plain_text_choices_and_layout(page, dashboard_site, width):
    url, _ = dashboard_site
    snapshot = page.request.get(f"{url}/api/issues").json()
    label = "team/" + "very-long-label-" * 8 + '<img src=x onerror="window.injected=true">'
    snapshot["issues"][0]["labels"] = [{"name": label, "color": "ffffff"}]
    page.route("**/api/issues", lambda route: route.fulfill(json=snapshot))
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(f"{url}/#issues")
    picker = page.get_by_role("combobox", name="Filter issues by label", exact=True)
    picker.fill("TEAM/")
    option = page.get_by_role("option")
    expect(option).to_have_text(label)
    assert option.evaluate("node => node.scrollWidth <= node.clientWidth")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.screenshot(path=f"reports/searchable-options-{width}.png", full_page=True)
    option.click()
    selected = picker.locator("xpath=../..").locator(".select-value")
    expect(selected).to_have_text(label)
    expect(selected).to_be_visible()
    assert selected.evaluate("node => node.scrollWidth <= node.clientWidth")
    expect(page.locator(".searchable-select img")).to_have_count(0)
    assert page.evaluate("window.injected") is None
    page.screenshot(path=f"reports/searchable-selected-{width}.png", full_page=True)
    if width == 390:
        sort = page.get_by_role("combobox", name="Sort issues by", exact=True)
        sort.fill("Last")
        expect(page.get_by_role("option")).to_have_text(["Last updated"])
        popup = page.locator(".select-popup:visible")
        bounds = popup.bounding_box()
        assert bounds and bounds["x"] >= 0 and bounds["x"] + bounds["width"] <= width
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        page.screenshot(path="reports/searchable-sort-mobile.png", full_page=True)


def test_searchable_workspace_clone_validation_and_escape(page, dashboard_site, workspace_routes):
    url, _ = dashboard_site
    info, _, requests = workspace_routes
    info["matches"] = []
    info["clones"] = ["/fixture/one", "/fixture/" + "long-clone-name/" * 8 + "two"]
    page.set_viewport_size({"width": 390, "height": 900})
    page.goto(f"{url}/#prs")
    page.locator("#pr-list tr").first.get_by_role(
        "button", name="Create workspace", exact=True
    ).click()
    dialog = page.get_by_role("dialog")
    clone = dialog.get_by_role("combobox", name="Local clone", exact=True)
    dialog.get_by_label("Task", exact=True).fill("Fix the issue")
    dialog.get_by_role("button", name="Create workspace", exact=True).click()
    expect(clone).to_be_focused()
    assert not requests
    clone.fill("two")
    expect(dialog.get_by_role("listbox").get_by_role("option")).to_have_text([info["clones"][1]])
    page.screenshot(path="reports/searchable-clone-mobile.png", full_page=True)
    clone.press("Escape")
    expect(dialog).to_be_visible()
    expect(clone).to_have_attribute("aria-expanded", "false")
    clone.fill("two")
    clone.press("Enter")
    expect(page.locator("#workspace-clone")).to_have_value(info["clones"][1])
    dialog.get_by_role("button", name="Create workspace", exact=True).click()
    expect(page.locator("#workspace-progress")).to_contain_text("Fetching PR")
    assert requests[-1]["clone"] == info["clones"][1]


@pytest.mark.parametrize("kind", ["pr", "issue"])
@pytest.mark.parametrize("clone", [False, True])
@pytest.mark.parametrize("settings", ["default", "codex", "claude"])
def test_workspace_optional_model_effort(page, dashboard_site, request, kind, clone, settings):
    routes = request.getfixturevalue(
        "workspace_routes" if kind == "pr" else "issue_workspace_routes"
    )
    info, snapshot, requests = routes
    info["matches"] = []
    if clone:
        info["clones"] = []
        info["destination"] = "/fixture/new"
    snapshot["agent_choices"] = {
        "codex": {
            "models": [{"id": "fixture-codex", "efforts": ["low", "ultra"]}],
            "efforts": ["low", "ultra"],
        },
        "claude": {
            "models": [{"id": "opus", "efforts": ["low", "high"]}, {"id": "haiku", "efforts": []}],
            "efforts": ["low", "high"],
        },
    }
    url, _ = dashboard_site
    page.goto(url + ("/#prs" if kind == "pr" else "/#issues"))
    page.locator("#pr-list tr" if kind == "pr" else "#issue-list tr").first.locator(
        ".pr-actions button"
    ).first.click()
    dialog = page.get_by_role("dialog")
    model = dialog.get_by_role("combobox", name="Model (optional)", exact=True)
    native_model = dialog.locator("#workspace-model")
    effort = dialog.get_by_role("combobox", name="Reasoning effort (optional)", exact=True)
    native_effort = dialog.locator("#workspace-effort")
    expect(model).to_have_value("Default")
    expect(effort).to_have_value("Default")
    expect(native_model.locator("option").first).to_have_text("Default")
    expect(native_effort.locator("option").first).to_have_text("Default")
    choose_option(model, "fixture-codex")
    choose_option(effort, "ultra")
    dialog.get_by_label("Agent", exact=True).select_option("claude")
    expect(model).to_have_value("Default")
    expect(effort).to_have_value("Default")
    expect(native_model.locator("option[value=fixture-codex]")).to_have_count(0)
    expect(native_effort.locator("option[value=ultra]")).to_have_count(0)
    choose_option(model, "opus")
    choose_option(effort, "high")
    choose_option(model, "haiku")
    expect(effort).to_have_value("Default")
    expect(native_effort.locator("option")).to_have_count(1)
    dialog.get_by_label("Agent", exact=True).select_option("codex")
    if settings != "default":
        dialog.get_by_label("Agent", exact=True).select_option(settings)
        choose_option(model, "fixture-codex" if settings == "codex" else "opus")
        choose_option(effort, "ultra" if settings == "codex" else "high")
    dialog.get_by_label("Task", exact=True).fill("Fix this")
    if kind == "pr" and not clone and settings == "codex":
        page.screenshot(path="reports/workspace-model-effort-desktop.png")
        page.set_viewport_size({"width": 390, "height": 844})
        page.screenshot(path="reports/workspace-model-effort-mobile.png")
        assert dialog.evaluate("el => el.scrollWidth <= el.clientWidth")
    dialog.get_by_role(
        "button", name="Clone and create" if clone else "Create workspace", exact=True
    ).click()
    expect(page.locator("#workspace-progress")).to_contain_text("running")
    body = requests[-1]
    assert body["action"] == ("clone-and-create" if clone else "create")
    if settings == "default":
        assert "model" not in body and "effort" not in body
    else:
        assert body["agent"] == settings
        assert body["model"] == ("fixture-codex" if settings == "codex" else "opus")
        assert body["effort"] == ("ultra" if settings == "codex" else "high")


@pytest.mark.parametrize("kind", ["pr", "issue"])
def test_existing_workspace_has_no_model_controls(page, dashboard_site, request, kind):
    info, _, requests = request.getfixturevalue(
        "workspace_routes" if kind == "pr" else "issue_workspace_routes"
    )
    info["matches"] = [
        {
            "path": "/fixture/checkout",
            "workspace_id": None,
            "name": "Saved session",
            "agent_status": "No workspace",
        }
    ]
    url, _ = dashboard_site
    page.goto(url + ("/#prs" if kind == "pr" else "/#issues"))
    page.locator("#pr-list tr" if kind == "pr" else "#issue-list tr").first.get_by_role(
        "button", name="Reopen workspace", exact=True
    ).click()
    dialog = page.get_by_role("dialog")
    expect(dialog.get_by_label("Model (optional)")).to_have_count(0)
    expect(dialog.get_by_label("Reasoning effort (optional)")).to_have_count(0)
    dialog.get_by_role("button", name="Reopen workspace", exact=True).click()
    expect(page.locator("#workspace-progress")).to_contain_text("running")
    assert requests[-1] == {
        "id": "pr-one" if kind == "pr" else "issue-one",
        "action": "reopen",
        "path": "/fixture/checkout",
    }
