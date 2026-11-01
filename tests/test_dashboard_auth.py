"""Dashboard sign-in: OIDC against an in-process provider, sessions, keys, break-glass
links, report-only mode and enforce mode.

Every dashboard runs on a free loopback port with temporary state and auth homes, and
every identity provider is the fake in ``oidc_provider``: nothing here contacts a real
identity provider or the live dashboard.
"""

import http.client
import json
import os
import stat
import sys
import threading
import time
from urllib.parse import parse_qs, quote, urlsplit

import dashboard
import dashboard_auth
import pr_supervisor
import pytest
import sandbox
from dashboard_auth import SESSION_ABSOLUTE, SESSION_IDLE, Auth, AuthError, OidcConfig
from oidc_provider import CLIENT_ID, CLIENT_SECRET, Provider

PUBLIC_HOST = "babysitter.example.ts.net"


class Clock:
    def __init__(self):
        self.now = time.time()

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def provider():
    with Provider() as value:
        yield value


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def site(tmp_path, provider, clock):
    """``start(mode, oidc=True, allowed_hosts=())``: a dashboard with auth, and its log."""
    servers = []
    state = tmp_path / "state"
    state.mkdir()

    def start(mode="report", oidc=True, allowed_hosts=()):
        server = dashboard.DashboardServer(state, 0, allowed_hosts=allowed_hosts)
        port = server.server_port
        config = (
            OidcConfig(
                provider.issuer,
                CLIENT_ID,
                CLIENT_SECRET,
                {
                    f"127.0.0.1:{port}": f"http://127.0.0.1:{port}/auth/callback",
                    **{host: f"https://{host}/auth/callback" for host in allowed_hosts},
                },
            )
            if oidc
            else None
        )
        server.log = []
        server.auth = Auth(tmp_path / "auth", mode, config, clock=clock, log=server.log.append)
        thread = threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True)
        thread.start()
        servers.append((server, thread))
        return server

    yield start
    for server, thread in servers:
        server.shutdown()
        thread.join(5)
        server.server_close()


def call(server, path, method="GET", headers=None, body=None, host=None):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
    try:
        conn.request(
            method,
            path,
            body,
            {"Host": host or f"127.0.0.1:{server.server_port}", **(headers or {})},
        )
        response = conn.getresponse()
        return response.status, response.getheaders(), response.read()
    finally:
        conn.close()


def header(headers, name):
    return next((value for key, value in headers if key.lower() == name.lower()), None)


def set_cookies(headers):
    """Each Set-Cookie by name: (value, its attributes)."""
    found = {}
    for key, value in headers:
        if key.lower() == "set-cookie":
            first, *attributes = [part.strip() for part in value.split(";")]
            name, _, cookie = first.partition("=")
            found[name] = (cookie, attributes)
    return found


def follow_provider(provider, url):
    parts = urlsplit(url)
    assert parts.netloc == f"127.0.0.1:{provider.server_port}"
    conn = http.client.HTTPConnection("127.0.0.1", provider.server_port, timeout=10)
    try:
        conn.request("GET", f"{parts.path}?{parts.query}")
        response = conn.getresponse()
        response.read()
        assert response.status == 302
        return response.getheader("Location")
    finally:
        conn.close()


def start_login(server, provider, next_path="/", host=None):
    """The callback path the provider sends the browser to, and the bound state."""
    status, headers, _ = call(server, f"/auth/login?next={quote(next_path)}", host=host)
    assert status == 302, status
    state = set_cookies(headers)[dashboard_auth.LOGIN_COOKIE][0]
    callback = urlsplit(follow_provider(provider, header(headers, "Location")))
    return f"{callback.path}?{callback.query}", state


def login(server, provider, next_path="/", host=None):
    callback, state = start_login(server, provider, next_path, host)
    return call(server, callback, headers={"Cookie": f"babysit_login={state}"}, host=host)


def session_cookie(headers):
    cookies = set_cookies(headers)
    name = next(n for n in cookies if n.endswith("babysit_session") and cookies[n][0])
    return f"{name}={cookies[name][0]}"


def signed_in(server, provider):
    status, headers, _ = login(server, provider)
    assert status == 302
    return session_cookie(headers)


def session(server, cookie=None):
    status, _, body = call(server, "/api/session", headers={"Cookie": cookie} if cookie else None)
    assert status == 200
    return json.loads(body)


def post(server, path, action, value, headers=None):
    return call(
        server,
        path,
        "POST",
        {"Content-Type": "application/json", "X-Babysit-Action": action, **(headers or {})},
        json.dumps(value),
    )


# OIDC


def test_login_creates_a_strict_http_only_session_and_drops_provider_tokens(
    site, provider, tmp_path
):
    server = site()
    status, headers, _ = call(server, "/auth/login?next=/%3Ftab%3Dprs")
    assert status == 302
    target = urlsplit(header(headers, "Location"))
    query = parse_qs(target.query)
    assert target.path == "/realms/test/auth"
    assert query["code_challenge_method"] == ["S256"] and query["client_id"] == [CLIENT_ID]
    assert query["redirect_uri"] == [f"http://127.0.0.1:{server.server_port}/auth/callback"]
    assert len(query["state"][0]) >= 43 and len(query["nonce"][0]) >= 43
    state, attributes = set_cookies(headers)["babysit_login"]
    assert state == query["state"][0]
    assert {"Path=/auth/", "HttpOnly", "SameSite=Lax", "Max-Age=600"} <= set(attributes)
    assert "Secure" not in attributes  # Plain http loopback only.

    callback = urlsplit(follow_provider(provider, header(headers, "Location")))
    status, headers, _ = call(
        server, f"{callback.path}?{callback.query}", headers={"Cookie": f"babysit_login={state}"}
    )
    assert status == 302 and header(headers, "Location") == "/?tab=prs"
    cookies = set_cookies(headers)
    assert cookies["babysit_login"][0] == "" and "Max-Age=0" in cookies["babysit_login"][1]
    value, attributes = cookies["babysit_session"]
    assert {"Path=/", "HttpOnly", "SameSite=Strict", f"Max-Age={SESSION_ABSOLUTE}"} <= set(
        attributes
    )
    view = session(server, f"babysit_session={value}")
    assert view["authenticated"] and view["user"] == "alice" and view["via"] == "oidc"
    # Only a digest of the cookie is stored, and nothing from the provider's tokens.
    stored = (tmp_path / "auth" / "auth.sqlite").read_bytes()
    assert value.encode() not in stored and b"fixture-refresh-token" not in stored
    assert dashboard_auth.digest(value).encode() in stored


def test_https_host_gets_secure_host_prefixed_cookies(site, provider):
    server = site(allowed_hosts=(PUBLIC_HOST,))
    status, headers, _ = call(server, "/auth/login", host=PUBLIC_HOST)
    assert status == 302
    assert "Secure" in set_cookies(headers)["babysit_login"][1]
    assert parse_qs(urlsplit(header(headers, "Location")).query)["redirect_uri"] == [
        f"https://{PUBLIC_HOST}/auth/callback"
    ]
    # The provider redirects to the https host; replay that callback on its Host.
    callback, state = start_login(server, provider, host=PUBLIC_HOST)
    status, headers, _ = call(
        server, callback, headers={"Cookie": f"babysit_login={state}"}, host=PUBLIC_HOST
    )
    assert status == 302
    value, attributes = set_cookies(headers)["__Host-babysit_session"]
    assert {"Secure", "HttpOnly", "SameSite=Strict", "Path=/"} <= set(attributes)
    assert session(server, f"__Host-babysit_session={value}")["authenticated"]


def test_redirect_uri_comes_from_config_never_from_the_request(site):
    server = site(allowed_hosts=("other.example.ts.net",))
    status, _, body = call(server, "/auth/login", host="unlisted.example")
    assert status == 403
    server.auth.oidc.config.redirect_uris.pop("other.example.ts.net")
    status, _, body = call(server, "/auth/login", host="other.example.ts.net")
    assert status == 403 and b"not configured for this host" in body


@pytest.mark.parametrize("target", ["//evil.example/x", "https://evil.example/", "/\\evil"])
def test_next_must_be_a_local_path(site, provider, target):
    server = site()
    status, headers, _ = login(server, provider, target)
    assert status == 302 and header(headers, "Location") == "/"


def test_callback_needs_the_browser_bound_state(site, provider):
    server = site()
    callback, state = start_login(server, provider)
    for cookie in (None, "babysit_login=" + "x" * 43):
        status, headers, body = call(server, callback, headers={"Cookie": cookie} if cookie else {})
        assert status == 400 and b"not started in this browser" in body
        assert "babysit_session" not in set_cookies(headers)
    # The genuine browser still completes: a forged attempt does not burn the state.
    status, headers, _ = call(server, callback, headers={"Cookie": f"babysit_login={state}"})
    assert status == 302 and session(server, session_cookie(headers))["authenticated"]


def test_replayed_callback_and_code_are_refused(site, provider):
    server = site()
    callback, state = start_login(server, provider)
    cookie = {"Cookie": f"babysit_login={state}"}
    assert call(server, callback, headers=cookie)[0] == 302
    status, headers, body = call(server, callback, headers=cookie)
    assert status == 400 and b"expired or was already used" in body
    assert "babysit_session" not in set_cookies(headers)
    assert provider.token_requests == 1


def test_provider_refuses_a_reused_code_or_wrong_verifier(site, provider):
    server = site()
    oidc = server.auth.oidc
    redirect = f"http://127.0.0.1:{server.server_port}/auth/callback"
    url = oidc.authorize_url(redirect, "s" * 43, "n" * 43, "v" * 64)
    code = parse_qs(urlsplit(follow_provider(provider, url)).query)["code"][0]
    with pytest.raises(AuthError, match="invalid_grant"):
        oidc.exchange(code, redirect, "w" * 64)
    with pytest.raises(AuthError, match="invalid_grant"):
        oidc.exchange(code, redirect, "v" * 64)


def test_login_expires_after_ten_minutes(site, provider, clock):
    server = site()
    callback, state = start_login(server, provider)
    clock.advance(601)
    status, _, body = call(server, callback, headers={"Cookie": f"babysit_login={state}"})
    assert status == 400 and b"expired" in body


def test_provider_error_is_shown_without_a_session(site):
    server = site()
    status, headers, body = call(server, "/auth/callback?error=access_denied&state=x")
    assert status == 403 and b"access_denied" in body
    assert header(headers, "Content-Type").startswith("text/html")
    assert "babysit_session" not in set_cookies(headers)


@pytest.mark.parametrize(
    ("claims", "message"),
    [
        ({"nonce": "n" * 43}, b"nonce"),
        ({"nonce": None}, b"nonce"),
        ({"iss": "https://evil.example/realms/test"}, b"another issuer"),
        ({"aud": "other-client", "azp": None}, b"another client"),
        ({"aud": ["other-client"], "azp": CLIENT_ID}, b"another client"),
        ({"aud": [CLIENT_ID, "other-client"], "azp": None}, b"another party"),
        ({"azp": "other-client"}, b"another party"),
        ({"exp": int(time.time()) - 120}, b"expired"),
        ({"exp": None}, b"expired"),
        ({"iat": int(time.time()) + 300}, b"future"),
        ({"sub": None}, b"no subject"),
    ],
)
def test_id_token_claims_are_validated(site, provider, tmp_path, claims, message):
    server = site()
    provider.claims = claims
    status, headers, body = login(server, provider)
    assert status == 403 and message in body, body
    assert "babysit_session" not in set_cookies(headers)
    assert server.auth.store.sessions() == []


def test_claims_within_clock_skew_and_with_azp_for_several_audiences_pass(site, provider):
    server = site()
    now = int(time.time())
    provider.claims = {"exp": now - 30, "iat": now + 30, "aud": [CLIENT_ID, "account"]}
    status, headers, _ = login(server, provider)
    assert status == 302 and session(server, session_cookie(headers))["authenticated"]


@pytest.mark.parametrize("alg", ["none", "HS256"])
def test_only_rs256_signatures_are_accepted(site, provider, alg):
    server = site()
    provider.sign_with = alg
    status, _, body = login(server, provider)
    assert status == 403 and b"RS256" in body


def test_tampered_payload_fails_signature_check(site, provider):
    server = site()
    token = provider.id_token("n" * 43)
    head, _, signature = token.split(".")
    provider.user["preferred_username"] = "mallory"
    forged = provider.id_token("n" * 43).split(".")[1]
    with pytest.raises(AuthError, match="signature"):
        server.auth.oidc.validate(f"{head}.{forged}.{signature}", "n" * 43)


def test_missing_role_creates_no_session(site, provider):
    server = site()
    provider.user["roles"] = ["some-other-role"]
    status, headers, body = login(server, provider)
    assert status == 403 and b"babysitter-admin" in body
    assert "babysit_session" not in set_cookies(headers)
    assert server.auth.store.sessions() == []
    provider.user["roles"] = []
    provider.claims = {"resource_access": {"other-client": {"roles": ["babysitter-admin"]}}}
    assert login(server, provider)[0] == 403


def test_jwks_rotation_refetches_at_most_once_a_minute(site, provider, clock):
    server = site()
    signed_in(server, provider)
    assert provider.jwks_requests == 1
    signed_in(server, provider)
    assert provider.jwks_requests == 1  # Cached.
    clock.advance(61)
    provider.rotate("key-2")
    signed_in(server, provider)
    assert provider.jwks_requests == 2
    provider.rotate("key-3")
    status, _, body = login(server, provider)
    assert status == 403 and b"unknown key" in body
    assert provider.jwks_requests == 2
    clock.advance(61)
    signed_in(server, provider)
    assert provider.jwks_requests == 3


def test_discovery_must_name_the_configured_issuer(site, provider):
    server = site()
    provider.discovery_issuer = "https://evil.example/realms/test"
    status, headers, body = call(server, "/auth/login")
    assert status == 503 and b"another issuer" in body
    assert "babysit_login" not in set_cookies(headers)


def test_oidc_config_validation(tmp_path):
    path = tmp_path / "oidc.json"
    good = {
        "issuer": "https://auth.example.org/realms/magmahome",
        "client_id": "babysitter",
        "client_secret": "s",
        "redirect_uris": {
            "babysitter.example.ts.net": "https://babysitter.example.ts.net/auth/callback"
        },
    }
    path.write_text(json.dumps(good))
    assert OidcConfig.load(path).role == "babysitter-admin"
    for bad in (
        {**good, "issuer": "http://auth.example.org/realms/magmahome"},
        {**good, "redirect_uris": {"a.example": "https://b.example/auth/callback"}},
        {**good, "redirect_uris": {"a.example": "http://a.example/auth/callback"}},
        {**good, "redirect_uris": {"a.example": "https://a.example/elsewhere"}},
        {**good, "client_secret": ""},
        {**good, "extra": 1},
    ):
        path.write_text(json.dumps(bad))
        with pytest.raises(ValueError):
            OidcConfig.load(path)


# Sessions


def test_session_idles_out_after_seven_days(site, provider, clock):
    server = site()
    cookie = signed_in(server, provider)
    clock.advance(SESSION_IDLE - 10)
    assert session(server, cookie)["authenticated"]
    clock.advance(SESSION_IDLE + 1)
    assert not session(server, cookie)["authenticated"]


def test_use_extends_idle_time_but_not_past_the_absolute_lifetime(site, provider, clock):
    server = site()
    cookie = signed_in(server, provider)
    for _ in range(4):
        clock.advance(6 * 86400)
        assert session(server, cookie)["authenticated"]
    clock.advance(6 * 86400)  # Day 30.
    assert not session(server, cookie)["authenticated"]
    assert server.auth.store.sessions() == []


def test_logout_ends_the_session_and_the_providers(site, provider):
    server = site()
    cookie = signed_in(server, provider)
    status, _, _ = call(server, "/auth/logout", "POST", {"Cookie": cookie}, "{}")
    assert status == 403  # Without the action header, as a cross-site form would be.
    assert session(server, cookie)["authenticated"]
    status, headers, body = post(server, "/auth/logout", "logout", {}, {"Cookie": cookie})
    assert status == 200
    redirect = urlsplit(json.loads(body)["redirect"])
    assert redirect.path == "/realms/test/logout"
    assert parse_qs(redirect.query) == {
        "client_id": [CLIENT_ID],
        "post_logout_redirect_uri": [f"http://127.0.0.1:{server.server_port}/"],
    }
    cleared = set_cookies(headers)["babysit_session"]
    assert cleared[0] == "" and "Max-Age=0" in cleared[1]
    assert not session(server, cookie)["authenticated"]


def test_cli_revocation_reaches_a_cached_session_within_a_minute(site, provider, clock):
    server = site()
    cookie = signed_in(server, provider)
    assert session(server, cookie)["authenticated"]
    other = dashboard_auth.Store(server.auth.store.home)  # As the CLI process opens it.
    assert other.revoke_sessions() == 1
    clock.advance(dashboard_auth.CACHE_SECONDS + 1)
    assert not session(server, cookie)["authenticated"]


# Keys and enforce mode


def test_enforce_refuses_anonymous_api_but_serves_the_shell(site, provider):
    server = site("enforce")
    status, _, body = call(server, "/api/status")
    assert status == 401 and json.loads(body)["login"] == "/auth/login"
    status, _, body = post(server, "/api/cancel", "cancel", {"id": "x"})
    assert status == 401
    assert call(server, "/")[0] == 200
    assert call(server, "/auth.js")[0] == 200
    view = session(server)
    assert view == {**view, "enabled": True, "mode": "enforce", "authenticated": False}
    # Routes that check their own credential still reach their handler.
    status, _, body = post(server, "/api/worker/register", "x", {})
    assert status == 403 and b"worker token" in body
    # A target that could parse to another path than a prefix test sees.
    status, _, _ = call(server, f"http://127.0.0.1:{server.server_port}/api/status")
    assert status == 401
    cookie = signed_in(server, provider)
    assert call(server, "/api/status", headers={"Cookie": cookie})[0] == 200
    assert server.log and "refused GET /api/status (anonymous" in server.log[0]


def test_key_scopes(site):
    server = site("enforce")
    store = server.auth.store

    def bearer(token):
        return {"Authorization": f"Bearer {token}"}

    _, read = store.create_key("read")
    assert call(server, "/api/status", headers=bearer(read))[0] == 200
    status, _, body = post(server, "/api/cancel", "cancel", {"id": "x"}, bearer(read))
    assert status == 403 and b"may not use this route" in body

    _, admin = store.create_key("admin")
    status, _, body = post(server, "/api/cancel", "cancel", {"id": "x"}, bearer(admin))
    assert status not in (401, 403), body

    _, launch = store.create_key("launch")
    assert call(server, "/api/status", headers=bearer(launch))[0] == 403
    assert call(server, "/api/agent/operation", headers=bearer(launch))[0] == 404  # No route yet.

    _, narrow = store.create_key("read", scopes=["GET /api/status"])
    assert call(server, "/api/status", headers=bearer(narrow))[0] == 200
    assert call(server, "/api/prs", headers=bearer(narrow))[0] == 403

    view = session(server, None)
    assert not view["authenticated"]
    status, _, body = call(server, "/api/session", headers=bearer(read))
    assert json.loads(body)["credential"] == "read"


def test_revoked_expired_and_malformed_keys_are_refused(site, clock):
    server = site("enforce")
    store = server.auth.store
    key_id, revoked = store.create_key("admin")
    store.revoke_key(key_id)
    _, expiring = store.create_key("admin", lifetime=60)
    assert call(server, "/api/status", headers={"Authorization": f"Bearer {expiring}"})[0] == 200
    clock.advance(61)
    for token in (revoked, expiring, "bsk_admin_short", "bsk_root_" + "a" * 43):
        status, _, _ = call(server, "/api/status", headers={"Authorization": f"Bearer {token}"})
        assert status == 401, token
    with pytest.raises(ValueError):
        store.revoke_key(key_id)
    with pytest.raises(ValueError):
        store.create_key("read", scopes=["DELETE /api/*"])


def test_keys_are_stored_as_digests(tmp_path):
    store = dashboard_auth.Store(tmp_path / "auth")
    key_id, token = store.create_key("read", label="voice")
    assert token.startswith("bsk_read_") and dashboard_auth.KEY.fullmatch(token)
    assert token.encode() not in (tmp_path / "auth" / "auth.sqlite").read_bytes()
    assert store.key(token)["id"] == key_id
    assert [k["label"] for k in store.keys()] == ["voice"]
    assert stat.S_IMODE((tmp_path / "auth").stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / "auth" / "auth.sqlite").stat().st_mode) == 0o600


def test_launch_keys_record_who_started_the_agent(tmp_path):
    store = dashboard_auth.Store(tmp_path / "auth")
    _, token = dashboard_auth.mint_launch_key(
        store, origin="cron", cwd="/work/tree", agent="claude", op="op-1", depth=1
    )
    binding = store.key(token)["binding"]
    assert binding == {
        "origin": "cron",
        "cwd": "/work/tree",
        "agent": "claude",
        "op": "op-1",
        "depth": 1,
        "docker": False,
        "sandbox": "safehouse",
    }
    for bad in (
        {"origin": "robot", "cwd": "/w", "agent": "claude"},
        {"origin": "user", "cwd": "relative", "agent": "claude"},
        {"origin": "user", "cwd": "/w", "agent": "claude", "extra": 1},
        {"origin": "user", "cwd": "/w", "agent": "claude", "depth": -1},
    ):
        with pytest.raises(ValueError):
            dashboard_auth.launch_binding(**bad)


# Break-glass


def test_break_glass_link_works_once_without_oidc(site, clock):
    server = site("enforce", oidc=False)
    token = server.auth.store.create_link()
    status, headers, body = call(server, "/auth/break-glass")
    assert status == 200 and b"/auth.js" in body
    assert "script-src 'self'" in header(headers, "Content-Security-Policy")
    status, _, _ = call(
        server,
        "/auth/break-glass",
        "POST",
        {"Content-Type": "application/json"},
        json.dumps({"token": token}),
    )
    assert status == 403  # No action header: not the page's own request.
    status, headers, _ = post(server, "/auth/break-glass", "break-glass", {"token": token})
    assert status == 200
    value, attributes = set_cookies(headers)["babysit_session"]
    assert f"Max-Age={dashboard_auth.BREAK_GLASS_LIFETIME}" in attributes
    view = session(server, f"babysit_session={value}")
    assert view["authenticated"] and view["via"] == "break-glass" and view["login"] is None
    assert call(server, "/api/status", headers={"Cookie": f"babysit_session={value}"})[0] == 200
    status, _, body = post(server, "/auth/break-glass", "break-glass", {"token": token})
    assert status == 403 and b"already used" in body
    clock.advance(dashboard_auth.BREAK_GLASS_LIFETIME + 1)
    assert not session(server, f"babysit_session={value}")["authenticated"]
    # OIDC sign-in is simply unavailable.
    assert call(server, "/auth/login")[0] == 404


def test_break_glass_link_expires_after_ten_minutes(site, clock):
    server = site("enforce", oidc=False)
    token = server.auth.store.create_link()
    clock.advance(dashboard_auth.LINK_TTL + 1)
    assert post(server, "/auth/break-glass", "break-glass", {"token": token})[0] == 403


# Report-only mode


def test_report_mode_refuses_nothing_and_counts_what_enforce_would(site, provider, clock):
    server = site("report")
    _, read = server.auth.store.create_key("read")
    requests = [
        lambda: call(server, "/api/status"),
        lambda: call(server, "/api/status"),
        lambda: call(server, "/api/status?refresh=1"),
        lambda: post(server, "/api/cancel", "cancel", {"id": "x"}),
        lambda: post(
            server, "/api/cancel", "cancel", {"id": "x"}, {"Authorization": f"Bearer {read}"}
        ),
        lambda: call(
            server, "/api/status", headers={"Authorization": "Bearer bsk_admin_" + "a" * 43}
        ),
        lambda: call(server, f"http://127.0.0.1:{server.server_port}/api/status"),
    ]
    for request in requests:
        status, _, body = request()
        assert status not in (401,) and b"Sign in to use" not in body and b"may not use" not in body
    status, _, body = call(server, "/api/status")
    assert status == 200 and json.loads(body)["auth"] == {"mode": "report", "would_deny": 8}
    rows = {
        (r["method"], r["route"], r["credential"]): r["count"]
        for r in json.loads(call(server, "/api/auth-report")[2])["rows"]
    }
    assert rows == {
        ("GET", "/api/status", "anonymous"): 4,
        ("POST", "/api/cancel", "anonymous"): 1,
        ("POST", "/api/cancel", "read"): 1,
        ("GET", "/api/status", "invalid"): 1,
        ("GET", "(unexpected target)", "invalid"): 1,
        ("GET", "/api/auth-report", "anonymous"): 1,
    }
    # One log line per route and credential a minute, without keys or query strings.
    assert sum("GET /api/status (anonymous" in line for line in server.log) == 1
    assert not any("bsk_" in line or "refresh" in line for line in server.log)
    clock.advance(61)
    call(server, "/api/status")
    assert sum("GET /api/status (anonymous" in line for line in server.log) == 2
    assert "would refuse" in server.log[0]
    # A session is never counted.
    cookie = signed_in(server, provider)
    before = server.auth.report.total()
    assert call(server, "/api/status", headers={"Cookie": cookie})[0] == 200
    assert server.auth.report.total() == before


# Configuration and the off switch


def test_load_turns_auth_on_only_when_configured(tmp_path):
    home = tmp_path / "auth"
    lines = []
    assert dashboard_auth.load("report", home, log=lines.append) is None
    assert not home.exists()  # Nothing is created while auth is off.
    enforced = dashboard_auth.load("enforce", home, log=lines.append)
    assert enforced is not None and enforced.oidc is None
    (home / "oidc.json").write_text("{not json")
    assert dashboard_auth.load("report", home, log=lines.append) is None
    assert "stays off" in lines[-1]
    with pytest.raises(ValueError):
        dashboard_auth.load("enforce", home, log=lines.append)
    config = {
        "issuer": "https://auth.example.org/realms/magmahome",
        "client_id": "babysitter",
        "client_secret": "s",
        "redirect_uris": {"127.0.0.1:8765": "http://127.0.0.1:8765/auth/callback"},
    }
    (home / "oidc.json").write_text(json.dumps(config))
    os.chmod(home / "oidc.json", 0o644)
    auth = dashboard_auth.load("report", home, log=lines.append)
    assert auth is not None and auth.mode == "report" and auth.oidc is not None
    assert "chmod 600" in lines[-1]
    assert dashboard_auth.load("off", home, log=lines.append) is None


def test_dashboard_without_auth_answers_as_before(tmp_path):
    server = dashboard.DashboardServer(tmp_path, 0)
    thread = threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True)
    thread.start()
    try:
        status, _, body = call(server, "/api/status")
        assert status == 200 and "auth" not in json.loads(body)
        assert json.loads(call(server, "/api/session")[2]) == {"enabled": False}
        assert call(server, "/auth/login")[0] == 404
        assert json.loads(call(server, "/api/auth-report")[2]) == {"enabled": False}
    finally:
        server.shutdown()
        thread.join(5)
        server.server_close()


# CLI


@pytest.fixture
def cli(monkeypatch, capsys, tmp_path):
    home = tmp_path / "auth"
    monkeypatch.setattr(sandbox, "confined", lambda: False)
    umask = os.umask(0o022)
    os.umask(umask)

    def run(*args):
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "pr_supervisor.py",
                "--home",
                str(tmp_path / "state"),
                "auth",
                "--auth-home",
                str(home),
                *args,
            ],
        )
        try:
            code = pr_supervisor.main()
        finally:
            os.umask(umask)
        return code, json.loads(capsys.readouterr().out)

    run.home = home
    return run


def test_cli_admin_key_file_is_private_and_rotates(cli):
    code, first = cli("admin-key")
    assert code == 0 and first["created"]
    path = cli.home / "admin-key"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(cli.home.stat().st_mode) == 0o700
    old = path.read_text().strip()
    assert old.startswith("bsk_admin_") and old not in json.dumps(first)
    assert cli("admin-key")[1]["created"] is False
    code, rotated = cli("admin-key", "--rotate")
    assert rotated["revoked"] == [first["id"]]
    store = dashboard_auth.Store(cli.home)
    assert store.key(old) is None and store.key(path.read_text().strip())["kind"] == "admin"


def test_cli_keys_create_list_and_revoke(cli):
    code, created = cli(
        "keys", "create", "--kind", "read", "--label", "voice", "--expires-days", "1"
    )
    assert code == 0 and created["key"].startswith("bsk_read_")
    code, listed = cli("keys", "list")
    assert [(k["id"], k["kind"], k["label"]) for k in listed] == [(created["id"], "read", "voice")]
    assert "digest" not in listed[0] and created["key"] not in json.dumps(listed)
    assert cli("keys", "revoke", created["id"])[1] == {"id": created["id"], "revoked": True}
    assert cli("keys", "list")[1] == []
    assert cli("keys", "list", "--all")[1][0]["revoked_at"]
    code, error = cli("keys", "revoke", created["id"])
    assert code == 1 and "No live key" in error["error"]
    code, error = cli("keys", "create", "--kind", "read", "--scope", "rm -rf")
    assert code == 1 and "Invalid scope" in error["error"]


def test_cli_login_link_and_sessions(cli):
    code, link = cli("login-link", "--port", "8799")
    assert code == 0 and link["expires_in"] == 600
    (url,) = link["links"]
    assert url.startswith("http://127.0.0.1:8799/auth/break-glass#t=")
    store = dashboard_auth.Store(cli.home)
    assert store.redeem_link(url.split("#t=")[1])
    store.create_session("break-glass", "break-glass", "break-glass")
    code, sessions = cli("sessions", "list")
    assert [s["via"] for s in sessions] == ["break-glass"] and len(sessions[0]["id"]) == 12
    assert cli("sessions", "revoke", sessions[0]["id"])[1] == {"revoked": 1}
    assert cli("sessions", "revoke")[0] == 1


def test_cli_launch_key_and_report(cli, tmp_path):
    code, minted = cli(
        "launch-key", "--agent", "codex", "--cwd", str(tmp_path), "--origin", "subtask"
    )
    assert code == 0 and minted["key"].startswith("bsk_launch_")
    binding = dashboard_auth.Store(cli.home).key(minted["key"])["binding"]
    assert binding["origin"] == "subtask" and binding["cwd"] == os.path.realpath(tmp_path)
    assert cli("keys", "create", "--kind", "launch")[0] == 1
    assert cli("report")[1] == []
    assert cli("report", "--reset")[1] == {"reset": True}
    code, status = cli("status")
    assert status["keys"]["launch"] == 1 and status["oidc"] is False


def test_cli_refuses_inside_a_sandbox(cli, monkeypatch):
    monkeypatch.setattr(sandbox, "confined", lambda: True)
    code, error = cli("admin-key")
    assert code == 1 and "outside the sandbox" in error["error"]
    assert not cli.home.exists()
