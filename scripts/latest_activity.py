"""Bounded, best-effort activity attribution shared by PR and issue discovery."""

COMMON_EVENTS = {
    "AssignedEvent": "changed assignees",
    "UnassignedEvent": "removed an assignee",
    "LabeledEvent": "added a label",
    "UnlabeledEvent": "removed a label",
    "RenamedTitleEvent": "renamed the title",
    "ClosedEvent": "closed this",
    "ReopenedEvent": "reopened this",
    "LockedEvent": "locked the conversation",
    "UnlockedEvent": "unlocked the conversation",
    "MilestonedEvent": "set a milestone",
    "DemilestonedEvent": "removed a milestone",
    "CrossReferencedEvent": "referenced this",
}
PR_EVENTS = {
    "ReadyForReviewEvent": "marked ready for review",
    "ConvertToDraftEvent": "converted to draft",
    "ReviewRequestedEvent": "requested a review",
    "ReviewRequestRemovedEvent": "removed a review request",
    "ReviewDismissedEvent": "dismissed a review",
    "MergedEvent": "merged this",
    "HeadRefForcePushedEvent": "force-pushed the branch",
    "HeadRefDeletedEvent": "deleted the branch",
    "HeadRefRestoredEvent": "restored the branch",
    "BaseRefChangedEvent": "changed the base branch",
}


def activity_fragment(*, pull_request=False):
    events = {**COMMON_EVENTS, **(PR_EVENTS if pull_request else {})}
    fragments = []
    for name in events:
        details = " label { name }" if name in {"LabeledEvent", "UnlabeledEvent"} else ""
        if name in {
            "ClosedEvent",
            "CrossReferencedEvent",
            "ConvertToDraftEvent",
            "ReadyForReviewEvent",
            "ReviewDismissedEvent",
            "MergedEvent",
        }:
            details += " url"
        fragments.append(f"... on {name} {{ createdAt actor {{ login __typename }}{details} }}")
    fragments.append("""... on IssueComment {
        createdAt author { login __typename } url lastEditedAt editor { login __typename }
    }""")
    if pull_request:
        fragments.append("""... on PullRequestReview {
            submittedAt state author { login __typename } url
            lastEditedAt editor { login __typename }
        }
        ... on PullRequestCommit {
            commit { committedDate committer { name user { login } } url }
        }""")
    return (
        """
      lastEditedAt editor { login __typename }
      timelineItems(last: 5) { nodes { __typename
    """
        + "\n".join(fragments)
        + " } }"
    )


def actor_name(actor):
    actor = actor or {}
    login = actor.get("login")
    if login and actor.get("__typename") == "Bot" and not login.endswith("[bot]"):
        return f"{login}[bot]"
    return login


def activity(kind, actor, action, at, url=None):
    return {"type": kind, "actor": actor, "action": action, "at": at, "url": url}


def content_edit(node, action):
    if node.get("lastEditedAt"):
        return activity(
            "ContentEdited",
            actor_name(node.get("editor")),
            action,
            node["lastEditedAt"],
            node.get("url"),
        )
    return None


def timeline_activity(event):
    kind = event.get("__typename")
    actor = actor_name(event.get("actor"))
    at, url = event.get("createdAt"), event.get("url")
    action = {**COMMON_EVENTS, **PR_EVENTS}.get(kind)
    if kind in {"LabeledEvent", "UnlabeledEvent"}:
        label = (event.get("label") or {}).get("name")
        if label:
            action = f"{action}: {label}"
    elif kind in {"IssueComment", "PullRequestReview"}:
        actor = actor_name(event.get("author"))
        action = "commented"
        if kind == "PullRequestReview":
            at = event.get("submittedAt")
            if not at or event.get("state") == "PENDING":
                return None
            action = {
                "APPROVED": "approved",
                "CHANGES_REQUESTED": "requested changes",
                "COMMENTED": "reviewed",
                "DISMISSED": "submitted a review (now dismissed)",
            }.get(event.get("state"), "reviewed")
        edit = content_edit(
            event, "edited a comment" if kind == "IssueComment" else "edited a review"
        )
        if edit and edit["at"] > (at or ""):
            return edit
    elif kind == "PullRequestCommit":
        commit = event.get("commit") or {}
        committer = commit.get("committer") or {}
        actor = actor_name(committer.get("user")) or committer.get("name")
        # A commit's identity/time is not evidence of who pushed it, or when.
        at, url, action = commit.get("committedDate"), commit.get("url"), "committed"
    if not action or not at:
        return None
    return activity(kind, actor, action, at, url)


def latest_activity(node):
    timeline = node.get("timelineItems")
    if timeline is None:
        return None
    events = timeline.get("nodes") or []
    # Do not present an older event as latest when the timeline ends in an
    # unsupported or inaccessible event. Keep the overview usable as GitHub evolves.
    if events and (not events[-1] or timeline_activity(events[-1]) is None):
        return None
    candidates = [timeline_activity(event) for event in events if event]
    candidates.append(content_edit(node, "edited the description"))
    if not events:
        candidates.append(
            activity(
                "Opened",
                actor_name(node.get("author")),
                "opened this",
                node.get("createdAt"),
                node.get("url"),
            )
        )
    known = [item for item in candidates if item and item["at"]]
    return max(known, key=lambda item: item["at"], default=None)


def notification_activity(node):
    """Bound the attribution window; incomplete timelines must not suppress alerts."""
    timeline = node.get("timelineItems")
    if timeline is None:
        return None
    nodes = timeline.get("nodes") or []
    events = [timeline_activity(event) if event else None for event in nodes]
    if any(event is None for event in events):
        return None
    since = (
        min((event["at"] for event in events if event), default=node.get("createdAt"))
        if len(nodes) >= 5
        else node.get("createdAt")
    )
    if len(nodes) < 5:
        events.append(
            activity("Opened", actor_name(node.get("author")), "opened this", node.get("createdAt"))
        )
    events.append(content_edit(node, "edited the description"))
    return {"since": since, "events": [event for event in events if event and event["at"]]}
