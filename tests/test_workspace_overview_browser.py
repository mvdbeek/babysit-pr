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
