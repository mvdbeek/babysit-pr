"""Authenticated extension requests use isolated state and fake launchers."""

import copy
import http.client
import json
import re
import shutil
import threading
from pathlib import Path
from types import SimpleNamespace

import agent_messages
import browser_extension
import dashboard
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

    action = new_task


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
    monkeypatch.setattr(agent_messages, "agents", lambda _: [{"pane": "p1", "session": "current"}])
    monkeypatch.setattr(
        agent_messages,
        "prompt",
        lambda target, text: sent.append((target["pane"], text)) or {"sent": True},
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


@pytest.fixture
def extension_browser(playwright, tmp_path, extension_server):
    extension_path = tmp_path / "extension"
    shutil.copytree(Path(__file__).resolve().parents[1] / "extension", extension_path)
    manifest_path = extension_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    # Pregrant only the temporary server for automation; shipped permissions remain optional.
    manifest["host_permissions"] = ["http://127.0.0.1/*"]
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
        worker = (
            context.service_workers[0]
            if context.service_workers
            else context.wait_for_event("serviceworker")
        )
        client = worker.url.split("/")[2]
        base = f"http://127.0.0.1:{extension_server.server_port}"
        page = context.new_page()
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
    monkeypatch.setattr(agent_messages, "agents", lambda _: [{"pane": "p1", "session": "session1"}])
    messages = []
    monkeypatch.setattr(
        agent_messages,
        "prompt",
        lambda target, text: messages.append((target["pane"], text)) or {"sent": True},
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
        lambda _: [{"pane": f"p{i}", "session": f"s{i}"} for i in range(count)],
    )
    sent = []
    monkeypatch.setattr(
        agent_messages,
        "prompt",
        lambda target, text: sent.append((target["pane"], text)) or {"sent": True},
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
