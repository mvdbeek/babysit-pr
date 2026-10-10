"""Dashboard follow-ups against isolated fixtures, with no live GitHub.

Ended watches load CI details on demand, link-prefilled tasks say so, only shown
overview rows count as seen, and the extension forgets drafts it never opened.
"""

import json
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

import pytest
from playwright.sync_api import Page, expect
from test_dashboard_browser import dashboard_site as dashboard_site  # noqa: F401 - fixture
from test_notifications_browser import count, refresh
from test_notifications_browser import inbox as inbox  # noqa: F401 - fixture
from test_polling_browser import pause_clock, settle, wait_for

pytestmark = pytest.mark.browser

ROOT = Path(__file__).resolve().parents[1]
CHECKS = [
    {
        "name": "unit tests",
        "link": "https://github.com/test/merged/actions/runs/1",
        "bucket": "fail",
        "workflow": "CI",
    }
]
FAILED_JOBS = [
    {
        "job_name": "pytest (3.12)",
        "html_url": "https://github.com/test/merged/actions/runs/1/job/2",
        "workflow_name": "CI",
    }
]
LOAD_ERROR = "Could not load CI checks: Unknown watch. Select the watch again to retry."


@pytest.fixture
def watches(page: Page, dashboard_site):
    """Serve /api/status with the closed watch's details omitted, and /api/watch for it."""
    url, _ = dashboard_site
    status = page.request.get(url + "/api/status").json()
    jobs = {job["id"]: job for job in status["jobs"]}
    full = dict(jobs["closed"], check_details=CHECKS, failed_jobs=FAILED_JOBS)
    jobs["closed"].update(
        check_details=[], failed_jobs=[], details_omitted=True, checks_result="FAILURE"
    )
    jobs["feedback"].update(
        check_details=[{"name": "lint", "link": "https://github.com/test/repo/runs/3"}],
        failed_jobs=[],
        details_omitted=False,
        checks_result="PENDING",
    )
    state = {"hold": False, "fail": False}
    asked, held = [], []

    def watch(route):
        asked.append(parse_qs(urlsplit(route.request.url).query))
        if state["hold"]:
            held.append(route)
        elif state["fail"]:
            route.fulfill(status=404, json={"error": "Unknown watch"})
        else:
            route.fulfill(json={"job": full})

    page.route("**/api/status", lambda route: route.fulfill(json=status))
    page.route("**/api/watch?*", watch)
    pause_clock(page)
    page.goto(url)
    expect(page.locator("#list .watch")).to_have_count(2)
    page.locator("#show-ended").click()
    return {"jobs": jobs, "full": full, "state": state, "asked": asked, "held": held}


def select(page: Page, repo: str) -> None:
    page.get_by_role("button", name=f"{repo}, feature,", exact=False).click()


def show_all(page: Page) -> None:
    page.locator("#filter").evaluate(
        "select => { select.value = 'all'; select.dispatchEvent(new Event('change')); }"
    )


def polls(page: Page, times: int = 3) -> None:
    for _ in range(times):
        page.clock.run_for(5000)
        settle(page)


def test_ended_watch_loads_ci_details_once_when_shown(page: Page, watches) -> None:
    watches["state"]["hold"] = True
    select(page, "test/merged")
    panel = page.locator("#detail [role=tabpanel]")
    expect(panel).to_have_text("Loading checks…")
    wait_for(page, lambda: len(watches["held"]) == 1)
    assert watches["asked"] == [{"id": ["closed"]}]
    watches["held"].pop().fulfill(json={"job": watches["full"]})
    expect(panel.get_by_role("link", name="unit tests", exact=True)).to_be_visible()
    expect(panel.locator(".section-label")).to_have_text("Failed jobs")
    expect(panel.get_by_role("link", name="pytest (3.12)", exact=True)).to_be_visible()
    # Quiet polls neither refetch nor rebuild the loaded details.
    panel.evaluate("node => { node.dataset.marker = 'kept'; }")
    polls(page)
    assert len(watches["asked"]) == 1
    expect(page.locator("#detail [role=tabpanel][data-marker=kept]")).to_have_count(1)
    # The logs tab needs no details; returning to the checks tab uses the cached ones.
    page.get_by_role("tab", name="Latest repair log", exact=True).click()
    page.get_by_role("tab", name="CI checks", exact=True).click()
    expect(panel.get_by_role("link", name="unit tests", exact=True)).to_be_visible()
    assert len(watches["asked"]) == 1
    # A changed watch is fetched again, once.
    watches["state"]["hold"] = False
    watches["jobs"]["closed"]["updated_at"] += 1
    polls(page)
    assert len(watches["asked"]) == 2
    expect(panel.get_by_role("link", name="unit tests", exact=True)).to_be_visible()


def test_late_details_do_not_replace_another_selected_watch(page: Page, watches) -> None:
    watches["state"]["hold"] = True
    select(page, "test/merged")
    wait_for(page, lambda: len(watches["held"]) == 1)
    show_all(page)
    select(page, "test/repo")
    panel = page.locator("#detail [role=tabpanel]")
    expect(panel.get_by_role("link", name="lint", exact=True)).to_be_visible()
    watches["held"].pop().fulfill(json={"job": watches["full"]})
    page.wait_for_timeout(200)  # Room for the late answer to be applied.
    expect(page.locator("#detail h2")).not_to_contain_text("test/merged")
    expect(panel.get_by_role("link", name="lint", exact=True)).to_be_visible()
    expect(panel.get_by_role("link", name="unit tests", exact=True)).to_have_count(0)
    # Selecting the ended watch again shows the cached answer without another request.
    select(page, "test/merged")
    expect(panel.get_by_role("link", name="unit tests", exact=True)).to_be_visible()
    assert len(watches["asked"]) == 1


def test_failed_details_show_an_error_and_retry_on_next_selection(page: Page, watches) -> None:
    watches["state"]["fail"] = True
    select(page, "test/merged")
    error = page.locator("#detail [role=alert]")
    expect(error).to_have_text(LOAD_ERROR)
    polls(page)
    assert len(watches["asked"]) == 1
    expect(error).to_have_text(LOAD_ERROR)
    watches["state"]["fail"] = False
    select(page, "test/merged")
    panel = page.locator("#detail [role=tabpanel]")
    expect(panel.get_by_role("link", name="unit tests", exact=True)).to_be_visible()
    expect(page.locator("#detail [role=alert]")).to_have_count(0)
    assert len(watches["asked"]) == 2


def test_active_watch_renders_details_inline_without_fetching(page: Page, watches) -> None:
    show_all(page)
    select(page, "test/repo")
    panel = page.locator("#detail [role=tabpanel]")
    expect(panel.get_by_role("link", name="lint", exact=True)).to_be_visible()
    expect(panel).not_to_contain_text("Loading checks")
    polls(page)
    assert watches["asked"] == []


@pytest.mark.parametrize("server", ["current", "older"])
def test_watcher_notifications_use_the_server_check_result(page: Page, inbox, server) -> None:
    url, packets = inbox
    job = packets["status"]["jobs"][0]
    if server == "current":
        # Ended watches omit their details; only the summary changes.
        job.update(check_details=[], failed_jobs=[], details_omitted=True)
        job["checks_result"] = "PENDING"
    else:
        job.pop("checks_result", None)
        job["check_details"] = [{"name": "a", "bucket": "pending"}]
    page.goto(url)
    count(page, 0)
    if server == "current":
        job["checks_result"] = "SUCCESS"
    else:
        job["check_details"] = [{"name": "a", "bucket": "pass"}]
    job["updated_at"] += 1
    refresh(page)
    count(page, 1)
    page.locator("#notifications-toggle").click()
    expect(page.locator("#notifications-list")).to_contain_text("CI updated")


def test_link_prefilled_task_says_it_came_from_a_link(page: Page, dashboard_site) -> None:
    url, _ = dashboard_site
    snapshot = {"prs": {}, "issues": {}, "new": {}, "error": None, "synced_at": 1234}
    page.route("**/api/workspaces", lambda route: route.fulfill(json=snapshot))
    clone = {"repo": "up/alpha", "clone": "/fixture/alpha", "remote": "origin", "active": 1}
    page.route(
        "**/api/workspace-repos*",
        lambda route: route.fulfill(json={"repos": [clone], "idle": 0}),
    )
    task = {"task": "Investigate the parser", "repo": "up/alpha", "name": "parser-fix"}
    page.goto(f"{url}/#task={quote(json.dumps(task), safe='')}")
    dialog = page.locator("#workspace-dialog")
    notice = dialog.locator("#workspace-content > p.alert")
    expect(notice).to_have_text(
        "Prefilled from a link. Check the repository and task before starting."
    )
    expect(dialog.get_by_label("Task", exact=True)).to_have_value("Investigate the parser")
    expect(dialog.get_by_label("New branch")).to_have_value("parser-fix")
    assert notice.evaluate("node => node.nextElementSibling.tagName") == "FORM"
    assert page.url == url + "/#watcher"
    page.keyboard.press("Escape")
    expect(dialog).to_be_hidden()
    # The dashboard's own button opens an empty dialog without the notice.
    page.get_by_role("button", name="New task").click()
    expect(dialog.get_by_label("Task", exact=True)).to_have_value("")
    expect(dialog.locator(".alert")).to_have_count(0)


@pytest.fixture
def overview(page: Page, dashboard_site):
    """A PR snapshot the test edits, served to every page in the browser context."""
    url, _ = dashboard_site
    snapshot = page.request.get(url + "/api/prs").json()
    page.context.route("**/api/prs", lambda route: route.fulfill(json=snapshot))
    return url, snapshot


def saved_visit(page: Page) -> dict:
    return page.evaluate("JSON.parse(localStorage.getItem('babysit-pr:seen-prs:v1:fixture'))")


def unseen(page: Page) -> list[str]:
    return sorted(page.evaluate("dashboardNotifications.unseen()"))


def test_rows_hidden_by_search_stay_unseen_and_highlighted_next_visit(page: Page, overview) -> None:
    url, snapshot = overview
    page.goto(url + "/#prs")
    rows = page.locator("#pr-list tr")
    expect(rows).to_have_count(2)
    expect(page.locator("#pr-changes")).to_contain_text("from this visit onward")
    page.get_by_label("Search pull requests").fill("test/alpha")
    expect(rows).to_have_count(1)
    hidden = snapshot["prs"][1]
    old_title = hidden["title"]
    hidden["title"] = "Renamed while filtered out"
    snapshot["synced_at"] += 1
    page.locator("#pr-refresh").click()
    expect(page.locator("#notifications-count")).to_have_text("1")
    assert unseen(page) == [hidden["url"]]
    assert saved_visit(page)["prs"][hidden["id"]]["title"] == old_title
    # The next visit still highlights it; showing it then marks it seen and saved.
    page.goto("about:blank")
    page.goto(url + "/#prs")
    expect(rows).to_have_count(2)
    changed = page.locator("#pr-list .pr-changed")
    expect(changed).to_have_count(1)
    expect(changed).to_contain_text("Renamed while filtered out")
    expect(page.locator("#notifications-count")).to_be_hidden()
    assert saved_visit(page)["prs"][hidden["id"]]["title"] == "Renamed while filtered out"


def test_rows_outside_the_window_are_seen_once_pinned_paged_or_sorted(page: Page, overview) -> None:
    url, snapshot = overview
    template = snapshot["prs"][0]
    snapshot["prs"] = [
        {
            **template,
            "id": f"pr-{index}",
            "number": 100 + index,
            "title": f"PR {index}",
            "url": f"https://github.com/test/alpha/pull/{100 + index}",
            # Newest first by default: PR 0 is the first row.
            "updated_at": f"2026-09-{28 - index // 24:02d}T{23 - index % 24:02d}:00:00Z",
        }
        for index in range(120)
    ]
    page.goto(url + "/#prs")
    rows = page.locator("#pr-list tr")
    expect(rows).to_have_count(50)
    for index in (10, 60, 75, 110):
        snapshot["prs"][index]["title"] = f"Changed {index}"
    snapshot["synced_at"] += 1
    page.locator("#pr-refresh").click()
    expect(rows.nth(10)).to_contain_text("Changed 10")
    expect(page.locator("#notifications-count")).to_have_text("3")

    def pending() -> list[str]:
        return [url.rsplit("/", 1)[1] for url in unseen(page)]

    assert pending() == ["160", "175", "210"]
    saved = saved_visit(page)["prs"]
    assert [saved[f"pr-{index}"]["title"] for index in (10, 60, 75, 110)] == [
        "Changed 10",
        "PR 60",
        "PR 75",
        "PR 110",
    ]
    # A pin brings PR 60 into the rendered rows.
    page.evaluate("localStorage.setItem('babysit-pr:pins-prs:v1:fixture', '[\"pr-60\"]')")
    page.locator("#pr-refresh").click()
    expect(rows.first).to_contain_text("Changed 60")
    expect(page.locator("#notifications-count")).to_have_text("2")
    assert pending() == ["175", "210"]
    # The next page renders PR 75.
    page.locator("#pr-more-button").dispatch_event("click")
    expect(rows).to_have_count(100)
    expect(page.locator("#notifications-count")).to_have_text("1")
    assert pending() == ["210"]
    # Oldest first renders PR 110 in the first page.
    page.get_by_role("button", name="Sort by Last updated", exact=True).click()
    expect(rows.nth(1)).to_contain_text("PR 119")
    expect(page.locator("#notifications-count")).to_be_hidden()
    saved = saved_visit(page)["prs"]
    assert [saved[f"pr-{index}"]["title"] for index in (10, 60, 75, 110)] == [
        "Changed 10",
        "Changed 60",
        "Changed 75",
        "Changed 110",
    ]


BACKGROUND = """async ([source, drafts, tabs, times]) => {
  const listeners = {};
  const event = (name) => ({ addListener: (callback) => { listeners[name] = callback; } });
  const store = structuredClone(drafts);
  const chrome = {
    runtime: { onInstalled: event("install"), getURL: (path) => `chrome-extension://x/${path}` },
    storage: {
      session: {
        get: async () => structuredClone(store),
        set: async (values) => Object.assign(store, structuredClone(values)),
        remove: async (keys) => keys.forEach((key) => delete store[key]),
      },
    },
    contextMenus: { onClicked: event("menu") },
    action: { onClicked: event("action") },
    tabs: { query: async () => tabs, create: async () => ({ id: 9 }), onRemoved: event("removed") },
  };
  let now = times[0];
  new Function("chrome", "Date", "crypto", source)(
    chrome,
    { now: () => now },
    { randomUUID: () => "opened" },
  );
  // The toolbar button on a page that cannot be captured opens an empty composer.
  await listeners.action({ url: "chrome://settings" });
  const steps = [];
  for (now of times) {
    await listeners.removed(1);
    steps.push(structuredClone(store));
  }
  return steps;
}"""


def test_extension_forgets_drafts_whose_composer_never_loaded(page: Page) -> None:
    minute = 60_000
    start = 1_000_000_000
    drafts = {
        "loading": {"source": {"selection": "a"}, "createdAt": start - minute},
        "abandoned": {"source": {"selection": "b"}, "createdAt": start - 11 * minute},
        "closed": {"source": {"selection": "c"}, "tabId": 5},
        "open": {"source": {"selection": "d"}, "tabId": 6},
        "open-undated": {"source": {"selection": "e"}},
        "undated": {"source": {"selection": "f"}},
    }
    tabs = [{"url": f"chrome-extension://x/compose.html#{key}"} for key in ("open", "open-undated")]
    first, later = page.evaluate(
        BACKGROUND,
        [
            (ROOT / "extension/background.js").read_text(),
            drafts,
            tabs,
            [start, start + 10 * minute],
        ],
    )
    # The new composer's draft is dated; it has no tab until the composer loads.
    assert first["opened"] == {"source": {}, "createdAt": start}
    # Open composers are kept; a closed composer's draft goes; undated ones get a date.
    assert sorted(first) == ["loading", "open", "open-undated", "opened", "undated"]
    assert first["undated"] == {"source": {"selection": "f"}, "createdAt": start}
    assert first["open-undated"] == drafts["open-undated"]
    # Ten minutes later, only drafts with an open composer remain.
    assert sorted(later) == ["open", "open-undated"]
