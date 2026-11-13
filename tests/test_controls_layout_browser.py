"""Daily dashboard flows across touch targets, responsive controls and long dialogs."""

import pr_supervisor as supervisor
import pytest
from playwright.sync_api import expect
from test_agents_browser import agents_routes as agents_routes
from test_attention_browser import feed_with, item
from test_dashboard_browser import choose_option, read_job
from test_dashboard_browser import dashboard_site as dashboard_site
from test_dashboard_browser import viewer_routes as viewer_routes
from test_dashboard_browser import workspace_routes as workspace_routes
from test_workspace_overview_browser import WORKSPACES

pytestmark = pytest.mark.browser


@pytest.mark.parametrize("width", [320, 390, 768, 1024])
def test_touch_actions_keep_their_size_when_they_wrap(browser, dashboard_site, width):
    context = browser.new_context(viewport={"width": width, "height": 900}, has_touch=True)
    page = context.new_page()
    try:
        page.goto(dashboard_site[0])
        expect(page.locator(".attention-item").first).to_be_visible()
        targets = page.locator(
            "#usage-toggle, #voice-toggle, #settings-toggle, #notifications-toggle, "
            ".attention-item button, .attention-group > .attention-batch button"
        )
        sizes = targets.evaluate_all(
            "els => els.filter(e => e.getClientRects().length).map(e => {"
            "const r=e.getBoundingClientRect();return [e.id || e.textContent,r.width,r.height]})"
        )
        assert sizes
        assert all(w >= 44 and h >= 44 for _, w, h in sizes), sizes
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    finally:
        context.close()


@pytest.mark.parametrize("width", [320, 390, 1024])
def test_task_is_first_and_footer_survives_scroll_and_execution_changes(
    page, dashboard_site, workspace_routes, width
):
    url, _ = dashboard_site
    _, snapshot, _ = workspace_routes
    snapshot["agent_choices"] = {
        "codex": {
            "models": [{"id": "fixture", "efforts": ["low", "high"]}],
            "efforts": ["low", "high"],
        }
    }
    page.route(
        "**/api/workspace-repos*",
        lambda r: r.fulfill(
            json={
                "repos": [
                    {"repo": "test/alpha", "clone": "/fixture/alpha", "active": 1},
                    {"repo": "test/beta", "clone": "/fixture/beta", "active": 1},
                ],
                "idle": 0,
            }
        ),
    )
    sent = []

    def start(route):
        sent.append(route.request.post_data_json)
        route.fulfill(
            json={
                "operation": {
                    "id": "test",
                    "pr": "new:1",
                    "status": "running",
                    "message": "Starting",
                    "log": "",
                }
            }
        )

    page.route("**/api/workspace-new", start)
    page.set_viewport_size({"width": width, "height": 844})
    page.goto(url)
    page.locator("#new-task").click()
    task = page.get_by_label("Task", exact=True)
    expect(task).to_be_in_viewport(ratio=1)
    assert task.bounding_box()["height"] >= 100
    page.screenshot(path=f"reports/layout-new-task-{width}.png")
    submit = page.get_by_role("button", name="Start task", exact=True)
    expect(submit).to_be_in_viewport(ratio=1)
    expect(page.locator(".execution-settings")).not_to_have_attribute("open", "")
    task.fill("Keep this draft while choosing a repository and execution settings")
    choose_option(page.get_by_role("combobox", name="Repository and local clone"), "1")
    page.get_by_label("New branch", exact=True).fill("layout")
    page.locator(".execution-settings > summary").click()
    choose_option(page.get_by_role("combobox", name="Model (optional)", exact=True), "fixture")
    page.get_by_label("Reasoning effort (optional)", exact=True).select_option("high")
    expect(page.locator(".execution-settings > summary")).to_contain_text("fixture · high")
    expect(task).to_have_value("Keep this draft while choosing a repository and execution settings")
    expect(page.locator('label[for="workspace-claude-account"]')).to_be_hidden()
    page.locator(".new-task-body").evaluate("e => e.scrollTop=e.scrollHeight")
    expect(submit).to_be_in_viewport(ratio=1)
    assert submit.evaluate(
        "e=>{const r=e.getBoundingClientRect();return e.contains(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2))}"
    )
    submit.click()
    expect(page.locator("#workspace-progress")).to_contain_text("running")
    assert sent[0]["repo"] == "test/beta"
    assert sent[0]["task"].startswith("Keep this draft")
    assert sent[0]["effort"] == "high"
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


@pytest.mark.parametrize("width", [320, 390])
def test_mobile_view_chooser_tracks_conditional_views_and_back_navigation(
    page, dashboard_site, width
):
    url, _ = dashboard_site
    page.set_viewport_size({"width": width, "height": 844})
    page.route(
        "**/api/workspace-overview*", lambda r: r.fulfill(json={"enabled": True, "workspaces": []})
    )
    page.goto(url)
    chooser = page.get_by_role("combobox", name="Dashboard view", exact=True)
    expect(chooser.locator('option[value="workspaces"]')).to_have_count(1)
    chooser.select_option("workspaces")
    expect(page.locator("#workspaces-panel")).to_be_visible()
    page.go_back()
    expect(page.locator("#attention-panel")).to_be_visible()
    expect(chooser).to_have_value("attention")
    tabs = page.locator(".page-tabs").bounding_box()
    tag = page.locator(".tag-focus-bar").bounding_box()
    assert tabs and tag and tabs["y"] + tabs["height"] <= tag["y"]
    assert tabs["width"] >= width - 30
    page.screenshot(path=f"reports/layout-navigation-{width}.png")


@pytest.mark.parametrize("width", [390, 1024])
def test_filter_chips_and_empty_state_recover_without_losing_search_or_selection(
    page, dashboard_site, width
):
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(dashboard_site[0] + "/#prs")
    row = page.locator("#pr-list tr").first
    row.locator(".item-select").check()
    if width == 390:
        page.locator("#pr-filters-toggle").click()
    ci = page.get_by_role("combobox", name="Filter pull requests by CI", exact=True)
    assert ci.evaluate("e=>e.tagName") == "SELECT"
    ci.select_option("FAILURE")
    if width == 390:
        page.locator("#pr-filters-toggle").click()
    chip = page.get_by_role("button", name="Remove CI filter: Failed", exact=True)
    expect(chip).to_be_visible()
    page.locator("#pr-search").fill("missing")
    expect(page.locator("#pr-empty")).to_be_visible()
    recovery = page.locator("#pr-empty").get_by_role("button", name="Clear filters", exact=True)
    recovery.focus()
    page.evaluate("prTable.refresh()")
    expect(recovery).to_be_focused()
    recovery.click()
    expect(page.locator("#pr-search")).to_have_value("missing")
    expect(page.locator("#pr-ci")).to_have_value("all")
    expect(page.locator("#pr-batch-count")).to_have_text("1 selected")
    page.locator("#pr-empty").get_by_role("button", name="Clear search", exact=True).click()
    expect(page.locator("#pr-list tr")).to_have_count(2)
    expect(page.locator("#pr-list .item-select:checked")).to_have_count(1)


def test_settings_keep_unsaved_prompt_on_declined_close_and_during_auto_save(page, dashboard_site):
    page.goto(dashboard_site[0])
    page.locator("#settings-toggle").click()
    task = page.get_by_label("Task prompt text")
    draft = "Review {url} and preserve my draft."
    task.fill(draft)
    expect(page.locator("#settings-prompt-status")).to_have_text("Unsaved changes")
    page.get_by_label("Main reviewer", exact=True).select_option("claude")
    expect(page.locator("#settings-auto-status")).to_have_text("Saved.")
    expect(task).to_have_value(draft)
    page.once("dialog", lambda d: d.dismiss())
    page.keyboard.press("Escape")
    expect(page.locator("#settings-dialog")).to_be_visible()
    expect(task).to_have_value(draft)
    page.once("dialog", lambda d: d.dismiss())
    page.locator("#settings-close").click()
    expect(task).to_have_value(draft)
    page.locator("#settings-save").click()
    expect(page.locator("#settings-prompt-status")).to_have_text("Prompt is up to date.")
    page.locator("#settings-close").click()
    expect(page.locator("#settings-dialog")).to_be_hidden()
    page.locator("#settings-toggle").click()
    expect(task).to_have_value(draft)


@pytest.mark.parametrize("width", [390, 1440])
def test_feedback_is_before_metadata_and_cancellation_is_confirmed(page, dashboard_site, width):
    url, home = dashboard_site
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(url + "/#watcher")
    page.locator("#list .watch").filter(has_text="test/repo").click()
    heading = page.locator(".feedback-heading")
    button = heading.get_by_role("button", name="Handle feedback", exact=True)
    expect(button).to_be_visible()
    assert page.locator(".feedback-section").evaluate(
        "e=>e.nextElementSibling.classList.contains('facts')"
    )
    if width == 1440:
        expect(button).to_be_in_viewport(ratio=1)
    page.locator(".watch-management > summary").click()
    page.once("dialog", lambda d: d.dismiss())
    page.get_by_role("button", name="Cancel watch", exact=True).click()
    assert read_job(home, "feedback")["status"] == "watching"
    page.once("dialog", lambda d: d.accept())
    page.get_by_role("button", name="Cancel watch", exact=True).click()
    expect(page.locator("#ended")).to_have_text("2")
    assert read_job(home, "feedback")["status"] == "stopped"


@pytest.mark.parametrize("width", [768, 1024, 1259])
def test_intermediate_cards_keep_metadata_and_actions_compact(
    page, dashboard_site, workspace_routes, width
):
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(dashboard_site[0] + "/#prs")
    rows = page.locator("#pr-list tr")
    expect(rows).to_have_count(2)
    box = rows.first.bounding_box()
    assert box and box["height"] < 340
    expect(rows.first.get_by_role("button", name="Open workspace", exact=True)).to_be_visible()
    rows.first.get_by_role("button", name="Dates and activity", exact=False).click()
    expect(rows.first.locator(".pr-opened")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path=f"reports/layout-tablet-{width}.png")


@pytest.mark.parametrize("width", [320, 390])
def test_viewer_title_has_its_own_row_and_view_tabs_stay_reachable(
    page, dashboard_site, agents_routes, width
):
    page.set_viewport_size({"width": width, "height": 844})
    page.goto(dashboard_site[0] + "/#agents")
    page.locator("#agents-list .agents-row").filter(has_text="working-work").locator(
        ".agents-open"
    ).click()
    title = page.locator("#ws-viewer-title").bounding_box()
    controls = page.locator(".ws-viewer-actions").bounding_box()
    assert title and controls and title["y"] + title["height"] <= controls["y"]
    assert title["width"] >= width - 40
    expect(page.get_by_role("tab", name="Transcript", exact=True)).to_be_visible()
    expect(page.get_by_role("tab", name="Terminal", exact=True)).to_be_visible()
    page.screenshot(path=f"reports/layout-viewer-{width}.png")


def test_prompt_save_preserves_edits_made_during_the_request(page, dashboard_site):
    page.goto(dashboard_site[0])
    page.locator("#settings-toggle").click()
    prompt = page.get_by_label("Task prompt text")
    held = []
    page.route("**/api/settings", lambda route: held.append(route))
    prompt.fill("Review {url} with the first instructions.")
    page.locator("#settings-save").click()
    expect(page.locator("#settings-save")).to_be_disabled()
    prompt.fill("Review {url} with newer instructions typed while saving.")
    held[0].fulfill(
        json={
            "task_prompts": {"prReview": "Review {url} with the first instructions."},
            "reviewer": "codex",
            "local_max_agents": 20,
        }
    )
    expect(page.locator("#settings-save")).to_be_enabled()
    expect(prompt).to_have_value("Review {url} with newer instructions typed while saving.")
    expect(page.locator("#settings-prompt-status")).to_have_text("Unsaved changes")


def test_long_feedback_keeps_approval_reachable_on_the_phone(page, dashboard_site):
    url, home = dashboard_site
    db = supervisor.open_db(home)
    with db:
        job = supervisor.get_job(db, "feedback")
        template = job["pending_reviews"][0]
        job["pending_reviews"] = [
            {**template, "id": str(index), "body": "Please review this part carefully. " * 80}
            for index in range(6)
        ]
        supervisor.save_job(db, job)
    db.close()
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(url + "/#watcher")
    page.locator("#list .watch").filter(has_text="test/repo").click()
    page.locator(".feedback-item").nth(2).scroll_into_view_if_needed()
    action = page.get_by_role("button", name="Handle feedback", exact=True)
    expect(action).to_be_in_viewport(ratio=1)
    assert action.evaluate(
        "e=>{const r=e.getBoundingClientRect();return e.contains(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2))}"
    )


@pytest.mark.parametrize("view", ["attention", "workspaces"])
def test_inventory_selection_has_a_full_touch_hit_area(browser, dashboard_site, view):
    context = browser.new_context(viewport={"width": 390, "height": 844}, has_touch=True)
    page = context.new_page()
    try:
        page.route(
            "**/api/workspace-overview*",
            lambda r: r.fulfill(
                json={"enabled": True, "workspaces": WORKSPACES, "warnings": [], "synced_at": 1}
            ),
        )
        feed = feed_with(
            [
                item(
                    "https://github.com/test/alpha/pull/8",
                    "review",
                    [("review_requested", "Your review is requested")],
                )
            ]
        )
        page.route("**/api/attention*", lambda r: r.fulfill(json=feed))
        feed["agents_waiting"] = 1
        page.goto(dashboard_site[0] + "/#" + view)
        checkbox = page.locator(
            ".attention-pick" if view == "attention" else "#ws-list .ws-select input:not(:disabled)"
        ).first
        expect(checkbox).to_be_visible()
        waiting = page.locator("#agents-waiting")
        expect(waiting).to_be_visible()
        waiting_bounds = waiting.bounding_box()
        assert waiting_bounds and waiting_bounds["width"] >= 44 and waiting_bounds["height"] >= 44
        bounds = checkbox.bounding_box()
        assert bounds and bounds["width"] >= 44 and bounds["height"] >= 44
        checkbox.click(position={"x": 2, "y": 22})
        expect(checkbox).to_be_checked()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    finally:
        context.close()
