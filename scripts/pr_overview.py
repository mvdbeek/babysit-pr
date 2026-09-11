"""Read-only GitHub PR discovery, cached independently of repair watches."""

import copy
import json
import os
import subprocess
import threading
import time
from pathlib import Path

POLL_SECONDS = 120
ROLES = {"author": "author", "assignee": "assignee", "reviewer": "review-involves"}
QUERY = """
query($query: String!, $cursor: String) {
  viewer { login }
  search(query: $query, type: ISSUE, first: 50, after: $cursor) {
    issueCount
    pageInfo { hasNextPage endCursor }
    nodes { ... on PullRequest {
      id number title url updatedAt state isDraft reviewDecision
      repository { nameWithOwner }
      author { login }
      statusCheckRollup { state }
    } }
  }
}
"""


def github_page(query, cursor=None):
    payload = {"query": QUERY, "variables": {"query": query, "cursor": cursor}}
    result = subprocess.run(
        ["gh", "api", "--hostname", "github.com", "graphql", "--input", "-"],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        timeout=45,
    )
    if result.returncode:
        raise ValueError(f"GitHub request failed: {result.stderr.strip()[:500]}")
    response = json.loads(result.stdout)
    if response.get("errors"):
        raise ValueError("GitHub returned incomplete PR data; retrying on the next refresh")
    return response["data"]


def collect():
    prs: dict[str, dict] = {}
    warnings = []
    login = None
    for role, qualifier in ROLES.items():
        cursor = None
        for _ in range(20):  # GitHub search exposes at most 1,000 results per query.
            data = github_page(f"is:pr is:open {qualifier}:@me sort:updated-desc", cursor)
            if login is not None and login != data["viewer"]["login"]:
                raise ValueError("GitHub account changed during refresh; retrying")
            login = data["viewer"]["login"]
            search = data["search"]
            for node in search["nodes"]:
                if not node or node.get("state") != "OPEN":
                    continue
                key = node["id"]
                roles = prs[key]["roles"] if key in prs else []
                if role not in roles:
                    roles.append(role)
                if key in prs and prs[key]["updated_at"] > node["updatedAt"]:
                    continue
                rollup = node.get("statusCheckRollup")
                prs[key] = {
                    "id": key,
                    "number": node["number"],
                    "title": node["title"],
                    "url": node["url"],
                    "repo": node["repository"]["nameWithOwner"],
                    "author": (node.get("author") or {}).get("login"),
                    "updated_at": node["updatedAt"],
                    "draft": node["isDraft"],
                    "review_decision": node.get("reviewDecision"),
                    "roles": roles,
                    "ci": rollup.get("state", "UNKNOWN") if rollup else "NONE",
                }
            page = search["pageInfo"]
            if not page["hasNextPage"]:
                if search["issueCount"] > 1000:
                    warnings.append(f"Only the most recently updated 1,000 {role} PRs are shown.")
                break
            if not page["endCursor"] or page["endCursor"] == cursor:
                raise ValueError("GitHub PR pagination did not advance")
            cursor = page["endCursor"]
        else:
            warnings.append(f"Only the most recently updated 1,000 {role} PRs are shown.")
    return {
        "login": login,
        "prs": sorted(prs.values(), key=lambda pr: pr["updated_at"], reverse=True),
        "warnings": warnings,
    }


class Overview:
    """Serve the last complete snapshot immediately; allow only one refresh at a time."""

    def __init__(self, home: Path):
        self.path = home / "pr-overview.json"
        self.lock = threading.Lock()
        self.worker: threading.Thread | None = None
        self.next_poll = 0.0
        self.value: dict = {
            "login": None,
            "prs": [],
            "warnings": [],
            "synced_at": None,
            "error": None,
        }
        try:
            saved = json.loads(self.path.read_text())
            if isinstance(saved, dict) and isinstance(saved.get("prs"), list):
                self.value.update(saved)
        except (OSError, ValueError):
            pass

    def snapshot(self):
        with self.lock:
            if time.monotonic() >= self.next_poll and not (self.worker and self.worker.is_alive()):
                self.worker = threading.Thread(target=self.refresh, daemon=True)
                self.worker.start()
            return {
                **copy.deepcopy(self.value),
                "refreshing": bool(self.worker and self.worker.is_alive()),
            }

    def refresh(self):
        try:
            value = {**collect(), "synced_at": time.time(), "error": None}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(".tmp")
            # This cache can contain private repository metadata, just like the watch queue.
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as stream:
                json.dump(value, stream)
            temp.replace(self.path)
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
            with self.lock:
                self.value = {**self.value, "error": f"Cannot sync GitHub PRs: {exc}"}
        else:
            with self.lock:
                self.value = value
        finally:
            with self.lock:
                self.next_poll = time.monotonic() + POLL_SECONDS
