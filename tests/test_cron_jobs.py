"""Cron job schedules, runs and dashboard endpoints against an isolated state directory."""

import json
import threading
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime

import attention
import cron_jobs
import dashboard
import pytest
from cron_jobs import Cron, CronJobs

SHELL = ["/bin/sh", "-c"]


def local(*parts):
    return datetime(*parts).timestamp()


def moments(expression, start, count=3):
    cron, times = Cron(expression), []
    for _ in range(count):
        start = cron.next_after(start)
        times.append(datetime.fromtimestamp(start))
    return times


def wait_for(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("Timed out waiting")


def finished(jobs, job_id):
    def done():
        runs = jobs.snapshot()["jobs"]
        run = next(job for job in runs if job["id"] == job_id)["runs"][0]
        return run if run["status"] != "running" else None

    return wait_for(done)


@pytest.fixture
def jobs(tmp_path):
    value = CronJobs(tmp_path, shell=SHELL)
    yield value
    value.close()


def job(jobs, **changes):
    request = {"name": "Job", "command": "echo hello", "schedule": {"every": 3600}}
    request.update(changes)
    return jobs.save(request)


# -- schedules --


def test_cron_steps_ranges_lists_and_names():
    start = local(2026, 10, 7, 8, 7)  # A Wednesday
    assert moments("*/15 * * * *", start) == [
        datetime(2026, 10, 7, 8, 15),
        datetime(2026, 10, 7, 8, 30),
        datetime(2026, 10, 7, 8, 45),
    ]
    assert moments("0 9 * * MON-fri", start, 4) == [
        datetime(2026, 10, 7, 9),
        datetime(2026, 10, 8, 9),
        datetime(2026, 10, 9, 9),
        datetime(2026, 10, 12, 9),
    ]
    assert moments("5 4 * jan,jul sun", start, 2) == [
        datetime(2027, 1, 3, 4, 5),
        datetime(2027, 1, 10, 4, 5),
    ]
    assert moments("0 0 * * 7", start, 1) == [datetime(2026, 10, 11)]
    assert moments("10-20/5 3 * * *", start) == [
        datetime(2026, 10, 8, 3, 10),
        datetime(2026, 10, 8, 3, 15),
        datetime(2026, 10, 8, 3, 20),
    ]
    assert moments("@monthly", start, 2) == [datetime(2026, 11, 1), datetime(2026, 12, 1)]
    assert moments("0 0 29 2 *", start, 1) == [datetime(2028, 2, 29)]


def test_day_of_month_and_week_match_either_unless_one_is_starred():
    start = local(2026, 10, 7, 8, 7)
    # Both restricted: the 15th or any Friday.
    assert moments("0 12 15 * fri", start) == [
        datetime(2026, 10, 9, 12),
        datetime(2026, 10, 15, 12),
        datetime(2026, 10, 16, 12),
    ]
    # A starred day of month (even with a step) leaves the weekday alone to decide.
    assert moments("0 12 */1 * fri", start, 1) == [datetime(2026, 10, 9, 12)]


def test_the_next_time_is_strictly_later():
    start = local(2026, 10, 7, 9, 0)
    assert moments("0 9 * * *", start, 1) == [datetime(2026, 10, 8, 9)]


@pytest.mark.parametrize(
    ("expression", "message"),
    [
        ("* * * *", "five fields"),
        ("60 * * * *", "minute field allows 0-59"),
        ("* 24 * * *", "hour field allows 0-23"),
        ("* * 0 * *", "day of month field allows 1-31"),
        ("* * * 13 *", "month field allows 1-12"),
        ("* * * * 8", "day of week field allows 0-7"),
        ("5-1 * * * *", "minute field allows"),
        ("*/0 * * * *", "step"),
        ("*/x * * * *", "step"),
        ("foo * * * *", "Unrecognized minute value"),
        ("0 0 31 2 *", "never matches"),
        ("0 0 30 feb *", "never matches"),
    ],
)
def test_invalid_cron_expressions_are_explained(expression, message):
    with pytest.raises(ValueError, match=message):
        Cron(expression)


def test_intervals_count_from_the_anchor():
    value = {"schedule": {"every": 600}, "anchor": 1000.0}
    assert cron_jobs.next_run(value, 1000.0) == 1600.0
    assert cron_jobs.next_run(value, 1599.0) == 1600.0
    assert cron_jobs.next_run(value, 1600.0) == 2200.0
    assert cron_jobs.next_run(value, 5000.0) == 5200.0


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"name": " "}, "name"),
        ({"name": "x" * 81}, "name"),
        ({"command": ""}, "command"),
        ({"command": "echo \0"}, "NUL"),
        ({"cwd": "relative/path"}, "absolute"),
        ({"cwd": "/does/not/exist/anywhere"}, "does not exist"),
        ({"schedule": {}}, "interval or a cron"),
        ({"schedule": {"every": 30}}, "1 minute to 31 days"),
        ({"schedule": {"every": 90}}, "whole minutes"),
        ({"schedule": {"every": True}}, "1 minute"),
        ({"schedule": {"cron": "bad"}}, "five fields"),
        ({"schedule": {"every": 60, "cron": "* * * * *"}}, "interval or a cron"),
        ({"timeout": 10}, "time limit"),
        ({"timeout": 86401}, "time limit"),
        ({"enabled": "yes"}, "enabled"),
    ],
)
def test_invalid_definitions_are_refused(jobs, changes, message):
    with pytest.raises(ValueError, match=message):
        job(jobs, **changes)
    assert jobs.snapshot()["jobs"] == []


def test_saving_normalizes_and_schedules(jobs, tmp_path):
    saved = job(
        jobs,
        name="  Backup ",
        cwd=str(tmp_path),
        schedule={"cron": " 0   2 * * * "},
        timeout=120,
    )
    assert saved["name"] == "Backup"
    assert saved["schedule"] == {"cron": "0 2 * * *"}
    assert datetime.fromtimestamp(saved["next_run"]).hour == 2
    [listed] = jobs.snapshot()["jobs"]
    assert listed["upcoming"][0] == saved["next_run"]
    assert len(listed["upcoming"]) == 3
    assert listed["runs"] == [] and listed["running"] is False
    assert (tmp_path / "cron.sqlite").stat().st_mode & 0o777 == 0o600


def test_editing_keeps_the_schedule_unless_it_changes(tmp_path):
    clock = [1000.0]
    jobs = CronJobs(tmp_path, shell=SHELL, clock=lambda: clock[0])
    saved = job(jobs, schedule={"every": 600})
    assert saved["next_run"] == 1600.0
    clock[0] = 1200.0
    renamed = jobs.save({**saved, "name": "Renamed"})
    assert renamed["next_run"] == 1600.0
    changed = jobs.save({**saved, "schedule": {"every": 1200}})
    assert changed["next_run"] == 2400.0


def test_pausing_and_resuming_skips_what_was_missed(tmp_path):
    clock = [1000.0]
    jobs = CronJobs(tmp_path, shell=SHELL, clock=lambda: clock[0])
    saved = job(jobs, schedule={"every": 600})
    jobs.action({"action": "enable", "id": saved["id"], "enabled": False})
    clock[0] = 10000.0
    jobs.tick()
    assert jobs.snapshot()["jobs"][0]["runs"] == []
    assert jobs.snapshot()["jobs"][0]["upcoming"] == []
    resumed = jobs.action({"action": "enable", "id": saved["id"], "enabled": True})["job"]
    assert resumed["next_run"] == 10600.0


def test_job_count_is_bounded(jobs, monkeypatch):
    monkeypatch.setattr(cron_jobs, "MAX_JOBS", 2)
    job(jobs)
    job(jobs)
    with pytest.raises(ValueError, match="At most 2 jobs"):
        job(jobs)


# -- runs --


def test_a_run_records_status_output_and_environment(jobs, tmp_path):
    saved = job(
        jobs,
        cwd=str(tmp_path),
        command='echo "out $BABYSIT_CRON_JOB $(pwd)"; echo err >&2; exit 3',
    )
    run = jobs.action({"action": "run", "id": saved["id"]})["run"]
    assert run["status"] == "running" and run["trigger"] == "manual"
    done = finished(jobs, saved["id"])
    assert done["status"] == "failed"
    assert done["exit_code"] == 3
    assert done["message"] == "Exited with status 3"
    assert done["tail"] == f"out {saved['id']} {tmp_path.resolve()}\nerr\n"
    log = jobs.log(done["id"])
    assert log["text"] == done["tail"]
    assert log["run"]["finished_at"] >= log["run"]["started_at"]
    assert (tmp_path / "cron-logs" / f"{done['id']}.log").stat().st_mode & 0o777 == 0o600


def test_a_successful_run_and_the_home_directory_default(jobs, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    saved = job(jobs, command="pwd")
    jobs.run_now(saved["id"])
    done = finished(jobs, saved["id"])
    assert done["status"] == "succeeded" and done["exit_code"] == 0
    assert done["tail"].strip() == str(tmp_path)


def test_a_missing_shell_or_directory_is_an_error(tmp_path):
    jobs = CronJobs(tmp_path, shell=[str(tmp_path / "no-shell"), "-c"])
    saved = job(jobs)
    jobs.run_now(saved["id"])
    done = finished(jobs, saved["id"])
    assert done["status"] == "error"
    assert done["message"].startswith("Could not start")


def test_stop_ends_the_process_group(jobs, tmp_path):
    marker = tmp_path / "late"
    saved = job(jobs, command=f"(sleep 3; touch {marker}) & sleep 30")
    jobs.run_now(saved["id"])
    with pytest.raises(ValueError, match="already running"):
        jobs.run_now(saved["id"])
    with pytest.raises(ValueError, match="Stop the running job"):
        jobs.action({"action": "delete", "id": saved["id"]})
    wait_for(lambda: jobs.snapshot()["jobs"][0]["running"])
    jobs.action({"action": "stop", "id": saved["id"]})
    done = finished(jobs, saved["id"])
    assert done["status"] == "stopped"
    assert jobs.snapshot()["jobs"][0]["running"] is False
    time.sleep(3.5)
    assert not marker.exists()
    with pytest.raises(ValueError, match="not running"):
        jobs.stop(saved["id"])


def test_a_run_past_its_time_limit_is_stopped(jobs, monkeypatch):
    saved = job(jobs, command="echo started; sleep 30", timeout=60)
    monkeypatch.setattr(cron_jobs, "STOP_GRACE", 0.1)
    jobs.launch({**saved, "timeout": 0.5}, "manual", time.time())
    done = finished(jobs, saved["id"])
    assert done["status"] == "timed_out"
    assert done["tail"] == "started\n"


def test_output_beyond_the_limit_is_counted_but_not_kept(jobs, monkeypatch):
    monkeypatch.setattr(cron_jobs, "LOG_LIMIT", 100)
    saved = job(jobs, command="i=0; while [ $i -lt 50 ]; do echo 0123456789; i=$((i+1)); done")
    jobs.run_now(saved["id"])
    done = finished(jobs, saved["id"])
    assert done["status"] == "succeeded"
    assert done["output_bytes"] == 550 and done["truncated"] is True
    assert len(jobs.log(done["id"])["text"]) == 100


def test_a_background_child_holding_output_does_not_block_completion(jobs, monkeypatch):
    monkeypatch.setattr(cron_jobs, "QUIET_AFTER_EXIT", 0.2)
    saved = job(jobs, command="sleep 30 & echo parent done")
    jobs.run_now(saved["id"])
    done = finished(jobs, saved["id"])
    assert done["status"] == "succeeded"
    assert done["tail"] == "parent done\n"


def test_history_is_pruned_with_its_logs(jobs, tmp_path, monkeypatch):
    monkeypatch.setattr(cron_jobs, "RUN_HISTORY", 2)
    saved = job(jobs, command="echo run")
    ids = []
    for _ in range(4):
        ids.append(jobs.run_now(saved["id"])["id"])
        finished(jobs, saved["id"])
    runs = jobs.snapshot()["jobs"][0]["runs"]
    assert [run["id"] for run in runs] == ids[:1:-1]
    assert not (tmp_path / "cron-logs" / f"{ids[0]}.log").exists()
    assert (tmp_path / "cron-logs" / f"{ids[3]}.log").exists()
    with pytest.raises(ValueError, match="no longer kept"):
        jobs.log(ids[0])


def test_deleting_removes_runs_and_logs(jobs, tmp_path):
    saved = job(jobs)
    run = jobs.run_now(saved["id"])
    finished(jobs, saved["id"])
    assert jobs.action({"action": "delete", "id": saved["id"]}) == {"deleted": saved["id"]}
    assert jobs.snapshot()["jobs"] == []
    assert not (tmp_path / "cron-logs" / f"{run['id']}.log").exists()
    with pytest.raises(ValueError, match="no longer exists"):
        jobs.action({"action": "run", "id": saved["id"]})


def test_unknown_actions_and_bad_ids(jobs):
    with pytest.raises(ValueError, match="Unknown job action"):
        jobs.action({"action": "explode"})
    with pytest.raises(ValueError, match="Supply a job ID"):
        jobs.action({"action": "run", "id": 5})
    with pytest.raises(ValueError, match="enabled"):
        jobs.action({"action": "enable", "id": "x", "enabled": "no"})
    with pytest.raises(ValueError, match="Supply a run ID"):
        jobs.log("../etc/passwd")


# -- dismissing --


def recorded(jobs, saved, status, **fields):
    """A finished run as the scheduler would store it."""
    with jobs.db() as db:
        return jobs.record(
            db,
            saved,
            trigger="schedule",
            started_at=1000.0,
            finished_at=1060.0,
            status=status,
            message=f"The {status} run",
            **fields,
        )


def test_dismissing_acknowledges_the_latest_run_and_keeps_its_result(tmp_path):
    clock = [5000.0]
    jobs = CronJobs(tmp_path, shell=SHELL, clock=lambda: clock[0])
    saved = job(jobs)
    recorded(jobs, saved, "failed")
    run = recorded(jobs, saved, "attention", kind="agent")
    value = jobs.action({"action": "dismiss", "id": saved["id"], "run": run["id"]})
    assert value["run"]["dismissed_at"] == 5000.0
    latest, older = jobs.snapshot()["jobs"][0]["runs"]
    assert latest["id"] == run["id"] and latest["dismissed_at"] == 5000.0
    # History is not rewritten: the run keeps its status and message.
    assert latest["status"] == "attention" and latest["message"] == "The attention run"
    assert "dismissed_at" not in older
    # Dismissing again changes nothing.
    clock[0] = 6000.0
    again = jobs.action({"action": "dismiss", "id": saved["id"], "run": run["id"]})
    assert again["run"]["dismissed_at"] == 5000.0
    jobs.close()


@pytest.mark.parametrize("status", sorted(cron_jobs.DISMISSABLE))
def test_every_alerting_status_can_be_dismissed(jobs, status):
    saved = job(jobs)
    run = recorded(jobs, saved, status)
    assert jobs.dismiss(saved["id"], run["id"])["dismissed_at"]


def test_an_older_run_can_be_dismissed_too(jobs):
    saved = job(jobs)
    older = recorded(jobs, saved, "attention")
    newer = recorded(jobs, saved, "succeeded")
    assert jobs.dismiss(saved["id"], older["id"])["dismissed_at"]
    latest, earlier = jobs.snapshot()["jobs"][0]["runs"]
    assert latest["id"] == newer["id"] and "dismissed_at" not in latest
    assert earlier["id"] == older["id"] and earlier["dismissed_at"]


def test_dismissing_is_refused_unless_the_run_needs_attention(jobs):
    saved = job(jobs)
    recorded(jobs, saved, "attention")
    newer = recorded(jobs, saved, "succeeded")
    with pytest.raises(ValueError, match="Only a run that failed or needs attention"):
        jobs.dismiss(saved["id"], newer["id"])
    with pytest.raises(ValueError, match="This job no longer exists"):
        jobs.dismiss("missing", newer["id"])
    with pytest.raises(ValueError, match="Supply a run ID"):
        jobs.dismiss(saved["id"], 5)
    with pytest.raises(ValueError, match="Supply a job ID"):
        jobs.action({"action": "dismiss", "run": newer["id"]})
    with pytest.raises(ValueError, match="not in the job's history"):
        jobs.dismiss(saved["id"], "999")
    other = job(jobs, name="Other")
    with pytest.raises(ValueError, match="not in the job's history"):
        jobs.dismiss(other["id"], newer["id"])
    assert all("dismissed_at" not in run for run in jobs.snapshot()["jobs"][0]["runs"])


def test_a_running_run_cannot_be_dismissed(jobs):
    saved = job(jobs, command="sleep 30")
    recorded(jobs, saved, "failed")
    run = jobs.run_now(saved["id"])
    wait_for(lambda: jobs.snapshot()["jobs"][0]["running"])
    with pytest.raises(ValueError, match="Only a run that failed or needs attention"):
        jobs.dismiss(saved["id"], run["id"])
    jobs.stop(saved["id"])
    finished(jobs, saved["id"])


def test_a_dismissed_run_leaves_needs_you_until_the_next_one_needs_attention(jobs):
    saved = job(jobs)
    run = recorded(jobs, saved, "attention", kind="agent")
    [item] = attention.collect(cron=jobs.snapshot)["items"]
    assert item["key"] == f"cron:{saved['id']}"
    assert item["cron"] == {"id": saved["id"], "run": run["id"]}
    jobs.dismiss(saved["id"], run["id"])
    assert attention.collect(cron=jobs.snapshot)["items"] == []
    newer = recorded(jobs, saved, "attention", kind="agent")
    [item] = attention.collect(cron=jobs.snapshot)["items"]
    assert item["cron"]["run"] == newer["id"]
    assert item["reasons"][0]["code"] == "cron_attention"


# -- scheduling --


def test_due_jobs_run_and_move_to_their_next_time(tmp_path):
    clock = [1000.0]
    jobs = CronJobs(tmp_path, shell=SHELL, clock=lambda: clock[0])
    saved = job(jobs, schedule={"every": 600}, command="echo tick")
    jobs.tick()
    assert jobs.snapshot()["jobs"][0]["runs"] == []
    clock[0] = 1601.0
    jobs.tick()
    done = finished(jobs, saved["id"])
    assert done["trigger"] == "schedule" and done["due_at"] == 1600.0
    assert done["status"] == "succeeded"
    assert jobs.snapshot()["jobs"][0]["next_run"] == 2200.0
    assert jobs.next_due() == 2200.0


def test_a_run_still_going_makes_the_next_one_skip(tmp_path):
    clock = [1000.0]
    jobs = CronJobs(tmp_path, shell=SHELL, clock=lambda: clock[0])
    saved = job(jobs, schedule={"every": 600}, command="sleep 30")
    clock[0] = 1600.0
    jobs.tick()
    wait_for(lambda: jobs.snapshot()["jobs"][0]["running"])
    clock[0] = 2200.0
    jobs.tick()
    runs = jobs.snapshot()["jobs"][0]["runs"]
    assert [run["status"] for run in runs] == ["skipped", "running"]
    jobs.stop(saved["id"])
    jobs.close()


def test_a_late_run_catches_up_once_and_a_very_late_one_is_missed(tmp_path):
    clock = [1000.0]
    jobs = CronJobs(tmp_path, shell=SHELL, clock=lambda: clock[0])
    saved = job(jobs, schedule={"every": 600}, command="true")
    clock[0] = 1600.0 + 5 * 600 + 1
    jobs.tick()
    done = finished(jobs, saved["id"])
    assert done["due_at"] == 1600.0 and done["status"] == "succeeded"
    clock[0] += 2 * 86400
    jobs.tick()
    [missed, _] = jobs.snapshot()["jobs"][0]["runs"]
    assert missed["status"] == "missed"
    assert jobs.snapshot()["jobs"][0]["next_run"] > clock[0]


def test_runs_left_running_by_a_previous_process_are_interrupted(tmp_path):
    jobs = CronJobs(tmp_path, shell=SHELL)
    saved = job(jobs, command="sleep 30")
    jobs.run_now(saved["id"])
    wait_for(lambda: jobs.snapshot()["jobs"][0]["running"])
    # A process that died without cleanup leaves its lock, and its run row, behind.
    jobs.owner.close()
    restarted = CronJobs(tmp_path, shell=SHELL)
    [run] = restarted.snapshot()["jobs"][0]["runs"]
    assert run["status"] == "interrupted"
    assert run["message"] == "The dashboard stopped during this run"
    jobs.close()


def test_closing_interrupts_running_jobs(tmp_path, monkeypatch):
    monkeypatch.setattr(cron_jobs, "STOP_GRACE", 0.1)
    jobs = CronJobs(tmp_path, shell=SHELL)
    saved = job(jobs, command="sleep 30")
    jobs.run_now(saved["id"])
    wait_for(lambda: jobs.snapshot()["jobs"][0]["running"])
    jobs.close()
    [run] = jobs.snapshot()["jobs"][0]["runs"]
    assert run["status"] == "interrupted"


def test_the_scheduler_thread_starts_due_jobs(tmp_path, monkeypatch):
    clock = [1000.0]
    jobs = CronJobs(tmp_path, shell=SHELL, clock=lambda: clock[0])
    saved = job(jobs, schedule={"every": 600}, command="echo loop")
    clock[0] = 1700.0
    jobs.start()
    try:
        done = finished(jobs, saved["id"])
        assert done["trigger"] == "schedule"
    finally:
        jobs.close()
    jobs.scheduler.join(5)
    assert not jobs.scheduler.is_alive()


# -- dashboard endpoints --


@pytest.fixture
def site(tmp_path):
    jobs = CronJobs(tmp_path, shell=SHELL)
    with dashboard.DashboardServer(tmp_path, 0, cron=jobs) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        yield f"http://127.0.0.1:{server.server_port}", jobs
        server.shutdown()
    jobs.close()


def call(url, path, body=None, action="cron-action"):
    request = urllib.request.Request(url + path)
    if body is not None:
        request.data = json.dumps(body).encode()
        request.add_header("Content-Type", "application/json")
        request.add_header("X-Babysit-Action", action)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def test_endpoints_define_run_and_read_jobs(site):
    url, jobs = site
    code, value = call(
        url,
        "/api/cron-action",
        {"action": "save", "name": "Hi", "command": "echo hi", "schedule": {"every": 60}},
    )
    assert code == 200
    job_id = value["job"]["id"]
    code, value = call(url, "/api/cron-action", {"action": "run", "id": job_id})
    assert code == 200
    run_id = value["run"]["id"]
    finished(jobs, job_id)
    code, snapshot = call(url, "/api/cron")
    assert code == 200 and snapshot["enabled"] is True
    assert snapshot["shell"] == "/bin/sh -c"
    assert snapshot["jobs"][0]["runs"][0]["tail"] == "hi\n"
    code, log = call(url, f"/api/cron-log?run={run_id}")
    assert code == 200 and log["text"] == "hi\n"
    code, value = call(url, "/api/cron-action", {"action": "save", "name": ""})
    assert code == 400 and "name" in value["error"]
    code, value = call(url, "/api/cron-log?run=1&run=2")
    assert code == 400


def test_the_dismiss_endpoint_acknowledges_a_run(site):
    url, jobs = site
    saved = job(jobs)
    run = recorded(jobs, saved, "attention", kind="agent")
    body = {"action": "dismiss", "id": saved["id"], "run": run["id"]}
    code, value = call(url, "/api/cron-action", body, action="cron-update")
    assert code == 403
    code, value = call(url, "/api/cron-action", body)
    assert code == 200 and value["run"]["dismissed_at"]
    newer = recorded(jobs, saved, "succeeded")
    code, value = call(url, "/api/cron-action", {**body, "run": newer["id"]})
    assert code == 400
    assert value["error"] == "Only a run that failed or needs attention can be dismissed"


def test_endpoints_report_a_disabled_feature(tmp_path):
    with dashboard.DashboardServer(tmp_path, 0) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.server_port}"
        assert call(url, "/api/cron") == (200, {"enabled": False})
        code, value = call(url, "/api/cron-log?run=1")
        assert code == 400 and value["error"] == "Cron jobs are not enabled"
        code, value = call(url, "/api/cron-action", {"action": "run", "id": "x"})
        assert code == 400 and value["error"] == "Cron jobs are not enabled"
        server.shutdown()


@pytest.fixture
def new_york(monkeypatch):
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def utc(*parts):
    return datetime(*parts, tzinfo=UTC).timestamp()


def clock_times(expression, start, count):
    cron, found = Cron(expression), []
    for _ in range(count):
        start = cron.next_after(start)
        found.append(time.strftime("%m-%d %H:%M %Z", time.localtime(start)))
    return found


def test_hourly_schedules_run_through_the_repeated_hour_when_clocks_go_back(new_york):
    # 01:50 EDT on 2025-11-02; clocks go back from 02:00 EDT to 01:00 EST.
    assert clock_times("*/5 * * * *", utc(2025, 11, 2, 5, 50), 4) == [
        "11-02 01:55 EDT",
        "11-02 01:00 EST",
        "11-02 01:05 EST",
        "11-02 01:10 EST",
    ]
    assert clock_times("0 * * * *", utc(2025, 11, 2, 5, 0), 2) == [
        "11-02 01:00 EST",
        "11-02 02:00 EST",
    ]
    # A fixed hour runs once.
    assert clock_times("30 1 * * *", utc(2025, 11, 2, 5, 0), 2) == [
        "11-02 01:30 EDT",
        "11-03 01:30 EST",
    ]


def test_a_time_skipped_when_clocks_go_forward_runs_after_the_change(new_york):
    assert clock_times("30 2 * * *", utc(2025, 3, 9, 6, 0), 2) == [
        "03-09 03:30 EDT",
        "03-10 02:30 EDT",
    ]


def test_skips_behind_a_long_run_are_counted_in_one_entry(tmp_path):
    clock = [1000.0]
    jobs = CronJobs(tmp_path, shell=SHELL, clock=lambda: clock[0])
    saved = job(jobs, schedule={"every": 60}, command="sleep 30")
    for minute in range(1, 5):
        clock[0] = 1000.0 + 60 * minute
        jobs.tick()
        wait_for(lambda: jobs.snapshot()["jobs"][0]["running"])
    skipped, running = jobs.snapshot()["jobs"][0]["runs"]
    assert running["status"] == "running"
    assert skipped["status"] == "skipped" and skipped["count"] == 3
    assert skipped["due_at"] == 1240.0
    assert skipped["message"] == "Skipped 3 times: the previous run was still going"
    jobs.stop(saved["id"])
    jobs.close()


def test_a_second_process_over_the_same_state_only_shows_jobs(jobs, tmp_path):
    saved = job(jobs, command="sleep 30")
    jobs.run_now(saved["id"])
    wait_for(lambda: jobs.snapshot()["jobs"][0]["running"])
    other = CronJobs(tmp_path, shell=SHELL)
    assert other.active is False and other.snapshot()["active"] is False
    # The owner's run is not mistaken for a leftover, and the copy runs nothing.
    assert other.snapshot()["jobs"][0]["runs"][0]["status"] == "running"
    with pytest.raises(ValueError, match="Another dashboard process"):
        other.run_now(saved["id"])
    other.start()
    assert other.scheduler is None
    other.close()
    jobs.stop(saved["id"])


def test_a_stopping_scheduler_starts_nothing_new(jobs):
    saved = job(jobs)
    jobs.close()
    with pytest.raises(ValueError, match="stopping"):
        jobs.run_now(saved["id"])
