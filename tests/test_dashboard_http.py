"""HTTP transport of the dashboard: compression, caching, failures and disconnects.

Every server runs on a free loopback port with temporary state and fake providers.
"""

import email.message
import gzip
import http.client
import json
import sqlite3
import threading
import time

import dashboard
import pytest
from pr_overview import Overview
from pr_supervisor import open_db, save_job


@pytest.fixture
def overview(tmp_path):
    value = Overview(tmp_path)
    value.next_poll = float("inf")  # Never contact GitHub.
    value.value.update(
        login="alice",
        synced_at=time.time(),
        prs=[
            {"id": f"PR_{n}", "number": n, "title": f"Fix the flaky test number {n}"}
            for n in range(200)
        ],
    )
    return value


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


def get(server, path, headers=None, method="GET", body=None):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
    try:
        # http.client sends `Accept-Encoding: identity` unless a header replaces it.
        conn.request(
            method, path, body, {"Host": f"127.0.0.1:{server.server_port}", **(headers or {})}
        )
        response = conn.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        conn.close()


def test_api_json_is_gzipped_only_when_accepted_and_never_cached(serve, overview):
    server = serve(overview=overview)
    status, plain_headers, plain = get(server, "/api/prs")
    assert status == 200 and "Content-Encoding" not in plain_headers
    assert int(plain_headers["Content-Length"]) == len(plain)
    status, headers, packed = get(server, "/api/prs", {"Accept-Encoding": "gzip, deflate, br"})
    assert status == 200 and headers["Content-Encoding"] == "gzip"
    assert int(headers["Content-Length"]) == len(packed) < len(plain) / 4
    assert json.loads(gzip.decompress(packed)) == json.loads(plain)
    for response in (plain_headers, headers):
        assert response["Cache-Control"] == "no-store"
        assert response["Vary"] == "Accept-Encoding"
        assert response["Content-Security-Policy"].startswith("default-src 'self'")
        assert response["X-Content-Type-Options"] == "nosniff"
    # An explicit refusal wins over a wildcard, in either order.
    for refusal in ("gzip;q=0", "*;q=1, gzip;q=0", "gzip;q=0, *"):
        assert "Content-Encoding" not in get(server, "/api/prs", {"Accept-Encoding": refusal})[1]
    assert get(server, "/api/prs", {"Accept-Encoding": "*"})[1]["Content-Encoding"] == "gzip"


def test_small_responses_and_images_are_sent_as_they_are(serve):
    server = serve()
    status, headers, body = get(server, "/api/codex-update", {"Accept-Encoding": "gzip"})
    assert status == 200 and "Content-Encoding" not in headers and json.loads(body)
    status, headers, body = get(server, "/icon-512.png", {"Accept-Encoding": "gzip"})
    assert status == 200 and "Content-Encoding" not in headers
    assert body == (dashboard.ASSETS / "icon-512.png").read_bytes()


@pytest.mark.parametrize("path", ["/", "/app.js", "/style.css", "/sw.js", "/manifest.webmanifest"])
def test_static_assets_revalidate_by_etag(serve, path):
    server = serve()
    status, headers, body = get(server, path, {"Accept-Encoding": "gzip"})
    assert status == 200 and headers["Cache-Control"] == "no-cache"
    etag = headers["ETag"]
    assert etag.startswith('W/"')
    filename = {"/": "index.html"}.get(path, path[1:])
    expected = (dashboard.ASSETS / filename).read_bytes()
    if len(expected) >= dashboard.COMPRESS_MIN:
        assert headers["Content-Encoding"] == "gzip" and gzip.decompress(body) == expected
        assert headers["Vary"] == "Accept-Encoding"
    else:
        assert body == expected
    plain = get(server, path)
    assert plain[2] == expected and plain[1]["ETag"] == etag
    for match in (etag, etag.removeprefix("W/"), f'"other", {etag}', "*"):
        status, headers, body = get(server, path, {"If-None-Match": match})
        assert status == 304 and body == b""
        assert headers["ETag"] == etag and headers["Cache-Control"] == "no-cache"
        assert "Content-Length" not in headers and "Content-Encoding" not in headers
    assert get(server, path, {"If-None-Match": '"other"'})[0] == 200


def test_a_changed_asset_gets_a_new_etag(serve, tmp_path, monkeypatch):
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "app.js").write_text("console.log(1);\n")
    monkeypatch.setattr(dashboard, "ASSETS", assets)
    monkeypatch.setattr(dashboard, "_assets_cache", {})
    server = serve()
    first = get(server, "/app.js")[1]["ETag"]
    (assets / "app.js").write_text("console.log(22);\n")
    status, headers, body = get(server, "/app.js", {"If-None-Match": first})
    assert status == 200 and body == b"console.log(22);\n" and headers["ETag"] != first


class Broken:
    def snapshot(self, *args, **kwargs):
        raise RuntimeError("provider exploded")

    batch = snapshot


def test_unexpected_errors_still_answer_with_json(serve, capsys):
    server = serve(overview=Broken(), workspaces=Broken())
    status, headers, body = get(server, "/api/prs")
    assert status == 500 and headers["Content-Type"].startswith("application/json")
    assert json.loads(body) == {
        "error": "Unexpected dashboard error: RuntimeError: provider exploded"
    }
    status, _, body = get(
        server,
        "/api/workspace-batch",
        {"X-Babysit-Action": "workspace-batch", "Content-Type": "application/json"},
        method="POST",
        body=b"{}",
    )
    assert status == 500 and "provider exploded" in json.loads(body)["error"]
    # The traceback goes to the dashboard log; the server keeps serving.
    assert "RuntimeError: provider exploded" in capsys.readouterr().err
    assert get(server, "/api/codex-update")[0] == 200


class Server:
    server_port = 8765
    allowed_hosts: set[str] = set()
    home = None

    class extension:
        @staticmethod
        def known_origin(origin):
            return False


class GoneClient:
    """A socket whose peer hung up: every write fails."""

    def __init__(self, error):
        self.error = error
        self.writes = 0

    def write(self, data):
        self.writes += 1
        raise self.error

    def flush(self):
        pass


def handler(path, wfile, home):
    request = dashboard.Handler.__new__(dashboard.Handler)
    request.server = Server()
    request.server.home = home
    request.path = path
    request.request_version = "HTTP/1.0"
    request.requestline = f"GET {path} HTTP/1.0"
    request.command = "GET"
    request.client_address = ("127.0.0.1", 1)
    request.close_connection = False
    headers = email.message.Message()
    headers["Host"] = "127.0.0.1:8765"
    request.headers = headers
    request.wfile = wfile
    return request


@pytest.mark.parametrize("error", [BrokenPipeError(32, "Broken pipe"), ConnectionResetError()])
def test_a_client_that_left_is_not_sent_a_second_response(tmp_path, error):
    wfile = GoneClient(error)
    request = handler("/api/status", wfile, tmp_path)
    request.do_GET()  # Before: the write error became a 503 written to the same socket.
    assert wfile.writes == 1 and request.started and request.close_connection
    request.fail(503, "Cannot read watcher data")
    assert wfile.writes == 1


def test_disconnects_are_not_logged_as_server_errors(tmp_path, capsys):
    server = dashboard.DashboardServer(tmp_path, 0)
    try:
        for error in (BrokenPipeError(), ConnectionResetError(), TimeoutError()):
            try:
                raise error
            except OSError:
                server.handle_error(None, ("127.0.0.1", 1))
        assert capsys.readouterr().err == ""
        try:
            raise RuntimeError("a real bug")
        except RuntimeError:
            server.handle_error(None, ("127.0.0.1", 1))
        assert "a real bug" in capsys.readouterr().err
    finally:
        server.server_close()


def test_handler_threads_have_a_socket_timeout():
    assert dashboard.Handler.timeout == 30


def test_kept_alive_responses_are_not_held_back_by_nagle():
    assert dashboard.Handler.disable_nagle_algorithm


class Sentry:
    def __init__(self):
        self.refreshes = 0

    def request_refresh(self):
        self.refreshes += 1

    def snapshot(self):
        return {"enabled": True, "groups": []}


def test_cross_site_links_open_the_dashboard_but_no_api(serve):
    sentry = Sentry()
    server = serve(sentry=sentry)
    navigate = {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate"}
    for path in ("/api/sentry?refresh=1", "/api/workspace-overview?refresh=1", "/api/status"):
        assert get(server, path, navigate)[0] == 403
    assert sentry.refreshes == 0
    assert get(server, "/", navigate)[0] == 200
    assert get(server, "/extension.html", navigate)[0] == 200
    # Same-origin navigation (a log opened in a new tab) and plain fetches still work.
    same = {"Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "navigate"}
    assert get(server, "/api/sentry?refresh=1", same)[0] == 200
    assert get(server, "/api/sentry?refresh=1", {"Sec-Fetch-Site": "same-origin"})[0] == 200
    assert sentry.refreshes == 2
    assert get(server, "/api/sentry", {"Sec-Fetch-Site": "cross-site"})[0] == 403


def test_read_jobs_is_cached_until_the_queue_changes(tmp_path, monkeypatch):
    assert dashboard.read_jobs(tmp_path) == []
    db = open_db(tmp_path)  # WAL mode, exactly as the watcher writes it.
    try:
        save_job(db, {"id": "one", "status": "watching"})
        db.commit()
        first = dashboard.read_jobs(tmp_path)
        # A write this recent may share the read's timestamp: it is not cached yet.
        assert dashboard.read_jobs(tmp_path) is not first
        monkeypatch.setattr(dashboard, "RACY_NS", 0)
        cached = dashboard.read_jobs(tmp_path)
        assert dashboard.read_jobs(tmp_path) is cached
        assert [job["id"] for job in cached] == ["one"]
        save_job(db, {"id": "two", "status": "watching"})
        db.commit()
        assert sorted(job["id"] for job in dashboard.read_jobs(tmp_path)) == ["one", "two"]
        # Rewriting a row is seen too.
        save_job(db, {"id": "two", "status": "stopped"})
        db.commit()
        jobs = {job["id"]: job["status"] for job in dashboard.read_jobs(tmp_path)}
        assert jobs == {"one": "watching", "two": "stopped"}
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        assert len(dashboard.read_jobs(tmp_path)) == 2
    finally:
        db.close()
    # A rollback-journal queue, with no WAL file, is read as well.
    monkeypatch.undo()
    other = tmp_path / "plain"
    other.mkdir()
    plain = sqlite3.connect(other / "queue.sqlite")
    plain.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, data TEXT NOT NULL)")
    plain.execute("INSERT INTO jobs VALUES ('a', ?)", (json.dumps({"id": "a"}),))
    plain.commit()
    assert [job["id"] for job in dashboard.read_jobs(other)] == ["a"]
    plain.execute("INSERT INTO jobs VALUES ('b', ?)", (json.dumps({"id": "b"}),))
    plain.commit()
    plain.close()
    assert sorted(job["id"] for job in dashboard.read_jobs(other)) == ["a", "b"]


def test_notification_preferences_read_only_the_login(tmp_path):
    from dashboard_push import PushInbox

    built = []

    def snapshots():
        built.append(1)
        return {"prs": {"login": "alice"}, "issues": {}, "watcher": {}}

    assert PushInbox(tmp_path, snapshots).preferences()["login"] == "alice"
    assert built == [1]
    inbox = PushInbox(tmp_path, snapshots, login=lambda: "alice")
    assert inbox.preferences() == {"login": "alice", "silenced": []}
    assert built == [1]  # The poll no longer builds every snapshot.


def test_an_error_after_the_response_began_closes_a_kept_alive_connection(tmp_path):
    """The client of a response cut short is not left waiting out the idle timeout."""
    server = dashboard.DashboardServer(tmp_path, 0)

    def known_origin(origin):
        raise sqlite3.OperationalError("database is locked")

    server.extension.known_origin = known_origin
    # A paired extension's request whose reply fails after its status line was begun.
    server.extension.authenticate = lambda headers: {"id": "fixture"}
    server.extension.dispatch = lambda server, client, request: {"ok": True}
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request(
            "POST",
            "/api/extension",
            body=b"{}",
            headers={"Content-Type": "application/json", "Origin": "chrome-extension://fixture"},
        )
        started = time.monotonic()
        # Before: no bytes and an open socket until the 30 s idle timeout (a TimeoutError here).
        with pytest.raises(http.client.RemoteDisconnected):
            connection.getresponse()
        assert time.monotonic() - started < 5
        connection.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
