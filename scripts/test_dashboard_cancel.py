"""Exercise cancellation through HTTP against an isolated queue, never live watches."""

import http.client
import json
import threading

import dashboard
import pr_supervisor as supervisor
import pytest


@pytest.mark.parametrize("draft", [True, False, None])
def test_present_job_draft_state(draft):
    job = {"id": "pr", "snapshot": {"pr": {"draft": draft}}}
    assert dashboard.present_job(job)["draft"] is draft
    assert dashboard.present_job({**job, "branch": "dev"})["draft"] is None


@pytest.fixture
def server(tmp_path):
    db = supervisor.open_db(tmp_path)
    with db:
        for state in (
            "watching",
            "running",
            "handoff",
            "awaiting_release",
            "paused",
            "blocked",
            "closed",
            "stopped",
        ):
            supervisor.save_job(
                db,
                {
                    "id": state,
                    "status": state,
                    "epoch": 2,
                    "dispatch_ready": True,
                    "attempt": "original-attempt",
                },
            )
    db.close()
    with dashboard.DashboardServer(tmp_path, 0, ["example.ts.net:8443"]) as httpd:
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        yield httpd
        httpd.shutdown()
        thread.join(timeout=5)


def request(server, job="watching", *, headers=None, body=None, method="POST"):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    request_headers = {"Content-Type": "application/json", "X-Babysit-Action": "cancel"}
    if headers:
        request_headers.update(headers)
    conn.request(
        method, "/api/cancel", json.dumps({"id": job}) if body is None else body, request_headers
    )
    response = conn.getresponse()
    result = response.status, json.loads(response.read())
    conn.close()
    return result


def saved(server, key):
    return next(j for j in dashboard.read_jobs(server.home) if j["id"] == key)


@pytest.mark.parametrize("state", ["watching", "handoff", "awaiting_release", "paused", "blocked"])
def test_cancel_stops_dispatch_and_invalidates_inflight_poll(server, state):
    code, result = request(server, state)
    assert code == 200
    assert result["job"]["status"] == "stopped"
    job = saved(server, state)
    assert job["epoch"] == 3 and not job["dispatch_ready"]
    assert saved(server, "running")["status"] == "running"
    assert request(server, state)[0] == 200
    assert saved(server, state)["epoch"] == 3


def test_running_repair_retains_ownership_until_completion(server):
    code, result = request(server, "running")
    assert code == 200
    assert result["job"]["status"] == "running"
    assert result["job"]["stop_after_run"]
    job = saved(server, "running")
    assert job["attempt"] == "original-attempt" and not job["dispatch_ready"]
    assert request(server, "running")[0] == 200
    assert saved(server, "running")["epoch"] == 3


@pytest.mark.parametrize("state", ["closed", "stopped"])
def test_ended_watches_are_unchanged(server, state):
    before = saved(server, state)
    assert request(server, state)[0] == 200
    assert saved(server, state) == before


@pytest.mark.parametrize(
    "headers",
    [
        {"Host": "attacker.invalid"},
        {"X-Babysit-Action": ""},
        {"Origin": "https://attacker.invalid"},
        {"Origin": "null"},
        {"Sec-Fetch-Site": "cross-site"},
    ],
)
def test_cross_site_mutations_are_rejected(server, headers):
    assert request(server, headers=headers)[0] == 403
    assert saved(server, "watching")["status"] == "watching"


def test_tailnet_same_origin_cancellation(server):
    assert (
        request(
            server, headers={"Host": "example.ts.net:8443", "Origin": "https://example.ts.net:8443"}
        )[0]
        == 200
    )


@pytest.mark.parametrize("body", ["[]", "{}", "{", '{"id":42}', "x" * 1025])
def test_invalid_input_cannot_cancel(server, body):
    assert request(server, body=body)[0] == 400
    assert saved(server, "watching")["status"] == "watching"


def test_unknown_watch_and_get_do_not_mutate(server):
    assert request(server, "unknown")[0] == 400
    assert request(server, method="GET")[0] == 404
    assert saved(server, "watching")["status"] == "watching"


def test_cancellation_does_not_create_missing_queue(tmp_path):
    with pytest.raises(Exception, match="unable to open database"):
        dashboard.cancel_watch(tmp_path, "unknown")
    assert not (tmp_path / "queue.sqlite").exists()
