"""Codex update checks and installs with an injected registry and a fake npm, never the network."""

import http.client
import io
import json
import os
import threading
import time
import urllib.error

import codex_updates
import dashboard
import pytest
import workspace_agents
from codex_updates import CodexUpdates

SHELL = ["/bin/sh", "-c"]
DAY = 24 * 3600
NPM = """#!/bin/sh
printf '%s\\n' "$*" >> {log}
if [ -n "$FAKE_NPM_WAIT" ]; then while [ ! -e "$FAKE_NPM_WAIT" ]; do sleep 0.05; done; fi
if [ -n "$FAKE_NPM_FAIL" ]; then echo "$FAKE_NPM_FAIL" >&2; exit 1; fi
for last; do :; done
printf '%s\\n' "${{last#@openai/codex@}}" > {version}
echo "changed 1 package"
"""


@pytest.fixture(autouse=True)
def fresh_version(monkeypatch):
    monkeypatch.setattr(workspace_agents, "_version", {})


@pytest.fixture
def npm_codex(tmp_path, monkeypatch):
    """A Codex 0.160.1 installed by npm, and an npm that installs the version it is given."""
    prefix = tmp_path / "prefix"
    package = prefix / "lib" / "node_modules" / "@openai" / "codex" / "bin"
    package.mkdir(parents=True)
    version = tmp_path / "installed-version"
    version.write_text("0.160.1\n")
    cli = package / "codex.js"
    cli.write_text(f'#!/bin/sh\necho "codex-cli $(cat {version})"\n')
    bin_dir = prefix / "bin"
    bin_dir.mkdir()
    (bin_dir / "codex").symlink_to(cli)
    log = tmp_path / "npm.log"
    (bin_dir / "npm").write_text(NPM.format(log=log, version=version))
    for path in (cli, bin_dir / "npm"):
        path.chmod(0o700)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    return log


def checker(home, latest="0.161.0", now=time.time):
    """A checker whose last check, just now, found ``latest``: snapshots do not fetch."""
    (home / codex_updates.STATE).write_text(
        json.dumps({"checked_at": now(), "latest": latest, "error": None})
    )
    return CodexUpdates(home, fetch=lambda: pytest.fail("fetched"), shell=SHELL, now=now)


def installed(updates):
    updates.installer.join(timeout=10)
    return updates.snapshot()


@pytest.mark.parametrize(
    "latest, current, expected",
    [
        ("0.161.0", "0.160.1", True),
        ("0.10.0", "0.9.9", True),
        ("1.0", "0.99.99", True),
        ("0.160.1", "0.160.1", False),
        ("0.160.0", "0.160.1", False),
        ("0.161.0-alpha.1", "0.160.1", False),
        ("0.161.0", "0.160.1-beta", False),
        ("0.161.0", None, False),
        (None, "0.160.1", False),
        ("7", "6", False),
    ],
)
def test_only_two_release_versions_are_compared(latest, current, expected):
    assert codex_updates.newer(latest, current) is expected


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def test_registry_reply_must_be_a_small_release_version(monkeypatch):
    replies = []

    def urlopen(request, timeout):
        assert request.full_url == codex_updates.URL and timeout == 10
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return Response(reply)

    monkeypatch.setattr(codex_updates.urllib.request, "urlopen", urlopen)
    replies.append(json.dumps({"name": "@openai/codex", "version": "0.161.0"}).encode())
    assert codex_updates.fetch_latest() == "0.161.0"
    for reply, why in [
        (json.dumps({"version": "0.161.0; touch X"}).encode(), "no release version"),
        (b"[]", "no release version"),
        (b" " * (codex_updates.MAX_BYTES + 1), "size limit"),
        (urllib.error.HTTPError(codex_updates.URL, 503, "busy", {}, None), "HTTP 503"),
        (TimeoutError(), "unavailable: TimeoutError"),
    ]:
        replies.append(reply)
        with pytest.raises(ValueError, match=why):
            codex_updates.fetch_latest()


def test_checks_are_daily_and_a_restart_keeps_the_pace(tmp_path, fake_tools):
    clock = [1_000_000.0]
    fetched = []

    def fetch():
        fetched.append(clock[0])
        return "0.161.0"

    def start():
        return CodexUpdates(tmp_path, fetch=fetch, shell=SHELL, now=lambda: clock[0])

    updates = start()
    assert updates.snapshot()["latest"] is None
    updates.checker.join(timeout=5)
    path = tmp_path / codex_updates.STATE
    assert path.stat().st_mode & 0o777 == 0o600
    assert json.loads(path.read_text()) == {
        "checked_at": clock[0],
        "latest": "0.161.0",
        "error": None,
    }
    clock[0] += DAY - 60
    assert updates.snapshot()["latest"] == "0.161.0"
    restarted = start()
    assert restarted.snapshot()["checked_at"] == 1_000_000.0
    assert restarted.checker is None and fetched == [1_000_000.0]
    clock[0] += 120
    restarted.snapshot()
    restarted.checker.join(timeout=5)
    assert fetched == [1_000_000.0, 1_000_000.0 + DAY + 60]


def test_a_failed_check_keeps_the_last_release_and_retries_hourly(tmp_path, fake_tools):
    clock = [1_000_000.0]
    replies = ["0.161.0", ValueError("npm registry answered HTTP 503"), "0.162.0"]

    def fetch():
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def check(updates):
        updates.snapshot()
        updates.checker.join(timeout=5)
        return updates.snapshot()

    updates = CodexUpdates(tmp_path, fetch=fetch, shell=SHELL, now=lambda: clock[0])
    check(updates)
    clock[0] += DAY
    failed = check(updates)
    assert failed["latest"] == "0.161.0" and failed["checked_at"] == clock[0]
    assert failed["error"] == "npm registry answered HTTP 503"
    saved = json.loads((tmp_path / codex_updates.STATE).read_text())
    assert saved["latest"] == "0.161.0" and saved["error"] == failed["error"]
    clock[0] += codex_updates.RETRY_SECONDS - 60
    restarted = CodexUpdates(tmp_path, fetch=fetch, shell=SHELL, now=lambda: clock[0])
    assert restarted.snapshot()["error"] and restarted.checker is None
    clock[0] += 60
    recovered = check(restarted)
    assert recovered["latest"] == "0.162.0" and recovered["error"] is None and not replies


def test_only_an_npm_installed_codex_is_updatable(tmp_path, fake_tools, monkeypatch):
    monkeypatch.setenv("FAKE_AGENT_VERSION", "codex-cli 0.160.1")
    shown = checker(tmp_path).snapshot()
    assert shown["installed"] == "0.160.1" and shown["available"] is True
    assert shown["updatable"] is False
    with pytest.raises(ValueError, match="not installed with npm"):
        checker(tmp_path).update()
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert codex_updates.npm_installed() is False


def test_update_installs_the_release_shown_and_reports_it(tmp_path, npm_codex):
    updates = checker(tmp_path)
    shown = updates.snapshot()
    assert shown["installed"] == "0.160.1" and shown["updatable"] is True
    assert shown["available"] is True and shown["update"] is None
    updates.update()
    done = installed(updates)
    assert npm_codex.read_text() == "install -g @openai/codex@0.161.0\n"
    # The unchanged executable's cached version was dropped, so the new one shows.
    assert done["installed"] == "0.161.0" and done["available"] is False
    assert done["update"] == {
        "running": False,
        "version": "0.161.0",
        "ok": True,
        "error": None,
        "finished_at": pytest.approx(time.time(), abs=10),
    }
    with pytest.raises(ValueError, match="No Codex update is available"):
        updates.update()


def test_a_failed_update_reports_the_end_of_npm_output(tmp_path, npm_codex, monkeypatch):
    monkeypatch.setenv("FAKE_NPM_FAIL", "npm error " + "x" * 600 + " EACCES")
    updates = checker(tmp_path)
    updates.update()
    done = installed(updates)
    assert done["update"]["ok"] is False and done["update"]["running"] is False
    assert done["update"]["error"].endswith("x EACCES")
    assert len(done["update"]["error"]) == codex_updates.ERROR_CHARS
    assert done["installed"] == "0.160.1" and done["available"] is True


def test_one_update_at_a_time(tmp_path, npm_codex, monkeypatch):
    release = tmp_path / "release"
    monkeypatch.setenv("FAKE_NPM_WAIT", str(release))
    updates = checker(tmp_path)
    updates.update()
    try:
        assert updates.snapshot()["update"]["running"] is True
        with pytest.raises(ValueError, match="already running"):
            updates.update()
    finally:
        release.touch()
    assert installed(updates)["update"]["ok"] is True
    assert npm_codex.read_text().count("install") == 1


def request(server, method="GET", headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    sent = {"Content-Type": "application/json", "X-Babysit-Action": "codex-update"}
    conn.request(
        method, "/api/codex-update", "{}" if method == "POST" else None, sent | (headers or {})
    )
    response = conn.getresponse()
    result = response.status, json.loads(response.read())
    conn.close()
    return result


@pytest.fixture
def serve():
    def start(home, **components):
        httpd = dashboard.DashboardServer(home, 0, ["example.ts.net:8443"], **components)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        started.append((httpd, thread))
        return httpd

    started = []
    yield start
    for httpd, thread in started:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()


def test_dashboard_serves_and_starts_the_update(tmp_path, npm_codex, serve):
    updates = checker(tmp_path)
    server = serve(tmp_path, codex_updates=updates)
    code, shown = request(server)
    assert code == 200 and shown["available"] is True and shown["latest"] == "0.161.0"
    for headers in [
        {"X-Babysit-Action": ""},
        {"Origin": "https://attacker.invalid"},
        {"Sec-Fetch-Site": "cross-site"},
    ]:
        assert request(server, "POST", headers)[0] == 403
    assert not npm_codex.exists()
    code, started = request(server, "POST")
    assert code == 200 and started["update"]["version"] == "0.161.0"
    updates.installer.join(timeout=10)
    code, shown = request(server)
    assert shown["installed"] == "0.161.0" and shown["update"]["ok"] is True
    assert request(server, "POST") == (400, {"error": "No Codex update is available"})


def test_a_dashboard_without_the_checker_offers_no_update(tmp_path, serve):
    server = serve(tmp_path)
    assert request(server) == (200, {"available": False, "update": None})
    assert request(server, "POST") == (400, {"error": "Codex update checks are not enabled"})
