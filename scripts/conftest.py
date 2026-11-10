"""Shared integration fixtures, explicitly re-exported for pytest discovery."""

import pytest
import sandbox
import workspace_viewer
from test_dashboard_cancel import server as server
from test_pr_supervisor import harness as harness


@pytest.fixture(autouse=True)
def github_token_from_env(monkeypatch):
    """No test opens the system keyring: gh gets its token from the environment."""
    monkeypatch.setenv("GH_TOKEN", "gho_fixture_token_0000000000000000000000")


@pytest.fixture(autouse=True)
def no_claude_trust(monkeypatch):
    """Launches never mark folders trusted in the user's real Claude config; the trust
    tests turn it back on against a fake home."""
    monkeypatch.setenv("BABYSIT_CLAUDE_TRUST", "0")


@pytest.fixture(autouse=True)
def safehouse_sandbox(monkeypatch):
    """In-process launch lines are Safehouse's on every platform; the bwrap tests name
    their backend. Child processes follow their platform."""
    monkeypatch.setattr(sandbox, "DEFAULT", "safehouse")


@pytest.fixture(autouse=True)
def private_transcript_store(monkeypatch, tmp_path_factory):
    """Each test opens its own transcript store, and caches probe bytecode privately."""
    store = tmp_path_factory.mktemp("transcripts") / "transcripts.sqlite"
    monkeypatch.setattr(workspace_viewer, "_store_path", store)
    monkeypatch.setattr(workspace_viewer, "_store", None)
    monkeypatch.setenv("BABYSIT_PROBE_CACHE", str(tmp_path_factory.mktemp("probe-cache")))
