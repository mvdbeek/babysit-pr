"""Workspace experiment browser coverage against temporary servers and a stubbed cleanup."""

import copy
import threading
import time

import dashboard
import pytest
import workspace_overview as wso
from playwright.sync_api import expect
from test_dashboard_browser import choose_option

pytestmark = pytest.mark.browser

BASE = "base/repo"


def link(kind, number, state, title="Some work"):
    return {
        "kind": kind,
        "repo": BASE,
        "number": number,
        "title": title,
        "url": f"https://github.com/{BASE}/{'issues' if kind == 'issue' else 'pull'}/{number}",
        "state": state,
        "draft": False,
    }


def row(name, **changes):
    value = {
        "key": f"/src/worktrees/repo/{name}",
        "path": f"/src/worktrees/repo/{name}",
        "name": name,
        "label": name,
        "repo": BASE,
        "repo_root": "/src/repo",
        "branch": name,
        "sha": "abc123def456",
        "main": False,
        "missing": False,
        "upstream": "fork/repo:" + name,
        "workspace_ids": [],
        "workspace_url": None,
        "agent_status": None,
        "agents": [],
        "changes": 0,
        "unpushed": 0,
        "links": [],
        "stale_links": False,
        "blockers": [],
        "remove_worktree": True,
        "removable": True,
        "status": "unlinked",
    }
    value.update(changes)
    return value


WORKSPACES = [
    row(
        "merged-work",
        links=[link("pr", 7, "merged", "Merged work")],
        status="ready",
        workspace_ids=["w1"],
        workspace_url="https://collie.example.ts.net/space/w1",
        agents=[{"agent": "codex", "status": "idle", "pane": "w1:p1"}],
    ),
    row("open-work", links=[link("pr", 8, "open", "Open work")], status="active"),
    row(
        "dirty-work",
        links=[link("issue", 12, "closed", "Crash on start")],
        status="blocked",
        changes=3,
        unpushed=2,
        blockers=[
            {"kind": "dirty", "text": "3 uncommitted change(s)"},
            {"kind": "unpushed", "text": "2 commit(s) only on this branch"},
        ],
    ),
    row(
        "other-repo-work",
        repo="other/repo",
        links=[],
        stale_links=True,
        status="unlinked",
    ),
    row(
        "repo",
        key="/src/repo",
        path="/src/repo",
        branch="main",
        main=True,
        status="protected",
        blockers=[
            {"kind": "main", "text": "Main checkout — cleanup only closes its herdr workspace"}
        ],
        remove_worktree=False,
        removable=False,
        workspace_ids=["w9"],
    ),
]


@pytest.mark.parametrize(
    "name,label", [("merged-work", "Open workspace"), ("open-work", "Create workspace")]
)
def test_workspace_open_button_and_popup_fallback(page, site, name, label):
    url, _, _ = site
    requests = []

    def action(route):
        requests.append(route.request.post_data_json)
        route.fulfill(json={"url": "https://collie.example.ts.net/space/w1"})

    page.route("**/api/workspace-open", action)
    page.goto(url + "/#workspaces")
    page.evaluate("window.open = () => null")
    row = page.locator("#ws-list tr").filter(has=page.get_by_text(name, exact=True))
    row.get_by_role("button", name=label, exact=True).click()
    expect(row.get_by_role("link", name="Open in Collie")).to_have_attribute(
        "href", "https://collie.example.ts.net/space/w1"
    )
    expect(row.get_by_role("button", name="Open workspace", exact=True)).to_be_enabled()
    assert requests == [{"key": f"/src/worktrees/repo/{name}"}]


def test_workspace_open_error_can_retry(page, site):
    url, _, _ = site
    page.route(
        "**/api/workspace-open",
        lambda route: route.fulfill(status=400, json={"error": "Workspace changed"}),
    )
    page.goto(url + "/#workspaces")
    page.evaluate("window.open = () => ({close() {window.closedPopup = true;}})")
    row = page.locator("#ws-list tr").filter(has=page.get_by_text("open-work", exact=True))
    row.get_by_role("button", name="Create workspace", exact=True).click()
    expect(row.get_by_role("alert")).to_have_text("Workspace changed")
    expect(row.get_by_role("button", name="Create workspace", exact=True)).to_be_enabled()
    assert page.evaluate("window.closedPopup")


@pytest.fixture
def site(tmp_path, monkeypatch):
    directory = tmp_path / "experiments" / "workspaces"
    directory.mkdir(parents=True)
    (directory / "config.json").write_text('{"enabled": true}')
    plugin = wso.WorkspaceOverview(tmp_path, lambda endpoint: pytest.fail("no GitHub call"))
    plugin.next_poll = float("inf")
    plugin.value = {"workspaces": WORKSPACES, "warnings": [], "synced_at": time.time()}
    # Cleanup schedules a rescan; keep that background scan inside the fixture too.
    monkeypatch.setattr(type(plugin), "collect", lambda self, **kwargs: copy.deepcopy(self.value))
    removed: list = []

    def remove(self, job, index, target):
        removed.append(target)
        self.record(job, index, status="done", message="Cleaned up", steps=["Closed workspace w1"])

    plugin.remove = remove.__get__(plugin, type(plugin))
    with dashboard.DashboardServer(tmp_path, 0, workspace_overview=plugin) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        yield f"http://127.0.0.1:{server.server_port}", plugin, removed
        server.shutdown()
        thread.join(3)


@pytest.mark.parametrize("width", [1440, 390])
def test_workspace_sorting_dates_filters_selection_and_refresh(page, site, width):
    url, plugin, _ = site
    plugin.value["workspaces"] = [
        row("older", updated_at=100, created_at=300),
        row("newer", updated_at=300, created_at=100),
        row("middle", updated_at=200, created_at=200),
        row("unknown", updated_at=None, created_at=None),
        row("legacy"),
    ]
    page.set_viewport_size({"width": width, "height": 1000})
    page.clock.install()
    page.goto(url + "/#workspaces")
    names = page.locator("#ws-list tr td:nth-child(2) > span")
    sort = page.get_by_role("combobox", name="Sort workspaces by")
    expect(sort).to_have_value("Last updated")
    expect(names).to_have_text(["newer", "middle", "older", "legacy", "unknown"])
    expect(page.locator("#ws-list")).to_contain_text("Updated")
    expect(page.locator("#ws-list")).to_contain_text("Created unknown")
    page.get_by_role("checkbox", name="Select newer").check()
    choose_option(sort, "created_at")
    expect(names).to_have_text(["older", "middle", "newer", "legacy", "unknown"])
    page.get_by_role("button", name="Newest first", exact=True).click()
    expect(names).to_have_text(["newer", "middle", "older", "legacy", "unknown"])
    page.get_by_role("searchbox", name="Search workspaces").fill("newer")
    expect(names).to_have_text(["newer"])
    page.get_by_role("searchbox", name="Search workspaces").fill("")
    plugin.value["workspaces"][0]["created_at"] = 50
    page.clock.fast_forward(5000)
    expect(names).to_have_text(["older", "newer", "middle", "legacy", "unknown"])
    expect(page.get_by_role("checkbox", name="Select newer")).to_be_checked()
    expect(sort).to_have_value("Created")
    expect(page.get_by_role("button", name="Oldest first", exact=True)).to_be_visible()
    choose_option(sort, "updated_at")
    expect(names).to_have_text(["newer", "middle", "older", "legacy", "unknown"])
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


@pytest.mark.parametrize("width", [1440, 390])
def test_listing_filters_links_keyboard_and_responsive_screenshots(page, site, width):
    url, plugin, _ = site
    page.set_viewport_size({"width": width, "height": 1000})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(url + "/#workspaces")
    expect(page.get_by_role("tab", name="Workspaces")).to_be_visible()
    expect(page.locator("#ws-list tr")).to_have_count(5)
    expect(page.locator("#ws-status-line")).to_contain_text("5 workspaces, 1 ready to clean up")
    expect(page.locator("#ws-list")).to_contain_text("Merged")
    expect(page.locator("#ws-list")).to_contain_text("3 uncommitted · 2 only on this branch")
    expect(page.locator("#ws-list")).to_contain_text("Checking GitHub…")
    expect(page.get_by_role("link", name="PR #7")).to_have_attribute(
        "href", f"https://github.com/{BASE}/pull/7"
    )
    expect(page.get_by_role("link", name="merged-work")).to_have_attribute(
        "href", "https://collie.example.ts.net/space/w1"
    )
    page.screenshot(path=f"reports/workspaces-{width}.png", full_page=True)
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    # The dashboard's global input width must not collapse the toolbar at any width.
    search = page.get_by_role("searchbox", name="Search workspaces").bounding_box()
    assert search["width"] >= 200, search
    choose_option(page.get_by_role("combobox", name="Filter workspaces by cleanup status"), "ready")
    expect(page.locator("#ws-list tr")).to_have_count(1)
    expect(page.locator("#ws-count")).to_have_text("1")
    choose_option(page.get_by_role("combobox", name="Filter workspaces by cleanup status"), "all")
    choose_option(
        page.get_by_role("combobox", name="Filter workspaces by repository"), "other/repo"
    )
    expect(page.locator("#ws-list tr")).to_have_count(1)
    choose_option(page.get_by_role("combobox", name="Filter workspaces by repository"), "all")
    page.get_by_role("searchbox", name="Search workspaces").fill("#12")
    expect(page.locator("#ws-list tr")).to_have_count(1)
    expect(page.locator("#ws-list")).to_contain_text("dirty-work")
    page.get_by_role("searchbox", name="Search workspaces").fill("nothing-here")
    expect(page.locator("#ws-empty")).to_contain_text("No workspace matches")
    page.get_by_role("searchbox", name="Search workspaces").fill("")
    page.get_by_role("tab", name="Workspaces").focus()
    page.keyboard.press("ArrowRight")
    expect(page.locator("#upstream-panel")).to_be_hidden()  # Hidden tabs are skipped.
    page.keyboard.press("Home")
    expect(page.locator("#watcher-panel")).to_be_visible()
    page.go_back()
    expect(page.locator("#workspaces-panel")).to_be_visible()
    assert not errors
    assert plugin.next_poll == float("inf")


def test_selection_confirmation_override_and_cleanup_progress(page, site):
    url, plugin, removed = site
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(url + "/#workspaces")
    expect(page.get_by_role("button", name="Clean up selected")).to_be_disabled()
    expect(page.get_by_role("checkbox", name="Select repo")).to_be_disabled()
    page.get_by_role("checkbox", name="Select all shown").check()
    expect(page.locator("#ws-selected")).to_have_text("4 selected")
    page.get_by_role("checkbox", name="Select open-work").uncheck()
    page.get_by_role("checkbox", name="Select other-repo-work").uncheck()
    expect(page.locator("#ws-selected")).to_have_text("2 selected")
    page.get_by_role("button", name="Clean up selected").click()
    dialog = page.locator("#ws-dialog")
    expect(dialog).to_be_visible()
    expect(dialog).to_contain_text("Clean up 2 workspace(s)")
    expect(dialog).to_contain_text("3 uncommitted change(s); 2 commit(s) only on this branch")
    assert dialog.locator("input[type=checkbox]").first.bounding_box()["width"] < 30
    page.screenshot(path="reports/workspaces-cleanup-dialog.png")
    page.get_by_role("button", name="Clean up 2 workspace(s)").click()
    expect(dialog).to_contain_text("Cleanup results")
    expect(dialog).to_contain_text("Closed workspace w1")
    assert sorted(target["key"] for target in removed) == [
        "/src/worktrees/repo/dirty-work",
        "/src/worktrees/repo/merged-work",
    ]
    # The blocked row was confirmed without ticking its override, so it stays gated.
    assert all(target["approve"] == [] for target in removed)
    page.get_by_role("button", name="Close").click()
    expect(dialog).to_be_hidden()
    removed.clear()
    page.get_by_role("checkbox", name="Select dirty-work").check()
    page.get_by_role("button", name="Clean up selected").click()
    page.get_by_role("checkbox", name="Remove anyway").check()
    page.get_by_role("button", name="Clean up 1 workspace(s)").click()
    expect(dialog).to_contain_text("Cleanup results")
    assert removed == [{"key": "/src/worktrees/repo/dirty-work", "approve": ["dirty", "unpushed"]}]
    assert not errors


def test_a_failed_cleanup_request_is_reported_in_the_dialog(page, site, monkeypatch):
    url, plugin, _ = site
    monkeypatch.setattr(
        type(plugin), "cleanup", lambda self, request: (_ for _ in ()).throw(ValueError("nope"))
    )
    page.goto(url + "/#workspaces")
    page.get_by_role("checkbox", name="Select merged-work").check()
    page.get_by_role("button", name="Clean up selected").click()
    page.get_by_role("button", name="Clean up 1 workspace(s)").click()
    expect(page.locator("#ws-dialog-error")).to_contain_text("nope")
    expect(page.get_by_role("button", name="Clean up 1 workspace(s)")).to_be_enabled()


def test_the_tab_stays_hidden_while_the_experiment_is_disabled(page, tmp_path):
    plugin = wso.WorkspaceOverview(tmp_path)
    with dashboard.DashboardServer(tmp_path, 0, workspace_overview=plugin) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            page.goto(f"http://127.0.0.1:{server.server_port}/")
            expect(page.get_by_role("tab", name="Pull requests")).to_be_visible()
            expect(page.locator("#workspaces-tab")).to_be_hidden()
            expect(page.locator("#workspaces-panel")).to_be_hidden()
        finally:
            server.shutdown()
            thread.join(3)


def test_rows_hidden_by_a_filter_are_never_submitted(page, site):
    url, plugin, removed = site
    page.goto(url + "/#workspaces")
    page.get_by_role("checkbox", name="Select all shown").check()
    expect(page.locator("#ws-selected")).to_have_text("4 selected")
    page.get_by_role("searchbox", name="Search workspaces").fill("merged-work")
    expect(page.locator("#ws-selected")).to_have_text("1 selected (3 hidden by filters)")
    page.get_by_role("button", name="Clean up selected").click()
    expect(page.locator("#ws-dialog")).to_contain_text("Clean up 1 workspace(s)")
    page.get_by_role("button", name="Clean up 1 workspace(s)").click()
    expect(page.locator("#ws-dialog")).to_contain_text("Cleanup results")
    assert [target["key"] for target in removed] == ["/src/worktrees/repo/merged-work"]


def test_a_batch_refused_because_another_is_running_keeps_the_selection(page, site, monkeypatch):
    url, plugin, _ = site
    monkeypatch.setattr(
        type(plugin),
        "cleanup",
        lambda self, request: {"cleanup": {"status": "running", "results": []}, "accepted": False},
    )
    page.goto(url + "/#workspaces")
    page.get_by_role("checkbox", name="Select merged-work").check()
    page.get_by_role("button", name="Clean up selected").click()
    page.get_by_role("button", name="Clean up 1 workspace(s)").click()
    expect(page.locator("#ws-dialog-error")).to_contain_text("another cleanup is still running")
    page.get_by_role("button", name="Cancel").click()
    expect(page.locator("#ws-selected")).to_have_text("1 selected")


DIFF = {
    "scope": "branch",
    "base": "origin/main",
    "bases": [{"ref": "origin/main", "ahead": 1}, {"ref": "upstream/release_1.0", "ahead": 4}],
    "commits": [{"sha": "abc123def456", "author": "Fixture", "time": 1, "subject": "Fix it"}],
    "files": [
        {
            "path": "app.py",
            "old_path": None,
            "status": "modified",
            "binary": False,
            "added": 1,
            "removed": 1,
            "lines": ["@@ -1 +1 @@", "-old line", "+new line " + "x" * 300],
            "truncated": False,
        },
        {
            "path": "logo.png",
            "old_path": None,
            "status": "added",
            "binary": True,
            "added": 0,
            "removed": 0,
            "lines": [],
            "truncated": False,
        },
    ],
    "untracked": ["notes.txt"],
    "untracked_more": 0,
    "added": 1,
    "removed": 1,
    "truncated": False,
}


def transcript(start=0, end=3):
    entries = [
        {"role": "user", "text": "Fix the crash", "time": None},
        {
            "role": "tool",
            "name": "Bash",
            "input": '{"command": "pytest"}',
            "output": "1 failed",
            "error": True,
            "time": None,
        },
        {"role": "assistant", "text": "Fixed and tested.", "time": None},
    ]
    session = {"id": "s1", "agent": "claude", "updated": 1, "size": 1, "title": "Fix the crash"}
    return {
        "sessions": [session, {**session, "id": "s0", "agent": "codex", "title": "Older"}],
        "session": session,
        "entries": entries[start:end],
        "start": start,
        "total": 3,
    }


@pytest.mark.parametrize("width", [1440, 390])
def test_diff_and_transcript_viewer(page, site, width):
    url, _, _ = site
    seen = []

    def diff(route):
        seen.append(route.request.url)
        older = "base=upstream" in route.request.url
        route.fulfill(json={**DIFF, "base": "upstream/release_1.0"} if older else DIFF)

    def conversation(route):
        seen.append(route.request.url)
        earlier = "before=1" in route.request.url
        route.fulfill(json=transcript(0, 1) if earlier else transcript(1))

    page.route("**/api/workspace-diff?*", diff)
    page.route("**/api/workspace-transcript?*", conversation)
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(url + "/#workspaces")
    line = page.locator("#ws-list tr").filter(has=page.get_by_text("open-work", exact=True))
    line.get_by_role("button", name="Diff", exact=True).click()
    dialog = page.locator("#ws-viewer")
    expect(dialog).to_be_visible()
    # Comments can go to the agent: its session resumes even without a workspace.
    expect(page.locator("#ws-viewer-meta")).to_have_text(
        "2 files changed, +1 −1 since origin/main. Click a line to comment on it."
    )
    expect(dialog.locator(".ws-add")).to_contain_text("+new line")
    expect(dialog.locator(".ws-del")).to_have_text("-old line\n")
    expect(dialog.get_by_text("Binary", exact=True)).to_be_visible()
    expect(dialog.get_by_text("notes.txt")).to_be_visible()
    assert "key=%2Fsrc%2Fworktrees%2Frepo%2Fopen-work" in seen[0] and "scope=branch" in seen[0]
    dialog.get_by_label("Base").select_option("upstream/release_1.0")
    expect(page.locator("#ws-viewer-meta")).to_contain_text("since upstream/release_1.0")
    assert "base=upstream%2Frelease_1.0" in seen[-1]
    if width == 1440:
        page.screenshot(path="reports/workspaces-diff.png")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")

    dialog.get_by_role("tab", name="Transcript").click()
    expect(page.locator("#ws-viewer-meta")).to_contain_text("Claude session s1, 3 entries")
    expect(dialog.get_by_role("button", name="Show earlier entries (1)")).to_be_visible()
    tool = dialog.locator(".ws-tool")
    expect(tool.locator("summary")).to_contain_text("Bash")
    expect(tool.get_by_text("Error")).to_be_visible()
    tool.locator("summary").click()
    expect(tool.locator(".ws-tool-error")).to_have_text("1 failed")
    dialog.get_by_role("button", name="Show earlier entries (1)").click()
    dialog.locator(".ws-tool summary").click()
    expect(dialog.locator(".ws-user")).to_contain_text("Fix the crash")
    expect(dialog.locator(".ws-msg")).to_have_count(2)
    expect(dialog.get_by_label("Session")).to_have_value("s1")
    if width == 1440:
        page.screenshot(path="reports/workspaces-transcript.png")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    dialog.get_by_role("button", name="Close workspace viewer").click()
    expect(dialog).to_be_hidden()


def test_viewer_reports_errors_and_skips_rows_without_a_checkout(page, site):
    url, plugin, _ = site
    plugin.value["workspaces"] = [
        *copy.deepcopy(WORKSPACES),
        row("gone", key="workspace:w5", path=None, status="missing", missing=True),
    ]

    def diff(route):
        if "scope=uncommitted" in route.request.url:
            route.fulfill(json={**DIFF, "scope": "uncommitted", "base": None, "bases": []})
        else:
            route.fulfill(status=400, json={"error": "Unknown workspace; refresh"})

    page.route("**/api/workspace-diff?*", diff)
    page.goto(url + "/#workspaces")
    gone = page.locator("#ws-list tr").filter(has=page.get_by_text("gone", exact=True))
    expect(gone.get_by_role("button", name="Diff")).to_have_count(0)
    line = page.locator("#ws-list tr").filter(has=page.get_by_text("open-work", exact=True))
    line.get_by_role("button", name="Diff", exact=True).click()
    expect(page.locator("#ws-viewer-error")).to_have_text("Unknown workspace; refresh")
    # The other choices stay reachable after a failure.
    page.locator("#ws-viewer").get_by_label("Changes").select_option("uncommitted")
    expect(page.locator("#ws-viewer-meta")).to_contain_text("since the last commit")
    expect(page.locator("#ws-viewer-error")).to_have_text("")


def test_a_slow_response_never_overrides_a_later_choice(page, site):
    url, _, _ = site
    held = []
    page.route("**/api/workspace-diff?*", lambda route: held.append(route))
    page.route("**/api/workspace-transcript?*", lambda route: route.fulfill(json=transcript()))
    page.goto(url + "/#workspaces")
    line = page.locator("#ws-list tr").filter(has=page.get_by_text("open-work", exact=True))
    line.get_by_role("button", name="Diff", exact=True).click()
    dialog = page.locator("#ws-viewer")
    expect(page.locator("#ws-viewer-meta")).to_have_text("Loading…")
    dialog.get_by_role("tab", name="Transcript").click()
    expect(dialog.locator(".ws-user")).to_contain_text("Fix the crash")
    held[0].fulfill(json=DIFF)
    page.wait_for_timeout(200)
    expect(dialog.locator(".ws-file")).to_have_count(0)
    expect(dialog.get_by_role("tab", name="Transcript")).to_have_attribute("aria-selected", "true")


def test_a_live_transcript_updates_in_place(page, site):
    url, _, _ = site
    session = {"id": "s1", "agent": "codex", "updated": 1, "size": 1, "title": "Go"}
    tool = {"role": "tool", "name": "exec", "input": "make test", "output": None, "error": False}
    first = {
        "sessions": [session],
        "session": session,
        "entries": [{"role": "user", "text": "Go"}, tool],
        "start": 0,
        "total": 2,
    }
    requests = []

    def conversation(route):
        requests.append(route.request.url)
        if "after=0" in route.request.url:
            done = {**tool, "output": '{"exit_code":2}', "error": True}
            route.fulfill(
                json={
                    **first,
                    "entries": [
                        first["entries"][0],
                        done,
                        {"role": "assistant", "text": "Tests fail."},
                    ],
                    "total": 3,
                }
            )
        else:
            route.fulfill(json=first)

    page.clock.install()
    page.route("**/api/workspace-transcript?*", conversation)
    page.goto(url + "/#workspaces")
    line = page.locator("#ws-list tr").filter(has=page.get_by_text("open-work", exact=True))
    line.get_by_role("button", name="Transcript", exact=True).click()
    dialog = page.locator("#ws-viewer")
    details = dialog.locator(".ws-tool")
    details.locator("summary").click()
    expect(details).to_have_attribute("open", "")
    page.clock.fast_forward(10000)
    expect(dialog.locator(".ws-assistant")).to_have_text("CodexTests fail.")
    expect(details.locator(".ws-tool-error")).to_have_text('{"exit_code":2}')
    expect(details).to_have_attribute("open", "")
    expect(page.locator("#ws-viewer-meta")).to_contain_text("3 entries")
    assert "after=0" in requests[-1]


def test_a_row_listed_without_a_workspace_can_resume_its_session(page, site):
    url, _, _ = site
    key = "/src/worktrees/repo/open-work"
    session = {"id": "s1", "agent": "claude", "title": "Fix the crash", "updated": 1}
    reopened = {"workspace": None}
    agents_seen, sent = [], []

    def agents(route):
        agents_seen.append(route.request.url)
        live = reopened["workspace"]
        route.fulfill(
            json={
                "agents": (
                    [{"pane": f"{live}:p1", "agent": "claude", "status": "working"}] if live else []
                ),
                "sessions": [session],
                "path": key,
                "workspace": live,
                "url": live and f"https://collie.example.ts.net/space/{live}",
            }
        )

    def message(route):
        body = route.request.post_data_json
        sent.append(body)
        reopened["workspace"] = "w4"
        pane = body.get("pane") or "w4:p1"
        route.fulfill(json={"sent": True, "pane": pane, "resumed": body.get("resume")})

    page.route("**/api/workspace-agents?*", agents)
    page.route("**/api/workspace-message", message)
    page.route("**/api/workspace-transcript?*", lambda route: route.fulfill(json=transcript()))
    page.goto(url + "/#workspaces")
    line = page.locator("#ws-list tr").filter(has=page.get_by_text("open-work", exact=True))
    line.get_by_role("button", name="Transcript", exact=True).click()
    dialog = page.locator("#ws-viewer")
    expect(page.locator("#ws-message-agent")).to_contain_text("No agent is running")
    assert "key=%2Fsrc%2Fworktrees%2Frepo%2Fopen-work" in agents_seen[0]
    dialog.get_by_label("Message the agent").fill("Keep going")
    dialog.get_by_role("button", name="Resume and send").click()
    expect(page.locator("#ws-message-status")).to_have_text(
        "Resumed the session in w4:p1 and sent the message."
    )
    assert sent == [{"key": key, "resume": "s1", "text": "Keep going"}]
    # The reopened workspace is followed: its agent is messaged there from now on.
    expect(page.locator("#ws-message-agent")).to_contain_text("To claude · working")
    expect(dialog.get_by_role("link", name="Open in Collie")).to_have_attribute(
        "href", "https://collie.example.ts.net/space/w4"
    )
    dialog.get_by_label("Message the agent").fill("And add a test")
    dialog.get_by_role("button", name="Send to agent").click()
    expect(page.locator("#ws-message-status")).to_have_text("Sent to the agent in w4:p1.")
    assert sent[-1] == {"workspace": "w4", "pane": "w4:p1", "text": "And add a test"}
