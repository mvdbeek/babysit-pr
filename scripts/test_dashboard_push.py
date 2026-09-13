"""Web Push tests use temporary state and a fake transport, never real subscribers."""

import base64
import copy
import http.client
import json
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from dashboard_push import PushInbox, deliver, destination, validate_subscription
from test_dashboard_cancel import server as server

ORIGIN = "https://babysitter.example"
URL = "https://github.com/test/repo/pull/1"


def subscription(suffix="one"):
    public = (
        ec.generate_private_key(ec.SECP256R1())
        .public_key()
        .public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    )
    return {
        "endpoint": f"https://web.push.apple.com/{suffix}",
        "keys": {
            "auth": base64.urlsafe_b64encode(b"a" * 16).decode().rstrip("="),
            "p256dh": base64.urlsafe_b64encode(public).decode().rstrip("="),
        },
    }


@pytest.fixture
def inbox(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("dashboard_push.time.time", lambda: clock[0])
    packets = {
        "prs": {
            "login": "fixture",
            "synced_at": 1000,
            "prs": [
                {"url": URL, "title": "First PR", "updated_at": "2026-01-01", "roles": ["author"]}
            ],
        },
        "issues": {"login": "fixture", "synced_at": 1000, "issues": []},
        "watcher": {"jobs": [{"url": URL, "status": "watching", "updated_at": 1000}]},
    }
    sent = []
    box = PushInbox(tmp_path, lambda: copy.deepcopy(packets), lambda *args: sent.append(args))
    sub = subscription()
    result = box.subscribe({"login": "fixture", "subscription": sub}, ORIGIN)
    identity = {"login": "fixture", "token": result["token"]}
    return box, packets, sent, clock, identity, sub


def read(box, identity):
    return box.action("push-read", identity, ORIGIN)


def change(box, packets, clock, title="Changed"):
    clock[0] += 20
    packets["prs"]["synced_at"] += 20
    packets["prs"]["prs"][0]["title"] = title
    box.tick()


def test_quiet_baseline_grouping_and_durable_seen(inbox, tmp_path):
    box, packets, sent, clock, identity, _ = inbox
    box.tick()
    assert not sent and read(box, identity)["count"] == 0
    change(box, packets, clock)
    packets["watcher"]["jobs"][0].update(status="running", updated_at=1021)
    box.tick()
    assert [args[1]["count"] for args in sent] == [1, 1]
    view = read(box, identity)
    assert len(view["entries"]) == 1 and len(view["entries"][URL]["notes"]) == 2
    ack = {**identity, "seen": {URL: view["entries"][URL]["at"]}}
    assert box.action("push-seen", ack, ORIGIN)["count"] == 0
    box.tick()
    assert len(sent) == 2  # Seeing updates does not produce a new background alert.
    restored = PushInbox(tmp_path, lambda: copy.deepcopy(packets), lambda *args: sent.append(args))
    restored.tick()
    assert read(restored, identity)["count"] == 0 and len(sent) == 2
    assert box.config() == restored.config()
    assert box.path.stat().st_mode & 0o777 == 0o600
    assert box.key_path.stat().st_mode & 0o777 == 0o600


def test_many_updates_count_subjects_and_keep_older_unread(inbox):
    box, packets, sent, clock, identity, _ = inbox
    packets["prs"]["prs"] += [
        {"url": f"https://github.com/test/repo/pull/{i}", "title": str(i)} for i in range(2, 14)
    ]
    change(box, packets, clock)
    assert read(box, identity)["count"] == 13
    assert len(sent) == 1 and sent[0][1]["count"] == 13
    current = read(box, identity)
    box.action(
        "push-seen",
        {**identity, "seen": {url: e["at"] for url, e in current["entries"].items()}},
        ORIGIN,
    )
    box.tick()
    assert len(read(box, identity)["entries"]) == 10


def test_migrates_more_than_ten_existing_unread(inbox):
    box, packets, _, _, _, _ = inbox
    urls = [f"https://github.com/test/repo/pull/{i}" for i in range(20, 40)]
    packets["prs"]["prs"] += [{"url": url, "title": url} for url in urls]
    result = box.subscribe(
        {"login": "fixture", "subscription": subscription("two"), "unseen": urls}, ORIGIN
    )
    assert result["count"] == 20


def test_migrates_history_for_a_now_closed_issue(inbox):
    box, _, _, _, _, _ = inbox
    url = "https://github.com/test/repo/issues/99"
    result = box.subscribe(
        {
            "login": "fixture",
            "subscription": subscription("history"),
            "unseen": [url],
            "history": {url: {"title": "Closed issue", "at": 100, "seenAt": 0}},
        },
        ORIGIN,
    )
    assert result["count"] == 1 and result["entries"][url]["title"] == "Closed issue"


def test_stale_seen_cannot_acknowledge_newer_update(inbox):
    box, packets, _, clock, identity, _ = inbox
    change(box, packets, clock)
    at = read(box, identity)["entries"][URL]["at"]
    change(box, packets, clock, "Another change")
    assert box.action("push-seen", {**identity, "seen": {URL: at}}, ORIGIN)["count"] == 1


def test_stale_errors_and_poll_metadata_do_not_notify(inbox):
    box, packets, sent, clock, identity, _ = inbox
    packets["watcher"]["jobs"][0]["updated_at"] += 10
    packets["prs"]["synced_at"] += 10
    box.tick()
    packets["prs"]["error"] = "Unavailable"
    change(box, packets, clock)
    packets["prs"]["error"] = None
    packets["prs"]["synced_at"] = 900
    box.tick()
    assert not sent and read(box, identity)["count"] == 0


def test_account_and_device_isolation(inbox):
    box, packets, sent, clock, identity, sub = inbox
    other = box.subscribe({"login": "fixture", "subscription": subscription("other")}, ORIGIN)
    change(box, packets, clock)
    view = read(box, identity)
    box.action("push-seen", {**identity, "seen": {URL: view["entries"][URL]["at"]}}, ORIGIN)
    assert read(box, {"token": other["token"], "login": "fixture"})["count"] == 1
    packets["prs"]["login"] = "another"
    change(box, packets, clock, "Private change")
    assert len(sent) == 2
    with pytest.raises(ValueError):
        box.action("push-read", {**identity, "login": "another"}, ORIGIN)
    with pytest.raises(ValueError):
        box.action("push-read", identity, "https://other.example")
    switched = box.subscribe({"login": "another", "subscription": sub}, ORIGIN)
    assert switched["count"] == 0
    with pytest.raises(ValueError):
        read(box, identity)


def test_retry_and_expired_subscription(inbox):
    box, packets, sent, clock, identity, _ = inbox
    calls = []

    def fail(*args):
        calls.append(args)
        raise RuntimeError("Do not leak the endpoint")

    box.sender = fail
    change(box, packets, clock)
    box.tick()
    assert len(calls) == 1 and "delayed" in read(box, identity)["error"]
    clock[0] += 100
    box.sender = lambda *args: sent.append(args)
    box.tick()
    assert len(sent) == 1 and read(box, identity)["error"] is None

    class Gone(Exception):
        status_code = 410

    def expired(*args):
        raise Gone()

    box.sender = expired
    change(box, packets, clock, "Next change")
    assert read(box, identity)["active"] is False
    box.tick()


def test_unsubscribe_stops_polling(inbox):
    box, _, sent, _, identity, _ = inbox
    box.action("push-unsubscribe", identity, ORIGIN)
    box.snapshots = lambda: pytest.fail("No subscribers must mean no background polling")
    box.tick()
    assert not sent


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://web.push.apple.com/a",
        "https://127.0.0.1/a",
        "https://web.push.apple.com.evil.test/a",
        "https://user@web.push.apple.com/a",
        "https://web.push.apple.com:444/a",
    ],
)
def test_rejects_arbitrary_push_destinations(endpoint):
    with pytest.raises(ValueError):
        validate_subscription({**subscription(), "endpoint": endpoint})


@pytest.mark.parametrize("host", ["web.push.apple.com", "regional.push.apple.com"])
def test_accepts_apple_push_subdomains(host):
    value = {**subscription(), "endpoint": f"https://{host}/test"}
    assert validate_subscription(value) == value


@pytest.mark.parametrize("source", ["prs", "issues", "watcher"])
def test_notification_link_uses_latest_source(source):
    from urllib.parse import parse_qs, urlsplit

    route = urlsplit(destination(URL, {"notes": {"prs": {"at": 1}, source: {"at": 2}}}))
    assert route.path == "/" and route.fragment == source
    assert parse_qs(route.query)["item"] == [URL]


@pytest.mark.parametrize(
    "host", ["push.apple.com.evil.test", "evilpush.apple.com", "web.push.apple.com.evil.test"]
)
def test_rejects_lookalike_apple_push_hosts(host):
    with pytest.raises(ValueError, match="Unsupported push service"):
        validate_subscription({**subscription(), "endpoint": f"https://{host}/test"})


def test_validation_and_missing_dependency(inbox):
    box, _, _, _, identity, sub = inbox
    for request in ({"token": []}, {**identity, "seen": {URL: "today"}}):
        with pytest.raises(ValueError):
            box.action("push-seen", request, ORIGIN)
    with pytest.raises(ValueError):
        validate_subscription({**sub, "keys": {"auth": "invalid", "p256dh": "invalid"}})
    box.available = False
    assert box.config()["available"] is False
    with pytest.raises(ValueError):
        box.subscribe({"login": "fixture", "subscription": sub}, ORIGIN)


def test_corrupt_state_does_not_get_overwritten(tmp_path):
    path = tmp_path / "dashboard-push.json"
    path.write_text("broken")
    box = PushInbox(tmp_path, lambda: {})
    assert not box.config()["available"]
    box.tick()
    assert path.read_text() == "broken"


def test_real_transport_encrypts_and_forbids_redirects(inbox, monkeypatch):
    box, _, _, _, _, sub = inbox
    calls = []

    def request(self, method, url, **kwargs):
        calls.append((method, url, kwargs))
        return SimpleNamespace(status_code=201, headers={}, text="")

    monkeypatch.setattr("requests.Session.request", request)
    deliver(sub, {"count": 3, "body": "private update"}, box.key_path, ORIGIN)
    method, url, kwargs = calls[0]
    assert method == "POST" and url == sub["endpoint"]
    assert kwargs["allow_redirects"] is False and kwargs["timeout"] == 15
    assert b"private update" not in kwargs["data"]
    assert kwargs["headers"]["content-encoding"] == "aes128gcm"


def test_http_push_routes_enforce_origin_and_token(server, inbox):
    box, _, _, _, _, sub = inbox
    server.push = box

    def post(action, data, origin=None):
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port)
        headers = {"Content-Type": "application/json", "X-Babysit-Action": action}
        if origin:
            headers["Origin"] = origin
        conn.request("POST", f"/api/{action}", json.dumps(data), headers)
        response = conn.getresponse()
        result = response.status, json.loads(response.read())
        conn.close()
        return result

    data = {"login": "fixture", "subscription": sub}
    assert post("push-subscribe", data, "https://evil.test")[0] == 403
    code, result = post("push-subscribe", data)
    assert code == 200
    identity = {"login": "fixture", "token": result["token"]}
    assert post("push-read", identity)[0] == 200
    assert post("push-seen", {**identity, "seen": {}})[0] == 200
    assert post("push-unsubscribe", identity)[0] == 200
    assert post("push-read", identity)[0] == 400
