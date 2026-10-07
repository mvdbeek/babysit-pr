"""Cron jobs tab in real Chromium against a temporary dashboard and state directory."""

import threading
import time

import cron_jobs
import dashboard
import pytest
from cron_jobs import CronJobs
from playwright.sync_api import expect
from test_dashboard_browser import choose_option

pytestmark = pytest.mark.browser


@pytest.fixture
def site(tmp_path):
    jobs = CronJobs(tmp_path, shell=["/bin/sh", "-c"])
    with dashboard.DashboardServer(tmp_path, 0, cron=jobs) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        yield f"http://127.0.0.1:{server.server_port}", jobs
        server.shutdown()
        thread.join(3)
    jobs.close()


@pytest.fixture
def errors(page):
    found: list[str] = []
    page.on("pageerror", lambda error: found.append(str(error)))
    yield found
    assert not found


def wait_idle(jobs):
    deadline = time.monotonic() + 10
    while any(job["running"] for job in jobs.snapshot()["jobs"]):
        assert time.monotonic() < deadline
        time.sleep(0.05)


def test_define_run_edit_pause_and_delete_a_job(page, site, errors, tmp_path):
    url, jobs = site
    page.set_viewport_size({"width": 1400, "height": 1000})
    page.goto(url + "/#cron")
    expect(page.get_by_role("tab", name="Cron jobs")).to_be_visible()
    expect(page.locator("#cron-empty")).to_be_visible()

    page.get_by_role("button", name="New job").click()
    dialog = page.locator("#cron-dialog")
    expect(dialog).to_be_visible()
    dialog.get_by_label("Name").fill("Greeting")
    dialog.get_by_label("Command").fill("echo hello from cron; echo '<b>not html</b>'")
    dialog.get_by_label("Working directory").fill("relative")
    dialog.get_by_role("button", name="Save job").click()
    expect(page.locator("#cron-form-error")).to_contain_text("absolute working directory")
    dialog.get_by_label("Working directory").fill(str(tmp_path))
    dialog.get_by_label("Interval", exact=True).fill("15")
    choose_option(dialog.get_by_role("combobox", name="Interval unit"), "60")
    dialog.get_by_role("button", name="Save job").click()
    expect(dialog).to_be_hidden()

    card = page.locator(".cron-job").filter(has_text="Greeting")
    expect(card).to_contain_text("Every 15 minutes")
    expect(card).to_contain_text("Next in 15 min")
    detail = page.locator("#cron-detail")
    expect(detail.locator("h3")).to_have_text("Greeting")
    expect(detail).to_contain_text("This job has not run yet.")

    detail.get_by_role("button", name="Run now").click()
    wait_idle(jobs)
    expect(detail.locator(".cron-runs tbody tr")).to_have_count(1)
    expect(detail.locator(".cron-runs")).to_contain_text("Succeeded")
    expect(detail.locator(".cron-runs")).to_contain_text("Run now")
    log = detail.locator(".cron-log")
    expect(log).to_have_text("hello from cron\n<b>not html</b>\n")
    assert detail.locator(".cron-log b").count() == 0
    expect(card.locator(".badge")).to_have_text("Succeeded")

    # Edit into a failing cron-scheduled command; the old run keeps its own command.
    detail.get_by_role("button", name="Edit").click()
    expect(dialog.get_by_label("Name")).to_have_value("Greeting")
    expect(dialog.get_by_label("Interval", exact=True)).to_have_value("15")
    choose_option(dialog.get_by_role("combobox", name="Repeat"), "cron")
    expect(dialog.get_by_label("Interval", exact=True)).to_be_hidden()
    dialog.get_by_label("Cron expression").fill("0 0 31 2 *")
    dialog.get_by_role("button", name="Save job").click()
    expect(page.locator("#cron-form-error")).to_have_text(
        "This cron expression never matches a date"
    )
    dialog.get_by_label("Cron expression").fill("30 4 * * mon")
    dialog.get_by_label("Command").fill("echo broken >&2; exit 4")
    dialog.get_by_role("button", name="Save job").click()
    expect(dialog).to_be_hidden()
    expect(card).to_contain_text("Cron: 30 4 * * mon")
    expect(detail).to_contain_text("Next: ")
    expect(detail.locator(".cron-command-then")).to_contain_text("This run used an earlier command")

    detail.get_by_role("button", name="Run now").click()
    wait_idle(jobs)
    expect(detail.locator(".cron-runs tbody tr")).to_have_count(2)
    expect(log).to_have_text("broken\n")
    expect(detail.locator(".cron-output")).to_contain_text("Exit status 4")
    expect(page.locator("#cron-tab-count")).to_have_text("1")
    expect(card.locator(".badge")).to_have_text("Failed")

    # Older runs open their own output.
    detail.locator(".cron-run-open").nth(1).click()
    expect(log).to_have_text("hello from cron\n<b>not html</b>\n")
    expect(detail.locator(".cron-runs tr.selected")).to_contain_text("Succeeded")

    detail.get_by_role("button", name="Pause").click()
    expect(card).to_contain_text("Paused")
    expect(detail.locator(".cron-title .badge")).to_have_text("Paused")
    detail.get_by_role("button", name="Resume").click()
    expect(card).not_to_contain_text("Paused")

    page.once("dialog", lambda prompt: prompt.accept())
    detail.get_by_role("button", name="Delete").click()
    expect(page.locator(".cron-job")).to_have_count(0)
    expect(page.locator("#cron-empty")).to_be_visible()
    assert jobs.snapshot()["jobs"] == []


def test_a_running_job_streams_output_and_can_be_stopped(page, site, errors, monkeypatch):
    url, jobs = site
    monkeypatch.setattr(cron_jobs, "STOP_GRACE", 0.2)
    saved = jobs.save(
        {
            "name": "Long",
            "command": "echo first; sleep 2; echo second; sleep 30",
            "schedule": {"every": 3600},
        }
    )
    page.goto(url + "/#cron")
    detail = page.locator("#cron-detail")
    expect(detail.locator("h3")).to_have_text("Long")
    detail.get_by_role("button", name="Run now").click()
    expect(page.locator(".cron-job .badge")).to_have_text("Running")
    log = detail.locator(".cron-log")
    expect(log).to_have_text("first\n")
    expect(log).to_have_text("first\nsecond\n", timeout=10000)
    expect(detail.get_by_role("button", name="Delete")).to_be_disabled()
    detail.get_by_role("button", name="Stop").click()
    expect(detail.locator(".cron-runs")).to_contain_text("Stopped from the dashboard")
    expect(detail.get_by_role("button", name="Run now")).to_be_visible()
    assert jobs.snapshot()["jobs"][0]["id"] == saved["id"]


@pytest.mark.parametrize("width", [1400, 390])
def test_responsive_layout(page, site, errors, width, tmp_path):
    url, jobs = site
    for name, command in [("Backup", "echo saved"), ("Disk check", "echo full; exit 1")]:
        saved = jobs.save({"name": name, "command": command, "schedule": {"cron": "@daily"}})
        jobs.run_now(saved["id"])
        wait_idle(jobs)
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(url + "/#cron")
    expect(page.locator(".cron-job")).to_have_count(2)
    if width < 900:
        # Phones show the list first and open details on selection.
        expect(page.locator("#cron-detail")).to_contain_text("Select a job")
    else:
        expect(page.locator("#cron-detail h3")).to_have_text("Backup")
    page.locator(".cron-job").filter(has_text="Disk check").click()
    expect(page.locator(".cron-log")).to_have_text("full\n")
    assert page.evaluate("document.documentElement.scrollWidth") <= width
    page.screenshot(path=str(tmp_path / f"cron-{width}.png"), full_page=True)
    page.get_by_role("button", name="New job").click()
    dialog = page.locator("#cron-dialog")
    interval = dialog.get_by_label("Interval", exact=True).bounding_box()
    unit = dialog.locator(".cron-every .searchable-select").bounding_box()
    assert interval and unit and abs(interval["y"] - unit["y"]) < 10
    page.screenshot(path=str(tmp_path / f"cron-dialog-{width}.png"))


def test_the_tab_stays_hidden_without_cron_jobs(page, tmp_path, errors):
    with dashboard.DashboardServer(tmp_path, 0) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            page.goto(f"http://127.0.0.1:{server.server_port}/#cron")
            expect(page.locator("#watcher-panel")).to_be_visible()
            expect(page.locator("#cron-tab")).to_be_hidden()
        finally:
            server.shutdown()
            thread.join(3)
