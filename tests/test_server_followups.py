"""Dashboard server traffic: ended watches' CI details, keep-alive, and bounded stores.

Temporary state, a temporary watcher queue and fake providers only.
"""

import copy
import gzip
import http.client
import json
import os
import socket
import threading
import time

import attachments
import dashboard
import dashboard_push
import pytest
from pr_supervisor import open_db, save_job

CLIENT = "a" * 32
PR = "https://github.com/test/repo/pull/{}"


def checks(count, failed=0):
    return [
        {
            "name": f"test ({n}, ubuntu-latest, 3.{n % 14})",
            "bucket": "fail" if n < failed else "pass",
            "state": "FAILURE" if n < failed else "SUCCESS",
            "link": f"https://github.com/test/repo/actions/runs/{1000 + n}/job/{9000 + n}",
            "workflow": "Tests",
            "event": "pull_request",
            "startedAt": "2026-10-01T10:00:00Z",
            "completedAt": "2026-10-01T10:20:00Z",
        }
        for n in range(count)
    ]


def job(number, status, details, failed_jobs=()):
    return {
        "id": f"job-{number}",
        "url": PR.format(number),
        "repo": "test/repo",
        "status": status,
        "updated_at": 1000 + number,
        "snapshot": {
            "pr": {"number": number, "title": f"PR {number}", "head_sha": "a" * 40},
            "check_details": details,
            "failed_jobs": list(failed_jobs),
        },
    }


def queue(home, jobs):
    db = open_db(home)
    try:
        for item in jobs:
            save_job(db, item)
        db.commit()
    finally:
        db.close()


@pytest.fixture
def serve(tmp_path):
    servers = []

    def start(**providers):
        server = dashboard.DashboardServer(tmp_path, 0, **providers)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        servers.append((server, thread))
        return server

    yield start
    for server, thread in servers:
        server.shutdown()
        thread.join(5)
        server.server_close()


def connect(server):
    return http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)


def ask(conn, method, path, body=None, headers=None):
    conn.request(method, path, body, {"Host": f"127.0.0.1:{conn.port}", **(headers or {})})
    response = conn.getresponse()
    return response.status, dict(response.getheaders()), response.read()


# -- ended watches' CI details --


def test_ended_watches_omit_ci_details_from_the_poll_but_keep_their_result(tmp_path, serve):
    failed = [{"run_id": 1, "job_id": 2, "name": "test (0)"}]
    queue(
        tmp_path,
        [
            job(1, "watching", checks(5, failed=1), failed),
            job(2, "closed", checks(5, failed=1), failed),
            job(3, "stopped", checks(3)),
            job(4, "blocked", checks(2, failed=2), failed),
        ],
    )
    jobs = {item["id"]: item for item in dashboard.status(tmp_path)["jobs"]}
    for key in ("job-1", "job-4"):
        assert len(jobs[key]["check_details"]) == len(checks(5 if key == "job-1" else 2))
        assert jobs[key]["failed_jobs"] == failed and "details_omitted" not in jobs[key]
    for key in ("job-2", "job-3"):
        assert jobs[key]["check_details"] == jobs[key]["failed_jobs"] == []
        assert jobs[key]["details_omitted"] is True
    assert {key: item["checks_result"] for key, item in jobs.items()} == {
        "job-1": "FAILURE",
        "job-2": "FAILURE",
        "job-3": "SUCCESS",
        "job-4": "FAILURE",
    }
    server = serve()
    conn = connect(server)
    code, _, body = ask(conn, "GET", "/api/watch?id=job-2")
    full = json.loads(body)["job"]
    assert code == 200 and full["status"] == "closed" and full["checks_result"] == "FAILURE"
    assert full["check_details"] == checks(5, failed=1) and full["failed_jobs"] == failed
    assert not full.get("details_omitted")
    assert {k: v for k, v in full.items() if k not in {"check_details", "failed_jobs"}} == {
        k: v
        for k, v in jobs["job-2"].items()
        if k not in {"check_details", "failed_jobs", "details_omitted"}
    }
    for path in ("/api/watch?id=missing", "/api/watch"):
        code, _, body = ask(conn, "GET", path)
        assert (code, json.loads(body)) == (404, {"error": "Unknown watch"})
    # The same Host and cross-site rules as every other API route.
    assert ask(conn, "GET", "/api/watch?id=job-2", headers={"Host": "evil.test"})[0] == 403
    cross = {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate"}
    assert ask(conn, "GET", "/api/watch?id=job-2", headers=cross)[0] == 403
    conn.close()


def test_push_samples_use_the_result_of_omitted_details():
    url = PR.format(1)
    omitted = {"url": url, "status": "closed", "check_details": [], "checks_result": "FAILURE"}
    legacy = {"url": url, "status": "closed", "check_details": checks(2, failed=1)}
    for item in (omitted, legacy):
        (sample,) = dashboard_push.samples("watcher", {"jobs": [item]})
        assert sample["values"]["checks"] == "FAILURE"


def test_status_with_many_ended_watches_is_much_smaller(tmp_path):
    # Measured: 1,397,190 -> 104,430 bytes; gzipped 23,336 -> 3,211.
    jobs = [job(n, "closed" if n % 2 else "stopped", checks(40, failed=n % 3)) for n in range(120)]
    queue(tmp_path, jobs)
    after = json.dumps(dashboard.status(tmp_path)).encode()
    full = dashboard.status(tmp_path)
    full["jobs"] = [dashboard.present_job(item) for item in jobs]
    before = json.dumps(full).encode()
    packed = [len(gzip.compress(body, dashboard.COMPRESS_LEVEL)) for body in (before, after)]
    assert len(after) < len(before) / 5 and packed[1] < packed[0] / 3


# -- keep-alive --


def test_requests_share_one_connection(serve):
    server = serve()
    conn = connect(server)
    code, headers, _ = ask(conn, "GET", "/api/codex-update")
    sock = conn.sock
    assert code == 200 and "Connection" not in headers and sock is not None
    # GET, 304, 4xx, POST and a CORS preflight each leave the connection ready.
    code, headers, _ = ask(conn, "GET", "/app.js")
    assert code == 200 and conn.sock is sock
    code, _, body = ask(conn, "GET", "/app.js", headers={"If-None-Match": headers["ETag"]})
    assert code == 304 and body == b"" and conn.sock is sock
    assert ask(conn, "GET", "/api/codex-update")[0] == 200 and conn.sock is sock
    assert ask(conn, "GET", "/missing")[0] == 404 and conn.sock is sock
    assert ask(conn, "GET", "/api/codex-update")[0] == 200 and conn.sock is sock
    action = {"Content-Type": "application/json", "X-Babysit-Action": "extension-pair"}
    code, _, body = ask(conn, "POST", "/api/extension-pair", json.dumps({"id": CLIENT}), action)
    assert code == 200 and json.loads(body)["token"] and conn.sock is sock
    assert ask(conn, "GET", "/api/codex-update")[0] == 200 and conn.sock is sock
    preflight = {
        "Origin": f"chrome-extension://{CLIENT}",
        "Access-Control-Request-Method": "POST",
    }
    code, headers, body = ask(conn, "OPTIONS", "/api/extension", headers=preflight)
    assert code == 204 and body == b"" and "Content-Length" not in headers
    assert headers["Access-Control-Allow-Origin"] == preflight["Origin"]
    assert ask(conn, "GET", "/api/codex-update")[0] == 200 and conn.sock is sock
    conn.close()


def test_a_post_refused_before_its_body_was_read_does_not_desync_the_next(serve):
    server = serve()
    conn = connect(server)
    ask(conn, "GET", "/api/codex-update")
    sock = conn.sock
    action = {"Content-Type": "application/json", "X-Babysit-Action": "cancel"}
    # Refused by origin, by route, by size and by type before the body is read: it is
    # read and dropped, never taken for the next request.
    for path, body, headers in [
        ("/api/cancel", '{"id": "x"}', {**action, "Origin": "https://evil.test"}),
        ("/api/other", '{"id": "x"}', action),
        ("/api/cancel", json.dumps({"id": "x" * 5000}), action),
        ("/api/cancel", '{"id": "x"}', {**action, "Content-Type": "text/plain"}),
        ("/api/extension", '{"action": "context"}', {"Content-Type": "application/json"}),
    ]:
        code, response, _ = ask(conn, "POST", path, body, headers)
        assert code in {400, 403, 404} and "Connection" not in response
        code, _, data = ask(conn, "GET", "/api/codex-update")
        assert code == 200 and json.loads(data) and conn.sock is sock
    # An action that fails after reading its body keeps the connection too.
    code, _, data = ask(conn, "POST", "/api/cancel", '{"id": ""}', action)
    assert (code, json.loads(data)) == (400, {"error": "Supply a watch ID"})
    assert ask(conn, "GET", "/api/codex-update")[0] == 200 and conn.sock is sock
    conn.close()


def test_a_body_that_cannot_be_read_ends_the_connection(serve):
    server = serve()
    conn = connect(server)
    action = {"Content-Type": "application/json", "X-Babysit-Action": "cancel"}
    # A chunked body is never read: the response says the connection ends.
    conn.putrequest("POST", "/api/cancel", skip_host=True)
    for name, value in {
        "Host": f"127.0.0.1:{server.server_port}",
        **action,
        "Transfer-Encoding": "chunked",
    }.items():
        conn.putheader(name, value)
    conn.endheaders(b'b\r\n{"id": "x"}\r\n0\r\n\r\n')
    response = conn.getresponse()
    assert response.status == 400 and response.getheader("Connection") == "close"
    response.read()
    # http.client opens a new connection for the next request, which is served.
    assert ask(conn, "GET", "/api/codex-update")[0] == 200
    conn.close()
    # A declared body too large to drop is not waited for either.
    with socket.create_connection(("127.0.0.1", server.server_port), timeout=10) as raw:
        raw.sendall(
            f"POST /api/attachment-upload HTTP/1.1\r\nHost: 127.0.0.1:{server.server_port}\r\n"
            "X-Babysit-Action: attachment-upload\r\nContent-Type: application/octet-stream\r\n"
            f"Content-Length: {attachments.MAX_FILE + 1}\r\n\r\npartial".encode()
        )
        received = b""
        while chunk := raw.recv(65536):
            received += chunk
    head, _, body = received.partition(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 400") and b"\r\nConnection: close" in head
    assert b"at most 25 MB" in body


def test_an_unexpected_error_ends_the_connection(serve, capsys):
    class Broken:
        def snapshot(self):
            raise RuntimeError("provider exploded")

    server = serve(overview=Broken())
    conn = connect(server)
    code, headers, _ = ask(conn, "GET", "/api/prs")
    assert code == 500 and headers["Connection"] == "close"
    assert ask(conn, "GET", "/api/codex-update")[0] == 200
    conn.close()
    assert "provider exploded" in capsys.readouterr().err


def test_http_1_0_clients_get_one_response_per_connection(serve):
    server = serve()
    with socket.create_connection(("127.0.0.1", server.server_port), timeout=10) as raw:
        raw.sendall(
            f"GET /api/codex-update HTTP/1.0\r\nHost: 127.0.0.1:{server.server_port}\r\n\r\n".encode()
        )
        received = b""
        while chunk := raw.recv(65536):
            received += chunk
    assert received.startswith(b"HTTP/1.1 200") and b"\r\nConnection: close" in received


# -- push baselines --


def overview(at, *items):
    return {
        "login": "fixture",
        "synced_at": at,
        "prs": [
            {"url": PR.format(n), "title": title, "ci": "SUCCESS", "updated_at": "2026-01-01"}
            for n, title in items
        ],
    }


def test_push_baselines_forget_items_missing_for_a_day_but_keep_unread_entries():
    device = {"login": "fixture", "baselines": {}, "entries": {}}
    hour = 3600 * 1000
    one, two = PR.format(1), PR.format(2)
    dashboard_push.observe(device, "prs", overview(1000, (1, "One"), (2, "Two")), 1000 * 1000)
    baseline = device["baselines"]["prs"]
    assert set(baseline) == set(device["outcomes"]) == {one, two}
    # Two changes, so it is unread, then drops out of the overview.
    dashboard_push.observe(device, "prs", overview(1100, (1, "One"), (2, "Two!")), 1100 * 1000)
    assert dashboard_push.unread(device) == {two: device["entries"][two]["at"]}
    now = 1200 * 1000
    dashboard_push.observe(device, "prs", overview(1200, (1, "One")), now)
    assert baseline[two]["missing"] == now and "missing" not in baseline[one]
    # A return within the day compares as usual: unchanged, nothing new.
    dashboard_push.observe(device, "prs", overview(1300, (1, "One"), (2, "Two!")), now + 5 * hour)
    assert "missing" not in baseline[two]
    assert dashboard_push.unread(device) == {two: device["entries"][two]["at"]}
    unread = copy.deepcopy(device["entries"])
    # Missing again: kept for a day, then forgotten with its CI outcome.
    for hours in (6, 20, 29.9):
        dashboard_push.observe(device, "prs", overview(1400, (1, "One")), now + hours * hour)
        assert two in baseline
    assert baseline[two]["missing"] == now + 6 * hour
    dashboard_push.observe(device, "prs", overview(1500, (1, "One")), now + 30 * hour)
    assert set(baseline) == set(device["outcomes"]) == {one}
    # The unread inbox entry survives its baseline.
    assert device["entries"] == unread and two in dashboard_push.unread(device)
    # Incomplete snapshots mark nothing missing.
    for payload in (
        {**overview(1600), "error": "GitHub is down"},
        {**overview(1600), "refreshing": True},
        {**overview(1600), "login": "someone-else"},
    ):
        dashboard_push.observe(device, "prs", payload, now + 60 * hour)
    assert "missing" not in baseline[one]


def test_push_state_on_disk_stays_bounded(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("dashboard_push.time.time", lambda: clock[0])
    packets = {"prs": overview(1000), "issues": {}, "watcher": {"jobs": []}}
    box = dashboard_push.PushInbox(tmp_path, lambda: copy.deepcopy(packets), lambda *a: None)
    box.available = True
    box.devices["token"] = {
        "login": "fixture",
        "origin": "https://babysitter.example",
        "subscription": {"endpoint": "https://web.push.apple.com/fixture", "keys": {}},
        "baselines": {},
        "entries": {},
        "sent": {},
        "retry_at": float("inf"),  # Nothing is delivered.
        "failures": 0,
    }
    # Three days of hourly overviews that each list 10 other PRs: a day's worth is kept.
    for hour in range(3 * 24):
        clock[0] += 3600
        first = hour * 10
        packets["prs"] = overview(clock[0], *((n, f"PR {n}") for n in range(first, first + 10)))
        box.tick()
    stored = json.loads((tmp_path / "dashboard-push.json").read_text())
    (device,) = stored["devices"].values()
    assert 24 * 10 <= len(device["baselines"]["prs"]) <= 26 * 10
    assert set(device["outcomes"]) == set(device["baselines"]["prs"])


# -- attachments --


def aged(path, days):
    when = time.time() - days * 86400
    os.utime(path, (when, when), follow_symlinks=False)
    return path


def test_only_old_uploads_the_dashboard_saved_are_pruned(tmp_path, monkeypatch):
    monkeypatch.setattr(attachments, "_pruned", {})
    folder = tmp_path / "uploads"
    folder.mkdir()

    def upload(days, name="shot.png", written=None):
        date = time.strftime("%Y-%m-%d", time.localtime(time.time() - days * 86400))
        path = folder / f"{date}-0123abcd-{name}"
        path.write_bytes(name.encode())
        return aged(path, days if written is None else written)

    days = attachments.KEEP_SECONDS // 86400
    old = upload(days + 30)
    recent = upload(10)
    touched = upload(days + 30, "touched.png", written=1)  # Dated long ago, written lately.
    renamed = upload(days + 10, "renamed.png")
    # Names the dashboard never gives.
    for path in (folder / "notes.txt", folder / (old.name + "\n")):
        path.write_text("someone else's")
        aged(path, days + 30)
    lookalike = folder / old.name.replace("shot.png", "dir")
    lookalike.mkdir()
    aged(lookalike, days + 30)
    outside = tmp_path / "outside.txt"
    outside.write_text("target")
    aged(outside, days + 30)
    link = folder / old.name.replace("shot.png", "link.txt")
    link.symlink_to(outside)
    aged(link, days + 30)
    keep = sorted(p.name for p in folder.iterdir() if p not in (old, renamed))
    attachments.prune(folder)
    assert not old.exists() and not renamed.exists()
    assert sorted(p.name for p in folder.iterdir()) == keep
    assert recent.exists() and touched.exists() and lookalike.is_dir()
    assert link.is_symlink() and outside.read_text() == "target"
    # At most hourly: an upload that ages meanwhile waits for the next pass.
    stale = upload(days + 1, "stale.png")
    attachments.prune(folder)
    assert stale.exists()
    attachments.prune(folder, time.time() + 3601)
    assert not stale.exists()
    # Saving prunes too, and keeps the new upload.
    monkeypatch.setattr(attachments, "_pruned", {})
    again = upload(days + 20, "again.png")
    saved = attachments.save(folder, "new.png", b"new")
    assert not again.exists() and (folder / saved["id"]).read_bytes() == b"new"


def test_uploads_outlive_the_latest_start_a_scheduled_batch_can_have():
    import pr_workspaces as pw

    latest = (
        pw.SCHEDULE_HORIZON
        + (pw.SCHEDULE_PENDING_LIMIT - 1) * pw.BATCH_INTERVAL_LIMIT
        + pw.SCHEDULE_MISSED_AFTER
    )
    assert attachments.LATEST_START_SECONDS == latest
    assert attachments.KEEP_SECONDS > latest


def test_a_missing_attachments_directory_is_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(attachments, "_pruned", {})
    attachments.prune(tmp_path / "missing")
    assert not (tmp_path / "missing").exists()
