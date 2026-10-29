"""Read-only dashboard tools for the voice assistant, served to Claude over MCP (stdio).

Every tool reads what the dashboard already serves its own pages, from the local dashboard
named by BABYSIT_DASHBOARD_URL, and answers compact JSON. Nothing here changes anything:
what the assistant proposes to do goes back to the browser, which asks the user first.
GitHub text, CI logs and agent transcripts are untrusted; the system prompt says so.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

PROTOCOL = "2025-06-18"
# Detail endpoints answer at once and fetch in the background; a tool waits this long.
WAIT_SECONDS = 25
POLL_SECONDS = 1.0
MAX_ANSWER = 30000
MAX_ENTRY = 1500
TRANSCRIPT_ENTRIES = 30
DIFF_LINES = 400
LOG_TAIL = 8000
WORKSPACE_ROWS = 25

TARGET = {
    "workspace": {"type": "string", "description": "The item's workspace_id, from needs_you"},
    "watch": {"type": "string", "description": "The item's watch id, from needs_you"},
}
TOOLS: list[dict] = [
    {
        "name": "needs_you",
        "description": "Everything waiting on the user (the Needs you list), in its order: "
        "each item's kind, repository, number, title, URL, group, the reasons it waits, its "
        "age, and the workspace_id or watch id of an agent working on it.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "pull_request",
        "description": "A pull request's state, review decision, open threads, requested "
        "reviewers and CI checks (failing and pending ones by name).",
        "inputSchema": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
    },
    {
        "name": "ci_failure",
        "description": "Why one CI check of a pull request failed: GitHub's annotations and "
        "the end of the job log. Call pull_request first for the check names.",
        "inputSchema": {
            "type": "object",
            "properties": {"url": {"type": "string"}, "check": {"type": "string"}},
            "required": ["url", "check"],
        },
    },
    {
        "name": "reviews",
        "description": "A pull request's submitted reviews and its unresolved review threads.",
        "inputSchema": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
    },
    {
        "name": "issue",
        "description": "An issue's labels, assignees, comment count and linked pull requests.",
        "inputSchema": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
    },
    {
        "name": "workspaces",
        "description": "Every checkout on every machine, whatever the Workspaces page's "
        "filters show, matched by `query` (words or a number from its name, branch, "
        "repository or linked pull requests and issues): its workspace_id when a workspace "
        "is open, its agents and their status, its cleanup status and what blocks cleanup. "
        "One without a workspace_id has no agent to message or open.",
        "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}},
    },
    {
        "name": "agent_transcript",
        "description": "The latest messages of the agent working on an item: what it was "
        "asked, what it said, and any question it waits on. Give a workspace or a watch.",
        "inputSchema": {"type": "object", "properties": TARGET},
    },
    {
        "name": "changes",
        "description": "What an agent's checkout changed against its base branch: commits, "
        "files, and the start of each file's patch. Give a workspace or a watch.",
        "inputSchema": {"type": "object", "properties": TARGET},
    },
]


class ToolError(Exception):
    """A tool's answer that explains why it has none."""


def fetch(path, **query):
    base = os.environ["BABYSIT_DASHBOARD_URL"].rstrip("/")
    url = f"{base}{path}"
    if query:
        url += "?" + urllib.parse.urlencode(query)
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            detail = json.load(exc).get("error")
        except (ValueError, AttributeError):
            detail = None
        raise ToolError(detail or f"The dashboard answered HTTP {exc.code}") from None
    except (OSError, ValueError) as exc:
        raise ToolError(f"Cannot reach the dashboard: {exc}") from None


def settled(path, ready, **query):
    """Poll a background-fetched detail until ``ready(value)``, its error, or the wait ends."""
    deadline = time.monotonic() + WAIT_SECONDS
    while True:
        value = fetch(path, **query)
        if ready(value):
            return value
        if not value.get("refreshing") and value.get("error"):
            raise ToolError(value["error"])
        if time.monotonic() >= deadline:
            raise ToolError("Still loading from GitHub; try again in a little while")
        time.sleep(POLL_SECONDS)


def clip(text, limit=MAX_ENTRY):
    text = str(text or "")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def age(seconds):
    if not seconds:
        return None
    elapsed = max(0, time.time() - seconds)
    for size, unit in ((86400, "d"), (3600, "h"), (60, "m")):
        if elapsed >= size:
            return f"{int(elapsed // size)}{unit} ago"
    return "just now"


def same(url, other):
    return str(url).rstrip("/").lower() == str(other or "").rstrip("/").lower()


def needs_you(_args):
    feed = fetch("/api/attention")
    labels = {group["key"]: group["label"] for group in feed.get("groups") or []}
    items = []
    for item in feed.get("items") or []:
        items.append(
            {
                "kind": item.get("kind"),
                "repo": item.get("repo"),
                "number": item.get("number"),
                "title": item.get("title"),
                "url": item.get("url"),
                "key": item.get("key"),
                "group": labels.get(item.get("group"), item.get("group")),
                "reasons": [reason.get("text") for reason in item.get("reasons") or []],
                "since": age(item.get("since")),
                "page": item.get("page"),
                "agent": (item.get("agent") or {}).get("status"),
                "watch": (item.get("watch") or {}).get("id"),
                "watch_status": (item.get("watch") or {}).get("status"),
                "workspace_id": item.get("workspace_id"),
            }
        )
    failed = [name for name, source in (feed.get("sources") or {}).items() if source.get("error")]
    return {
        "items": items,
        "set_aside": len(feed.get("waiting") or []),
        "sources_unavailable": failed,
    }


def find_pr(url):
    prs = fetch("/api/prs").get("prs") or []
    pr = next((p for p in prs if same(p.get("url"), url)), None)
    if not pr:
        raise ToolError("That pull request is not in the dashboard's PR overview")
    return pr


def checks(pr):
    value = settled("/api/pr-ci", lambda v: v.get("value") is not None, id=pr["id"])["value"]
    return value


def pull_request(args):
    pr = find_pr(args["url"])
    fields = (
        "repo",
        "number",
        "title",
        "url",
        "author",
        "state",
        "draft",
        "head_branch",
        "review_decision",
        "unresolved_threads",
        "review_requests",
        "roles",
        "updated_at",
    )
    answer = {field: pr.get(field) for field in fields}
    answer["ci"] = pr.get("ci")
    try:
        value = checks(pr)
    except ToolError as exc:
        answer["checks_error"] = str(exc)
        return answer
    counts: dict[str, int] = {}
    for check in value["checks"]:
        counts[check["bucket"]] = counts.get(check["bucket"], 0) + 1
    answer["check_counts"] = counts
    answer["not_passing"] = [
        {"name": c["name"], "state": c["state"], "workflow": c.get("workflow")}
        for c in value["checks"]
        if c["bucket"] in {"fail", "pending", "cancel"}
    ][:40]
    return answer


def ci_failure(args):
    pr = find_pr(args["url"])
    wanted = args["check"].strip().lower()
    listed = checks(pr)["checks"]
    check = next((c for c in listed if c["name"].lower() == wanted), None) or next(
        (c for c in listed if wanted in c["name"].lower()), None
    )
    if not check:
        raise ToolError("No check of that name; call pull_request for the names")
    answer = {"check": check["name"], "state": check["state"], "workflow": check.get("workflow")}
    if check.get("description"):
        answer["description"] = clip(check["description"])
    if not check.get("has_details"):
        answer["note"] = "No failure details: it did not fail, or it is not a GitHub check run."
        return answer
    try:
        failure = settled(
            "/api/pr-ci", lambda v: v.get("value") is not None, id=pr["id"], check=check["id"]
        )["value"]
        answer["annotations"] = [
            {
                "path": a.get("path"),
                "line": a.get("line"),
                "title": a.get("title"),
                "message": clip(a.get("message")),
            }
            for a in failure.get("annotations") or []
        ][:20]
        answer["summary"] = clip(failure.get("summary"), 3000)
    except ToolError as exc:
        answer["annotations_error"] = str(exc)
    try:
        log = settled(
            "/api/pr-ci-log",
            lambda v: v.get("state") not in {"queued", "downloading"} or not v.get("refreshing"),
            id=pr["id"],
            check=check["id"],
        )
        if log.get("state") == "ready":
            last = fetch("/api/pr-ci-log", id=pr["id"], check=check["id"], page=1)
            if last.get("pages", 1) > 1:
                last = fetch("/api/pr-ci-log", id=pr["id"], check=check["id"], page=last["pages"])
            answer["log_tail"] = last["text"][-LOG_TAIL:]
        else:
            answer["log"] = log.get("message")
    except ToolError as exc:
        answer["log_error"] = str(exc)
    return answer


def reviews(args):
    pr = find_pr(args["url"])
    value = settled("/api/pr-reviews", lambda v: v.get("value") is not None, id=pr["id"])["value"]
    return {
        "review_decision": pr.get("review_decision"),
        "reviews": [
            {"author": r.get("author"), "state": r.get("state"), "body": clip(r.get("body"))}
            for r in value.get("reviews") or []
        ],
        "unresolved_threads": [
            {
                "path": t.get("path"),
                "line": t.get("line"),
                "outdated": t.get("outdated"),
                "comments": [
                    {"author": c.get("author"), "body": clip(c.get("body"), 800)}
                    for c in t.get("comments") or []
                ],
            }
            for t in value.get("threads") or []
        ],
    }


def issue(args):
    issues = fetch("/api/issues").get("issues") or []
    found = next((i for i in issues if same(i.get("url"), args["url"])), None)
    if not found:
        raise ToolError("That issue is not in the dashboard's issue overview")
    return {
        "repo": found.get("repo"),
        "number": found.get("number"),
        "title": found.get("title"),
        "author": found.get("author"),
        "assignees": found.get("assignees"),
        "labels": [label.get("name") for label in found.get("labels") or []],
        "comments": found.get("comments"),
        "linked_prs": found.get("linked_prs"),
        "updated_at": found.get("updated_at"),
    }


def workspaces(args):
    value = fetch("/api/workspace-overview")
    if not value.get("enabled"):
        raise ToolError("The Workspaces page is off, so checkouts are not listed")
    rows = value.get("workspaces") or []
    if not rows and (value.get("loading") or value.get("error")):
        raise ToolError(value.get("error") or "Still finding workspaces; try again shortly")
    words = str(args.get("query") or "").lower().split()

    def text(row):
        links = row.get("links") or []
        return " ".join(
            str(part)
            for part in (
                row.get("name"),
                row.get("label"),
                row.get("branch"),
                row.get("repo"),
                *(link.get("number") for link in links),
                *(link.get("title") for link in links),
            )
            if part
        ).lower()

    matched = [row for row in rows if all(word in text(row) for word in words)]
    # Open workspaces first: those are the agents the user can reach.
    matched.sort(key=lambda row: (not row.get("workspace_ids"), -(row.get("updated_at") or 0)))
    return {
        "matched": len(matched),
        "of": len(rows),
        "workspaces": [
            {
                "name": row.get("label") or row.get("name"),
                "repo": row.get("repo"),
                "branch": row.get("branch"),
                "machine": row.get("machine_label"),
                "workspace_id": (row.get("workspace_ids") or [None])[0],
                "agents": [
                    {"agent": agent.get("agent"), "status": agent.get("status")}
                    for agent in row.get("agents") or []
                ],
                "status": row.get("status"),
                "blockers": [blocker.get("text") for blocker in row.get("blockers") or []],
                "links": [
                    {
                        "kind": link.get("kind"),
                        "repo": link.get("repo"),
                        "number": link.get("number"),
                        "title": clip(link.get("title"), 200),
                        "state": link.get("state"),
                    }
                    for link in row.get("links") or []
                ],
                "updated": age(row.get("updated_at")),
            }
            for row in matched[:WORKSPACE_ROWS]
        ],
    }


def target(args):
    named = {key: args[key] for key in ("workspace", "watch") if args.get(key)}
    if len(named) != 1:
        raise ToolError("Give either a workspace or a watch")
    return named


def agent_transcript(args):
    value = fetch("/api/workspace-transcript", **target(args))
    if value.get("error"):
        raise ToolError(value["error"])
    session = value.get("session") or {}
    entries = value.get("entries") or []
    return {
        "agent": session.get("agent"),
        "entries": [
            {
                "role": entry.get("role"),
                "kind": entry.get("kind"),
                "text": clip(entry.get("text")),
            }
            for entry in entries[-TRANSCRIPT_ENTRIES:]
        ],
        "earlier_entries": max(0, len(entries) - TRANSCRIPT_ENTRIES),
    }


def changes(args):
    value = fetch("/api/workspace-diff", **target(args))
    if value.get("error"):
        raise ToolError(value["error"])
    budget = DIFF_LINES
    files = []
    for entry in value.get("files") or []:
        lines = (entry.get("lines") or [])[: max(0, budget)]
        budget -= len(lines)
        files.append(
            {
                "path": entry.get("path"),
                "status": entry.get("status"),
                "added": entry.get("added"),
                "removed": entry.get("removed"),
                "patch": "\n".join(clip(line, 300) for line in lines),
            }
        )
    return {
        "base": value.get("base"),
        "commits": [c.get("subject") for c in value.get("commits") or []][:30],
        "added": value.get("added"),
        "removed": value.get("removed"),
        "files": files,
        "untracked": value.get("untracked"),
    }


HANDLERS = {
    "needs_you": needs_you,
    "pull_request": pull_request,
    "ci_failure": ci_failure,
    "reviews": reviews,
    "issue": issue,
    "workspaces": workspaces,
    "agent_transcript": agent_transcript,
    "changes": changes,
}


def call(name, args):
    handler = HANDLERS.get(name)
    if not handler:
        return {"content": [{"type": "text", "text": f"Unknown tool {name}"}], "isError": True}
    args = args if isinstance(args, dict) else {}
    schema = next(tool["inputSchema"] for tool in TOOLS if tool["name"] == name)
    for required in schema.get("required", []):
        if not isinstance(args.get(required), str) or not args[required].strip():
            return {
                "content": [{"type": "text", "text": f"Missing argument {required!r}"}],
                "isError": True,
            }
    try:
        text = json.dumps(handler(args), ensure_ascii=False)
        error = False
    except ToolError as exc:
        text, error = str(exc), True
    except Exception as exc:
        # The dashboard answered something unexpected: say so, and keep serving the turn.
        text, error = f"Could not read that: {type(exc).__name__}: {exc}", True
    if len(text) > MAX_ANSWER:
        text = text[:MAX_ANSWER] + " …(cut short)"
    return {"content": [{"type": "text", "text": text}], "isError": error}


def respond(message):
    """The JSON-RPC answer to one request, or None for a notification."""
    method = message.get("method")
    if "id" not in message:
        return None
    if method == "initialize":
        result = {
            "protocolVersion": (message.get("params") or {}).get("protocolVersion", PROTOCOL),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "babysit-dashboard", "version": "1"},
        }
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        params = message.get("params") or {}
        result = call(params.get("name"), params.get("arguments") or {})
    elif method == "ping":
        result = {}
    else:
        return {
            "jsonrpc": "2.0",
            "id": message["id"],
            "error": {"code": -32601, "message": f"Unknown method {method}"},
        }
    return {"jsonrpc": "2.0", "id": message["id"], "result": result}


def main():
    for line in sys.stdin:
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if not isinstance(message, dict):
            continue
        answer = respond(message)
        if answer is not None:
            sys.stdout.write(json.dumps(answer) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
