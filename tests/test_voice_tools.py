"""The voice assistant's read-only tools, against a fake dashboard on loopback."""

import json
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest
import voice_tools as vt

PR = {
    "id": "PR_1",
    "number": 12,
    "title": "Fix the crash",
    "url": "https://github.com/o/r/pull/12",
    "repo": "o/r",
    "author": "me",
    "state": "OPEN",
    "draft": False,
    "head_branch": "fix",
    "review_decision": "CHANGES_REQUESTED",
    "unresolved_threads": 1,
    "review_requests": [],
    "roles": ["author"],
    "updated_at": "2026-10-27T10:00:00Z",
    "ci": "FAILURE",
}
CHECKS = {
    "sha": "abc",
    "state": "FAILURE",
    "checks": [
        {"id": "C1", "name": "lint", "state": "FAILURE", "bucket": "fail", "has_details": True},
        {"id": "C2", "name": "test (3.12)", "state": "IN_PROGRESS", "bucket": "pending"},
        {"id": "C3", "name": "build", "state": "SUCCESS", "bucket": "pass"},
    ],
}


class Dashboard:
    """Answers the routes the tools read; `pending` counts answers still loading."""

    def __init__(self):
        self.requests = []
        self.pending = {"/api/pr-ci": 1}
        self.routes = {
            "/api/attention": {
                "items": [
                    {
                        "key": "https://github.com/o/r/pull/12",
                        "kind": "pr",
                        "repo": "o/r",
                        "number": 12,
                        "title": "Fix the crash",
                        "url": "https://github.com/o/r/pull/12",
                        "group": "answer",
                        "reasons": [{"text": "The agent asks a question", "code": "agent_blocked"}],
                        "since": time.time() - 7200,
                        "page": "workspaces",
                        "agent": {"status": "blocked"},
                        "watch": None,
                        "workspace_id": "w1",
                    }
                ],
                "waiting": [{"key": "x"}],
                "groups": [{"key": "answer", "label": "Answer an agent"}],
                "sources": {"prs": {"error": None}, "cron": {"error": "cron is down"}},
            },
            "/api/prs": {"prs": [PR]},
            "/api/issues": {
                "issues": [
                    {
                        "url": "https://github.com/o/r/issues/3",
                        "repo": "o/r",
                        "number": 3,
                        "title": "It crashes",
                        "labels": [{"name": "bug", "color": "red"}],
                        "assignees": ["me"],
                        "comments": 4,
                        "linked_prs": [],
                    }
                ]
            },
        }

    def answer(self, path, query):
        if path == "/api/pr-ci":
            if self.pending.get(path):
                self.pending[path] -= 1
                return 200, {"value": None, "refreshing": True}
            if query.get("check") == ["C1"]:
                return 200, {
                    "value": {
                        "annotations": [
                            {"path": "a.py", "line": 3, "title": "F401", "message": "unused"}
                        ],
                        "summary": "1 error",
                    }
                }
            return 200, {"value": CHECKS, "refreshing": False}
        if path == "/api/pr-ci-log":
            if "page" in query:
                page = int(query["page"][0])
                return 200, {"page": page, "pages": 2, "text": f"log page {page} ERROR at end"}
            return 200, {"state": "ready", "refreshing": False}
        if path == "/api/pr-reviews":
            return 200, {
                "value": {
                    "reviews": [{"author": "rev", "state": "CHANGES_REQUESTED", "body": "Nope"}],
                    "threads": [
                        {"path": "a.py", "line": 3, "comments": [{"author": "rev", "body": "Why?"}]}
                    ],
                }
            }
        if path == "/api/workspace-transcript":
            if query.get("workspace") == ["gone"]:
                return 400, {"error": "That workspace is closed"}
            return 200, {
                "session": {"agent": "claude"},
                "entries": [{"role": "user", "kind": "prompt", "text": "fix it"}]
                + [{"role": "assistant", "text": f"step {n}"} for n in range(40)],
            }
        if path == "/api/workspace-diff":
            return 200, {
                "base": "main",
                "commits": [{"subject": "Fix the crash"}],
                "added": 2,
                "removed": 1,
                "files": [
                    {
                        "path": "a.py",
                        "status": "modified",
                        "added": 2,
                        "removed": 1,
                        "lines": ["+x"],
                    }
                ],
                "untracked": [],
            }
        if path in self.routes:
            return 200, self.routes[path]
        return 404, {"error": "Not found"}


@pytest.fixture
def dashboard(monkeypatch):
    fake = Dashboard()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            route = urlsplit(self.path)
            query = parse_qs(route.query)
            fake.requests.append((route.path, query, self.headers.get("Host")))
            code, body = fake.answer(route.path, query)
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}"
    monkeypatch.setenv("BABYSIT_DASHBOARD_URL", url)
    monkeypatch.setattr(vt, "POLL_SECONDS", 0.01)
    fake.url = url
    yield fake
    server.shutdown()


def call(name, **args):
    result = vt.call(name, args)
    text = result["content"][0]["text"]
    return (text if result["isError"] else json.loads(text)), result["isError"]


def test_needs_you_lists_items_in_order_with_their_agents(dashboard):
    value, error = call("needs_you")
    assert not error
    assert value["items"] == [
        {
            "kind": "pr",
            "repo": "o/r",
            "number": 12,
            "title": "Fix the crash",
            "url": "https://github.com/o/r/pull/12",
            "key": "https://github.com/o/r/pull/12",
            "group": "Answer an agent",
            "reasons": ["The agent asks a question"],
            "since": "2h ago",
            "page": "workspaces",
            "agent": "blocked",
            "watch": None,
            "watch_status": None,
            "workspace_id": "w1",
        }
    ]
    assert value["set_aside"] == 1
    assert value["sources_unavailable"] == ["cron"]
    # Read as the dashboard's own pages read it: on loopback, under its own host name.
    assert dashboard.requests[0][2] == dashboard.url.removeprefix("http://")


def test_a_pull_request_waits_for_its_checks_and_names_those_not_passing(dashboard):
    value, error = call("pull_request", url="https://github.com/O/R/pull/12/")
    assert not error
    assert value["review_decision"] == "CHANGES_REQUESTED"
    assert value["check_counts"] == {"fail": 1, "pending": 1, "pass": 1}
    assert [c["name"] for c in value["not_passing"]] == ["lint", "test (3.12)"]
    assert [path for path, _, _ in dashboard.requests].count("/api/pr-ci") == 2
    value, error = call("pull_request", url="https://github.com/o/r/pull/99")
    assert error and "not in the dashboard" in value


def test_a_ci_failure_has_annotations_and_the_end_of_its_log(dashboard):
    value, error = call("ci_failure", url=PR["url"], check="Lint")
    assert not error
    assert value["annotations"] == [
        {"path": "a.py", "line": 3, "title": "F401", "message": "unused"}
    ]
    # The last page, where a failure's reason usually is.
    assert value["log_tail"] == "log page 2 ERROR at end"
    value, _ = call("ci_failure", url=PR["url"], check="build")
    assert "No failure details" in value["note"]
    value, error = call("ci_failure", url=PR["url"], check="deploy")
    assert error and "No check of that name" in value


def test_details_that_keep_loading_give_up_with_a_reason(dashboard, monkeypatch):
    monkeypatch.setattr(vt, "WAIT_SECONDS", 0.05)
    dashboard.pending["/api/pr-ci"] = 10**6
    value, error = call("pull_request", url=PR["url"])
    assert not error
    assert value["checks_error"] == "Still loading from GitHub; try again in a little while"


def test_reviews_issues_transcripts_and_changes(dashboard):
    value, _ = call("reviews", url=PR["url"])
    assert value["reviews"] == [{"author": "rev", "state": "CHANGES_REQUESTED", "body": "Nope"}]
    assert value["unresolved_threads"][0]["comments"] == [{"author": "rev", "body": "Why?"}]
    value, _ = call("issue", url="https://github.com/o/r/issues/3")
    assert value["labels"] == ["bug"] and value["comments"] == 4
    value, _ = call("agent_transcript", workspace="w1")
    assert value["agent"] == "claude"
    assert len(value["entries"]) == vt.TRANSCRIPT_ENTRIES
    assert value["entries"][-1] == {"role": "assistant", "kind": None, "text": "step 39"}
    assert value["earlier_entries"] == 41 - vt.TRANSCRIPT_ENTRIES
    value, error = call("agent_transcript", workspace="gone")
    assert error and value == "That workspace is closed"
    value, error = call("agent_transcript", workspace="w1", watch="j1")
    assert error and value == "Give either a workspace or a watch"
    value, _ = call("changes", watch="j1")
    assert value["files"] == [
        {"path": "a.py", "status": "modified", "added": 2, "removed": 1, "patch": "+x"}
    ]
    assert ("/api/workspace-diff", {"watch": ["j1"]}) in [(p, q) for p, q, _ in dashboard.requests]


def overview_row(name, workspace=None, status="active", number=None, updated=1.0):
    return {
        "name": name,
        "label": None,
        "repo": "o/r",
        "branch": name,
        "machine_label": None,
        "workspace_ids": [workspace] if workspace else [],
        "agents": [{"agent": "claude", "status": "done", "pane": "w:p"}] if workspace else [],
        "status": status,
        "blockers": [{"kind": "unpushed", "text": "1 commit(s) only on this branch"}],
        "links": [
            {"kind": "pr", "repo": "o/r", "number": number, "title": "Fix it", "state": "open"}
        ]
        if number
        else [],
        "updated_at": updated,
    }


def test_workspaces_finds_any_checkout_whatever_the_page_filters(dashboard):
    dashboard.routes["/api/workspace-overview"] = {
        "enabled": True,
        "loading": False,
        "error": None,
        "workspaces": [
            overview_row("review-pr-24371", status="blocked", number=24371, updated=5.0),
            overview_row("review-pr-24371-again", workspace="w7", number=24371, updated=1.0),
            overview_row("other", workspace="w8", status="unlinked"),
        ],
    }
    value, error = call("workspaces", query="24371")
    assert not error
    assert (value["matched"], value["of"]) == (2, 3)
    # The one with an agent comes first, though the closed checkout changed later.
    first, second = value["workspaces"]
    assert (first["workspace_id"], first["agents"]) == (
        "w7",
        [{"agent": "claude", "status": "done"}],
    )
    assert second["workspace_id"] is None and second["status"] == "blocked"
    assert second["blockers"] == ["1 commit(s) only on this branch"]
    assert second["links"][0]["number"] == 24371
    value, _ = call("workspaces")
    assert value["matched"] == 3
    dashboard.routes["/api/workspace-overview"] = {"enabled": False, "workspaces": []}
    value, error = call("workspaces", query="x")
    assert error and value == "The Workspaces page is off, so checkouts are not listed"


def test_the_assistant_may_use_every_tool_the_server_offers():
    import voice_assistant

    assert sorted(voice_assistant.TOOLS) == sorted(tool["name"] for tool in vt.TOOLS)
    assert set(voice_assistant.PROGRESS) == set(voice_assistant.TOOLS)


def test_bad_calls_answer_errors_not_exceptions(dashboard):
    value, error = call("pull_request")
    assert error and value == "Missing argument 'url'"
    value, error = call("ci_failure", url=PR["url"], check=["lint"])
    assert error and value == "Missing argument 'check'"
    result = vt.call("delete_everything", {})
    assert result["isError"]
    # The dashboard answering something unexpected is an error for this call alone.
    dashboard.routes["/api/attention"] = {"items": [{"reasons": ["not a dict"]}]}
    value, error = call("needs_you")
    assert error and value.startswith("Could not read that: AttributeError")


def test_the_server_speaks_mcp_over_stdio(dashboard):
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "X"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        "not json",
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "issue", "arguments": {"url": "https://github.com/o/r/issues/3"}},
        },
        {"jsonrpc": "2.0", "id": 4, "method": "resources/list"},
    ]
    lines = "".join((m if isinstance(m, str) else json.dumps(m)) + "\n" for m in messages)
    done = subprocess.run(
        [sys.executable, str(vt.__file__)],
        input=lines,
        capture_output=True,
        text=True,
        timeout=30,
        env={"BABYSIT_DASHBOARD_URL": dashboard.url, "PATH": "/usr/bin:/bin"},
    )
    answers = [json.loads(line) for line in done.stdout.splitlines()]
    assert [a["id"] for a in answers] == [1, 2, 3, 4]
    assert answers[0]["result"]["protocolVersion"] == "X"
    assert answers[0]["result"]["capabilities"] == {"tools": {}}
    assert [t["name"] for t in answers[1]["result"]["tools"]] == list(vt.HANDLERS)
    assert json.loads(answers[2]["result"]["content"][0]["text"])["title"] == "It crashes"
    assert answers[3]["error"]["code"] == -32601
