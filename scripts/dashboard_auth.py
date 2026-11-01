"""Dashboard sign-in: OIDC sessions, scoped keys, break-glass links and the access policy.

Secrets live in the auth home (``~/.config/babysit-pr-auth``, 0700), which no sandbox is
granted: ``auth.sqlite`` (sessions, keys, login links, the report), ``admin-key`` and
``oidc.json``, each 0600. Only SHA-256 digests of cookies, keys and links are stored;
their high entropy makes a slow hash unnecessary.

Modes: ``off`` checks nothing. ``report`` classifies every request and counts and logs
the ones ``enforce`` would refuse, but refuses none. ``enforce`` refuses them.
"""

import base64
import contextlib
import hashlib
import html
import json
import os
import re
import secrets
import sqlite3
import ssl
import stat
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit

MODES = ("off", "report", "enforce")
DEFAULT_HOME = Path.home() / ".config" / "babysit-pr-auth"
OIDC_FILE = "oidc.json"
ADMIN_KEY_FILE = "admin-key"
DEFAULT_ROLE = "babysitter-admin"
# Clock skew allowed between this machine and the identity provider.
SKEW = 60
LOGIN_TTL = 600
# Unauthenticated visitors can start logins; the oldest pending ones go past this many.
PENDING_MAX = 256
DISCOVERY_TTL = 24 * 3600
# An ID token signed with an unknown kid refetches the JWKS at most this often.
JWKS_MIN_REFETCH = 60
SESSION_IDLE = 7 * 86400
SESSION_ABSOLUTE = 30 * 86400
BREAK_GLASS_LIFETIME = 12 * 3600
# Plain-http loopback cookies are sent to every port on 127.0.0.1 (cookies are not
# port-isolated), so a local dev server the browser visits could read them: short.
LOOPBACK_IDLE = 3600
LOOPBACK_ABSOLUTE = 8 * 3600
LINK_TTL = 600
# Pending sign-ins per source address; a flood from one source evicts only its own.
PENDING_PER_SOURCE = 8
# A failed discovery is not retried for this long, so an outage does not hold the lock
# on every request.
DISCOVERY_RETRY = 30
LAUNCH_KEY_LIFETIME = 7 * 86400
# last_seen and last_used are written at most this often, and a session row is cached
# this long, so polling does not reach sqlite on every request.
TOUCH_EVERY = 300
CACHE_SECONDS = 60
REPORT_LOG_EVERY = 60
HTTP_TIMEOUT = 10
HTTP_MAX = 1024 * 1024
LOGIN_COOKIES = ("__Secure-babysit_login", "babysit_login")
SESSION_COOKIES = ("__Host-babysit_session", "babysit_session")
TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")
KINDS = ("admin", "launch", "read", "worker", "ext", "push")
KEY = re.compile(r"bsk_(" + "|".join(KINDS) + r")_([A-Za-z0-9_-]{43})")
SCOPE = re.compile(r"(?:(GET|POST|OPTIONS) )?(\*|/[A-Za-z0-9/_.-]*\*?)")
# What each credential may use: an optional method, then a path, or a prefix ending in *.
# A session may use everything, as an admin key may.
POLICY: dict[str, tuple[str, ...]] = {
    "admin": ("*",),
    "read": ("GET /api/*",),
    "launch": ("/api/agent/*",),
    "worker": ("POST /api/worker/*",),
    "ext": ("/api/extension",),
    "push": ("POST /api/push-read", "POST /api/push-seen"),
}
# Routes whose handler checks a credential of its own, kept until Phase 2 moves those
# credentials into the key store: worker tokens, extension pairing tokens and push
# device tokens (sent in the body).
SELF_AUTHENTICATED = (
    "POST /api/worker/*",
    "/api/extension",
    "POST /api/push-read",
    "POST /api/push-seen",
    "POST /api/push-unsubscribe",
)
LAUNCH_ORIGINS = ("user", "cron", "subtask")
LAUNCH_FIELDS = {
    "origin",
    "cwd",
    "agent",
    "config_dir",
    "claude_account",
    "docker",
    "sandbox",
    "op",
    "root",
    "depth",
}
LOOPBACK = {"127.0.0.1", "localhost", "::1"}
CONTROL = re.compile(r"[\x00-\x1f\x7f]")
PROVIDER_ERROR = re.compile(r"[a-z_]{1,40}")
REPORT_KEYS_MAX = 1000
# Enforce mode would let anything that can write the state home through these routes,
# since their tokens live there; tests set this to exercise enforce before Phase 2.
ALLOW_OWN_ROUTES_ENV = "BABYSIT_AUTH_TEST_ALLOW_OWN_ROUTES"


class AuthError(Exception):
    """A refused sign-in step; ``status`` is the HTTP status to answer with."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64url(text: str) -> bytes:
    if not isinstance(text, str) or not re.fullmatch(r"[A-Za-z0-9_-]*", text):
        raise AuthError("Malformed token encoding", 403)
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except ValueError:
        raise AuthError("Malformed token encoding", 403) from None


def ensure_home(home: Path) -> Path:
    """The auth home, created 0700, its mode narrowed if anything widened it.

    A symlink, or a directory someone else owns, is refused: whoever controls it
    controls every credential.
    """
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = os.lstat(home)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ValueError(f"{home} must be a real directory, not a symlink")
    if info.st_uid != os.getuid():
        raise ValueError(f"{home} is owned by another user")
    if info.st_mode & 0o077:
        os.chmod(home, 0o700)
    return home


def write_secret(path: Path, text: str) -> None:
    """Replace ``path`` atomically with a 0600 file holding ``text``."""
    partial = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}")
    descriptor = os.open(partial, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(text)
    os.replace(partial, path)


def scope_allows(scope: str, method: str, path: str) -> bool:
    want, _, pattern = scope.rpartition(" ")
    if want and want != method:
        return False
    if pattern == "*":
        return True
    if pattern.endswith("*"):
        return path.startswith(pattern[:-1])
    return path == pattern


def check_scopes(scopes) -> list[str]:
    if not isinstance(scopes, list) or not scopes or len(scopes) > 32:
        raise ValueError("Give between 1 and 32 scopes")
    for scope in scopes:
        if not isinstance(scope, str) or not SCOPE.fullmatch(scope):
            raise ValueError(f"Invalid scope {scope!r}: use [METHOD ]/path or /prefix*")
    return list(scopes)


def request_path(target: str) -> str | None:
    """The path of an origin-form request target, as the handlers read it; None otherwise.

    Anything else (``//host/...``, ``http://...``) could parse to another path than a
    plain prefix test sees, so it is never classified as a public or own route.
    """
    if not target.startswith("/") or target.startswith("//"):
        return None
    return urlsplit(target).path


def public(method: str, path: str) -> bool:
    """The static shell, sign-in routes and the session probe answer anyone."""
    if path.startswith("/auth/") or path == "/api/session":
        return True
    return method == "GET" and not path.startswith("/api/")


def safe_next(value) -> str:
    """A page of the dashboard to return to after sign-in: never another origin, an API
    route (some GETs start work) or a sign-in route."""
    if (
        isinstance(value, str)
        and value.isascii()
        and value.startswith("/")
        and not value.startswith(("//", "/\\", "/api/", "/auth/"))
        and len(value) <= 2000
        and not CONTROL.search(value)
    ):
        return value
    return "/"


def loopback_url(url: str) -> bool:
    return (urlsplit(url).hostname or "") in LOOPBACK


def allowed_url(url) -> bool:
    """https, or plain http to this machine (a local identity provider, or tests)."""
    if not isinstance(url, str):
        return False
    parts = urlsplit(url)
    return bool(parts.netloc) and (
        parts.scheme == "https" or (parts.scheme == "http" and loopback_url(url))
    )


def read_cookie(header: str | None, name: str) -> str | None:
    for part in (header or "").split(";"):
        key, sep, value = part.strip().partition("=")
        if sep and key == name:
            return value
    return None


def cookie(name, value, *, secure, max_age, path="/", same_site="Strict") -> str:
    attributes = [f"{name}={value}", f"Path={path}", f"Max-Age={int(max_age)}", "HttpOnly"]
    if secure:
        attributes.append("Secure")
    attributes.append(f"SameSite={same_site}")
    return "; ".join(attributes)


def session_cookie_name(secure: bool) -> str:
    # __Host- requires Secure, Path=/ and no Domain: only the https host can set it.
    return SESSION_COOKIES[0] if secure else SESSION_COOKIES[1]


def login_cookie_name(secure: bool) -> str:
    return LOGIN_COOKIES[0] if secure else LOGIN_COOKIES[1]


def provider_error(value) -> str:
    """The provider's error code, if it is one; never text an attacker chose."""
    return value if isinstance(value, str) and PROVIDER_ERROR.fullmatch(value) else "an error"


def clean_text(value, limit=200) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return CONTROL.sub("", value)[:limit] or None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


# Identity-provider requests verify TLS and ignore proxy environment variables.
_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}),
    urllib.request.HTTPSHandler(context=ssl.create_default_context()),
    _NoRedirect(),
)


def http_json(url: str, data: bytes | None = None, headers: dict | None = None) -> Any:
    if not allowed_url(url):
        raise AuthError(f"Refusing a non-https identity provider URL: {url}", 503)
    request = urllib.request.Request(
        url, data=data, headers={"Accept": "application/json", **(headers or {})}
    )
    try:
        with _OPENER.open(request, timeout=HTTP_TIMEOUT) as response:
            body = response.read(HTTP_MAX + 1)
    except urllib.error.HTTPError as exc:
        reason = ""
        with contextlib.suppress(Exception):
            reason = str(json.loads(exc.read(4096)).get("error", ""))[:100]
        raise AuthError(
            f"The identity provider answered {exc.code}{': ' + reason if reason else ''}", 502
        ) from None
    except (OSError, ValueError) as exc:
        raise AuthError(f"Cannot reach the identity provider: {exc}", 503) from None
    if len(body) > HTTP_MAX:
        raise AuthError("The identity provider's answer is too large", 502)
    try:
        return json.loads(body)
    except ValueError:
        raise AuthError("The identity provider did not answer JSON", 502) from None


@dataclass(frozen=True)
class OidcConfig:
    issuer: str
    client_id: str
    client_secret: str
    # Host header -> the redirect URI registered for it; never taken from the request.
    redirect_uris: dict[str, str]
    role: str = DEFAULT_ROLE

    @classmethod
    def load(cls, path: Path) -> "OidcConfig":
        try:
            value = json.loads(path.read_text())
        except ValueError as exc:
            raise ValueError(f"{path} is not JSON: {exc}") from None
        if not isinstance(value, dict):
            raise ValueError(f"{path} must hold an object")
        unknown = set(value) - {"issuer", "client_id", "client_secret", "redirect_uris", "role"}
        if unknown:
            raise ValueError(f"{path}: unknown fields {sorted(unknown)}")
        for name in ("issuer", "client_id", "client_secret"):
            if not isinstance(value.get(name), str) or not value[name]:
                raise ValueError(f"{path}: {name} is required")
        if not allowed_url(value["issuer"]):
            raise ValueError(f"{path}: issuer must be an https URL")
        uris = value.get("redirect_uris")
        if not isinstance(uris, dict) or not uris:
            raise ValueError(f"{path}: redirect_uris must map each Host to its callback URL")
        for host, uri in uris.items():
            parts = urlsplit(uri) if isinstance(uri, str) else None
            if (
                parts is None
                or parts.netloc != host
                or parts.path != "/auth/callback"
                or parts.query
                or not (parts.scheme == "https" or (host.split(":")[0] in LOOPBACK))
            ):
                raise ValueError(
                    f"{path}: redirect URI for {host} must be https://{host}/auth/callback "
                    "(plain http only for 127.0.0.1 or localhost)"
                )
        role = value.get("role", DEFAULT_ROLE)
        if not isinstance(role, str) or not role:
            raise ValueError(f"{path}: role must be a non-empty string")
        return cls(value["issuer"], value["client_id"], value["client_secret"], dict(uris), role)


def _rsa_key(jwk: dict):
    from cryptography.hazmat.primitives.asymmetric import rsa

    n = int.from_bytes(unb64url(jwk["n"]), "big")
    e = int.from_bytes(unb64url(jwk["e"]), "big")
    if n.bit_length() < 2048:
        raise AuthError("The identity provider's signing key is too short", 502)
    return rsa.RSAPublicNumbers(e, n).public_key()


def _verify_rs256(key, signature: bytes, signed: bytes) -> bool:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    try:
        key.verify(signature, signed, padding.PKCS1v15(), hashes.SHA256())
    except InvalidSignature:
        return False
    return True


class Oidc:
    """Discovery, the code exchange and ID-token validation against one issuer."""

    def __init__(self, config: OidcConfig, clock: Callable[[], float] = time.time):
        self.config = config
        self.clock = clock
        # Reentrant: a JWKS fetch reads the discovery document under the same lock.
        self.lock = threading.RLock()
        self.discovered: dict | None = None
        self.discovered_at = 0.0
        self.failed: tuple[float, AuthError] | None = None
        self.keys: dict[str | None, Any] = {}
        self.keys_at: float | None = None
        self.jwks_fetches = 0
        self.min_refetch = JWKS_MIN_REFETCH

    def metadata(self) -> dict:
        with self.lock:
            if self.discovered and self.clock() - self.discovered_at < DISCOVERY_TTL:
                return self.discovered
            if self.failed and self.clock() - self.failed[0] < DISCOVERY_RETRY:
                raise self.failed[1]
            try:
                value = self._discover()
            except AuthError as exc:
                self.failed = (self.clock(), exc)
                raise
            self.failed = None
            self.discovered, self.discovered_at = value, self.clock()
            return value

    def _discover(self) -> dict:
        url = self.config.issuer.rstrip("/") + "/.well-known/openid-configuration"
        value = http_json(url)
        if not isinstance(value, dict) or value.get("issuer") != self.config.issuer:
            raise AuthError("The identity provider names another issuer", 503)
        for name in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            if not allowed_url(value.get(name)):
                raise AuthError(f"The identity provider's {name} is not https", 503)
        end = value.get("end_session_endpoint")
        if end is not None and not allowed_url(end):
            value = {k: v for k, v in value.items() if k != "end_session_endpoint"}
        return value

    def authorize_url(self, redirect_uri, state, nonce, verifier) -> str:
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self.config.client_id,
                "redirect_uri": redirect_uri,
                "scope": "openid profile",
                "state": state,
                "nonce": nonce,
                "code_challenge": b64url(hashlib.sha256(verifier.encode()).digest()),
                "code_challenge_method": "S256",
            }
        )
        endpoint = self.metadata()["authorization_endpoint"]
        return f"{endpoint}{'&' if '?' in endpoint else '?'}{query}"

    def exchange(self, code: str, redirect_uri: str, verifier: str) -> str:
        """The ID token for an authorization code; access and refresh tokens are dropped."""
        from urllib.parse import quote

        basic = base64.b64encode(
            f"{quote(self.config.client_id, safe='')}:{quote(self.config.client_secret, safe='')}".encode()
        ).decode()
        answer = http_json(
            self.metadata()["token_endpoint"],
            urlencode(
                {
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect_uri,
                    "code_verifier": verifier,
                }
            ).encode(),
            {
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        token = answer.get("id_token") if isinstance(answer, dict) else None
        if not isinstance(token, str):
            raise AuthError("The identity provider returned no ID token", 502)
        return token

    def _fetch_keys(self) -> None:
        value = http_json(self.metadata()["jwks_uri"])
        self.jwks_fetches += 1
        self.keys_at = self.clock()
        keys: dict[str | None, Any] = {}
        for jwk in value.get("keys", []) if isinstance(value, dict) else []:
            if (
                not isinstance(jwk, dict)
                or jwk.get("kty") != "RSA"
                or jwk.get("use", "sig") != "sig"
                or jwk.get("alg", "RS256") != "RS256"
                or not isinstance(jwk.get("n"), str)
                or not isinstance(jwk.get("e"), str)
            ):
                continue
            kid = jwk.get("kid") if isinstance(jwk.get("kid"), str) else None
            # One malformed or short key never takes the others down with it.
            try:
                keys[kid] = _rsa_key(jwk)
            except (AuthError, ValueError):
                continue
        self.keys = keys

    def _select(self, kid):
        if kid is not None:
            return self.keys.get(kid)
        return next(iter(self.keys.values())) if len(self.keys) == 1 else None

    def signing_key(self, kid):
        with self.lock:
            if self.keys_at is None:
                self._fetch_keys()
            key = self._select(kid)
            # A rotated key: refetch, but at most once a minute however many tokens ask.
            if key is None and self.clock() - (self.keys_at or 0) >= self.min_refetch:
                self._fetch_keys()
                key = self._select(kid)
        if key is None:
            raise AuthError("The ID token is signed with an unknown key", 403)
        return key

    def validate(self, token: str, nonce: str) -> dict:
        parts = token.split(".")
        if len(parts) != 3:
            raise AuthError("Malformed ID token", 403)
        try:
            header = json.loads(unb64url(parts[0]))
            claims = json.loads(unb64url(parts[1]))
        except (ValueError, UnicodeDecodeError):
            raise AuthError("Malformed ID token", 403) from None
        if not isinstance(header, dict) or not isinstance(claims, dict):
            raise AuthError("Malformed ID token", 403)
        if header.get("alg") != "RS256" or "crit" in header:
            raise AuthError("The ID token is not signed with RS256", 403)
        kid = header.get("kid")
        if kid is not None and not isinstance(kid, str):
            raise AuthError("Malformed ID token", 403)
        key = self.signing_key(kid)
        if not _verify_rs256(key, unb64url(parts[2]), f"{parts[0]}.{parts[1]}".encode()):
            raise AuthError("The ID token's signature does not verify", 403)
        client = self.config.client_id
        if claims.get("iss") != self.config.issuer:
            raise AuthError("The ID token comes from another issuer", 403)
        audience = claims.get("aud")
        audience = [audience] if isinstance(audience, str) else audience
        if not isinstance(audience, list) or client not in audience:
            raise AuthError("The ID token is for another client", 403)
        if ("azp" in claims or len(audience) > 1) and claims.get("azp") != client:
            raise AuthError("The ID token was issued to another party", 403)
        now = self.clock()
        exp, iat = claims.get("exp"), claims.get("iat")
        if not isinstance(exp, (int, float)) or isinstance(exp, bool) or exp <= now - SKEW:
            raise AuthError("The ID token has expired", 403)
        if not isinstance(iat, (int, float)) or isinstance(iat, bool) or iat > now + SKEW:
            raise AuthError("The ID token is issued in the future", 403)
        if not isinstance(claims.get("nonce"), str) or not secrets.compare_digest(
            claims["nonce"], nonce
        ):
            raise AuthError("The ID token's nonce does not match this sign-in", 403)
        if not isinstance(claims.get("sub"), str) or not claims["sub"]:
            raise AuthError("The ID token names no subject", 403)
        return claims

    def has_role(self, claims: dict) -> bool:
        access = claims.get("resource_access")
        client = access.get(self.config.client_id) if isinstance(access, dict) else None
        roles = client.get("roles") if isinstance(client, dict) else None
        return isinstance(roles, list) and self.config.role in roles

    def logout_url(self, origin: str) -> str:
        """Where the page goes to end the identity provider's session too."""
        try:
            end = self.metadata().get("end_session_endpoint")
        except AuthError:
            end = None
        if not end:
            return "/"
        query = urlencode(
            {"client_id": self.config.client_id, "post_logout_redirect_uri": origin + "/"}
        )
        return f"{end}{'&' if '?' in end else '?'}{query}"


class Store:
    """auth.sqlite: sessions, keys, break-glass links and the would-refuse report."""

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS sessions(
        digest TEXT PRIMARY KEY, sub TEXT NOT NULL, username TEXT, via TEXT NOT NULL,
        created REAL NOT NULL, last_seen REAL NOT NULL, idle_expiry REAL NOT NULL,
        absolute_expiry REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS keys(
        id TEXT PRIMARY KEY, digest TEXT UNIQUE NOT NULL, kind TEXT NOT NULL,
        scopes TEXT NOT NULL, binding TEXT, label TEXT, created REAL NOT NULL,
        expires REAL, last_used REAL, revoked_at REAL);
    CREATE TABLE IF NOT EXISTS login_links(
        digest TEXT PRIMARY KEY, created REAL NOT NULL, expires REAL NOT NULL, used REAL);
    CREATE TABLE IF NOT EXISTS report(
        method TEXT NOT NULL, route TEXT NOT NULL, credential TEXT NOT NULL, reason TEXT,
        count INTEGER NOT NULL, first REAL NOT NULL, last REAL NOT NULL,
        PRIMARY KEY(method, route, credential));
    """

    def __init__(self, home: Path, clock: Callable[[], float] = time.time):
        self.home = ensure_home(home)
        self.path = home / "auth.sqlite"
        self.clock = clock
        self.lock = threading.Lock()
        # Live sessions only: a miss is never cached, so nothing waits on a stale miss.
        self.cache: dict[str, tuple[dict, float]] = {}
        if not self.path.exists():
            os.close(os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600))
        info = os.lstat(self.path)
        if stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError(f"{self.path} must be a regular file of this user")
        if info.st_mode & 0o077:
            os.chmod(self.path, 0o600)
        with self.db() as db:
            db.executescript(self.SCHEMA)

    @contextlib.contextmanager
    def db(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    # Sessions

    def create_session(self, sub, username, via, idle=SESSION_IDLE, absolute=SESSION_ABSOLUTE):
        token = secrets.token_urlsafe(32)
        now = self.clock()
        with self.db() as db:
            db.execute(
                "INSERT INTO sessions VALUES(?,?,?,?,?,?,?,?)",
                (digest(token), sub, username, via, now, now, now + idle, now + absolute),
            )
        return token

    def session(self, token: str | None) -> dict | None:
        """The live session ``token`` names; idle time restarts at most every few minutes.

        A row is cached for a minute, so a revocation by another process (the CLI) takes
        effect within a minute, or at once when the session's idle time is next renewed.
        """
        if not isinstance(token, str) or not TOKEN.fullmatch(token):
            return None
        key, now = digest(token), self.clock()
        with self.lock:
            cached = self.cache.get(key)
        if cached and now - cached[1] < CACHE_SECONDS:
            row = cached[0]
        else:
            with self.db() as db:
                found = db.execute("SELECT * FROM sessions WHERE digest=?", (key,)).fetchone()
            if found is None:
                with self.lock:
                    self.cache.pop(key, None)
                return None
            row = dict(found)
            with self.lock:
                if len(self.cache) > 1000:
                    self.cache = {k: v for k, v in self.cache.items() if now - v[1] < CACHE_SECONDS}
                self.cache[key] = (row, now)
        if now >= row["idle_expiry"] or now >= row["absolute_expiry"]:
            self.forget_session(key)
            return None
        if now - row["last_seen"] >= TOUCH_EVERY:
            idle = row["idle_expiry"] - row["last_seen"]
            row = {**row, "last_seen": now, "idle_expiry": min(now + idle, row["absolute_expiry"])}
            with self.db() as db:
                changed = db.execute(
                    "UPDATE sessions SET last_seen=?, idle_expiry=? WHERE digest=?",
                    (row["last_seen"], row["idle_expiry"], key),
                ).rowcount
            with self.lock:
                if changed:
                    self.cache[key] = (row, now)
                else:
                    # Revoked since it was cached.
                    self.cache.pop(key, None)
            if not changed:
                return None
        return row

    def forget_session(self, key: str) -> None:
        with self.db() as db:
            db.execute("DELETE FROM sessions WHERE digest=?", (key,))
        with self.lock:
            self.cache.pop(key, None)

    def end_session(self, token: str | None) -> None:
        if isinstance(token, str) and TOKEN.fullmatch(token):
            self.forget_session(digest(token))

    def sessions(self) -> list[dict]:
        now = self.clock()
        with self.db() as db:
            db.execute(
                "DELETE FROM sessions WHERE idle_expiry<=? OR absolute_expiry<=?", (now, now)
            )
            rows = db.execute("SELECT * FROM sessions ORDER BY created").fetchall()
        return [
            {"id": row["digest"][:12], **{k: row[k] for k in row.keys() if k != "digest"}}
            for row in rows
        ]

    def revoke_sessions(self, session_id: str | None = None) -> int:
        """End one session by its listed id, or every session; a cached one within a minute."""
        with self.db() as db:
            if session_id is None:
                count = db.execute("DELETE FROM sessions").rowcount
            else:
                if not re.fullmatch(r"[0-9a-f]{12}", session_id):
                    raise ValueError("Give a session id from `auth sessions list`")
                count = db.execute(
                    "DELETE FROM sessions WHERE substr(digest, 1, 12)=?", (session_id,)
                ).rowcount
        with self.lock:
            self.cache.clear()
        return count

    # Keys

    def create_key(self, kind, scopes=None, binding=None, label=None, lifetime=None):
        """A new key, returned once as ``(id, key)``; only its digest is kept."""
        if kind not in KINDS:
            raise ValueError(f"Unknown key kind {kind!r}; use one of {', '.join(KINDS)}")
        scopes = check_scopes(list(POLICY[kind]) if scopes is None else scopes)
        token = f"bsk_{kind}_{secrets.token_urlsafe(32)}"
        key_id = f"k_{secrets.token_hex(6)}"
        now = self.clock()
        with self.db() as db:
            db.execute(
                "INSERT INTO keys(id, digest, kind, scopes, binding, label, created, expires)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (
                    key_id,
                    digest(token),
                    kind,
                    json.dumps(scopes),
                    json.dumps(binding) if binding is not None else None,
                    clean_text(label),
                    now,
                    now + lifetime if lifetime else None,
                ),
            )
        return key_id, token

    def key(self, token: str) -> dict | None:
        """The live key ``token`` is; None when unknown, revoked or expired."""
        if not KEY.fullmatch(token or ""):
            return None
        now = self.clock()
        with self.db() as db:
            row = db.execute(
                "SELECT * FROM keys WHERE digest=? AND revoked_at IS NULL"
                " AND (expires IS NULL OR expires>?)",
                (digest(token), now),
            ).fetchone()
            if row is not None and (row["last_used"] or 0) <= now - TOUCH_EVERY:
                db.execute("UPDATE keys SET last_used=? WHERE id=?", (now, row["id"]))
        if row is None:
            return None
        value = dict(row)
        value["scopes"] = json.loads(value["scopes"])
        value["binding"] = json.loads(value["binding"]) if value["binding"] else None
        return value

    def keys(self, include_revoked=False) -> list[dict]:
        with self.db() as db:
            rows = db.execute(
                "SELECT * FROM keys"
                + ("" if include_revoked else " WHERE revoked_at IS NULL")
                + " ORDER BY created"
            ).fetchall()
        return [
            {
                **{k: row[k] for k in row.keys() if k not in {"digest", "scopes", "binding"}},
                "scopes": json.loads(row["scopes"]),
                "binding": json.loads(row["binding"]) if row["binding"] else None,
            }
            for row in rows
        ]

    def revoke_key(self, key_id: str) -> None:
        with self.db() as db:
            changed = db.execute(
                "UPDATE keys SET revoked_at=? WHERE id=? AND revoked_at IS NULL",
                (self.clock(), key_id),
            ).rowcount
        if not changed:
            raise ValueError(f"No live key {key_id}")

    # Break-glass links

    def create_link(self, ttl=LINK_TTL) -> str:
        token = secrets.token_urlsafe(32)
        now = self.clock()
        with self.db() as db:
            db.execute("DELETE FROM login_links WHERE expires<=?", (now,))
            db.execute(
                "INSERT INTO login_links(digest, created, expires) VALUES(?,?,?)",
                (digest(token), now, now + ttl),
            )
        return token

    def redeem_link(self, token) -> bool:
        """Whether ``token`` is an unused, unexpired link, which it no longer is after."""
        if not isinstance(token, str) or not TOKEN.fullmatch(token):
            return False
        with self.db() as db:
            return (
                db.execute(
                    "UPDATE login_links SET used=? WHERE digest=? AND used IS NULL AND expires>?",
                    (self.clock(), digest(token), self.clock()),
                ).rowcount
                == 1
            )


@dataclass(frozen=True)
class Decision:
    """How a request is authenticated and whether the policy lets it through.

    ``credential`` is ``public``, ``own`` (its handler checks its own credential),
    ``session``, a key kind, ``anonymous`` or ``invalid`` (a key that is unknown,
    revoked or expired, or a request target that is not a plain path).
    """

    credential: str
    allowed: bool
    reason: str = ""
    subject: str | None = None
    session: dict | None = field(default=None, repr=False)
    key: dict | None = field(default=None, repr=False)

    @property
    def authenticated(self) -> bool:
        return self.credential == "session" or self.credential in KINDS


def bucket(method: str, path: str | None, routes=frozenset()) -> tuple[str, str]:
    """A report row's method and route: one of the dashboard's own ``routes``, else a
    fixed name, so anonymous requests cannot grow the report; never a query string."""
    method = method if method in {"GET", "POST", "OPTIONS"} else "other"
    if path is None:
        return method, "(unexpected target)"
    if path in routes:
        return method, path
    return method, "/api/(other)" if path.startswith("/api/") else "/(other)"


def lifetimes(via: str, secure: bool) -> tuple[float, float]:
    """A new session's idle and absolute lifetimes."""
    idle, absolute = (
        (BREAK_GLASS_LIFETIME, BREAK_GLASS_LIFETIME)
        if via == "break-glass"
        else (SESSION_IDLE, SESSION_ABSOLUTE)
    )
    if not secure:
        idle, absolute = min(idle, LOOPBACK_IDLE), min(absolute, LOOPBACK_ABSOLUTE)
    return idle, absolute


class Report:
    """Would-refuse counts: logged at most once a minute per route and kind, kept in sqlite.

    Nothing secret is recorded: method, route without its query, credential kind and a
    fixed reason.
    """

    def __init__(self, store: Store, clock, log=None):
        self.store = store
        self.clock = clock
        self.log = log or (lambda line: print(line, file=sys.stderr, flush=True))
        self.lock = threading.Lock()
        self.pending: dict[tuple[str, str, str], dict] = {}
        self.logged: dict[tuple[str, str, str], float] = {}

    def record(
        self, method: str, path: str | None, decision: Decision, refused=False, routes=frozenset()
    ) -> None:
        method, route = bucket(method, path, routes)
        key = (method, route, decision.credential)
        now = self.clock()
        with self.lock:
            if len(self.logged) > REPORT_KEYS_MAX:
                self.logged.clear()
            if len(self.pending) > REPORT_KEYS_MAX:
                self._flush()
            entry = self.pending.setdefault(
                key, {"count": 0, "first": now, "reason": decision.reason}
            )
            entry["count"] += 1
            entry["last"] = now
            if now - self.logged.get(key, float("-inf")) < REPORT_LOG_EVERY:
                return
            self.logged[key] = now
            count = entry["count"]
            self._flush()
        verb = "refused" if refused else "would refuse"
        self.log(
            f"dashboard auth: {verb} {method} {route} ({decision.credential}: "
            f"{decision.reason}); {count} since the last line"
        )

    def _flush(self) -> None:
        if not self.pending:
            return
        with self.store.db() as db:
            for (method, route, credential), entry in self.pending.items():
                db.execute(
                    "INSERT INTO report VALUES(?,?,?,?,?,?,?) ON CONFLICT(method, route, credential)"
                    " DO UPDATE SET count=count+excluded.count, last=excluded.last,"
                    " reason=excluded.reason",
                    (
                        method,
                        route,
                        credential,
                        entry["reason"],
                        entry["count"],
                        entry["first"],
                        entry["last"],
                    ),
                )
        self.pending = {}

    def rows(self) -> list[dict]:
        with self.lock:
            self._flush()
        with self.store.db() as db:
            rows = db.execute("SELECT * FROM report ORDER BY count DESC, last DESC").fetchall()
        return [dict(row) for row in rows]

    def total(self) -> int:
        with self.lock:
            pending = sum(entry["count"] for entry in self.pending.values())
        with self.store.db() as db:
            stored = db.execute("SELECT COALESCE(SUM(count), 0) FROM report").fetchone()[0]
        return int(stored) + pending

    def reset(self) -> None:
        with self.lock:
            self.pending, self.logged = {}, {}
            with self.store.db() as db:
                db.execute("DELETE FROM report")


@dataclass
class Pending:
    nonce: str
    verifier: str
    next: str
    host: str
    expires: float
    source: str = ""


class Auth:
    """The dashboard's authenticator: one per server."""

    def __init__(
        self,
        home: Path,
        mode: str = "report",
        oidc: OidcConfig | None = None,
        clock: Callable[[], float] | None = None,
        log=None,
    ):
        if mode not in ("report", "enforce"):
            raise ValueError(f"Auth mode must be report or enforce, not {mode!r}")
        if mode == "enforce" and SELF_AUTHENTICATED and not os.environ.get(ALLOW_OWN_ROUTES_ENV):
            raise ValueError(
                "Enforce mode needs Phase 2 (worker, extension and push tokens in the auth "
                "store) and Phase 3 (the state home out of Safehouse's write grants): until "
                "then anything that can write the state home can mint those tokens"
            )
        self.mode = mode
        self.clock = clock or time.time
        self.store = Store(home, lambda: self.clock())
        self.oidc = Oidc(oidc, lambda: self.clock()) if oidc else None
        self.report = Report(self.store, lambda: self.clock(), log)
        self.lock = threading.Lock()
        self.pending: dict[str, Pending] = {}

    @property
    def enforce(self) -> bool:
        return self.mode == "enforce"

    def session_token(self, headers, secure: bool) -> str | None:
        """The session cookie of this scheme only: on https, never a plain-http one."""
        return read_cookie(headers.get("Cookie"), session_cookie_name(secure))

    def decide(self, method: str, target: str, headers, secure: bool = False) -> Decision:
        path = request_path(target)
        if path is None:
            return Decision("invalid", False, "unexpected request target")
        if public(method, path):
            return Decision("public", True)
        authorization = headers.get("Authorization") or ""
        if authorization.startswith("Bearer bsk_"):
            key = self.store.key(authorization.removeprefix("Bearer "))
            if key is None:
                return Decision("invalid", False, "unknown, revoked or expired key")
            allowed = any(scope_allows(scope, method, path) for scope in key["scopes"])
            return Decision(
                key["kind"],
                allowed,
                "" if allowed else "key scope does not cover this route",
                subject=key["id"],
                key=key,
            )
        session = self.store.session(self.session_token(headers, secure))
        if session is not None:
            return Decision("session", True, subject=session["username"], session=session)
        if any(scope_allows(scope, method, path) for scope in SELF_AUTHENTICATED):
            return Decision("own", True)
        return Decision("anonymous", False, "no session or key")

    # OIDC sign-in

    def redirect_uri(self, host: str | None) -> str:
        assert self.oidc is not None
        uri = self.oidc.config.redirect_uris.get(host or "")
        if uri is None:
            raise AuthError("Sign-in is not configured for this host", 403)
        return uri

    def begin_login(self, host: str | None, next_path, source: str = "") -> tuple[str, str]:
        """The identity provider URL to send the browser to, and the state to bind it."""
        if self.oidc is None:
            raise AuthError(
                "OIDC sign-in is not configured; use `pr_supervisor.py auth login-link`", 404
            )
        redirect = self.redirect_uri(host)
        state, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        url = self.oidc.authorize_url(redirect, state, nonce, verifier)
        now = self.clock()
        with self.lock:
            self.pending = {k: v for k, v in self.pending.items() if v.expires > now}
            # A flood evicts its own source's sign-ins first, never a quiet user's.
            mine = [k for k, v in self.pending.items() if v.source == source]
            while len(mine) >= PENDING_PER_SOURCE:
                self.pending.pop(mine.pop(0))
            while len(self.pending) >= PENDING_MAX:
                counts: dict[str, int] = {}
                for v in self.pending.values():
                    counts[v.source] = counts.get(v.source, 0) + 1
                busiest = max(counts, key=lambda k: counts[k])
                self.pending.pop(next(k for k, v in self.pending.items() if v.source == busiest))
            self.pending[state] = Pending(
                nonce, verifier, safe_next(next_path), host or "", now + LOGIN_TTL, source
            )
        return url, state

    def finish_login(self, host, state, code, bound_state, secure=False) -> tuple[str, dict]:
        """A new session's cookie value and row for a callback; each state works once."""
        if self.oidc is None:
            raise AuthError("OIDC sign-in is not configured", 404)
        if (
            not isinstance(state, str)
            or not isinstance(bound_state, str)
            or not secrets.compare_digest(state.encode(), bound_state.encode())
        ):
            raise AuthError("This sign-in was not started in this browser; sign in again")
        with self.lock:
            pending = self.pending.pop(state, None)
        if pending is None or pending.expires <= self.clock() or pending.host != (host or ""):
            raise AuthError("This sign-in has expired or was already used; sign in again")
        if not isinstance(code, str) or not code or len(code) > 4096:
            raise AuthError("The identity provider sent no authorization code")
        redirect = self.redirect_uri(host)
        claims = self.oidc.validate(
            self.oidc.exchange(code, redirect, pending.verifier), pending.nonce
        )
        if not self.oidc.has_role(claims):
            raise AuthError(
                f"Your account lacks the {self.oidc.config.role} role on "
                f"{self.oidc.config.client_id}",
                403,
            )
        username = (
            clean_text(claims.get("preferred_username"))
            or clean_text(claims.get("email"))
            or clean_text(claims["sub"])
        )
        idle, absolute = lifetimes("oidc", secure)
        token = self.store.create_session(claims["sub"], username, "oidc", idle, absolute)
        return token, {"next": pending.next, "username": username, "max_age": absolute}

    def redeem_link(self, token, secure=False) -> tuple[str, float]:
        """A break-glass session's cookie value and lifetime for an unused link."""
        if not self.store.redeem_link(token):
            raise AuthError("This sign-in link has expired or was already used", 403)
        idle, absolute = lifetimes("break-glass", secure)
        value = self.store.create_session(
            "break-glass", "break-glass", "break-glass", idle, absolute
        )
        return value, absolute

    def logout(self, headers, origin: str, secure=False) -> str:
        """End the request's session; where the page goes next."""
        self.store.end_session(self.session_token(headers, secure))
        return self.oidc.logout_url(origin) if self.oidc else "/"

    def view(self, headers, secure=False) -> dict:
        """What /api/session tells the page."""
        decision = self.decide("GET", "/api/status", headers, secure)
        session = decision.session
        return {
            "enabled": True,
            "mode": self.mode,
            "authenticated": decision.authenticated,
            "credential": decision.credential if decision.authenticated else None,
            "user": session["username"] if session else decision.subject,
            "via": session["via"] if session else None,
            "expires": min(session["idle_expiry"], session["absolute_expiry"]) if session else None,
            "login": "/auth/login" if self.oidc else None,
            "would_deny": self.report.total(),
        }

    def summary(self) -> dict:
        return {"mode": self.mode, "would_deny": self.report.total()}


def login_page(message: str, status: int) -> bytes:
    """A small page for a failed sign-in step; no script or style, so the CSP holds."""
    retry = '<p><a href="/auth/login">Sign in again</a> · <a href="/">Dashboard</a></p>'
    return (
        "<!doctype html><meta charset=utf-8><title>Babysitter sign-in</title>"
        f"<h1>Sign-in failed ({status})</h1><p>{html.escape(message)}</p>{retry}"
    ).encode()


def load(mode: str, home: Path, oidc_path: Path | None = None, log=None) -> Auth | None:
    """The configured authenticator, or None while auth is off.

    ``report`` (the default) turns on only once ``oidc.json`` exists, so deploying is a
    no-op until then; a broken configuration is reported and leaves auth off rather
    than stop the dashboard. ``enforce`` always turns on and fails closed.
    """
    say = log or (lambda line: print(line, file=sys.stderr, flush=True))
    if mode == "off":
        return None
    if mode not in MODES:
        raise ValueError(f"Unknown auth mode {mode!r}")
    path = oidc_path or home / OIDC_FILE
    config = None
    try:
        config = OidcConfig.load(path)
        if path.stat().st_mode & 0o077:
            say(f"Warning: {path} is readable by other users; chmod 600 it")
    except FileNotFoundError:
        if mode == "report":
            return None
    except (OSError, ValueError) as exc:
        if mode == "enforce":
            raise
        say(f"Dashboard auth stays off: {exc}")
        return None
    if config is not None:
        try:
            import cryptography  # noqa: F401
        except ImportError:
            if mode == "enforce":
                raise RuntimeError("Dashboard sign-in needs the cryptography package") from None
            say("Warning: the cryptography package is missing, so OIDC sign-in will fail")
    return Auth(home, mode, config, log=log)


def add_arguments(parser) -> None:
    """The dashboard's auth options, shared by dashboard.py and pr_supervisor.py."""
    parser.add_argument(
        "--auth",
        choices=MODES,
        default="report",
        help="Sign-in: report counts what enforce would refuse (default; on once oidc.json "
        "exists), enforce refuses it, off checks nothing",
    )
    parser.add_argument(
        "--auth-home",
        type=Path,
        default=DEFAULT_HOME,
        help=f"Directory of auth.sqlite, admin-key and oidc.json (default: {DEFAULT_HOME})",
    )
    parser.add_argument(
        "--oidc-config", type=Path, help="OIDC client settings (default: AUTH_HOME/oidc.json)"
    )


# CLI helpers (pr_supervisor.py auth ...)


def admin_key(home: Path, rotate=False) -> dict:
    """The local admin key file, created when missing or replaced with ``rotate``."""
    store = Store(home)
    path = home / ADMIN_KEY_FILE
    current = [k for k in store.keys() if k["kind"] == "admin" and k["label"] == ADMIN_KEY_FILE]
    if path.exists() and current and not rotate:
        return {"path": str(path), "id": current[-1]["id"], "created": False}
    key_id, token = store.create_key("admin", label=ADMIN_KEY_FILE)
    write_secret(path, token + "\n")
    for old in current:
        store.revoke_key(old["id"])
    return {"path": str(path), "id": key_id, "created": True, "revoked": [k["id"] for k in current]}


def launch_binding(**fields) -> dict:
    """What a launch key binds: who started the agent, where, and how it may launch more.

    ``origin`` tells agents the user started (``user``) from cron runs and their
    sub-tasks (``cron``, ``subtask``), which get tighter launch limits (Phase 2/3).
    """
    unknown = set(fields) - LAUNCH_FIELDS
    if unknown:
        raise ValueError(f"Unknown launch binding fields {sorted(unknown)}")
    if fields.get("origin") not in LAUNCH_ORIGINS:
        raise ValueError(f"origin must be one of {', '.join(LAUNCH_ORIGINS)}")
    if not isinstance(fields.get("cwd"), str) or not fields["cwd"].startswith("/"):
        raise ValueError("cwd must be an absolute path")
    if fields.get("agent") not in {"claude", "codex"}:
        raise ValueError("agent must be claude or codex")
    if not isinstance(fields.get("docker", False), bool):
        raise ValueError("docker must be true or false")
    depth = fields.get("depth", 0)
    if type(depth) is not int or depth < 0:
        raise ValueError("depth must be a non-negative integer")
    return {"sandbox": "safehouse", "docker": False, "depth": 0, **fields}


def mint_launch_key(store: Store, **fields) -> tuple[str, str]:
    binding = launch_binding(**fields)
    return store.create_key(
        "launch",
        binding=binding,
        label=f"{binding['origin']} {binding['agent']} in {binding['cwd']}",
        lifetime=LAUNCH_KEY_LIFETIME,
    )


def link_origins(home: Path, port: int) -> list[str]:
    """Each dashboard origin a break-glass link can open: oidc.json's hosts, then loopback."""
    origins = []
    with contextlib.suppress(OSError, ValueError):
        for uri in OidcConfig.load(home / OIDC_FILE).redirect_uris.values():
            parts = urlsplit(uri)
            origins.append(f"{parts.scheme}://{parts.netloc}")
    loopback = f"http://127.0.0.1:{port}"
    return [o for o in origins if o != loopback] + [loopback]


def login_link(home: Path, port: int = 8765) -> dict:
    token = Store(home).create_link()
    return {
        "expires_in": LINK_TTL,
        "links": [f"{origin}/auth/break-glass#t={token}" for origin in link_origins(home, port)],
    }


def add_cli(sub) -> None:
    """``pr_supervisor.py auth ...``: the auth store's command line."""
    parser = sub.add_parser(
        "auth", help="Dashboard sign-in: keys, sessions, break-glass links and the report"
    )
    parser.add_argument("--auth-home", type=Path, default=DEFAULT_HOME)
    commands = parser.add_subparsers(dest="auth_command", required=True)
    commands.add_parser("status", help="Mode inputs, live keys and sessions, would-refuse counts")
    admin = commands.add_parser("admin-key", help="Create the local admin key file (0600)")
    admin.add_argument("--rotate", action="store_true", help="Replace it; the old key stops")
    keys = commands.add_parser("keys", help="List, create or revoke scoped keys")
    key_commands = keys.add_subparsers(dest="keys_command", required=True)
    key_commands.add_parser("list").add_argument(
        "--all", action="store_true", help="Include revoked keys"
    )
    create = key_commands.add_parser("create", help="Print a new key once; only its digest is kept")
    create.add_argument("--kind", choices=KINDS, required=True)
    create.add_argument(
        "--scope",
        action="append",
        help="[METHOD ]/path or /prefix*, repeatable (default: the kind's policy)",
    )
    create.add_argument("--expires-days", type=float)
    create.add_argument("--label")
    key_commands.add_parser("revoke").add_argument("id")
    sessions = commands.add_parser("sessions", help="List or end browser sessions")
    session_commands = sessions.add_subparsers(dest="sessions_command", required=True)
    session_commands.add_parser("list")
    revoke = session_commands.add_parser("revoke")
    revoke.add_argument("id", nargs="?")
    revoke.add_argument("--all", action="store_true")
    link = commands.add_parser(
        "login-link", help="Print a one-time sign-in link, valid 10 minutes, that needs no OIDC"
    )
    link.add_argument("--port", type=int, default=8765, help="The loopback dashboard's port")
    launch = commands.add_parser(
        "launch-key", help="Mint a launch key for an agent started outside the dashboard"
    )
    launch.add_argument("--cwd", default=os.getcwd())
    launch.add_argument("--agent", choices=["claude", "codex"], required=True)
    launch.add_argument("--docker", action="store_true")
    launch.add_argument("--origin", choices=LAUNCH_ORIGINS, default="user")
    launch.add_argument("--config-dir")
    launch.add_argument("--claude-account")
    report = commands.add_parser("report", help="What enforce would refuse, by route and kind")
    report.add_argument("--reset", action="store_true", help="Start counting afresh")


def cli(args) -> Any:
    """Run one ``auth`` command; its JSON-able answer. Errors are ValueError or OSError."""
    home = args.auth_home.expanduser()
    command = args.auth_command
    if command == "admin-key":
        return admin_key(home, args.rotate)
    if command == "login-link":
        return login_link(home, args.port)
    store = Store(home)
    report = Report(store, time.time)
    if command == "status":
        keys = store.keys()
        return {
            "auth_home": str(home),
            "oidc": (home / OIDC_FILE).exists(),
            "admin_key": (home / ADMIN_KEY_FILE).exists(),
            "keys": {kind: sum(k["kind"] == kind for k in keys) for kind in KINDS},
            "sessions": len(store.sessions()),
            "would_deny": report.rows(),
        }
    if command == "keys":
        if args.keys_command == "list":
            return store.keys(include_revoked=args.all)
        if args.keys_command == "revoke":
            store.revoke_key(args.id)
            return {"id": args.id, "revoked": True}
        if args.kind == "launch":
            raise ValueError("Mint launch keys with `auth launch-key`, which records the binding")
        if args.expires_days is not None and args.expires_days <= 0:
            raise ValueError("--expires-days must be positive")
        key_id, token = store.create_key(
            args.kind,
            args.scope,
            label=args.label,
            lifetime=args.expires_days * 86400 if args.expires_days else None,
        )
        answer = {"id": key_id, "key": token, "note": "Shown once; only its digest is stored"}
        loose = [
            scope
            for scope in args.scope or []
            if scope.endswith("*") and not scope.endswith("/*") and scope.rpartition(" ")[2] != "*"
        ]
        if loose:
            answer["warning"] = (
                f"{', '.join(loose)}: a prefix not ending in /* also matches longer route "
                "names (/api/push* covers /api/push-subscribe)"
            )
        return answer
    if command == "sessions":
        if args.sessions_command == "list":
            return store.sessions()
        if bool(args.id) == bool(args.all):
            raise ValueError("Give a session id or --all")
        return {"revoked": store.revoke_sessions(None if args.all else args.id)}
    if command == "launch-key":
        fields = {
            "origin": args.origin,
            "cwd": os.path.realpath(args.cwd),
            "agent": args.agent,
            "docker": args.docker,
        }
        if args.config_dir:
            fields["config_dir"] = os.path.realpath(args.config_dir)
        if args.claude_account:
            fields["claude_account"] = args.claude_account
        key_id, token = mint_launch_key(store, **fields)
        return {"id": key_id, "key": token, "expires_in": LAUNCH_KEY_LIFETIME}
    if command == "report":
        if args.reset:
            report.reset()
            return {"reset": True}
        return report.rows()
    raise ValueError(f"Unknown auth command {command}")
