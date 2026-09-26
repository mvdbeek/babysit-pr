"""Handle for Sentry issue groups: temporary clones, fake herdr/agents, no live workspaces."""

import json
import time
from pathlib import Path

import pr_workspaces as pw
import pytest
from test_pr_workspaces import finish, helper_calls, local  # noqa: F401 (fixture)

URL = "https://sentry.example.org/organizations/galaxy/issues/1/"


class FakeSentry:
    def __init__(self, home):
        self.home = home
        self.mcp_calls = 0
        self.fail = None

    def targets(self):
        return [
            {
                "id": "sentry:abc123",
                "kind": "sentry",
                "repo": "base/repo",
                "title": "ValueError: bad int",
                "short_id": "GALAXY-MAIN-1",
                "url": URL,
                "culprit": "galaxy.tools in from_json",
                "projects": [
                    {
                        "slug": "galaxy-main",
                        "short_id": "GALAXY-MAIN-1",
                        "count": 9,
                        "users": 2,
                        "permalink": URL,
                    },
                    {
                        "slug": "usegalaxy-eu-main",
                        "short_id": "EU-7",
                        "count": 1,
                        "users": 0,
                        "permalink": None,
                    },
                ],
                "count": 10,
                "users": 2,
                "first_seen": "2026-09-01T00:00:00Z",
                "last_seen": "2026-09-25T00:00:00Z",
            }
        ]

    def handle_mcp_config(self):
        self.mcp_calls += 1
        if self.fail:
            raise ValueError(self.fail)
        path = Path(self.home) / "mcp.json"
        path.write_text("{}")
        return path


@pytest.fixture
def sentry(local, tmp_path):  # noqa: F811
    manager = local[0]
    manager.sentry = FakeSentry(tmp_path)
    return local


def test_sentry_targets_are_described_and_only_handle_creates(sentry):
    manager, *_ = sentry
    manager.scan()  # The snapshot serves the cached inventory.
    snapshot = manager.snapshot()
    info = snapshot["sentry"]["sentry:abc123"]
    assert info["clones"] == [str(manager.src / "repo")] and not info["matches"]
    assert pw.sentry_branch(manager.sentry.targets()[0]) == "sentry-galaxy-main-1"
    with pytest.raises(ValueError, match="Use Handle"):
        manager.action({"id": "sentry:abc123", "action": "create", "task": "Fix"})


def test_handle_claude_gets_private_mcp_config_and_sentry_brief(sentry):
    manager, _, git, state, _ = sentry
    calls = helper_calls(manager)
    request = {"id": "sentry:abc123", "action": "handle", "agent": "claude", "task": "Fix it"}
    manager.action(request)
    op = finish(manager, "sentry:abc123")
    assert op["status"] == "complete", op
    assert op["branch"] == "sentry-galaxy-main-1"
    assert git("rev-parse", "HEAD", cwd=op["path"]) == git("rev-parse", "main")
    prompt = manager.home / "workspace-prompts" / op["id"]
    mcp = str(manager.sentry.home / "mcp.json")
    assert calls == [
        [
            "wt",
            "--claude",
            "--no-focus",
            "--name",
            "sentry-galaxy-main-1",
            "--label",
            "sentry-repo-GALAXY-MAIN-1",
            "--repo-path",
            str(manager.src / "repo"),
            "--worktree-root",
            str(manager.src / "worktrees/repo"),
            "--prompt-file",
            str(prompt),
            "--agent-arg",
            "--mcp-config",
            "--agent-arg",
            mcp,
            "--agent-arg",
            "--disallowedTools",
            "--agent-arg",
            "mcp__sentry__execute_sentry_tool",
            "--agent-arg",
            "mcp__sentry__search_sentry_tools",
        ]
    ]
    agent = json.loads(state.read_text())["agents"][0]
    # `--` keeps the variadic --disallowedTools from consuming the task.
    assert agent["argv"][:-1] == [
        "--mcp-config",
        mcp,
        "--disallowedTools",
        "mcp__sentry__execute_sentry_tool",
        "mcp__sentry__search_sentry_tools",
        "--",
    ]
    task = agent["task"]
    assert task.startswith("Fix it\n\nSentry issue: " + URL)
    assert "- galaxy-main: GALAXY-MAIN-1 (9 events, 2 users) " + URL in task
    assert "- usegalaxy-eu-main: EU-7 (1 events, 0 users)\n" in task
    assert "untrusted input" in task and "available as `sentry` (read-only)" in task
    # The checkout links back to the Sentry group by branch name.
    manager.scan()
    info = manager.snapshot()["sentry"]["sentry:abc123"]
    assert [m["path"] for m in info["matches"]] == [op["path"]]
    # Handling again starts a separate workspace with a suffixed branch.
    manager.action({**request, "agent": "codex"})
    second = finish(manager, "sentry:abc123")
    assert second["status"] == "complete", second
    assert second["branch"] == "sentry-galaxy-main-1-2"
    assert "--agent-arg" not in calls[-1]
    codex_task = json.loads(state.read_text())["agents"][1]["task"]
    assert "Use your configured Sentry MCP server" in codex_task
    assert manager.sentry.mcp_calls == 1


def test_handle_without_mcp_config_still_starts_with_facts(sentry):
    manager, _, _, state, _ = sentry
    manager.sentry.fail = "Sentry token is missing or malformed"
    manager.action({"id": "sentry:abc123", "action": "handle", "agent": "claude", "task": "Fix"})
    assert finish(manager, "sentry:abc123")["status"] == "complete"
    agent = json.loads(state.read_text())["agents"][0]
    assert "--mcp-config" not in agent["argv"]
    assert "The Sentry MCP server is unavailable (Sentry token is missing" in agent["task"]


def test_sentry_target_rejects_bad_repository_and_short_id(sentry):
    manager, *_ = sentry
    target = manager.sentry.targets()[0]
    manager.sentry.targets = lambda: [{**target, "repo": "../x"}]
    with pytest.raises(ValueError, match="Invalid repository"):
        manager.target("sentry:abc123")
    # A malformed target is dropped rather than breaking every other workspace.
    manager.sentry.targets = lambda: [{**target, "short_id": "!!"}]
    with pytest.raises(ValueError, match="Unknown PR or issue"):
        manager.target("sentry:abc123")
    assert "PR_one" in manager.snapshot()["prs"]

    def broken():
        raise RuntimeError("experiment exploded")

    manager.sentry.targets = broken
    assert manager.snapshot()["sentry"] == {} and "PR_one" in manager.snapshot()["prs"]


def test_sentry_state_reports_handle_progress_without_scanning(sentry):
    manager, _, _, _, _ = sentry
    assert manager.sentry_state() == {}
    manager.action({"id": "sentry:abc123", "action": "handle", "agent": "codex", "task": "Fix"})
    op = finish(manager, "sentry:abc123")
    manager.scan = None  # sentry_state must not rescan.
    state = manager.sentry_state()["sentry:abc123"]
    assert state["status"] == "complete" and state["agent"] == "codex"
    assert state["path"] == op["path"] and state["workspace_url"].startswith(pw.COLLIE_URL)
    # A closed herdr workspace keeps the checkout but loses its link.
    manager.inventory = {**manager.inventory, "workspaces": [], "synced_at": time.time()}
    state = manager.sentry_state()["sentry:abc123"]
    assert state["path"] == op["path"] and state["workspace_url"] is None
    # A removed worktree no longer counts as ongoing work.
    import shutil

    shutil.rmtree(op["path"])
    assert manager.sentry_state()["sentry:abc123"]["path"] is None


def test_attach_handling_is_best_effort():
    import dashboard

    class Workspaces:
        def __init__(self, value):
            self.value = value

        def sentry_state(self):
            if isinstance(self.value, Exception):
                raise self.value
            return self.value

    value = {"groups": [{"key": "a"}, {"key": "b"}]}
    dashboard.attach_handling(value, Workspaces({"sentry:a": {"status": "running"}}))
    assert [g.get("handling") for g in value["groups"]] == [{"status": "running"}, None]
    untouched = {"groups": [{"key": "a"}]}
    dashboard.attach_handling(untouched, Workspaces(RuntimeError("db locked")))
    dashboard.attach_handling(untouched, None)
    assert untouched == {"groups": [{"key": "a"}]}
