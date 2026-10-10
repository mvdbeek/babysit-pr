"""Shared integration fixtures, explicitly re-exported for pytest discovery."""

import pytest
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
