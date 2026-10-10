"""CI detail and log workers under failures: backoff, unexpected errors, no write churn.

Fake GitHub responses and temporary state only.
"""

import json
import threading
import time
from unittest.mock import Mock

import pr_ci as ci
import pr_ci_logs as logs
import pytest
from pr_overview import Overview

SHA = "a" * 40
CHECK = {
    "id": "check1",
    "name": "pytest",
    "bucket": "fail",
    "repository": "fork/repo",
    "run_id": 123,
    "database_id": 456,
    "url": "https://github.com/fork/repo/actions/runs/123/job/456",
}


@pytest.fixture
def cache(tmp_path):
    overview = Overview(tmp_path)
    overview.next_poll = float("inf")
    overview.value.update(
        login="test",
        synced_at=time.time(),
        prs=[{"id": "pr1", "head_sha": SHA, "ci": "FAILURE", "updated_at": "2026-10-01T10:00:00Z"}],
    )
    return ci.CiDetails(tmp_path, overview)


def wait(cache):
    with cache.lock:
        workers = list(cache.workers.values())
    for worker in workers:
        worker.join(5)
        assert not worker.is_alive()


def test_a_failed_review_fetch_waits_out_its_backoff(cache, monkeypatch):
    fetch = Mock(side_effect=ValueError("rate limited"))
    monkeypatch.setattr(ci, "collect_reviews", fetch)
    cache.snapshot("pr1", reviews=True)
    wait(cache)
    # Before: the failed entry has no value, so updated_at never matched and every
    # ~2 s poll refetched.
    for _ in range(5):
        result = cache.snapshot("pr1", reviews=True)
        assert result["error"] == "rate limited" and not result["refreshing"]
        wait(cache)
    assert fetch.call_count == 1
    # Once the retry delay passes it is fetched again, and new activity still counts.
    fetch.side_effect = lambda pr: {
        "reviews": [],
        "threads": [],
        "for_updated_at": pr["updated_at"],
    }
    with cache.lock:
        next(iter(cache.entries.values()))["expires_at"] = 0
    cache.snapshot("pr1", reviews=True)
    wait(cache)
    assert cache.snapshot("pr1", reviews=True)["error"] is None and fetch.call_count == 2
    cache.overview.value["prs"][0]["updated_at"] = "2026-10-01T11:00:00Z"
    assert cache.snapshot("pr1", reviews=True)["refreshing"]
    wait(cache)
    assert fetch.call_count == 3


def test_a_cache_save_failure_is_shown_but_does_not_delay_a_refetch(cache, monkeypatch):
    fetch = Mock(side_effect=lambda pr: {"reviews": [], "for_updated_at": pr["updated_at"]})
    monkeypatch.setattr(ci, "collect_reviews", fetch)
    original = ci.os.open

    def full(path, *args, **kwargs):
        if str(path).endswith(".tmp"):
            raise OSError("No space left on device")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(ci.os, "open", full)
    cache.snapshot("pr1", reviews=True)
    wait(cache)
    result = cache.snapshot("pr1", reviews=True)
    assert result["error"] == "CI details loaded, but the local cache could not be saved"
    assert result["value"] and not result["refreshing"]
    # Before: the save failure counted as a failed load, so new activity waited out the TTL.
    cache.overview.value["prs"][0]["updated_at"] = "2026-10-01T11:00:00Z"
    assert cache.snapshot("pr1", reviews=True)["refreshing"]
    wait(cache)
    assert fetch.call_count == 2


def test_an_unexpected_loader_error_ends_the_refresh(cache, monkeypatch, capsys):
    fetch = Mock(side_effect=RuntimeError("bug"))
    monkeypatch.setattr(ci, "collect_checks", fetch)
    cache.snapshot("pr1")
    wait(cache)
    # Before: the worker never left `workers`, so this PR showed "refreshing" forever
    # and two such failures blocked every other CI request.
    result = cache.snapshot("pr1")
    assert not result["refreshing"] and result["error"] == "bug"
    assert not cache.workers and fetch.call_count == 1
    assert "RuntimeError: bug" in capsys.readouterr().err


def test_the_cache_file_is_written_outside_the_lock_in_order(cache, monkeypatch):
    monkeypatch.setattr(ci, "collect_checks", lambda pr: {"checks": [], "sha": SHA})
    writing = threading.Event()
    release = threading.Event()
    original = ci.os.fdopen

    def slow(fd, *args, **kwargs):
        stream = original(fd, *args, **kwargs)
        if threading.current_thread().name.endswith("(refresh)"):
            writing.set()
            assert release.wait(5)
        return stream

    monkeypatch.setattr(ci.os, "fdopen", slow)
    cache.snapshot("pr1")
    assert writing.wait(5)
    try:
        # Readers are not held up while the 2 MB cache file is written.
        assert cache.lock.acquire(timeout=1)
        cache.lock.release()
    finally:
        release.set()
    wait(cache)
    saved = json.loads(cache.path.read_text())
    assert list(saved) == list(cache.entries) and cache.saved == cache.version == 1


@pytest.fixture
def collector(cache):
    pr = cache.overview.value["prs"][0]
    cache.entries[cache.key("test", pr)] = {
        "value": {"sha": SHA, "checks": [CHECK]},
        "expires_at": time.time() + 300,
        "error": None,
    }
    return logs.BackgroundLogs(cache.path.parent, cache.overview, cache)


def test_log_status_polls_only_read_the_budget(collector, monkeypatch):
    collector.enqueue("test", collector.overview.value["prs"][0], {"sha": SHA, "checks": [CHECK]})
    with collector.db() as db:
        db.execute("INSERT INTO requests VALUES ('download', ?)", (time.time() - 7200,))
    statements = []
    original = collector.db

    def traced():
        context = original()
        db = context.__enter__()
        db.set_trace_callback(statements.append)

        class Wrapper:
            def __enter__(self):
                return db

            def __exit__(self, *exc):
                return context.__exit__(*exc)

        return Wrapper()

    monkeypatch.setattr(collector, "db", traced)
    result = collector.snapshot("pr1", "check1")
    assert result["state"] == "queued" and result["refreshing"]
    writes = [
        s for s in statements if s.split()[0].upper() in {"BEGIN", "DELETE", "INSERT", "UPDATE"}
    ]
    assert writes == []
    assert sum("FROM requests" in s for s in statements) == 1
    # The worker's claim still prunes and records.
    assert collector.budget("download", logs.DOWNLOADS_PER_HOUR, claim=True)
    with original() as db:
        assert db.execute("SELECT count(*) FROM requests").fetchone()[0] == 1


def test_an_unexpected_download_error_fails_the_job_and_the_worker_lives_on(
    collector, monkeypatch, capsys
):
    monkeypatch.setattr(logs, "read_job", Mock(side_effect=RuntimeError("bug")))
    collector.step()
    with collector.db() as db:
        state, data = db.execute("SELECT state,data FROM jobs").fetchone()
    # Before: the job stayed "downloading" and the exception ended the worker thread.
    assert state == "error" and json.loads(data)["error"] == "bug"
    assert "RuntimeError: bug" in capsys.readouterr().err

    def broken():
        with collector.db() as db:
            db.execute("UPDATE jobs SET state='downloading'")
        raise AttributeError("other bug")

    monkeypatch.setattr(collector, "step", broken)
    monkeypatch.setattr(collector.stopping, "wait", lambda timeout: collector.stopping.set())
    collector.run()  # Returns once stopped, rather than dying on the first step.
    assert collector.error == "other bug"
    with collector.db() as db:
        assert db.execute("SELECT state FROM jobs").fetchone()[0] == "error"
