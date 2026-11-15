"""A file share for tests that answers as dufs does: JSON listings, and the files.

``serve(root)`` runs an in-process stand-in on a free loopback port. It answers as
dufs 0.46 does with ``--allow-all``: a missing directory is an empty one it could create
(``dir_exists: false``), a file answers ``?json`` with its own details, and a folder's
size counts its entries. ``serve(root, real=True)`` runs the dufs binary instead; the
tests that take both run against it wherever ``dufs`` is on PATH, which is what shows
the stand-in still answers as dufs does. CI has no dufs and runs the stand-in only.
"""

import contextlib
import json
import mimetypes
import shutil
import socket
import subprocess
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit


def installed():
    """The dufs binary on PATH, if any."""
    return shutil.which("dufs")


def details(path, name):
    stat = path.stat()
    folder = path.is_dir()
    return {
        "path_type": ("Symlink" if path.is_symlink() else "") + ("Dir" if folder else "File"),
        "name": name,
        "mtime": int(stat.st_mtime * 1000),
        "size": len(list(path.iterdir())) if folder else stat.st_size,
    }


class Share(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, root):
        self.root = Path(root).resolve()
        # Every request's path and query, for tests that count what was asked.
        self.requests: list[str] = []
        super().__init__(("127.0.0.1", 0), Handler)


class Handler(BaseHTTPRequestHandler):
    server: Share

    def log_message(self, *args):
        pass

    def answer(self, code, body, content_type):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.server.requests.append(self.path)
        route = urlsplit(self.path)
        relative = unquote(route.path).strip("/")
        target = (self.server.root / relative).resolve()
        if not target.is_relative_to(self.server.root):
            self.answer(403, b"Forbidden", "text/plain")
            return
        if "json" in parse_qs(route.query, keep_blank_values=True):
            if target.is_file():
                value = details(target, relative)
            else:
                exists = target.is_dir()
                value = {
                    "href": "/" + relative,
                    "kind": "Index",
                    "uri_prefix": "/",
                    "allow_upload": True,
                    "dir_exists": exists,
                    "paths": [details(entry, entry.name) for entry in sorted(target.iterdir())]
                    if exists
                    else [],
                }
            self.answer(200, json.dumps(value).encode(), "application/json")
        elif target.is_file():
            kind = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            self.answer(200, target.read_bytes(), kind)
        else:
            self.answer(404, b"Not Found", "text/plain")


@contextlib.contextmanager
def serve(root, real=False):
    """The base URL of a share of ``root``; with ``real``, the dufs binary serves it."""
    if not real:
        with Share(root) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                yield f"http://127.0.0.1:{server.server_port}"
            finally:
                server.shutdown()
                thread.join(timeout=5)
        return
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    process = subprocess.Popen(
        [installed(), "--bind", "127.0.0.1", "--port", str(port), "--allow-all", str(root)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 10
        while True:
            try:
                urllib.request.urlopen(base + "/?json", timeout=1).close()
                break
            except OSError:
                if process.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError("dufs did not start") from None
                time.sleep(0.05)
        yield base
    finally:
        process.terminate()
        process.wait(timeout=10)
