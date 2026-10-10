"""Resuming a session a babysit watch owns, in Chromium against a temporary dashboard.

The dashboard, its queue and the checkout are temporary; herdr and the agents are the
fakes from conftest, and PATH holds nothing else that could reach a live one.
"""

import re

import pytest
from playwright.sync_api import expect
from test_workspace_overview import exited as exited
from test_workspace_overview import load_watch, read, save_watch
from test_workspace_overview import server as server
from test_workspace_overview import site as site

pytestmark = pytest.mark.browser


@pytest.fixture
def owned(exited, server, tmp_path, monkeypatch):  # noqa: F811
    port, plugin = server
    # Only the fakes, git and the shell the fake herdr types into.
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:/usr/bin:/bin")
    plugin.value = plugin.collect()
    return f"http://127.0.0.1:{port}", *exited


def open_transcript(page, url):
    page.goto(url + "/#workspaces")
    line = page.locator("#ws-list tr").filter(has=page.get_by_text("merged-work", exact=True))
    line.get_by_role("button", name="Transcript", exact=True).click()
    return page.locator("#ws-viewer")


def test_cancel_watch_and_send_resumes_the_session(page, owned):
    url, state, sid, _, home = owned
    save_watch(home, sid)
    dialog = open_transcript(page, url)
    notice = page.locator("#ws-agent-interaction")
    expect(notice).to_contain_text(
        "This session belongs to babysit watch w-1 (blocked), for base/repo#7: Merged work. "
        "The watch resumes it itself"
    )
    expect(notice.get_by_role("link", name="Show the watch")).to_have_attribute(
        "href", "/?item=https%3A%2F%2Fgithub.com%2Fbase%2Frepo%2Fpull%2F7#watcher"
    )
    dialog.get_by_label("Message the agent").fill("Address review comment")
    dialog.get_by_role("button", name="Cancel watch and send…").click()
    # Nothing happens before the reader confirms.
    expect(notice).to_contain_text("Cancel babysit watch w-1 (blocked)?")
    assert load_watch(home)["status"] == "blocked"
    notice.get_by_role("button", name="Cancel watch and send", exact=True).click()
    expect(page.locator("#ws-message-status")).to_have_text(
        re.compile(
            r"Cancelled babysit watch w-1, resumed the session in w1:p\d+ and sent the message\."
        )
    )
    assert load_watch(home)["status"] == "stopped"
    assert read(state)["agents"][0]["argv"] == ["--resume", sid, "--", "Address review comment"]
    expect(dialog.get_by_label("Message the agent")).to_have_value("")
    expect(notice).not_to_contain_text("babysit watch")


def test_a_watch_in_a_repair_is_cancelled_after_it_and_nothing_resumes(page, owned):
    url, state, sid, _, home = owned
    save_watch(home, sid, status="running")
    dialog = open_transcript(page, url)
    notice = page.locator("#ws-agent-interaction")
    expect(notice).to_contain_text("It is running a repair now, which finishes first.")
    expect(dialog.get_by_role("button", name="Resume and send")).to_be_disabled()
    dialog.get_by_label("Message the agent").fill("Address review comment")
    notice.get_by_role("button", name="Cancel watch after this repair").click()
    expect(notice).to_contain_text("stops monitoring this pull request once its current repair")
    notice.get_by_role("button", name="Cancel watch", exact=True).click()
    expect(page.locator("#ws-message-status")).to_have_text(
        "Babysit watch w-1 stops once its current repair finishes; send your message then."
    )
    expect(notice).to_contain_text("Its cancellation is pending")
    job = load_watch(home)
    assert job["status"] == "running" and job["stop_after_run"]
    assert not read(state).get("runs")
    expect(dialog.get_by_label("Message the agent")).to_have_value("Address review comment")
