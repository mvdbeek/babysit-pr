"""Usage readings use isolated stores and fake readers, never a live login or endpoint."""

import http.client
import json
import threading
import time

import claude_accounts
import dashboard
import llm_usage
import pytest


@pytest.fixture
def stores(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    claude_accounts.account_home("default", create=True)
    work = claude_accounts.account_home("work", create=True)
    return work


def window(name, used, resets_at=None, agent="claude"):
    return {"agent": agent, "name": name, "used_percent": used, "resets_at": resets_at}


def test_readings_cover_every_login_and_keep_the_last_good_one(stores):
    calls = []
    replies = {
        "codex": ([window("five_hour", 40.0, agent="codex")], None),
        None: (None, "stored Claude login has expired; it refreshes on Claude's next use"),
        str(stores): ([window("five_hour", 10.0), window("seven_day", 20.0)], None),
    }

    def reader(agent, config_env=False):
        calls.append((agent, config_env))
        return replies["codex" if agent == "codex" else config_env]

    previous = {
        "accounts": [
            {"id": "claude:default", "windows": [window("five_hour", 90.0)], "checked_at": 5.0}
        ]
    }
    value = llm_usage.take_readings(previous, 100.0, reader)
    # The default login keeps Claude's unsuffixed Keychain entry; a named one its literal path.
    assert calls == [("codex", False), ("claude", None), ("claude", str(stores))]
    by_id = {a["id"]: a for a in value["accounts"]}
    assert list(by_id) == ["codex", "claude:default", "claude:work"]
    assert by_id["claude:work"]["account"] == "work" and by_id["claude:work"]["checked_at"] == 100
    assert by_id["claude:default"]["account"] is None
    assert by_id["claude:default"]["windows"] == [window("five_hour", 90.0)]
    assert by_id["claude:default"]["checked_at"] == 5.0
    assert by_id["claude:default"]["error"].startswith("stored Claude login has expired")
    assert all("config_dir" not in a for a in value["accounts"])
    # Launches set the resolved path, whatever the daemon's own CLAUDE_CONFIG_DIR says.
    link = stores.parents[2] / "work-link"
    link.symlink_to(stores)
    calls.clear()
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("CLAUDE_CONFIG_DIR", str(link) + "/")
        llm_usage.take_readings(None, 1.0, reader)
    assert calls[1:] == [("claude", None), ("claude", str(stores))]

    def broken(agent, config_env=False):
        raise TypeError("unexpected")

    failed = llm_usage.take_readings(None, 1.0, broken)
    assert {a["error"] for a in failed["accounts"]} == {"TypeError"}


def test_present_ranks_by_tightest_window_and_forgets_passed_resets(tmp_path):
    now = time.time()
    past, future = "2020-01-01T00:00:00Z", "2999-01-01T00:00:00.5+00:00"
    accounts = [
        {
            "id": "codex",
            "agent": "codex",
            "label": "Codex",
            "windows": [window("five_hour", 10.0, future), window("seven_day", 70.0, future)],
        },
        {
            "id": "claude:default",
            "agent": "claude",
            "account": None,
            "label": "Claude · Default",
            # The weekly window has reset since this reading; Opus-only limits are not ranked.
            "windows": [
                window("five_hour", 45.0, future),
                window("seven_day", 99.0, past),
                window("seven_day_opus", 100.0, future),
            ],
        },
        {"id": "claude:work", "agent": "claude", "label": "Claude · work", "windows": []},
        {"id": "other", "agent": "unknown", "windows": []},
    ]
    (tmp_path / llm_usage.SNAPSHOT).write_text(
        json.dumps({"attempted_at": 1, "accounts": accounts})
    )
    value = llm_usage.present(tmp_path, now, reader="running")
    lefts = {a["id"]: a["left_percent"] for a in value["accounts"]}
    assert lefts == {"codex": 30.0, "claude:default": 55.0, "claude:work": None}
    assert value["best"] == "claude:default" and value["reader"] == "running"
    assert value["accounts"][1]["windows"][1]["used_percent"] == 0.0
    assert llm_usage.present(tmp_path, now)["reader"] == "offline"
    # A failing login keeps its last reading; only an expired token or a recent reading ranks.
    accounts[1].update(error="no readable Claude login", checked_at=now - 7200)
    (tmp_path / llm_usage.SNAPSHOT).write_text(json.dumps({"accounts": accounts}))
    value = llm_usage.present(tmp_path, now)
    assert value["best"] == "codex" and value["accounts"][1]["left_percent"] is None
    assert value["accounts"][1]["windows"]
    for error, checked in ((llm_usage.EXPIRED + "; it refreshes", now - 7200), ("HTTP 429", now)):
        accounts[1].update(error=error, checked_at=checked)
        (tmp_path / llm_usage.SNAPSHOT).write_text(json.dumps({"accounts": accounts}))
        assert llm_usage.present(tmp_path, now)["best"] == "claude:default"
    del accounts[1]["error"]
    # Ties keep catalog order.
    for account in accounts[:2]:
        account["windows"] = [window("five_hour", 50.0)]
    (tmp_path / llm_usage.SNAPSHOT).write_text(json.dumps({"accounts": accounts}))
    assert llm_usage.present(tmp_path, now)["best"] == "codex"
    (tmp_path / llm_usage.SNAPSHOT).write_text("not json")
    assert llm_usage.present(tmp_path, now)["best"] is None


def test_worker_reads_only_while_wanted_and_paces_requests(tmp_path, stores):
    clock = [1000.0]
    reads = []

    def reader(agent, config_env=False):
        reads.append(agent)
        return [window("five_hour", 1.0, agent=agent)], None

    worker = llm_usage.Worker(tmp_path, reader, now=lambda: clock[0])

    def tick(advance):
        clock[0] += advance
        worker.tick()
        if worker.thread:
            worker.thread.join(timeout=5)

    tick(0)
    assert reads == []  # No dashboard has asked.
    llm_usage.request(tmp_path, now=clock[0])
    tick(llm_usage.REQUEST_CHECK_SECONDS)
    assert len(reads) == 3
    assert json.loads((tmp_path / llm_usage.SNAPSHOT).read_text())["attempted_at"] == clock[0]
    assert (tmp_path / llm_usage.SNAPSHOT).stat().st_mode & 0o777 == 0o600
    llm_usage.request(tmp_path, refresh=True, now=clock[0] + 1)
    tick(llm_usage.REQUEST_CHECK_SECONDS)
    assert len(reads) == 3  # A refresh still waits for the minimum interval.
    tick(llm_usage.MIN_REFRESH_SECONDS)
    assert len(reads) == 6
    tick(llm_usage.REFRESH_SECONDS - 1)
    assert len(reads) == 6
    llm_usage.request(tmp_path, now=clock[0])
    tick(llm_usage.REQUEST_CHECK_SECONDS)
    assert len(reads) == 9
    # A restarted daemon keeps the pace of the saved reading.
    restarted = llm_usage.Worker(tmp_path, reader, now=lambda: clock[0])
    restarted.tick()
    assert restarted.thread is None

    # A failed reading is raised once from the loop's tick, which logs it.
    def broken(agent, config_env=False):
        raise AssertionError("never called")

    failing = llm_usage.Worker(tmp_path, broken, now=lambda: clock[0])
    failing.last_attempt = 0
    llm_usage.request(tmp_path, now=clock[0])
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(llm_usage, "take_readings", lambda *a: 1 / 0)
        failing.tick()
        failing.thread.join(timeout=5)
    with pytest.raises(RuntimeError, match="ZeroDivisionError"):
        failing.tick()
    failing.next_check = float("inf")
    failing.tick()
    # Nobody has looked for a while: readings stop.
    tick(llm_usage.WANTED_SECONDS + llm_usage.REFRESH_SECONDS)
    assert len(reads) == 9


def test_reader_state_names_a_daemon_without_the_usage_reader(tmp_path):
    fresh = {"time": time.time(), "pid": 42}
    assert llm_usage.reader_state(tmp_path, {}) == "offline"
    assert llm_usage.reader_state(tmp_path, {**fresh, "time": time.time() - 60}) == "offline"
    assert llm_usage.reader_state(tmp_path, fresh) == "outdated"
    (tmp_path / llm_usage.READER).write_text(json.dumps({"pid": 42}))
    assert llm_usage.reader_state(tmp_path, fresh) == "running"


def test_dashboard_requests_and_serves_usage(tmp_path):
    (tmp_path / "heartbeat.json").write_text(json.dumps({"time": time.time(), "pid": 7}))
    (tmp_path / llm_usage.READER).write_text(json.dumps({"pid": 7}))
    (tmp_path / llm_usage.SNAPSHOT).write_text(
        json.dumps(
            {
                "accounts": [
                    {
                        "id": "codex",
                        "agent": "codex",
                        "label": "Codex",
                        "windows": [window("five_hour", 25.0, agent="codex")],
                    }
                ]
            }
        )
    )
    with dashboard.DashboardServer(tmp_path, 0) as httpd:
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=5)
            conn.request("GET", "/api/llm-usage?refresh=1")
            response = conn.getresponse()
            body = json.loads(response.read())
        finally:
            httpd.shutdown()
            thread.join(timeout=5)
    assert response.status == 200
    assert body["best"] == "codex" and body["reader"] == "running"
    assert body["accounts"][0]["left_percent"] == 75.0
    wanted = json.loads((tmp_path / llm_usage.REQUEST).read_text())["wanted_at"]
    assert json.loads((tmp_path / llm_usage.REFRESH).read_text())["refresh_at"] == wanted
