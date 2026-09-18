"""Bounded CI metadata shared by on-demand details and background log collection."""

import copy
import hashlib
import json
import os
import subprocess
import threading
import time
from collections import OrderedDict
from functools import partial
from pathlib import Path

import github_cli

TTL = 300
ERROR_TTL = 60
MAX_ENTRIES = 128
MAX_WORKERS = 2
MAX_PAGES = 3
FAILURES = {"FAILURE", "ERROR", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE", "STALE"}
CHECKS_QUERY = """
query($id: ID!, $cursor: String) {
  node(id: $id) { ... on PullRequest {
    headRefOid
    statusCheckRollup {
      state commit { oid }
      contexts(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          __typename
          ... on CheckRun {
            id databaseId name status conclusion detailsUrl repository { nameWithOwner }
            checkSuite { workflowRun { databaseId workflow { name } } }
          }
          ... on StatusContext { id context state description targetUrl }
        }
      }
    }
  } }
}
"""
FAILURE_QUERY = """
query($id: ID!) {
  node(id: $id) { ... on CheckRun {
    id title summary text checkSuite { commit { oid } }
    annotations(first: 20) {
      totalCount
      nodes { annotationLevel title message path location { start { line } } }
    }
  } }
}
"""


def graphql(query, variables):
    result = github_cli.run(
        ["gh", "api", "--hostname", "github.com", "graphql", "--input", "-"],
        input=json.dumps({"query": query, "variables": variables}),
        text=True,
        timeout=45,
    )
    if result.returncode:
        raise ValueError(f"GitHub CI request failed: {result.stderr.strip()[:500]}")
    value = json.loads(result.stdout)
    if value.get("errors"):
        raise ValueError("GitHub could not return complete CI details; try again later")
    return value["data"]["node"]


def bucket(state):
    if state in FAILURES:
        return "fail"
    if state == "SUCCESS":
        return "pass"
    if state in {"NEUTRAL", "SKIPPED"}:
        return "skipping"
    if state == "CANCELLED":
        return "cancel"
    return "pending"


def collect_checks(pr):
    checks = {}
    cursor = None
    state = "NONE"
    commit = pr["head_sha"]
    truncated = False
    for _ in range(MAX_PAGES):
        node = graphql(CHECKS_QUERY, {"id": pr["id"], "cursor": cursor})
        if not node or node["headRefOid"] != pr["head_sha"]:
            raise ValueError(
                "The PR head changed. Wait for the PR overview to sync, then reopen CI details."
            )
        rollup = node.get("statusCheckRollup")
        if not rollup:
            break
        current_commit = (rollup.get("commit") or {}).get("oid") or pr["head_sha"]
        if cursor and current_commit != commit:
            raise ValueError("CI changed during pagination; reopen CI details later")
        commit = current_commit
        state = rollup["state"]
        connection = rollup["contexts"]
        for check in connection["nodes"]:
            if not check:
                continue
            is_run = check["__typename"] == "CheckRun"
            conclusion = (
                (
                    (check.get("conclusion") or "UNKNOWN")
                    if check.get("status") == "COMPLETED"
                    else check.get("status", "UNKNOWN")
                )
                if is_run
                else check["state"]
            )
            workflow = (
                ((check.get("checkSuite") or {}).get("workflowRun") or {}).get("workflow") or {}
            ).get("name")
            checks[check["id"]] = {
                "id": check["id"],
                "name": check["name"] if is_run else check["context"],
                "state": conclusion,
                "bucket": bucket(conclusion),
                "url": check.get("detailsUrl") if is_run else check.get("targetUrl"),
                "workflow": workflow,
                "database_id": check.get("databaseId"),
                "repository": (check.get("repository") or {}).get("nameWithOwner"),
                "run_id": ((check.get("checkSuite") or {}).get("workflowRun") or {}).get(
                    "databaseId"
                ),
                "description": (check.get("description") or "")[:2000],
                "has_details": is_run and conclusion in FAILURES,
            }
        page = connection["pageInfo"]
        truncated = page["hasNextPage"]
        if not truncated:
            break
        if not page["endCursor"] or page["endCursor"] == cursor:
            raise ValueError("CI pagination did not advance")
        cursor = page["endCursor"]
    order = {"fail": 0, "pending": 1, "cancel": 2, "pass": 3, "skipping": 4}
    return {
        "sha": commit,
        "head_sha": pr["head_sha"],
        "state": state,
        "truncated": truncated,
        "checks": sorted(checks.values(), key=lambda c: (order[c["bucket"]], c["name"].lower())),
    }


def collect_failure(check_id, sha):
    node = graphql(FAILURE_QUERY, {"id": check_id})
    if not node or node["id"] != check_id or node["checkSuite"]["commit"]["oid"] != sha:
        raise ValueError("This check no longer belongs to the selected commit")
    connection = node.get("annotations") or {"nodes": [], "totalCount": 0}
    annotations = [
        {
            "title": (a.get("title") or "")[:500],
            "message": (a.get("message") or "")[:4000],
            "path": a.get("path"),
            "line": a["location"]["start"]["line"],
            "level": a["annotationLevel"],
        }
        for a in connection["nodes"]
        if a
    ]
    summary = node.get("summary") or ""
    text = node.get("text") or ""
    return {
        "title": (node.get("title") or "")[:500],
        "summary": summary[:12000],
        "text": text[:12000],
        "annotations": annotations,
        "annotation_count": connection["totalCount"],
        "truncated": connection["totalCount"] > len(annotations)
        or len(summary) > 12000
        or len(text) > 12000
        or any(len(a.get("message") or "") > 4000 for a in connection["nodes"] if a),
    }


class CiDetails:
    """One shared cache, two workers maximum; only explicit detail requests schedule work."""

    def __init__(self, home: Path, overview):
        self.path = home / "pr-ci-cache.json"
        self.overview = overview
        self.lock = threading.Lock()
        self.entries: OrderedDict[str, dict] = OrderedDict()
        self.workers: dict[str, threading.Thread] = {}
        try:
            saved = json.loads(self.path.read_text())
            self.entries.update(list(saved.items())[-MAX_ENTRIES:])
        except (OSError, ValueError, AttributeError):
            pass

    @staticmethod
    def key(login, pr, check_id=None):
        return hashlib.sha256(
            json.dumps([2, login, pr["id"], pr["head_sha"], check_id]).encode()
        ).hexdigest()

    def snapshot(self, pr_id, check_id=None):
        overview = self.overview.snapshot()
        pr = next((p for p in overview["prs"] if p["id"] == pr_id), None)
        if not pr or not pr.get("head_sha"):
            raise ValueError("Unknown PR or missing head commit; refresh the PR overview")
        key = self.key(overview.get("login"), pr, check_id)
        with self.lock:
            if check_id:
                parent = self.entries.get(self.key(overview.get("login"), pr), {}).get("value")
                if not parent or not any(
                    c["id"] == check_id and c["has_details"] for c in parent["checks"]
                ):
                    raise ValueError(
                        "Open this PR's CI checks before requesting a reported failure"
                    )
                loader = partial(collect_failure, check_id, parent["sha"])
            else:
                loader = partial(collect_checks, copy.deepcopy(pr))
            entry = self.entries.get(
                key, {"value": None, "error": None, "synced_at": None, "expires_at": 0}
            )
            expired = time.time() >= entry["expires_at"]
            busy = False
            if expired and key not in self.workers:
                if len(self.workers) >= MAX_WORKERS:
                    busy = True
                else:
                    worker = threading.Thread(target=self.refresh, args=(key, loader), daemon=True)
                    self.workers[key] = worker
                    worker.start()
            if key in self.entries:
                self.entries.move_to_end(key)
            return {
                **copy.deepcopy(entry),
                "refreshing": key in self.workers or busy,
                "busy": busy,
                "stale": expired and entry["value"] is not None,
            }

    def refresh(self, key, loader):
        try:
            value = loader()
            entry = {
                "value": value,
                "error": None,
                "synced_at": time.time(),
                "expires_at": time.time() + TTL,
            }
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
            with self.lock:
                old = self.entries.get(key, {})
            entry = {
                "value": old.get("value"),
                "synced_at": old.get("synced_at"),
                "error": str(exc),
                "expires_at": time.time() + ERROR_TTL,
            }
        with self.lock:
            try:
                self.entries[key] = entry
                self.entries.move_to_end(key)
                while len(self.entries) > MAX_ENTRIES:
                    self.entries.popitem(last=False)
                self.path.parent.mkdir(parents=True, exist_ok=True)
                temp = self.path.with_suffix(".tmp")
                fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w") as stream:
                    json.dump(self.entries, stream)
                temp.replace(self.path)
            except OSError:
                entry["error"] = "CI details loaded, but the local cache could not be saved"
            finally:
                self.workers.pop(key, None)
