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


SHA = "b" * 40


def register_workers(site_home):
    """The watcher status names workers, which is what lets the badges poll at all."""
    worker = {"id": "mini", "label": "Mac mini", "ssh": "mini", "os": "darwin"}
    (site_home / "machines.json").write_text(json.dumps({"version": 1, "machines": [worker]}))


def open_new_task(page, url):
    page.goto(url)
    page.get_by_role("button", name="New task").click()
    dialog = page.locator("#workspace-dialog")
    dialog.locator(".execution-settings > summary").click()
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
    # Badges are asked for only once the status names workers, so wait for that first
    # answer itself: machinesBusy is also false before it was ever asked.
    with page.expect_response("**/api/machines") as answer:
        page.goto(url + "/#workspaces")
    assert answer.value.status == 500
    # A failed answer is asked again by the next poll, never left for good.
    page.wait_for_function("() => !machinesBusy")
    expect(page.locator("#ws-machines")).to_be_hidden()
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


def test_a_refusal_for_a_stale_worker_offers_to_update_it(page, site):
    url, snapshot, _, sent, calls = site
    chonk = {**BOX, "id": "chonk", "label": "chonk", "os": "linux", "error": None}
    chonk.update(available=True, reason=None, updatable=True, update=None)
    snapshot["machines"] = [LOCAL, MINI, chonk]
    page.route(
        "**/api/placement?*",
        lambda route: route.fulfill(
            json={
                "machine": "local",
                "label": "This machine",
                "reason": "auto",
                "options": [{"id": "chonk", "label": "chonk", "note": "", "disabled": False}],
            }
        ),
    )

    def start(route):
        sent.append(route.request.post_data_json)
        message = "chonk runs babysit-pr 735c74d406ac, this machine b8583a4112d7: update"
        op = {"id": "op1", "pr": "new:1", "status": "failed", "machine": "chonk"}
        op.update(message=message, log=message, stale_worker={"machine": "chonk", "sha": SHA})
        op["subject"] = {"kind": "scratch", "repo": "up/alpha", "branch": "try"}
        route.fulfill(json={"operation": op})

    page.route("**/api/workspace-new", start)
    posted = []
    running = {"running": True, "sha": SHA, "ok": None, "error": None, "finished_at": None}

    def update(route):
        posted.append((route.request.headers, route.request.post_data_json))
        chonk["update"] = running
        route.fulfill(json=running)

    page.route("**/api/worker-update", update)
    dialog = open_new_task(page, url)
    dialog.get_by_label("Machine", exact=True).select_option("chonk")
    dialog.get_by_label("New branch").fill("try")
    dialog.get_by_label("Task", exact=True).fill("Explore")
    dialog.get_by_role("button", name="Start task").click()
    notice = dialog.locator(".worker-update")
    expect(notice).to_contain_text("chonk runs another babysit-pr commit than this machine.")
    notice.get_by_role("button", name="Update chonk").click()
    expect(notice).to_contain_text("Updating babysit-pr on chonk to bbbbbbbbbbbb…")
    expect(notice.get_by_role("button", name="Update chonk")).to_be_disabled()
    [(headers, body)] = posted
    assert headers["x-babysit-action"] == "worker-update" and body == {"machine": "chonk"}
    chonk["update"] = {**running, "running": False, "ok": True, "finished_at": 1300}
    expect(notice).to_have_text(
        "chonk now runs babysit-pr bbbbbbbbbbbb. Start the task again.", timeout=10000
    )
    expect(notice.get_by_role("button")).to_have_count(0)
    # The failed launch filed nothing, so it can be started again.
    expect(dialog.get_by_role("button", name="Start task")).to_be_enabled()


def test_an_update_to_an_older_commit_offers_the_update_again(page, site):
    url, snapshot, *_ = site
    chonk = {**BOX, "id": "chonk", "label": "chonk", "os": "linux", "error": None}
    older = {"running": False, "sha": "a" * 40, "ok": True, "error": None, "finished_at": 1}
    chonk.update(available=True, reason=None, updatable=True, update=older)
    snapshot["machines"] = [LOCAL, chonk]

    def start(route):
        op = {"id": "op1", "pr": "new:1", "status": "failed", "machine": "chonk"}
        op.update(message="chonk runs babysit-pr aaaaaaaaaaaa", log="")
        op["stale_worker"] = {"machine": "chonk", "sha": SHA}
        route.fulfill(json={"operation": op})

    page.route("**/api/workspace-new", start)
    dialog = open_new_task(page, url)
    dialog.get_by_label("Machine", exact=True).select_option("chonk")
    dialog.get_by_label("New branch").fill("try")
    dialog.get_by_label("Task", exact=True).fill("Explore")
    dialog.get_by_role("button", name="Start task").click()
    notice = dialog.locator(".worker-update")
    expect(notice).to_contain_text("chonk runs another babysit-pr commit than this machine.")
    expect(notice.get_by_role("button", name="Update chonk")).to_be_enabled()


def test_a_worker_provisioning_does_not_manage_gets_no_update_button(page, site):
    url, snapshot, *_ = site
    mini = {**MINI, "updatable": False, "update": None}
    snapshot["machines"] = [LOCAL, mini]

    def start(route):
        op = {"id": "op1", "pr": "new:1", "status": "failed", "machine": "mini"}
        op.update(message="Mac mini runs babysit-pr 735c74d406ac", log="")
        op["stale_worker"] = {"machine": "mini", "sha": SHA}
        route.fulfill(json={"operation": op})

    page.route("**/api/workspace-new", start)
    dialog = open_new_task(page, url)
    dialog.get_by_label("Machine", exact=True).select_option("mini")
    dialog.get_by_label("New branch").fill("try")
    dialog.get_by_label("Task", exact=True).fill("Explore")
    dialog.get_by_role("button", name="Start task").click()
    expect(page.locator("#workspace-progress")).to_contain_text("Mac mini runs babysit-pr")
    expect(dialog.locator(".worker-update")).to_have_count(0)


IN_DIRECTORY = "Work in an existing directory, as it is"


def previewed(previews, query):
    """Wait for the placement preview the dialog asks for after a choice changed."""
    deadline = time.monotonic() + 5
    while previews[-1:] != [query] and time.monotonic() < deadline:
        time.sleep(0.02)
    assert previews[-1:] == [query]


def test_a_task_in_a_directory_names_its_machine_and_sends_only_the_directory(page, site):
    url, snapshot, previews, sent, _ = site
    snapshot["machines"] = [LOCAL, MINI, BOX]
    dialog = open_new_task(page, url)
    select = dialog.get_by_label("Machine", exact=True)
    expect(select).to_have_value("auto")
    directory = dialog.get_by_label("Directory", exact=True)
    expect(directory).to_be_hidden()
    dialog.get_by_label(IN_DIRECTORY).check()
    # Nothing of a repository, a branch, an issue or other clones is asked for.
    expect(directory).to_be_visible()
    expect(dialog.get_by_role("combobox", name="Repository and local clone")).to_be_hidden()
    for name in ("New branch", "Base branch (optional)"):
        expect(dialog.get_by_label(name)).to_be_hidden()
    for text in (
        "Clone another repository from GitHub",
        "File it as a GitHub issue first",
        "Also work in other repositories (optional)",
    ):
        expect(dialog.get_by_text(text)).to_be_hidden()
    # A directory is one machine's: this one stands in for Auto, which is not offered.
    expect(select).to_have_value("local")
    expect(select.locator("option").first).to_be_disabled()
    expect(dialog.locator(".machine-choice")).to_have_text("Runs on this machine.")
    previewed(previews, "agent=codex&kind=directory")
    # This machine's clones are suggested; a worker's paths are typed.
    suggested = dialog.locator("#new-task-directories option")
    expect(suggested).to_have_count(1)
    assert suggested.first.get_attribute("value") == "/fixture/alpha"
    select.select_option("mini")
    expect(suggested).to_have_count(0)
    expect(dialog.locator(".machine-choice")).to_have_text(
        "full (4/4). The agent starts on Mac mini, in that directory there."
    )
    dialog.get_by_label("Task", exact=True).fill("Tidy the notes")
    # A relative path names nothing on another machine: the form keeps it.
    directory.fill("notes")
    dialog.get_by_role("button", name="Start task").click()
    assert not sent and directory.evaluate("e => e.validity.patternMismatch")
    directory.fill(" ~/src/notes ")
    page.screenshot(path="reports/new-task-directory.png")
    dialog.get_by_role("button", name="Start task").click()
    expect(page.locator("#workspace-progress")).to_have_text(
        "failed: Stopped by the test · On Mac mini"
    )
    assert sent == [
        {"directory": "~/src/notes", "agent": "codex", "machine": "mini", "task": "Tidy the notes"}
    ]


def test_leaving_the_directory_mode_brings_the_repository_fields_and_auto_back(page, site):
    url, snapshot, previews, sent, _ = site
    snapshot["machines"] = [LOCAL, MINI, BOX]
    dialog = open_new_task(page, url)
    select = dialog.get_by_label("Machine", exact=True)
    dialog.get_by_label(IN_DIRECTORY).check()
    expect(select).to_have_value("local")
    dialog.get_by_label(IN_DIRECTORY).uncheck()
    expect(select).to_have_value("auto")
    expect(select.locator("option").first).to_be_enabled()
    # A machine the person chose meanwhile is theirs to keep.
    dialog.get_by_label(IN_DIRECTORY).check()
    select.select_option("mini")
    select.select_option("local")
    dialog.get_by_label(IN_DIRECTORY).uncheck()
    expect(select).to_have_value("local")
    select.select_option("auto")
    expect(dialog.get_by_label("Directory", exact=True)).to_be_hidden()
    previewed(previews, "agent=codex&kind=scratch&repo=up%2Falpha")
    dialog.get_by_label("New branch").fill("try")
    dialog.get_by_label("Task", exact=True).fill("Explore")
    dialog.get_by_role("button", name="Start task").click()
    expect(page.locator("#workspace-progress")).to_have_text("failed: Stopped by the test")
    assert sent[0]["name"] == "try" and sent[0]["machine"] == "auto"
    assert "directory" not in sent[0]


def test_on_one_machine_a_task_in_a_directory_sends_no_machine(page, site):
    url, _, previews, sent, _ = site
    dialog = open_new_task(page, url)
    # What was typed for a repository stays, hidden, and never blocks this task.
    dialog.get_by_label("New branch").fill("two words")
    dialog.get_by_text("Clone another repository from GitHub").click()
    dialog.get_by_label("GitHub repository").fill("not a repository")
    # Leaving the field checks it and shows why it is refused, which moves what is below.
    dialog.get_by_label("GitHub repository").press("Tab")
    expect(dialog.locator(".new-task-clone-note")).to_contain_text("Name the repository")
    dialog.get_by_label(IN_DIRECTORY).check()
    expect(dialog.locator("#workspace-machine")).to_be_hidden()
    dialog.get_by_label("Directory", exact=True).fill("/fixture/alpha")
    dialog.get_by_label("Task", exact=True).fill("Tidy")
    dialog.get_by_role("button", name="Start task").click()
    expect(page.locator("#workspace-progress")).to_have_text("failed: Stopped by the test")
    assert sent == [{"directory": "/fixture/alpha", "agent": "codex", "task": "Tidy"}]
    assert not previews
