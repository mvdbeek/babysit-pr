"""Shared integration fixtures, explicitly re-exported for pytest discovery."""

import pytest
from test_dashboard_cancel import server as server
from test_pr_supervisor import harness as harness


@pytest.fixture(autouse=True)
def github_token_from_env(monkeypatch):
    """No test opens the system keyring: gh gets its token from the environment."""
    monkeypatch.setenv("GH_TOKEN", "gho_fixture_token_0000000000000000000000")
