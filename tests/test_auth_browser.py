"""Real Chromium signing in to an enforcing dashboard through the fake identity provider.

The dashboard and provider run on free loopback ports with temporary homes; nothing
here contacts a real identity provider or the live dashboard.
"""

import threading

import dashboard
import pytest
from dashboard_auth import Auth, OidcConfig
from oidc_provider import CLIENT_ID, CLIENT_SECRET, Provider
from playwright.sync_api import expect

pytestmark = pytest.mark.browser


@pytest.fixture
def signed_site(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    with Provider() as provider:
        server = dashboard.DashboardServer(state, 0)
        port = server.server_port
        config = OidcConfig(
            provider.issuer,
            CLIENT_ID,
            CLIENT_SECRET,
            {f"127.0.0.1:{port}": f"http://127.0.0.1:{port}/auth/callback"},
        )
        server.auth = Auth(tmp_path / "auth", "enforce", config, log=lambda line: None)
        thread = threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{port}", server
        finally:
            server.shutdown()
            thread.join(5)
            server.server_close()


def test_sign_in_and_out_through_the_identity_provider(page, signed_site):
    url, server = signed_site
    page.goto(url + "/")
    banner = page.locator("#auth-banner")
    chip = page.locator("#auth-chip")
    expect(banner).to_contain_text("signed out")
    expect(chip).to_have_text("Sign in")

    chip.click()
    expect(page).to_have_url(url + "/")
    expect(chip).to_have_text("alice")
    expect(chip).to_have_attribute("title", "Signed in as alice (oidc). Click to sign out.")
    expect(banner).to_be_hidden()
    (cookie,) = [c for c in page.context.cookies() if c["name"] == "babysit_session"]
    assert cookie["httpOnly"] and cookie["sameSite"] == "Strict"
    expect(page.locator("#health")).not_to_have_text("Connecting")

    chip.click()
    expect(page).to_have_url(url + "/")
    expect(chip).to_have_text("Sign in")
    assert server.auth.store.sessions() == []


def test_break_glass_link_signs_in_once(page, signed_site):
    url, server = signed_site
    token = server.auth.store.create_link()
    page.goto(f"{url}/auth/break-glass#t={token}")
    expect(page).to_have_url(url + "/")
    expect(page.locator("#auth-chip")).to_have_text("break-glass")

    page.goto(f"{url}/auth/break-glass#t={token}")
    expect(page.locator("#break-glass-status")).to_contain_text("already used")
    # The token never stays in the address bar.
    expect(page).to_have_url(f"{url}/auth/break-glass")
