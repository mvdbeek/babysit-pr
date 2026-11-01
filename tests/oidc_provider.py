"""An in-process OpenID provider for tests: discovery, JWKS, authorize, token and logout.

It runs on a free loopback port, signs ID tokens with RSA keys made by ``cryptography``,
checks the client secret, the redirect URI and the PKCE verifier, and lets a test
rotate keys or bend any claim. It never contacts a real identity provider.
"""

import base64
import hashlib
import json
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit

from cryptography.hazmat.primitives import hashes, hmac
from cryptography.hazmat.primitives.asymmetric import padding, rsa

CLIENT_ID = "babysitter"
CLIENT_SECRET = "fixture-client-secret"
ROLE = "babysitter-admin"
_KEYS: list = []


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def rsa_key(number):
    """The ``number``th 2048-bit key; each is made once per process, as it takes a moment."""
    while len(_KEYS) <= number:
        _KEYS.append(rsa.generate_private_key(public_exponent=65537, key_size=2048))
    return _KEYS[number]


def jwk(kid, key):
    numbers = key.public_key().public_numbers()
    return {
        "kty": "RSA",
        "kid": kid,
        "use": "sig",
        "alg": "RS256",
        "n": b64url(numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")),
        "e": b64url(numbers.e.to_bytes(3, "big")),
    }


class Provider(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.issuer = f"http://127.0.0.1:{self.server_port}/realms/test"
        self.keys = {"key-1": rsa_key(0)}
        self.rotations = 0
        self.signing = "key-1"
        self.codes: dict[str, dict] = {}
        self.claims: dict = {}  # Merged into every ID token; None values are removed.
        self.header: dict = {}  # Merged into every JWS header.
        self.sign_with: str | None = None  # "none" or "HS256" to forge a token.
        self.discovery_issuer: str | None = None
        self.user = {"sub": "user-1", "preferred_username": "alice", "roles": [ROLE]}
        self.jwks_requests = 0
        self.token_requests = 0
        self.thread = threading.Thread(target=self.serve_forever, args=(0.05,), daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.shutdown()
        self.thread.join(5)
        self.server_close()

    def rotate(self, kid):
        """Sign with a new key; the JWKS then serves only that key."""
        self.rotations += 1
        self.keys = {kid: rsa_key(self.rotations)}
        self.signing = kid

    def id_token(self, nonce):
        now = int(time.time())
        claims = {
            "iss": self.issuer,
            "aud": CLIENT_ID,
            "azp": CLIENT_ID,
            "sub": self.user["sub"],
            "preferred_username": self.user["preferred_username"],
            "iat": now,
            "exp": now + 300,
            "nonce": nonce,
            "resource_access": {CLIENT_ID: {"roles": self.user["roles"]}},
        }
        claims.update(self.claims)
        claims = {k: v for k, v in claims.items() if v is not None}
        alg = self.sign_with or "RS256"
        header = {"alg": alg, "typ": "JWT", "kid": self.signing, **self.header}
        signed = f"{b64url(json.dumps(header).encode())}.{b64url(json.dumps(claims).encode())}"
        if alg == "none":
            signature = b""
        elif alg == "HS256":
            mac = hmac.HMAC(CLIENT_SECRET.encode(), hashes.SHA256())
            mac.update(signed.encode())
            signature = mac.finalize()
        else:
            signature = self.keys[self.signing].sign(
                signed.encode(), padding.PKCS1v15(), hashes.SHA256()
            )
        return f"{signed}.{b64url(signature)}"


class _Handler(BaseHTTPRequestHandler):
    server: Provider

    def log_message(self, *args):
        pass

    def reply(self, code, value=None, location=None):
        body = json.dumps(value).encode() if value is not None else b""
        self.send_response(code)
        if location:
            self.send_header("Location", location)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        provider = self.server
        route = urlsplit(self.path)
        base = provider.issuer
        query = {k: v[0] for k, v in parse_qs(route.query).items()}
        if route.path == "/realms/test/.well-known/openid-configuration":
            self.reply(
                200,
                {
                    "issuer": provider.discovery_issuer or base,
                    "authorization_endpoint": f"{base}/auth",
                    "token_endpoint": f"{base}/token",
                    "jwks_uri": f"{base}/certs",
                    "end_session_endpoint": f"{base}/logout",
                },
            )
        elif route.path == "/realms/test/certs":
            provider.jwks_requests += 1
            self.reply(200, {"keys": [jwk(kid, key) for kid, key in provider.keys.items()]})
        elif route.path == "/realms/test/auth":
            # Signs the user straight in, as a provider with a live SSO session does.
            if (
                query.get("client_id") != CLIENT_ID
                or query.get("response_type") != "code"
                or query.get("code_challenge_method") != "S256"
            ):
                self.reply(400, {"error": "invalid_request"})
                return
            code = secrets.token_urlsafe(16)
            provider.codes[code] = {
                "nonce": query["nonce"],
                "challenge": query["code_challenge"],
                "redirect_uri": query["redirect_uri"],
                "used": False,
            }
            target = (
                query["redirect_uri"] + "?" + urlencode({"code": code, "state": query["state"]})
            )
            self.reply(302, location=target)
        elif route.path == "/realms/test/logout":
            self.reply(302, location=query.get("post_logout_redirect_uri", "/"))
        else:
            self.reply(404, {"error": "not_found"})

    def do_POST(self):
        provider = self.server
        if urlsplit(self.path).path != "/realms/test/token":
            self.reply(404, {"error": "not_found"})
            return
        provider.token_requests += 1
        expected = "Basic " + base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
        if self.headers.get("Authorization") != expected:
            self.reply(401, {"error": "invalid_client"})
            return
        length = int(self.headers.get("Content-Length", "0"))
        form = {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode()).items()}
        grant = provider.codes.get(form.get("code", ""))
        verifier = form.get("code_verifier", "")
        if (
            form.get("grant_type") != "authorization_code"
            or grant is None
            or grant["used"]
            or grant["redirect_uri"] != form.get("redirect_uri")
            or b64url(hashlib.sha256(verifier.encode()).digest()) != grant["challenge"]
        ):
            if grant is not None:
                grant["used"] = True
            self.reply(400, {"error": "invalid_grant"})
            return
        grant["used"] = True
        self.reply(
            200,
            {
                "access_token": "fixture-access-token",
                "refresh_token": "fixture-refresh-token",
                "token_type": "Bearer",
                "id_token": provider.id_token(grant["nonce"]),
            },
        )
