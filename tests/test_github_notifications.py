"""Notification checks use fake gh output, never the live account."""

import json
import subprocess
from unittest.mock import Mock

import github_notifications as notifications
import owned_process
import pytest


def response(status=200, threads=(), poll="60", modified="Sat, 10 Oct 2026 10:00:00 GMT"):
    reason = {200: "OK", 304: "Not Modified"}[status]
    head = f"HTTP/2.0 {status} {reason}\r\nX-Poll-Interval: {poll}\r\nLast-Modified: {modified}\r\n"
    body = f"\r\n{json.dumps(list(threads))}" if status == 200 else ""
    return subprocess.CompletedProcess(
        [], 0 if status == 200 else 1, head + body, "" if status == 200 else "gh: HTTP 304"
    )


def thread(key="1", kind="PullRequest", updated="2026-10-10T10:00:00Z"):
    return {"id": key, "updated_at": updated, "subject": {"type": kind, "title": "private"}}


@pytest.fixture
def gh(monkeypatch):
    run = Mock()
    monkeypatch.setattr(owned_process, "run", run)
    return run


@pytest.fixture
def watch():
    return notifications.NotificationWatch({"prs": Mock(), "issues": Mock()})


def test_request_is_conditional_and_parses_headers(gh):
    gh.return_value = response(threads=[thread()])
    status, headers, threads = notifications.request()
    assert (status, headers["x-poll-interval"], threads) == (200, "60", [thread()])
    args = gh.call_args.args[0]
    assert args == ["gh", "api", "--hostname", "github.com", "--include", notifications.ENDPOINT]
    assert "participating=true" in args[-1] and "all=true" in args[-1]
    gh.return_value = response(304)
    assert notifications.request("then")[:2] == (
        304,
        {"x-poll-interval": "60", "last-modified": "Sat, 10 Oct 2026 10:00:00 GMT"},
    )
    assert gh.call_args.args[0][-2:] == ["--header", "If-Modified-Since: then"]


@pytest.mark.parametrize(
    "result",
    [
        subprocess.CompletedProcess([], 1, "", "HTTP 403: Missing the 'notifications' scope"),
        subprocess.CompletedProcess([], 1, "HTTP/2.0 403 Forbidden\n\n{}", "gh: HTTP 403"),
        subprocess.CompletedProcess([], 0, "HTTP/2.0 200 OK\n\n{}", ""),
    ],
)
def test_request_rejects_failures_and_unexpected_bodies(gh, result):
    gh.return_value = result
    with pytest.raises(ValueError):
        notifications.request()


def woken(watch):
    found = {key for key, overview in watch.overviews.items() if overview.wake.called}
    for overview in watch.overviews.values():
        overview.wake.reset_mock()
    return found


def test_first_answer_is_a_baseline_and_later_threads_wake_their_overview(gh, watch):
    old = [thread(), thread("2", "Issue", "2026-10-10T09:00:00Z")]
    gh.return_value = response(threads=old)
    assert watch.tick() == 60
    assert woken(watch) == set()

    gh.return_value = response(304)
    assert watch.tick() == 60
    assert gh.call_args.args[0][-1] == "If-Modified-Since: Sat, 10 Oct 2026 10:00:00 GMT"
    assert woken(watch) == set()

    # Unchanged threads and subjects no overview lists wake nothing.
    newer = [thread("3", "Release", "2026-10-10T11:00:00Z"), *old]
    gh.return_value = response(threads=newer, modified="later")
    watch.tick()
    assert woken(watch) == set() and watch.last_modified == "later"

    # A late-delivered issue thread older than the newest PR thread still wakes issues.
    gh.return_value = response(threads=[*newer, thread("4", "Issue", "2026-10-10T09:30:00Z")])
    watch.tick()
    assert woken(watch) == {"issues"}


def test_every_wake_is_repeated_on_the_next_check_for_search_lag(gh, watch):
    gh.return_value = response(threads=[thread()])
    watch.tick()
    gh.return_value = response(threads=[thread(updated="2026-10-10T10:01:00Z")])
    watch.tick()
    assert woken(watch) == {"prs"}
    gh.return_value = response(304)
    watch.tick()
    assert woken(watch) == {"prs"}
    watch.tick()
    assert woken(watch) == set()


def test_check_suites_wake_prs_and_reading_a_thread_wakes_nothing(gh, watch):
    gh.return_value = response(threads=[thread()])
    watch.tick()
    read = {**thread(), "unread": False}
    gh.return_value = response(threads=[read])
    watch.tick()
    assert woken(watch) == set()
    gh.return_value = response(threads=[thread("2", "CheckSuite", "2026-10-10T10:01:00Z"), read])
    watch.tick()
    assert woken(watch) == {"prs"}


def test_resurfacing_old_thread_is_not_news(gh, watch):
    gh.return_value = response(threads=[thread("new", updated="2026-10-10T10:00:00Z")])
    watch.tick()
    # The newer thread was removed, so an older one moves up into the page.
    gh.return_value = response(threads=[thread("old", updated="2026-10-01T10:00:00Z")])
    watch.tick()
    assert woken(watch) == set()


def test_empty_baseline_wakes_on_the_first_thread(gh, watch):
    gh.return_value = response(threads=[])
    watch.tick()
    gh.return_value = response(threads=[thread()])
    watch.tick()
    assert woken(watch) == {"prs"}


@pytest.mark.parametrize("poll, expected", [("120", 120), ("5", 60), ("soon", 60)])
def test_honours_github_poll_interval_with_a_floor(gh, watch, poll, expected):
    gh.return_value = response(poll=poll)
    assert watch.tick() == expected


@pytest.mark.parametrize(
    "error", [OSError("no gh"), subprocess.TimeoutExpired("gh", 45), ValueError("bad")]
)
def test_failures_back_off_without_logging_private_details(gh, watch, caplog, error):
    gh.side_effect = error
    assert watch.tick() == notifications.RETRY_SECONDS
    assert "no gh" not in caplog.text and "bad" not in caplog.text


def test_watch_thread_stops_promptly(gh, watch):
    gh.return_value = response(304)
    watch.start()
    watch.close()
    assert not watch.worker.is_alive()
