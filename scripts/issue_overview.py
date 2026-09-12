"""Read-only GitHub issue discovery sharing the PR overview's search and cache machinery."""

import re

import pr_overview
from latest_activity import activity_fragment, latest_activity

ROLES = {
    "author": "author",
    "assignee": "assignee",
    "mentioned": "mentions",
    "participant": "commenter",
}
# Linked PRs carry head provenance so a checkout of the fix can be verified like a PR checkout.
ISSUE_FRAGMENT = (
    """... on Issue {
      id number title url createdAt updatedAt state
      repository { nameWithOwner }
      author { login }
      labels(first: 20) { nodes { name color } }
      assignees(first: 10) { nodes { login } }
      comments { totalCount }
      closedByPullRequestsReferences(first: 10, includeClosedPrs: false) {
        nodes {
          id number title url state isDraft
          repository { nameWithOwner }
          headRepository { nameWithOwner }
          headRefName headRefOid
        }
      }
    """
    + activity_fragment()
    + "}"
)
BRANCH = re.compile(r"issue-(\d+)(?:-|$)")


def linked_pr(node):
    return {
        "id": node["id"],
        "number": node["number"],
        "title": node.get("title"),
        "url": node["url"],
        "repo": node["repository"]["nameWithOwner"],
        "state": node.get("state"),
        "draft": bool(node.get("isDraft")),
        "head_repo": (node.get("headRepository") or {}).get("nameWithOwner"),
        "head_branch": node.get("headRefName"),
        "head_sha": node.get("headRefOid"),
    }


def issue_record(node, roles):
    labels = (node.get("labels") or {}).get("nodes") or []
    assignees = (node.get("assignees") or {}).get("nodes") or []
    linked = (node.get("closedByPullRequestsReferences") or {}).get("nodes") or []
    return {
        "id": node["id"],
        "number": node["number"],
        "title": node["title"],
        "url": node["url"],
        "repo": node["repository"]["nameWithOwner"],
        "author": (node.get("author") or {}).get("login"),
        "assignees": [a["login"] for a in assignees if a and a.get("login")],
        "labels": [
            {"name": label["name"], "color": label.get("color")} for label in labels if label
        ],
        "comments": (node.get("comments") or {}).get("totalCount", 0),
        "updated_at": node["updatedAt"],
        "latest_activity": latest_activity(node),
        "opened_at": node["createdAt"],
        "roles": roles,
        "linked_prs": [linked_pr(p) for p in linked if p],
    }


ISSUES = pr_overview.Kind(
    key="issues",
    label="issue",
    search="is:issue is:open",
    roles=ROLES,
    fragment=ISSUE_FRAGMENT,
    cache="issue-overview.json",
    record=issue_record,
)


def collect():
    return pr_overview.collect(ISSUES)


def branch_number(branch):
    """The issue number encoded in a `wti`-style branch name, or None."""
    match = BRANCH.match(branch or "")
    return int(match[1]) if match else None


class Overview(pr_overview.Overview):
    kind = ISSUES

    def fetch(self):
        return collect()
