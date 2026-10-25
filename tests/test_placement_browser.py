"""The Machine select, placement preview and machine badges, with stubbed workspace data."""

import time

import pytest
from playwright.sync_api import expect
from test_dashboard_browser import dashboard_site as dashboard_site

pytestmark = pytest.mark.browser

LOCAL = {
    "id": "local",
    "label": "This machine",
    "local": True,
    "agents": 2,
    "working": 1,
    "error": None,
}
MINI = {
    "id": "mini",
    "label": "Mac mini",
    "local": False,
    "enabled": True,
    "max_agents": 4,
    "agents": 4,
    "working": 2,
    "error": None,
}
BOX = {
    "id": "box",
    "label": "Linux box",
    "local": False,
    "enabled": True,
    "max_agents": 8,
    "agents": 0,
    "working": 0,
    "error": "ssh: connect to host box port 22: Connection refused",
}


@pytest.fixture
def site(page, dashboard_site):
    snapshot = {
        "prs": {},
        "issues": {},
        "new": {},
        "error": None,
        "synced_at": 1234,
        "agent_choices": {"codex": {"models": [], "efforts": []}},
        "machines": [LOCAL],
    }
    page.route("**/api/workspaces", lambda route: route.fulfill(json=snapshot))
    page.route(
        "**/api/workspace-repos*",
        lambda route: route.fulfill(
            json={
                "repos": [
                    {"repo": "up/alpha", "clone": "/fixture/alpha", "remote": "origin", "active": 1}
                ],
                "idle": 0,
            }
        ),
    )
    previews = []

    def preview(route):
        previews.append(route.request.url.partition("?")[2])
        docker = "docker=1" in previews[-1]
        route.fulfill(
            json={
                "machine": "local",
                "label": "This machine",
                "reason": "auto: workers are only used when chosen, 6/8 free",
                "options": [
                    {
                        "id": "mini",
                        "label": "Mac mini",
                        "note": "lacks docker" if docker else "full (4/4)",
                        "disabled": docker,
                    },
                    {"id": "box", "label": "Linux box", "note": "unreachable 3m", "disabled": True},
                ],
            }
        )

    page.route("**/api/placement?*", preview)
    sent = []

    def start(route):
        body = route.request.post_data_json
        sent.append(body)
        op = {
            "id": "op1",
            "pr": "new:1",
            "status": "failed",
            "machine": body.get("machine") if body.get("machine") != "auto" else None,
            "message": "Stopped by the test",
            "log": "",
            "subject": {"kind": "scratch", "repo": "up/alpha", "branch": "try"},
        }
        route.fulfill(json={"operation": op})

    page.route("**/api/workspace-new", start)
    page.route(
        "**/api/workspace-overview*",
        lambda route: route.fulfill(
            json={"enabled": True, "workspaces": [], "warnings": [], "synced_at": time.time()}
        ),
    )
    return dashboard_site[0], snapshot, previews, sent


def open_new_task(page, url):
    page.goto(url)
    page.get_by_role("button", name="New task").click()
    dialog = page.locator("#workspace-dialog")
    expect(dialog.get_by_label("Agent", exact=True)).to_be_visible()
    return dialog


def test_one_machine_shows_no_select_and_sends_no_machine(page, site):
    url, _, previews, sent = site
    dialog = open_new_task(page, url)
    expect(dialog.locator("#workspace-machine")).to_be_hidden()
    dialog.get_by_label("New branch").fill("try")
    dialog.get_by_label("Task", exact=True).fill("Explore")
    dialog.get_by_role("button", name="Start task").click()
    expect(page.locator("#workspace-progress")).to_have_text("failed: Stopped by the test")
    assert "machine" not in sent[0] and not previews
    page.goto(url + "/#workspaces")
    expect(page.locator("#ws-count")).to_have_text("0")
    expect(page.locator("#ws-machines")).to_be_hidden()


def test_machine_select_previews_and_disables_machines_that_cannot_take_it(page, site):
    url, snapshot, previews, sent = site
    snapshot["machines"] = [LOCAL, MINI, BOX]
    dialog = open_new_task(page, url)
    select = dialog.get_by_label("Machine", exact=True)
    expect(select).to_be_visible()
    options = select.locator("option")
    expect(options).to_have_text(
        ["Auto (→ This machine)", "This machine · 2 agents", "Mac mini · full (4/4)"]
        + ["Linux box · unreachable 3m"]
    )
    expect(select).to_have_value("auto")
    note = dialog.locator(".machine-choice")
    expect(note).to_have_text("auto: workers are only used when chosen, 6/8 free")
    expect(options.nth(3)).to_be_disabled()
    expect(options.nth(2)).to_be_enabled()  # Full: a person may still choose it.
    assert previews[0] == "agent=codex&repo=up%2Falpha"
    # Docker changes what each machine can take.
    dialog.get_by_label("Allow Docker in the agent’s sandbox").check()
    expect(options.nth(2)).to_have_text("Mac mini · lacks docker")
    expect(options.nth(2)).to_be_disabled()
    assert previews[-1] == "agent=codex&repo=up%2Falpha&docker=1"
    dialog.get_by_label("Allow Docker in the agent’s sandbox").uncheck()
    expect(options.nth(2)).to_be_enabled()
    select.select_option("mini")
    expect(note).to_have_text("full (4/4). Starting agents on Mac mini is not supported yet.")
    dialog.get_by_label("New branch").fill("try")
    dialog.get_by_label("Task", exact=True).fill("Explore")
    dialog.get_by_role("button", name="Start task").click()
    expect(page.locator("#workspace-progress")).to_have_text(
        "failed: Stopped by the test · On Mac mini"
    )
    assert sent[-1]["machine"] == "mini"
    page.screenshot(path="reports/placement-dialog.png")


@pytest.mark.parametrize("width", [1440, 390])
def test_workspaces_toolbar_shows_machine_health(page, site, width):
    url, snapshot, _, _ = site
    snapshot["machines"] = [
        LOCAL,
        MINI,
        BOX,
        {**MINI, "id": "off", "label": "Old", "enabled": False},
    ]
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(url + "/#workspaces")
    badges = page.locator("#ws-machines .badge")
    expect(badges).to_have_text(
        [
            "This machine · 2 agents",
            "Mac mini · 4/4 busy",
            "Linux box · unreachable",
            "Old · disabled",
        ]
    )
    expect(badges.nth(0)).to_have_class("badge green")
    expect(badges.nth(1)).to_have_class("badge amber")
    expect(badges.nth(2)).to_have_class("badge red")
    expect(badges.nth(2)).to_have_attribute("title", BOX["error"])
    expect(badges.nth(3)).to_have_class("badge")
    assert page.evaluate("document.documentElement.scrollWidth") <= width
    page.screenshot(path=f"reports/placement-badges-{width}.png")
