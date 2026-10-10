"""Persistent per-device inboxes and optional Web Push delivery for the dashboard."""

import base64
import copy
import importlib.util
import json
import logging
import os
import re
import secrets
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlsplit

LOG = logging.getLogger(__name__)
PUSH_HOSTS = {"web.push.apple.com", "fcm.googleapis.com", "updates.push.services.mozilla.com"}
# How long, in milliseconds, an item may be missing from its source before its baseline goes.
FORGET_MISSING = 24 * 60 * 60 * 1000
FIELDS = {
    "prs": {
        "repo": "Repository",
        "title": "Title",
        "author": "Author",
        "ci": "CI",
        "review_decision": "Review",
        "draft": "Draft status",
        "roles": "Your role",
        "head_sha": "New commits",
        "updated_at": "Activity",
    },
    "issues": {
        "repo": "Repository",
        "title": "Title",
        "author": "Author",
        "assignees": "Assignees",
        "labels": "Labels",
        "roles": "Your role",
        "comments": "Comments",
        "linked_prs": "Linked PRs",
        "updated_at": "Activity",
    },
    "watcher": {
        "status": "Watch status",
        "summary": "Watcher activity",
        "sha": "New commits",
        "pr_outcome": "PR outcome",
        "attempts": "Repair activity",
        "feedback_approved": "Feedback approval",
        "feedback": "Feedback",
        "checks": "CI",
    },
}
OWN_ACTIVITY_FIELDS = {
    "IssueComment": {"comments", "updated_at"},
    "ContentEdited": {"updated_at"},
    "RenamedTitleEvent": {"title", "updated_at"},
    "AssignedEvent": {"assignees", "roles", "updated_at"},
    "UnassignedEvent": {"assignees", "roles", "updated_at"},
    "LabeledEvent": {"labels", "updated_at"},
    "UnlabeledEvent": {"labels", "updated_at"},
    "ReadyForReviewEvent": {"draft", "updated_at"},
    "ConvertToDraftEvent": {"draft", "updated_at"},
    "ReviewRequestedEvent": {"roles", "updated_at"},
    "ReviewRequestRemovedEvent": {"roles", "updated_at"},
    "PullRequestReview": {"review_decision", "updated_at"},
    "ReviewDismissedEvent": {"review_decision", "updated_at"},
    "HeadRefForcePushedEvent": {"head_sha", "updated_at"},
}


def check_result(checks):
    if not checks:
        return "NONE"
    if any(
        check.get("bucket") == "pending"
        or check.get("state")
        in {
            "PENDING",
            "QUEUED",
            "IN_PROGRESS",
            "EXPECTED",
            "WAITING",
            "REQUESTED",
            "PENDING_APPROVAL",
        }
        for check in checks
    ):
        return "PENDING"
    return (
        "FAILURE"
        if any(
            check.get("bucket") in {"fail", "cancel"}
            or check.get("state")
            in {"FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED"}
            for check in checks
        )
        else "SUCCESS"
    )


def notification_fields(source, sample, old, login):
    values = sample["values"]
    previous = dict(old["values"]) if old else {}
    # Upgrade saved per-check fingerprints without announcing an existing result.
    if source == "watcher" and isinstance(previous.get("checks"), list):
        previous["checks"] = check_result(previous["checks"])
    fields = {field for field in values if not old or previous.get(field) != values[field]}
    if source == "prs" and values.get("ci") not in {"SUCCESS", "FAILURE", "ERROR"}:
        fields.discard("ci")
    if source == "watcher":
        if values.get("status") == "running":
            fields.discard("attempts")
            fields.discard("status")
        if values.get("checks") not in {"SUCCESS", "FAILURE", "ERROR"}:
            fields.discard("checks")
        fields.discard("summary")  # Poll wording alone is not a new outcome.
        if not old:
            return fields
        for field in list(fields):
            expected = previous.get(field)
            for action in sample.get("actions", []):
                if (
                    action["at"] * 1000 >= old["at"]
                    and field in action["before"]
                    and expected == action["before"][field]
                ):
                    expected = action["after"][field]
            if expected == values[field]:
                fields.discard(field)
        old_feedback = {
            f"{item['kind']}:{item['id']}": item for item in previous.get("feedback", [])
        }
        current = {f"{item['kind']}:{item['id']}": item for item in values.get("feedback", [])}
        authors = {
            f"{item['kind']}:{item['id']}": (item.get("author") or "").lower()
            for item in sample.get("feedback_authors", [])
        }
        added = current.keys() - old_feedback.keys()
        if (
            added
            and all(current.get(key) == item for key, item in old_feedback.items())
            and all(authors.get(key) == login.lower() for key in added)
        ):
            fields.discard("feedback")
        return fields
    history = sample.get("activity")
    cursor = previous.get("updated_at")
    updated = values.get("updated_at")
    if (
        not old
        or not history
        or not cursor
        or not updated
        or not history.get("since")
        or history["since"] > cursor
    ):
        return fields
    events = [event for event in history["events"] if event["at"] > cursor]
    if (
        not events
        or max(event["at"] for event in events) < updated
        or any(
            (event.get("actor") or "").lower() != login.lower()
            or event["type"] not in OWN_ACTIVITY_FIELDS
            for event in events
        )
    ):
        return fields
    return fields - set().union(*(OWN_ACTIVITY_FIELDS[event["type"]] for event in events))


def subject(raw):
    if not isinstance(raw, str):
        return None
    match = re.fullmatch(r"https://github\.com/([^/]+)/([^/]+)/(pull|issues|tree)/([^?#]+?)/?", raw)
    if not match or (match[3] != "tree" and not match[4].isdigit()):
        return None
    return f"https://github.com/{match[1].lower()}/{match[2].lower()}/{match[3]}/{match[4]}"


def samples(source, payload):
    result = []
    for item in payload.get("jobs" if source == "watcher" else source, []):
        url = subject(item.get("url"))
        if not url:
            continue
        values = {field: item.get(field) for field in FIELDS[source]}
        for field in ("roles", "assignees"):
            if field in values:
                values[field] = sorted(item.get(field) or [], key=str.casefold)
        if source == "issues":
            values["labels"] = sorted(label["name"] for label in item.get("labels", []))
            values["linked_prs"] = sorted(
                f"{pr['repo']}#{pr['number']}" for pr in item.get("linked_prs", [])
            )
        if source == "watcher":
            values["feedback"] = [
                {field: comment.get(field) for field in ("kind", "id", "body")}
                for comment in item.get("feedback", [])
            ]
            # Ended watches leave out their check details, but not their overall result.
            values["checks"] = (
                item["checks_result"]
                if "checks_result" in item
                else check_result(item.get("check_details", []))
            )
        result.append(
            {
                "url": url,
                "title": item.get("title") or item.get("branch") or url.split("github.com/")[1],
                "at": (item.get("updated_at") or 0) * 1000
                if source == "watcher"
                else payload["synced_at"] * 1000,
                "values": values,
                "actions": item.get("notification_actions", []),
                "activity": item.get("notification_activity"),
                "feedback_authors": item.get("feedback", []),
            }
        )
        result[-1]["seed_at"] = result[-1]["at"]
        if source != "watcher" and item.get("updated_at"):
            try:
                result[-1]["seed_at"] = (
                    datetime.fromisoformat(item["updated_at"]).timestamp() * 1000
                )
            except (TypeError, ValueError):
                pass
    return result


def observe(device, source, payload, now, silenced=()):
    if not payload or payload.get("error") or payload.get("refreshing"):
        return
    if source != "watcher" and (
        payload.get("login") != device["login"] or not payload.get("synced_at")
    ):
        return
    initialized = source in device["baselines"]
    baseline = device["baselines"].setdefault(source, {})
    listed = samples(source, payload)
    for sample in listed:
        url = sample["url"]
        old = baseline.get(url)
        if old and sample["at"] < old["at"]:
            continue
        baseline[url] = sample
        changed_fields = notification_fields(source, sample, old, device["login"])
        ci_field = "ci" if source == "prs" else "checks" if source == "watcher" else None
        result = sample["values"].get(ci_field)
        if result in {"SUCCESS", "FAILURE", "ERROR"} and (not old or ci_field in changed_fields):
            outcome = {
                "sha": sample["values"].get("head_sha" if source == "prs" else "sha"),
                "result": result,
            }
            outcomes = device.setdefault("outcomes", {})
            if outcomes.get(url) == outcome:
                changed_fields.discard(ci_field)
            outcomes[url] = outcome
        if url in silenced:
            entry = device["entries"].get(url)
            if entry:
                entry["seenAt"] = entry["at"]
            continue
        changed = initialized and bool(changed_fields)
        entry = device["entries"].get(url)
        if not entry and not changed and old:
            continue
        if entry is None:
            entry = device["entries"][url] = {
                "title": sample["title"],
                "at": sample["seed_at"],
                "seenAt": sample["seed_at"],
                "notes": {},
            }
        if source != "watcher" or "prs" not in entry["notes"]:
            entry["title"] = sample["title"]
        if changed:
            entry["changedAt"] = now
            entry["at"] = max(now, entry["at"] + 1, entry["seenAt"] + 1)
            fields = [label for field, label in FIELDS[source].items() if field in changed_fields]
            entry["notes"][source] = {"at": entry["at"], "text": " · ".join(fields) + " updated"}
        elif source not in entry["notes"]:
            entry["notes"][source] = {"at": entry["at"], "text": "Recent activity"}
    # As the browser inbox does: a snapshot lists all of its source's items, but one can
    # drop out for a while. Its baseline is kept, so a return compares as usual, and is
    # forgotten after a day missing, with CI outcomes no source still lists. Entries stay.
    present = {sample["url"] for sample in listed}
    for url, old in list(baseline.items()):
        if url in present:
            old.pop("missing", None)
        elif "missing" not in old:
            old["missing"] = now
        elif now - old["missing"] >= FORGET_MISSING:
            del baseline[url]
    known = set().union(*device["baselines"].values())
    for url in [url for url in device.get("outcomes", {}) if url not in known]:
        del device["outcomes"][url]
    ordered = sorted(device["entries"], key=lambda key: device["entries"][key]["at"], reverse=True)
    for key in ordered[10:]:
        entry = device["entries"][key]
        if entry["seenAt"] >= entry["at"]:
            del device["entries"][key]


def unread(device):
    return {
        url: entry["at"]
        for url, entry in device["entries"].items()
        if entry["at"] > entry["seenAt"]
    }


def destination(url, entry):
    source = max(
        entry["notes"],
        key=lambda source: (entry["notes"][source]["at"], source == "watcher"),
        default="",
    )
    if source not in FIELDS:
        source = "issues" if "/issues/" in url else "watcher" if "/tree/" in url else "prs"
    return f"/?item={quote(url, safe='')}#{source}"


def validate_subscription(value):
    if not isinstance(value, dict) or not isinstance(value.get("endpoint"), str):
        raise ValueError("Invalid push subscription")
    url = urlsplit(value["endpoint"])
    if (
        url.scheme != "https"
        or not (
            url.hostname in PUSH_HOSTS
            or (url.hostname and url.hostname.endswith(".push.apple.com"))
        )
        or url.port not in (None, 443)
        or url.username
        or url.password
        or url.fragment
        or len(value["endpoint"]) > 4096
    ):
        raise ValueError("Unsupported push service")
    keys = value.get("keys")
    if not isinstance(keys, dict):
        raise ValueError("Invalid push keys")
    for name, size in (("auth", 16), ("p256dh", 65)):
        encoded = keys.get(name)
        if not isinstance(encoded, str) or not re.fullmatch(r"[A-Za-z0-9_-]+={0,2}", encoded):
            raise ValueError("Invalid push keys")
        try:
            raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        except ValueError as exc:
            raise ValueError("Invalid push keys") from exc
        if len(raw) != size or (name == "p256dh" and raw[0] != 4):
            raise ValueError("Invalid push keys")
    return {
        "endpoint": value["endpoint"],
        "keys": {name: keys[name] for name in ("auth", "p256dh")},
    }


def deliver(subscription, payload, key, origin):
    from pywebpush import webpush  # type: ignore[import-untyped]
    from requests import Session

    class NoRedirects(Session):
        def request(self, *args, **kwargs):
            kwargs["allow_redirects"] = False
            return super().request(*args, **kwargs)

    with NoRedirects() as session:
        response = webpush(
            subscription_info=subscription,
            data=json.dumps(payload),
            vapid_private_key=str(key),
            vapid_claims={"sub": origin},
            ttl=3600,
            timeout=15,
            headers={"Topic": "babysitter-updates", "Urgency": "normal"},
            requests_session=session,
        )
        if not 200 <= response.status_code < 300:
            raise RuntimeError("Push service did not accept the notification")


# needs(): the login follows the feed, but it cannot be read right now.
UNAVAILABLE = "unavailable"


class PushInbox:
    def __init__(self, home: Path, snapshots, sender=deliver, login=None):
        self.path = home / "dashboard-push.json"
        self.key_path = home / "dashboard-vapid.pem"
        self.snapshots = snapshots
        # The signed-in GitHub login without building every snapshot; polled often.
        self.login = login or (lambda: self.snapshots().get("prs", {}).get("login"))
        self.sender = sender
        self.lock = threading.RLock()
        self.stopping = threading.Event()
        self.worker: threading.Thread | None = None
        self.devices: dict[str, dict] = {}
        self.silenced: dict[str, list[str]] = {}
        # Per login: alerts and the badge follow what needs the user (on unless turned off).
        self.focused: dict[str, bool] = {}
        # The attention feed, set once the dashboard server exists; None leaves alerts as is.
        self.attention: Callable[[], dict] | None = None
        self.load_error = False
        try:
            state = json.loads(self.path.read_text())
            self.devices = state["devices"]
            self.silenced = state.get("silenced", {})
            self.focused = state.get("focus", {})
            if not isinstance(self.focused, dict) or any(
                not isinstance(login, str) or not isinstance(value, bool)
                for login, value in self.focused.items()
            ):
                raise ValueError("Invalid notification preferences")
            if not isinstance(self.silenced, dict) or any(
                not isinstance(login, str)
                or not isinstance(urls, list)
                or any(not subject(url) for url in urls)
                for login, urls in self.silenced.items()
            ):
                raise ValueError("Invalid notification preferences")
            if not isinstance(self.devices, dict) or any(
                not isinstance(value, dict) for value in self.devices.values()
            ):
                raise ValueError("Invalid device state")
        except FileNotFoundError:
            pass
        except (ValueError, KeyError, TypeError):
            self.devices = {}
            self.load_error = True
        self.available = importlib.util.find_spec("pywebpush") is not None

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(
                {"devices": self.devices, "silenced": self.silenced, "focus": self.focused}, stream
            )
        temp.replace(self.path)

    def preferences(self):
        login = self.login()
        with self.lock:
            return {
                "login": login,
                "silenced": list(self.silenced.get(login, [])),
                "focus": self.focused.get(login, True),
            }

    def focus_on(self, login):
        return self.focused.get(login, True)

    def focus(self, request):
        """Whether alerts and the badge follow what needs this login, on every device."""
        if self.load_error:
            raise ValueError("The saved notification state needs repair")
        login = self.login()
        if not login or request.get("login") != login:
            raise ValueError("Wait for the PR overview to finish syncing")
        if not isinstance(request.get("focus"), bool):
            raise ValueError("Supply the notification focus preference")
        with self.lock:
            self.focused[login] = request["focus"]
            self.save()
        return self.preferences()

    def needs(self, login):
        """What needs this login now, read outside the lock: the feed can be slow.

        None when the login does not follow the feed; UNAVAILABLE when it does but the
        feed cannot say (not wired, failed, or another login's); otherwise the items that
        are not parked or silenced, their GitHub subjects, and their count.
        """
        if self.attention is None or not self.focus_on(login):
            return None
        try:
            feed = self.attention()
        except Exception:
            return UNAVAILABLE
        if feed.get("login") != login:
            return UNAVAILABLE
        with self.lock:
            silenced = set(self.silenced.get(login, []))
        items = [
            {
                "key": item["key"],
                "url": subject(item.get("url")),
                "since": item.get("since") or 0,
                "title": item.get("title") or item["key"],
            }
            for item in feed.get("items", [])
            if not item.get("parked") and subject(item.get("url")) not in silenced
        ]
        return {
            "items": items,
            "subjects": {item["url"] for item in items} - {None},
            "count": len(items),
        }

    def silence(self, request):
        if self.load_error:
            raise ValueError("The saved notification state needs repair")
        packets = self.snapshots()
        login = packets.get("prs", {}).get("login")
        url = subject(request.get("url"))
        if not login or request.get("login") != login:
            raise ValueError("Wait for the PR overview to finish syncing")
        if not url or "/pull/" not in url or not isinstance(request.get("silenced"), bool):
            raise ValueError("Supply a PR and its notification preference")
        with self.lock:
            urls = set(self.silenced.get(login, []))
            # Observe the current snapshot while muted, including when unsilencing,
            # so activity during the quiet period cannot be replayed later.
            for device in self.devices.values():
                if device["login"] == login:
                    for source, payload in packets.items():
                        observe(device, source, payload, time.time() * 1000, urls | {url})
                    entry = device["entries"].get(url)
                    if entry:
                        entry["seenAt"] = entry["at"]
            if request["silenced"]:
                urls.add(url)
            else:
                urls.discard(url)
            self.silenced[login] = sorted(urls)
            self.save()
            return {"login": login, "silenced": sorted(urls)}

    def config(self):
        if self.load_error:
            return {"available": False, "error": "The saved notification state needs repair."}
        if not self.available:
            return {
                "available": False,
                "error": "Background notifications are not enabled on this dashboard.",
            }
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec

        with self.lock:
            try:
                key = serialization.load_pem_private_key(self.key_path.read_bytes(), password=None)
            except FileNotFoundError:
                key = ec.generate_private_key(ec.SECP256R1())
                self.key_path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(self.key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(
                        key.private_bytes(
                            serialization.Encoding.PEM,
                            serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption(),
                        )
                    )
            public = key.public_key().public_bytes(
                serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
            )
        return {
            "available": True,
            "publicKey": base64.urlsafe_b64encode(public).decode().rstrip("="),
        }

    def view(self, device, needs=None):
        """The device's state; `needs` comes from needs() read before the lock was taken."""
        return {
            "login": device["login"],
            "entries": copy.deepcopy(device["entries"]),
            "count": len(unread(device)),
            # What the Home Screen badge shows: what needs the user, else the unseen count.
            "badge": needs["count"] if isinstance(needs, dict) else len(unread(device)),
            "focus": needs is not None,
            "error": device.get("error"),
            "active": bool(device.get("subscription")),
            "revision": time.time() * 1000,
            "observed": {
                source: {url: sample["at"] for url, sample in values.items()}
                for source, values in device["baselines"].items()
            },
        }

    def action(self, action, request, origin):
        if action == "push-subscribe":
            return self.subscribe(request, origin)
        needs = self.needs(request.get("login")) if isinstance(request.get("login"), str) else None
        with self.lock:
            if not isinstance(request.get("token"), str):
                raise ValueError("Supply a device token")
            device = self.devices.get(request.get("token"))
            if not device or device["origin"] != origin or device["login"] != request.get("login"):
                raise ValueError("This device is not subscribed; enable notifications again")
            if action == "push-unsubscribe":
                del self.devices[request["token"]]
                self.save()
                return {"disabled": True}
            if action == "push-seen":
                seen = request.get("seen")
                if not isinstance(seen, dict) or len(seen) > 5000:
                    raise ValueError("Supply the updates that were seen")
                for url, at in seen.items():
                    if not isinstance(at, (float, int)) or not 0 <= at < 1e16:
                        raise ValueError("Invalid seen update")
                    entry = device["entries"].get(url)
                    if entry and at <= entry["at"]:
                        entry["seenAt"] = max(entry["seenAt"], at)
                self.save()
            return self.view(device, needs)

    def subscribe(self, request, origin):
        if not self.config()["available"]:
            raise ValueError("Web Push is unavailable")
        subscription = validate_subscription(request.get("subscription"))
        seed = request.get("unseen", [])
        if not isinstance(seed, list) or len(seed) > 5000 or any(not subject(url) for url in seed):
            raise ValueError("Invalid unread items")
        history = request.get("history", {})
        if not isinstance(history, dict) or len(history) > 5000:
            raise ValueError("Invalid notification history")
        for url, entry in history.items():
            if (
                not subject(url)
                or not isinstance(entry, dict)
                or not isinstance(entry.get("title"), str)
                or len(entry["title"]) > 1000
                or any(
                    not isinstance(entry.get(field), (int, float)) or not 0 <= entry[field] < 1e16
                    for field in ("at", "seenAt")
                )
            ):
                raise ValueError("Invalid notification history")
        packets = self.snapshots()
        login = packets.get("prs", {}).get("login")
        if not login or login != request.get("login"):
            raise ValueError(
                "Wait for the PR overview to finish syncing before enabling notifications"
            )
        needs = self.needs(login)
        with self.lock:
            for token, device in self.devices.items():
                if (
                    device.get("subscription") == subscription
                    and device["origin"] == origin
                    and device["login"] == login
                ):
                    return {**self.view(device, needs), "token": token}
            if len(self.devices) >= 100:
                raise ValueError("Too many subscribed devices")
            # The subscription belongs to one account at a time on this installation.
            for token in list(self.devices):
                if (self.devices[token].get("subscription") or {}).get("endpoint") == subscription[
                    "endpoint"
                ]:
                    del self.devices[token]
            now = time.time() * 1000
            device = {
                "login": login,
                "origin": origin,
                "subscription": subscription,
                "baselines": {},
                "entries": {},
                "sent": {},
                "retry_at": 0,
                "failures": 0,
            }
            for source, payload in packets.items():
                observe(device, source, payload, now, self.silenced.get(login, []))
            for url, entry in history.items():
                device["entries"][subject(url)] = {
                    "title": entry["title"],
                    "at": entry["at"],
                    "seenAt": entry["seenAt"],
                    "notes": {
                        "prs" if "/pull/" in url else "issues": {
                            "at": entry["at"],
                            "text": "Recent activity",
                        }
                    },
                }
            for url in seed:
                url = subject(url)
                for source, baseline in device["baselines"].items():
                    if url in baseline:
                        device["entries"].setdefault(
                            url,
                            {
                                "title": baseline[url]["title"],
                                "at": now,
                                "seenAt": 0,
                                "notes": {source: {"at": now, "text": "Recent activity"}},
                            },
                        )
                if url in device["entries"]:
                    device["entries"][url]["seenAt"] = 0
            for url in self.silenced.get(login, []):
                if url in device["entries"]:
                    device["entries"][url]["seenAt"] = device["entries"][url]["at"]
            device["sent"] = unread(device)
            # Nothing that already needs the user is announced on subscribing.
            device["needs_sent"] = (
                {item["key"]: item["since"] for item in needs["items"]}
                if isinstance(needs, dict)
                else {}
            )
            token = secrets.token_urlsafe(32)
            self.devices[token] = device
            self.save()
            return {**self.view(device, needs), "token": token}

    def tick(self):
        if not self.available or self.load_error:
            return
        with self.lock:
            if not any(device.get("subscription") for device in self.devices.values()):
                return
        packets = self.snapshots()
        with self.lock:
            before = json.dumps(self.devices, sort_keys=True)
            now = time.time()
            for device in self.devices.values():
                # Never route a new GitHub account's watcher data to the old account.
                if packets.get("prs", {}).get("login") != device["login"]:
                    continue
                for source, payload in packets.items():
                    observe(
                        device, source, payload, now * 1000, self.silenced.get(device["login"], [])
                    )
            if before != json.dumps(self.devices, sort_keys=True):
                self.save()
            tokens = list(self.devices)
            logins = {
                device["login"] for device in self.devices.values() if device.get("subscription")
            }
        # The feed is read once per login, outside the lock other requests wait on.
        needs_by_login = {login: self.needs(login) for login in logins}
        for token in tokens:
            with self.lock:
                if token not in self.devices:
                    continue
                device = self.devices[token]
                if not device.get("subscription") or device["retry_at"] > now:
                    continue
                if packets.get("prs", {}).get("login") != device["login"]:
                    continue
                needs = needs_by_login.get(device["login"])
                if needs is UNAVAILABLE:
                    # Following the feed, but it cannot answer now: better quiet than an
                    # alert for every unseen update; the next tick tries again.
                    continue
                pending = unread(device)
                fresh = {url: at for url, at in pending.items() if at > device["sent"].get(url, 0)}
                # Let a subject settle for 30 seconds; CI and repair outcomes arriving
                # in adjacent polls become one alert. Other subjects do not delay it.
                fresh = {
                    url: at
                    for url, at in fresh.items()
                    if now * 1000 - device["entries"][url].get("changedAt", 0) >= 30000
                    and url not in self.silenced.get(device["login"], [])
                }
                arrived = {}
                if needs is not None:
                    # Only changes to items that need the user wake the phone; the rest stay
                    # in the inbox for the bell, and count as sent so they do not alert
                    # later. An item that newly needs the user (an agent blocked, a launch
                    # failed) alerts even without an inbox entry.
                    skipped = {url: at for url, at in fresh.items() if url not in needs["subjects"]}
                    fresh = {url: at for url, at in fresh.items() if url in needs["subjects"]}
                    if skipped:
                        device["sent"].update(skipped)
                        self.save()
                    sent = device.setdefault("needs_sent", {})
                    keys = {item["key"] for item in needs["items"]}
                    for key in [key for key in sent if key not in keys]:
                        del sent[key]  # Gone for now; it alerts again if it comes back.
                    arrived = {
                        item["key"]: item
                        for item in needs["items"]
                        if item["key"] not in sent or item["since"] > sent[item["key"]]
                    }
                    # The change that made an item need the user is still settling in the
                    # inbox: one alert covers both once it has.
                    unsettled = any(
                        now * 1000 - device["entries"][url].get("changedAt", 0) < 30000
                        for url in pending
                        if url in needs["subjects"]
                    )
                    if unsettled:
                        continue
                if not fresh and not arrived:
                    continue
                if fresh:
                    latest = max(fresh, key=lambda url: fresh[url])
                    entry = device["entries"][latest]
                    title, url = entry["title"], destination(latest, entry)
                else:
                    item = max(arrived.values(), key=lambda item: item["since"])
                    title = item["title"]
                    entry = device["entries"].get(item["url"]) if item["url"] else None
                    url = destination(item["url"], entry) if entry else "/#attention"
                count = needs["count"] if needs is not None else len(pending)
                payload = {
                    "title": "Babysitter",
                    "body": f"{count} {'need you' if needs is not None else 'unseen items'}"
                    f" · {title[:140]}",
                    "count": count,
                    "kind": "needs" if needs is not None else "unseen",
                    "url": url,
                    "revision": now * 1000,
                }
                sending = copy.deepcopy(device)
            try:
                self.sender(sending["subscription"], payload, self.key_path, sending["origin"])
            except Exception as exc:
                response = getattr(exc, "response", None)
                code = getattr(exc, "status_code", None) or getattr(response, "status_code", None)
                with self.lock:
                    if device is None or self.devices.get(token) is not device:
                        continue
                    if code in (404, 410):
                        device["subscription"] = None
                        device["error"] = "Notification subscription expired; enable it again."
                    else:
                        device["failures"] += 1
                        device["retry_at"] = now + min(3600, 30 * 2 ** min(device["failures"], 7))
                        device["error"] = "Push delivery is delayed; retrying automatically."
                    self.save()
            else:
                with self.lock:
                    if device is not None and self.devices.get(token) is device:
                        device["sent"].update(fresh)
                        if needs is not None:
                            device.setdefault("needs_sent", {}).update(
                                {key: item["since"] for key, item in arrived.items()}
                            )
                        device["failures"] = 0
                        device["error"] = None
                        self.save()

    def start(self):
        def run():
            while not self.stopping.is_set():
                try:
                    self.tick()
                except Exception:
                    # Do not log subscriptions or private notification contents.
                    LOG.error("Dashboard push refresh failed; will retry")
                self.stopping.wait(15)

        self.worker = threading.Thread(target=run, daemon=True, name="dashboard-push")
        self.worker.start()

    def close(self):
        self.stopping.set()
        if self.worker:
            self.worker.join(timeout=20)
