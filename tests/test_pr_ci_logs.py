"""Automatic log collection uses temporary state and fake GitHub executables."""

import fcntl
import gzip
import http.client
import json
import os
import sys
import threading
import time
from unittest.mock import Mock

import dashboard
import pr_ci
import pr_ci_logs as logs
import pytest
from pr_overview import Overview

SHA = "a" * 40
CHECK = {
    "id": "check1",
    "name": "pytest (3.14)",
    "bucket": "fail",
    "repository": "fork/repo",
    "run_id": 123,
    "database_id": 456,
    "url": "https://github.com/fork/repo/actions/runs/123/job/456",
}
JOB = {
    "id": 456,
    "run_id": 123,
    "head_sha": SHA,
    "check_run_url": "https://api.github.com/repos/fork/repo/check-runs/456",
    "status": "completed",
    "conclusion": "failure",
    "steps": [{"name": "Run pytest", "conclusion": "failure"}],
}
RAW = b"2026-09-12T01:02:03.000Z \x1b[31mFAILED tests/test_cache.py::test_atomic - AssertionError\x1b[0m\n"


@pytest.fixture
def collector(tmp_path):
    overview = Overview(tmp_path)
    overview.next_poll = float("inf")
    overview.value.update(
        login="test",
        synced_at=time.time(),
        prs=[{"id": "pr1", "head_sha": SHA, "ci": "FAILURE"}],
    )
    ci = pr_ci.CiDetails(tmp_path, overview)
    return logs.BackgroundLogs(tmp_path, overview, ci)


def seed(collector, checks=None):
    pr = collector.overview.value["prs"][0]
    value = {"sha": SHA, "checks": checks if checks is not None else [CHECK.copy()]}
    collector.ci.entries[collector.ci.key("test", pr)] = {
        "value": value,
        "expires_at": time.time() + 300,
        "error": None,
    }
    return value


def records(collector):
    with collector.db() as db:
        return db.execute("SELECT key,state,data FROM jobs ORDER BY updated").fetchall()


@pytest.fixture
def github(monkeypatch):
    metadata = Mock(return_value=JOB.copy())
    download = Mock(return_value=(RAW, False))
    monkeypatch.setattr(logs, "read_job", metadata)
    monkeypatch.setattr(logs, "download_log", download)
    return metadata, download


def test_targets_require_failed_actions_provenance_and_support_forks():
    assert logs.job_target(CHECK)["repo"] == "fork/repo"
    assert (
        logs.job_target({**CHECK, "url": "https://github.com/fork/repo/runs/456"})["job_id"] == 456
    )
    for change in [
        {"url": "https://evil.test/fork/repo/actions/runs/123/job/456"},
        {"url": "https://github.com/other/repo/actions/runs/123/job/456"},
        {"run_id": 999},
        {"database_id": None},
        {"bucket": "pass"},
        {"repository": "fork/.."},
        {"repository": "--flag"},
        {"url": "http://github.com/fork/repo/runs/456"},
        {"url": "https://github.com/fork/repo/runs/789"},
    ]:
        assert logs.job_target({**CHECK, **change}) is None


def test_background_discovers_and_downloads_without_browser_once_across_restart(
    collector, github, monkeypatch
):
    metadata, download = github
    fetch = Mock(return_value={"sha": SHA, "checks": [CHECK]})
    monkeypatch.setattr(pr_ci, "collect_checks", fetch)
    collector.step()  # Automatically discovers failing PRs, without an HTTP request.
    assert records(collector)[0][1] == "queued"
    collector.step()
    value = collector.snapshot("pr1", "check1")
    assert value["state"] == "ready"
    assert value["value"]["tests"] == ["tests/test_cache.py::test_atomic"]
    assert value["value"]["failed_steps"] == ["Run pytest"]
    assert collector.raw_log("pr1", "check1") == RAW
    restarted = logs.BackgroundLogs(collector.path.parent, collector.overview, collector.ci)
    restarted.step()
    assert restarted.snapshot("pr1", "check1")["state"] == "ready"
    assert fetch.call_count == metadata.call_count == download.call_count == 1
    assert collector.path.stat().st_mode & 0o777 == 0o600
    assert collector.root.stat().st_mode & 0o777 == 0o700
    packed = next(collector.root.glob("*.gz"))
    assert packed.stat().st_mode & 0o777 == 0o600 and gzip.decompress(packed.read_bytes()) == RAW


def test_passes_are_not_discovered_and_stale_overview_is_not_used(collector, monkeypatch):
    fetch = Mock()
    monkeypatch.setattr(pr_ci, "collect_checks", fetch)
    collector.overview.value["prs"][0]["ci"] = "SUCCESS"
    collector.step()
    collector.overview.value.update(error="offline")
    collector.overview.value["prs"][0]["ci"] = "FAILURE"
    collector.step()
    assert not fetch.called and not records(collector)


def test_new_head_prevents_queued_download(collector, github):
    value = seed(collector)
    collector.enqueue("test", collector.overview.value["prs"][0], value)
    collector.overview.value["prs"][0].update(head_sha="b" * 40, ci="SUCCESS")
    collector.step()
    assert records(collector)[0][1] == "obsolete"
    assert not github[1].called
    with pytest.raises(ValueError, match="current CI"):
        collector.snapshot("pr1", "check1")


def test_shared_job_remains_available_when_first_pr_closes(collector, github):
    value = seed(collector)
    collector.enqueue("test", collector.overview.value["prs"][0], value)
    collector.overview.value["prs"][0]["id"] = "pr2"
    seed(collector)
    collector.step()
    assert collector.snapshot("pr2", "check1")["state"] == "ready"
    assert github[1].call_count == 1


def test_rerun_with_new_job_downloads_again(collector, github):
    seed(collector)
    collector.step()
    new = {
        **CHECK,
        "id": "check2",
        "database_id": 789,
        "url": "https://github.com/fork/repo/actions/runs/123/job/789",
    }
    seed(collector, [new])
    github[0].return_value = {
        **JOB,
        "id": 789,
        "check_run_url": "https://api.github.com/repos/fork/repo/check-runs/789",
    }
    collector.step()
    assert len(records(collector)) == 2 and github[1].call_count == 2


@pytest.mark.parametrize(
    "field,value",
    [("id", 999), ("run_id", 999), ("head_sha", "wrong"), ("check_run_url", "https://evil.test")],
)
def test_job_identity_verified_before_downloading(collector, github, field, value):
    seed(collector)
    github[0].return_value = {**JOB, field: value}
    collector.step()
    assert records(collector)[0][1] == "error" and not github[1].called
    assert "identity" in collector.snapshot("pr1", "check1")["error"]


def test_success_or_running_job_never_downloaded(collector, github):
    seed(collector)
    github[0].return_value = {**JOB, "conclusion": "success"}
    collector.step()
    assert records(collector)[0][1] == "obsolete" and not github[1].called


def test_download_failures_backoff_and_stop_after_three_attempts(collector, github):
    seed(collector)
    github[1].side_effect = ValueError("logs expired")
    collector.step()
    collector.step()
    assert github[1].call_count == 1
    for _ in range(2):
        with collector.db() as db:
            db.execute("UPDATE jobs SET retry=0")
        collector.step()
    collector.step()
    result = collector.snapshot("pr1", "check1")
    assert github[1].call_count == 3 and result["state"] == "unavailable"
    assert "expired" in result["error"] and not result["refreshing"]


def test_persistent_hourly_budgets_bound_discovery_and_downloads(collector, github, monkeypatch):
    monkeypatch.setattr(logs, "DOWNLOADS_PER_HOUR", 1)
    seed(collector)
    collector.step()
    restarted = logs.BackgroundLogs(collector.path.parent, collector.overview, collector.ci)
    assert not restarted.budget("download", 1, claim=True)
    assert restarted.budget("discovery", 1, claim=True)
    fetch = Mock()
    monkeypatch.setattr(pr_ci, "collect_checks", fetch)
    monkeypatch.setattr(logs, "DISCOVERIES_PER_HOUR", 1)
    collector.overview.value["prs"].append({"id": "pr2", "head_sha": SHA, "ci": "FAILURE"})
    collector.step()
    assert not fetch.called
    with collector.db() as db:
        db.execute("UPDATE requests SET time=?", (time.time() - 3601,))
    assert restarted.budget("download", 1, claim=True)
    assert github[1].call_count == 1


def test_discovery_fairness_and_per_pr_hourly_backoff(collector, monkeypatch):
    fetch = Mock(return_value={"sha": SHA, "checks": []})
    monkeypatch.setattr(pr_ci, "collect_checks", fetch)
    collector.overview.value["prs"].append({"id": "pr2", "head_sha": SHA, "ci": "FAILURE"})
    collector.step()
    collector.step()
    for entry in collector.ci.entries.values():
        entry["expires_at"] = 0
    collector.step()
    assert [c.args[0]["id"] for c in fetch.call_args_list] == ["pr1", "pr2"]


def test_queue_capacity_and_cached_reads_never_download(collector, monkeypatch, github):
    monkeypatch.setattr(logs, "MAX_PENDING_JOBS", 1)
    value = seed(
        collector,
        [
            CHECK,
            {
                **CHECK,
                "id": "other",
                "url": "https://github.com/fork/repo/actions/runs/123/job/789",
            },
        ],
    )
    collector.enqueue("test", collector.overview.value["prs"][0], value)
    assert len(records(collector)) == 1
    assert collector.snapshot("pr1", "check1")["state"] == "queued"
    with pytest.raises(ValueError):
        collector.raw_log("pr1", "check1")
    with pytest.raises(ValueError):
        collector.snapshot("unknown", "check1")
    with pytest.raises(ValueError):
        collector.snapshot("pr1", "arbitrary")
    assert not github[0].called and not github[1].called


def test_cache_eviction_does_not_redownload_and_retention_removes_logs(
    collector, github, monkeypatch
):
    seed(collector)
    collector.step()
    monkeypatch.setattr(logs, "MAX_CACHE_BYTES", 0)
    collector.prune()
    collector.step()
    assert records(collector)[0][1] == "evicted" and github[1].call_count == 1
    assert not list(collector.root.glob("*.gz"))
    with collector.db() as db:
        db.execute("UPDATE jobs SET updated=?", (time.time() - logs.RETENTION - 1,))
    collector.prune()
    assert not records(collector)


def test_worker_recovers_interrupted_download_and_only_one_worker_holds_lock(collector, github):
    value = seed(collector)
    collector.enqueue("test", collector.overview.value["prs"][0], value)
    with collector.db() as db:
        db.execute("UPDATE jobs SET state='downloading'")
    with (collector.root / "worker.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        collector.run()
        assert records(collector)[0][1] == "downloading" and not github[1].called
    collector.step = Mock(side_effect=collector.close)
    collector.run()
    assert records(collector)[0][1] == "queued"
    assert collector.step.call_count == 1


def test_plaintext_summary_is_bounded_and_finds_pytest_and_unittest():
    data = RAW + b"FAIL: test_read (TestCache)\n" + b"progress\n" * 50000
    result = logs.summarize_log(data)
    assert result["tests"] == ["tests/test_cache.py::test_atomic", "test_read (TestCache)"]
    assert "\x1b" not in result["text"] and "2026-09" not in result["text"]
    assert result["excerpt"] and len(result["text"].encode()) <= logs.EXCERPT_BYTES


def test_truncated_logs_and_unsupported_providers_are_explicit(collector, github):
    seed(collector)
    github[1].return_value = (RAW, True)
    collector.step()
    assert collector.snapshot("pr1", "check1")["value"]["truncated"]
    seed(collector, [{**CHECK, "repository": None}])
    assert collector.snapshot("pr1", "check1")["state"] == "unsupported"


def test_account_switch_cannot_read_another_accounts_log(collector, github):
    seed(collector)
    collector.step()
    collector.overview.value["login"] = "another"
    with pytest.raises(ValueError, match="current CI"):
        collector.raw_log("pr1", "check1")


@pytest.fixture
def fake_gh(tmp_path, monkeypatch):
    executable = tmp_path / "gh"
    executable.write_text(
        f"#!{sys.executable}\n"
        + """import json, os, sys, time
assert sys.argv[1:4] == ['api', '--hostname', 'github.com']
assert sys.argv[-1].startswith('repos/fork/repo/actions/jobs/456')
if not sys.argv[-1].endswith('/logs'):
    print(os.environ['FAKE_JOB'])
else:
    mode = os.environ.get('FAKE_LOG', 'ok')
    if mode == 'error':
        print('expired', file=sys.stderr)
        sys.exit(1)
    if mode == 'slow':
        time.sleep(5)
    sys.stdout.buffer.write(b'FAILED test_example' if mode == 'ok' else b'x' * 100000)
"""
    )
    executable.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("FAKE_JOB", json.dumps(JOB))
    return logs.job_target(CHECK)


def test_fake_gh_individual_job_metadata_and_plaintext_log(fake_gh):
    assert logs.read_job(fake_gh) == JOB
    assert logs.download_log(fake_gh) == (b"FAILED test_example", False)


def test_streaming_download_caps_transfer_and_exposes_errors(fake_gh, monkeypatch):
    monkeypatch.setenv("FAKE_LOG", "large")
    assert logs.download_log(fake_gh, limit=1024) == (b"x" * 1024, True)
    monkeypatch.setenv("FAKE_LOG", "error")
    with pytest.raises(ValueError, match="expired"):
        logs.download_log(fake_gh)
    monkeypatch.setenv("FAKE_LOG", "slow")
    with pytest.raises(TimeoutError, match="timed out"):
        logs.download_log(fake_gh, timeout=0.3)


def test_log_http_protection_and_cached_download(collector, github):
    seed(collector)
    collector.step()
    with dashboard.DashboardServer(
        collector.path.parent, 0, overview=collector.overview, ci=collector.ci, ci_logs=collector
    ) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:

            def request(path, headers=None):
                conn = http.client.HTTPConnection("127.0.0.1", server.server_port)
                conn.request("GET", path, headers=headers or {})
                response = conn.getresponse()
                value = response.status, response.read(), response.getheader("Content-Type")
                conn.close()
                return value

            url = "/api/pr-ci-log?id=pr1&check=check1"
            assert request(url, {"Host": "evil.test"})[0] == 403
            assert request(url, {"Sec-Fetch-Site": "cross-site"})[0] == 403
            assert request("/api/pr-ci-log?id=pr1&check=other")[0] == 400
            result = request(url)
            assert result[0] == 200 and json.loads(result[1])["state"] == "ready"
            status, raw, content_type = request(url + "&download=1")
            assert status == 200 and raw == RAW and content_type.startswith("text/plain")
            paged = request(url + "&page=1")
            assert paged[0] == 200 and json.loads(paged[1])["page"] == 1
            assert "FAILED tests/test_cache.py" in json.loads(paged[1])["text"]
            for suffix in [
                "&page=0",
                "&page=999",
                "&page=no",
                "&page=",
                "&page=1&page=2",
                "&page=1&download=1",
            ]:
                assert request(url + suffix)[0] == 400
            assert request(url + "&page=1", {"Host": "evil.test"})[0] == 403
            assert request(url + "&page=1", {"Sec-Fetch-Site": "cross-site"})[0] == 403
            assert github[1].call_count == 1
        finally:
            server.shutdown()
            thread.join()


def test_log_pages_cover_every_character_with_unicode_and_long_lines(
    collector, github, monkeypatch
):
    monkeypatch.setattr(logs, "PAGE_CHARACTERS", 25)
    text = "start\n" + "α😀" * 40 + "\nlast line\n"
    github[1].return_value = (text.encode(), False)
    seed(collector)
    collector.step()
    first = collector.log_page("pr1", "check1")
    pages = [collector.log_page("pr1", "check1", p) for p in range(1, first["pages"] + 1)]
    assert "".join(p["text"] for p in pages) == text
    assert all(len(p["text"]) <= 25 for p in pages)
    assert pages[0]["line_start"] == 1 and pages[-1]["line_end"] == 3
    assert all(not p["truncated"] for p in pages)
    assert github[1].call_count == 1
    with pytest.raises(ValueError, match="between"):
        collector.log_page("pr1", "check1", first["pages"] + 1)


@pytest.mark.parametrize("page", [0, -1, True, "1"])
def test_invalid_log_pages_rejected_before_cache_access(collector, page):
    with pytest.raises(ValueError, match="positive"):
        collector.log_page("pr1", "check1", page)


def test_empty_and_capped_log_pages_and_missing_cache(collector, github):
    seed(collector)
    github[1].return_value = (b"", True)
    collector.step()
    result = collector.log_page("pr1", "check1")
    assert result == {
        "page": 1,
        "pages": 1,
        "text": "",
        "line_start": 0,
        "line_end": 0,
        "truncated": True,
    }
    next(collector.root.glob("*.gz")).unlink()
    with pytest.raises(FileNotFoundError):
        collector.log_page("pr1", "check1")


def test_log_pages_strip_terminal_escapes_but_preserve_download(collector, github):
    seed(collector)
    collector.step()
    result = collector.log_page("pr1", "check1")
    assert "\x1b" not in result["text"] and "2026-09-12" in result["text"]
    assert collector.raw_log("pr1", "check1") == RAW
    collector.overview.value["prs"][0]["head_sha"] = "new-head"
    with pytest.raises(ValueError, match="current CI"):
        collector.log_page("pr1", "check1")
