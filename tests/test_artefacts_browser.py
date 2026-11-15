"""The workspace viewer's Artefacts tab in Chromium, against a temporary dashboard.

The listing tests run end to end: a recorded Claude session, the dashboard's own
transcript search, and a share that is a stand-in or, where it is installed, dufs.
"""

import json
import os
import struct
import threading
import zlib

import dashboard
import dufs_share
import pytest
from playwright.sync_api import expect
from pr_supervisor import open_db, save_job
from test_dashboard_browser import dashboard_site as dashboard_site
from test_dashboard_browser import viewer_routes as viewer_routes
from test_dashboard_browser import workspace_routes as workspace_routes
from test_workspace_viewer import write_jsonl

pytestmark = pytest.mark.browser

SID = "8f2c6f0e-3b7a-4d34-9a8f-2b8d6c1e9a10"
OVERFLOW = "document.documentElement.scrollWidth <= document.documentElement.clientWidth"


def png(width, height, colours):
    """A real PNG of horizontal bands, so a thumbnail has something to show."""

    def chunk(kind, data):
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    rows = b"".join(
        b"\x00" + bytes(colours[row * len(colours) // height]) * width for row in range(height)
    )
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


@pytest.fixture
def share(request):
    """Which share the test runs against: the stand-in unless parametrized."""
    kind = getattr(request, "param", "stand-in")
    if kind == "dufs" and not dufs_share.installed():
        pytest.skip("the dufs binary is not installed")
    return kind


@pytest.fixture
def artefact_site(share, tmp_path, monkeypatch, fake_tools):
    """A watch whose checkout has one Claude session that made a report, two
    screenshots and a patch, on a configured share; yields the dashboard's URL."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    checkout = tmp_path / "wt" / "dufs-integration"
    checkout.mkdir(parents=True)
    outbox = tmp_path / "share" / "outbox"
    task = outbox / "dufs-tab"
    (task / "shots").mkdir(parents=True)
    (task / "2026-11-15-report.md").write_text("# Artefacts tab\n\nWhat changed and why.\n" * 40)
    (task / "fix-listing.patch").write_text("diff --git a/x b/x\n" * 12)
    (task / "scratch-notes.txt").write_text("not named by anyone")
    green, paper, ink = (40, 119, 90), (244, 245, 241), (37, 52, 47)
    (task / "shots" / "desktop.png").write_bytes(png(320, 200, [green, paper, paper, ink, paper]))
    (task / "shots" / "phone.png").write_bytes(png(120, 240, [ink, paper, green, paper]))
    for age, path in enumerate(
        [
            task / "2026-11-15-report.md",
            task / "shots" / "desktop.png",
            task / "shots" / "phone.png",
            task / "fix-listing.patch",
        ]
    ):
        os.utime(path, (1_794_700_000 - age * 3600, 1_794_700_000 - age * 3600))
    home = tmp_path / "home"
    folder = (
        tmp_path / "claude" / "projects" / "".join(c if c.isalnum() else "-" for c in str(checkout))
    )
    with dufs_share.serve(outbox.parent, real=share == "dufs") as base:
        url = f"{base}/outbox"
        common = {"sessionId": SID, "cwd": str(checkout)}

        def at(minute, item):
            return {**common, "timestamp": f"2026-11-15T10:{minute:02d}:00Z", **item}

        def assistant(*blocks):
            return {"type": "assistant", "message": {"content": list(blocks)}}

        write_jsonl(
            folder / f"{SID}.jsonl",
            at(0, {"type": "user", "message": {"content": "Add an Artefacts tab; show me shots"}}),
            at(
                5,
                assistant(
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Bash",
                        "input": {
                            "command": f"git diff > {task}/fix-listing.patch",
                            "description": "Save the patch",
                        },
                    }
                ),
            ),
            at(
                9,
                assistant(
                    {
                        "type": "tool_use",
                        "id": "t2",
                        "name": "Bash",
                        "input": {"command": f"cp reports/*.png {task}/shots/*.png"},
                    }
                ),
            ),
            at(
                12,
                assistant(
                    {
                        "type": "text",
                        "text": f"The tab is in. [Report]({url}/dufs-tab/2026-11-15-report.md?view),"
                        f" and an older draft at {url}/dufs-tab/2026-11-14-draft.md.",
                    }
                ),
            ),
        )
        config = home / "experiments" / "artefacts" / "config.json"
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps({"enabled": True, "url": url, "root": str(outbox)}))
        db = open_db(home)
        with db:
            save_job(
                db,
                {
                    "id": "w",
                    "repo": "mvdbeek/babysit-pr",
                    "url": "https://github.com/mvdbeek/babysit-pr/pull/173",
                    "cwd": str(checkout),
                    "epoch": 0,
                    "attempts": 0,
                    "max_repairs": 5,
                    "status": "watching",
                    "summary": "",
                    "branch": None,
                    "pending_reviews": [],
                    "snapshot": {"pr": {"head_branch": "dufs-integration", "number": 173}},
                },
            )
        db.close()
        with dashboard.DashboardServer(home, 0) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                yield f"http://127.0.0.1:{server.server_port}", url
            finally:
                server.shutdown()
                thread.join(timeout=5)


def open_artefacts(page, site):
    page.goto(site + "/#watcher")
    # Nobody to message in these tests: the watch's checkout has no workspace open.
    page.route(
        "**/api/workspace-agents?*",
        lambda route: route.fulfill(json={"agents": [], "sessions": [], "path": "/fixture"}),
    )
    page.evaluate(
        "window.workspaceViewer.open({watch: 'w', name: 'babysit-pr · dufs-integration'},"
        " 'transcript')"
    )
    viewer = page.locator("#ws-viewer")
    tab = viewer.get_by_role("tab", name="Artefacts")
    expect(tab).to_be_visible()
    tab.click()
    expect(tab).to_have_attribute("aria-selected", "true")
    return viewer


@pytest.mark.parametrize("share", ["stand-in", "dufs"], indirect=True)
@pytest.mark.parametrize("width", [1280, 390])
def test_the_tab_lists_the_workspaces_artefacts_from_the_share(page, artefact_site, share, width):
    site, url = artefact_site
    page.set_viewport_size({"width": width, "height": 844})
    viewer = open_artefacts(page, site)
    expect(page.locator("#ws-viewer-meta")).to_have_text(
        "4 artefacts in 2 folders, named in the transcripts of 1 session."
    )
    expect(viewer.get_by_role("link", name="Open the share")).to_have_attribute("href", url + "/")
    folders = viewer.locator(".ws-artefact-folder")
    expect(folders).to_have_count(2)
    # The folder of the newest file first. Its unnamed notes are counted, not listed.
    task = folders.nth(0)
    expect(task.get_by_role("heading")).to_contain_text("dufs-tab/")
    expect(task.get_by_role("heading")).to_contain_text("2 files · 1 other file in this folder")
    expect(task.get_by_role("heading").get_by_role("link")).to_have_attribute(
        "href", f"{url}/dufs-tab/"
    )
    report = task.locator(".ws-artefact").nth(0)
    link = report.get_by_role("link", name="2026-11-15-report.md")
    expect(link).to_have_attribute("href", f"{url}/dufs-tab/2026-11-15-report.md?view")
    expect(link).to_have_attribute("target", "_blank")
    expect(link).to_have_attribute("rel", "noopener noreferrer")
    expect(report.locator(".ws-artefact-kind")).to_have_text("MD")
    expect(report.locator(".ws-artefact-facts")).to_contain_text("1.5 KiB · changed")
    expect(report.get_by_role("button")).to_contain_text("Named by Claude in a reply")
    patch = task.locator(".ws-artefact").nth(1)
    expect(patch.locator(".ws-artefact-kind")).to_have_text("PATCH")
    expect(patch.get_by_role("button")).to_contain_text("Named by Claude in Bash")
    expect(task).not_to_contain_text("scratch-notes.txt")
    # The folder the copy command named: everything in it, with thumbnails off the share.
    shots = folders.nth(1)
    expect(shots.get_by_role("heading")).to_contain_text("dufs-tab/shots/")
    expect(shots).to_contain_text("The folder itself was named, so all its files are listed.")
    expect(shots.locator(".ws-artefact")).to_have_count(2)
    image = shots.locator(".ws-artefact").nth(0).locator("img")
    expect(image).to_have_attribute("src", f"{url}/dufs-tab/shots/desktop.png")
    # The page's content security policy let it load from the share's origin.
    expect(image).to_have_js_property("naturalWidth", 320)
    # A draft the agent linked that is gone from the share.
    missing = viewer.locator(".ws-artefact-missing")
    expect(missing.locator("summary")).to_have_text("1 named path is not on the share")
    missing.locator("summary").click()
    expect(missing.locator("code")).to_have_text("dufs-tab/2026-11-14-draft.md")
    assert page.evaluate(OVERFLOW)
    tabs = viewer.get_by_role("tablist", name="Workspace view").bounding_box()
    assert tabs["x"] >= 0 and tabs["x"] + tabs["width"] <= width
    page.screenshot(path=f"reports/artefacts-{share}-{width}.png")
    if width == 1280:
        page.emulate_media(color_scheme="dark")
        page.screenshot(path=f"reports/artefacts-{share}-dark.png")
        page.emulate_media(color_scheme="light")
    # Where the transcript names it: the session opens around that entry, marked.
    patch.get_by_role("button").click()
    expect(viewer.get_by_role("tab", name="Transcript")).to_have_attribute("aria-selected", "true")
    found = viewer.locator(".ws-found")
    expect(found).to_have_count(1)
    expect(found).to_contain_text("fix-listing.patch")
    page.screenshot(path=f"reports/artefacts-{share}-named-{width}.png")


def test_refresh_reads_the_share_again(page, artefact_site, tmp_path):
    site, url = artefact_site
    viewer = open_artefacts(page, site)
    expect(viewer.locator(".ws-artefact")).to_have_count(4)
    (tmp_path / "share" / "outbox" / "dufs-tab" / "shots" / "tablet.png").write_bytes(
        png(8, 8, [(0, 0, 0)])
    )
    viewer.get_by_role("button", name="Refresh").click()
    expect(viewer.locator(".ws-artefact")).to_have_count(5)
    expect(page.locator("#ws-viewer-meta")).to_contain_text("5 artefacts in 2 folders")


def test_the_tab_is_not_offered_without_a_share(page, dashboard_site, viewer_routes):
    url, _ = dashboard_site
    page.goto(url + "/#prs")
    with page.expect_response("**/api/workspace-artefacts") as answered:
        page.locator("#pr-list tr").first.get_by_role(
            "button", name="Transcript", exact=True
        ).click()
    assert answered.value.json() == {"enabled": False}
    viewer = page.locator("#ws-viewer")
    expect(viewer.get_by_role("tab", name="Transcript")).to_be_visible()
    expect(viewer.locator("#ws-view-artefacts")).to_be_hidden()


@pytest.fixture
def offered(page, dashboard_site, viewer_routes):
    """A viewer whose dashboard says it has a share; ``answer`` is what it lists."""
    url, _ = dashboard_site
    state = {"answer": None, "asked": 0}

    def artefacts(route):
        state["asked"] += 1
        if isinstance(state["answer"], str):
            route.fulfill(status=400, json={"error": state["answer"]})
        else:
            route.fulfill(json=state["answer"])

    page.route(
        "**/api/workspace-artefacts",
        lambda route: route.fulfill(json={"enabled": True, "error": None, "share": "https://s/"}),
    )
    page.route("**/api/workspace-artefacts?*", artefacts)
    page.goto(url + "/#prs")
    page.locator("#pr-list tr").first.get_by_role("button", name="Transcript", exact=True).click()
    return state


def listing(**values):
    return {
        "enabled": True,
        "share": "https://s/",
        "folders": [],
        "missing": [],
        "notes": [],
        "searched": 2,
        "behind": 0,
        **values,
    }


def test_empty_partial_and_failed_answers_each_say_so(page, offered):
    viewer = page.locator("#ws-viewer")
    tab = viewer.get_by_role("tab", name="Artefacts")
    offered["answer"] = listing(behind=1)
    tab.click()
    expect(page.locator("#ws-viewer-meta")).to_have_text(
        "No artefacts: nothing on the share is named in the transcripts of 2 sessions."
        " 1 session was not read to the end in time; refresh to read on."
    )
    expect(viewer.locator(".ws-artefacts")).to_contain_text(
        "Links an agent gives to the share, and files it writes or copies there, are listed here."
    )
    # Names that could not be looked up are not "nothing named".
    offered["answer"] = listing(notes=["The share did not list its top folder (HTTP 401)."])
    viewer.get_by_role("button", name="Refresh").click()
    expect(page.locator("#ws-viewer-meta")).to_have_text(
        "No artefacts could be listed from the transcripts of 2 sessions."
    )
    expect(viewer.locator(".ws-artefact-note")).to_have_text(
        "The share did not list its top folder (HTTP 401)."
    )
    offered["answer"] = listing(
        notes=["The share could not be read completely: connection reset."],
        folders=[
            {
                "path": "",
                "url": "https://s/",
                "named": None,
                "more": 3,
                "others": 0,
                "files": [
                    {
                        "name": "raw",
                        "path": "raw",
                        "kind": "folder",
                        "size": 1,
                        "modified": None,
                        "url": "https://s/raw/",
                        "image": None,
                        "named": None,
                    },
                    {
                        "name": "<img src=x onerror=window.injected=true>.html",
                        "path": "x.html",
                        "kind": "file",
                        "size": None,
                        "modified": None,
                        "url": "javascript:window.injected=true",
                        "image": "javascript:window.injected=true",
                        "named": None,
                    },
                ],
            }
        ],
    )
    viewer.get_by_role("button", name="Refresh").click()
    expect(viewer.locator(".ws-artefact-note")).to_have_text(
        "The share could not be read completely: connection reset."
    )
    # A folder in the listing is a folder, not one of the artefacts counted.
    expect(viewer.get_by_role("heading", name="Top of the share")).to_contain_text(
        "1 file · 1 folder · 3 more on the share"
    )
    expect(page.locator("#ws-viewer-meta")).to_contain_text("1 artefact in 1 folder")
    folder = viewer.locator(".ws-artefact").nth(0)
    expect(folder.get_by_role("link", name="raw/")).to_have_attribute("href", "https://s/raw/")
    expect(folder.locator(".ws-artefact-facts")).to_have_text("1 entry")
    row = viewer.locator(".ws-artefact").nth(1)
    # Names are text, and only web addresses become links or pictures.
    expect(row.locator(".ws-artefact-name")).to_have_text(
        "<img src=x onerror=window.injected=true>.html"
    )
    expect(row.get_by_role("link")).to_have_count(0)
    expect(row.locator("img")).to_have_count(0)
    expect(row.locator(".ws-artefact-kind")).to_have_text("HTML")
    assert page.evaluate("window.injected") is None
    offered["answer"] = "The share at http://127.0.0.1:8642 could not be read: refused"
    viewer.get_by_role("button", name="Refresh").click()
    expect(page.locator("#ws-viewer-error")).to_have_text(
        "The share at http://127.0.0.1:8642 could not be read: refused"
    )
    expect(viewer.locator(".ws-artefact")).to_have_count(0)
    # A dashboard restarted without its share since the page loaded.
    offered["answer"] = {"enabled": False}
    viewer.get_by_role("button", name="Refresh").click()
    expect(page.locator("#ws-viewer-meta")).to_have_text(
        "This dashboard no longer has an artefact share configured; reload the page."
    )
    expect(page.locator("#ws-viewer-error")).to_be_empty()


def test_a_status_check_that_failed_is_asked_again(page, dashboard_site, viewer_routes):
    url, _ = dashboard_site
    answers = [{"status": 503, "json": {"error": "starting"}}, {"json": {"enabled": True}}]
    page.route("**/api/workspace-artefacts", lambda route: route.fulfill(**answers.pop(0)))
    page.goto(url + "/#prs")
    button = page.locator("#pr-list tr").first.get_by_role("button", name="Transcript", exact=True)
    viewer = page.locator("#ws-viewer")
    button.click()
    expect(viewer.get_by_role("tab", name="Transcript")).to_be_visible()
    expect(viewer.locator("#ws-view-artefacts")).to_be_hidden()
    page.keyboard.press("Escape")
    button.click()
    expect(viewer.get_by_role("tab", name="Artefacts")).to_be_visible()


def test_a_minimized_artefacts_view_comes_back_as_the_transcript(page, offered):
    viewer = page.locator("#ws-viewer")
    offered["answer"] = listing()
    viewer.get_by_role("tab", name="Artefacts").click()
    expect(page.locator("#ws-viewer-meta")).to_contain_text("No artefacts")
    viewer.get_by_role("button", name="Minimize workspace viewer").click()
    chip = page.locator(".ws-tray-restore")
    expect(chip).to_contain_text("Transcript")
    chip.click()
    expect(viewer.get_by_role("tab", name="Transcript")).to_have_attribute("aria-selected", "true")
    # Still offered once the viewer is open again.
    expect(viewer.get_by_role("tab", name="Artefacts")).to_be_visible()
