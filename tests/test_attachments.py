"""Files attached to agent messages and tasks: storage, naming, and the text agents get."""

import http.client
import json
import re
import threading

import attachments
import dashboard
import pytest


def test_uploads_get_new_safe_names_and_are_never_overwritten(tmp_path):
    first = attachments.save(tmp_path, "../../etc/Screen Shot 1.png", b"png")
    second = attachments.save(tmp_path, "../../etc/Screen Shot 1.png", b"other")
    assert first["id"] != second["id"]
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}-[0-9a-f]{8}-Screen-Shot-1\.png", first["id"])
    assert first["name"] == "../../etc/Screen Shot 1.png" and first["size"] == 3
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted([first["id"], second["id"]])
    assert (tmp_path / first["id"]).read_bytes() == b"png"
    for name, stored in [
        (".env", "env"),
        ("..", "file"),
        ("C:\\x\\notes.txt", "notes.txt"),
        ("-rf", "rf"),
    ]:
        assert attachments.save(tmp_path, name, b"x")["id"].endswith(f"-{stored}")
    for data in (b"", b"x" * (attachments.MAX_FILE + 1)):
        with pytest.raises(ValueError, match="at most 25 MB"):
            attachments.save(tmp_path, "big.bin", data)


def test_attached_files_become_paths_after_the_text(tmp_path):
    one = attachments.save(tmp_path, "a.png", b"1")["id"]
    two = attachments.save(tmp_path, "b.log", b"2")["id"]
    request = {"text": "Look at these ", "attachments": [one, two]}
    attachments.attach(tmp_path, "workspace-message", request)
    assert request == {
        "text": f"Look at these\n\nAttached files (read them from these paths):\n"
        f"- {tmp_path / one}\n- {tmp_path / two}"
    }
    # Attachments alone make a message.
    alone = {"text": "", "attachments": [one]}
    attachments.attach(tmp_path, "workspace-message", alone)
    assert alone["text"].startswith("Attached files")
    # A task filed as an issue keeps its paths for the agent only.
    filed = {"task": "It crashes", "issue_title": "Crash", "attachments": [one]}
    attachments.attach(tmp_path, "workspace-new", filed)
    assert filed["task"] == "It crashes" and str(tmp_path / one) in filed["task_files"]
    for action in ("workspace-action", "workspace-batch", "workspace-new"):
        task = {"task": "Fix", "attachments": [one]}
        attachments.attach(tmp_path, action, task)
        assert task["task"].endswith(str(tmp_path / one))
    untouched = {"task": "Fix", "attachments": []}
    attachments.attach(tmp_path, "workspace-action", untouched)
    assert untouched == {"task": "Fix"}


@pytest.mark.parametrize(
    ("ids", "error"),
    [
        ("x", "at most 10"),
        (["../secret"], "at most 10"),
        (["2026-10-05-0123abcd-gone.txt"], "missing"),
        ([f"2026-10-05-0123abcd-{n}.txt" for n in range(11)], "at most 10"),
    ],
)
def test_only_existing_uploads_can_be_attached(tmp_path, ids, error):
    with pytest.raises(ValueError, match=error):
        attachments.attach(tmp_path, "workspace-message", {"text": "x", "attachments": ids})
    with pytest.raises(ValueError, match="cannot be attached"):
        attachments.attach(tmp_path, "cancel", {"id": "x", "attachments": ["a"]})


def post(port, path, body, headers):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.request("POST", path, body, {"Host": f"127.0.0.1:{port}", **headers})
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def test_the_dashboard_stores_uploads_and_refuses_cross_site_ones(tmp_path):
    folder = tmp_path / "inbox"
    with dashboard.DashboardServer(tmp_path, 0, attachments=folder) as httpd:
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            port = httpd.server_port
            upload = {
                "Content-Type": "application/octet-stream",
                "X-Babysit-Action": "attachment-upload",
                "X-Filename": "shot%20%C3%A9.png",
            }
            status, value = post(port, "/api/attachment-upload", b"\x89PNG", upload)
            assert status == 200 and value["name"] == "shot é.png" and value["size"] == 4
            assert (folder / value["id"]).read_bytes() == b"\x89PNG"
            status, value = post(
                port, "/api/attachment-upload", b"x", {**upload, "Content-Type": "image/png"}
            )
            assert status == 400 and "at most 25 MB" in value["error"]
            status, _ = post(
                port, "/api/attachment-upload", b"x", {**upload, "Sec-Fetch-Site": "cross-site"}
            )
            assert status == 403
            assert len(list(folder.iterdir())) == 1
        finally:
            httpd.shutdown()
            thread.join(5)


def test_client_sent_paths_and_duplicate_attachments_are_refused(tmp_path):
    one = attachments.save(tmp_path, "a.png", b"1")["id"]
    forged = {"task": "Fix", "issue_title": "Bug", "task_files": "- /etc/passwd"}
    attachments.attach(tmp_path, "workspace-new", forged)
    assert "task_files" not in forged
    with pytest.raises(ValueError, match="at most 10"):
        attachments.attach(tmp_path, "workspace-message", {"text": "x", "attachments": [one, one]})


def test_prompt_history_keeps_the_task_without_its_attachments(tmp_path):
    one = attachments.save(tmp_path, "a.png", b"1")["id"]
    request = {"task": "Reproduce it", "attachments": [one]}
    attachments.attach(tmp_path, "workspace-action", request)
    assert attachments.without_note(request["task"]) == "Reproduce it"
    assert attachments.without_note("No files here") == "No files here"


def test_an_oversized_upload_is_refused_before_its_body_is_read(tmp_path):
    with dashboard.DashboardServer(tmp_path, 0) as httpd:
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=10)
            connection.putrequest("POST", "/api/attachment-upload")
            for name, value in {
                "Host": f"127.0.0.1:{httpd.server_port}",
                "Content-Type": "application/octet-stream",
                "X-Babysit-Action": "attachment-upload",
                "Content-Length": str(attachments.MAX_FILE + 1),
            }.items():
                connection.putheader(name, value)
            connection.endheaders()
            response = connection.getresponse()
            assert response.status == 400 and b"at most 25 MB" in response.read()
            connection.close()
            assert not (tmp_path / "attachments").exists()
        finally:
            httpd.shutdown()
            thread.join(5)
