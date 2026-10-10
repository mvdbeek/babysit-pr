"""Polling cost and stability in a real Chromium against the isolated dashboard fixture.

Most tests pause the page clock, so every 5-second poll runs only when a test advances it.
"""

import time

import pytest
from playwright.sync_api import Browser, Page, expect
from test_dashboard_browser import dashboard_site as dashboard_site
from test_dashboard_browser import workspace_routes as workspace_routes

pytestmark = pytest.mark.browser


def pause_clock(page: Page) -> None:
    page.clock.install()
    page.clock.pause_at(time.time() + 1)


def wait_until(page: Page, expression: str) -> None:
    """Poll from Python: the page's own timers are paused with its clock."""
    deadline = time.monotonic() + 10
    while not page.evaluate(expression):
        assert time.monotonic() < deadline, expression
        time.sleep(0.02)


def wait_for(page: Page, condition) -> None:
    """Let Playwright run route handlers until `condition()` holds."""
    deadline = time.monotonic() + 10
    while not condition():
        assert time.monotonic() < deadline, "condition not reached"
        page.wait_for_timeout(20)


def settle(page: Page) -> None:
    wait_until(page, "!busy && !prTable.busy && !issueTable.busy")


def test_hidden_overview_polls_slowly_and_is_not_rebuilt(page: Page, dashboard_site) -> None:
    url, _ = dashboard_site
    snapshot = page.request.get(url + "/api/prs").json()
    requests = []

    def prs(route):
        requests.append(route.request.url)
        route.fulfill(json=snapshot)

    page.route("**/api/prs", prs)
    pause_clock(page)
    page.goto(url + "/#prs")
    rows = page.locator("#pr-list tr")
    expect(rows).to_have_count(2)
    settle(page)
    # A shown overview follows every 5-second poll.
    before = len(requests)
    for _ in range(3):
        page.clock.run_for(5000)
        settle(page)
    assert len(requests) == before + 3
    rows.first.evaluate("row => { row.dataset.marker = 'kept'; }")
    page.get_by_role("tab", name="Watcher", exact=True).click()
    snapshot["prs"][0]["title"] = "Renamed while hidden"
    snapshot["synced_at"] += 1
    # Behind another tab it is fetched every 30 seconds, for the notification bell.
    before = len(requests)
    for _ in range(5):
        page.clock.run_for(5000)
        settle(page)
    assert len(requests) == before
    page.clock.run_for(5000)
    settle(page)
    assert len(requests) == before + 1
    # The new snapshot arrived, but the hidden rows were not rebuilt for it.
    expect(page.locator("#pr-count")).to_have_text("2 / 2")
    expect(page.locator("#pr-list tr[data-marker=kept]")).to_have_count(1)
    expect(rows.first).not_to_contain_text("Renamed while hidden")
    page.get_by_role("tab", name="Pull requests", exact=True).click()
    expect(rows.first).to_contain_text("Renamed while hidden")
    expect(page.locator("#pr-list tr[data-marker=kept]")).to_have_count(0)


def test_a_tab_click_shows_its_page_once(page: Page, dashboard_site) -> None:
    url, _ = dashboard_site
    page.goto(url)
    expect(page.locator("#watcher-panel")).to_be_visible()
    page.evaluate(
        """() => {
          const show = showPage;
          window.shown = [];
          window.showPage = (name) => { shown.push(name); show(name); };
        }"""
    )
    page.get_by_role("tab", name="Pull requests", exact=True).click()
    expect(page).to_have_url(url + "/#prs")
    page.wait_for_timeout(300)  # Room for the hashchange and popstate a hash would fire.
    assert page.evaluate("shown") == ["prs"]
    page.go_back()
    expect(page.locator("#watcher-panel")).to_be_visible()
    page.wait_for_timeout(300)
    assert page.evaluate("shown") == ["prs", "watcher"]


def test_a_stalled_status_poll_gives_up_and_polling_resumes(page: Page, dashboard_site) -> None:
    url, _ = dashboard_site
    state = {"stall": False, "summary": None}
    asked = []

    def status(route):
        asked.append(route)
        if state["stall"]:
            return  # Never answered, like a request lost while a phone slept.
        data = route.fetch().json()
        if state["summary"]:
            for job in data["jobs"]:
                job["summary"] = state["summary"]
        route.fulfill(json=data)

    page.route("**/api/status", status)
    pause_clock(page)
    page.goto(url)
    expect(page.locator("#list .watch")).to_have_count(2)
    settle(page)
    stalled = len(asked)
    state["stall"] = True
    page.clock.run_for(5000)
    wait_for(page, lambda: len(asked) == stalled + 1)
    state.update(stall=False, summary="Seen after the stall")
    # Polls skip while the stalled request holds the busy flag, until it gives up.
    page.clock.run_for(10000)
    page.wait_for_timeout(200)
    assert len(asked) == stalled + 1
    expect(page.locator("#alert")).not_to_contain_text("No response")
    page.clock.run_for(5000)
    expect(page.locator("#alert")).to_contain_text(
        "Dashboard disconnected: No response in 15 seconds"
    )
    page.clock.run_for(5000)
    expect(page.locator("#list .watch").first).to_contain_text("Seen after the stall")
    expect(page.locator("#alert")).not_to_contain_text("No response")
    assert len(asked) == stalled + 2


def test_repair_log_and_poll_time_update_without_rebuilding(page: Page, dashboard_site) -> None:
    url, _ = dashboard_site
    polls = {"count": 0}

    def status(route):
        data = route.fetch().json()
        # The watcher moves these every cycle; nothing else about the watch changes.
        polls["count"] += 1
        for job in data["jobs"]:
            job.update(last_poll=1_700_000_000 + polls["count"] * 60, next_poll=polls["count"])
            job["updated_at"] = (job["updated_at"] or 0) + polls["count"]
        route.fulfill(json=data)

    logs = []

    def log(route):
        logs.append(route.request.url)
        route.fulfill(json={"text": "Repair output\nline two", "truncated": False})

    page.route("**/api/status", status)
    page.route("**/api/log?*", log)
    pause_clock(page)
    page.goto(url)
    page.get_by_role("button", name="test/repo, feature,", exact=False).click()
    page.get_by_role("tab", name="Latest repair log", exact=True).click()
    output = page.locator("#repair-log")
    expect(output).to_have_text("Repair output\nline two")
    observed = page.locator("#detail .fact").filter(has_text="Last CI observation")
    first = observed.locator("span").text_content()
    output.evaluate("node => { node.marker = 'kept'; node.firstChild.marker = 'kept'; }")
    settle(page)
    loads = len(logs)
    page.clock.run_for(5000)
    settle(page)
    wait_for(page, lambda: len(logs) > loads)
    page.wait_for_timeout(200)  # Room for the answer to be applied.
    # The same pane and text node remain, so a selection in the log survives the poll.
    expect(observed.locator("span")).not_to_have_text(first)
    assert output.evaluate("node => node.marker === 'kept' && node.firstChild.marker === 'kept'")


def scheduled_task(key: str, start: float) -> dict:
    return {
        "id": key,
        "status": "scheduled",
        "start_at": start,
        "updated_at": start - 7200,
        "message": "Scheduled",
        "request": {"action": "handle", "agent": "codex", "task": f"Task text for {key}"},
        "subject": {
            "kind": "pr",
            "repo": "test/alpha",
            "number": 8,
            "title": "Test PR",
            "url": "https://github.com/test/alpha/pull/8",
        },
    }


def test_opened_scheduled_task_and_focus_survive_polls(page: Page, dashboard_site) -> None:
    url, _ = dashboard_site
    now = time.time()
    tasks = [scheduled_task("later", now + 7200)]
    page.route(
        "**/api/scheduled-tasks",
        lambda route: route.fulfill(json={"enabled": True, "time": now, "tasks": tasks}),
    )
    pause_clock(page)
    page.goto(url + "/#scheduled")
    card = page.locator("#scheduled-pending li").first
    expect(card).to_contain_text("in 2 h")
    card.get_by_text("Task", exact=True).click()
    expect(card.locator("details")).to_have_attribute("open", "")
    card.get_by_role("button", name="Cancel").focus()
    card.evaluate("node => { node.dataset.marker = 'kept'; }")
    # Unchanged polls leave the card alone; only its countdown moves.
    page.clock.run_for(120000)
    wait_until(page, "!scheduledBusy")
    expect(card.locator("[data-until]")).to_have_text("in 1 h 58 min")
    expect(page.locator("#scheduled-pending li[data-marker=kept]")).to_have_count(1)
    expect(card.locator("details")).to_have_attribute("open", "")
    expect(card.get_by_role("button", name="Cancel")).to_be_focused()
    # A changed list is rebuilt, keeping the opened task and focus on the same control.
    tasks.append(scheduled_task("next", now + 3 * 3600))
    page.clock.run_for(15000)
    expect(page.locator("#scheduled-pending li")).to_have_count(2)
    expect(page.locator("#scheduled-pending li[data-marker=kept]")).to_have_count(0)
    expect(card.locator("details")).to_have_attribute("open", "")
    expect(page.locator("#scheduled-pending li").last.locator("details")).not_to_have_attribute(
        "open", ""
    )
    expect(card.get_by_role("button", name="Cancel")).to_be_focused()


def test_typed_new_task_survives_a_backdrop_tap(page: Page, dashboard_site) -> None:
    url, _ = dashboard_site
    snapshot = {"prs": {}, "issues": {}, "new": {}, "error": None, "synced_at": 1234}
    page.route("**/api/workspaces", lambda route: route.fulfill(json=snapshot))
    clone = {"repo": "up/alpha", "clone": "/fixture/alpha", "remote": "origin", "active": 1}
    page.route(
        "**/api/workspace-repos*",
        lambda route: route.fulfill(json={"repos": [clone], "idle": 0}),
    )
    page.goto(url)
    dialog = page.locator("#workspace-dialog")
    task = dialog.get_by_label("Task", exact=True)
    page.get_by_role("button", name="New task").click()
    expect(task).to_be_visible()
    # Nothing typed: a tap on the backdrop closes it, as before.
    page.mouse.click(2, 2)
    expect(dialog).to_be_hidden()
    page.get_by_role("button", name="New task").click()
    task.fill("Keep this text")
    page.mouse.click(2, 2)
    expect(dialog).to_be_visible()
    expect(task).to_have_value("Keep this text")
    task.fill("")
    page.mouse.click(2, 2)
    expect(dialog).to_be_hidden()
    page.get_by_role("button", name="New task").click()
    task.fill("Typed again")
    page.keyboard.press("Escape")  # The keyboard and Close button still close it.
    expect(dialog).to_be_hidden()


def test_submit_stays_disabled_while_its_request_is_pending(
    page: Page, dashboard_site, workspace_routes
) -> None:
    url, _ = dashboard_site
    info, _, _ = workspace_routes
    info["matches"] = []
    info["operation"] = {"id": "op0", "status": "failed", "message": "Clone failed"}
    held = []
    page.route("**/api/workspace-action", lambda route: held.append(route))
    page.goto(url + "/#prs")
    page.locator("#pr-list tr").first.get_by_role(
        "button", name="Create workspace", exact=True
    ).click()
    dialog = page.locator("#workspace-dialog")
    submit = dialog.get_by_role("button", name="Create workspace", exact=True)
    expect(submit).to_be_enabled()  # A failed launch may be retried.
    dialog.get_by_label("Task", exact=True).fill("Try again")
    submit.click()
    expect(submit).to_be_disabled()
    assert len(held) == 1
    # A background refresh still sees the failed launch, but must not offer a second one.
    page.evaluate("refreshWorkspaces()")
    expect(submit).to_be_disabled()
    op = {"id": "op1", "status": "running", "message": "Fetching PR", "log": ""}
    info["operation"] = op
    held[0].fulfill(json={"operation": op})
    expect(page.locator("#workspace-progress")).to_contain_text("running: Fetching PR")
    expect(submit).to_be_disabled()


def test_a_slow_workspace_snapshot_is_awaited_not_stacked(
    page: Page, dashboard_site, workspace_routes
) -> None:
    url, _ = dashboard_site
    _, snapshot, _ = workspace_routes
    held = []
    page.route("**/api/workspaces", lambda route: held.append(route))
    pause_clock(page)
    page.goto(url + "/#prs")
    diff = page.locator("#pr-list tr").first.get_by_role("button", name="Diff", exact=True)
    expect(page.locator("#pr-list tr")).to_have_count(2)
    wait_for(page, lambda: len(held) == 1)
    # The first snapshot after a restart runs git for every PR and can take most of a
    # minute. Before: it was abandoned after 15 seconds and the next poll asked again,
    # stacking another computation on the server.
    for _ in range(9):
        page.clock.run_for(5000)
        settle(page)
    page.wait_for_timeout(200)
    assert len(held) == 1
    expect(diff).to_have_count(0)
    held[0].fulfill(json=snapshot)
    expect(diff).to_be_visible()


def test_revealed_item_widens_the_window_only_to_its_page(page: Page, dashboard_site) -> None:
    from urllib.parse import quote

    url, _ = dashboard_site
    snapshot = page.request.get(url + "/api/prs").json()
    template = snapshot["prs"][0]
    snapshot["prs"] = [
        {
            **template,
            "id": f"pr-{i}",
            "number": i,
            "title": f"PR {i}",
            "url": f"https://github.com/test/alpha/pull/{i}",
            "updated_at": f"2026-01-01T00:{i // 60:02d}:{i % 60:02d}Z",
        }
        for i in range(120)
    ]
    page.route("**/api/prs", lambda route: route.fulfill(json=snapshot))
    page.emulate_media(reduced_motion="reduce")
    # Newest first, so PR 64 is the 56th row: on the second page of 50.
    target = "https://github.com/test/alpha/pull/64"
    page.goto(f"{url}/?item={quote(target, safe='')}#prs")
    row = page.locator("#pr-list tr.notification-target")
    expect(row).to_contain_text("PR 64")
    expect(row).to_be_focused()
    rows = page.locator("#pr-list tr")
    expect(rows).to_have_count(100)
    expect(page.locator("#pr-more-button")).to_have_text("Show 20 more · 100 of 120 shown")
    page.evaluate("prTable.refresh()")
    expect(rows).to_have_count(100)


def test_short_pickers_skip_the_keyboard_on_touch_screens(
    browser: Browser, page: Page, dashboard_site
) -> None:
    url, _ = dashboard_site
    snapshot = page.request.get(url + "/api/prs").json()
    template = snapshot["prs"][0]
    # More repositories than a short list holds, so that filter keeps typing to narrow.
    snapshot["prs"] = [
        {**template, "id": f"pr-{i}", "number": i, "repo": f"test/repo-{i}"} for i in range(15)
    ]
    page.goto(url + "/#prs")
    role = page.get_by_role("combobox", name="Filter pull requests by role", exact=True)
    expect(role).to_be_visible()
    assert role.get_attribute("inputmode") is None  # A mouse keeps typing everywhere.
    context = browser.new_context(
        has_touch=True, is_mobile=True, viewport={"width": 390, "height": 844}
    )
    try:
        phone = context.new_page()
        phone.route("**/api/prs", lambda route: route.fulfill(json=snapshot))
        phone.goto(url + "/#prs")
        assert phone.evaluate("matchMedia('(pointer: coarse)').matches")
        expect(phone.locator("#pr-list tr")).to_have_count(15)
        expect(phone.get_by_role("combobox", name="Sort by", exact=True)).to_have_attribute(
            "inputmode", "none"
        )
        phone.locator("#pr-filters-toggle").click()
        expect(
            phone.get_by_role("combobox", name="Filter pull requests by role", exact=True)
        ).to_have_attribute("inputmode", "none")
        repo = phone.get_by_role("combobox", name="Filter pull requests by repository")
        expect(repo).to_be_visible()
        assert repo.get_attribute("inputmode") is None
    finally:
        context.close()
