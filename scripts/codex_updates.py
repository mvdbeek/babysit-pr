"""Whether a newer Codex CLI is published, and its npm install on request.

Codex launched by the babysitter skips its own startup update check (see
``wt.agent_words``), so the dashboard checks instead: at most daily, from the npm
registry, on a background thread. The check needs no login or Keychain, so unlike usage
readings it runs in the dashboard process. A failed check keeps the last known release.
"""

import contextlib
import json
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import cron_jobs
import owned_process
import sentry_llm
import workspace_agents

URL = "https://registry.npmjs.org/@openai/codex/latest"
PACKAGE = "@openai/codex"
STATE = "codex-update.json"
CHECK_SECONDS = 24 * 3600
RETRY_SECONDS = 3600
# The latest manifest is a few kilobytes; anything far larger is not the registry.
MAX_BYTES = 1 << 20
INSTALL_TIMEOUT = 300
ERROR_CHARS = 500
VERSION = re.compile(r"\d+(\.\d+)+")


def parse(version):
    """A comparable release version, or None for anything but dotted integers."""
    if not isinstance(version, str) or not VERSION.fullmatch(version):
        return None
    return tuple(int(part) for part in version.split("."))


def newer(latest, installed):
    """Only two readable versions are compared; a prerelease or unknown CLI shows nothing."""
    a, b = parse(latest), parse(installed)
    return a is not None and b is not None and a > b


def fetch_latest(timeout=10):
    """The version npm's ``latest`` tag names for the Codex package."""
    request = urllib.request.Request(URL, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = response.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise ValueError(f"npm registry answered HTTP {exc.code}") from None
    except (OSError, ValueError) as exc:
        raise ValueError(f"npm registry unavailable: {type(exc).__name__}") from None
    if len(data) > MAX_BYTES:
        raise ValueError("npm registry response exceeded its size limit")
    value = json.loads(data)
    version = value.get("version") if isinstance(value, dict) else None
    # The version becomes part of an install command, so only a plain release is used.
    if parse(version) is None:
        raise ValueError("npm registry named no release version")
    return version


def npm_installed():
    """Whether the ``codex`` on PATH runs from an npm package an npm install replaces."""
    path = shutil.which("codex")
    return path is not None and f"/node_modules/{PACKAGE}/" in os.path.realpath(path)


def read_state(path):
    try:
        saved = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        saved = None
    saved = saved if isinstance(saved, dict) else {}
    checked = saved.get("checked_at")
    error = saved.get("error")
    return {
        "checked_at": checked if isinstance(checked, int | float) else None,
        "latest": saved["latest"] if parse(saved.get("latest")) else None,
        "error": error if isinstance(error, str) else None,
    }


class CodexUpdates:
    """Serve the last check immediately; check and install on background threads."""

    def __init__(self, home, fetch=fetch_latest, shell=None, now=time.time):
        self.path = Path(home) / STATE
        self.fetch = fetch
        # A login shell gives npm the user's PATH even under launchd.
        self.shell = shell or cron_jobs.default_shell()
        self.now = now
        self.lock = threading.Lock()
        self.checker: threading.Thread | None = None
        self.installer: threading.Thread | None = None
        self.last_update: dict | None = None
        # Read back so a restarted dashboard keeps the pace instead of checking at once.
        self.check = read_state(self.path)

    def due(self):
        checked = self.check["checked_at"]
        wait = RETRY_SECONDS if self.check["error"] else CHECK_SECONDS
        # A clock set back must not postpone checks indefinitely.
        return checked is None or not 0 <= self.now() - checked < wait

    def snapshot(self):
        with self.lock:
            if self.due() and not (self.checker and self.checker.is_alive()):
                self.checker = threading.Thread(target=self.refresh, daemon=True)
                self.checker.start()
            check, update = dict(self.check), self.last_update and dict(self.last_update)
        # Outside the lock: it may run ``codex --version``.
        version = workspace_agents.codex_version()
        return {
            "installed": version,
            "latest": check["latest"],
            "available": newer(check["latest"], version),
            "updatable": npm_installed(),
            "checked_at": check["checked_at"],
            "error": check["error"],
            "update": update,
        }

    def refresh(self):
        now = self.now()
        try:
            latest, error = self.fetch(), None
        except Exception as exc:  # Any failure is recorded and paced like a network one.
            latest, error = None, str(exc) or type(exc).__name__
        with self.lock:
            self.check = {
                "checked_at": now,
                "latest": latest or self.check["latest"],
                "error": error,
            }
            value = dict(self.check)
        with contextlib.suppress(OSError):
            sentry_llm.write_private(self.path, value)

    def update(self):
        """Install exactly the release the dashboard showed; ValueError if it cannot."""
        shown = self.snapshot()
        with self.lock:
            if self.last_update and self.last_update["running"]:
                raise ValueError("A Codex update is already running")
            if not shown["available"]:
                raise ValueError("No Codex update is available")
            if not shown["updatable"]:
                raise ValueError("Codex was not installed with npm; update it the way it was")
            version = shown["latest"]
            self.last_update = {
                "running": True,
                "version": version,
                "ok": None,
                "error": None,
                "finished_at": None,
            }
            # Running agents keep the binary they loaded, so they need not stop first.
            self.installer = threading.Thread(target=self.install, args=(version,), daemon=True)
            self.installer.start()

    def install(self, version):
        command = f"npm install -g {shlex.quote(f'{PACKAGE}@{version}')}"
        try:
            done = owned_process.run(
                [*self.shell, command],
                timeout=INSTALL_TIMEOUT,
                stdin=subprocess.DEVNULL,
                text=True,
            )
            output = (done.stdout + done.stderr).strip()
            ok = done.returncode == 0
            error = None if ok else output[-ERROR_CHARS:] or f"npm exited with {done.returncode}"
        except subprocess.TimeoutExpired:
            ok, error = False, f"npm did not finish within {INSTALL_TIMEOUT} seconds"
        except (OSError, subprocess.SubprocessError) as exc:
            ok, error = False, f"Cannot run npm: {exc}"
        # Even a failed install may have replaced the CLI; the next snapshot asks it again.
        with workspace_agents._version_lock:
            workspace_agents._version.clear()
        with self.lock:
            self.last_update = {
                "running": False,
                "version": version,
                "ok": ok,
                "error": error,
                "finished_at": self.now(),
            }
