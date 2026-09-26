"""Opt-in Sentry experiment: recent issues, severity triage, and publishing to GitHub.

Reads the Sentry REST API directly (structured fields the MCP server's Markdown
omits), groups the same exception seen on several servers, and scores each group
with a deterministic, explained heuristic. LLM work (triage, the publish
sanitizer) is queued for the supervisor-side worker in `sentry_llm`. Nothing here
changes Sentry except the explicit write-back of a created GitHub issue's link.
"""

import argparse
import copy
import hashlib
import json
import os
import re
import stat
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

import github_cli
import sentry_llm

CACHE_VERSION = 1
INTERVAL = 300
MANUAL_INTERVAL = 60
MAX_RESPONSE = 4 * 1024 * 1024
MAX_STATE = 4 * 1024 * 1024
MAX_REQUESTS = 40
MAX_GROUPS = 500
WORKER_STALE = 30
CREATE_TIMEOUT = 300
SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+")
PROJECT = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
LEVELS = {"fatal": 4, "error": 3, "warning": 2, "info": 1, "debug": 0}
SUBSTATUS = {"escalating": 3, "regressed": 2, "new": 1, "ongoing": 0}
PRIORITIES = {"high": 2, "medium": 1, "low": 0}
BUCKETS = (("critical", 10), ("high", 7), ("medium", 4), ("low", -100))
PERIODS = {"24h", "7d", "14d", "30d", "90d"}

# Backstop for the LLM sanitizer: anything matching blocks publishing.
PATTERNS = [
    ("email", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    (
        "ip",
        re.compile(
            r"(?<![\d.])(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)(?![\d.])"
        ),
    ),
    ("ip", re.compile(r"(?<![:\w])(?:[0-9a-fA-F]{1,4}:){4,7}[0-9a-fA-F]{1,4}(?![:\w])")),
    (
        "token",
        re.compile(
            r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{16,}"
            r"|xox[abpr]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|sntry[su]_[A-Za-z0-9_=-]{20,})"
        ),
    ),
    ("token", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    ("secret", re.compile(r"(?<![A-Za-z0-9])[0-9a-fA-F]{32,}(?![A-Za-z0-9])")),
    # Base64-like runs must mix cases and digits, so paths and words do not match.
    (
        "secret",
        re.compile(
            r"(?<![A-Za-z0-9+/])(?=[A-Za-z0-9+/]*[0-9])(?=[A-Za-z0-9+/]*[A-Z])"
            r"(?=[A-Za-z0-9+/]*[a-z])[A-Za-z0-9+]{24,}[A-Za-z0-9+/]{16,}={0,2}(?![A-Za-z0-9+/])"
        ),
    ),
    ("path", re.compile(r"(?:/home/|/Users/|/root/|[A-Za-z]:\\Users\\)[^\s/\\]+")),
    (
        "path",
        re.compile(r"(?<![\w.:/])/(?:srv|data|mnt|scratch|opt|var|tmp|net|corral)/[^\s/]+/\S+"),
    ),
    ("mention", re.compile(r"(?<![\w.@`])@[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})\b(?!\.)")),
]
# Links the draft itself generates are public identifiers, never findings.
PUBLIC_LINKS = re.compile(r"https://github\.com/\S+")


def findings(value, host=""):
    """Fixed-pattern matches in text headed for a public repository.

    Sentry permalinks on the configured host and github.com links are removed first:
    they are public identifiers the draft generates, and long numeric issue IDs would
    otherwise look like secrets.
    """
    value = PUBLIC_LINKS.sub(" ", value)
    if host:
        value = re.sub(re.escape(host) + r"/organizations/[a-z0-9_-]+/issues/\d+/?", " ", value)
    found = []
    for kind, pattern in PATTERNS:
        for match in pattern.finditer(value):
            excerpt = match.group(0)
            found.append(
                {"kind": kind, "excerpt": excerpt[:6] + "…" if len(excerpt) > 8 else excerpt}
            )
            if len(found) >= 20:
                return found
    return found


def load_config(directory: Path):
    """Validated configuration, None when absent. Raises ValueError when malformed."""
    path = directory / "config.json"
    if not path.exists():
        return None
    if path.stat().st_size > 65536:
        raise ValueError("Experiment configuration exceeds size limit")
    raw = json.loads(path.read_text())
    if not isinstance(raw, dict):
        raise ValueError("Experiment configuration must be a JSON object")
    host = raw.get("host", "")
    parsed = urllib.parse.urlsplit(host) if isinstance(host, str) else None
    if (
        not parsed
        or parsed.scheme != "https"
        or not parsed.hostname
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.username
    ):
        raise ValueError("Configure host as an https:// Sentry URL")
    org = raw.get("organization")
    if not isinstance(org, str) or not PROJECT.fullmatch(org):
        raise ValueError("Configure the Sentry organization slug")
    projects = raw.get("projects")
    if (
        not isinstance(projects, dict)
        or not 1 <= len(projects) <= 40
        or not all(
            isinstance(k, str) and PROJECT.fullmatch(k) and isinstance(v, str) and SLUG.fullmatch(v)
            for k, v in projects.items()
        )
    ):
        raise ValueError("Configure projects as {sentry-project: owner/repo}, at most 40")
    llm = raw.get("llm") or {}
    quota = llm.get("quota") or {}
    triage = llm.get("triage") or {}
    config = {
        "enabled": raw.get("enabled") is True,
        "host": f"https://{parsed.hostname}" + (f":{parsed.port}" if parsed.port else ""),
        "organization": org,
        "projects": dict(sorted(projects.items())),
        "query": raw.get("query", "is:unresolved"),
        "period": raw.get("period", "14d"),
        "per_project": raw.get("per_project", 100),
        "max_pages": raw.get("max_pages", 2),
        "token_file": raw.get("token_file", "~/.config/babysit-pr/sentry-token"),
        "writeback": raw.get("writeback", True),
        "llm": {
            "agent": llm.get("agent", "claude"),
            "command": llm.get("command", "claude"),
            "model": llm.get("model", "sonnet"),
            "triage": triage.get("enabled", True),
            "sanitizer": (llm.get("sanitizer") or {}).get("enabled", True),
            "per_refresh": triage.get("per_refresh", 5),
            "per_day": llm.get("per_day", triage.get("per_day", 40)),
            "quota": {
                "min_left_percent": quota.get("min_left_percent", 30),
                "windows": quota.get("windows", ["five_hour", "seven_day"]),
                "when_unknown": quota.get("when_unknown", "fixed_caps"),
            },
        },
    }
    checks = [
        (isinstance(config["query"], str) and 0 < len(config["query"]) <= 500, "query"),
        (config["period"] in PERIODS, "period (24h, 7d, 14d, 30d or 90d)"),
        (
            isinstance(config["per_project"], int) and 1 <= config["per_project"] <= 100,
            "per_project (1-100)",
        ),
        (isinstance(config["max_pages"], int) and 1 <= config["max_pages"] <= 3, "max_pages (1-3)"),
        (isinstance(config["token_file"], str) and config["token_file"], "token_file"),
        (isinstance(config["writeback"], bool), "writeback (true or false)"),
        (config["llm"]["agent"] == "claude", "llm.agent (only claude runs LLM jobs)"),
        (isinstance(config["llm"]["command"], str) and config["llm"]["command"], "llm.command"),
        (
            config["llm"]["model"] is None
            or (
                isinstance(config["llm"]["model"], str)
                and re.fullmatch(r"[A-Za-z0-9._:/-]{1,80}", config["llm"]["model"])
            ),
            "llm.model",
        ),
        (
            isinstance(config["llm"]["triage"], bool)
            and isinstance(config["llm"]["sanitizer"], bool),
            "llm.*.enabled",
        ),
        (
            isinstance(config["llm"]["per_refresh"], int)
            and 0 <= config["llm"]["per_refresh"] <= 50,
            "llm.triage.per_refresh (0-50)",
        ),
        (
            isinstance(config["llm"]["per_day"], int) and 0 <= config["llm"]["per_day"] <= 1000,
            "llm.per_day (0-1000)",
        ),
        (
            isinstance(quota.get("min_left_percent", 30), int | float)
            and 0 <= config["llm"]["quota"]["min_left_percent"] <= 100,
            "llm.quota.min_left_percent (0-100)",
        ),
        (
            isinstance(config["llm"]["quota"]["windows"], list)
            and all(
                w in {"five_hour", "seven_day", "seven_day_sonnet", "seven_day_opus"}
                for w in config["llm"]["quota"]["windows"]
            ),
            "llm.quota.windows",
        ),
        (
            config["llm"]["quota"]["when_unknown"] in {"fixed_caps", "pause"},
            "llm.quota.when_unknown (fixed_caps or pause)",
        ),
    ]
    for ok, name in checks:
        if not ok:
            raise ValueError(f"Invalid experiment setting: {name}")
    if len(config["projects"]) * config["max_pages"] + 3 > MAX_REQUESTS:
        raise ValueError(
            f"Too many projects for max_pages={config['max_pages']}: a refresh may make at most "
            f"{MAX_REQUESTS} Sentry requests; lower max_pages or map fewer projects"
        )
    mcp_token = raw.get("mcp_token_file")
    if mcp_token is not None and not (isinstance(mcp_token, str) and mcp_token):
        raise ValueError("Invalid experiment setting: mcp_token_file")
    config["mcp_token_file"] = mcp_token
    return config


def read_token(config, purpose="api"):
    """SENTRY_ACCESS_TOKEN, else a private token file; never logged or sent to browsers.

    Agents (purpose "mcp") use `mcp_token_file` when configured, so they can hold a
    read-only token while the dashboard keeps the one that can write back links.
    """
    token = "" if purpose == "mcp" and config.get("mcp_token_file") else None
    token = os.environ.get("SENTRY_ACCESS_TOKEN", "").strip() if token is None else token
    if not token:
        name = config.get("mcp_token_file") if purpose == "mcp" else None
        path = Path(name or config["token_file"]).expanduser()
        info = path.stat()
        if info.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise ValueError(f"Sentry token file {path} must not be readable by others (chmod 600)")
        token = path.read_text().strip()
    if not re.fullmatch(r"[A-Za-z0-9_=+/.-]{20,512}", token):
        raise ValueError("Sentry token is missing or malformed")
    return token


class Api:
    """Bounded Sentry REST client; errors never include the token."""

    def __init__(self, host, token, opener=urllib.request.urlopen, max_requests=MAX_REQUESTS):
        self.host = host
        self.token = token
        self.opener = opener
        self.max_requests = max_requests
        self.calls = 0

    def request(self, path, params=None, method="GET", body=None):
        if self.calls >= self.max_requests:
            raise ValueError("Sentry request budget exhausted for this refresh")
        self.calls += 1
        url = path if path.startswith(self.host + "/") else f"{self.host}/api/0/{path}"
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params, doseq=True)
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
                **({"Content-Type": "application/json"} if data else {}),
            },
        )
        try:
            with self.opener(request, timeout=30) as response:
                payload = response.read(MAX_RESPONSE + 1)
                headers = dict(response.headers)
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise ValueError("Sentry rejected the access token (HTTP 401)") from None
            raise ValueError(f"Sentry answered HTTP {exc.code} for {path.split('?')[0]}") from None
        except (OSError, ValueError) as exc:
            raise ValueError(f"Sentry is unreachable: {type(exc).__name__}") from None
        if len(payload) > MAX_RESPONSE:
            raise ValueError("Sentry response exceeded its size limit")
        return (json.loads(payload) if payload else None), headers

    @staticmethod
    def next_page(headers):
        for part in (headers.get("Link") or headers.get("link") or "").split(","):
            if 'rel="next"' in part and 'results="true"' in part:
                match = re.search(r"<([^>]+)>", part)
                if match:
                    return match.group(1)
        return None


def normalize(value):
    value = re.sub(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
        "<uuid>",
        value,
    )
    value = re.sub(r"\b[0-9a-fA-F]{8,}\b", "<hex>", value)
    value = re.sub(r"'[^']*'|\"[^\"]*\"", "<str>", value)
    value = re.sub(r"\d+", "<n>", value)
    return re.sub(r"\s+", " ", value).strip()[:200]


def when(value):
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def score(group, now):
    """Deterministic points with the reasons behind them.

    Nearly every issue on the busy Galaxy servers is `priority: high`, so priority
    only counts when it is lower; substatus, reach and recency separate issues.
    """
    points, reasons = 0, []

    def add(value, reason):
        nonlocal points
        points += value
        reasons.append(reason)

    if group["priority"] == "medium":
        add(-1, "medium priority")
    elif group["priority"] == "low":
        add(-3, "low priority")
    servers = len({project["slug"] for project in group["projects"]})
    if servers >= 3:
        add(3, f"{servers} servers")
    elif servers == 2:
        add(1, "2 servers")
    if group["level"] == "fatal":
        add(3, "fatal")
    elif group["level"] == "error":
        add(2, "error")
    if group["unhandled"]:
        add(2, "unhandled")
    if group["substatus"] == "escalating":
        add(3, "escalating")
    elif group["substatus"] == "regressed":
        add(2, "regressed")
    first, last = when(group["first_seen"]), when(group["last_seen"])
    if first and now - first < timedelta(hours=24):
        add(1, "new today")
    users = group["users"]
    if users >= 100:
        add(3, f"{users} users")
    elif users >= 10:
        add(2, f"{users} users")
    elif users >= 1:
        add(1, f"{users} user" + ("s" if users > 1 else ""))
    events = group["events_24h"]
    if events >= 1000:
        add(2, f"{events} events/24h")
    elif events >= 100:
        add(1, f"{events} events/24h")
    if last and now - last > timedelta(days=7):
        add(-2, "quiet for 7+ days")
    bucket = next(name for name, floor in BUCKETS if points >= floor)
    return points, bucket, reasons


def number(value):
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def issue_record(issue, slug):
    meta = issue.get("metadata") if isinstance(issue.get("metadata"), dict) else {}
    stats = (issue.get("stats") or {}).get("24h") or []
    try:
        events_24h = sum(int(point[1]) for point in stats if isinstance(point, list | tuple))
    except (TypeError, ValueError, IndexError):
        events_24h = 0
    count = number(issue.get("count"))
    return {
        "id": str(issue.get("id", "")),
        "slug": slug,
        "short_id": str(issue.get("shortId", "")),
        "permalink": issue.get("permalink")
        if str(issue.get("permalink", "")).startswith("https://")
        else None,
        "title": str(issue.get("title") or "")[:300],
        "culprit": str(issue.get("culprit") or "")[:300],
        "type": str(meta.get("type") or "")[:120],
        "value": str(meta.get("value") or meta.get("title") or issue.get("title") or ""),
        "level": issue.get("level") if issue.get("level") in LEVELS else "error",
        "priority": issue.get("priority") if issue.get("priority") in PRIORITIES else None,
        "status": str(issue.get("status") or ""),
        "substatus": issue.get("substatus") if issue.get("substatus") in SUBSTATUS else None,
        "unhandled": issue.get("isUnhandled") is True,
        "first_seen": issue.get("firstSeen"),
        "last_seen": issue.get("lastSeen"),
        "count": count,
        "users": number(issue.get("userCount")),
        "events_24h": events_24h,
    }


def group_issues(records, repos, now, previous=None):
    """Group by (repo, type, normalized value, culprit).

    `previous` maps Sentry issue IDs to last refresh's group keys. A group keeps its old
    key when Sentry edits a culprit or message, so its triage, publish record and
    workspace stay attached.
    """
    previous = previous or {}
    groups: dict[str, dict] = {}
    for record in records:
        repo = repos[record["slug"]]
        identity = "\0".join(
            [repo.lower(), record["type"], normalize(record["value"]), record["culprit"]]
        )
        key = hashlib.sha1(identity.encode()).hexdigest()[:16]
        groups.setdefault(key, {"key": key, "repo": repo, "members": []})["members"].append(record)
    result = []
    claimed: set[str] = set()
    ordered = sorted(groups.values(), key=lambda g: -sum(m["count"] for m in g["members"]))
    for group in ordered:
        members = sorted(group["members"], key=lambda m: (-m["count"], m["slug"]))
        lead = members[0]
        for member in members:
            old = previous.get(member["id"])
            if old and old not in claimed:
                group["key"] = old
                break
        claimed.add(group["key"])
        value = {
            "key": group["key"],
            "repo": group["repo"],
            "title": lead["title"],
            "culprit": lead["culprit"],
            "type": lead["type"],
            "level": max((m["level"] for m in members), key=LEVELS.__getitem__),
            "priority": max(
                (m["priority"] for m in members if m["priority"]),
                key=PRIORITIES.__getitem__,
                default=None,
            ),
            "status": lead["status"],
            "substatus": max(
                (m["substatus"] for m in members if m["substatus"]),
                key=SUBSTATUS.__getitem__,
                default=None,
            ),
            "unhandled": any(m["unhandled"] for m in members),
            "first_seen": min((m["first_seen"] for m in members if m["first_seen"]), default=None),
            "last_seen": max((m["last_seen"] for m in members if m["last_seen"]), default=None),
            "count": sum(m["count"] for m in members),
            "users": sum(m["users"] for m in members),
            "events_24h": sum(m["events_24h"] for m in members),
            "short_id": lead["short_id"],
            "permalink": lead["permalink"],
            "projects": [
                {k: m[k] for k in ("slug", "short_id", "id", "permalink", "count", "users")}
                | {"release": m.get("release")}
                for m in members
            ],
        }
        value["score"], value["bucket"], value["reasons"] = score(value, now)
        result.append(value)
    result.sort(key=lambda g: (g["score"], g["last_seen"] or ""), reverse=True)
    return result[:MAX_GROUPS]


def collect(config, api, now, project_ids=None, previous=None):
    """One observation of every configured project's recent issues."""
    warnings = []
    ids = dict(project_ids or {})
    if not all(slug in ids for slug in config["projects"]):
        ids = {}
        page, headers = api.request(f"organizations/{config['organization']}/projects/")
        for _ in range(3):
            ids.update(
                {p["slug"]: str(p["id"]) for p in page or [] if isinstance(p, dict) and "slug" in p}
            )
            following = api.next_page(headers)
            if not following or all(slug in ids for slug in config["projects"]):
                break
            page, headers = api.request(following)
    records, summary = [], []
    for slug, repo in config["projects"].items():
        if slug not in ids:
            warnings.append(f"{slug}: no such Sentry project in {config['organization']}")
            continue
        found: list[dict] = []
        truncated = False
        page, headers = api.request(
            f"organizations/{config['organization']}/issues/",
            {
                "project": ids[slug],
                "query": config["query"],
                "statsPeriod": config["period"],
                "sort": "freq",
                "limit": config["per_project"],
            },
        )
        for pages in range(config["max_pages"]):
            found.extend(issue_record(i, slug) for i in page or [] if isinstance(i, dict))
            following = api.next_page(headers)
            if not following:
                break
            if pages + 1 == config["max_pages"]:
                truncated = True
                break
            page, headers = api.request(following)
        if truncated:
            warnings.append(f"{slug}: showing the {len(found)} most frequent issues; more exist")
        records.extend(found)
        summary.append({"slug": slug, "repo": repo, "issues": len(found), "truncated": truncated})
    return (
        {
            "groups": group_issues(records, config["projects"], now, previous),
            "projects": summary,
            "warnings": warnings,
            "requests": api.calls,
        },
        ids,
    )


def fingerprint(group):
    """What makes a triage stale: status, order of magnitude of volume, reach."""
    magnitude = len(str(max(group["count"], 1)))
    servers = len({project["slug"] for project in group["projects"]})
    return f"{group['substatus']}|{magnitude}|{servers}|{group['priority']}"


def disagrees(bucket, severity):
    order = [name for name, _ in BUCKETS]
    return abs(order.index(bucket) - order.index(severity)) >= 2


class SentryIssues:
    """Lazy, single-flight refresh with its own cache, queue, and failure boundary."""

    def __init__(self, home: Path, opener=urllib.request.urlopen, gh=github_cli.run):
        self.home = Path(home)
        self.directory = self.home / "experiments" / "sentry"
        self.opener = opener
        self.gh = gh
        self.lock = threading.Lock()
        self.publish_lock = threading.RLock()
        self.next_poll = 0.0
        self.last_manual = 0.0
        self.loading = False
        self.value: dict = {}
        self.project_ids: dict = {}
        self.config: dict = {}
        self.error = None
        self.enabled = False
        self.store: sentry_llm.Store | None = None
        try:
            config = load_config(self.directory)
            if not config:
                return
            self.config = config
            self.enabled = config["enabled"]
            if not self.enabled:
                return
            self.store = sentry_llm.Store(self.directory)
            cache_path = self.directory / "cache.json"
            if cache_path.exists() and cache_path.stat().st_size <= MAX_STATE:
                cache = json.loads(cache_path.read_text())
                if cache.get("version") == CACHE_VERSION and cache.get("config") == self.digest():
                    self.value = cache["value"]
                    self.project_ids = cache.get("project_ids", {})
                    self.next_poll = self.value.get("synced_at", 0) + INTERVAL
        except Exception as exc:
            self.error = f"Experiment configuration/cache unavailable: {exc}"

    @property
    def queue(self) -> sentry_llm.Store:
        if self.store is None:
            raise ValueError("The Sentry experiment is disabled")
        return self.store

    def digest(self):
        scope = {k: self.config[k] for k in ("host", "organization", "projects", "query", "period")}
        return hashlib.sha1(json.dumps(scope, sort_keys=True).encode()).hexdigest()

    def api(self, max_requests=MAX_REQUESTS):
        return Api(self.config["host"], read_token(self.config), self.opener, max_requests)

    # -- snapshot --

    def request_refresh(self):
        with self.lock:
            now = time.time()
            if now - self.last_manual >= MANUAL_INTERVAL:
                self.last_manual = now
                self.next_poll = min(self.next_poll, now)

    def snapshot(self):
        with self.lock:
            if self.enabled and not self.loading and time.time() >= self.next_poll:
                self.loading = True
                self.next_poll = time.time() + INTERVAL
                threading.Thread(target=self._refresh, daemon=True).start()
            value = copy.deepcopy(self.value)
            state = {
                "enabled": self.enabled,
                "loading": self.loading,
                "error": self.error,
                "stale": bool(
                    value and (self.error or time.time() - value.get("synced_at", 0) >= INTERVAL)
                ),
            }
        value.setdefault("groups", [])
        value.update(state)
        if self.enabled:
            value.update(host=self.config["host"], organization=self.config["organization"])
            self.decorate(value)
        return value

    def decorate(self, value):
        """Attach queue, publish, and worker state; the LLM store is optional to reads."""
        if not self.store:
            return
        store = self.store
        try:
            triage = store.latest("triage")
            publishes = store.publish()
            for group in value["groups"]:
                job = triage.get(group["key"])
                group["triage"] = self.triage_view(job, group) if job else None
                record = publishes.get(group["key"])
                if record and record["status"] in {"sanitizing", "creating"}:
                    record = self.sync_publish(group["key"])
                group["publish"] = self.publish_view(record) if record else None
            now = time.time()
            worker = store.meta("worker") or {}
            gate = store.meta("gate") or {
                "state": "unknown",
                "reason": "Not checked yet",
                "windows": [],
                "checked_at": None,
            }
            llm = self.config["llm"]
            value["llm"] = {
                "worker": {
                    "alive": now - worker.get("heartbeat_at", 0) < WORKER_STALE,
                    "heartbeat_at": worker.get("heartbeat_at"),
                },
                "gate": gate,
                "budget": {
                    "per_refresh": llm["per_refresh"],
                    "per_day": llm["per_day"],
                    "used_today": store.calls_since(sentry_llm.midnight(now)),
                },
                "triage_enabled": llm["triage"],
                "sanitizer_enabled": llm["sanitizer"],
            }
        except Exception as exc:
            value.setdefault("warnings", []).append(f"LLM queue unavailable: {exc}")

    @staticmethod
    def triage_view(job, group):
        view = {"status": job["status"], "error": job["error"], "updated_at": job["updated"]}
        if job["status"] == "done" and isinstance(job["output"], dict):
            view.update(job["output"])
            view["disagrees"] = disagrees(group["bucket"], job["output"]["severity"])
        return view

    @staticmethod
    def publish_view(record):
        return {
            k: record.get(k)
            for k in (
                "status",
                "existing",
                "repo_public",
                "draft",
                "sanitized",
                "findings",
                "url",
                "number",
                "writeback",
                "error",
                "edited",
            )
        }

    def _refresh(self):
        try:
            with self.lock:
                previous = {
                    project["id"]: group["key"]
                    for group in self.value.get("groups", [])
                    for project in group["projects"]
                }
            value, ids = collect(
                self.config, self.api(), datetime.now(UTC), self.project_ids, previous
            )
            value["synced_at"] = time.time()
            self.write_cache(value, ids)
            with self.lock:
                self.value, self.project_ids, self.error = value, ids, None
            self.queue_triage(value["groups"])
        except Exception as exc:
            with self.lock:
                self.error = f"Sentry refresh failed: {exc}"
        finally:
            with self.lock:
                self.loading = False

    def write_cache(self, value, ids):
        payload = json.dumps(
            {"version": CACHE_VERSION, "config": self.digest(), "value": value, "project_ids": ids}
        )
        if len(payload.encode()) > MAX_STATE:
            raise ValueError("Experiment cache size limit exceeded")
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", dir=self.directory, suffix=".tmp", delete=False
        ) as stream:
            temporary = Path(stream.name)
            try:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
                temporary.replace(self.directory / "cache.json")
            finally:
                temporary.unlink(missing_ok=True)

    def queue_triage(self, groups):
        llm = self.config["llm"]
        if not llm["triage"] or not llm["per_refresh"] or not self.store:
            return
        latest = self.queue.latest("triage")
        queued = 0
        for group in groups:
            if queued >= llm["per_refresh"]:
                break
            job = latest.get(group["key"])
            if job and (
                job["status"] in {"queued", "running"} or job["fingerprint"] == fingerprint(group)
            ):
                continue
            self.queue.enqueue("triage", group["key"], triage_input(group), fingerprint(group))
            queued += 1

    # -- targets for Handle --

    def targets(self):
        with self.lock:
            groups = copy.deepcopy(self.value.get("groups", []))
        return [
            {
                "id": f"sentry:{g['key']}",
                "kind": "sentry",
                "repo": g["repo"],
                "title": g["title"],
                "short_id": g["short_id"],
                "url": g["permalink"],
                "culprit": g["culprit"],
                "projects": g["projects"],
                "count": g["count"],
                "users": g["users"],
                "first_seen": g["first_seen"],
                "last_seen": g["last_seen"],
            }
            for g in groups
            if g.get("short_id")
        ]

    def handle_mcp_config(self):
        """A private, read-only MCP config for Handle's Claude agents; never global config."""
        path = self.directory / "mcp.json"
        return sentry_llm.write_private(
            path, sentry_llm.mcp_document(self.config, ["--skills=inspect"])
        )

    # -- actions --

    def group(self, key):
        with self.lock:
            group = next((g for g in self.value.get("groups", []) if g["key"] == key), None)
        if not isinstance(key, str) or group is None:
            raise ValueError("Unknown Sentry issue group; refresh the dashboard")
        return copy.deepcopy(group)

    def action(self, request):
        if not self.enabled or not self.store:
            raise ValueError("The Sentry experiment is disabled")
        allowed = {"action", "key", "title", "body"}
        if set(request) - allowed or not all(isinstance(request.get(f, ""), str) for f in allowed):
            raise ValueError("Invalid Sentry action parameters")
        group = self.group(request.get("key"))
        action = request.get("action")
        if action == "retriage":
            if not self.config["llm"]["triage"]:
                raise ValueError("LLM triage is disabled in the experiment configuration")
            self.queue.enqueue(
                "triage", group["key"], triage_input(group), fingerprint(group), force=True
            )
        elif action == "publish-draft":
            self.publish_draft(group)
        elif action == "publish-create":
            self.publish_create(group, request.get("title", ""), request.get("body", ""))
        elif action == "writeback-retry":
            self.writeback_retry(group)
        else:
            raise ValueError("Unknown Sentry action")
        snapshot = self.snapshot()
        # A refresh may have dropped the group meanwhile; the action still happened.
        return {"group": next((g for g in snapshot["groups"] if g["key"] == group["key"]), group)}

    # -- publishing --

    def gh_json(self, *args):
        result = self.gh(["gh", *args], timeout=60, text=True)
        if result.returncode:
            raise ValueError(
                f"gh {args[0]} failed: {(result.stderr or result.stdout).strip()[-300:]}"
            )
        return json.loads(result.stdout or "null")

    def existing_issue(self, group):
        found = self.gh_json(
            "issue",
            "list",
            "-R",
            group["repo"],
            "--state",
            "all",
            "--search",
            f'"{group["short_id"]}" in:body',
            "--json",
            "number,url",
            "--limit",
            "5",
        )
        return (
            found[0] if isinstance(found, list) and found and isinstance(found[0], dict) else None
        )

    def latest_event(self, group):
        lead = group["projects"][0]
        event, _ = self.api(4).request(
            f"organizations/{self.config['organization']}/issues/{lead['id']}/events/latest/"
        )
        frames, message = [], ""
        for entry in (event or {}).get("entries", []):
            if entry.get("type") != "exception":
                continue
            values = (entry.get("data") or {}).get("values") or []
            if values:
                message = str(values[-1].get("value") or "")[:500]
            for value in values:
                for frame in (value.get("stacktrace") or {}).get("frames") or []:
                    if frame.get("inApp"):
                        where = frame.get("module") or frame.get("filename") or "?"
                        frames.append(
                            f"{where}:{frame.get('function') or '?'}:{frame.get('lineNo') or '?'}"
                        )
        return frames[-8:], message

    def draft(self, group, frames):
        rows = "\n".join(
            f"| {p['slug']} | [{p['short_id']}]({p['permalink']}) | {p['count']} | {p['users']} |"
            for p in group["projects"]
            if p.get("permalink")
        )
        status = ", ".join(
            filter(None, [group["substatus"], "unhandled" if group["unhandled"] else ""])
        )
        body = [
            f"Reported by Sentry as **{group['short_id']}**"
            + (f" and {len(group['projects']) - 1} more" if len(group["projects"]) > 1 else "")
            + ".",
            "",
            "| Server | Sentry issue | Events | Users |",
            "| --- | --- | --- | --- |",
            rows,
            "",
            f"- **Level:** {group['level']}"
            + (f" · **Priority:** {group['priority']}" if group["priority"] else ""),
            f"- **Status:** {status or group['status']}",
            f"- **First seen:** {group['first_seen']} · **Last seen:** {group['last_seen']}",
            f"- **Events in the last 24 h:** {group['events_24h']}",
        ]
        if group["culprit"]:
            body.append(f"- **Culprit:** `{group['culprit']}`")
        if frames:
            body += ["", "### In-app frames (most recent call last)", "", "```", *frames, "```"]
        body += ["", f"<sub>Sentry: {group['short_id']}</sub>"]
        return {"title": group["title"][:120], "body": "\n".join(body)}

    def publish_draft(self, group):
        if not self.config["llm"]["sanitizer"]:
            raise ValueError("Publishing requires the LLM sanitizer, which is disabled")
        link = False
        with self.publish_lock:
            record = self.sync_publish(group["key"])
            if record and record["status"] in {"sanitizing", "creating", "created"}:
                return
            # After an interrupted create the issue may exist: adopt it rather than redraft.
            existing = self.existing_issue(group)
            base = {
                "repo": group["repo"],
                "existing": existing,
                "draft": None,
                "sanitized": None,
                "findings": [],
                "url": None,
                "number": None,
                "writeback": "pending" if self.config["writeback"] else "disabled",
                "written": [],
                "error": None,
                "updated_at": time.time(),
            }
            if existing:
                # Linking an existing issue needs no sanitizer run and no new issue.
                self.queue.set_publish(
                    group["key"],
                    {
                        **base,
                        "status": "created",
                        "repo_public": None,
                        "url": existing.get("url"),
                        "number": existing.get("number"),
                    },
                )
                link = self.config["writeback"]
            else:
                visibility = self.gh_json("repo", "view", group["repo"], "--json", "visibility")
                frames, message = self.latest_event(group)
                draft = self.draft(group, frames)
                job = self.queue.enqueue(
                    "sanitize",
                    group["key"],
                    {"draft": draft, "context": {"error_message": message, "frames": frames}},
                    force=True,
                )
                self.queue.set_publish(
                    group["key"],
                    {
                        **base,
                        "status": "sanitizing",
                        "job": job["id"],
                        "repo_public": (visibility or {}).get("visibility") != "PRIVATE",
                        "draft": draft,
                    },
                )
        if link:
            self.writeback(group)

    def sync_publish(self, key, record=None):
        """Advance a record from its sanitizer job; under the lock so no write is lost."""
        with self.publish_lock:
            record = self.queue.publish(key)
            if not record:
                return None
            if (
                record["status"] == "creating"
                and time.time() - record.get("updated_at", 0) > CREATE_TIMEOUT
            ):
                record.update(
                    status="uncertain",
                    error="Creating the GitHub issue was interrupted; it may exist. "
                    "Try again to link it or draft again.",
                )
            elif record["status"] == "sanitizing":
                job = self.queue.get(record.get("job"))
                if job and job["status"] == "done":
                    sanitized = job["output"]
                    found = findings(
                        f"{sanitized['title']}\n{sanitized['body']}", self.config["host"]
                    )
                    ready = sanitized["safe_to_publish"] and not found
                    record.update(
                        sanitized=sanitized, findings=found, status="ready" if ready else "blocked"
                    )
                    if not ready:
                        record["error"] = (
                            "The sanitizer was not confident the text is safe to publish"
                            if not sanitized["safe_to_publish"]
                            else "The pattern check found text that must not be published"
                        )
                elif not job or job["status"] == "failed":
                    record.update(
                        status="failed",
                        error=(job or {}).get("error") or "Sanitizer job is missing",
                    )
                else:
                    return record
            else:
                return record
            record["updated_at"] = time.time()
            self.queue.set_publish(key, record)
            return record

    def publish_create(self, group, title, body):
        title, body = title.strip(), body.strip()
        if not 0 < len(title) <= 200 or not 0 < len(body) <= 60000 or "\0" in title + body:
            raise ValueError("Supply a title of 1-200 and a body of 1-60,000 characters")
        with self.publish_lock:
            record = self.sync_publish(group["key"])
            if not record or record["status"] != "ready":
                raise ValueError("Review a sanitized draft before creating the GitHub issue")
            found = findings(f"{title}\n{body}", self.config["host"])
            if found:
                kinds = ", ".join(sorted({f["kind"] for f in found}))
                raise ValueError(
                    f"The edited text contains data that must not be published ({kinds})"
                )
            sanitized = record["sanitized"] or {}
            existing = self.existing_issue(group)
            if existing:
                record.update(
                    status="created",
                    existing=existing,
                    url=existing.get("url"),
                    number=existing.get("number"),
                )
            else:
                if group["short_id"] not in body:
                    body += f"\n\n<sub>Sentry: {group['short_id']}</sub>"
                # Recorded before the call: a crash or timeout leaves `creating`, which
                # becomes `uncertain` and is resolved by searching, never by re-creating.
                record.update(
                    status="creating",
                    edited=(title, body) != (sanitized.get("title"), sanitized.get("body")),
                    updated_at=time.time(),
                )
                self.queue.set_publish(group["key"], record)
                try:
                    created = self.gh_json(
                        "api",
                        f"repos/{group['repo']}/issues",
                        "-X",
                        "POST",
                        "-f",
                        f"title={title}",
                        "-f",
                        f"body={body}",
                    )
                except ValueError as exc:
                    # gh reported a failure: nothing was created.
                    record.update(status="ready", error=f"GitHub issue was not created: {exc}")
                    self.queue.set_publish(group["key"], record)
                    raise ValueError(record["error"]) from None
                except Exception:
                    record.update(
                        status="uncertain",
                        error="Creating the GitHub issue did not finish; it may exist. "
                        "Try again to link it or draft again.",
                    )
                    self.queue.set_publish(group["key"], record)
                    raise ValueError(record["error"]) from None
                record.update(url=created.get("html_url"), number=created.get("number"))
                record["status"] = "created"
            record.update(error=None, updated_at=time.time())
            self.queue.set_publish(group["key"], record)
        if self.config["writeback"]:
            self.writeback(group)

    def writeback(self, group):
        """Comment the link on each Sentry issue not yet linked; safe to repeat."""
        with self.publish_lock:
            record = self.queue.publish(group["key"])
            if not record or record["status"] != "created" or not record.get("url"):
                return
            written = set(record.get("written") or [])
            try:
                api = self.api(12)
                for project in group["projects"][:10]:
                    if project["id"] in written:
                        continue
                    api.request(
                        f"issues/{project['id']}/comments/",
                        method="POST",
                        body={"text": f"GitHub issue: {record['url']}"},
                    )
                    written.add(project["id"])
                record.update(writeback="ok", error=None)
            except Exception as exc:
                record.update(writeback="failed", error=f"Link not written to Sentry: {exc}")
            record["written"] = sorted(written)
            self.queue.set_publish(group["key"], record)

    def writeback_retry(self, group):
        record = self.queue.publish(group["key"])
        if not record or record["status"] != "created" or record.get("writeback") != "failed":
            raise ValueError("Nothing to write back for this issue")
        self.writeback(group)


def triage_input(group):
    return {
        k: group.get(k)
        for k in (
            "key",
            "repo",
            "title",
            "culprit",
            "type",
            "level",
            "priority",
            "substatus",
            "unhandled",
            "first_seen",
            "last_seen",
            "count",
            "users",
            "events_24h",
            "short_id",
            "permalink",
            "projects",
            "score",
            "bucket",
            "reasons",
        )
    }


def main():
    """Print the score distribution for the live configuration (threshold tuning)."""
    parser = argparse.ArgumentParser(description=main.__doc__)
    parser.add_argument("--home", type=Path, default=Path.home() / ".local/state/babysit-pr")
    args = parser.parse_args()
    plugin = SentryIssues(args.home.expanduser())
    if not plugin.enabled:
        raise SystemExit(plugin.error or "The Sentry experiment is not enabled")
    value, _ = collect(plugin.config, plugin.api(), datetime.now(UTC))
    counts: dict[str, int] = {}
    for group in value["groups"]:
        counts[group["bucket"]] = counts.get(group["bucket"], 0) + 1
    print(
        json.dumps(
            {"groups": len(value["groups"]), "buckets": counts, "warnings": value["warnings"]},
            indent=1,
        )
    )
    for group in value["groups"][:15]:
        print(
            f"{group['score']:>3} {group['bucket']:<8} {group['short_id']:<34} {', '.join(group['reasons'])}"
        )


if __name__ == "__main__":
    main()
