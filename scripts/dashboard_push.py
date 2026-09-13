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
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlsplit

LOG = logging.getLogger(__name__)
PUSH_HOSTS = {"web.push.apple.com", "fcm.googleapis.com", "updates.push.services.mozilla.com"}
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
            values["checks"] = sorted(
                (
                    {field: check.get(field) for field in ("name", "bucket", "state")}
                    for check in item.get("check_details", [])
                ),
                key=lambda value: json.dumps(value),
            )
        result.append(
            {
                "url": url,
                "title": item.get("title") or item.get("branch") or url.split("github.com/")[1],
                "at": (item.get("updated_at") or 0) * 1000
                if source == "watcher"
                else payload["synced_at"] * 1000,
                "values": values,
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


def observe(device, source, payload, now):
    if not payload or payload.get("error") or payload.get("refreshing"):
        return
    if source != "watcher" and (
        payload.get("login") != device["login"] or not payload.get("synced_at")
    ):
        return
    initialized = source in device["baselines"]
    baseline = device["baselines"].setdefault(source, {})
    for sample in samples(source, payload):
        url = sample["url"]
        old = baseline.get(url)
        if old and sample["at"] < old["at"]:
            continue
        baseline[url] = sample
        changed = initialized and (not old or old["values"] != sample["values"])
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
            entry["at"] = max(now, entry["at"] + 1, entry["seenAt"] + 1)
            fields = [
                label
                for field, label in FIELDS[source].items()
                if not old or old["values"].get(field) != sample["values"][field]
            ]
            entry["notes"][source] = {"at": entry["at"], "text": " · ".join(fields) + " updated"}
        elif source not in entry["notes"]:
            entry["notes"][source] = {"at": entry["at"], "text": "Recent activity"}
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


class PushInbox:
    def __init__(self, home: Path, snapshots, sender=deliver):
        self.path = home / "dashboard-push.json"
        self.key_path = home / "dashboard-vapid.pem"
        self.snapshots = snapshots
        self.sender = sender
        self.lock = threading.RLock()
        self.stopping = threading.Event()
        self.worker: threading.Thread | None = None
        self.devices: dict[str, dict] = {}
        self.load_error = False
        try:
            self.devices = json.loads(self.path.read_text())["devices"]
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
            json.dump({"devices": self.devices}, stream)
        temp.replace(self.path)

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

    def view(self, device):
        return {
            "login": device["login"],
            "entries": copy.deepcopy(device["entries"]),
            "count": len(unread(device)),
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
            return self.view(device)

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
        with self.lock:
            for token, device in self.devices.items():
                if (
                    device.get("subscription") == subscription
                    and device["origin"] == origin
                    and device["login"] == login
                ):
                    return {**self.view(device), "token": token}
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
                observe(device, source, payload, now)
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
            device["sent"] = unread(device)
            token = secrets.token_urlsafe(32)
            self.devices[token] = device
            self.save()
            return {**self.view(device), "token": token}

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
                    observe(device, source, payload, now * 1000)
            if before != json.dumps(self.devices, sort_keys=True):
                self.save()
            tokens = list(self.devices)
        for token in tokens:
            with self.lock:
                if token not in self.devices:
                    continue
                device = self.devices[token]
                if not device.get("subscription") or device["retry_at"] > now:
                    continue
                if packets.get("prs", {}).get("login") != device["login"]:
                    continue
                pending = unread(device)
                fresh = {url: at for url, at in pending.items() if at > device["sent"].get(url, 0)}
                if not fresh:
                    continue
                latest = max(fresh, key=lambda url: fresh[url])
                entry = device["entries"][latest]
                payload = {
                    "title": "Babysitter",
                    "body": f"{len(pending)} unseen items · {entry['title'][:140]}",
                    "count": len(pending),
                    "url": destination(latest, entry),
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
                        device["sent"].update(pending)
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
