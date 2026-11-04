"""Moving the transcript's agent to a worker from the viewer, in real Chromium."""

import copy

import pytest
from playwright.sync_api import expect
from test_dashboard_browser import dashboard_site as dashboard_site
from test_dashboard_browser import viewer_routes as viewer_routes
from test_dashboard_browser import workspace_routes as workspace_routes

pytestmark = pytest.mark.browser

PLAN = {
    "workspace": "w1",
    "pane": "w1:p2",
    "session": "s1",
    "agent": "claude",
    "status": "working",
    "path": "/fixture/checkout",
    "branch": "feature",
    "head": "a" * 40,
    "changed": 2,
    "untracked": 1,
    "unpushed": 3,
    "children": [{"pid": 5000, "command": "/bin/zsh -c pytest -x"}],
    "children_count": 1,
    "watch": None,
    "docker": False,
    "ignored_kept": ["debug.log"],
    "ignored_kept_count": 1,
    "model": "opus",
    "effort": None,
    "confirm": True,
    "options": [
        {"id": "box", "label": "Linux box", "note": "3/4 free", "disabled": False},
        {"id": "full", "label": "Full box", "note": "full (4/4)", "disabled": True},
    ],
    "machine": "box",
    "label": "Linux box",
    "reason": "auto: least busy, 3/4 free",
    "error": None,
}
RESULT = {
    "machine": "box",
    "machine_label": "Linux box",
    "path": "/home/b/src/worktrees/repo/feature",
    "url": "http://box.test:8787/?workspace=w1",
    "warnings": [],
}


def open_viewer(page, url, agents):
    # With workers registered, the server names this machine beside them.
    page.route(
        "**/api/workspace-agents?*",
        lambda route: route.fulfill(
            json={
                "agents": agents,
                "sessions": [],
                "path": "/fixture/checkout",
                "machine_label": "this Mac",
            }
        ),
    )
    page.goto(url + "/#prs")
    page.locator("#pr-list tr").first.get_by_role("button", name="Transcript", exact=True).click()
    return page.locator("#ws-viewer").get_by_role("region", name="Agent status")


@pytest.mark.parametrize("width", [1280, 390])
def test_moving_a_working_agent_asks_first_and_follows_the_move(
    page, dashboard_site, viewer_routes, width
):
    url, _ = dashboard_site
    agents = [{"pane": "w1:p2", "agent": "claude", "status": "working", "session": "s1"}]
    posts = []
    follows = [
        {"status": "running", "message": "Copying feature to Linux box (12 KB)"},
        {"status": "moved", "message": "Moved to Linux box", "result": RESULT},
    ]

    def move(route):
        assert route.request.headers["x-babysit-action"] == "workspace-move"
        body = route.request.post_data_json
        posts.append(body)
        if body.get("plan"):
            route.fulfill(json={"plan": copy.deepcopy(PLAN)})
        elif "id" in body:
            route.fulfill(json={"move": {"id": "m1", "result": None, **follows.pop(0)}})
        else:
            route.fulfill(
                json={"move": {"id": "m1", "status": "running", "message": "Stopping its turn"}}
            )

    page.route("**/api/workspace-move", move)
    page.set_viewport_size({"width": width, "height": 844})
    page.clock.install()
    live = open_viewer(page, url, agents)
    live.get_by_role("button", name="Move Claude in w1:p2 to another machine").click()
    card = live.get_by_role("group", name="Confirm moving the agent")
    expect(card).to_contain_text("Move Claude in w1:p2 to another machine?")
    expect(card).to_contain_text("Branch feature goes there with 3 commits no remote has")
    expect(card).to_contain_text("its turn is stopped first")
    expect(card).to_contain_text("Exiting it here stops 1 process it started:")
    expect(card).to_contain_text("Files Git ignores are not copied.")
    expect(card).to_contain_text("1 ignored path would not be copied, so the worktree stays")
    expect(card.locator(".ws-move-children li")).to_have_text(
        ["debug.log", "/bin/zsh -c pytest -x"]
    )
    target = card.get_by_label("Machine to move to")
    expect(target.locator("option").first).to_have_text("Auto (→ Linux box)")
    expect(target.locator("option[value=full]")).to_have_js_property("disabled", True)
    # Enter on the question keeps the agent here.
    expect(card.get_by_role("button", name="Keep it here")).to_be_focused()
    assert posts == [
        {"workspace": "w1", "pane": "w1:p2", "session": "s1", "machine": "auto", "plan": True}
    ]
    if width == 390:
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        live.screenshot(path="reports/viewer-move-confirm-phone.png")
    card.get_by_role("button", name="Stop its turn and move to Linux box").click()
    status = page.locator("#ws-live-status")
    expect(status).to_have_text("Stopping its turn")
    assert posts[1] == {
        "workspace": "w1",
        "pane": "w1:p2",
        "session": "s1",
        "machine": "box",
        "confirm_working": True,
    }
    expect(
        live.get_by_role("button", name="Move Claude in w1:p2 to another machine")
    ).to_be_disabled()
    page.clock.run_for(2100)
    expect(status).to_have_text("Copying feature to Linux box (12 KB)")
    agents.clear()
    page.clock.run_for(2100)
    expect(status).to_contain_text("Moved to Linux box: /home/b/src/worktrees/repo/feature")
    expect(status.get_by_role("link", name="Open it in Collie")).to_have_attribute(
        "href", RESULT["url"]
    )
    assert posts[2:] == [{"id": "m1"}, {"id": "m1"}]


def test_a_move_that_cannot_go_says_why_and_a_failed_one_how_to_resume(
    page, dashboard_site, viewer_routes
):
    url, _ = dashboard_site
    agents = [{"pane": "w1:p2", "agent": "claude", "status": "idle", "session": "s1"}]
    plans = [
        {
            **PLAN,
            "status": "idle",
            "confirm": False,
            "children": [],
            "children_count": 0,
            "watch": {"id": "0123456789abcdef", "status": "awaiting_release"},
            "machine": None,
            "label": None,
            "error": "babysit watch 0123456789abcdef follows this workspace, and watches on "
            "Mac mini need a Linux worker; stop the watch first",
        },
        {
            **PLAN,
            "status": "idle",
            "confirm": False,
            "children": [],
            "children_count": 0,
            "ignored_kept": [],
            "ignored_kept_count": 0,
        },
    ]
    failed = {
        "id": "m2",
        "status": "failed",
        "error": "Linux box's herdr did not answer",
        "result": {
            "stage": "after_exit",
            "source_kept": True,
            "resume_command": "cd /fixture/checkout && claude --resume s1",
        },
    }

    def move(route):
        body = route.request.post_data_json
        if body.get("plan"):
            route.fulfill(json={"plan": plans.pop(0)})
        else:
            route.fulfill(json={"move": failed})

    page.route("**/api/workspace-move", move)
    live = open_viewer(page, url, agents)
    live.get_by_role("button", name="Move Claude in w1:p2 to another machine").click()
    card = live.get_by_role("group", name="Confirm moving the agent")
    expect(card.locator(".ws-move-error")).to_contain_text("need a Linux worker")
    expect(card.get_by_role("button", name="Move", exact=True)).to_be_disabled()
    expect(card).not_to_contain_text("stopped first")
    card.get_by_label("Machine to move to").select_option("box")
    card.get_by_role("button", name="Move to Linux box").click()
    expect(page.locator("#ws-live-status")).to_have_text(
        "Linux box's herdr did not answer. The checkout here was kept; resume it here with: "
        "cd /fixture/checkout && claude --resume s1"
    )
