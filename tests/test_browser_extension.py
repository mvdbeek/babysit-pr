"""Authenticated extension requests use isolated state and fake launchers."""

import copy
import http.client
import io
import json
import re
import shutil
import threading
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

import agent_messages
import agent_reviews
import browser_extension
import dashboard
import pr_questions
import pytest
from playwright.sync_api import expect

CLIENT = "a" * 32


class FakeWorkspaces:
    def __init__(self):
        self.calls = []
        self.operations = {}

    def targets(self):
        return [
            {
                "id": "pr-one",
                "repo": "test/repo",
                "url": "https://github.com/test/repo/pull/1",
                "title": "Fix tests",
            }
        ]

    def local_repositories(self, everything=False):
        return {"repos": [{"repo": "test/repo", "clone": "/fake/repo", "active": 1}], "idle": 0}

    def snapshot(self):
        return {
            "prs": {},
            "issues": {},
            "watches": {},
            "new": self.operations,
            "agent_choices": {
                "codex": {"models": [{"id": "fixture", "efforts": ["high"]}], "efforts": ["high"]}
            },
            "error": None,
            "refreshing": False,
            "synced_at": 1,
        }

    def new_task(self, payload):
        self.calls.append(copy.deepcopy(payload))
        operation = {
            "id": f"launch-{len(self.calls)}",
            "pr": "new-one",
            "status": "complete",
            "message": "Started",
            "result": {"url": "http://127.0.0.1:8787/space/fake"},
        }
        self.operations[operation["id"]] = {"operation": operation}
        return {"operation": operation}

    def operation_status(self, key):
        return self.operations.get(key, {}).get("operation")

    action = new_task


ISSUE = "https://github.com/test/repo/issues/3"


def with_issue(manager, monkeypatch, clones=None):
    """test/repo#3 in the issue overview, with these local clones of its repository."""
    targets = [
        *manager.targets(),
        {
            "id": "issue-three",
            "kind": "issue",
            "repo": "test/repo",
            "url": ISSUE,
            "title": "Broken",
        },
    ]
    monkeypatch.setattr(manager, "targets", lambda: targets)
    snapshot = manager.snapshot()
    snapshot["issues"] = {
        "issue-three": {"clones": clones or [], "destination": "/src/repo", "matches": []}
    }
    monkeypatch.setattr(manager, "snapshot", lambda: snapshot)


@pytest.fixture
def extension_server(tmp_path, monkeypatch):
    monkeypatch.setattr(browser_extension, "herdr", lambda *args: {"agents": []})
    with dashboard.DashboardServer(tmp_path, 0, workspaces=FakeWorkspaces()) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        yield server
        server.shutdown()
        thread.join(timeout=5)


def request(server, path, body=None, headers=None, method="POST"):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
    conn.request(method, path, json.dumps(body) if body is not None else None, headers or {})
    response = conn.getresponse()
    data = response.read()
    result = response.status, json.loads(data) if data else None, dict(response.getheaders())
    conn.close()
    return result


def pair(server):
    code, value, _ = request(
        server,
        "/api/extension-pair",
        {"id": CLIENT},
        {"Content-Type": "application/json", "X-Babysit-Action": "extension-pair"},
    )
    assert code == 200
    return {
        "Authorization": f"Bearer {value['token']}",
        "X-Babysit-Extension": CLIENT,
        "Origin": f"chrome-extension://{CLIENT}",
    }


def submission():
    return {
        "action": "submit",
        "request_id": "request-1234567890",
        "mode": "new",
        "payload": {
            "repo": "test/repo",
            "clone": "/fake/repo",
            "name": "test-task",
            "task": "Fix this",
        },
        "source": {
            "url": "https://github.com/test/repo/pull/1",
            "selection": "<script>untrusted</script>",
        },
        "babysit": True,
    }


def test_pairing_auth_cors_and_revocation(extension_server):
    server = extension_server
    assert request(server, "/api/extension", {"action": "context"})[0] == 403
    headers = pair(server)
    code, value, response = request(server, "/api/extension", {"action": "context"}, headers)
    assert code == 200 and value["repos"][0]["repo"] == "test/repo"
    assert response["Access-Control-Allow-Origin"] == headers["Origin"]
    assert (
        request(
            server,
            "/api/extension",
            headers={"Origin": headers["Origin"], "Access-Control-Request-Method": "POST"},
            method="OPTIONS",
        )[0]
        == 204
    )
    for changes in [
        {"Origin": "https://evil.test"},
        {"Origin": "null"},
        {"Host": "evil.test"},
        {"Authorization": "Bearer bad"},
        {"X-Babysit-Extension": "b" * 32},
    ]:
        assert (
            request(server, "/api/extension", {"action": "context"}, {**headers, **changes})[0]
            == 403
        )
    assert (
        request(
            server,
            "/api/extension-pair",
            {"id": CLIENT},
            {**headers, "Content-Type": "application/json", "X-Babysit-Action": "extension-pair"},
        )[0]
        == 403
    )
    assert (
        request(server, "/api/cancel", {"id": "watch"}, {**headers, "X-Babysit-Action": "cancel"})[
            0
        ]
        == 403
    )
    pair(server)  # Rotation invalidates the old credential.
    assert request(server, "/api/extension", {"action": "context"}, headers)[0] == 403
    headers = pair(server)
    server.extension.pair({"id": CLIENT, "revoke": True})
    assert request(server, "/api/extension", {"action": "context"}, headers)[0] == 403
    assert server.extension.path.stat().st_mode & 0o777 == 0o600


def test_delivery_is_idempotent_across_restart(extension_server):
    server = extension_server
    headers = pair(server)
    first = request(server, "/api/extension", submission(), headers)
    assert first[0] == 200
    server.extension = browser_extension.Extension(server.home)
    assert request(server, "/api/extension", submission(), headers)[1] == first[1]
    assert len(server.workspaces.calls) == 1
    task = server.workspaces.calls[0]["task"]
    assert "dashboard approval gate" in task and "untrusted page content" in task
    changed = submission()
    changed["payload"]["task"] = "Something else"
    assert request(server, "/api/extension", changed, headers)[0] == 400
    assert len(server.workspaces.calls) == 1
    assert (
        request(server, "/api/extension", {"action": "status", "id": "launch-1"}, headers)[1][
            "operation"
        ]["status"]
        == "complete"
    )


def test_the_extension_cannot_start_a_task_in_a_directory(extension_server):
    server = extension_server
    headers = pair(server)
    asked = submission()
    asked["payload"] = {"directory": "/fake/repo", "task": "Fix this"}
    code, value, _ = request(server, "/api/extension", asked, headers)
    assert code == 400 and "repository's clone only" in value["error"]
    assert server.workspaces.calls == []


def test_pending_or_failed_delivery_never_repeats(tmp_path):
    extension = browser_extension.Extension(tmp_path)
    calls = []

    def failed():
        calls.append(1)
        raise OSError("Partially delivered")

    assert extension.once(CLIENT, submission(), failed)["error"] == "Partially delivered"
    extension.once(CLIENT, submission(), failed)
    assert len(calls) == 1
    with extension.db() as db:
        db.execute("UPDATE requests SET result=NULL")
    with pytest.raises(ValueError, match="uncertain"):
        extension.once(CLIENT, submission(), failed)
    assert len(calls) == 1


def test_concurrent_delivery_is_claimed_before_launch(tmp_path):
    extension = browser_extension.Extension(tmp_path)
    started = threading.Event()
    finish = threading.Event()
    results = []

    def launch():
        started.set()
        assert finish.wait(5)
        return {"operation": {"id": "one"}}

    thread = threading.Thread(
        target=lambda: results.append(extension.once(CLIENT, submission(), launch))
    )
    thread.start()
    try:
        assert started.wait(5)
        with pytest.raises(ValueError, match="in progress"):
            extension.once(CLIENT, submission(), launch)
    finally:
        finish.set()
        thread.join(timeout=5)
    assert results == [{"operation": {"id": "one"}}]
    assert (
        extension.once(CLIENT, submission(), lambda: pytest.fail("Duplicate launch")) == results[0]
    )


@pytest.mark.parametrize(
    "change",
    [
        {"source": {"url": "file:///etc/passwd"}},
        {"source": {"selection": 4}},
        {"payload": []},
        {"babysit": "true"},
        {"mode": "invalid"},
        {"payload": {"task": "x" * 32000}},
    ],
)
def test_invalid_submission_never_launches(extension_server, change):
    assert (
        request(
            extension_server, "/api/extension", {**submission(), **change}, pair(extension_server)
        )[0]
        == 400
    )
    assert not extension_server.workspaces.calls


def test_followup_revalidates_session(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(
        agent_messages, "agents", lambda _, machine=None: [{"pane": "p1", "session": "current"}]
    )
    monkeypatch.setattr(
        agent_messages,
        "prompt",
        lambda target, text, machine=None: sent.append((target["pane"], text)) or {"sent": True},
    )
    request = {
        "mode": "message",
        "payload": {"workspace": "w1", "pane": "p1", "session": "old", "text": "Continue"},
    }
    server = SimpleNamespace(home=tmp_path)
    with pytest.raises(ValueError, match="session changed"):
        browser_extension.submit(server, request)
    assert not sent
    request["payload"]["session"] = "current"
    browser_extension.submit(server, request)
    assert sent == [("p1", "Continue")]


def test_the_dashboard_serves_its_extension_and_names_its_version(extension_server):
    server = extension_server
    shipped = browser_extension.shipped()
    source = Path(__file__).resolve().parents[1] / "extension"
    assert request(server, "/api/extension-package", method="GET")[1] == shipped
    assert re.fullmatch(r"[a-p]{32}", shipped["id"])

    def download(headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
        conn.request("GET", "/extension.zip", headers=headers or {})
        response = conn.getresponse()
        result = response.status, response.read(), dict(response.getheaders())
        conn.close()
        return result

    code, body, headers = download()
    assert code == 200 and headers["Content-Type"] == "application/zip"
    assert headers["Content-Disposition"] == (
        f'attachment; filename="babysitter-extension-{shipped["version"]}.zip"'
    )
    archive = zipfile.ZipFile(io.BytesIO(body))
    expected = {
        f"babysitter-extension/{path.relative_to(source).as_posix()}"
        for path in source.rglob("*")
        if path.is_file()
    }
    assert set(archive.namelist()) == expected
    assert archive.read("babysitter-extension/manifest.json") == (
        (source / "manifest.json").read_bytes()
    )
    # The same files make the same bytes, so a rebuild keeps the ETag.
    browser_extension._package_cache.clear()
    assert download()[1] == body
    assert download({"If-None-Match": headers["ETag"]})[0] == 304

    # Every reply to the extension names the version this dashboard ships.
    auth = pair(server)
    code, value, response = request(server, "/api/extension", {"action": "version"}, auth)
    assert code == 200 and value == {"version": shipped["version"]}
    assert response["X-Babysit-Extension-Version"] == shipped["version"]
    assert response["Access-Control-Expose-Headers"] == "X-Babysit-Extension-Version"


@pytest.fixture
def extension_browser(playwright, tmp_path, extension_server):
    extension_path = tmp_path / "extension"
    shutil.copytree(Path(__file__).resolve().parents[1] / "extension", extension_path)
    manifest_path = extension_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    # Pregrant only the temporary server and (for its content script, which tests route
    # to fixture pages) GitHub for automation; shipped permissions remain optional.
    manifest["host_permissions"] = ["http://127.0.0.1/*", "https://github.com/*"]
    manifest_path.write_text(json.dumps(manifest))
    with playwright.chromium.launch_persistent_context(
        tmp_path / "profile",
        channel="chromium",
        headless=True,
        args=[
            f"--disable-extensions-except={extension_path}",
            f"--load-extension={extension_path}",
        ],
    ) as context:
        # Register before checking the existing workers: one can start between a
        # check and wait_for_event, leaving that wait expecting a second event.
        started = []
        context.on("serviceworker", lambda worker: started.append(worker))
        page = context.new_page()
        deadline = time.monotonic() + 60
        while not started and not context.service_workers:
            if time.monotonic() >= deadline:
                raise RuntimeError("The extension service worker did not start")
            # Yield to Playwright so it can dispatch a worker event or update its list.
            page.wait_for_timeout(100)
        worker = (started or context.service_workers)[0]
        client = worker.url.split("/")[2]
        base = f"http://127.0.0.1:{extension_server.server_port}"
        page.goto(f"{base}/extension.html#id={client}")
        expect(page.locator("#extension-id")).to_have_value(client)
        page.get_by_role("button", name="Create pairing token").click()
        expect(page.locator("#token-box")).to_be_visible()
        token = page.locator("#token").input_value()
        worker.evaluate(
            "config => chrome.storage.local.set({config})", {"url": base, "token": token}
        )
        yield context, worker, client, base


@pytest.mark.browser
def test_real_extension_launch_reload_target_and_revoke(extension_browser, extension_server):
    context, worker, client, base = extension_browser
    page = context.new_page()
    page.goto(f"chrome-extension://{client}/compose.html")
    expect(page.locator("#status")).to_have_text("Ready.")
    expect(page.locator("#choices")).not_to_have_attribute("open", "")
    expect(page.locator("#destination-summary")).to_contain_text("test/repo")
    page.locator("#task").fill("Fix the selected problem")
    page.locator("#babysit").check()
    page.get_by_role("button", name="Start task", exact=True).click()
    expect(page.locator("#workspace")).to_have_attribute("href", "http://127.0.0.1:8787/space/fake")
    assert len(extension_server.workspaces.calls) == 1
    assert extension_server.workspaces.calls[0]["name"].startswith("fix-the-selected-problem-")
    assert "dashboard approval gate" in extension_server.workspaces.calls[0]["task"]
    page.reload()
    expect(page.locator("#send")).to_be_disabled()
    assert len(extension_server.workspaces.calls) == 1

    # GitHub subpages resolve to the known PR, select its repository and reuse Handle.
    draft = "12345678-1234-1234-1234-123456789012"
    worker.evaluate(
        "draft => chrome.storage.session.set({[draft]: {source: {url: 'https://github.com/test/repo/pull/1/files#diff', selection: 'failing test'}}})",
        draft,
    )
    page = context.new_page()
    page.goto(f"chrome-extension://{client}/compose.html#{draft}")
    expect(page.locator("#mode")).to_have_value("target")
    expect(page.locator("#branch-fields")).to_be_hidden()
    page.locator("#task").fill("Handle this PR")
    page.get_by_role("button", name="Start task", exact=True).click()
    expect(page.locator("#workspace")).to_be_visible()
    assert extension_server.workspaces.calls[-1]["action"] == "handle"
    assert extension_server.workspaces.calls[-1]["id"] == "pr-one"
    assert "failing test" in extension_server.workspaces.calls[-1]["task"]
    extension_server.extension.pair({"id": client, "revoke": True})
    page.get_by_role("button", name="Reload choices").click()
    expect(page.locator("#status")).to_contain_text("Pair this extension")


@pytest.mark.browser
def test_real_extension_dashboard_handoff_without_token(extension_browser, extension_server):
    context, worker, client, base = extension_browser
    worker.evaluate("config => chrome.storage.local.set({config})", {"url": base, "token": ""})
    page = context.new_page()
    page.goto(f"chrome-extension://{client}/compose.html")
    page.locator("#task").fill("Investigate the rendering")
    with context.expect_page() as opened:
        page.get_by_role("button", name="Open in dashboard").click()
    dashboard_page = opened.value
    expect(dashboard_page.locator("#workspace-task")).to_have_value("Investigate the rendering")
    expect(dashboard_page.locator("#new-task-name")).to_have_value(
        re.compile("investigate-the-rendering-[a-f0-9]{8}")
    )
    assert dashboard_page.url == base + "/#watcher"
    assert not extension_server.workspaces.calls  # A handoff only prefills, never launches.


@pytest.mark.browser
def test_real_extension_followup(extension_browser, extension_server, monkeypatch):
    context, _, client, _ = extension_browser
    monkeypatch.setattr(
        browser_extension,
        "herdr",
        lambda *args: {
            "agents": [
                {
                    "workspace_id": "w1",
                    "pane_id": "p1",
                    "agent": "codex",
                    "agent_session": {"value": "session1"},
                }
            ]
        },
    )
    monkeypatch.setattr(
        agent_messages, "agents", lambda _, machine=None: [{"pane": "p1", "session": "session1"}]
    )
    messages = []
    monkeypatch.setattr(
        agent_messages,
        "prompt",
        lambda target, text, machine=None: (
            messages.append((target["pane"], text)) or {"sent": True}
        ),
    )
    page = context.new_page()
    page.goto(f"chrome-extension://{client}/compose.html")
    expect(page.locator("#status")).to_have_text("Ready.")
    page.locator("#choices summary").click()
    page.locator("#mode").select_option("message")
    expect(page.locator("#recipient option")).to_have_count(2)
    page.locator("#recipient").select_option("w1:p1:session1")
    page.locator("#task").fill("Also check the regression")
    page.get_by_role("button", name="Send follow-up").click()
    expect(page.locator("#status")).to_contain_text("Follow-up sent")
    assert messages == [("p1", "Also check the regression")]


def test_agent_context_uses_verified_workspace_associations(extension_server, monkeypatch):
    manager = extension_server.workspaces
    snapshot = manager.snapshot()
    snapshot["prs"] = {"pr-one": {"matches": [{"workspace_id": "w1"}]}}
    monkeypatch.setattr(manager, "snapshot", lambda: snapshot)
    monkeypatch.setattr(
        browser_extension,
        "herdr",
        lambda *args: {
            "agents": [
                {
                    "workspace_id": "w1",
                    "pane_id": "p1",
                    "agent": "codex",
                    "agent_session": {"value": "s1"},
                },
                {
                    "workspace_id": "unrelated",
                    "pane_id": "p2",
                    "agent": "codex",
                    "agent_session": {"value": "s2"},
                },
            ]
        },
    )
    value = request(
        extension_server, "/api/extension", {"action": "context"}, pair(extension_server)
    )[1]
    assert value["agents"][0]["targets"] == ["https://github.com/test/repo/pull/1"]
    assert value["agents"][0]["repos"] == ["test/repo"]
    assert not value["agents"][1]["targets"]


def test_agent_discovery_failure_does_not_block_new_tasks(extension_server, monkeypatch):
    def unavailable(*args):
        raise ValueError("herdr offline")

    monkeypatch.setattr(browser_extension, "herdr", unavailable)
    code, value, _ = request(
        extension_server, "/api/extension", {"action": "context"}, pair(extension_server)
    )
    assert code == 200 and value["repos"]
    assert value["agents"] == [] and "herdr offline" in value["agents_error"]


def draft_page(context, worker, client, source):
    import uuid

    draft = str(uuid.uuid4())
    worker.evaluate(
        "args => chrome.storage.session.set({[args.id]: {source: args.source}})",
        {"id": draft, "source": source},
    )
    page = context.new_page()
    page.goto(f"chrome-extension://{client}/compose.html#{draft}")
    expect(page.locator("#status")).to_have_text("Ready.")
    return page


@pytest.mark.browser
def test_page_inference_and_remembered_settings(extension_browser, extension_server, monkeypatch):
    context, worker, client, _ = extension_browser
    monkeypatch.setattr(
        extension_server.workspaces,
        "local_repositories",
        lambda everything=False: {
            "repos": [
                {"repo": "other/project", "clone": "/other"},
                {"repo": "test/repo", "clone": "/fake/repo"},
            ]
        },
    )
    source = {
        "url": "https://github.com/test/repo/blob/main/file.py",
        "title": "file.py",
        "content": "def relevant_function(): pass",
    }
    page = draft_page(context, worker, client, source)
    expect(page.locator("#destination-summary")).to_contain_text("test/repo")
    expect(page.locator("#choices")).not_to_have_attribute("open", "")
    assert "Inspect this file" in page.locator("#task").input_value()
    page.locator("#choices summary").click()
    page.locator("#model").select_option("fixture")
    page.locator("#effort").select_option("high")
    page.get_by_role("button", name="Start task", exact=True).click()
    expect(page.locator("#workspace")).to_be_visible()
    payload = extension_server.workspaces.calls[-1]
    assert payload["clone"] == "/fake/repo" and payload["effort"] == "high"
    assert "relevant_function" in payload["task"]
    assert payload["name"].startswith("inspect-this-file-")
    next_page = draft_page(context, worker, client, source)
    expect(next_page.locator("#model")).to_have_value("fixture")
    expect(next_page.locator("#effort")).to_have_value("high")
    next_page.get_by_role("button", name="Start task", exact=True).click()
    expect(next_page.locator("#workspace")).to_be_visible()
    assert extension_server.workspaces.calls[-1]["name"] != payload["name"]


@pytest.mark.browser
@pytest.mark.parametrize("count", [1, 2])
def test_exact_pr_conversation_and_ambiguity(
    extension_browser, extension_server, monkeypatch, count
):
    context, worker, client, _ = extension_browser
    snapshot = extension_server.workspaces.snapshot()
    snapshot["prs"] = {"pr-one": {"matches": [{"workspace_id": "w1"}]}}
    monkeypatch.setattr(extension_server.workspaces, "snapshot", lambda: snapshot)
    monkeypatch.setattr(
        browser_extension,
        "herdr",
        lambda *args: {
            "agents": [
                {
                    "workspace_id": "w1",
                    "pane_id": f"p{i}",
                    "agent": "codex",
                    "terminal_title_stripped": f"Fix PR {i}",
                    "agent_session": {"value": f"s{i}"},
                }
                for i in range(count)
            ]
        },
    )
    monkeypatch.setattr(
        agent_messages,
        "agents",
        lambda _, machine=None: [{"pane": f"p{i}", "session": f"s{i}"} for i in range(count)],
    )
    sent = []
    monkeypatch.setattr(
        agent_messages,
        "prompt",
        lambda target, text, machine=None: sent.append((target["pane"], text)) or {"sent": True},
    )
    page = draft_page(
        context,
        worker,
        client,
        {"url": "https://github.com/test/repo/pull/1", "selection": "Please fix this assertion"},
    )
    expect(page.locator("#mode")).to_have_value("message")
    if count == 2:
        expect(page.locator("#choices")).to_have_attribute("open", "")
        expect(page.locator("#recipient")).to_have_value("")
        page.get_by_role("button", name="Send follow-up").click()
        assert not sent
        page.locator("#recipient").select_option("w1:p1:s1")
    else:
        expect(page.locator("#choices")).not_to_have_attribute("open", "")
        expect(page.locator("#destination-summary")).to_contain_text("Continue Fix PR 0")
    page.get_by_role("button", name="Send follow-up").click()
    expect(page.locator("#status")).to_contain_text("Follow-up sent")
    assert len(sent) == 1 and "Please fix this assertion" in sent[0][1]
    assert not extension_server.workspaces.calls


@pytest.mark.browser
def test_lost_response_retry_keeps_automatic_branch_and_request_id(
    extension_browser, extension_server
):
    context, worker, client, _ = extension_browser
    page = draft_page(
        context,
        worker,
        client,
        {"url": "https://github.com/test/repo", "selection": "Fix this error"},
    )
    delivered = []

    def drop_response(route):
        payload = route.request.post_data_json
        if payload.get("action") == "submit" and not delivered:
            delivered.append(payload)
            route.fetch()
            route.abort()
        else:
            route.continue_()

    page.route("**/api/extension", drop_response)
    page.get_by_role("button", name="Start task", exact=True).click()
    expect(page.locator("#status")).to_contain_text("delivery is uncertain")
    assert len(extension_server.workspaces.calls) == 1
    page.reload()
    expect(page.locator("#status")).to_have_text("Ready.")
    page.get_by_role("button", name="Start task", exact=True).click()
    expect(page.locator("#workspace")).to_be_visible()
    assert len(extension_server.workspaces.calls) == 1


REPORT = """# Review: test/repo#1

- PR: https://github.com/test/repo/pull/1
- Base: `main`
- Reviewed head: `abc1234def` (1 commit)
- Verdict: **changes needed**. The queue loses data on restart.

## Findings

### Major
**M1. <img src=x onerror="window.injected=true"> Data loss.**

### Nit
**N1. A typo.**
"""


class FakeOverview:
    """The Workspaces tab's rows: a checkout here linked to test/repo#1 and one on a worker
    linked to issue #3."""

    def snapshot(self, start=True):
        return {
            "workspaces": [
                {
                    "key": "/src/worktrees/repo/review-pr-1",
                    "name": "review-pr-1",
                    "branch": "review-pr-1",
                    "agent_status": "idle",
                    "workspace_url": "http://collie.example/space/w1",
                    "links": [{"url": "https://github.com/test/repo/pull/1", "kind": "pr"}],
                },
                {
                    "key": "m:mini:/src/worktrees/repo/issue-3",
                    "name": "issue-3",
                    "branch": "issue-3",
                    "machine_label": "mini",
                    "agent_status": "working",
                    "links": [{"url": "https://github.com/test/repo/issues/3", "kind": "issue"}],
                },
            ]
        }


@pytest.fixture
def reviewed(extension_server, monkeypatch, tmp_path):
    """test/repo#1 has a sweep review of an older head, a workspace and a linked checkout;
    issue #3 has a checkout on a worker."""
    out = tmp_path / "out"
    out.mkdir()
    (out / "2026-11-22-pr1.md").write_text(REPORT)
    settings = {"owner": "test", "default_repo": "repo", "out": str(out)}
    store = agent_reviews.Reviews(
        tmp_path / "reviews", settings=lambda: settings, user_home=tmp_path / "home"
    )
    store.record(
        {
            "source": "sweep",
            "repo": "test/repo",
            "number": 1,
            "head": "abc1234def",
            "date": "2026-11-22",
            "label": "2026-11-22",
            "report": str(out / "2026-11-22-pr1.md"),
        }
    )
    monkeypatch.setattr(extension_server, "reviews", store)
    monkeypatch.setattr(extension_server, "workspace_overview", FakeOverview())
    manager = extension_server.workspaces
    snapshot = manager.snapshot()
    snapshot["prs"] = {"pr-one": {"matches": [{"workspace_id": "w1"}]}}
    monkeypatch.setattr(manager, "snapshot", lambda: snapshot)
    targets = manager.targets()
    targets[0].update(kind="pr", head_sha="fff0000aaaa")
    monkeypatch.setattr(manager, "targets", lambda: targets)
    return extension_server


OPEN_WORKSPACE = "/?item=https%3A%2F%2Fgithub.com%2Ftest%2Frepo%2Fpull%2F1&view=1#prs"
ONE_CHECKOUT = "/?workspace=%2Fsrc%2Fworktrees%2Frepo%2Freview-pr-1#workspaces"


def test_pages_report_reviews_workspaces_and_where_the_dashboard_shows_them(reviewed):
    headers = pair(reviewed)
    one, two = "https://github.com/test/repo/pull/1", "https://github.com/test/repo/pull/2"
    issue = "https://github.com/test/repo/issues/3"
    code, value, _ = request(
        reviewed,
        "/api/extension",
        {"action": "pages", "urls": [one, two, issue, "https://example.com/test/repo/pull/1"]},
        headers,
    )
    assert code == 200 and set(value["pages"]) == {one, two, issue}
    page = value["pages"][one]
    assert page["dashboard"] == OPEN_WORKSPACE and page["workspace"] is True
    assert page["review"]["verdict"] == "changes needed"
    # GitHub's page scripts can read what the page holds: no local paths or sessions.
    assert not {"report", "checkout", "session"} & set(page["review"])
    assert page["review"]["counts"] == [
        {"severity": "Major", "count": 1, "tone": "danger"},
        {"severity": "Nit", "count": 1, "tone": "muted"},
    ]
    # The head the dashboard knows now, for "new commits since this review".
    assert page["review"]["head"] == "abc1234def" and page["head"] == "fff0000aaaa"
    assert page["workspaces"] == [
        {
            "name": "review-pr-1",
            "branch": "review-pr-1",
            "machine": None,
            "status": "idle",
            "collie": "http://collie.example/space/w1",
            "dashboard": ONE_CHECKOUT,
        }
    ]
    # An issue lists its checkouts, a worker's too, with no review.
    assert value["pages"][issue]["kind"] == "issue" and value["pages"][issue]["review"] is None
    assert [w["machine"] for w in value["pages"][issue]["workspaces"]] == ["mini"]
    # Neither in the overview nor reviewed nor checked out: nothing to show or open.
    assert value["pages"][two] == {
        "kind": "pr",
        "review": None,
        "head": None,
        "workspace": False,
        "workspaces": [],
        "dashboard": None,
    }
    for invalid in ({"action": "pages", "urls": one}, {"action": "pages", "urls": [one] * 101}):
        assert request(reviewed, "/api/extension", invalid, headers)[0] == 400
    # Only a paired extension may ask.
    assert request(reviewed, "/api/extension", {"action": "pages", "urls": [one]})[0] == 403


LIST_PAGE = """<!doctype html><title>Pull requests</title><main>
<div><a id="issue_1_link" href="/test/repo/pull/1">Fix tests</a></div>
<div><a id="issue_2_link" href="/test/repo/pull/2">Other</a></div>
</main>"""
PULL_PAGE = """<!doctype html><title>Fix tests</title><main>
<p>Follows up <a href="/test/repo/pull/2">#2</a>.</p>
<div id="partial-discussion-sidebar"><div>Reviewers</div></div>
</main>"""
ISSUE_PAGE = """<!doctype html><title>An issue</title><main><p>Broken.</p></main>"""


@pytest.mark.browser
def test_github_pages_show_reviews_workspaces_and_open_babysitter(extension_browser, reviewed):
    context, worker, _, base = extension_browser
    pages = {
        "/test/repo/pulls": LIST_PAGE,
        "/test/repo/pull/1": PULL_PAGE,
        "/test/repo/pull/2": PULL_PAGE,
        "/test/repo/issues/3": ISSUE_PAGE,
    }
    context.route(
        "https://github.com/**",
        lambda route: route.fulfill(
            body=pages.get(route.request.url.removeprefix("https://github.com"), "missing"),
            content_type="text/html",
        ),
    )
    # Off until Settings turns it on.
    page = context.new_page()
    page.goto("https://github.com/test/repo/pulls")
    page.wait_for_timeout(500)
    expect(page.locator(".babysit-chips")).to_have_count(0)
    worker.evaluate("chrome.storage.local.set({github: true})")
    worker.evaluate(
        """async () => {
          while (!(await chrome.scripting.getRegisteredContentScripts()).length)
            await new Promise((resolve) => setTimeout(resolve, 50));
        }"""
    )
    page.reload()
    chips = page.locator("#issue_1_link + .babysit-chips")
    # The PR has commits the review did not see.
    expect(chips.locator(".babysit-chip").first).to_have_text(
        "Review (old head): changes needed · 1 Major · 1 Nit"
    )
    expect(chips).to_contain_text("1 workspace")
    expect(chips.get_by_role("link", name="Open workspace")).to_have_attribute(
        "href", base + OPEN_WORKSPACE
    )
    # A pull request Babysitter knows nothing about is left alone.
    expect(page.locator(".babysit-chips")).to_have_count(1)

    page.goto("https://github.com/test/repo/pull/1")
    card = page.locator("#partial-discussion-sidebar > .babysit-card")
    expect(card).to_contain_text("Sweep review 2026-11-22, 2026-11-22 at abc1234de")
    expect(card).to_contain_text("The pull request has new commits since this review.")
    expect(card.locator(".babysit-findings li").first).to_have_text(
        'Major <img src=x onerror="window.injected=true"> Data loss'
    )
    expect(card).to_contain_text("The queue loses data on restart.")
    checkout = card.locator(".babysit-workspaces li")
    expect(checkout).to_contain_text("review-pr-1 idle Collie")
    expect(checkout.get_by_role("link", name="review-pr-1")).to_have_attribute(
        "href", base + ONE_CHECKOUT
    )
    expect(card.get_by_role("link", name="Open workspace")).to_have_attribute(
        "href", base + OPEN_WORKSPACE
    )
    # Links in the pull request's description are not list rows.
    expect(page.locator(".babysit-chips")).to_have_count(0)
    assert page.evaluate("window.injected") is None

    # A page Babysitter knows nothing about says so.
    page.goto("https://github.com/test/repo/pull/2")
    card = page.locator(".babysit-card")
    expect(card).to_contain_text("No agent review found.")
    expect(card).to_contain_text("Not in your dashboard's overview.")

    # An issue page lists its checkouts, a worker's too.
    page.goto("https://github.com/test/repo/issues/3")
    card = page.locator(".babysit-card")
    expect(card.locator(".babysit-workspaces li")).to_contain_text("issue-3 mini · working")
    expect(card).not_to_contain_text("agent review")

    worker.evaluate("chrome.storage.local.set({github: false})")
    worker.evaluate(
        """async () => {
          while ((await chrome.scripting.getRegisteredContentScripts()).length)
            await new Promise((resolve) => setTimeout(resolve, 50));
        }"""
    )
    page.reload()
    page.wait_for_timeout(500)
    expect(page.locator(".babysit-card")).to_have_count(0)


def handle(url, **extra):
    return {"action": "handle-issue", "request_id": "handle-1234567890ab", "url": url, **extra}


def test_handle_issue_runs_the_issue_task_with_the_extra_instructions(
    extension_server, monkeypatch
):
    headers = pair(extension_server)
    manager = extension_server.workspaces
    with_issue(manager, monkeypatch)
    request_ = handle(ISSUE, instructions="  Only touch the parser.  ", agent="claude")
    code, value, _ = request(extension_server, "/api/extension", request_, headers)
    assert code == 200 and value["operation"]["status"] == "complete"
    assert manager.calls == [
        {
            "id": "issue-three",
            "action": "handle",
            "agent": "claude",
            # No clone here yet: Handle clones it where the dashboard would.
            "destination": "/src/repo",
            "task": f"Investigate and resolve {ISSUE}. Read the issue and relevant code, "
            "implement the fix, and run the appropriate tests. Summarize the changes and "
            "validation.\n\nAdditional instructions:\nOnly touch the parser.",
        }
    ]
    # The same request again is answered without a second launch.
    assert request(extension_server, "/api/extension", request_, headers)[1] == value
    assert len(manager.calls) == 1


def test_handle_issue_outside_the_overview_starts_a_task_in_a_local_clone(
    extension_server, monkeypatch
):
    headers = pair(extension_server)
    manager = extension_server.workspaces
    extension_server.home.joinpath("dashboard-settings.json").write_text(
        json.dumps({"task_prompts": {"issue": "Fix {url}."}})
    )
    url = "https://github.com/test/repo/issues/9"
    code, value, _ = request(extension_server, "/api/extension", handle(url), headers)
    assert code == 200
    assert manager.calls == [
        {"repo": "test/repo", "clone": "/fake/repo", "name": "issue-9", "task": f"Fix {url}."}
    ]
    other = handle("https://github.com/elsewhere/repo/issues/1", request_id="handle-other-12345")
    code, value, _ = request(extension_server, "/api/extension", other, headers)
    assert "No clone of elsewhere/repo" in value["error"]
    for invalid in (
        handle("https://github.com/test/repo/pull/1", request_id="handle-pull-123456"),
        handle(url, request_id="handle-long-1234567", instructions="x" * 8001),
        handle(url, request_id="handle-field-123456", directory="/"),
    ):
        assert "error" in request(extension_server, "/api/extension", invalid, headers)[1]
    assert len(manager.calls) == 1


def review(url, **extra):
    return {"action": "review-pr", "request_id": "review-1234567890ab", "url": url, **extra}


def test_review_pr_runs_the_review_task_with_the_main_reviewer(extension_server, monkeypatch):
    headers = pair(extension_server)
    manager = extension_server.workspaces
    extension_server.home.joinpath("dashboard-settings.json").write_text(
        json.dumps({"task_prompts": {"prReview": "Review {url}."}, "reviewer": "claude"})
    )
    snapshot = manager.snapshot()
    snapshot["prs"] = {"pr-one": {"clones": ["/fake/repo"], "matches": []}}
    monkeypatch.setattr(manager, "snapshot", lambda: snapshot)
    url = "https://github.com/test/repo/pull/1"
    request_ = review(url, instructions="Focus on the parser.", clone="/fake/repo")
    code, value, _ = request(extension_server, "/api/extension", request_, headers)
    assert code == 200 and value["operation"]["status"] == "complete"
    assert manager.calls == [
        {
            "id": "pr-one",
            "action": "handle",
            "agent": "claude",
            "clone": "/fake/repo",
            "task": f"Review {url}.\n\nAdditional instructions:\nFocus on the parser.",
        }
    ]
    # An agent the page picks replaces the main reviewer, with its own settings.
    picked = review(url, request_id="review-codex-123456", agent="codex", effort="high")
    request(extension_server, "/api/extension", picked, headers)
    assert manager.calls[1]["agent"] == "codex" and manager.calls[1]["effort"] == "high"


def test_review_pr_outside_the_overview_starts_a_task_in_a_local_clone(extension_server):
    headers = pair(extension_server)
    manager = extension_server.workspaces
    url = "https://github.com/test/repo/pull/9"
    code, value, _ = request(extension_server, "/api/extension", review(url), headers)
    assert code == 200
    [call] = manager.calls
    assert {k: v for k, v in call.items() if k != "task"} == {
        "repo": "test/repo",
        "clone": "/fake/repo",
        "name": "review-pr-9",
        # The main reviewer when Settings names none.
        "agent": "codex",
    }
    assert call["task"].startswith(f"Review {url}. Read its description")
    for invalid in (
        review(ISSUE, request_id="review-issue-123456"),
        review(url, request_id="review-field-123456", directory="/"),
    ):
        assert "error" in request(extension_server, "/api/extension", invalid, headers)[1]
    assert len(manager.calls) == 1


def test_questions_are_asked_and_read_back_by_the_extension_that_asked(extension_server):
    headers = pair(extension_server)
    home = extension_server.home
    url = "https://github.com/test/repo/pull/1"
    code, value, _ = request(
        extension_server,
        "/api/extension",
        {"action": "ask", "url": url, "question": "Why?"},
        headers,
    )
    assert code == 400 and "supervisor is not running" in value["error"]
    (home / "heartbeat.json").write_text(json.dumps({"time": time.time(), "pid": 1}))
    code, value, _ = request(
        extension_server,
        "/api/extension",
        {"action": "ask", "url": url, "question": "Why?"},
        headers,
    )
    assert code == 200
    question_id = value["id"]
    saved = json.loads((home / "pr-questions" / f"{question_id}.json").read_text())
    # The supervisor reads the code from this machine's clone of the repository.
    assert saved["clone"] == "/fake/repo" and saved["client"] == CLIENT
    answer = {"action": "answer", "id": question_id}
    code, value, _ = request(extension_server, "/api/extension", answer, headers)
    assert code == 200 and value["status"] == "pending"
    cancel = {"action": "ask-cancel", "id": question_id}
    assert request(extension_server, "/api/extension", cancel, headers)[1] == {"cancelled": True}
    assert request(extension_server, "/api/extension", {**answer, "x": 1}, headers)[0] == 400
    assert request(extension_server, "/api/extension", answer)[0] == 403
    assert pr_questions.status(home, question_id, CLIENT)["status"] == "pending"


@pytest.fixture
def answering(extension_server):
    """The supervisor's question worker, answering in two steps with a fake Claude."""
    home = extension_server.home
    asked = []

    def answer(entry, update):
        asked.append(entry)
        update(force=True, activity=["Reading app.py"], answer="It prints")
        time.sleep(1)
        return f"It prints for **debugging** in `app.py`.\n\n- Asked: {entry['question']}"

    worker = pr_questions.Worker(home, answer=answer)
    stop = threading.Event()

    def loop():
        while not stop.is_set():
            (home / "heartbeat.json").write_text(json.dumps({"time": time.time(), "pid": 1}))
            worker.tick()
            time.sleep(0.1)

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    yield asked
    stop.set()
    thread.join(5)


@pytest.mark.browser
def test_github_pages_handle_issues_and_answer_questions(
    extension_browser, extension_server, monkeypatch, answering
):
    context, worker, _, _ = extension_browser
    manager = extension_server.workspaces
    with_issue(manager, monkeypatch, clones=["/fake/repo"])
    pages = {"/test/repo/issues/3": ISSUE_PAGE, "/test/repo/pull/1": PULL_PAGE}
    context.route(
        "https://github.com/**",
        lambda route: route.fulfill(
            body=pages.get(route.request.url.removeprefix("https://github.com"), "missing"),
            content_type="text/html",
        ),
    )
    worker.evaluate("chrome.storage.local.set({github: true})")
    worker.evaluate(
        """async () => {
          while (!(await chrome.scripting.getRegisteredContentScripts()).length)
            await new Promise((resolve) => setTimeout(resolve, 50));
        }"""
    )
    page = context.new_page()
    page.goto(ISSUE)
    card = page.locator(".babysit-card")
    card.get_by_label("Extra instructions (optional)").fill("Keep it small.")
    card.get_by_label("Agent").select_option("claude")
    card.get_by_role("button", name="Handle issue").click()
    expect(card.locator(".babysit-status")).to_have_text("Started Open in Collie")
    expect(card.get_by_role("link", name="Open in Collie")).to_have_attribute(
        "href", "http://127.0.0.1:8787/space/fake"
    )
    [call] = manager.calls
    assert call["id"] == "issue-three" and call["agent"] == "claude"
    assert call["task"].endswith("Additional instructions:\nKeep it small.")
    expect(card.get_by_label("Extra instructions (optional)")).to_have_value("")
    expect(card.get_by_label("Ask about this pull request")).to_have_count(0)
    expect(card.get_by_role("button", name="Review pull request")).to_have_count(0)

    page.goto("https://github.com/test/repo/pull/1")
    card = page.locator("#partial-discussion-sidebar > .babysit-card")
    # A review starts from the card, by the main reviewer unless the card picks one.
    expect(card.get_by_label("Agent")).to_have_value("")
    card.get_by_label("Extra instructions (optional)").fill("Mind the tests.")
    card.get_by_role("button", name="Review pull request").click()
    expect(card.locator(".babysit-status")).to_have_text("Started Open in Collie")
    call = manager.calls[1]
    assert call["id"] == "pr-one" and call["action"] == "handle" and call["agent"] == "codex"
    assert call["task"].startswith("Review https://github.com/test/repo/pull/1. Read")
    assert call["task"].endswith("Additional instructions:\nMind the tests.")
    expect(card.get_by_label("Extra instructions (optional)")).to_have_value("")
    box = card.get_by_label("Ask about this pull request")
    box.fill("Why print?")
    box.press("Enter")
    turn = card.locator(".babysit-turn")
    expect(turn.locator(".babysit-question")).to_have_text("Why print?")
    # The answer streams in, with what the agent is doing.
    expect(turn.locator(".babysit-answer")).to_have_text("It prints")
    expect(turn.locator(".babysit-activity")).to_have_text("Reading app.py")
    expect(card.get_by_role("button", name="Stop")).to_be_visible()
    answer = turn.locator(".babysit-answer")
    expect(answer.locator("strong")).to_have_text("debugging")
    expect(answer.locator("code")).to_have_text("app.py")
    expect(answer.locator("li")).to_have_text("Asked: Why print?")
    expect(turn.locator(".babysit-activity")).to_have_count(0)
    expect(card.get_by_role("button", name="Stop")).to_be_hidden()
    # A follow-up carries the conversation so far.
    box.fill("And then?")
    card.get_by_role("button", name="Ask").click()
    expect(card.locator(".babysit-turn")).to_have_count(2)
    expect(card.locator(".babysit-turn").nth(1).locator("li")).to_have_text("Asked: And then?")
    first = "It prints for **debugging** in `app.py`.\n\n- Asked: Why print?"
    assert answering[1]["history"] == [{"question": "Why print?", "answer": first}]
    card.get_by_role("button", name="Clear").click()
    expect(card.locator(".babysit-turn")).to_have_count(0)


@pytest.mark.browser
def test_the_extension_downloads_from_the_dashboard_and_offers_its_updates(
    extension_browser, extension_server, monkeypatch
):
    context, worker, client, base = extension_browser
    shipped = browser_extension.shipped()
    # The manifest's key fixes the ID Chrome gives the unpacked folder, wherever it is.
    assert client == shipped["id"]
    page = context.new_page()
    page.goto(f"{base}/extension.html")
    expect(page.locator("#extension-id")).to_have_value(shipped["id"])
    expect(page.locator("#shipped-id")).to_have_text(shipped["id"])
    expect(page.locator("#shipped-version")).to_have_text(f"(version {shipped['version']})")
    with page.expect_download() as download:
        page.get_by_role("link", name="Download the extension").click()
    assert download.value.suggested_filename == f"babysitter-extension-{shipped['version']}.zip"

    page.goto(f"chrome-extension://{client}/options.html")
    expect(page.locator("#version")).to_have_text(shipped["version"])
    expect(page.locator("#update")).to_be_hidden()
    monkeypatch.setattr(browser_extension, "shipped", lambda: {**shipped, "version": "99.0.0"})
    page.reload()
    expect(page.locator("#update")).to_be_visible()
    expect(page.locator("#update-version")).to_have_text("99.0.0")
    expect(page.locator("#update-link")).to_have_attribute("href", f"{base}/extension.zip")

    page.goto(f"chrome-extension://{client}/compose.html")
    expect(page.locator("#update")).to_contain_text("Version 99.0.0 of this extension")

    context.route(
        "https://github.com/**",
        lambda route: route.fulfill(body=ISSUE_PAGE, content_type="text/html"),
    )
    worker.evaluate("chrome.storage.local.set({github: true})")
    worker.evaluate(
        """async () => {
          while (!(await chrome.scripting.getRegisteredContentScripts()).length)
            await new Promise((resolve) => setTimeout(resolve, 50));
        }"""
    )
    page.goto(ISSUE)
    card = page.locator(".babysit-card")
    expect(card).to_contain_text("Extension 99.0.0 is on your dashboard")
    expect(card.get_by_role("link", name="download it")).to_have_attribute(
        "href", f"{base}/extension.zip"
    )
