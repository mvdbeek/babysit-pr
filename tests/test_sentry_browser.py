"""Sentry experiment browser coverage against temporary servers and a canned backend."""

import copy
import threading
import time

import dashboard
import pytest
from playwright.sync_api import expect
from test_dashboard_browser import choose_option

pytestmark = pytest.mark.browser

HOST = "https://sentry.example.org/organizations/galaxy/issues"
GALAXY = "galaxyproject/galaxy"
GTN = "galaxyproject/training-material"


def project(slug, short_id, number, release="26.1.2.dev0", count=10):
    return {
        "slug": slug,
        "short_id": short_id,
        "id": str(number),
        "permalink": f"{HOST}/{number}/",
        "count": count,
        "users": 2,
        "release": release,
    }


def group(key, short_id, title, **changes):
    value = {
        "key": key,
        "repo": GALAXY,
        "title": title,
        "culprit": "galaxy.tools.parameters.basic in from_json",
        "type": title.split(":")[0],
        "level": "error",
        "priority": "high",
        "status": "unresolved",
        "substatus": "ongoing",
        "unhandled": True,
        "first_seen": "2026-09-20T10:00:00Z",
        "last_seen": "2026-09-25T09:00:00Z",
        "count": 20,
        "users": 3,
        "events_24h": 4,
        "short_id": short_id,
        "permalink": f"{HOST}/{key}/",
        "projects": [project("galaxy-main", short_id, 100)],
        "score": 1,
        "bucket": "low",
        "reasons": [],
        "triage": None,
        "publish": None,
    }
    value.update(changes)
    return value


DONE_TRIAGE = {
    "status": "done",
    "severity": "low",
    "confidence": "medium",
    "summary": "Integer parsing fails for blank form values.",
    "likely_cause": "An empty string reaches int() in from_json.",
    "user_impact": "Tool forms fail to load for affected users.",
    "suggested_area": "lib/galaxy/tools/parameters/basic.py",
    "reasons": ["Only a validation error", "No data loss"],
    "error": None,
    "updated_at": 1790000000.0,
    "disagrees": True,
}

GROUPS = [
    group(
        "a1",
        "GALAXY-MAIN-1A",
        "ValueError: invalid literal for int()",
        substatus="escalating",
        count=1349,
        users=11,
        events_24h=212,
        score=11,
        bucket="critical",
        reasons=["escalating", "unhandled", "3 servers", "212 events/24h"],
        projects=[
            project("galaxy-main", "GALAXY-MAIN-1A", 101, count=1200),
            project("galaxy-eu", "GALAXY-EU-7", 102, release="26.1.1", count=100),
            project("galaxy-org", "GALAXY-ORG-3", 103, release="26.0.4", count=49),
        ],
        triage=DONE_TRIAGE,
    ),
    group(
        "b2",
        "GALAXY-MAIN-2B",
        "KeyError: 'history_id'",
        substatus="regressed",
        score=7,
        bucket="high",
        reasons=["regressed"],
        triage={"status": "queued", "error": None, "updated_at": 1790000000.0},
    ),
    group(
        "c3",
        "GTN-9",
        "TypeError: cannot read properties of undefined",
        repo=GTN,
        substatus="new",
        last_seen="2026-09-24T09:00:00Z",
        score=4,
        bucket="medium",
        projects=[project("gtn", "GTN-9", 300, release=None)],
        publish={
            "status": "created",
            "existing": None,
            "repo_public": True,
            "draft": {"title": "TypeError", "body": "Draft"},
            "sanitized": None,
            "findings": [],
            "url": f"https://github.com/{GTN}/issues/77",
            "number": 77,
            "writeback": "ok",
            "error": None,
        },
    ),
    group(
        "e5",
        "GALAXY-MAIN-5E",
        "AttributeError: 'NoneType' object has no attribute 'id'",
        score=4,
        bucket="medium",
        last_seen="2026-09-25T08:00:00Z",
    ),
    group(
        "d4",
        "GALAXY-MAIN-4D",
        '<img src=x onerror="window.injected=true">',
        culprit="<script>window.injected=true</script>",
        permalink="javascript:window.injected=true",
        projects=[
            {**project("galaxy-main", "GALAXY-MAIN-4D", 104), "permalink": "javascript:alert(1)"}
        ],
        reasons=["<b>bold</b>"],
    ),
]
ORDER = ["GALAXY-MAIN-1A", "GALAXY-MAIN-2B", "GALAXY-MAIN-5E", "GTN-9", "GALAXY-MAIN-4D"]


def canned():
    return {
        "enabled": True,
        "loading": False,
        "stale": False,
        "error": None,
        "synced_at": time.time(),
        "host": "https://sentry.example.org",
        "organization": "galaxy",
        "warnings": ["gtn: list truncated at 200 issues"],
        "projects": [
            {"slug": slug, "repo": repo, "issues": 3, "truncated": False}
            for slug, repo in [
                ("galaxy-main", GALAXY),
                ("galaxy-eu", GALAXY),
                ("galaxy-org", GALAXY),
                ("gtn", GTN),
            ]
        ],
        "groups": copy.deepcopy(GROUPS),
        "llm": {
            "worker": {"alive": True, "heartbeat_at": time.time()},
            "gate": {
                "state": "open",
                "reason": "Claude weekly quota 64% left",
                "checked_at": time.time(),
                "windows": [],
            },
            "budget": {"per_refresh": 5, "per_day": 40, "used_today": 3},
            "triage_enabled": True,
            "sanitizer_enabled": True,
        },
    }


class FakeSentry:
    """The SentryIssues surface the dashboard calls, with a scripted publish pipeline."""

    def __init__(self):
        self.value = canned()
        self.refreshes = 0
        self.requests = []
        self.sanitized = False  # The test decides when the sanitizer finishes.
        self.findings = []
        self.existing = None
        self.fail = None

    def find(self, key):
        found = [entry for entry in self.value["groups"] if entry["key"] == key]
        if not found:
            raise ValueError("Unknown Sentry group")
        return found[0]

    def snapshot(self):
        for entry in self.value["groups"]:
            publish = entry["publish"]
            if publish and publish["status"] == "sanitizing" and self.sanitized:
                publish.update(
                    status="ready",
                    sanitized={
                        "title": f"{entry['type']} in tool parameters",
                        "body": "Seen 1349 times.\nReported by [email] on [host].",
                        "redactions": [
                            {
                                "kind": "email",
                                "original_excerpt": "jane@example.org",
                                "replacement": "[email]",
                            }
                        ],
                        "concerns": ["The stack trace mentions an internal hostname"],
                        "safe_to_publish": True,
                    },
                    findings=self.findings,
                )
        return copy.deepcopy(self.value)

    def request_refresh(self):
        self.refreshes += 1

    def action(self, request):
        self.requests.append(request)
        if self.fail:
            raise ValueError(self.fail)
        entry = self.find(request.get("key"))
        if request["action"] == "retriage":
            entry["triage"] = {"status": "queued", "error": None, "updated_at": time.time()}
        elif request["action"] == "publish-draft":
            entry["publish"] = {
                "status": "ready" if self.existing else "sanitizing",
                "existing": self.existing,
                "repo_public": True,
                "draft": {
                    "title": entry["title"],
                    "body": "Reported by jane@example.org on galaxy-internal.example.org",
                },
                "sanitized": None,
                "findings": [],
                "url": None,
                "number": None,
                "writeback": "pending",
                "error": None,
            }
        elif request["action"] == "publish-create":
            if entry["publish"]["status"] != "ready":
                raise ValueError("The draft is not ready to publish")
            entry["publish"].update(
                status="created",
                url=f"https://github.com/{entry['repo']}/issues/42",
                number=42,
                writeback="failed",
            )
        elif request["action"] == "writeback-retry":
            entry["publish"]["writeback"] = "ok"
        return {"group": copy.deepcopy(entry)}


@pytest.fixture
def site(tmp_path):
    fake = FakeSentry()
    with dashboard.DashboardServer(tmp_path, 0, sentry=fake) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        yield f"http://127.0.0.1:{server.server_port}", fake
        server.shutdown()
        thread.join(3)


@pytest.fixture
def errors(page):
    found: list[str] = []
    page.on("pageerror", lambda error: found.append(str(error)))
    yield found
    assert not found


def rows(page):
    return page.locator("#sentry-list tr")


def row(page, short_id):
    return rows(page).filter(has=page.locator(f'.sentry-issue > a:text-is("{short_id}")'))


@pytest.mark.parametrize("width", [1440, 390])
def test_listing_sort_filters_keyboard_and_responsive_screenshots(page, site, errors, width):
    url, fake = site
    page.set_viewport_size({"width": width, "height": 1000})
    page.goto(url + "/#sentry")
    expect(page.get_by_role("tab", name="Sentry")).to_be_visible()
    expect(rows(page)).to_have_count(5)
    # Score first, then the most recent last_seen breaks the tie between the two mediums.
    assert rows(page).locator(".sentry-issue > a").all_inner_texts() == ORDER
    expect(page.locator("#sentry-count")).to_have_text("5")
    expect(page.locator("#sentry-status")).to_contain_text(
        "5 issue groups from 4 Sentry projects in galaxy"
    )
    expect(page.locator("#sentry-llm")).to_have_text(
        "LLM worker running · Gate open: Claude weekly quota 64% left · Budget 3/40 today"
    )
    expect(page.locator("#sentry-notices")).to_contain_text("list truncated at 200 issues")
    lead = row(page, "GALAXY-MAIN-1A")
    expect(lead.locator(".badge.red")).to_have_text("Critical")
    expect(lead).to_contain_text("Score 11")
    expect(lead.locator(".sentry-chip")).to_have_count(4)
    expect(lead.locator(".sentry-llm-badge.sentry-disagrees")).to_have_text("LLM: low")
    expect(lead).to_contain_text("212/24h")
    expect(lead).to_contain_text("1,349 events")
    expect(lead).to_contain_text("11 users")
    expect(lead.get_by_role("link", name="GALAXY-MAIN-1A")).to_have_attribute("href", f"{HOST}/a1/")
    for slug, number in [("galaxy-main", 101), ("galaxy-eu", 102), ("galaxy-org", 103)]:
        expect(lead.get_by_role("link", name=slug, exact=True)).to_have_attribute(
            "href", f"{HOST}/{number}/"
        )
    expect(lead).to_contain_text("26.1.1")
    lead.get_by_text("Triage notes", exact=True).click()
    expect(lead).to_contain_text("Likely cause: An empty string reaches int() in from_json.")
    expect(lead).to_contain_text("lib/galaxy/tools/parameters/basic.py")
    with page.expect_response("**/api/sentry"):  # Open notes survive the row rebuild.
        page.evaluate("window.dispatchEvent(new Event('sentry-visible'))")
    expect(lead.locator(".sentry-notes p").first).to_be_visible()
    expect(row(page, "GALAXY-MAIN-2B")).to_contain_text("Triage queued")
    expect(row(page, "GALAXY-MAIN-2B").get_by_role("button", name="Re-triage")).to_be_disabled()
    expect(row(page, "GTN-9").get_by_role("link", name="Open GitHub issue")).to_have_attribute(
        "href", f"https://github.com/{GTN}/issues/77"
    )
    expect(row(page, "GTN-9").get_by_role("button", name="Publish to GitHub")).to_have_count(0)
    page.screenshot(path=f"reports/sentry-{width}.png", full_page=True)
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    search = page.get_by_role("searchbox", name="Search Sentry issues").bounding_box()
    assert search["width"] >= 200, search

    severity = page.get_by_role("combobox", name="Filter Sentry issues by severity")
    choose_option(severity, "critical")
    expect(rows(page)).to_have_count(1)
    choose_option(severity, "medium")
    expect(rows(page)).to_have_count(2)
    choose_option(severity, "all")
    status = page.get_by_role("combobox", name="Filter Sentry issues by status")
    choose_option(status, "regressed")
    expect(rows(page)).to_have_count(1)
    expect(rows(page)).to_contain_text("KeyError")
    choose_option(status, "all")
    scope = page.get_by_role("combobox", name="Filter Sentry issues by repository")
    choose_option(scope, f"repo:{GTN}")
    expect(rows(page)).to_have_count(1)
    choose_option(scope, "project:galaxy-eu")
    expect(rows(page)).to_have_count(1)
    expect(page.locator("#sentry-count")).to_have_text("1")
    choose_option(scope, "all")
    search_box = page.get_by_role("searchbox", name="Search Sentry issues")
    search_box.fill("GALAXY-ORG-3")  # A secondary server's short id finds the group.
    expect(rows(page)).to_have_count(1)
    search_box.fill("tools.parameters.basic")  # Culprits are searched; the hostile one differs.
    expect(rows(page)).to_have_count(4)
    search_box.fill("training-material")
    expect(rows(page)).to_have_count(1)
    search_box.fill("nothing-here")
    expect(rows(page)).to_have_count(0)
    expect(page.locator("#sentry-empty")).to_have_text("No Sentry issue matches these filters.")
    search_box.fill("")

    # Workspaces and Upstream tests are disabled here, so arrows skip their hidden tabs.
    page.get_by_role("tab", name="Sentry").focus()
    page.keyboard.press("ArrowLeft")
    expect(page.locator("#issues-panel")).to_be_visible()
    page.keyboard.press("End")
    expect(page.locator("#sentry-panel")).to_be_visible()
    page.keyboard.press("ArrowRight")
    expect(page.locator("#watcher-panel")).to_be_visible()
    page.go_back()
    expect(page.locator("#sentry-panel")).to_be_visible()
    assert fake.refreshes == 0


def test_more_rows_load_fifty_at_a_time(page, site, errors):
    url, fake = site
    fake.value["groups"] = [
        group(f"k{index}", f"GALAXY-MAIN-{index}", f"Error {index}", score=index)
        for index in range(120)
    ]
    page.goto(url + "/#sentry")
    expect(rows(page)).to_have_count(50)
    expect(rows(page).first).to_contain_text("GALAXY-MAIN-119")
    more = page.get_by_role("button", name="Show 50 more")
    expect(more).to_have_text("Show 50 more (70 remaining)")
    more.click()
    expect(rows(page)).to_have_count(100)
    more.click()
    expect(rows(page)).to_have_count(120)
    expect(page.locator("#sentry-more")).to_be_hidden()
    choose_option(page.get_by_role("combobox", name="Filter Sentry issues by severity"), "low")
    expect(rows(page)).to_have_count(50)  # A new filter starts from the first page again.


def test_the_tab_stays_hidden_while_the_experiment_is_disabled(page, tmp_path, errors):
    with dashboard.DashboardServer(tmp_path, 0) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.server_port}"
        try:
            page.goto(url + "/#sentry")
            expect(page.locator("#sentry-status")).to_have_text(
                "Sentry overview is disabled. Enable it in experiments/sentry/config.json."
            )
            expect(page.locator("#sentry-tab")).to_be_hidden()
            page.get_by_role("tab", name="Watcher", exact=True).click()
            page.keyboard.press("End")
            expect(page.locator("#issues-panel")).to_be_visible()
            assert (
                "error"
                in page.request.post(
                    url + "/api/sentry-action",
                    headers={"X-Babysit-Action": "sentry-action"},
                    data={"action": "retriage", "key": "a1"},
                ).json()
            )
        finally:
            server.shutdown()
            thread.join(3)


def test_loading_stale_errors_empty_and_worker_state(page, site, errors):
    url, fake = site
    fake.value.update(loading=True, synced_at=None, groups=[], warnings=[])
    fake.value["llm"]["worker"]["alive"] = False
    fake.value["llm"]["gate"].update(state="paused", reason="Claude weekly quota 12% left (< 30%)")
    page.goto(url + "/#sentry")
    expect(page.locator("#sentry-status")).to_contain_text("Loading / refreshing…")
    expect(page.locator("#sentry-status")).to_contain_text("No observation yet.")
    expect(page.locator("#sentry-empty")).to_have_text("No Sentry snapshot yet.")
    expect(page.locator("#sentry-llm")).to_contain_text(
        "LLM worker not running (start the supervisor)"
    )
    expect(page.locator("#sentry-llm")).to_contain_text("Gate paused: Claude weekly quota 12% left")
    expect(page.locator("#sentry-llm")).to_have_class("pr-sync sentry-warn")

    fake.value.update(
        loading=False,
        stale=True,
        synced_at=time.time(),
        error="Sentry returned HTTP 401: check the token",
        warnings=[f"warning {index}" for index in range(8)],
    )
    page.locator("#sentry-refresh").click()
    expect(page.locator("#sentry-status")).to_contain_text("Stale snapshot — 0 issue groups")
    expect(page.locator("#sentry-notices .alert")).to_have_count(6)
    expect(page.locator("#sentry-notices")).to_contain_text("HTTP 401")
    expect(page.locator("#sentry-empty")).to_have_text(
        "No unresolved Sentry issues in the configured projects."
    )
    assert fake.refreshes == 1

    page.route("**/api/sentry?*", lambda route: route.fulfill(status=503, body="unavailable"))
    page.locator("#sentry-refresh").click()
    expect(page.locator("#sentry-status")).to_contain_text("Sentry API unavailable: HTTP 503")
    assert page.request.get(url + "/api/status").ok


def test_untrusted_text_is_rendered_as_text(page, site, errors):
    url, _ = site
    page.goto(url + "/#sentry")
    hostile = row(page, "GALAXY-MAIN-4D")
    expect(hostile).to_contain_text('<img src=x onerror="window.injected=true">')
    expect(hostile).to_contain_text("<script>window.injected=true</script>")
    expect(hostile).to_contain_text("<b>bold</b>")
    expect(page.locator("#sentry-list img, #sentry-list script, #sentry-list b")).to_have_count(0)
    # Non-https links stay inert text.
    expect(hostile.locator("a[href]")).to_have_count(0)
    assert page.evaluate("window.injected") is None


def test_retriage_posts_the_group_and_reports_refusals(page, site, errors):
    url, fake = site
    page.goto(url + "/#sentry")
    lead = row(page, "GALAXY-MAIN-1A")
    lead.get_by_role("button", name="Re-triage").click()
    expect(lead).to_contain_text("Triage queued")
    expect(lead.get_by_role("button", name="Re-triage")).to_be_disabled()
    assert fake.requests == [{"action": "retriage", "key": "a1"}]

    fake.fail = "The daily LLM budget is used up"
    other = row(page, "GALAXY-MAIN-5E")
    other.get_by_role("button", name="Re-triage").click()
    expect(other.get_by_role("alert")).to_have_text("The daily LLM budget is used up")
    expect(other.get_by_role("button", name="Re-triage")).to_be_enabled()

    fake.value["llm"]["triage_enabled"] = False
    page.locator("#sentry-refresh").click()
    expect(other.get_by_role("button", name="Re-triage")).to_be_disabled()
    expect(other.get_by_role("button", name="Re-triage")).to_have_attribute(
        "title", "LLM triage is disabled in the experiment config."
    )


def test_publish_dialog_reviews_sanitized_text_before_creating(page, site, errors):
    url, fake = site
    page.goto(url + "/#sentry")
    row(page, "GALAXY-MAIN-1A").get_by_role("button", name="Publish to GitHub").click()
    dialog = page.locator("#sentry-publish-dialog")
    expect(dialog).to_be_visible()
    expect(page.locator("#sentry-publish-status")).to_have_text(
        "The sanitizer is reviewing the draft…"
    )
    assert fake.requests == [{"action": "publish-draft", "key": "a1"}]
    expect(page.locator("#sentry-publish-public")).to_be_visible()
    create = dialog.get_by_role("button", name="Create GitHub issue")
    expect(create).to_be_disabled()
    expect(page.locator("#sentry-publish-form")).to_be_hidden()

    fake.sanitized = True
    title = dialog.get_by_role("textbox", name="Title")
    expect(title).to_have_value("ValueError in tool parameters")
    body = dialog.get_by_role("textbox", name="Body")
    expect(body).to_have_value("Seen 1349 times.\nReported by [email] on [host].")
    expect(dialog).to_contain_text("email: jane@example.org → [email]")
    expect(dialog).to_contain_text("The stack trace mentions an internal hostname")
    dialog.get_by_text("Server draft before sanitizing", exact=False).click()
    expect(page.locator("#sentry-publish-draft-text")).to_contain_text("jane@example.org")
    expect(create).to_be_disabled()  # Still needs the explicit review.
    reviewed = dialog.get_by_role("checkbox", name="I reviewed the sanitized text")
    assert reviewed.bounding_box()["width"] < 30
    reviewed.check()
    expect(create).to_be_enabled()
    title.fill("ValueError when a tool form has a blank integer")
    with page.expect_response("**/api/sentry"):  # A poll must not overwrite the edit.
        page.evaluate("window.dispatchEvent(new Event('sentry-visible'))")
    expect(title).to_have_value("ValueError when a tool form has a blank integer")
    page.screenshot(path="reports/sentry-publish-dialog.png")
    create.click()
    expect(dialog.get_by_role("link", name="#42")).to_have_attribute(
        "href", f"https://github.com/{GALAXY}/issues/42"
    )
    assert fake.requests[-1] == {
        "action": "publish-create",
        "key": "a1",
        "title": "ValueError when a tool form has a blank integer",
        "body": "Seen 1349 times.\nReported by [email] on [host].",
    }
    expect(create).to_be_hidden()
    expect(dialog).to_contain_text("The link could not be written back to Sentry.")
    dialog.get_by_role("button", name="Retry Sentry link").click()
    expect(dialog).to_contain_text("Linked back in Sentry.")
    assert fake.requests[-1] == {"action": "writeback-retry", "key": "a1"}
    dialog.get_by_role("button", name="Close GitHub issue draft").click()
    expect(dialog).to_be_hidden()
    expect(
        row(page, "GALAXY-MAIN-1A").get_by_role("link", name="Open GitHub issue")
    ).to_have_attribute("href", f"https://github.com/{GALAXY}/issues/42")


def test_pattern_findings_keep_create_disabled(page, site, errors):
    url, fake = site
    fake.sanitized = True
    fake.findings = [{"kind": "email", "excerpt": "ops@example.org"}]
    page.goto(url + "/#sentry")
    row(page, "GALAXY-MAIN-2B").get_by_role("button", name="Publish to GitHub").click()
    dialog = page.locator("#sentry-publish-dialog")
    expect(page.locator("#sentry-publish-findings")).to_contain_text("email (ops@example.org)")
    dialog.get_by_role("checkbox", name="I reviewed the sanitized text").check()
    expect(dialog.get_by_role("button", name="Create GitHub issue")).to_be_disabled()
    assert [r["action"] for r in fake.requests] == ["publish-draft"]


def test_a_refused_create_is_shown_inline(page, site, errors):
    url, fake = site
    fake.sanitized = True
    page.goto(url + "/#sentry")
    row(page, "GALAXY-MAIN-5E").get_by_role("button", name="Publish to GitHub").click()
    dialog = page.locator("#sentry-publish-dialog")
    dialog.get_by_role("checkbox", name="I reviewed the sanitized text").check()
    fake.fail = "The pattern check found an email address in the body"
    dialog.get_by_role("button", name="Create GitHub issue").click()
    expect(page.locator("#sentry-publish-error")).to_have_text(
        "The pattern check found an email address in the body"
    )
    expect(dialog.get_by_role("button", name="Create GitHub issue")).to_be_enabled()


def test_an_existing_issue_is_linked_instead_of_created(page, site, errors):
    url, fake = site
    fake.existing = {"url": f"https://github.com/{GALAXY}/issues/5", "number": 5}
    page.goto(url + "/#sentry")
    row(page, "GALAXY-MAIN-2B").get_by_role("button", name="Publish to GitHub").click()
    dialog = page.locator("#sentry-publish-dialog")
    expect(dialog.get_by_role("link", name="#5")).to_have_attribute(
        "href", f"https://github.com/{GALAXY}/issues/5"
    )
    expect(dialog.get_by_role("button", name="Create GitHub issue")).to_be_hidden()
    expect(page.locator("#sentry-publish-form")).to_be_hidden()


def test_handle_opens_the_workspace_dialog_with_a_prefilled_task(page, site, errors):
    url, _ = site
    submitted = []
    operation = {"value": None}

    def workspaces(route):
        route.fulfill(
            json={
                "prs": {},
                "issues": {},
                "watches": {},
                "sentry": {
                    "sentry:a1": {
                        "matches": [],
                        "suggestions": [],
                        "clones": ["/src/galaxy"],
                        "preferred_clone": "/src/galaxy",
                        "destination": None,
                        "operation": operation["value"],
                    }
                },
                "agent_choices": {
                    "codex": {"models": [], "efforts": []},
                    "claude": {"models": [], "efforts": []},
                },
                "error": None,
                "refreshing": False,
                "synced_at": 1,
            }
        )

    page.route("**/api/workspaces", workspaces)

    def action(route):
        submitted.append(route.request.post_data_json)
        route.fulfill(json={"operation": {"status": "queued", "message": "Queued"}})

    page.route("**/api/workspace-action", action)
    page.goto(url + "/#sentry")
    row(page, "GALAXY-MAIN-1A").get_by_role("button", name="Handle").click()
    dialog = page.locator("#workspace-dialog")
    expect(dialog).to_be_visible()
    expect(page.locator("#workspace-title")).to_have_text(f"{GALAXY} · GALAXY-MAIN-1A")
    task = page.locator("#workspace-task")
    expect(task).to_have_value(
        f"Investigate and fix Sentry issue GALAXY-MAIN-1A ({HOST}/a1/), also seen as "
        "GALAXY-EU-7 on galaxy-eu, GALAXY-ORG-3 on galaxy-org. Use the Sentry MCP tools to "
        "read the stack trace, breadcrumbs and recent events, find the root cause, implement "
        "the fix and add a regression test. Put `Fixes GALAXY-MAIN-1A` in the commit message. "
        "Do not resolve or change the issue in Sentry. Summarize the root cause, the fix and "
        "the validation."
    )
    dialog.get_by_role("button", name="Handle").click()
    expect(page.locator("#workspace-progress")).to_have_text("queued: Queued")
    # The dialog keeps polling from the Sentry tab until the operation finishes.
    operation["value"] = {
        "status": "complete",
        "message": "Workspace ready — Open in Collie",
        "result": {"url": "https://collie.example.ts.net/space/w1"},
        "log": "started",
    }
    expect(page.locator("#workspace-progress")).to_have_text(
        "complete: Workspace ready — Open in Collie", timeout=10000
    )
    expect(dialog.get_by_role("link", name="Open in Collie")).to_have_attribute(
        "href", "https://collie.example.ts.net/space/w1"
    )
    assert submitted[0]["id"] == "sentry:a1"
    assert submitted[0]["action"] == "handle"
    assert submitted[0]["clone"] == "/src/galaxy"


def test_a_failed_sanitizer_run_can_be_tried_again(page, site, errors):
    url, fake = site
    page.goto(url + "/#sentry")
    row(page, "GALAXY-MAIN-5E").get_by_role("button", name="Publish to GitHub").click()
    dialog = page.locator("#sentry-publish-dialog")
    expect(page.locator("#sentry-publish-status")).to_have_text(
        "The sanitizer is reviewing the draft…"
    )
    fake.find("e5")["publish"].update(status="failed", error="The sanitizer timed out")
    expect(page.locator("#sentry-publish-error")).to_have_text("The sanitizer timed out")
    expect(dialog.get_by_role("button", name="Create GitHub issue")).to_be_disabled()
    dialog.get_by_role("button", name="Try again").click()
    expect(page.locator("#sentry-publish-status")).to_have_text(
        "The sanitizer is reviewing the draft…"
    )
    expect(dialog.get_by_role("button", name="Try again")).to_be_hidden()
    assert [r["action"] for r in fake.requests] == ["publish-draft", "publish-draft"]


def test_handled_issues_show_their_work_and_can_be_filtered(page, site, errors):
    url, fake = site
    fake.find("a1")["handling"] = {
        "status": "complete",
        "message": "Workspace ready — Open in Collie",
        "agent": "codex",
        "updated_at": 1,
        "path": "/src/worktrees/galaxy/sentry-galaxy-main-1a",
        "workspace_url": "https://collie.example.ts.net/space/w7",
    }
    starting = fake.value["groups"][1]
    starting["handling"] = {"status": "running", "message": "Fetching Sentry issue", "path": None}
    failed = fake.value["groups"][2]
    failed["handling"] = {"status": "failed", "message": "clone refused", "path": None}
    page.goto(url + "/#sentry")
    handled = row(page, "GALAXY-MAIN-1A")
    expect(handled.locator(".sentry-work")).to_contain_text("Being handled")
    expect(handled.locator(".sentry-work")).to_contain_text("sentry-galaxy-main-1a")
    expect(handled.get_by_role("link", name="Open workspace")).to_have_attribute(
        "href", "https://collie.example.ts.net/space/w7"
    )
    expect(handled.get_by_role("button", name="Handle again")).to_be_enabled()
    busy = row(page, starting["short_id"])
    expect(busy.locator(".sentry-work")).to_contain_text("Starting agent…")
    expect(busy.get_by_role("button", name="Handle", exact=True)).to_be_disabled()
    broken = row(page, failed["short_id"])
    expect(broken.locator(".sentry-work")).to_have_count(0)
    expect(broken).to_contain_text("Last Handle failed: clone refused")
    choose_option(page.get_by_role("combobox", name="Filter Sentry issues by work"), "handled")
    expect(rows(page)).to_have_count(2)
    choose_option(page.get_by_role("combobox", name="Filter Sentry issues by work"), "unhandled")
    expect(rows(page)).to_have_count(len(fake.value["groups"]) - 2)
    # A worktree that exists without its herdr workspace still counts, without a link.
    fake.find("a1")["handling"]["workspace_url"] = None
    choose_option(page.get_by_role("combobox", name="Filter Sentry issues by work"), "all")
    page.locator("#sentry-refresh").click()
    expect(handled.locator(".sentry-work")).to_contain_text("Checkout exists")
    expect(handled.get_by_role("link", name="Open workspace")).to_have_count(0)
