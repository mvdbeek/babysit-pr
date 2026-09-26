"""Isolated Sentry experiment: fake Sentry HTTP, fake gh, fake claude, private state."""

import io
import json
import subprocess
import time
import urllib.error
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
import sentry_issues as si
import sentry_llm as sl

NOW = datetime(2026, 9, 25, 12, tzinfo=UTC)
HOST = "https://sentry.example.org"
TOKEN = "sntrys_" + "x" * 40


def issue(issue_id, slug, short, **changes):
    value = {
        "id": str(issue_id),
        "shortId": short,
        "permalink": f"{HOST}/organizations/galaxy/issues/{issue_id}/",
        "title": "ValueError: invalid literal for int() with base 10: 'abc'",
        "culprit": "galaxy.tools.parameters.basic in from_json",
        "metadata": {
            "type": "ValueError",
            "value": "invalid literal for int() with base 10: 'abc'",
        },
        "level": "error",
        "priority": "high",
        "status": "unresolved",
        "substatus": "ongoing",
        "isUnhandled": False,
        "count": "10",
        "userCount": 0,
        "firstSeen": "2026-09-01T00:00:00Z",
        "lastSeen": "2026-09-25T11:00:00Z",
        "stats": {"24h": [[0, 1], [1, 2]]},
        "project": {"slug": slug},
    }
    value.update(changes)
    return value


class Response:
    def __init__(self, value, headers=None):
        self.data = json.dumps(value).encode()
        self.headers = headers or {}

    def read(self, limit):
        return self.data[:limit]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeSentry:
    """urlopen stand-in: projects, paginated issues, latest events and comments."""

    def __init__(self):
        self.requests = []
        self.projects = [{"slug": "galaxy-main", "id": 3}, {"slug": "usegalaxy-eu-main", "id": 7}]
        self.issues = {
            "3": [
                issue(1, "galaxy-main", "GALAXY-MAIN-1", count="100", isUnhandled=True),
                issue(
                    2,
                    "galaxy-main",
                    "GALAXY-MAIN-2",
                    title="KeyError: 'x'",
                    metadata={"type": "KeyError", "value": "'x'"},
                    culprit="galaxy.jobs in finish",
                    level="warning",
                    priority="low",
                    lastSeen="2026-09-01T00:00:00Z",
                ),
            ],
            "7": [
                issue(
                    3,
                    "usegalaxy-eu-main",
                    "USEGALAXY-EU-MAIN-9",
                    metadata={
                        "type": "ValueError",
                        "value": "invalid literal for int() with base 10: 'xyz'",
                    },
                    substatus="escalating",
                    userCount=12,
                )
            ],
        }
        self.pages = {}
        self.error = None

    def __call__(self, request, timeout):
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        parts = urlsplit(request.full_url)
        query = parse_qs(parts.query)
        self.requests.append((request.get_method(), parts.path, query, request.data))
        if self.error:
            raise self.error
        if parts.path.endswith("/projects/"):
            return Response(self.projects)
        if parts.path.endswith("/issues/") and request.get_method() == "GET":
            if "cursor" in query:
                return Response(self.pages[query["cursor"][0]])
            project = query["project"][0]
            headers = {}
            if project in self.pages:
                headers["Link"] = (
                    f"<{HOST}/api/0/organizations/galaxy/issues/?cursor={project}>; "
                    'rel="next"; results="true"; cursor="x"'
                )
            return Response(self.issues.get(project, []), headers)
        if parts.path.endswith("/events/latest/"):
            return Response(
                {
                    "entries": [
                        {
                            "type": "exception",
                            "data": {
                                "values": [
                                    {
                                        "value": "invalid literal for int(): 'abc'",
                                        "stacktrace": {
                                            "frames": [
                                                {"inApp": False, "module": "sqlalchemy"},
                                                {
                                                    "inApp": True,
                                                    "module": "galaxy.tools.parameters.basic",
                                                    "function": "from_json",
                                                    "lineNo": 42,
                                                },
                                            ]
                                        },
                                    }
                                ]
                            },
                        }
                    ]
                }
            )
        if parts.path.endswith("/comments/"):
            return Response({"id": "c1"})
        raise AssertionError(f"unexpected request {parts.path}")


class FakeGh:
    def __init__(self):
        self.calls = []
        self.existing = []
        self.visibility = "PUBLIC"
        self.fail_create = False

    def __call__(self, args, timeout, text):
        self.calls.append(args)
        if args[1:3] == ["issue", "list"]:
            out = self.existing
        elif args[1:3] == ["repo", "view"]:
            out = {"visibility": self.visibility}
        elif args[1] == "api":
            if self.fail_create:
                return subprocess.CompletedProcess(args, 1, "", "HTTP 403")
            out = {"html_url": "https://github.com/galaxyproject/galaxy/issues/77", "number": 77}
        else:
            raise AssertionError(args)
        return subprocess.CompletedProcess(args, 0, json.dumps(out), "")


def configure(home, **changes):
    directory = home / "experiments" / "sentry"
    directory.mkdir(parents=True, exist_ok=True)
    token = home / "token"
    token.write_text(TOKEN)
    token.chmod(0o600)
    config = {
        "enabled": True,
        "host": HOST,
        "organization": "galaxy",
        "projects": {
            "galaxy-main": "galaxyproject/galaxy",
            "usegalaxy-eu-main": "galaxyproject/galaxy",
        },
        "token_file": str(token),
        "llm": {"triage": {"per_refresh": 2, "per_day": 10}},
    }
    config.update(changes)
    (directory / "config.json").write_text(json.dumps(config))
    return directory


@pytest.fixture
def plugin(tmp_path, monkeypatch):
    monkeypatch.delenv("SENTRY_ACCESS_TOKEN", raising=False)
    configure(tmp_path)
    fake, gh = FakeSentry(), FakeGh()
    value = si.SentryIssues(tmp_path, opener=fake, gh=gh)
    value.fake, value.fake_gh = fake, gh
    value.next_poll = float("inf")  # Tests refresh explicitly.
    value._refresh()
    return value


def test_config_validation_and_token_file_permissions(tmp_path, monkeypatch):
    monkeypatch.delenv("SENTRY_ACCESS_TOKEN", raising=False)
    directory = configure(tmp_path)
    config = si.load_config(directory)
    assert config["host"] == HOST and config["llm"]["per_refresh"] == 2
    assert config["llm"]["quota"] == {
        "min_left_percent": 30,
        "windows": ["five_hour", "seven_day"],
        "when_unknown": "fixed_caps",
    }
    assert si.read_token(config) == TOKEN
    (tmp_path / "token").chmod(0o644)
    with pytest.raises(ValueError, match="chmod 600"):
        si.read_token(config)
    monkeypatch.setenv("SENTRY_ACCESS_TOKEN", TOKEN)
    assert si.read_token(config) == TOKEN
    for bad in (
        {"host": "http://sentry.example.org"},
        {"host": "https://sentry.example.org/path"},
        {"projects": {"galaxy-main": "not a repo"}},
        {"projects": {}},
        {"period": "1y"},
        {"llm": {"agent": "codex"}},
        {"llm": {"quota": {"when_unknown": "guess"}}},
        {"llm": {"quota": {"min_left_percent": 150}}},
    ):
        configure(tmp_path, **bad)
        with pytest.raises(ValueError):
            si.load_config(directory)
    (directory / "config.json").unlink()
    assert si.load_config(directory) is None


def test_request_budget_is_validated_against_projects_and_pages(tmp_path):
    projects = {f"p{i}": "galaxyproject/galaxy" for i in range(20)}
    directory = configure(tmp_path, projects=projects, max_pages=2)
    with pytest.raises(ValueError, match="at most 40 Sentry requests"):
        si.load_config(directory)
    configure(tmp_path, projects=projects, max_pages=1)
    assert si.load_config(directory)["max_pages"] == 1


def test_group_keys_survive_culprit_changes_and_bad_fields_do_not_fail(plugin):
    before = plugin.snapshot()["groups"][0]["key"]
    for item in plugin.fake.issues["3"][:1] + plugin.fake.issues["7"]:
        item["culprit"] = "galaxy.tools.parameters.basic in to_json"
        item["userCount"] = "n/a"
        item["firstSeen"] = "2026-09-25T10:00:00"  # No timezone.
    plugin._refresh()
    value = plugin.snapshot()
    assert value["error"] is None
    assert value["groups"][0]["key"] == before and value["groups"][0]["users"] == 0


def test_scoring_reasons_and_buckets():
    base = {
        "priority": "high",
        "projects": [{"slug": "a"}],
        "level": "warning",
        "unhandled": False,
        "substatus": None,
        "first_seen": "2026-09-01T00:00:00Z",
        "last_seen": "2026-09-25T11:00:00Z",
        "users": 0,
        "events_24h": 0,
    }
    assert si.score(base, NOW) == (0, "low", [])
    worst = {
        **base,
        "projects": [{"slug": "a"}, {"slug": "b"}, {"slug": "c"}, {"slug": "c"}],
        "level": "fatal",
        "unhandled": True,
        "substatus": "escalating",
        "first_seen": "2026-09-25T00:00:00Z",
        "users": 150,
        "events_24h": 5000,
    }
    points, bucket, reasons = si.score(worst, NOW)
    assert (points, bucket) == (17, "critical")
    assert reasons == [
        "3 servers",
        "fatal",
        "unhandled",
        "escalating",
        "new today",
        "150 users",
        "5000 events/24h",
    ]
    quiet = {**base, "priority": "low", "last_seen": "2026-09-01T00:00:00Z", "level": "error"}
    assert si.score(quiet, NOW) == (-3, "low", ["low priority", "error", "quiet for 7+ days"])
    assert si.score({**base, "level": "error", "users": 10, "events_24h": 100}, NOW)[1] == "medium"


def test_collect_groups_across_servers_and_reports_truncation(plugin):
    value = plugin.snapshot()
    assert value["enabled"] and not value["error"]
    groups = value["groups"]
    assert len(groups) == 2
    top = groups[0]
    # Same exception, culprit and repo on two servers (quoted value normalized): one row.
    assert [p["short_id"] for p in top["projects"]] == ["GALAXY-MAIN-1", "USEGALAXY-EU-MAIN-9"]
    assert top["short_id"] == "GALAXY-MAIN-1"
    assert top["count"] == 110 and top["users"] == 12 and top["events_24h"] == 6
    assert top["substatus"] == "escalating" and top["unhandled"]
    assert top["reasons"] == ["2 servers", "error", "unhandled", "escalating", "12 users"]
    assert groups[1]["bucket"] == "low"
    # Paginate at most max_pages, and say when more exist.
    plugin.fake.pages = {"3": [issue(10, "galaxy-main", "GALAXY-MAIN-10")]}
    plugin.config["max_pages"] = 1
    plugin._refresh()
    assert plugin.snapshot()["warnings"] == [
        "galaxy-main: showing the 2 most frequent issues; more exist"
    ]
    plugin.config["max_pages"] = 2
    plugin._refresh()
    members = [p["short_id"] for p in plugin.snapshot()["groups"][0]["projects"]]
    # Ties on count keep project order.
    assert members == ["GALAXY-MAIN-1", "GALAXY-MAIN-10", "USEGALAXY-EU-MAIN-9"]
    # Only the project list and issue pages were requested; nothing was written.
    assert {method for method, *_ in plugin.fake.requests} == {"GET"}


def test_refresh_errors_keep_last_snapshot_stale_and_hide_token(plugin):
    plugin.fake.error = urllib.error.HTTPError(HOST, 401, "no", {}, io.BytesIO())
    plugin._refresh()
    value = plugin.snapshot()
    assert value["stale"] and len(value["groups"]) == 2
    assert value["error"] == "Sentry refresh failed: Sentry rejected the access token (HTTP 401)"
    assert TOKEN not in json.dumps(value)
    api = si.Api(HOST, TOKEN, plugin.fake, max_requests=1)
    plugin.fake.error = None
    api.request("organizations/galaxy/projects/")
    with pytest.raises(ValueError, match="budget"):
        api.request("organizations/galaxy/projects/")


def test_cache_restores_after_restart_and_config_change_discards_it(plugin, tmp_path):
    cache = tmp_path / "experiments/sentry/cache.json"
    assert oct(cache.stat().st_mode & 0o777) == "0o600"
    restarted = si.SentryIssues(tmp_path, opener=plugin.fake, gh=plugin.fake_gh)
    assert len(restarted.value["groups"]) == 2
    configure(tmp_path, query="is:unresolved level:error")
    changed = si.SentryIssues(tmp_path, opener=plugin.fake, gh=plugin.fake_gh)
    assert changed.value == {}


def test_disabled_and_broken_configuration_are_isolated(tmp_path):
    assert si.SentryIssues(tmp_path).snapshot() == {
        "groups": [],
        "enabled": False,
        "loading": False,
        "error": None,
        "stale": False,
    }
    configure(tmp_path, host="ftp://x")
    broken = si.SentryIssues(tmp_path)
    assert not broken.enabled and "Configure host" in broken.snapshot()["error"]
    with pytest.raises(ValueError, match="disabled"):
        broken.action({"action": "retriage", "key": "x"})


def test_triage_queue_respects_per_refresh_fingerprints_and_retriage(plugin):
    store = plugin.store
    jobs = store.latest("triage")
    assert len(jobs) == 2  # per_refresh=2, both groups queued in score order.
    top = plugin.snapshot()["groups"][0]
    assert top["triage"]["status"] == "queued"
    for job in jobs.values():
        store.finish(job["id"], "done", {**TRIAGE, "severity": "low"})
    plugin._refresh()  # Same fingerprints: nothing new is queued.
    assert all(j["status"] == "done" for j in store.latest("triage").values())
    view = plugin.snapshot()["groups"][0]["triage"]
    assert top["bucket"] == "critical"
    assert view["severity"] == "low" and view["disagrees"] is True
    assert plugin.snapshot()["groups"][1]["triage"]["disagrees"] is False  # low vs low
    plugin.fake.issues["3"][0]["substatus"] = "regressed"
    plugin.fake.issues["7"][0]["substatus"] = "regressed"
    plugin._refresh()
    assert store.latest("triage")[top["key"]]["status"] == "queued"
    plugin.action({"action": "retriage", "key": top["key"]})  # A pending job is reused.
    assert len([j for j in store.latest("triage").values() if j["status"] == "queued"]) == 1
    with pytest.raises(ValueError, match="Unknown Sentry issue group"):
        plugin.action({"action": "retriage", "key": "nope"})
    with pytest.raises(ValueError, match="Invalid"):
        plugin.action({"action": "retriage", "key": top["key"], "extra": 1})


TRIAGE = {
    "severity": "high",
    "confidence": "medium",
    "summary": "Tool form crashes",
    "likely_cause": "bad int",
    "user_impact": "form fails",
    "suggested_area": "lib/galaxy/tools",
    "reasons": ["many users"],
}
SANITIZED = {
    "title": "ValueError in tool parameter parsing",
    "body": "Reported by Sentry as GALAXY-MAIN-1.\n\n<sub>Sentry: GALAXY-MAIN-1</sub>",
    "redactions": [{"kind": "other", "original_excerpt": "'abc'", "replacement": "<value>"}],
    "concerns": [],
    "safe_to_publish": True,
}


def finish_sanitizer(plugin, output=SANITIZED, status="done"):
    job = next(j for j in plugin.store.latest("sanitize").values())
    plugin.store.finish(job["id"], status, output if status == "done" else None, "boom")


def test_publish_pipeline_sanitizes_checks_patterns_creates_and_writes_back(plugin):
    key = plugin.snapshot()["groups"][0]["key"]
    group = plugin.action({"action": "publish-draft", "key": key})["group"]
    publish = group["publish"]
    assert publish["status"] == "sanitizing" and publish["repo_public"]
    draft = publish["draft"]["body"]
    assert "galaxy.tools.parameters.basic:from_json:42" in draft
    assert "sqlalchemy" not in draft and "Sentry: GALAXY-MAIN-1" in draft
    job = next(iter(plugin.store.latest("sanitize").values()))
    assert job["input"]["context"]["error_message"] == "invalid literal for int(): 'abc'"
    with pytest.raises(ValueError, match="Review a sanitized draft"):
        plugin.action({"action": "publish-create", "key": key, "title": "t", "body": "b"})
    finish_sanitizer(plugin)
    publish = plugin.snapshot()["groups"][0]["publish"]
    assert publish["status"] == "ready" and publish["findings"] == []
    with pytest.raises(ValueError, match=r"must not be published \(email\)"):
        plugin.action(
            {"action": "publish-create", "key": key, "title": "t", "body": "mail a@b.example"}
        )
    created = plugin.action(
        {"action": "publish-create", "key": key, "title": "Crash", "body": "Plain report"}
    )["group"]["publish"]
    assert created["status"] == "created" and created["number"] == 77
    assert created["writeback"] == "ok"
    create = next(c for c in plugin.fake_gh.calls if c[1] == "api")
    assert "body=Plain report\n\n<sub>Sentry: GALAXY-MAIN-1</sub>" in create
    comments = [r for r in plugin.fake.requests if r[1].endswith("/comments/")]
    assert [json.loads(r[3])["text"] for r in comments] == [
        "GitHub issue: https://github.com/galaxyproject/galaxy/issues/77"
    ] * 2
    # A second publish request does not create another issue.
    plugin.action({"action": "publish-draft", "key": key})
    assert len([c for c in plugin.fake_gh.calls if c[1] == "api"]) == 1


def test_publish_blocks_unsafe_sanitizer_output_failed_jobs_and_existing_issues(plugin):
    key = plugin.snapshot()["groups"][0]["key"]
    plugin.action({"action": "publish-draft", "key": key})
    finish_sanitizer(plugin, {**SANITIZED, "body": "token ghp_" + "a" * 36})
    publish = plugin.snapshot()["groups"][0]["publish"]
    assert publish["status"] == "blocked"
    assert {"kind": "token", "excerpt": "ghp_aa…"} in publish["findings"]
    plugin.action({"action": "publish-draft", "key": key})
    finish_sanitizer(plugin, {**SANITIZED, "safe_to_publish": False, "concerns": ["unsure"]})
    assert plugin.snapshot()["groups"][0]["publish"]["error"].startswith("The sanitizer was not")
    plugin.action({"action": "publish-draft", "key": key})
    finish_sanitizer(plugin, status="failed")
    assert plugin.snapshot()["groups"][0]["publish"]["status"] == "failed"
    calls = plugin.store.calls_since(0)
    plugin.fake_gh.existing = [
        {"number": 5, "url": "https://github.com/galaxyproject/galaxy/issues/5"}
    ]
    publish = plugin.action({"action": "publish-draft", "key": key})["group"]["publish"]
    # An existing issue is linked without another sanitizer call or a new issue.
    assert publish["status"] == "created" and publish["url"].endswith("/issues/5")
    assert publish["writeback"] == "ok"
    assert plugin.store.calls_since(0) == calls
    assert not [c for c in plugin.fake_gh.calls if c[1] == "api"]


def test_failed_create_and_writeback_can_be_retried(plugin):
    key = plugin.snapshot()["groups"][0]["key"]
    plugin.action({"action": "publish-draft", "key": key})
    finish_sanitizer(plugin)
    plugin.fake_gh.fail_create = True
    with pytest.raises(ValueError, match="GitHub issue was not created"):
        plugin.action({"action": "publish-create", "key": key, "title": "t", "body": "b"})
    assert plugin.snapshot()["groups"][0]["publish"]["status"] == "ready"
    plugin.fake_gh.fail_create = False
    plugin.fake.error = OSError("down")
    publish = plugin.action({"action": "publish-create", "key": key, "title": "t", "body": "b"})[
        "group"
    ]["publish"]
    assert publish["status"] == "created" and publish["writeback"] == "failed"
    plugin.fake.error = None
    retried = plugin.action({"action": "writeback-retry", "key": key})["group"]["publish"]
    assert retried["writeback"] == "ok" and retried["error"] is None
    with pytest.raises(ValueError, match="Nothing to write back"):
        plugin.action({"action": "writeback-retry", "key": key})


def test_writeback_retry_only_comments_on_issues_still_missing_the_link(plugin):
    key = plugin.snapshot()["groups"][0]["key"]
    plugin.action({"action": "publish-draft", "key": key})
    finish_sanitizer(plugin)
    fake = plugin.fake
    posted = []

    def flaky(request, timeout):
        if request.get_method() == "POST":
            posted.append(request.full_url.split("/issues/")[1])
            if len(posted) == 2:
                raise OSError("down")
        return fake(request, timeout)

    plugin.opener = flaky
    publish = plugin.action({"action": "publish-create", "key": key, "title": "t", "body": "b"})[
        "group"
    ]["publish"]
    assert publish["writeback"] == "failed"
    plugin.action({"action": "writeback-retry", "key": key})
    assert posted == ["1/comments/", "3/comments/", "3/comments/"]


def test_interrupted_create_becomes_uncertain_and_is_resolved_by_searching(plugin):
    key = plugin.snapshot()["groups"][0]["key"]
    plugin.action({"action": "publish-draft", "key": key})
    finish_sanitizer(plugin)
    gh = plugin.fake_gh

    def hang(args, timeout, text):
        if args[1] == "api":
            raise subprocess.TimeoutExpired(args, timeout)
        return gh(args, timeout, text)

    plugin.gh = hang
    with pytest.raises(ValueError, match="may exist"):
        plugin.action({"action": "publish-create", "key": key, "title": "t", "body": "b"})
    assert plugin.snapshot()["groups"][0]["publish"]["status"] == "uncertain"
    # The issue did get created: trying again links it instead of creating another.
    plugin.gh = gh
    gh.existing = [{"number": 9, "url": "https://github.com/galaxyproject/galaxy/issues/9"}]
    publish = plugin.action({"action": "publish-draft", "key": key})["group"]["publish"]
    assert publish["status"] == "created" and publish["number"] == 9
    # A create left running by a restart turns uncertain after the timeout.
    record = plugin.store.publish(key)
    plugin.store.set_publish(key, {**record, "status": "creating", "updated_at": 0})
    assert plugin.snapshot()["groups"][0]["publish"]["status"] == "uncertain"


def test_pattern_check_catches_what_a_sanitizer_might_miss():
    text = (
        "user jane@example.org from 192.168.1.20 and fe80:0:0:0:200:f8ff:fe21:67cf "
        "key AKIAABCDEFGHIJKLMNOP token eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0.SflKxwRJSMeKKF2QT4 "
        "path /home/jane/galaxy/database hex 0123456789abcdef0123456789abcdef"
    )
    kinds = [f["kind"] for f in si.findings(text)]
    assert kinds == ["email", "ip", "ip", "token", "token", "secret", "path"]
    safe = "Galaxy 26.1.2.dev0 in lib/galaxy/tools/__init__.py line 42, 1098 events/24h"
    assert si.findings(safe) == []
    # Generated public links and long source paths are not secrets.
    public = (
        f"[X-1]({HOST}/organizations/galaxy/issues/12345678/) "
        "https://github.com/galaxyproject/galaxy/issues/77 "
        "lib/galaxy/webapps/galaxy/api/workflows/invocations.py:show:12 "
        "dist/galaxy-app-CYgwyqT0:XMLHttpRequest.g:8 `@property`"
    )
    assert si.findings(public, HOST) == []
    risky = "key " + "AbCd0123" * 6 + " hex " + "DEADBEEF" * 4 + " /srv/galaxy/jobs/1/out @someone"
    # AbCd0123… is both hex and base64-like, so it matches twice.
    kinds = [f["kind"] for f in si.findings(risky, HOST)]
    assert kinds == ["secret", "secret", "secret", "path", "mention"]


def test_targets_and_private_mcp_config(plugin, tmp_path):
    targets = plugin.targets()
    assert targets[0]["id"] == f"sentry:{plugin.snapshot()['groups'][0]['key']}"
    assert targets[0]["kind"] == "sentry" and targets[0]["short_id"] == "GALAXY-MAIN-1"
    path = plugin.handle_mcp_config()
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    server = json.loads(path.read_text())["mcpServers"]["sentry"]
    assert server["args"] == ["-y", sl.MCP_PACKAGE, "--skills=inspect"]
    assert server["env"] == {"SENTRY_HOST": "sentry.example.org", "SENTRY_ACCESS_TOKEN": TOKEN}
    # A separate read-only token for agents keeps the write-capable one in the dashboard.
    read_only = tmp_path / "ro-token"
    read_only.write_text("sntrys_" + "r" * 40)
    read_only.chmod(0o600)
    plugin.config["mcp_token_file"] = str(read_only)
    server = json.loads(plugin.handle_mcp_config().read_text())["mcpServers"]["sentry"]
    assert server["env"]["SENTRY_ACCESS_TOKEN"] == "sntrys_" + "r" * 40
    assert si.read_token(plugin.config) == TOKEN


# -- LLM worker, quota gate --


def llm_config(**quota):
    return {
        "agent": "claude",
        "per_day": 3,
        "quota": {
            "min_left_percent": 30,
            "windows": ["five_hour", "seven_day"],
            "when_unknown": "fixed_caps",
            **quota,
        },
    }


def window(name, used, agent="claude"):
    return {"agent": agent, "name": name, "used_percent": used, "resets_at": "2026-10-02T15:00:00Z"}


def test_gate_budget_quota_unknown_and_pause(tmp_path):
    store = sl.Store(tmp_path)
    now = time.time()
    assert sl.gate(llm_config(), [window("seven_day", 50)], None, store, now)["state"] == "open"
    paused = sl.gate(llm_config(), [window("seven_day", 88)], None, store, now)
    assert paused["state"] == "paused"
    assert paused["reason"] == (
        "Claude seven day quota 12% left (< 30%); resets 2026-10-02T15:00:00Z"
    )
    # Windows outside the configured list, and other agents, never gate.
    assert (
        sl.gate(
            llm_config(),
            [window("seven_day_opus", 99), window("seven_day", 99, "codex")],
            None,
            store,
            now,
        )["state"]
        == "open"
    )
    assert sl.gate(llm_config(), None, "offline", store, now)["state"] == "unknown"
    assert (
        sl.gate(llm_config(when_unknown="pause"), None, "offline", store, now)["state"] == "paused"
    )
    for _ in range(3):
        job = store.enqueue("triage", str(time.time()), {}, force=True)
        store.claim()
        store.finish(job["id"], "done", {})
    assert sl.gate(llm_config(), [], None, store, now)["reason"] == "Daily LLM budget used (3/3)"
    store.set_meta("pause", {"until": now + 60, "reason": "Rate limited"})
    assert sl.gate(llm_config(), [], None, store, now)["reason"] == "Rate limited"


def test_usage_readers_parse_vendor_shapes_and_never_refresh(tmp_path, monkeypatch):
    claude_body = {
        "five_hour": {"utilization": 26.0, "resets_at": "2026-09-25T16:20:00Z"},
        "seven_day": {"utilization": 88.0, "resets_at": "2026-10-02T15:00:00Z"},
        "seven_day_sonnet": None,
        "unrelated": {"utilization": 1},
    }
    assert [w["name"] for w in sl.claude_windows(claude_body)] == ["five_hour", "seven_day"]
    codex_body = {
        "rate_limit": {
            "primary_window": {
                "used_percent": 9,
                "limit_window_seconds": 604800,
                "reset_at": 1790844297,
            },
            "secondary_window": None,
        }
    }
    assert sl.codex_windows(codex_body) == [
        {
            "agent": "codex",
            "name": "seven_day",
            "used_percent": 9.0,
            "resets_at": "2026-10-01T08:44:57Z",
        }
    ]
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    login = {
        "claudeAiOauth": {
            "accessToken": "tok",
            "expiresAt": (time.time() + 3600) * 1000,
            "scopes": ["user:profile"],
        }
    }

    def keychain(value, code=0):
        return lambda args, timeout, text: subprocess.CompletedProcess(
            args, code, json.dumps(value), ""
        )

    fetched = []

    def fetch(url, headers):
        fetched.append((url, headers))
        return claude_body

    windows, why = sl.usage_reading("claude", keychain(login), fetch)
    assert why is None and len(windows) == 2
    assert fetched == [
        (
            "https://api.anthropic.com/api/oauth/usage",
            {"Authorization": "Bearer tok", "anthropic-beta": "oauth-2025-04-20"},
        )
    ]
    expired = {"claudeAiOauth": {**login["claudeAiOauth"], "expiresAt": 1}}
    assert sl.usage_reading("claude", keychain(expired), fetch) == (
        None,
        "stored Claude login has expired; it refreshes on Claude's next use",
    )
    inference_only = {"claudeAiOauth": {**login["claudeAiOauth"], "scopes": ["user:inference"]}}
    assert sl.usage_reading("claude", keychain(inference_only), fetch)[0] is None
    # The credentials file is the fallback when the Keychain has nothing.
    (tmp_path / ".credentials.json").write_text(json.dumps(login))
    assert sl.usage_reading("claude", keychain({}, 44), fetch)[1] is None
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    (tmp_path / "auth.json").write_text(
        json.dumps({"tokens": {"access_token": "c", "account_id": "a"}})
    )
    windows, _ = sl.usage_reading("codex", keychain({}), lambda url, headers: codex_body)
    assert windows[0]["agent"] == "codex"

    def failing(url, headers):
        raise ValueError("usage endpoint answered HTTP 429")

    assert sl.usage_reading("codex", keychain({}), failing) == (
        None,
        "usage endpoint answered HTTP 429",
    )


class FakeClaude:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, args, timeout, input, text, cwd):
        self.calls.append({"args": args, "input": input, "cwd": cwd, "timeout": timeout})
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return subprocess.CompletedProcess(args, 0, json.dumps(reply), "")


def run_worker(worker):
    worker.tick()
    if worker.thread:
        worker.thread.join(5)


def test_worker_runs_triage_with_read_only_sentry_tools(plugin, tmp_path):
    fake = FakeClaude(
        [
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "structured_output": TRIAGE,
            },
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "structured_output": {"severity": "bogus"},
            },
        ]
    )
    worker = sl.Worker(tmp_path, run=fake, usage=lambda agent: ([window("seven_day", 10)], None))
    run_worker(worker)
    run_worker(worker)
    triage = plugin.snapshot()["groups"]
    assert triage[0]["triage"]["severity"] == "high" and triage[0]["triage"]["status"] == "done"
    assert triage[1]["triage"] == {
        "status": "failed",
        "error": "Triage result did not match its schema",
        "updated_at": triage[1]["triage"]["updated_at"],
    }
    args = fake.calls[0]["args"]
    assert args[args.index("--tools") + 1] == ""
    assert "--strict-mcp-config" in args and args[args.index("--model") + 1] == "sonnet"
    allowed = args[args.index("--allowedTools") + 1 :]
    assert allowed == list(sl.TRIAGE_TOOLS)
    assert "mcp__sentry__execute_sentry_tool" in args[args.index("--disallowedTools") + 1 :]
    mcp = json.loads(Path(args[args.index("--mcp-config") + 1]).read_text())
    assert mcp["mcpServers"]["sentry"]["args"][-1] == "--skills=inspect"
    assert "GALAXY-MAIN-1" in fake.calls[0]["input"] and "untrusted" in fake.calls[0]["input"]
    assert TOKEN not in json.dumps(fake.calls[0]["args"])
    llm = plugin.snapshot()["llm"]
    assert llm["worker"]["alive"] and llm["gate"]["state"] == "open"
    assert llm["budget"]["used_today"] == 2


def test_worker_sanitizer_has_no_tools_and_limit_errors_pause_the_queue(plugin, tmp_path):
    key = plugin.snapshot()["groups"][0]["key"]
    plugin.action({"action": "publish-draft", "key": key})
    fake = FakeClaude(
        [
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "structured_output": SANITIZED,
            },
            {
                "type": "result",
                "subtype": "error",
                "is_error": True,
                "api_error_status": 429,
                "result": "usage limit reached",
            },
        ]
    )
    worker = sl.Worker(tmp_path, run=fake, usage=lambda agent: ([window("five_hour", 10)], None))
    run_worker(worker)  # The sanitizer runs before queued triage: a person is waiting.
    args = fake.calls[0]["args"]
    assert "--mcp-config" not in args and args[args.index("--tools") + 1] == ""
    assert "PUBLIC repository" in fake.calls[0]["input"]
    assert plugin.snapshot()["groups"][0]["publish"]["status"] == "ready"
    run_worker(worker)
    assert plugin.store.meta("pause")["until"] > time.time() + 3000
    run_worker(worker)  # Paused: nothing else starts.
    assert len(fake.calls) == 2
    assert plugin.snapshot()["llm"]["gate"]["reason"].startswith("Claude usage limit hit")


def test_worker_quota_gate_blocks_calls_and_timeouts_fail_jobs(plugin, tmp_path):
    fake = FakeClaude([subprocess.TimeoutExpired("claude", 240)])
    usage = {"value": ([window("seven_day", 95)], None)}
    worker = sl.Worker(tmp_path, run=fake, usage=lambda agent: usage["value"])
    run_worker(worker)
    assert not fake.calls
    assert plugin.snapshot()["llm"]["gate"]["state"] == "paused"
    usage["value"] = ([window("seven_day", 5)], None)
    worker.next_quota = 0
    run_worker(worker)
    assert plugin.store.latest("triage")[plugin.snapshot()["groups"][0]["key"]]["error"] == (
        "Claude call timed out"
    )


def test_worker_reaps_orphans_and_skips_disabled_job_kinds(plugin, tmp_path):
    key = plugin.snapshot()["groups"][0]["key"]
    plugin.action({"action": "publish-draft", "key": key})
    orphan = plugin.store.claim()  # A sanitizer call interrupted by a supervisor restart.
    assert orphan["kind"] == "sanitize"
    fake = FakeClaude([])
    worker = sl.Worker(tmp_path, run=fake, usage=lambda agent: ([], None))
    config = json.loads((tmp_path / "experiments/sentry/config.json").read_text())
    config["llm"]["triage"]["enabled"] = False
    (tmp_path / "experiments/sentry/config.json").write_text(json.dumps(config))
    run_worker(worker)
    assert plugin.store.get(orphan["id"])["error"] == "Interrupted before finishing"
    assert plugin.snapshot()["groups"][0]["publish"]["status"] == "failed"
    assert not fake.calls
    failed = [j for j in plugin.store.latest("triage").values() if j["status"] == "failed"]
    assert failed[0]["error"] == "Triage is disabled in the experiment configuration"


def test_worker_is_idle_without_config_and_recovers_interrupted_jobs(tmp_path, monkeypatch):
    worker = sl.Worker(tmp_path, run=FakeClaude([]), usage=lambda agent: ([], None))
    worker.tick()
    assert worker.store is None and not (tmp_path / "experiments").exists()
    store = sl.Store(tmp_path)
    job = store.enqueue("triage", "k", {})
    assert store.claim()["id"] == job["id"]
    monkeypatch.setattr(sl, "STALE_RUNNING", -1)
    assert store.claim() is None
    assert store.get(job["id"])["status"] == "failed"


def test_validators_strip_control_characters_and_bound_lengths():
    value = sl.validate_triage(
        {**TRIAGE, "summary": "a\x1b[31mb" + "c" * 900, "reasons": ["x"] * 9}
    )
    assert value["summary"].startswith("a[31mb") and len(value["summary"]) == 600
    assert len(value["reasons"]) == 6
    with pytest.raises(ValueError):
        sl.validate_sanitized({**SANITIZED, "redactions": [{"kind": "weird"}]})
    assert sl.validate_sanitized(SANITIZED)["redactions"][0]["kind"] == "other"


def test_store_files_are_private(tmp_path):
    sl.Store(tmp_path / "x")
    assert oct((tmp_path / "x/state.sqlite").stat().st_mode & 0o777) == "0o600"
    assert oct((tmp_path / "x").stat().st_mode & 0o777) == "0o700"


def test_midnight_is_local_day_start():
    now = time.time()
    start = time.localtime(sl.midnight(now))
    assert start[:3] == time.localtime(now)[:3] and start.tm_hour == start.tm_min == 0
