"""Read-only GitHub PR discovery, cached independently of repair watches.

The search, merge, pagination and cache machinery is parametrised by a `Kind` so the
issue overview (`issue_overview.py`) shares it without copying it.
"""

import json
import os
import re
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import github_cli
from latest_activity import (
    activity_fragment,
    latest_activity,
    notification_activity,
    timeline_activity,
)

POLL_SECONDS = 300
# GitHub search exposes at most this many results per query.
SEARCH_LIMIT = 1000
# Keep post-closure discussion without scanning years of finished PRs on every refresh.
CLOSED_MENTION_DAYS = 30
# Nodes per search page. GitHub stops a GraphQL query after about 10 seconds and answers
# 502 or 504; 50 PRs with their threads and activity took that long, 25 about 6 seconds.
PAGE_SIZE = 25
MIN_PAGE_SIZE = 5
TIMED_OUT = re.compile(r"\bHTTP 50[24]\b")
ROLES = {
    "author": "author",
    "assignee": "assignee",
    "reviewer": "review-involves",
    # GitHub resolves team membership for this search; `reviewRequests` only names the team.
    "requested": "review-requested",
    "mentioned": "mentions",
}
SETTINGS = "overview-config.json"
PR_FRAGMENT = (
    """... on PullRequest {
      id number title url createdAt updatedAt closedAt state isDraft reviewDecision
      repository { nameWithOwner }
      headRepository { nameWithOwner }
      headRefName headRefOid
      author { login }
      statusCheckRollup { state }
      reviewThreads(last: 100) { nodes { isResolved } }
      reviewRequests(first: 20) {
        nodes { requestedReviewer { ... on User { login } ... on Team { slug } } }
      }
    """
    + activity_fragment(pull_request=True)
    + "}"
)
QUERY = """
query($query: String!, $cursor: String, $first: Int!) {
  viewer { login }
  search(query: $query, type: ISSUE, first: $first, after: $cursor) {
    issueCount
    pageInfo { hasNextPage endCursor }
    nodes { %s }
  }
}
"""


# What someone says, as opposed to what they do: a branch deletion is not a reply.
DISCUSSION = {"IssueComment", "PullRequestReview", "ContentEdited"}


def discussed_after_close(node, login):
    """Whether another person commented or reviewed after this PR closed or merged.

    Deleting a head branch or relabelling bumps `updatedAt` on every PR it touches,
    which brings back years of finished PRs that merely mention the user. Bots
    comment on merged PRs routinely, so they do not count either.
    """
    closed = node.get("closedAt")
    if not closed:
        return False
    for event in (node.get("timelineItems") or {}).get("nodes") or []:
        found = timeline_activity(event) if event else None
        if not found or found["type"] not in DISCUSSION or found["at"] <= closed:
            continue
        actor = found["actor"] or ""
        if actor and actor != login and not actor.endswith("[bot]"):
            return True
    return False


def pr_record(node, roles):
    rollup = node.get("statusCheckRollup")
    return {
        "id": node["id"],
        "number": node["number"],
        "title": node["title"],
        "url": node["url"],
        "repo": node["repository"]["nameWithOwner"],
        "author": (node.get("author") or {}).get("login"),
        "state": node["state"],
        "head_repo": (node.get("headRepository") or {}).get("nameWithOwner"),
        "head_branch": node.get("headRefName"),
        "head_sha": node.get("headRefOid"),
        "updated_at": node["updatedAt"],
        "latest_activity": latest_activity(node),
        "notification_activity": notification_activity(node),
        "opened_at": node["createdAt"],
        "draft": node["isDraft"],
        "review_decision": node.get("reviewDecision"),
        # Outdated threads still count: GitHub keeps them open until someone resolves them.
        "unresolved_threads": sum(
            not thread["isResolved"]
            for thread in (node.get("reviewThreads") or {}).get("nodes") or []
        ),
        # Who still has to review: user logins, and `team:<slug>` for team requests.
        "review_requests": [
            f"team:{who['slug']}" if "slug" in who else who["login"]
            for request in (node.get("reviewRequests") or {}).get("nodes") or []
            for who in [request.get("requestedReviewer") or {}]
            if who.get("login") or who.get("slug")
        ],
        "roles": roles,
        "ci": rollup.get("state", "UNKNOWN") if rollup else "NONE",
    }


@dataclass(frozen=True)
class Kind:
    """One searchable GitHub item type: how to find it, map it, and where to cache it."""

    key: str  # Snapshot/API key holding the list, e.g. "prs".
    label: str  # Human label used in warnings and errors, e.g. "PR".
    search: str  # Search prefix, e.g. "is:pr is:open".
    roles: dict[str, str]  # Displayed role -> search qualifier.
    fragment: str  # GraphQL inline fragment for one search node.
    cache: str  # Cache file name under the watcher state directory.
    record: Callable[[dict, list[str]], dict] = field(repr=False)
    # Discussions can still need a response after a PR has merged or closed.
    mention_search: str | None = None


PRS = Kind(
    key="prs",
    label="PR",
    search="is:pr is:open",
    roles=ROLES,
    fragment=PR_FRAGMENT,
    cache="pr-overview.json",
    record=pr_record,
    mention_search="is:pr is:closed",
)


class SearchTimeout(ValueError):
    """GitHub gave up on a search page; a smaller one may fit inside its time limit."""


def github_page(query, cursor=None, fragment=PR_FRAGMENT, first=PAGE_SIZE):
    payload = {
        "query": QUERY % fragment,
        "variables": {"query": query, "cursor": cursor, "first": first},
    }
    result = github_cli.run(
        ["gh", "api", "--hostname", "github.com", "graphql", "--input", "-"],
        input=json.dumps(payload),
        text=True,
        timeout=45,
    )
    if result.returncode:
        message = f"GitHub request failed: {result.stderr.strip()[:500]}"
        raise (SearchTimeout if TIMED_OUT.search(result.stderr) else ValueError)(message)
    response = json.loads(result.stdout)
    if response.get("errors"):
        raise ValueError("GitHub returned incomplete data; retrying on the next refresh")
    return response["data"]


def include_mentions(home: Path):
    """Whether discovery searches @mentions: on unless the settings file turns it off."""
    path = home / SETTINGS
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        return True
    except (OSError, ValueError) as exc:
        raise ValueError(f"Cannot read {path}: {exc}") from exc
    value = raw.get("mentions", True) if isinstance(raw, dict) else None
    if not isinstance(value, bool):
        raise ValueError(f'{path} must be a JSON object with "mentions": true or false')
    return value


def collect(kind: Kind = PRS, mentions=True):
    items: dict[str, dict] = {}
    warnings = []
    login = None
    searched = {
        role: qualifier for role, qualifier in kind.roles.items() if mentions or role != "mentioned"
    }
    searches = [
        (role, f"{kind.search} {qualifier}:@me", {"OPEN"}) for role, qualifier in searched.items()
    ]
    if mentions and kind.mention_search:
        since = (datetime.now(UTC) - timedelta(days=CLOSED_MENTION_DAYS)).date().isoformat()
        searches.append(
            (
                "mentioned",
                f"{kind.mention_search} mentions:@me updated:>={since}",
                {"CLOSED", "MERGED"},
            )
        )
    for role, query, states in searches:
        cursor, size, listed = None, PAGE_SIZE, 0
        while listed < SEARCH_LIMIT:
            try:
                data = github_page(
                    f"{query} sort:updated-desc",
                    cursor,
                    kind.fragment,
                    size,
                )
            except SearchTimeout:
                # A slow moment at GitHub: the same page again, half as long.
                if size <= MIN_PAGE_SIZE:
                    raise
                size = max(MIN_PAGE_SIZE, size // 2)
                continue
            listed += size
            if login is not None and login != data["viewer"]["login"]:
                raise ValueError("GitHub account changed during refresh; retrying")
            login = data["viewer"]["login"]
            search = data["search"]
            for node in search["nodes"]:
                if not node or node.get("state") not in states:
                    continue
                key = node["id"]
                # Only discussion after closure makes a finished PR a mention again; the
                # search still refreshes the state of a PR that merged during this refresh.
                counts = "OPEN" in states or discussed_after_close(node, login)
                if not counts and key not in items:
                    continue
                roles = items[key]["roles"] if key in items else []
                if counts and role not in roles:
                    roles.append(role)
                if key in items and items[key]["updated_at"] > node["updatedAt"]:
                    continue
                items[key] = kind.record(node, roles)
            page = search["pageInfo"]
            if not page["hasNextPage"]:
                if search["issueCount"] > SEARCH_LIMIT:
                    warnings.append(
                        f"Only the most recently updated 1,000 {role} {kind.label}s are shown."
                    )
                break
            if not page["endCursor"] or page["endCursor"] == cursor:
                raise ValueError(f"GitHub {kind.label} pagination did not advance")
            cursor = page["endCursor"]
        else:
            warnings.append(f"Only the most recently updated 1,000 {role} {kind.label}s are shown.")
    return {
        "login": login,
        kind.key: sorted(items.values(), key=lambda item: item["updated_at"], reverse=True),
        "roles": list(searched),
        "warnings": warnings,
    }


class Overview:
    """Serve the last complete snapshot immediately; allow only one refresh at a time."""

    kind: Kind = PRS

    def __init__(self, home: Path):
        self.home = home
        self.path = home / self.kind.cache
        self.lock = threading.Lock()
        self.worker: threading.Thread | None = None
        self.next_poll = 0.0
        # Set under the lock until the refresh's final bookkeeping, unlike thread liveness.
        self.refreshing = False
        self.pending = False  # A wake arrived during the running refresh.
        self.value: dict = {
            "login": None,
            self.kind.key: [],
            "warnings": [],
            "synced_at": None,
            "error": None,
        }
        try:
            saved = json.loads(self.path.read_text())
            if isinstance(saved, dict) and isinstance(saved.get(self.kind.key), list):
                self.value.update(saved)
        except (OSError, ValueError):
            pass

    def fetch(self):
        # The module-level collector is looked up at call time so tests can replace it.
        return collect(mentions=include_mentions(self.home))

    def _start(self):
        self.refreshing = True
        self.worker = threading.Thread(target=self.refresh, daemon=True)
        try:
            self.worker.start()
        except RuntimeError:
            self.refreshing = False
            raise

    def snapshot(self):
        with self.lock:
            if time.monotonic() >= self.next_poll and not self.refreshing:
                self._start()
            # Shallow: refresh() replaces the value whole and nothing changes it in place,
            # so callers share its items and must copy before mutating them.
            return {
                **self.value,
                "refreshing": self.refreshing,
            }

    def wake(self):
        """Refresh now; a refresh already running is followed by one more."""
        with self.lock:
            if self.refreshing:
                self.pending = True
            else:
                self._start()

    def refresh(self):
        try:
            value = {**self.fetch(), "synced_at": time.time(), "error": None}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(".tmp")
            # This cache can contain private repository metadata, just like the watch queue.
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as stream:
                json.dump(value, stream)
            temp.replace(self.path)
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
            with self.lock:
                self.value = {
                    **self.value,
                    "error": f"Cannot sync GitHub {self.kind.label}s: {exc}",
                }
        else:
            with self.lock:
                self.value = value
        finally:
            with self.lock:
                self.next_poll = time.monotonic() + POLL_SECONDS
                self.refreshing = False
                # A wake during this refresh may describe a change its search already missed.
                if self.pending:
                    self.pending = False
                    self._start()
