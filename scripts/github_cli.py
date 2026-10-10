"""Bounded, non-interactive GitHub CLI invocations that read the keyring once.

`gh` keeps its token in the system keyring and reads it back on every invocation,
which on macOS means spawning `/usr/bin/security` and waiting on the login
Keychain. The supervisor and dashboard issue many gh calls at once; a stalled
keyring read leaves each of those processes parked on the Keychain, and the
user's own gh and gpg (pinentry-mac fetches the signing passphrase from the same
Keychain) queue up behind them. This module resolves the token once through
`gh auth token`, passes it to each child as GH_TOKEN so gh never opens the
keyring, keeps git and gh from prompting, and caps concurrent gh processes.

Without a token gh would fall back to unauthenticated requests, each still
opening the keyring first and burning the per-IP rate limit, so no gh process is
started until a token is known; callers get one clear failure instead.

The token lives only in memory and in child process environments; it is never
written to state files or logs. Callers must keep `env` out of error messages.
"""

import contextlib
import os
import re
import subprocess
import threading
import time
from collections.abc import Iterator, Mapping

import owned_process

LOOKUP_TIMEOUT = 20
RETRY_AFTER = 60
TOKEN_TTL = 6 * 3600
MAX_CONCURRENT = 4
TOKEN = re.compile(r"^[A-Za-z0-9_.-]{20,512}$")
UNAUTHENTICATED = ("HTTP 401", "Requires authentication", "gh auth login")

# Unattended git/gh must fail fast rather than wait on a terminal or dialog.
NON_INTERACTIVE = {
    "GH_PROMPT_DISABLED": "1",
    "GH_NO_UPDATE_NOTIFIER": "1",
    "GIT_TERMINAL_PROMPT": "0",
    "GCM_INTERACTIVE": "never",
}


class CredentialsUnavailable(OSError):
    """No GitHub token is readable where this process runs."""


class Credentials:
    """One keyring read per process, refreshed on expiry or after a 401."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self._token: str | None = None
        self._read_at = 0.0
        self._retry_at = 0.0
        self.reason = "not looked up yet"

    def token(self) -> str | None:
        env_token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        if env_token:
            return env_token
        with self.lock:
            now = time.monotonic()
            if self._token and now - self._read_at < TOKEN_TTL:
                return self._token
            if now < self._retry_at:
                return self._token
            token, self.reason = self._lookup()
            if token:
                self._token, self._read_at = token, now
            else:
                self._retry_at = now + RETRY_AFTER
            return self._token

    def explain(self) -> str:
        return (
            f"GitHub CLI has no usable token in this environment (gh auth token: {self.reason}). "
            "Run `gh auth login` where the babysitter runs; a launchd service or sandbox that "
            "cannot read the Keychain needs `gh auth login --insecure-storage` or GH_TOKEN."
        )

    def forget(self, token: str | None) -> None:
        """Drop a token GitHub rejected so the next call re-reads the keyring once."""
        with self.lock:
            if token and token == self._token:
                self._token = None
                self._retry_at = 0.0

    @staticmethod
    def _lookup() -> tuple[str | None, str]:
        try:
            result = owned_process.run(
                ["gh", "auth", "token", "--hostname", "github.com"],
                text=True,
                timeout=LOOKUP_TIMEOUT,
                env={**os.environ, **NON_INTERACTIVE},
            )
        except FileNotFoundError:
            return None, "gh not found on PATH"
        except subprocess.TimeoutExpired:
            return None, f"no answer within {LOOKUP_TIMEOUT} seconds; the keyring may be stuck"
        except OSError as exc:
            return None, str(exc)
        if result.returncode:
            return None, result.stderr.strip()[:200] or f"exit status {result.returncode}"
        token = result.stdout.strip()
        if not TOKEN.fullmatch(token):
            return None, "output was not a token"
        return token, "ok"


CREDENTIALS = Credentials()
SLOTS = threading.BoundedSemaphore(MAX_CONCURRENT)


def environment(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """A child environment for git or gh: non-interactive, with the cached token.

    git commands still run without a token; they only need it for the gh credential
    helper, and a plain git failure explains itself.
    """
    env = dict(os.environ if base is None else base)
    env.update(NON_INTERACTIVE)
    token = CREDENTIALS.token()
    if token:
        env["GH_TOKEN"] = token
    return env


def _require_token(base: Mapping[str, str] | None) -> dict[str, str]:
    env = environment(base)
    if not env.get("GH_TOKEN"):
        raise CredentialsUnavailable(CREDENTIALS.explain())
    return env


def unauthenticated(result: subprocess.CompletedProcess) -> bool:
    if not result.returncode:
        return False
    text = ""
    for stream in (result.stderr, result.stdout):
        if isinstance(stream, bytes):
            text += stream.decode("utf-8", errors="replace")
        elif stream:
            text += stream
    return any(marker in text for marker in UNAUTHENTICATED)


def run(args, *, timeout, env=None, **kwargs) -> subprocess.CompletedProcess:
    """Run one bounded gh command with the shared token and concurrency cap.

    Without a token the command is not started; the result carries the explanation
    the way a failed gh invocation would, so every caller reports it unchanged.
    """
    try:
        env = _require_token(env)
    except CredentialsUnavailable as exc:
        message: str | bytes = str(exc) if kwargs.get("text") else str(exc).encode()
        return subprocess.CompletedProcess(list(args), 1, type(message)(), message)
    with SLOTS:
        result = owned_process.run(list(args), timeout=timeout, env=env, **kwargs)
    if unauthenticated(result):
        CREDENTIALS.forget(env.get("GH_TOKEN"))
    return result


@contextlib.contextmanager
def command(args, env=None, slot=True, **kwargs) -> Iterator[subprocess.Popen]:
    """A streaming gh command; the slot is held until the caller is done with it.

    ``slot=False`` is for a long-running program that is not gh itself, such as a
    helper that only calls gh now and then: it gets the token without blocking gh.
    """
    env = _require_token(env)
    with (
        SLOTS if slot else contextlib.nullcontext(),
        owned_process.command(list(args), env=env, **kwargs) as proc,
    ):
        yield proc
