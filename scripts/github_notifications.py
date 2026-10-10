"""Bring the PR and issue overview refreshes forward when GitHub reports new activity.

GitHub offers no push channel for everything involving one account, so this polls the
notifications API at the interval GitHub asks for (`X-Poll-Interval`, normally 60
seconds). Each request carries `If-Modified-Since`, so a quiet minute costs one 304.
Only notification threads updated since the last check wake an overview; marking a
thread read changes the list but not its `updated_at`, so it wakes nothing.

Notifications cover the threads the account participates in: authored, assigned,
review requested, mentioned and commented on. CI results and pushes to other people's
PRs do not notify, so the overviews keep their own scheduled refresh as a backstop.
"""

import json
import logging
import subprocess
import threading

import github_cli

LOG = logging.getLogger(__name__)
ENDPOINT = "/notifications?all=true&participating=true&per_page=50"
MIN_INTERVAL = 60
RETRY_SECONDS = 300
# Notification subject type -> overview it can change. CheckSuite is CI on your own runs.
SUBJECTS = {"PullRequest": "prs", "CheckSuite": "prs", "Issue": "issues"}


def request(last_modified=None):
    """One conditional notifications request: (status, lower-cased headers, threads)."""
    args = ["gh", "api", "--hostname", "github.com", "--include", ENDPOINT]
    if last_modified:
        args += ["--header", f"If-Modified-Since: {last_modified}"]
    result = github_cli.run(args, text=True, timeout=45)
    head, _, body = result.stdout.replace("\r\n", "\n").partition("\n\n")
    lines = head.splitlines()
    status = lines[0].split()[1] if lines and lines[0].startswith("HTTP/") else ""
    # gh exits non-zero on a 304 but still prints its status line and headers.
    if not status.isdigit() or (result.returncode and status != "304"):
        raise ValueError(f"GitHub notifications request failed: {result.stderr.strip()[:500]}")
    headers = {}
    for line in lines[1:]:
        name, _, value = line.partition(":")
        headers[name.strip().lower()] = value.strip()
    threads = json.loads(body) if status == "200" else []
    if not isinstance(threads, list):
        raise ValueError("GitHub notifications response was not a list")
    return int(status), headers, threads


class NotificationWatch:
    """Wake each overview whose kind of thread GitHub reports as new or updated.

    GitHub search indexes a new PR, review request or mention a little after notifying
    about it, so every wake is repeated on the following check to pick those up.
    """

    def __init__(self, overviews):
        self.overviews = overviews  # Overview key ("prs", "issues") -> object with wake().
        self.last_modified = None
        self.threads: dict[str, str] | None = None  # Thread id -> updated_at; None before.
        self.recheck: set[str] = set()
        self.stopping = threading.Event()
        self.worker: threading.Thread | None = None

    def changed(self, threads):
        """Overview keys with a thread that is new or updated since the previous answer."""
        current = {
            str(thread["id"]): (thread["updated_at"], (thread.get("subject") or {}).get("type"))
            for thread in threads
            if isinstance(thread, dict)
            and thread.get("id") is not None
            and isinstance(thread.get("updated_at"), str)
        }
        previous, self.threads = self.threads, {key: value[0] for key, value in current.items()}
        # The first answer is only a baseline: each overview refreshes on its own at startup.
        if previous is None:
            return set()
        # Only one page is read: an old thread that resurfaces when a newer one is deleted
        # is not news, so unknown threads must be at least as new as the oldest one kept.
        oldest = min(previous.values(), default="")
        return {
            SUBJECTS[kind]
            for key, (updated, kind) in current.items()
            if kind in SUBJECTS
            and previous.get(key) != updated
            and (key in previous or updated >= oldest)
        }

    def tick(self):
        """Check once and return the number of seconds to wait before the next check."""
        try:
            status, headers, threads = request(self.last_modified)
        except (OSError, ValueError, subprocess.SubprocessError):
            # The message can name private repositories; the overviews still refresh on schedule.
            LOG.warning("GitHub notification check failed; retrying in %s seconds", RETRY_SECONDS)
            return RETRY_SECONDS
        try:
            interval = max(MIN_INTERVAL, int(headers.get("x-poll-interval", MIN_INTERVAL)))
        except ValueError:
            interval = MIN_INTERVAL
        keys = set(self.recheck)
        self.recheck = set()
        if status == 200:
            self.last_modified = headers.get("last-modified")
            self.recheck = self.changed(threads)
            keys |= self.recheck
        for key in sorted(keys & self.overviews.keys()):
            self.overviews[key].wake()
        return interval

    def start(self):
        def run():
            delay = 0
            while not self.stopping.wait(delay):
                try:
                    delay = self.tick()
                except Exception:
                    LOG.error("GitHub notification check crashed; retrying")
                    delay = RETRY_SECONDS

        self.worker = threading.Thread(target=run, daemon=True, name="github-notifications")
        self.worker.start()

    def close(self):
        self.stopping.set()
        if self.worker:
            # A daemon thread: never hold up shutdown for an in-flight request.
            self.worker.join(timeout=5)
