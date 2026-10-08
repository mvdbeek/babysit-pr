"""Subscription usage for Codex and every Claude login, and the one with most quota left.

The supervisor daemon takes the readings because it runs in the user's session: the
launchd dashboard cannot read the login Keychain. Readings happen only while a
dashboard is asking for them (it touches a request file), at most every few minutes,
and reuse the Sentry experiment's read-only reader: the vendors' undocumented usage
endpoints with the CLIs' stored logins, never refreshed. A failed reading keeps the
previous windows, marked stale, and a window past its reset time counts as unused.
"""

import contextlib
import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path

import claude_accounts
import sentry_llm

SNAPSHOT = "llm-usage.json"
REQUEST = "llm-usage-request.json"
REFRESH = "llm-usage-refresh.json"
READER = "llm-usage-reader.json"
REFRESH_SECONDS = 300
# A requested refresh still waits this long after the previous reading.
MIN_REFRESH_SECONDS = 60
# Readings stop when no dashboard has asked for usage for this long.
WANTED_SECONDS = 900
REQUEST_CHECK_SECONDS = 5
# Model-specific weekly windows only limit that model, so they are shown but not ranked.
RANKED = ("five_hour", "seven_day")
# An idle login's token expires until Claude next uses it, so its last reading stays
# useful. After other failures (revoked login, endpoint errors) a reading older than
# this is shown but not ranked: the login may not work at all.
EXPIRED = "stored Claude login has expired"
STALE_RANKED_SECONDS = 3600


def read_json(path):
    try:
        value = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def accounts():
    """Every login a task can be sent to: Codex, then each Claude configuration."""
    found = [{"id": "codex", "agent": "codex", "account": None, "label": "Codex"}]
    for entry in claude_accounts.catalog():
        found.append(
            {
                "id": f"claude:{entry['id']}",
                "agent": "claude",
                # The dashboard's empty choice launches the default login.
                "account": None if entry["id"] == "default" else entry["id"],
                "label": f"Claude · {entry['label']}",
                "config_dir": entry["config_dir"],
            }
        )
    return found


def read_account(account, reader=sentry_llm.usage_reading):
    if account["agent"] == "codex":
        return reader("codex")
    # The Keychain entry is named after CLAUDE_CONFIG_DIR exactly as launches set it.
    home = Path(account["config_dir"]).resolve()
    return reader(
        "claude", config_env=None if home == claude_accounts.default_home() else str(home)
    )


def take_readings(previous, now, reader=sentry_llm.usage_reading):
    """Read every account; a failure keeps that account's last windows."""
    earlier = {a.get("id"): a for a in (previous or {}).get("accounts", []) if isinstance(a, dict)}
    result = []
    for account in accounts():
        try:
            windows, why = read_account(account, reader)
        except Exception as exc:  # One unreadable login must not hide the others.
            windows, why = None, f"{type(exc).__name__}"
        entry = {k: v for k, v in account.items() if k != "config_dir"}
        if windows is not None:
            entry.update(windows=windows, error=None, checked_at=now)
        else:
            old = earlier.get(account["id"]) or {}
            entry.update(
                windows=old.get("windows") or [],
                error=why or "usage unavailable",
                checked_at=old.get("checked_at"),
            )
        result.append(entry)
    return {"attempted_at": now, "accounts": result}


def reset_passed(window, now):
    reset = window.get("resets_at")
    if not isinstance(reset, str):
        return False
    try:
        moment = datetime.fromisoformat(reset.replace("Z", "+00:00"))
    except ValueError:
        return False
    return moment.tzinfo is not None and moment.timestamp() <= now


def left_percent(windows, now):
    """Quota left in the tightest ranked window, or None when no window is known."""
    left = [
        100.0 if reset_passed(w, now) else 100.0 - float(w["used_percent"])
        for w in windows
        if isinstance(w, dict)
        and w.get("name") in RANKED
        and isinstance(w.get("used_percent"), int | float)
    ]
    return max(0.0, min(left)) if left else None


def ranked(account, now):
    error = account.get("error")
    checked = account.get("checked_at")
    return (
        not error
        or str(error).startswith(EXPIRED)
        or (isinstance(checked, int | float) and now - checked < STALE_RANKED_SECONDS)
    )


def reader_state(home, heartbeat):
    """Running, offline, or outdated: a daemon started before usage readings existed."""
    if not heartbeat.get("time") or time.time() - heartbeat["time"] >= 15:
        return "offline"
    reader = read_json(Path(home) / READER) or {}
    return "running" if reader.get("pid") == heartbeat.get("pid") else "outdated"


def present(home, now=None, reader="offline"):
    """The dashboard's view: each account's remaining quota and the best account."""
    now = time.time() if now is None else now
    snapshot = read_json(Path(home) / SNAPSHOT) or {}
    shown = []
    for account in snapshot.get("accounts", []):
        if not isinstance(account, dict) or account.get("agent") not in ("codex", "claude"):
            continue
        windows = [
            {**w, "used_percent": 0.0} if reset_passed(w, now) else w
            for w in account.get("windows") or []
            if isinstance(w, dict) and isinstance(w.get("used_percent"), int | float)
        ]
        left = left_percent(windows, now) if ranked(account, now) else None
        shown.append({**account, "windows": windows, "left_percent": left})
    known = [a for a in shown if a["left_percent"] is not None]
    # Ties keep catalog order, so the choice is stable between refreshes.
    best = max(known, key=lambda a: a["left_percent"], default=None)
    return {
        "accounts": shown,
        "best": best["id"] if best else None,
        "attempted_at": snapshot.get("attempted_at"),
        "reader": reader,
    }


def request(home, refresh=False, now=None):
    """Ask the daemon to keep usage current (and, if ``refresh``, to read it soon)."""
    now = time.time() if now is None else now
    # Separate files, never read back: concurrent requests cannot drop a refresh.
    with contextlib.suppress(OSError):
        sentry_llm.write_private(Path(home) / REQUEST, {"wanted_at": now})
        if refresh:
            sentry_llm.write_private(Path(home) / REFRESH, {"refresh_at": now})


class Worker:
    """Runs from the supervisor loop; readings (Keychain + HTTP) run on their own thread."""

    def __init__(self, home, reader=sentry_llm.usage_reading, now=time.time):
        self.home = Path(home)
        self.reader = reader
        self.now = now
        self.thread: threading.Thread | None = None
        self.next_check = 0.0
        self.error: str | None = None
        sentry_llm.write_private(self.home / READER, {"pid": os.getpid()})
        previous = read_json(self.home / SNAPSHOT) or {}
        attempted = previous.get("attempted_at")
        # A restarted daemon keeps the previous pace instead of reading at once.
        self.last_attempt = float(attempted) if isinstance(attempted, int | float) else 0.0

    def tick(self):
        if self.error:
            # Reported by the supervisor loop, which logs each distinct failure once.
            error, self.error = self.error, None
            raise RuntimeError(error)
        now = self.now()
        if now < self.next_check or (self.thread and self.thread.is_alive()):
            return
        self.next_check = now + REQUEST_CHECK_SECONDS
        wanted = read_json(self.home / REQUEST) or {}
        if now - float(wanted.get("wanted_at") or 0) > WANTED_SECONDS:
            return
        elapsed = now - self.last_attempt
        refresh = read_json(self.home / REFRESH) or {}
        forced = float(refresh.get("refresh_at") or 0) > self.last_attempt
        if elapsed < REFRESH_SECONDS and not (forced and elapsed >= MIN_REFRESH_SECONDS):
            return
        self.last_attempt = now
        self.thread = threading.Thread(target=self.refresh, args=(now,), daemon=True)
        self.thread.start()

    def refresh(self, now):
        try:
            previous = read_json(self.home / SNAPSHOT)
            value = take_readings(previous, now, self.reader)
            sentry_llm.write_private(self.home / SNAPSHOT, value)
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
