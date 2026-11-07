#!/usr/bin/env python3
"""Deploy babysit-pr from this checkout: pull, restart, then update the workers.

``make deploy`` runs this in the checkout the live dashboard runs from. It
fast-forwards ``main`` from ``origin``, has the dashboard restart the watcher daemon
and then itself (as the Services dialog does), waits for the new dashboard, and asks
it to update every enabled worker it can update to the same commit (as the machine
listing's Update does: ``provision_worker.sh --user-step`` there, then a skills sync).

Everything after the pull goes through the dashboard's API, so it works from a shell
that may not signal processes or use launchctl, such as one inside Agent Safehouse.
Restarting the dashboard interrupts running cron jobs, so it refuses while one runs
unless given ``--force``.
"""

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_TIMEOUT = 90
# worker_updates.DEPLOY_TIMEOUT, with room for the dashboard to record the result.
WORKER_TIMEOUT = 660
POLL = 2


class Failed(Exception):
    """A step that failed; its message is what the person is told."""


def git(*args):
    return subprocess.run(
        ["git", "-C", str(ROOT), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def request(url, path, body=None, timeout=60):
    """The dashboard's JSON answer; a POST carries the action header it requires."""
    headers = {}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers = {"Content-Type": "application/json", "X-Babysit-Action": path.rsplit("/", 1)[1]}
    call = urllib.request.Request(url + path, data=data, headers=headers)
    try:
        with urllib.request.urlopen(call, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            said = json.load(exc).get("error")
        except ValueError:
            said = None
        raise Failed(f"{path}: HTTP {exc.code}: {said or exc.reason}") from None


def pull():
    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    if branch != "main":
        raise Failed(f"{ROOT} is on {branch}, not main")
    if git("status", "--porcelain", "--untracked-files=no"):
        raise Failed(f"{ROOT} has local changes; commit or stash them first")
    before = git("rev-parse", "HEAD")
    git("pull", "--ff-only", "--quiet", "origin", "main")
    after = git("rev-parse", "HEAD")
    if before == after:
        print(f"Already at {after[:12]}")
    else:
        print(f"Pulled {before[:12]}..{after[:12]}:")
        print(git("log", "--oneline", "--first-parent", f"{before}..{after}"))
    return after


def refuse_running_jobs(url):
    running = [
        job["name"] for job in request(url, "/api/cron").get("jobs", []) if job.get("running")
    ]
    if running:
        raise Failed(
            "Restarting the dashboard would interrupt running cron jobs: "
            f"{', '.join(running)}. Wait for them, or pass --force."
        )


def restart(url):
    daemon = request(url, "/api/service-restart", {"service": "daemon"}, timeout=120)["daemon"]
    print(f"Watcher restarted: pid {daemon.get('pid')} ({daemon.get('health')})")
    old = request(url, "/api/service-restart", {"service": "dashboard"})["dashboard"]["pid"]
    deadline = time.monotonic() + DASHBOARD_TIMEOUT
    while time.monotonic() < deadline:
        time.sleep(POLL)
        try:
            pid = request(url, "/api/service", timeout=10)["dashboard"]["pid"]
        except (OSError, Failed, KeyError, ValueError):
            continue  # Down between the two processes.
        if pid != old:
            print(f"Dashboard restarted: pid {pid}")
            return
    raise Failed(f"The dashboard did not come back within {DASHBOARD_TIMEOUT} seconds")


def update_workers(url, sha):
    workers = [
        m
        for m in request(url, "/api/machines")["machines"]
        if not m.get("local") and m.get("enabled") and m.get("updatable")
    ]
    for m in workers:
        request(url, "/api/worker-update", {"machine": m["id"]})
    failed = []
    deadline = time.monotonic() + WORKER_TIMEOUT
    pending = {m["id"]: m["label"] for m in workers}
    while pending and time.monotonic() < deadline:
        time.sleep(POLL)
        for m in request(url, "/api/machines")["machines"]:
            update = m.get("update") or {}
            if m["id"] not in pending or update.get("running") is not False:
                continue
            label = pending.pop(m["id"])
            if update.get("ok") and update.get("sha") == sha:
                print(f"{label} updated to {sha[:12]}")
            else:
                failed.append(label)
                print(f"{label} not updated: {update.get('error') or update}", file=sys.stderr)
    for label in pending.values():
        failed.append(label)
        print(f"{label} did not finish within {WORKER_TIMEOUT} seconds", file=sys.stderr)
    if failed:
        raise Failed(f"Workers not updated: {', '.join(failed)}")
    if not workers:
        print("No workers to update")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Pull, restart, then update the workers.")
    parser.add_argument("--url", default="http://127.0.0.1:8765", help="the live dashboard")
    parser.add_argument(
        "--force", action="store_true", help="restart even while cron jobs are running"
    )
    args = parser.parse_args(argv)
    url = args.url.rstrip("/")
    try:
        if not args.force:
            refuse_running_jobs(url)
        sha = pull()
        restart(url)
        update_workers(url, sha)
    except subprocess.CalledProcessError as exc:
        raise SystemExit(f"git {' '.join(exc.cmd[3:])} failed: {exc.stderr.strip()}") from None
    except OSError as exc:
        raise SystemExit(f"Cannot reach the dashboard at {url}: {exc}") from None
    except Failed as exc:
        raise SystemExit(str(exc)) from None


if __name__ == "__main__":
    main()
