"""Cron jobs tab in real Chromium against a temporary dashboard and state directory."""

import json
import shlex
import threading
import time

import claude_accounts
import cron_agents
import cron_jobs
import dashboard
import pytest
import workspace_exit
from cron_jobs import CronJobs
from playwright.sync_api import expect
from test_dashboard_browser import choose_option
from test_pr_workspaces import local  # noqa: F401 (fixture)

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
    dialog.get_by_label("Command", exact=True).fill("echo hello from cron; echo '<b>not html</b>'")
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
    dialog.get_by_label("Command", exact=True).fill("echo broken >&2; exit 4")
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


def test_a_running_job_streams_output_and_can_be_stopped(page, site, errors, monkeypatch, tmp_path):
    url, jobs = site
    monkeypatch.setattr(cron_jobs, "STOP_GRACE", 0.2)
    # The job holds "second" back until the page has shown "first", however slow polling is.
    release = tmp_path / "release"
    saved = jobs.save(
        {
            "name": "Long",
            "command": f"echo first; until [ -e {shlex.quote(str(release))} ]; do sleep 0.1; done; echo second; sleep 30",
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
    release.touch()
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


@pytest.fixture
def agent_site(local):  # noqa: F811
    manager, _, git, state, _ = local
    jobs = CronJobs(manager.home, shell=["/bin/sh", "-c"], workspaces=manager)
    with dashboard.DashboardServer(
        manager.home, 0, overview=manager.overview, workspaces=manager, cron=jobs
    ) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        yield f"http://127.0.0.1:{server.server_port}", jobs, manager, state
        server.shutdown()
        thread.join(3)
    jobs.close()


@pytest.mark.parametrize("width", [1400, 390])
def test_define_and_follow_an_agent_job(page, agent_site, errors, width, monkeypatch, tmp_path):
    url, jobs, manager, state = agent_site
    page.set_viewport_size({"width": width, "height": 1000})
    page.goto(url + "/#cron")
    # Agent jobs are offered once the first snapshot says agents can run.
    expect(page.locator("#cron-status")).not_to_have_text("Loading cron jobs…")
    page.get_by_role("button", name="New job").click()
    dialog = page.locator("#cron-dialog")
    expect(dialog.get_by_label("An agent task")).to_be_checked()
    expect(dialog.get_by_label("Command", exact=True)).to_be_hidden()
    dialog.get_by_label("Name").fill("Nightly triage")
    clone = str((manager.src / "repo").resolve())
    expect(dialog.locator("#cron-repo option")).to_have_text([f"base/repo · {clone}"])
    dialog.get_by_label("Branch", exact=True).fill("nightly")
    dialog.get_by_label("Base branch (optional)").fill("feature")
    dialog.get_by_label("Agent", exact=True).select_option("codex")
    choose_option(dialog.get_by_role("combobox", name="Model (optional)"), "fixture-codex")
    choose_option(dialog.get_by_role("combobox", name="Reasoning effort (optional)"), "high")
    dialog.get_by_label("Allow Docker in the agent’s sandbox").check()
    dialog.get_by_label("Prompt").fill("Triage new issues")
    page.screenshot(path=str(tmp_path / f"agent-dialog-{width}.png"))
    dialog.get_by_role("button", name="Save job").click()
    expect(dialog).to_be_hidden()
    [saved] = jobs.snapshot()["jobs"]
    assert {k: saved[k] for k in ("kind", "repo", "clone", "branch", "base", "agent")} == {
        "kind": "agent",
        "repo": "base/repo",
        "clone": clone,
        "branch": "nightly",
        "base": "feature",
        "agent": "codex",
    }
    assert (saved["model"], saved["effort"], saved["docker"]) == ("fixture-codex", "high", True)

    if width < 900:
        page.locator(".cron-job").first.click()
    detail = page.locator("#cron-detail")
    expect(detail).to_contain_text("base/repo · branch nightly")
    expect(detail).to_contain_text("Codex · fixture-codex · high effort · Docker")
    expect(detail.locator(".cron-command")).to_have_text("Triage new issues")
    detail.get_by_role("button", name="Run now").click()
    expect(detail.locator(".cron-runs")).to_contain_text("Waiting for the agent to finish")
    expect(detail.get_by_role("button", name="Run now")).to_be_disabled()
    expect(detail.get_by_role("link", name="Open in Collie")).to_have_attribute(
        "href", "http://127.0.0.1:8787/space/w1"
    )
    expect(detail.get_by_role("button", name="Transcript")).to_be_visible()
    expect(detail.locator(".cron-log")).to_contain_text("Codex started in ")
    assert page.evaluate("document.documentElement.scrollWidth") <= width

    # The agent confirms it is done and is exited; its answer becomes the run's output.
    run = jobs.snapshot()["jobs"][0]["runs"][0]
    marker = run["agent_run"]["exit_marker"]
    monkeypatch.setattr(
        workspace_exit,
        "step",
        lambda record, *a: {
            **record,
            "state": "exited",
            "target": {"rollout": "r", "session_id": "s"},
        },
    )
    monkeypatch.setattr(
        cron_agents.handoff, "latest_turn", lambda *a: ("t", f"Labelled 3 issues.\n{marker}")
    )
    jobs.watch()
    expect(detail.locator(".cron-runs")).to_contain_text("Succeeded", timeout=20000)
    expect(detail.locator(".cron-log")).to_contain_text("Final response:\nLabelled 3 issues.")
    expect(detail.get_by_role("button", name="Run now")).to_be_enabled()
    page.screenshot(path=str(tmp_path / f"agent-detail-{width}.png"), full_page=True)

    # Editing shows the saved agent settings.
    detail.get_by_role("button", name="Edit").click()
    expect(dialog.get_by_label("Prompt")).to_have_value("Triage new issues")
    expect(dialog.get_by_label("Branch", exact=True)).to_have_value("nightly")
    expect(dialog.get_by_label("Agent", exact=True)).to_have_value("codex")
    expect(dialog.locator("#cron-effort")).to_have_value("high")
    expect(dialog.get_by_label("Allow Docker in the agent’s sandbox")).to_be_checked()
    dialog.get_by_role("button", name="Close").click()

    # A model the agent catalog no longer lists is shown, not silently replaced by
    # Default: saving then asks for an available one.
    with jobs.db() as db:
        job = jobs.job(db, saved["id"])
        job.update(model="retired-model", effort="ultra")
        db.execute("UPDATE jobs SET data=? WHERE id=?", (json.dumps(job), job["id"]))
    page.reload()
    if width < 900:
        page.locator(".cron-job").first.click()
    detail.get_by_role("button", name="Edit").click()
    expect(dialog.locator("#cron-model")).to_have_value("retired-model")
    expect(dialog.locator("#cron-effort")).to_have_value("ultra")
    expect(dialog.get_by_role("combobox", name="Model (optional)")).to_have_value(
        "retired-model (not listed)"
    )
    dialog.get_by_label("Name").fill("Nightly triage, renamed")
    dialog.get_by_role("button", name="Save job").click()
    expect(page.locator("#cron-form-error")).to_contain_text("Select an available model")
    assert jobs.snapshot()["jobs"][0]["model"] == "retired-model"
    dialog.get_by_role("button", name="Close").click()

    detail.get_by_role("button", name="Edit").click()
    dialog.get_by_label("A shell command").check()
    expect(dialog.get_by_label("Prompt")).to_be_hidden()
    dialog.get_by_label("Command", exact=True).fill("echo switched")
    dialog.get_by_role("button", name="Save job").click()
    expect(dialog).to_be_hidden()
    expect(detail.locator(".cron-command")).to_have_text("echo switched")
    assert "prompt" not in jobs.snapshot()["jobs"][0]


def test_an_agent_job_can_run_outside_safehouse(page, agent_site, errors, tmp_path):
    url, jobs, _, state = agent_site
    page.set_viewport_size({"width": 390, "height": 1000})
    page.goto(url + "/#cron")
    page.get_by_role("button", name="New job").click()
    dialog = page.locator("#cron-dialog")
    dialog.get_by_label("Name").fill("Rebase elsewhere")
    dialog.get_by_label("Branch", exact=True).fill("nightly")
    dialog.get_by_label("Agent", exact=True).select_option("claude")
    docker = dialog.get_by_label("Allow Docker in the agent’s sandbox")
    outside = dialog.get_by_label("Run outside Safehouse (can write anywhere)")
    expect(dialog.locator(".cron-unsandboxed-note")).to_contain_text(
        "Agents it starts with wt stay in Safehouse"
    )
    expect(outside).not_to_be_checked()
    docker.check()
    # Docker access is a Safehouse setting: running outside it clears and locks it.
    outside.check()
    expect(docker).not_to_be_checked()
    expect(docker).to_be_disabled()
    outside.uncheck()
    expect(docker).to_be_enabled()
    outside.check()
    dialog.get_by_label("Prompt").fill("Rebase the conflicting PRs")
    page.screenshot(path=str(tmp_path / "unsandboxed-dialog.png"))
    dialog.get_by_role("button", name="Save job").click()
    expect(dialog).to_be_hidden()
    [saved] = jobs.snapshot()["jobs"]
    assert (saved["unsandboxed"], saved["docker"]) == (True, False)
    page.locator(".cron-job").first.click()
    detail = page.locator("#cron-detail")
    expect(detail).to_contain_text("Claude · Outside Safehouse")
    detail.get_by_role("button", name="Edit").click()
    expect(outside).to_be_checked()
    expect(docker).to_be_disabled()
    assert page.evaluate("document.documentElement.scrollWidth") <= 390


def test_a_job_saved_on_a_hidden_default_login_opens_on_the_same_login(
    page, agent_site, errors, monkeypatch
):
    url, jobs, _, _ = agent_site
    # Default is hidden because psu is the same login; work has the most quota left.
    monkeypatch.setattr(
        claude_accounts,
        "choices",
        lambda: [
            {"id": "psu", "label": "psu", "config_dir": "/fixture/psu", "default": True},
            {"id": "work", "label": "work", "config_dir": "/fixture/work"},
        ],
    )
    page.route(
        "**/api/llm-usage*",
        lambda route: route.fulfill(
            json={
                "accounts": [
                    {
                        "id": "claude:work",
                        "agent": "claude",
                        "account": "work",
                        "label": "Claude · work",
                        "windows": [],
                        "left_percent": 90,
                        "error": None,
                        "checked_at": 1,
                    }
                ],
                "best": "claude:work",
                "attempted_at": 1,
                "reader": "running",
            }
        ),
    )
    page.goto(url + "/#cron")
    page.get_by_role("button", name="New job").click()
    dialog = page.locator("#cron-dialog")
    dialog.get_by_label("Name").fill("Nightly triage")
    dialog.get_by_label("Branch", exact=True).fill("nightly")
    dialog.get_by_label("Agent", exact=True).select_option("codex")
    dialog.get_by_label("Prompt").fill("Triage new issues")
    dialog.get_by_role("button", name="Save job").click()
    expect(dialog).to_be_hidden()
    with jobs.db() as db:
        [job] = jobs.snapshot()["jobs"]
        job = jobs.job(db, job["id"])
        job.update(agent="claude", claude_account="")
        db.execute("UPDATE jobs SET data=? WHERE id=?", (json.dumps(job), job["id"]))
    page.reload()
    page.get_by_role("button", name="Edit").click()
    account = dialog.locator("#cron-claude-account")
    expect(account.locator("option")).to_have_text(["psu", "work · 90% left"])
    expect(account).to_have_value("psu")
