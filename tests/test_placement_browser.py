"""The Machine select, placement preview and machine badges, with stubbed workspace data."""

import json
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
    "pending": False,
    "available": True,
    "reason": None,
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
    "pending": False,
    "available": True,
    "reason": "full (4/4)",
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
    "pending": False,
    "available": False,
    "reason": "unreachable 3m",
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
    calls = {"workspaces": 0, "machines": 0}

    def workspaces(route):
        calls["workspaces"] += 1
        route.fulfill(json=snapshot)

    def machines(route):
        calls["machines"] += 1
        if calls.get("machines_down"):
            route.fulfill(status=500, json={"error": "down"})
        else:
            route.fulfill(json={"machines": snapshot["machines"]})

    page.route("**/api/workspaces", workspaces)
    page.route("**/api/machines", machines)
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
                "reason": "auto: least busy, 18/20 free",
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
    return dashboard_site[0], snapshot, previews, sent, calls


def register_workers(site_home):
    """The watcher status names workers, which is what lets the badges poll at all."""
    worker = {"id": "mini", "label": "Mac mini", "ssh": "mini", "os": "darwin"}
    (site_home / "machines.json").write_text(json.dumps({"version": 1, "machines": [worker]}))


def open_new_task(page, url):
    page.goto(url)
    page.get_by_role("button", name="New task").click()
    dialog = page.locator("#workspace-dialog")
    expect(dialog.get_by_label("Agent", exact=True)).to_be_visible()
    return dialog


def test_one_machine_shows_no_select_and_sends_no_machine(page, site):
    url, _, previews, sent, _ = site
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
    url, snapshot, previews, sent, _ = site
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
    expect(note).to_have_text("auto: least busy, 18/20 free")
    expect(options.nth(3)).to_be_disabled()
    expect(options.nth(2)).to_be_enabled()  # Full: a person may still choose it.
    assert previews[0] == "agent=codex&kind=scratch&repo=up%2Falpha"
    # Docker changes what each machine can take.
    dialog.get_by_label("Allow Docker in the agent’s sandbox").check()
    expect(options.nth(2)).to_have_text("Mac mini · lacks docker")
    expect(options.nth(2)).to_be_disabled()
    assert previews[-1] == "agent=codex&kind=scratch&repo=up%2Falpha&docker=1"
    dialog.get_by_label("Allow Docker in the agent’s sandbox").uncheck()
    expect(options.nth(2)).to_be_enabled()
    select.select_option("mini")
    expect(note).to_have_text(
        "full (4/4). The agent starts on Mac mini, in its own clone of the repository; "
        "only reviews and new tasks can start there yet."
    )
    dialog.get_by_label("New branch").fill("try")
    dialog.get_by_label("Task", exact=True).fill("Explore")
    dialog.get_by_role("button", name="Start task").click()
    expect(page.locator("#workspace-progress")).to_have_text(
        "failed: Stopped by the test · On Mac mini"
    )
    assert sent[-1]["machine"] == "mini"
    page.screenshot(path="reports/placement-dialog.png")


@pytest.mark.parametrize("width", [1440, 390])
def test_workspaces_toolbar_shows_machine_health(page, site, dashboard_site, width):
    url, snapshot, _, _, calls = site
    register_workers(dashboard_site[1])
    snapshot["machines"] = [
        LOCAL,
        MINI,
        BOX,
        {**MINI, "id": "off", "label": "Old", "enabled": False}
        | {"available": False, "reason": "disabled"},
        {**MINI, "id": "new", "label": "New", "agents": 0, "pending": True, "reason": "checking…"},
    ]
    page.set_viewport_size({"width": width, "height": 900})
    page.clock.install()
    page.clock.pause_at(time.time() + 1)
    page.goto(url + "/#workspaces")
    badges = page.locator("#ws-machines .badge")
    expect(badges).to_have_text(
        [
            "This machine · 2 agents",
            "Mac mini · 4/4 busy",
            "Linux box · unreachable 3m",
            "Old · disabled",
            "New · checking…",
        ]
    )
    expect(badges.nth(0)).to_have_class("badge green")
    expect(badges.nth(1)).to_have_class("badge amber")
    expect(badges.nth(2)).to_have_class("badge red")
    expect(badges.nth(2)).to_have_attribute("title", BOX["error"])
    expect(badges.nth(3)).to_have_class("badge")
    expect(badges.nth(4)).to_have_class("badge")
    # The badges poll the machines alone, never the whole workspace snapshot.
    asked = calls["machines"]
    snapshot["machines"] = [LOCAL, {**MINI, "agents": 1, "reason": None}]
    page.clock.run_for(15000)
    expect(badges).to_have_text(["This machine · 2 agents", "Mac mini · 1/4 busy"])
    assert calls["machines"] == asked + 1 and calls["workspaces"] == 0
    assert page.evaluate("document.documentElement.scrollWidth") <= width
    page.screenshot(path=f"reports/placement-badges-{width}.png")


def test_one_machine_never_asks_for_machines(page, site):
    url, _, _, _, calls = site
    page.clock.install()
    page.clock.pause_at(time.time() + 1)
    with page.expect_response("**/api/status"):
        page.goto(url + "/#workspaces")
    expect(page.locator("#ws-count")).to_have_text("0")
    for _ in range(3):
        with page.expect_response("**/api/status"):
            page.clock.run_for(15000)
    expect(page.locator("#ws-machines")).to_be_hidden()
    assert calls["machines"] == 0 and calls["workspaces"] == 0


def test_badges_appear_once_the_status_names_workers(page, site, dashboard_site):
    url, snapshot, _, _, calls = site
    register_workers(dashboard_site[1])
    snapshot["machines"] = [LOCAL, MINI]
    calls["machines_down"] = True
    page.goto(url + "/#workspaces")
    # A failed answer is asked again by the next poll, never left for good.
    page.wait_for_function("() => !machinesBusy")
    expect(page.locator("#ws-machines")).to_be_hidden()
    asked = calls["machines"]
    assert asked >= 1
    calls["machines_down"] = False
    expect(page.locator("#ws-machines .badge")).to_have_text(
        ["This machine · 2 agents", "Mac mini · 4/4 busy"], timeout=20000
    )
    assert calls["workspaces"] == 0


def test_a_refused_choice_stays_with_its_reason(page, site):
    url, snapshot, _, sent, _ = site
    snapshot["machines"] = [LOCAL, MINI, BOX]
    dialog = open_new_task(page, url)
    select = dialog.get_by_label("Machine", exact=True)
    note = dialog.locator(".machine-choice")
    select.select_option("mini")
    expect(note).to_have_text(
        "full (4/4). The agent starts on Mac mini, in its own clone of the repository; "
        "only reviews and new tasks can start there yet."
    )
    dialog.get_by_label("Allow Docker in the agent’s sandbox").check()
    expect(select.locator("option").nth(2)).to_be_disabled()
    expect(select).to_have_value("mini")
    expect(note).to_have_text("Mac mini cannot take this launch: lacks docker.")
    dialog.get_by_label("New branch").fill("try")
    dialog.get_by_label("Task", exact=True).fill("Explore")
    dialog.get_by_role("button", name="Start task").click()
    expect(page.locator("#workspace-progress")).to_contain_text("failed:")
    # The server, not the page, refuses it.
    assert sent[-1]["machine"] == "mini"


def test_new_task_previews_again_when_it_files_an_issue(page, site):
    url, snapshot, *_ = site
    snapshot["machines"] = [LOCAL, MINI, BOX]
    dialog = open_new_task(page, url)
    expect(dialog.get_by_label("Machine", exact=True)).to_be_visible()
    issue = dialog.get_by_label("File it as a GitHub issue first")

    def preview(kind):
        return lambda request: "/api/placement?" in request.url and f"kind={kind}" in request.url

    # Workers take no issue work: the preview is asked again for one, and back.
    with page.expect_request(preview("issue")):
        issue.check()
    with page.expect_request(preview("scratch")):
        issue.uncheck()
