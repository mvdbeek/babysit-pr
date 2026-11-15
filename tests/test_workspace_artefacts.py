"""The viewer's Artefacts tab: names found in a checkout's transcripts, looked up on a
dufs share. The share is a stand-in on a loopback port and, where the dufs binary is
installed, dufs itself; sessions, state and the dashboard are temporary."""

import http.client
import json
import os
import threading
import time
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import dashboard
import dufs_share
import pytest
import workspace_artefacts as wa
from pr_supervisor import open_db, save_job
from test_workspace_viewer import write_jsonl

SID = "8f2c6f0e-3b7a-4d34-9a8f-2b8d6c1e9a10"
OLDER = "7a2c6f0e-3b7a-4d34-9a8f-2b8d6c1e9a11"


def reply(text):
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}


def tool(name, value, call="t1", output=None):
    items = [
        {
            "type": "assistant",
            "message": {
                "content": [{"type": "tool_use", "id": call, "name": name, "input": value}]
            },
        }
    ]
    if output is not None:
        items.append(
            {
                "type": "user",
                "message": {
                    "content": [{"type": "tool_result", "tool_use_id": call, "content": output}]
                },
            }
        )
    return items


def prompt(text):
    return {"type": "user", "message": {"content": text}}


@pytest.fixture(params=["stand-in", "dufs"])
def workspace(request, tmp_path, monkeypatch):
    """A checkout with a Claude session store, a shared ``outbox`` and a dashboard home
    configured for it. ``record(*items)`` writes a session; ``artefacts()`` reads the
    configuration as a dashboard start would."""
    if request.param == "dufs" and not dufs_share.installed():
        pytest.skip("the dufs binary is not installed")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    checkout = tmp_path / "wt" / "repo"
    checkout.mkdir(parents=True)
    outbox = tmp_path / "share" / "outbox"
    outbox.mkdir(parents=True)
    home = tmp_path / "home"
    folder = (
        tmp_path / "claude" / "projects" / "".join(c if c.isalnum() else "-" for c in str(checkout))
    )

    def record(*items, session=SID):
        flat = []
        for item in items:
            flat.extend(item if isinstance(item, list) else [item])
        common = {"sessionId": session, "cwd": str(checkout), "timestamp": "2026-11-15T10:00:00Z"}
        write_jsonl(folder / f"{session}.jsonl", *({**common, **item} for item in flat))

    with dufs_share.serve(outbox.parent, real=request.param == "dufs") as base:

        def configure(**config):
            path = home / "experiments" / "artefacts" / "config.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {"enabled": True, "url": f"{base}/outbox", "root": str(outbox), **config}
                )
            )

        configure()
        yield types.SimpleNamespace(
            checkout=checkout,
            outbox=outbox,
            home=home,
            url=f"{base}/outbox",
            record=record,
            configure=configure,
            artefacts=lambda **options: wa.Artefacts(home, **options),
        )


def files(value):
    return {group["path"]: [row["name"] for row in group["files"]] for group in value["folders"]}


def test_files_an_agent_wrote_or_linked_are_listed_with_what_the_share_holds(workspace):
    task = workspace.outbox / "dufs tab"
    task.mkdir()
    (task / "2026-11-15-report.md").write_text("# Report\n")
    (task / "shot.png").write_bytes(b"\x89PNG" + b"0" * 60)
    (task / "notes.txt").write_text("nobody named this")
    os.utime(task / "shot.png", (1_800_000_000, 1_800_000_000))
    workspace.record(
        prompt("Write a report"),
        tool("Bash", {"command": "make report"}, "t1", "ok"),
        reply(
            f"Done: [Report]({workspace.url}/dufs%20tab/2026-11-15-report.md?view) and a"
            f" screenshot at {workspace.url}/dufs%20tab/shot.png."
        ),
    )
    value = workspace.artefacts().listing(workspace.checkout)
    assert (value["enabled"], value["share"]) == (True, workspace.url + "/")
    assert (value["searched"], value["behind"], value["missing"], value["notes"]) == (1, 0, [], [])
    [folder] = value["folders"]
    assert (folder["path"], folder["url"]) == ("dufs tab", f"{workspace.url}/dufs%20tab/")
    # The folder itself was not named: its third file is counted, not listed.
    assert (folder["named"], folder["others"], folder["more"]) == (None, 1, 0)
    shot, report = folder["files"]  # Newest first.
    assert (shot["name"], shot["kind"], shot["size"], shot["modified"]) == (
        "shot.png",
        "file",
        64,
        1_800_000_000,
    )
    assert shot["url"] == shot["image"] == f"{workspace.url}/dufs%20tab/shot.png"
    assert (report["path"], report["size"], report["image"]) == (
        "dufs tab/2026-11-15-report.md",
        9,
        None,
    )
    # Markdown opens in the share's viewer, as artefact-url links it.
    assert report["url"] == f"{workspace.url}/dufs%20tab/2026-11-15-report.md?view"
    # Each is traced to the entry that names it: here the reply, which is no tool call.
    assert report["named"] == {
        "session": SID,
        "agent": "claude",
        "title": "Write a report",
        "index": 2,
        "time": "2026-11-15T10:00:00Z",
        "tool": None,
    }
    assert shot["named"]["index"] == 2


def test_a_written_file_is_traced_to_its_tool_call(workspace):
    (workspace.outbox / "task").mkdir()
    (workspace.outbox / "task" / "fix.patch").write_text("diff")
    workspace.record(
        prompt("Make a patch"),
        tool("Bash", {"command": f"git diff > {workspace.outbox}/task/fix.patch; echo done"}),
    )
    [folder] = workspace.artefacts().listing(workspace.checkout)["folders"]
    [patch] = folder["files"]
    assert (patch["name"], patch["named"]["tool"], patch["named"]["index"]) == (
        "fix.patch",
        "Bash",
        1,
    )


def test_prompts_tool_output_and_reading_tools_name_nothing(workspace):
    other = workspace.outbox / "other-task"
    other.mkdir()
    for name in ("prompted.md", "read.md", "printed.md", "fetched.md"):
        (other / name).write_text("someone else's")
    workspace.record(
        prompt(f"Compare with {workspace.url}/other-task/prompted.md"),
        tool("Read", {"file_path": f"{other}/read.md"}, "t1", "text"),
        tool("WebFetch", {"url": f"{workspace.url}/other-task/fetched.md"}, "t2", "text"),
        tool("Bash", {"command": "ls"}, "t3", f"{other}/printed.md\n"),
        reply("Nothing to share."),
    )
    value = workspace.artefacts().listing(workspace.checkout)
    assert (value["folders"], value["missing"], value["searched"]) == ([], [], 1)


def test_a_named_folder_lists_all_its_files_and_a_glob_names_its_folder(workspace):
    shots = workspace.outbox / "task" / "shots"
    shots.mkdir(parents=True)
    (shots / "a.png").write_bytes(b"a")
    (shots / "b.png").write_bytes(b"bb")
    os.utime(shots / "a.png", (1_700_000_000, 1_700_000_000))  # The older of the two.
    (shots / "raw").mkdir()
    (shots / "raw" / "c.png").write_bytes(b"c")
    (workspace.outbox / "task" / "summary.md").write_text("s")
    workspace.record(
        prompt("Screenshots please"),
        tool("Bash", {"command": f"mkdir -p {workspace.outbox}/task/shots/"}, "t1"),
        tool("Bash", {"command": f"cp /tmp/x/*.png {workspace.outbox}/task/shots/*.png"}, "t2"),
        reply(f"See {workspace.url}/task/shots/ and {workspace.url}/task/summary.md"),
    )
    value = workspace.artefacts().listing(workspace.checkout)
    # Newest file first, folders after the files.
    assert files(value) == {"task/shots": ["b.png", "a.png", "raw"], "task": ["summary.md"]}
    shots_group = next(group for group in value["folders"] if group["path"] == "task/shots")
    # Named as a folder, by the reply: nothing in it is counted as someone else's.
    assert (shots_group["named"]["index"], shots_group["others"]) == (3, 0)
    raw = shots_group["files"][-1]  # Folders after files; its size counts its entries.
    assert (raw["kind"], raw["size"], raw["url"], raw["image"], raw["named"]) == (
        "folder",
        1,
        f"{workspace.url}/task/shots/raw/",
        None,
        None,
    )
    assert all(row["named"] is None for row in shots_group["files"])
    task = next(group for group in value["folders"] if group["path"] == "task")
    # `shots` is a folder beside the named file, not one of the folder's other files.
    assert task["others"] == 0


def test_names_the_share_does_not_hold_are_missing_and_unsafe_ones_ignored(workspace):
    (workspace.outbox / "task").mkdir()
    (workspace.outbox / "task" / "kept.md").write_text("k")
    (workspace.outbox.parent / "secret.txt").write_text("outside the base")
    workspace.record(
        prompt("Go"),
        reply(
            f"Kept {workspace.url}/task/kept.md, deleted {workspace.url}/task/gone.md,"
            f" never made {workspace.url}/nowhere/x.md, escaping {workspace.url}/../secret.txt"
            f" and {workspace.url}/task/%2e%2e/%2e%2e/secret.txt, another base"
            f" {workspace.url}-old/task/kept.md, another host https://example.org/task/kept.md,"
            f" bad encoding {workspace.url}/task/%ff.md, and a file as a folder"
            f" {workspace.url}/task/kept.md/inner.md"
        ),
    )
    value = workspace.artefacts().listing(workspace.checkout)
    assert files(value) == {"task": ["kept.md"]}
    assert sorted(entry["path"] for entry in value["missing"]) == [
        "nowhere/x.md",
        "task/gone.md",
        "task/kept.md/inner.md",
    ]
    assert all(entry["named"]["session"] == SID for entry in value["missing"])


def test_the_newest_session_that_names_a_file_is_the_one_linked(workspace):
    (workspace.outbox / "task").mkdir()
    (workspace.outbox / "task" / "plan.md").write_text("p")
    workspace.record(prompt("Plan"), reply(f"{workspace.url}/task/plan.md"), session=OLDER)
    old = next((workspace.checkout.parent.parent / "claude").rglob(f"{OLDER}.jsonl"))
    os.utime(old, (1_700_000_000, 1_700_000_000))
    workspace.record(prompt("Revise"), reply("Thinking."), reply(f"{workspace.url}/task/plan.md"))
    value = workspace.artefacts().listing(workspace.checkout)
    [plan] = value["folders"][0]["files"]
    assert value["searched"] == 2
    assert (plan["named"]["session"], plan["named"]["index"], plan["named"]["title"]) == (
        SID,
        2,
        "Revise",
    )


def test_a_tilde_spelling_of_the_shared_directory_is_recognised(workspace, monkeypatch):
    monkeypatch.setenv("HOME", str(workspace.outbox.parent))
    workspace.configure(root="~/outbox/")
    (workspace.outbox / "task").mkdir()
    (workspace.outbox / "task" / "a.txt").write_text("a")
    (workspace.outbox / "task" / "b.txt").write_text("b")
    workspace.record(
        prompt("Go"),
        tool("Bash", {"command": "cp a.txt ~/outbox/task/a.txt"}, "t1"),
        tool("Bash", {"command": f"cp b.txt {workspace.outbox}/task/b.txt"}, "t2"),
        # Not the shared directory: a longer path that ends the same way, and a sibling.
        tool("Bash", {"command": f"ls /backup{workspace.outbox}/task/c.txt ~/outbox2/d"}, "t3"),
    )
    value = workspace.artefacts().listing(workspace.checkout)
    assert sorted(files(value)["task"]) == ["a.txt", "b.txt"]
    assert value["missing"] == []


def test_an_unreachable_share_is_an_error_and_a_partial_read_a_note(workspace):
    (workspace.outbox / "one").mkdir()
    (workspace.outbox / "one" / "a.md").write_text("a")
    (workspace.outbox / "two").mkdir()
    (workspace.outbox / "two" / "b.md").write_text("b")
    workspace.record(prompt("Go"), reply(f"{workspace.url}/one/a.md {workspace.url}/two/b.md"))
    workspace.configure(api="http://127.0.0.1:9/outbox")
    with pytest.raises(ValueError, match=r"The share at http://127\.0\.0\.1:9/outbox could not"):
        workspace.artefacts().listing(workspace.checkout)
    workspace.configure()
    asked = []

    def flaky(url, timeout):
        asked.append(url)
        if len(asked) > 1:
            raise OSError("connection reset")
        return wa.read_share(url, timeout)

    value = workspace.artefacts(read=flaky).listing(workspace.checkout)
    # The first folder is shown; the share is not asked again once it failed.
    assert (len(value["folders"]), len(asked)) == (1, 2)
    assert value["notes"] == ["The share could not be read completely: connection reset."]


def test_one_answer_asks_the_share_a_bounded_number_of_times(workspace, monkeypatch):
    monkeypatch.setattr(wa, "MAX_LISTINGS", 3)
    links = []
    for number in range(5):
        (workspace.outbox / f"task{number}").mkdir()
        (workspace.outbox / f"task{number}" / "a.md").write_text("a")
        links.append(f"{workspace.url}/task{number}/a.md {workspace.url}/task{number}/b.md")
    workspace.record(prompt("Go"), reply(" ".join(links)))
    asked = []

    def counting(url, timeout):
        asked.append(url)
        return wa.read_share(url, timeout)

    value = workspace.artefacts(read=counting).listing(workspace.checkout)
    # One listing serves both names of a folder.
    assert asked == [f"{workspace.url}/task{number}/?json" for number in range(3)]
    assert len(value["folders"]) == 3 and len(value["missing"]) == 3
    assert value["notes"] == [
        "4 named paths were not looked up: one answer reads only so much of the share."
    ]


def test_more_names_than_are_kept_say_so(workspace, monkeypatch):
    monkeypatch.setattr(wa, "MAX_NAMED", 2)
    (workspace.outbox / "task").mkdir()
    workspace.record(
        prompt("Go"), reply(" ".join(f"{workspace.url}/task/{n}.md" for n in range(4)))
    )
    value = workspace.artefacts().listing(workspace.checkout)
    assert [entry["path"] for entry in value["missing"]] == ["task/0.md", "task/1.md"]
    assert value["notes"] == ["Only the newest 2 names were looked up."]
    monkeypatch.setattr(wa, "MAX_MATCHES", 1)
    assert workspace.artefacts().listing(workspace.checkout)["notes"] == [
        "Only the newest 1 transcript entries that mention the share were read.",
        "Only the newest 2 names were looked up.",
    ]


def test_a_workers_agents_are_found_by_their_links_only(workspace):
    artefacts = workspace.artefacts()
    assert str(workspace.outbox) in artefacts.expression(local=True)
    assert artefacts.expression(local=False) == f'"{workspace.url}"'
    session = {"id": SID, "agent": "codex", "title": "Go"}
    entry = {
        "role": "assistant",
        "text": f"{workspace.url}/chonk/a.md and {workspace.outbox}/task/b.md",
        "time": None,
    }
    named, clipped = artefacts.named([(session, 4, entry, "")], local=False)
    assert (list(named), clipped) == (["chonk/a.md"], False)
    assert list(artefacts.named([(session, 4, entry, "")], local=True)[0]) == [
        "chonk/a.md",
        "task/b.md",
    ]


# Configuration -------------------------------------------------------------------


def test_the_integration_is_off_until_configured(tmp_path):
    artefacts = wa.Artefacts(tmp_path)
    assert artefacts.status() == {"enabled": False}
    assert artefacts.image_source() is None
    assert artefacts.listing(tmp_path) == {"enabled": False}
    config = tmp_path / "experiments" / "artefacts" / "config.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"enabled": False, "url": "https://share.example"}))
    assert wa.Artefacts(tmp_path).status() == {"enabled": False}


@pytest.mark.parametrize(
    "config,error",
    [
        ({}, "`url` must be a web address"),
        ({"url": "ftp://share.example"}, "must be http"),
        ({"url": "https://user:secret@share.example"}, "without credentials"),
        ({"url": "https://share.example/?q=1"}, "without credentials"),
        ({"url": "https://share.example/'; script-src *"}, "without credentials"),
        ({"url": "https://share.example:port"}, "not a web address"),
        ({"url": "https://share.example", "api": "file:///etc"}, "`api` must be http"),
        ({"url": "https://share.example", "root": "outbox"}, "`root` must be an absolute path"),
        ({"url": "https://share.example", "root": "/"}, "too short"),
    ],
)
def test_a_broken_configuration_offers_the_tab_with_its_error(tmp_path, config, error):
    path = tmp_path / "experiments" / "artefacts" / "config.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"enabled": True, **config}))
    artefacts = wa.Artefacts(tmp_path)
    status = artefacts.status()
    assert status["enabled"] is True and error in status["error"]
    # No origin from a configuration that was refused reaches a response header.
    assert artefacts.image_source() is None
    with pytest.raises(ValueError, match="configuration unavailable"):
        artefacts.listing(tmp_path)


def test_the_share_address_gives_links_listings_and_the_image_origin(tmp_path):
    path = tmp_path / "experiments" / "artefacts" / "config.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "enabled": True,
                "url": "https://Share.Example:8443/out box/",
                "api": "http://127.0.0.1:8642/outbox",
            }
        )
    )
    artefacts = wa.Artefacts(tmp_path)
    assert artefacts.status() == {
        "enabled": True,
        "error": "Artefact share configuration unavailable: `url` must be"
        " http(s)://host[:port][/path] without credentials",
        "share": None,
    }
    path.write_text(
        json.dumps(
            {
                "enabled": True,
                "url": "https://share.example:8443/outbox/",
                "api": "http://127.0.0.1:8642/outbox",
            }
        )
    )
    artefacts = wa.Artefacts(tmp_path)
    assert artefacts.status() == {
        "enabled": True,
        "error": None,
        "share": "https://share.example:8443/outbox/",
    }
    assert artefacts.image_source() == "https://share.example:8443"
    assert artefacts.api == "http://127.0.0.1:8642/outbox"
    # Without `api`, listings are read where the links lead.
    path.write_text(json.dumps({"enabled": True, "url": "https://share.example"}))
    assert wa.Artefacts(tmp_path).api == "https://share.example"


def test_a_listing_keeps_only_well_formed_entries(monkeypatch):
    assert wa.listed({"dir_exists": False, "paths": []}) == (None, False)
    assert wa.listed({"path_type": "File", "name": "a/b.md", "mtime": 1, "size": 1}) == (
        None,
        False,
    )
    with pytest.raises(wa.Unanswered, match="did not answer with a listing"):
        wa.listed(["not", "an", "object"])
    value, cut = wa.listed(
        {
            "dir_exists": True,
            "paths": [
                {"path_type": "File", "name": "ok.md", "mtime": 2500, "size": 3},
                {"path_type": "SymlinkDir", "name": "linked", "mtime": 1000, "size": 2},
                {"path_type": "File", "name": "odd.md", "mtime": "soon", "size": True},
                # Numbers that would not survive as JSON, or as a date.
                {"path_type": "File", "name": "nan.md", "mtime": float("nan"), "size": -1},
                {"path_type": "File", "name": "huge.md", "mtime": 10**400, "size": 2**70},
                {"path_type": "File", "name": "back\\slash.md", "mtime": 1, "size": 1},
                {"path_type": "File", "name": "../up.md", "mtime": 1, "size": 1},
                {"path_type": "File", "name": "a/b.md", "mtime": 1, "size": 1},
                {"path_type": "Socket", "name": "sock", "mtime": 1, "size": 1},
                {"path_type": "File", "name": 7, "mtime": 1, "size": 1},
                "text",
            ],
        }
    )
    unknown = {"folder": False, "size": None, "modified": None}
    assert (value, cut) == (
        {
            "ok.md": {"folder": False, "size": 3, "modified": 2.5},
            "linked": {"folder": True, "size": 2, "modified": 1.0},
            "odd.md": unknown,
            "nan.md": unknown,
            "huge.md": unknown,
        },
        False,
    )
    monkeypatch.setattr(wa, "MAX_ENTRIES", 1)
    entry = {"path_type": "File", "mtime": 1, "size": 1}
    assert wa.listed({"paths": [{**entry, "name": "a"}, {**entry, "name": "b"}]}) == (
        {"a": {"folder": False, "size": 1, "modified": 0.001}},
        True,
    )


class Odd(BaseHTTPRequestHandler):
    """A share that answers each path in its own wrong way."""

    def log_message(self, *args):
        pass

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/moved":
            self.send_response(302)
            self.send_header("Location", self.server.elsewhere + "/?json")
            self.end_headers()
        elif path == "/broken":
            self.send_response(500)
            self.end_headers()
        elif path in ("/slow", "/large"):
            self.send_response(200)
            self.end_headers()
            try:
                for _ in range(40):
                    self.wfile.write(b" " * (64 * 1024 if path == "/large" else 1))
                    self.wfile.flush()
                    if path == "/slow":
                        time.sleep(0.05)
            except OSError:
                pass  # The reader gave up, as it should.
        else:
            body = b'{"paths": [], "mtime": NaN}' if path == "/nan" else b"<html>"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body) + (path == "/short") * 10))
            self.end_headers()
            self.wfile.write(body)


def test_the_share_is_not_followed_elsewhere_and_odd_answers_are_refused(tmp_path, monkeypatch):
    (tmp_path / "a.md").write_text("a")
    monkeypatch.setattr(wa, "MAX_LISTING", 1024 * 1024)
    with dufs_share.serve(tmp_path) as base, ThreadingHTTPServer(("127.0.0.1", 0), Odd) as odd:
        odd.elsewhere = base
        thread = threading.Thread(target=odd.serve_forever, daemon=True)
        thread.start()
        try:
            there = f"http://127.0.0.1:{odd.server_port}"
            assert wa.read_share(f"{base}/?json")["paths"][0]["name"] == "a.md"
            # Only "not found" means it is not there.
            assert wa.read_share(f"{base}/missing.md") is None
            for path, reason in (
                ("/moved", "HTTP 302"),  # Not followed to the listing it points at.
                ("/broken", "HTTP 500"),
                ("/large", "exceeds the size limit"),
                ("/nan", "did not answer with JSON"),
                ("/html", "did not answer with JSON"),
            ):
                with pytest.raises(wa.Unanswered, match=reason):
                    wa.read_share(there + path)
            # A share that cannot be relied on is an OSError: it is not asked again.
            with pytest.raises(OSError, match="broke off"):
                wa.read_share(there + "/short")
            started = time.monotonic()
            with pytest.raises(OSError, match="too slowly"):
                wa.read_share(there + "/slow", timeout=0.3)
            assert time.monotonic() - started < 1.5  # The whole answer, not each read.
            with pytest.raises(OSError):
                wa.read_share("http://127.0.0.1:9/")
        finally:
            odd.shutdown()
            thread.join(timeout=5)


def test_a_folder_the_share_will_not_list_costs_only_the_names_in_it(workspace):
    for name in ("good", "bad", "later"):
        (workspace.outbox / name).mkdir()
        (workspace.outbox / name / "a.md").write_text("a")
    workspace.record(
        prompt("Go"),
        reply(" ".join(f"{workspace.url}/{name}/a.md" for name in ("good", "bad", "later"))),
    )

    def read(url, timeout):
        if "/bad/" in url:
            raise wa.Unanswered("HTTP 500")
        return wa.read_share(url, timeout)

    value = workspace.artefacts(read=read).listing(workspace.checkout)
    assert sorted(files(value)) == ["good", "later"]
    assert (value["missing"], value["notes"]) == ([], ["The share did not list bad (HTTP 500)."])


def test_a_listing_cut_short_does_not_call_its_other_names_missing(workspace, monkeypatch):
    monkeypatch.setattr(wa, "MAX_ENTRIES", 2)
    for name in ("a", "b", "c", "d"):
        (workspace.outbox / name).mkdir()
        (workspace.outbox / name / "x.md").write_text("x")
    workspace.record(
        prompt("Go"),
        tool("Bash", {"command": " ".join(f"ls {workspace.outbox}/{n}/" for n in "abcd")}),
        reply(f"And {workspace.url}/never-there/"),
    )
    value = workspace.artefacts().listing(workspace.checkout)
    # Two of the four named folders are in the two entries read of the top folder; the
    # others may be past them, as may the one that is not there at all.
    assert len(value["folders"]) == 2 and value["missing"] == []
    assert value["notes"] == [
        "A folder on the share holds more than 2 entries; what is past them was not looked up."
    ]


def test_long_folders_large_images_and_empty_folders(workspace, monkeypatch):
    monkeypatch.setattr(wa, "MAX_FILES", 2)
    monkeypatch.setattr(wa, "MAX_PREVIEW", 4)
    task = workspace.outbox / "task"
    (task / "empty").mkdir(parents=True)
    for name, size in (("small.png", 4), ("large.png", 5), ("c.txt", 1)):
        (task / name).write_bytes(b"x" * size)
    workspace.record(prompt("Go"), reply(f"{workspace.url}/task/ and {workspace.url}/task/empty/"))
    value = workspace.artefacts().listing(workspace.checkout)
    # The named folder that holds nothing is not a section of its own.
    [folder] = value["folders"]
    assert (folder["path"], len(folder["files"]), folder["more"]) == ("task", 2, 2)
    images = {row["name"]: row["image"] for row in folder["files"]}
    assert all((image is None) == (name != "small.png") for name, image in images.items()), images


# Through the dashboard -------------------------------------------------------------


def request(port, path):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), json.loads(response.read())
    finally:
        connection.close()


def test_the_dashboard_answers_for_a_watchs_checkout_and_allows_the_shares_images(workspace):
    (workspace.outbox / "task").mkdir()
    (workspace.outbox / "task" / "shot.png").write_bytes(b"png")
    workspace.record(prompt("Go"), reply(f"Shot: {workspace.url}/task/shot.png"))
    db = open_db(workspace.home)
    with db:
        save_job(
            db,
            {
                "id": "w",
                "repo": "o/r",
                "url": "https://github.com/o/r/pull/1",
                "cwd": str(workspace.checkout),
                "epoch": 0,
                "attempts": 0,
                "max_repairs": 5,
                "status": "watching",
                "summary": "",
                "branch": None,
                "pending_reviews": [],
                "snapshot": {"pr": {"head_branch": "f", "number": 1}},
            },
        )
    db.close()
    origin = workspace.url.removesuffix("/outbox")
    with dashboard.DashboardServer(workspace.home, 0) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_port
            status, headers, value = request(port, "/api/workspace-artefacts")
            assert (status, value) == (
                200,
                {"enabled": True, "error": None, "share": workspace.url + "/"},
            )
            assert f"img-src 'self' {origin};" in headers["Content-Security-Policy"]
            status, _, value = request(port, "/api/workspace-artefacts?watch=w")
            assert status == 200, value
            assert files(value) == {"task": ["shot.png"]}
            status, _, value = request(port, "/api/workspace-artefacts?watch=w&q=1")
            assert (status, value["error"]) == (400, "Unknown artefacts parameter")
            status, _, value = request(port, "/api/workspace-artefacts?watch=nope")
            assert (status, value["error"]) == (400, "Unknown watch; refresh")
        finally:
            server.shutdown()
            thread.join(timeout=5)
    # Without a configured share, the policy names no other origin.
    with dashboard.DashboardServer(workspace.home.parent / "plain", 0) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            status, headers, value = request(server.server_port, "/api/workspace-artefacts")
            assert (status, value) == (200, {"enabled": False})
            assert "img-src" not in headers["Content-Security-Policy"]
            status, _, value = request(server.server_port, "/api/workspace-artefacts?watch=w")
            assert status == 400  # No such watch in this home.
        finally:
            server.shutdown()
            thread.join(timeout=5)
