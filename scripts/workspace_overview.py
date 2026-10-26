"""Opt-in workspace inventory experiment with explicit, bounded cleanup.

Lists linked Git worktrees joined with their herdr workspaces and agents, resolves the
GitHub state of the pull request or issue each checkout belongs to, opens workspace
containers for existing checkouts, and performs explicitly requested cleanup: exit agents, close the herdr workspace, remove the
worktree, delete the local branch. It never starts an agent, pushes, or repairs
anything, and its collection and cleanup failures stay inside this experiment.
"""

import concurrent.futures
import copy
import fcntl
import hashlib
import json
import os
import re
import subprocess
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from urllib.parse import quote

import github_cli
import machines
import owned_process
import remote_probe
import safe_git
import workspace_viewer
from pr_workspaces import (
    PANE,
    SLUG,
    clone_worktrees,
    collie_url,
    herdr,
    remote_slugs,
    repo_config,
)

INTERVAL = 300  # Local rescan cadence; GitHub state has its own freshness window.
MIN_REFRESH = 60  # Floor between requested rescans, however often the tab asks.
LINK_TTL = 900  # Open items move; the other states are rechecked far less often.
SETTLED_TTL = 21600
NONE_TTL = 3600
BASE_TTL = 86400
CALL_BUDGET = 100
LOOKUP_BATCH = 50  # Lookups per GraphQL request; each costs GitHub one rate-limit point.
HEAD_PULLS = 20  # Pull requests read per head branch name, across every fork.
MAX_DIGITS = 9
MAX_TARGETS = 50
# Lookups the last scan used are always kept, so live entries are never evicted only
# to be fetched again; this many others survive for checkouts that may come back.
MAX_LINKS = 500
MAX_STATE = 4 * 1024 * 1024
MAX_RESPONSE = 4 * 1024 * 1024
MAX_UNPUSHED = remote_probe.MAX_UNPUSHED
# One worker's whole scan, herdr listings and probe together; slower ones are left stale.
REMOTE_TIMEOUT = 120
# A worker that failed is not asked again for twice the rescan interval, doubling with
# each further failure up to this many intervals.
MAX_BACKOFF = 16
REMOTE_GRACE = 5  # Beyond a worker's deadline, for an answer already on its way back.
HERDR_TIMEOUT = 30
# A worker's re-read of one clone, and its removal of one worktree with Git's own room.
RESOLVE_TIMEOUT = 120
REMOVE_TIMEOUT = 1200
# What one worker may contribute to a scan, and so to the cache shared with this machine.
MAX_REMOTE_CLONES = 200
MAX_REMOTE_ITEMS = 1000
MAX_REMOTE_ROWS = 300  # Checkout rows, and separately workspace rows.
MAX_REMOTE_AGENTS = 1000
MAX_REMOTE_TEXT = 4096
EXIT_WAIT = 5
CACHE_VERSION = 1
WORKING = {"working", "blocked"}
# A cleanup override approves these by kind, so a changed count is not a new blocker
# but a newly appeared condition is, and is never swept along by one tick.
OVERRIDABLE = {"main", "dirty", "unpushed", "agent", "unknown", "detached"}
# Only these approve losing what Git's clean check would keep: a working agent's
# approval must not sweep away what it writes until its workspace closes.
CLEAN_OVERRIDES = {"dirty", "unknown"}
CLOSED = {"closed", "merged"}
ISSUE_NAME = re.compile(r"^issue-(\d+)(?:-|$)")
PR_NAME = re.compile(r"^pr-([A-Za-z0-9][A-Za-z0-9-]*?)-(\d+)(?:-\d+)?$")
# A branch name cleanup deletes; one starting with `-` would read as an option.
BRANCH_NAME = remote_probe.BRANCH_NAME
BASE_BRANCH = re.compile(
    r"^(main|master|dev|develop|trunk|next|release[_-]v?\d+(\.\d+)*|\d+\.\d+)$"
)


def git(path, *args, timeout=60, strip=True):
    """Hardened Git in one checkout. Large repositories need more room than a quick query."""
    argv, env = safe_git.prepare(str(path), *args, env=github_cli.environment())
    result = owned_process.run(argv, env=env, text=True, timeout=timeout)
    if result.returncode:
        raise ValueError((result.stderr or result.stdout or "Git failed")[-4000:].strip())
    return result.stdout.strip() if strip else result.stdout


def gh_json(endpoint, payload=None, timeout=20):
    """One bounded, read-only GitHub call through the authenticated `gh`.

    A `payload` is a GraphQL query. `gh` exits non-zero when any part of one fails, yet
    still prints the partial answer, whose per-alias errors the caller sorts out.
    """
    args = ["gh", "api", "--hostname", "github.com", endpoint]
    result = github_cli.run(
        [*args, "--input", "-"] if payload is not None else args,
        input=json.dumps(payload) if payload is not None else None,
        text=True,
        timeout=timeout,
    )
    if len(result.stdout) > MAX_RESPONSE:
        raise ValueError("GitHub response exceeded byte limit")
    if result.returncode:
        if payload is not None and result.stdout.startswith("{"):
            value = json.loads(result.stdout)
            if isinstance(value.get("data"), dict):
                return value
        raise ValueError((result.stderr or result.stdout or "GitHub request failed")[-400:].strip())
    return json.loads(result.stdout)


class Budget:
    """A per-refresh ceiling on GitHub calls; exhaustion leaves links stale, not wrong."""

    def __init__(self, fetch, limit=None):
        self.fetch = fetch
        self.limit = CALL_BUDGET if limit is None else limit
        self.calls = 0

    def get(self, endpoint, payload=None):
        if self.calls >= self.limit:
            raise ValueError("GitHub request budget exhausted for this refresh")
        self.calls += 1
        return self.fetch(endpoint) if payload is None else self.fetch(endpoint, payload)


checkout_times = remote_probe.checkout_times


def session_cwd(file, agent):
    session = workspace_viewer.details(file, agent)
    return session["cwd"] if session else None


def transcript_updates(paths, src):
    """This machine's session stores, scanned once per inventory; see ``remote_probe``."""
    return remote_probe.transcript_updates(paths, src, workspace_viewer.session_files, session_cwd)


def local_state(path, branch=None):
    """Uncommitted changes and commits only this branch has, through this machine's Git."""
    return remote_probe.local_state(git, path, branch)


def candidates(item, base, origin_owner):
    """Every GitHub item this checkout may belong to, from Git provenance and wt naming.

    Names are a hint the repository must agree with: a `pr-<owner>-<number>` directory
    only names a pull request when `<owner>` owns the resolved base repository. Tracking
    configuration can name the *base* branch a topic branch was cut from, so base
    branches are excluded. Other upstreams name the PR head even when a checkout
    has a different local name (for example, `review-pr-24342`).
    """
    found = []
    branch = item.get("branch") or ""
    directory = Path(item["path"]).name
    upstream = item.get("upstream") or [None, None]
    if branch and not BASE_BRANCH.match(branch):
        if upstream[0] and upstream[1] and not BASE_BRANCH.match(upstream[1]):
            found.append(("head", f"{upstream[0].split('/')[0]}:{upstream[1]}"))
        if origin_owner:
            found.append(("head", f"{origin_owner}:{branch}"))
    # A number past GraphQL's 32-bit Int would fail every lookup batched with it.
    for name in dict.fromkeys([branch, directory]):
        issue = ISSUE_NAME.match(name)
        if issue and len(issue[1]) <= MAX_DIGITS:
            found.append(("issue", issue[1]))
        pull = PR_NAME.match(name)
        if (
            pull
            and len(pull[2]) <= MAX_DIGITS
            and base
            and pull[1].lower() == base.split("/")[0].lower()
        ):
            found.append(("pr", pull[2]))
    return list(dict.fromkeys(found))


def link_record(kind, value, base):
    """Normalize one REST payload into the displayed link, with merged split out."""
    if kind == "none":
        return {
            "kind": "pr",
            "repo": base,
            "number": None,
            "title": None,
            "url": None,
            "state": "none",
            "draft": False,
        }
    pull = value.get("pull_request") if kind == "issue" else None
    # A single pull request carries `merged`; list entries and issues carry `merged_at`.
    merged = bool(value.get("merged") or value.get("merged_at") or (pull or {}).get("merged_at"))
    state = "merged" if merged else str(value.get("state") or "unknown").lower()
    return {
        "kind": "pr" if kind == "pr" or pull else "issue",
        "repo": base,
        "number": value.get("number"),
        "title": value.get("title"),
        "url": value.get("html_url"),
        "state": state,
        "draft": bool(value.get("draft")),
    }


PULL_FIELDS = "__typename number title url state isDraft headRepositoryOwner { login }"
ISSUE_FIELDS = "__typename number title url state"


def lookup_query(targets):
    """One repository's lookups as aliased GraphQL fields, each answered on its own.

    A head target reads the newest pull requests from that branch name in every fork at
    once, so all candidate owners of one branch share a single alias.
    """
    declared = ["$owner: String!", "$name: String!"]
    fields, variables = [], {}
    for index, (kind, value) in enumerate(targets):
        alias = f"a{index}"
        if kind == "head":
            declared.append(f"${alias}: String!")
            variables[alias] = value
            fields.append(
                f"{alias}: pullRequests(headRefName: ${alias}, first: {HEAD_PULLS}, "
                f"orderBy: {{field: CREATED_AT, direction: DESC}}) {{ nodes {{ {PULL_FIELDS} }} }}"
            )
        else:
            declared.append(f"${alias}: Int!")
            variables[alias] = int(value)
            fields.append(
                f"{alias}: issueOrPullRequest(number: ${alias}) {{ "
                f"... on Issue {{ {ISSUE_FIELDS} }} ... on PullRequest {{ {PULL_FIELDS} }} }}"
            )
    query = f"query({', '.join(declared)}) {{ repository(owner: $owner, name: $name) {{ {' '.join(fields)} }} }}"
    return query, variables


def graph_link(node, base):
    """A GraphQL issue or pull request through the same normalization as REST payloads."""
    pull = node.get("__typename") == "PullRequest"
    value = {
        "number": node.get("number"),
        "title": node.get("title"),
        "html_url": node.get("url"),
        "state": node.get("state"),
        "merged": node.get("state") == "MERGED",
        "draft": node.get("isDraft"),
    }
    return link_record("pr" if pull else "issue", value, base)


def fetch_links(budget, base, targets):
    """Resolve one repository's targets in one request: per target, nodes or an error.

    A head target answers with its pull request nodes, a number target with its issue
    or pull request. A failure confined to one alias, such as an unknown number, fails
    only that target; anything else fails the whole request.
    """
    owner, name = base.split("/")
    query, variables = lookup_query(targets)
    value = budget.get(
        "graphql", {"query": query, "variables": {"owner": owner, "name": name, **variables}}
    )
    errors = {}
    for error in value.get("errors") or []:
        path = error.get("path") or []
        message = str(error.get("message") or "GitHub lookup failed")[:200]
        if len(path) < 2 or path[0] != "repository":
            raise ValueError(message)
        errors[path[1]] = ValueError(message)
    repository = (value.get("data") or {}).get("repository")
    if not isinstance(repository, dict):
        raise ValueError("Unexpected GitHub lookup response")
    answers: dict[tuple, list | dict | ValueError] = {}
    for index, target in enumerate(targets):
        alias = f"a{index}"
        found = repository.get(alias)
        if alias in errors:
            answers[target] = errors[alias]
        elif (
            target[0] == "head" and isinstance(found, dict) and isinstance(found.get("nodes"), list)
        ):
            answers[target] = found["nodes"]
        elif target[0] == "number" and isinstance(found, dict):
            answers[target] = found
        else:
            answers[target] = ValueError("Unexpected GitHub lookup answer")
    return answers


def answer_link(answer, base, kind, key):
    """The link one target's answer gives this candidate; a head picks its fork owner.

    None means the answer cannot tell: a full page of other forks' pull requests from
    that branch name may push this owner's off the end, so absence proves nothing.
    """
    if kind != "head":
        return graph_link(answer, base)
    owner = key.partition(":")[0].lower()
    nodes = [node for node in answer if isinstance(node, dict)]
    found = [
        node
        for node in nodes
        if ((node.get("headRepositoryOwner") or {}).get("login") or "").lower() == owner
    ]
    if found:
        return graph_link(max(found, key=lambda node: node.get("number") or 0), base)
    return None if len(answer) >= HEAD_PULLS else link_record("none", {}, base)


def fetch_head(budget, base, key):
    """One fork owner's pull requests from a branch, filtered by GitHub itself."""
    owner, _, branch = key.partition(":")
    found = budget.get(
        f"repos/{base}/pulls?state=all&per_page=5&sort=created&direction=desc"
        f"&head={quote(owner, safe='')}:{quote(branch, safe='')}"
    )
    if not isinstance(found, list):
        raise ValueError("Unexpected pull request list")
    if not found:
        return link_record("none", {}, base)
    return link_record("pr", max(found, key=lambda pr: pr.get("number") or 0), base)


def blocker(kind, text):
    return {"kind": kind, "text": text}


def link_ttl(link):
    if link["state"] in CLOSED:
        return SETTLED_TTL
    return NONE_TTL if link["state"] == "none" else LINK_TTL


def row_status(row):
    if row["blockers"]:
        return "blocked" if row["removable"] else "protected"
    links = [link for link in row["links"] if link["state"] not in {"none", "unknown", "error"}]
    if not links:
        return "unlinked"
    return "ready" if all(link["state"] in CLOSED for link in links) else "active"


class WorkspaceOverview:
    """Lazy, single-flight inventory with its own bounded cache and cleanup worker."""

    def __init__(self, home, fetch=gh_json, src=None, jobs=None):
        self.home = Path(home)
        self.directory = self.home / "experiments" / "workspaces"
        self.fetch = fetch
        self.jobs: Callable[[], list] = jobs or (lambda: [])
        self.lock = threading.Lock()
        self.next_poll = 0.0
        self.loading = False
        self.enabled = False
        self.error = None
        self.config: dict = {}
        self.links: dict = {}
        self.bases: dict = {}
        self.live: set[str] = set()  # Link cache keys the latest scan looked up.
        self.job: dict | None = None
        self.value: dict = {"workspaces": [], "warnings": [], "synced_at": None}
        self.partial: dict | None = None
        self.src = Path(src).expanduser() if src else Path.home() / "src"
        self.roots: list[Path] = []
        # Worker machine ids whose failed scan is not retried before this time, and how
        # many scans in a row each has failed.
        self.retry: dict[str, float] = {}
        self.failures: dict[str, int] = {}
        try:
            config_path = self.directory / "config.json"
            if not config_path.exists():
                return
            if config_path.stat().st_size > 65536:
                raise ValueError("Experiment configuration exceeds size limit")
            self.config = json.loads(config_path.read_text())
            self.enabled = self.config.get("enabled") is True
            if src is None and isinstance(self.config.get("src"), str):
                self.src = Path(self.config["src"]).expanduser()
            self.roots = [Path(p).expanduser() for p in self.config.get("roots") or []]
            cache_path = self.directory / "cache.json"
            if self.enabled and cache_path.exists() and cache_path.stat().st_size <= MAX_STATE:
                cache = json.loads(cache_path.read_text())
                if cache.get("version") == CACHE_VERSION:
                    self.links = cache.get("links") or {}
                    self.bases = cache.get("bases") or {}
                    value = cache.get("value")
                    if (
                        cache.get("scope") == self.inventory_scope()
                        and isinstance(value, dict)
                        and isinstance(value.get("workspaces"), list)
                        and isinstance(value.get("warnings"), list)
                        and isinstance(value.get("synced_at"), (int, float))
                        and 0 < value["synced_at"] <= time.time()
                    ):
                        self.value = value
                        self.next_poll = value["synced_at"] + INTERVAL
        except Exception as exc:
            self.error = f"Experiment configuration/cache unavailable: {exc}"

    # Inventory -----------------------------------------------------------------

    def inventory_scope(self):
        scope: dict = {
            "src": str(self.src.resolve()),
            "roots": sorted({str(root.resolve()) for root in self.roots}),
        }
        # A registry change discards the cached rows; without one the scope is as before.
        if workers := sorted(machine.id for machine in self.remote_machines()):
            scope["machines"] = workers
        return scope

    def remote_machines(self, warnings=None):
        """Enabled worker machines, scanned beside this one; none without a registry."""
        try:
            return [machine for machine in machines.load(self.home).values() if machine.enabled]
        except (OSError, ValueError) as exc:
            if warnings is not None:
                warnings.append(f"Machine registry unavailable; scanning this machine only: {exc}")
            return []

    def snapshot(self, start=True):
        """The inventory as it is; `start=False` reads it without bringing a scan forward."""
        with self.lock:
            if start and self.enabled and not self.loading and time.time() >= self.next_poll:
                self.loading = True
                self.next_poll = time.time() + INTERVAL
                threading.Thread(target=self._refresh, daemon=True).start()
            # Shallow: rows are never changed once listed; refreshes replace the value and
            # publish_row() replaces the partial list. The cleanup job changes in place.
            return {
                **(self.partial if self.partial is not None else self.value),
                "enabled": self.enabled,
                "loading": self.loading,
                "error": self.error,
                "cleanup": copy.deepcopy(self.job),
                "stale": bool(
                    self.value.get("synced_at")
                    and (self.error or time.time() - self.value["synced_at"] >= INTERVAL)
                ),
            }

    def request_refresh(self):
        """An explicit Refresh brings the next rescan forward, never below the floor."""
        with self.lock:
            synced = self.value.get("synced_at") or 0
            if self.enabled and not self.loading and time.time() - synced >= MIN_REFRESH:
                self.next_poll = 0.0

    def _refresh(self):
        try:
            with self.lock:
                self.partial = copy.deepcopy(self.value)

            def complete(value, final):
                value["synced_at"] = time.time()
                with self.lock:
                    self.value, self.error = value, None
                    # Workers still scanning publish their rows onto this machine's.
                    self.partial = None if final else copy.deepcopy(value)
                    if self.next_poll:
                        self.next_poll = value["synced_at"] + INTERVAL
                self.save()

            # This machine's rows are complete while workers may still take minutes.
            value = self.collect(
                publish=self.publish_row, local_done=lambda value: complete(value, False)
            )
            complete(value, True)
        except Exception as exc:
            with self.lock:
                self.error = f"Workspace inventory failed: {exc}"
        finally:
            with self.lock:
                self.partial = None
                self.loading = False

    def publish_row(self, row):
        """Expose completed rows during a scan; only a complete inventory is persisted."""
        with self.lock:
            if self.partial is None:
                return
            rows = {entry["key"]: entry for entry in self.partial["workspaces"]}
            rows[row["key"]] = row
            self.partial["workspaces"] = self.sorted_rows(rows.values())

    @staticmethod
    def sorted_rows(rows):
        return sorted(
            rows,
            key=lambda row: (
                -(row.get("updated_at") or 0),
                row["repo"] or "~",
                row["name"].lower(),
                row["key"],
            ),
        )

    def save(self):
        with self.lock:
            recent = sorted(
                (kv for kv in self.links.items() if kv[0] not in self.live),
                key=lambda kv: kv[1].get("checked_at", 0),
            )
            kept = {key for key, _ in recent[-MAX_LINKS:]} | self.live
            self.links = {key: value for key, value in self.links.items() if key in kept}
            # Clones come and go; a base repository nobody asked about expires with its TTL.
            cutoff = time.time() - BASE_TTL
            self.bases = {
                path: value
                for path, value in self.bases.items()
                if value.get("checked_at", 0) >= cutoff
            }
            cache = {
                "version": CACHE_VERSION,
                "links": self.links,
                "bases": self.bases,
                "scope": self.inventory_scope(),
                "value": self.value,
            }
            payload = json.dumps(cache)
            if len(payload.encode()) > MAX_STATE:
                # Workers' rows are the part no local limit bounds; they are left out
                # rather than let them stop this machine's inventory from being saved.
                rows = [row for row in self.value["workspaces"] if row.get("machine") is None]
                payload = json.dumps({**cache, "value": {**self.value, "workspaces": rows}})
        if len(payload.encode()) > MAX_STATE:
            with self.lock:
                self.error = "Workspace cache exceeded its size limit; it was not saved"
            return
        self.directory.mkdir(parents=True, exist_ok=True)
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

    def spaces(self, warnings, machine=None, timeout=HERDR_TIMEOUT):
        try:
            return herdr("workspace", "list", machine=machine, timeout=timeout)["workspaces"]
        except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
            where = f"{machine.label}: " if machine else ""
            warnings.append(f"{where}herdr workspaces unavailable: {exc}")
            return []

    def agents(self, warnings, machine=None, timeout=HERDR_TIMEOUT):
        try:
            return herdr("agent", "list", machine=machine, timeout=timeout)["agents"]
        except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
            where = f"{machine.label}: " if machine else ""
            warnings.append(f"{where}herdr agents unavailable: {exc}")
            return []

    def scanned_roots(self, spaces):
        """Clone roots this experiment is allowed to touch: its own scan, nothing else."""
        return remote_probe.scanned_roots(
            self.src,
            self.roots,
            [w["worktree"]["repo_root"] for w in spaces if w.get("worktree")],
        )

    def inventory(self, warnings):
        """Every clone's worktrees plus the herdr workspaces and agents attached to them."""
        spaces = self.spaces(warnings)
        agents = self.agents(warnings)
        roots = self.scanned_roots(spaces)
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            found = list(pool.map(clone_worktrees, sorted(roots)))
        clones, checkouts = {}, {}
        for result in found:
            if not result:
                continue
            main, items = result
            clones[main["path"]] = main
            for item in items:
                checkouts[item["path"]] = item
        return clones, checkouts, spaces, agents

    def base_repo(self, clone, budget, warnings, config=None, key=None):
        """The repository pull requests target: an `upstream` remote, or origin's parent.

        A worker's clone brings the ``config`` its probe read and caches under its row key.
        """
        remotes = remote_slugs(repo_config(clone["path"]) if config is None else config)
        key = key or clone["path"]
        saved = self.bases.get(key)
        if saved and time.time() - saved.get("checked_at", 0) < BASE_TTL:
            return saved["repo"], saved.get("origin_owner")
        origin = remotes.get("origin")
        owner = origin.split("/")[0] if origin else None
        base = remotes.get("upstream")
        if not base and origin:
            try:
                value = budget.get(f"repos/{origin}")
                parent = (value.get("parent") or {}).get("full_name")
                base = parent if value.get("fork") and parent else value.get("full_name")
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                # An exhausted budget or a transient error must not be cached as an
                # answer for a day; keep the previous one and retry next refresh.
                warnings.append(f"{origin}: repository lookup failed ({exc})")
                return (saved["repo"] if saved else None), owner
        if base and (not SLUG.fullmatch(base) or base.split("/")[1] in {".", ".."}):
            base = None
        self.bases[key] = {
            "repo": base,
            "origin_owner": owner,
            "checked_at": time.time(),
        }
        return base, owner

    def due_links(self, wanted):
        """Lookups missing or expired, never-resolved first, then the longest aged."""
        now = time.time()
        due = []
        for cache_key, lookup in wanted.items():
            saved = self.links.get(cache_key)
            if not saved:
                due.append((-1.0, cache_key, lookup))
            elif now - saved.get("checked_at", 0) >= link_ttl(saved["link"]):
                due.append((saved.get("checked_at", 0), cache_key, lookup))
        due.sort(key=lambda entry: entry[0])
        return [(cache_key, lookup) for _, cache_key, lookup in due]

    def refresh_links(self, due, budget):
        """Fetch the due lookups in batches, in their order of urgency.

        Lookups are grouped per repository into one alias per head branch or number, so
        a whole inventory usually costs a handful of requests. Should the budget still
        run out, entries nobody has seen yet go before ones that merely aged. Returns
        each cache key left unrefreshed with the reason.
        """
        grouped: dict[str, dict] = {}
        for rank, (cache_key, (base, kind, key)) in enumerate(due):
            target = ("head", key.partition(":")[2]) if kind == "head" else ("number", key)
            group = grouped.setdefault(base, {}).setdefault(target, {"rank": rank, "keys": []})
            group["keys"].append((cache_key, kind, key))
        # Targets keep their most urgent rank, so ordering chunks by their first target
        # interleaves repositories by urgency.
        chunks = []
        for base, targets in grouped.items():
            items = [(target, group["keys"]) for target, group in targets.items()]
            for start in range(0, len(items), LOOKUP_BATCH):
                first = targets[items[start][0]]["rank"]
                chunks.append((first, base, items[start : start + LOOKUP_BATCH]))
        chunks.sort(key=lambda chunk: chunk[0])
        failed = {}
        limited = None
        for _, base, items in chunks:
            try:
                if limited:
                    raise ValueError(limited)  # Further requests would only fail too.
                answers = fetch_links(budget, base, [target for target, _ in items])
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                if "rate limit" in str(exc).lower():
                    limited = str(exc)
                failed.update(
                    {cache_key: str(exc) for _, group in items for cache_key, _, _ in group}
                )
                continue
            for target, group in items:
                answer = answers[target]
                for cache_key, kind, key in group:
                    if isinstance(answer, Exception):
                        failed[cache_key] = str(answer)
                        continue
                    try:
                        link = answer_link(answer, base, kind, key)
                        if link is None:
                            link = fetch_head(budget, base, key)
                    except (
                        AttributeError,
                        TypeError,
                        KeyError,
                        OSError,
                        ValueError,
                        subprocess.SubprocessError,
                    ) as exc:
                        failed[cache_key] = str(exc) or "Unexpected GitHub lookup answer"
                        continue
                    self.links[cache_key] = {"link": link, "checked_at": time.time()}
        return failed

    def resolve_links(self, item, base, origin_owner, failed):
        """A checkout's links from the cache this scan refreshed; stale if any failed."""
        links: list[dict] = []
        stale = head_found = False
        for kind, key in candidates(item, base, origin_owner):
            if kind == "head" and head_found:
                continue  # One head reference is enough; the rest would repeat it.
            cache_key = f"{base}#{kind}#{key}"
            saved = self.links.get(cache_key)
            stale = stale or cache_key in failed
            if not saved:
                continue
            link = saved["link"]
            if link["state"] == "none":
                continue
            head_found = head_found or kind == "head"
            if not any(
                existing["kind"] == link["kind"] and existing["number"] == link["number"]
                for existing in links
            ):
                links.append(link)
        return links, stale

    def watched_checkouts(self, machine=None, jobs=None):
        """Ended watches remain in history but no longer reserve their checkout.

        A worker's watch names its checkout as the worker spells it.
        """
        return {
            job["cwd"] if machine else str(Path(job["cwd"]).resolve())
            for job in (self.jobs() if jobs is None else jobs)
            if job.get("cwd")
            and job.get("machine") == (machine.id if machine else None)
            and job.get("status") not in {"closed", "stopped"}
        }

    def collect(self, publish=None, local_done=None):
        """The whole inventory; ``local_done`` gets this machine's part before any worker's.

        That part keeps each worker's last rows and status until its scan answers.
        """
        warnings: list[str] = []
        # Workers scan in their own threads while this machine scans as it always has.
        scans = self.start_scans(self.remote_machines(warnings))
        clones, checkouts, spaces, agents = self.inventory(warnings)
        watched = self.watched_checkouts()
        opened = {
            str(Path(space["worktree"]["checkout_path"]).resolve())
            for space in spaces
            if space.get("worktree")
        }
        # A clone's own checkout is not a disposable workspace: it is listed only while
        # herdr holds it open, and its branch never contributes a pull request lookup.
        selected = [path for path in checkouts if path not in clones or path in opened]
        selected.sort(key=lambda path: (path not in opened, path))
        budget = Budget(self.fetch)
        updates = transcript_updates(set(selected), self.src.resolve())
        rows = self.orphan_rows(spaces, agents, checkouts)
        for row in rows:
            if publish:
                publish(row)
        # Resolve each clone once per scan, sharing its answer across all its worktrees,
        # and gather every checkout's lookups so they are fetched together.
        bases, wanted = {}, {}
        needs: dict[str, set[str]] = {}
        for path in selected:
            item = checkouts[path]
            root = str(Path(item["common"]).parent)
            clone = clones.get(root)
            if root not in bases:
                bases[root] = self.base_repo(clone, budget, warnings) if clone else (None, None)
            base, owner = bases[root]
            if base and path not in clones:
                needs[path] = set()
                for kind, key in candidates(item, base, owner):
                    wanted[f"{base}#{kind}#{key}"] = (base, kind, key)
                    needs[path].add(f"{base}#{kind}#{key}")
        with self.lock:
            self.live = set(wanted)
        due = self.due_links(wanted)
        refreshing = {cache_key for cache_key, _ in due}
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            lookups = pool.submit(self.refresh_links, due, budget)
            pending = {
                pool.submit(local_state, path, checkouts[path].get("branch")): path
                for path in selected
            }

            def add(path, state):
                item = checkouts[path]
                main = path in clones
                base, owner = bases[str(Path(item["common"]).parent)]
                if main:
                    links: list[dict] = []
                    stale = False
                elif base:
                    failed = lookups.result() if needs[path] & refreshing else {}
                    links, stale = self.resolve_links(item, base, owner, failed)
                else:
                    links, stale = [], True
                row = self.row(item, state, links, stale, spaces, agents, watched, base, main)
                if path in updates:
                    row["updated_at"] = max(row.get("updated_at") or 0, updates[path])
                rows.append(row)
                if publish:
                    publish(row)

            # A row whose lookups are all fresh need not wait for GitHub; the others are
            # held back until the refresh finishes, without blocking the rows behind them.
            waiting: list[tuple[str, dict]] = []
            for future in concurrent.futures.as_completed([*pending, lookups]):
                if future is lookups:
                    for path, state in waiting:
                        add(path, state)
                    waiting = []
                    continue
                path = pending[future]
                if needs.get(path, set()) & refreshing and not lookups.done():
                    waiting.append((path, future.result()))
                else:
                    add(path, future.result())
        failed = lookups.result()
        statuses = {}
        if scans and local_done:
            workers = {machine.id for machine in scans}
            local_done(
                {
                    "workspaces": self.sorted_rows(
                        [
                            *rows,
                            *(r for r in self.value["workspaces"] if r.get("machine") in workers),
                        ]
                    ),
                    "warnings": list(warnings),
                    "calls": budget.calls,
                    "src": str(self.src),
                    "machines": {
                        machine_id: status
                        for machine_id, status in (self.value.get("machines") or {}).items()
                        if machine_id in workers
                    },
                }
            )
        if scans:
            remote, statuses = self.collect_remote(scans, budget, warnings, wanted, failed, publish)
            rows.extend(remote)
        # Only lookups never answered are worth a warning; the rest keep their last answer.
        # One shared cause, such as a failed request, is reported once for all it affected.
        unresolved: dict[str, list] = {}
        for cache_key, reason in failed.items():
            if cache_key not in self.links:
                unresolved.setdefault(reason, []).append(wanted[cache_key])
        for reason, missed in unresolved.items():
            if len(missed) == 1:
                warnings.append("{} {} {}: {}".format(*missed[0], reason))
            else:
                warnings.append(f"{len(missed)} GitHub lookups unresolved: {reason}")
        value = {
            "workspaces": self.sorted_rows(rows),
            "warnings": warnings,
            "calls": budget.calls,
            "src": str(self.src),
        }
        if statuses:
            value["machines"] = statuses
        return value

    # Worker machines --------------------------------------------------------------

    def start_scans(self, workers):
        """One thread per worker with its deadline; None for one still backing off.

        The deadline starts with the thread, and the worker's whole scan (herdr listings
        and probe) runs within it, so the wait for it is never shorter than its work.
        """
        scans: dict = {}
        for machine in workers:
            if time.time() < self.retry.get(machine.id, 0):
                scans[machine] = None
                continue
            future: concurrent.futures.Future = concurrent.futures.Future()
            deadline = time.monotonic() + REMOTE_TIMEOUT

            def scan(machine=machine, future=future, deadline=deadline):
                try:
                    future.set_result(self.scan_machine(machine, deadline))
                except BaseException as exc:  # Everything a worker raises stays its own.
                    future.set_exception(exc)

            threading.Thread(target=scan, daemon=True).start()
            scans[machine] = (future, deadline)
        return scans

    @staticmethod
    def remote_text(value):
        return value if isinstance(value, str) and len(value) <= MAX_REMOTE_TEXT else None

    def remote_space(self, space):
        """A worker's herdr workspace rebuilt from typed fields; None when unusable."""
        if not isinstance(space, dict):
            return None
        wid = space.get("workspace_id")
        if not isinstance(wid, str) or not machines.WORKSPACE.fullmatch(wid):
            return None
        worktree = space.get("worktree")
        return {
            "workspace_id": wid,
            "label": remote_probe.display_text(self.remote_text(space.get("label"))),
            "agent_status": self.remote_text(space.get("agent_status")),
            "worktree": {
                field: self.remote_text(worktree.get(field))
                for field in ("repo_root", "checkout_path", "repo_name")
            }
            if isinstance(worktree, dict)
            else None,
        }

    def remote_agent(self, agent):
        if not isinstance(agent, dict):
            return None
        found = {
            field: self.remote_text(agent.get(field))
            for field in ("workspace_id", "agent", "agent_status")
        }
        # Passed to herdr's command line by cleanup: never one read as an option.
        pane = agent.get("pane_id")
        found["pane_id"] = pane if isinstance(pane, str) and PANE.fullmatch(pane) else None
        return found

    @staticmethod
    def capped(found, notes, label):
        """A worker's inventory within MAX_REMOTE_*, saying so when it was cut."""
        clones, kept = [], 0
        for clone in found["clones"][:MAX_REMOTE_CLONES]:
            room = MAX_REMOTE_ITEMS - kept
            if room <= 0:
                break
            main = clone["main"]
            others = [item for item in clone["items"] if item["path"] != main["path"]]
            items = [main, *others[: room - 1]]
            kept += len(items)
            clones.append({**clone, "items": items})
        listed = {item["path"] for clone in clones for item in clone["items"]}
        states = dict(
            [(path, state) for path, state in found["states"].items() if path in listed][
                :MAX_REMOTE_ROWS
            ]
        )
        if len(listed) < sum(len(clone["items"]) for clone in found["clones"]) or len(states) < len(
            found["states"]
        ):
            notes.append(f"{label}: inventory truncated to its first {len(states)} checkouts")
        return {
            **found,
            "clones": clones,
            "states": states,
            "updates": {path: when for path, when in found["updates"].items() if path in states},
        }

    def scan_machine(self, machine, deadline=None):
        """A worker's herdr listings and its one probe call; raises when it is unreachable.

        Everything herdr says about a worker is rebuilt from type-checked fields, as the
        probe's answer is validated, before any of it reaches a row.
        """
        deadline = time.monotonic() + REMOTE_TIMEOUT if deadline is None else deadline

        def left():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired("scan", REMOTE_TIMEOUT)
            return remaining

        notes: list[str] = []
        raw = self.spaces(notes, machine, min(HERDR_TIMEOUT, left()))
        raw = raw if isinstance(raw, list) else []
        spaces = [space for space in map(self.remote_space, raw) if space]
        raw = self.agents(notes, machine, min(HERDR_TIMEOUT, left()))
        raw = raw if isinstance(raw, list) else []
        agents = [agent for agent in map(self.remote_agent, raw) if agent]
        if len(spaces) > MAX_REMOTE_ROWS or len(agents) > MAX_REMOTE_AGENTS:
            notes.append(f"{machine.label}: herdr listings truncated")
            spaces, agents = spaces[:MAX_REMOTE_ROWS], agents[:MAX_REMOTE_AGENTS]

        found = remote_probe.call(
            self.home,
            machine,
            "inventory",
            left(),
            src=machine.src,
            roots=[],
            repo_roots=self.space_paths(spaces, "repo_root"),
            opened=self.space_paths(spaces, "checkout_path"),
        )
        found = self.capped(found, notes, machine.label)
        self.resolved_spaces(spaces, found["paths"])
        return spaces, agents, found, notes

    @staticmethod
    def space_paths(spaces, field):
        """The absolute paths a worker's herdr workspaces name in ``field``, for its probe."""
        values = (space["worktree"][field] for space in spaces if space["worktree"])
        return sorted({v for v in values if v and v.startswith("/")})

    @staticmethod
    def resolved_spaces(spaces, paths):
        # Its paths are the worker's: the checkout herdr names becomes the one the probe
        # resolved there, and nothing about it is looked up on this filesystem.
        for space in spaces:
            if worktree := space["worktree"]:
                info = paths.get(worktree["checkout_path"])
                worktree["checkout_path"] = info["path"] if info else None
                worktree["exists"] = bool(info and info["exists"])

    def stale_machine(self, machine, error, rows, statuses, warnings, retry):
        """A worker this scan could not use: its last rows, marked stale, and a warning."""
        last = (self.value.get("machines") or {}).get(machine.id) or {}
        if retry:
            failures = self.failures[machine.id] = self.failures.get(machine.id, 0) + 1
            # Longer than the rescan interval, so a failed worker skips at least one scan.
            self.retry[machine.id] = time.time() + INTERVAL * min(2**failures, MAX_BACKOFF)
        kept = [
            {**row, "machine_stale": True}
            for row in self.value["workspaces"]
            if row.get("machine") == machine.id
        ]
        rows.extend(kept)
        statuses[machine.id] = {
            "label": machine.label,
            "status": "stale" if kept else "unreachable",
            "error": error,
            "synced_at": last.get("synced_at"),
        }
        shown = "; showing its last scanned workspaces" if kept else ""
        warnings.append(f"{machine.label} unavailable: {error}{shown}")

    def collect_remote(self, scans, budget, warnings, wanted, failed, publish):
        """Every worker's rows: from this scan's probe, or its last rows marked stale.

        Each worker's GitHub lookups get an equal share of what this machine left of the
        scan's budget, a share one worker leaves unspent passing to the next, and add to
        ``wanted`` and ``failed``. Whatever goes wrong building one worker's rows leaves
        it stale and the other rows alone. Returns the rows and each worker's status.
        """
        rows: list[dict] = []
        statuses: dict[str, dict] = {}
        scanned = []
        for machine, scan in scans.items():
            last = (self.value.get("machines") or {}).get(machine.id) or {}
            try:
                if scan is None:
                    raise ValueError(last.get("error") or "Waiting before the next attempt")
                future, deadline = scan
                left = max(0.0, deadline - time.monotonic()) + REMOTE_GRACE
                scanned.append((machine, *future.result(timeout=left)))
            except Exception as exc:
                if isinstance(exc, concurrent.futures.TimeoutError):
                    error = f"No answer within {REMOTE_TIMEOUT} seconds"
                else:
                    error = (str(exc) or type(exc).__name__)[-400:]
                self.stale_machine(machine, error, rows, statuses, warnings, scan is not None)
        jobs = self.jobs()
        seen = set()
        for index, (machine, spaces, agents, found, notes) in enumerate(scanned):
            share = Budget(
                self.fetch, max(0, budget.limit - budget.calls) // (len(scanned) - index)
            )
            notes = list(notes)
            try:
                built, own, missed, bases = self.machine_rows(
                    machine, spaces, agents, found, jobs, share, notes
                )
            except Exception as exc:
                error = f"Unusable inventory: {(str(exc) or type(exc).__name__)[-400:]}"
                self.stale_machine(machine, error, rows, statuses, warnings, True)
                continue
            finally:
                budget.calls += share.calls
            self.retry.pop(machine.id, None)
            self.failures.pop(machine.id, None)
            seen |= bases
            warnings.extend(notes)
            with self.lock:
                self.live |= set(own)
            wanted.update(own)
            failed.update(missed)
            statuses[machine.id] = {
                "label": machine.label,
                "status": "ok",
                "error": None,
                "synced_at": time.time(),
            }
            rows.extend(built)
            for row in built:
                if publish:
                    publish(row)
        # A worker's base repositories last as long as its clones do: a worker gone from
        # the registry, or a clone its latest answer no longer lists, takes them along.
        current = {machine.id for machine in scans}
        answered = {machine.id for machine, *_ in scanned if statuses[machine.id]["status"] == "ok"}
        with self.lock:
            self.bases = {
                key: value
                for key, value in self.bases.items()
                if not key.startswith("m:")
                or (
                    key.split(":", 2)[1] in current
                    and (key.split(":", 2)[1] not in answered or key in seen)
                )
            }
        return rows, statuses

    def machine_rows(self, machine, spaces, agents, found, jobs, budget, warnings):
        """One worker's rows, its link lookups, those that failed and its base cache keys."""
        clones = {clone["main"]["path"]: clone for clone in found["clones"]}
        checkouts = {item["path"]: item for clone in found["clones"] for item in clone["items"]}
        watched = self.watched_checkouts(machine, jobs)
        rows = self.orphan_rows(spaces, agents, checkouts, machine)
        bases: dict = {}
        own: dict[str, tuple] = {}
        pending = []
        for path, state in found["states"].items():
            item = checkouts[path]
            root = str(Path(item["common"]).parent)
            clone = clones.get(root)
            key = f"m:{machine.id}:{root}"
            if key not in bases:
                bases[key] = (
                    self.base_repo(clone["main"], budget, warnings, config=clone["config"], key=key)
                    if clone
                    else (None, None)
                )
            base, owner = bases[key]
            main = path in clones
            if base and not main:
                for kind, lookup in candidates(item, base, owner):
                    own[f"{base}#{kind}#{lookup}"] = (base, kind, lookup)
            pending.append((item, state, base, owner, main, found["updates"].get(path)))
        failed = self.refresh_links(self.due_links(own), budget)
        for item, state, base, owner, main, update in pending:
            if main:
                links, stale = [], False
            elif base:
                links, stale = self.resolve_links(item, base, owner, failed)
            else:
                links, stale = [], True
            row = self.row(item, state, links, stale, spaces, agents, watched, base, main, machine)
            if update:
                row["updated_at"] = max(row.get("updated_at") or 0, update)
            rows.append(row)
        return rows, own, failed, set(bases)

    @staticmethod
    def on_machine(row, machine):
        """A worker's row: keys and workspace ids naming it, and its own Collie."""
        key = row["key"]
        workspace = key.startswith("workspace:")
        row.update(
            key=f"workspace:{machine.id}:{key[10:]}" if workspace else f"m:{machine.id}:{key}",
            machine=machine.id,
            machine_label=machine.label,
            machine_stale=False,
            workspace_ids=[machines.qualify(machine.id, wid) for wid in row["workspace_ids"]],
            # The worker's own Collie, if the registry names one.
            workspace_url=collie_url(machine, row["workspace_ids"][0])
            if row["workspace_ids"]
            else None,
        )
        return row

    @staticmethod
    def workspace_records(spaces, agents, path, resolve=True):
        # A worker's spaces arrive with the checkout its probe resolved.
        found = [
            space
            for space in spaces
            if space.get("worktree")
            and (
                str(Path(space["worktree"]["checkout_path"]).resolve())
                if resolve
                else space["worktree"]["checkout_path"]
            )
            == path
        ]
        ids = {space["workspace_id"] for space in found}
        return found, [agent for agent in agents if agent.get("workspace_id") in ids]

    def row(self, item, state, links, stale, spaces, agents, watched, base, main, machine=None):
        found, attached = self.workspace_records(spaces, agents, item["path"], machine is None)
        watch = item["path"] in watched
        blockers = []
        if main:
            blockers.append(
                blocker("main", "Main checkout — cleanup only closes its herdr workspace")
            )
        if watch:
            blockers.append(
                blocker("watch", "An unfinished watch uses this checkout; cancel the watch first")
            )
        if state["error"]:
            blockers.append(blocker("unknown", state["error"]))
        if state["changes"]:
            blockers.append(blocker("dirty", f"{state['changes']} uncommitted change(s)"))
        if state["unpushed"]:
            count = "100+" if state["unpushed"] >= MAX_UNPUSHED else state["unpushed"]
            blockers.append(blocker("unpushed", f"{count} commit(s) only on this branch"))
        working = [a for a in attached if a.get("agent_status") in WORKING]
        if working:
            blockers.append(blocker("agent", f"{len(working)} agent(s) still working"))
        row = {
            "created_at": state.get("created_at"),
            "updated_at": state.get("updated_at"),
            "key": item["path"],
            "path": item["path"],
            "name": Path(item["path"]).name,
            "label": found[0]["label"] if found else None,
            "repo": base or Path(item["common"]).parent.name,
            "repo_root": str(Path(item["common"]).parent),
            "branch": item.get("branch"),
            "sha": (item.get("sha") or "")[:12],
            "main": main,
            "missing": False,
            "upstream": ":".join(part for part in item.get("upstream") or [] if part) or None,
            "workspace_ids": [space["workspace_id"] for space in found],
            "workspace_url": collie_url(None, found[0]["workspace_id"]) if found else None,
            "agent_status": found[0].get("agent_status") if found else None,
            "agents": [
                {
                    "agent": agent.get("agent"),
                    "status": agent.get("agent_status"),
                    "pane": agent.get("pane_id"),
                }
                for agent in attached
            ],
            "changes": state["changes"],
            "unpushed": state["unpushed"],
            "links": links,
            "stale_links": stale,
            "blockers": blockers,
            # A clone's own checkout is never removed; only its workspace is closed.
            "remove_worktree": not main,
            "removable": (not main or bool(found)) and not watch,
        }
        row["status"] = row_status(row)
        return self.on_machine(row, machine) if machine else row

    def orphan_rows(self, spaces, agents, checkouts, machine=None):
        """herdr workspaces with no known checkout: closeable, with nothing to remove."""
        rows = []
        for space in spaces:
            worktree = space.get("worktree") or {}
            path = worktree.get("checkout_path")
            # A worker's checkout is resolved, and checked for, by its own probe.
            resolved = (str(Path(path).resolve()) if machine is None else path) if path else None
            if resolved in checkouts:
                continue
            attached = [a for a in agents if a.get("workspace_id") == space["workspace_id"]]
            if machine is None:
                missing = resolved is not None and not Path(resolved).exists()
            else:
                missing = resolved is not None and not worktree.get("exists")
            if not resolved:
                blockers = [
                    blocker("detached", "No Git checkout is attached; closing this ends its panes")
                ]
                removable = True
            elif missing:
                blockers = []
                removable = True
            else:
                blockers = [blocker("outside", "Checkout is outside the scanned clones")]
                removable = False
            working = [a for a in attached if a.get("agent_status") in WORKING]
            if working:
                blockers.append(blocker("agent", f"{len(working)} agent(s) still working"))
            rows.append(
                {
                    "created_at": None,
                    "updated_at": None,
                    "key": f"workspace:{space['workspace_id']}",
                    "path": resolved,
                    "name": space.get("label") or space["workspace_id"],
                    "label": space.get("label"),
                    "repo": worktree.get("repo_name"),
                    "repo_root": worktree.get("repo_root"),
                    "branch": None,
                    "sha": "",
                    "main": False,
                    "missing": missing,
                    "upstream": None,
                    "workspace_ids": [space["workspace_id"]],
                    "workspace_url": collie_url(None, space["workspace_id"]),
                    "agent_status": space.get("agent_status"),
                    "agents": [
                        {
                            "agent": a.get("agent"),
                            "status": a.get("agent_status"),
                            "pane": a.get("pane_id"),
                        }
                        for a in attached
                    ],
                    "changes": None,
                    "unpushed": None,
                    "links": [],
                    "stale_links": False,
                    "blockers": blockers,
                    "remove_worktree": False,
                    "removable": removable,
                }
            )
            rows[-1]["status"] = "missing" if missing else row_status(rows[-1])
            if machine:
                self.on_machine(rows[-1], machine)
        return rows

    def open_workspace(self, request):
        """Open a listed workspace, creating only its herdr container when absent."""
        key = request.get("key")
        if set(request) != {"key"} or not isinstance(key, str) or not 0 < len(key) <= 1024:
            raise ValueError("Supply a workspace key")
        # Opening one on a worker waits for later; its Collie is its own.
        if (label := self.worker_label(key)) is not None:
            raise ValueError(f"Not available for workspaces on {label} yet")
        with self.lock:
            if not self.enabled:
                raise ValueError("The workspace experiment is disabled")
            if not any(row["key"] == key for row in self.value["workspaces"]):
                raise ValueError("Unknown workspace; refresh the list")
        row = self.resolve(key)
        if row is None:
            raise ValueError("Workspace changed; refresh the list")
        with self.lock_file(row["repo_root"] or key).open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            row = self.resolve(key)
            if row is None:
                raise ValueError("Workspace changed; refresh the list")
            if not row["workspace_ids"]:
                if row["missing"] or not row["repo_root"]:
                    raise ValueError("The checkout is unavailable; refresh the list")
                herdr(
                    "worktree",
                    "open",
                    "--cwd",
                    row["repo_root"],
                    "--path",
                    row["path"],
                    "--no-focus",
                )
                row = self.resolve(key)
                if row is None or not row["workspace_ids"]:
                    raise ValueError("Workspace was not found after opening; refresh the list")
            return {"url": row["workspace_url"]}

    def checkout(self, key):
        """The directory of a listed worktree row, for the read-only diff and transcript views.

        Only rows this inventory found as Git worktrees qualify here; a row that is only a
        herdr workspace is viewed through its workspace ID instead.
        """
        if not isinstance(key, str) or not 0 < len(key) <= 1024:
            raise ValueError("Supply a workspace key")
        with self.lock:
            if not self.enabled:
                raise ValueError("The workspace experiment is disabled")
            listed = self.value["workspaces"] + ((self.partial or {}).get("workspaces") or [])
            row = next((entry for entry in listed if entry["key"] == key), None)
        if row is not None and row.get("machine"):
            raise ValueError(f"Not available for workspaces on {row['machine_label']} yet")
        if row is None or row["path"] != key or row["missing"] or not Path(key).is_dir():
            raise ValueError("Unknown workspace; refresh the list")
        return key

    def worker_checkout(self, key):
        """A worker's listed worktree row as (machine, path), for the read-only views.

        None for a key of this machine. The machine is the registered one the row names,
        and the path the worker's own, which its probe resolves and checks again.
        """
        if not isinstance(key, str) or not 0 < len(key) <= 1024:
            raise ValueError("Supply a workspace key")
        if self.worker_label(key) is None:
            return None
        with self.lock:
            if not self.enabled:
                raise ValueError("The workspace experiment is disabled")
            listed = self.value["workspaces"] + ((self.partial or {}).get("workspaces") or [])
            row = next((entry for entry in listed if entry["key"] == key), None)
        if (
            row is None
            or not row.get("machine")
            or key != f"m:{row['machine']}:{row['path']}"
            or row["missing"]
        ):
            raise ValueError("Unknown workspace; refresh the list")
        machine = machines.resolve(self.home, row["machine"])
        if machine is None or not machine.enabled:
            raise ValueError(f"{row['machine_label']} is disabled")
        return machine, row["path"]

    # Cleanup -------------------------------------------------------------------

    def cleanup(self, request):
        """Validate and start one batch; a running batch is reported, never queued."""
        if set(request) - {"id", "action", "targets"}:
            raise ValueError("Invalid cleanup parameters")
        targets = request.get("targets")
        if not isinstance(targets, list) or not 0 < len(targets) <= MAX_TARGETS:
            raise ValueError(f"Select between 1 and {MAX_TARGETS} workspaces")
        wanted = []
        for target in targets:
            approve = target.get("approve", []) if isinstance(target, dict) else None
            if (
                not isinstance(target, dict)
                or set(target) - {"key", "approve"}
                or not isinstance(target.get("key"), str)
                or not 0 < len(target["key"]) <= 1024
                or not isinstance(approve, list)
                or len(approve) > len(OVERRIDABLE)
                or any(kind not in OVERRIDABLE for kind in approve)
            ):
                raise ValueError("Invalid cleanup target")
            wanted.append({"key": target["key"], "approve": sorted(set(approve))})
        if len({target["key"] for target in wanted}) != len(wanted):
            raise ValueError("Each workspace may only be listed once")
        with self.lock:
            if not self.enabled:
                raise ValueError("The workspace experiment is disabled")
            if self.job and self.job["status"] == "running":
                # The running batch owns the worker; this request is reported, not queued.
                return {"cleanup": copy.deepcopy(self.job), "accepted": False}
            job = {
                "id": str(uuid.uuid4()),
                "status": "running",
                "started_at": time.time(),
                "finished_at": None,
                "results": [
                    {"key": target["key"], "status": "pending", "message": "Queued", "steps": []}
                    for target in wanted
                ],
            }
            self.job = job
            threading.Thread(target=self._cleanup, args=(job, wanted), daemon=True).start()
            return {"cleanup": copy.deepcopy(job), "accepted": True}

    def record(self, job, index, **changes):
        with self.lock:
            job["results"][index].update(changes)

    def _cleanup(self, job, targets):
        try:
            for index, target in enumerate(targets):
                self.record(job, index, status="running", message="Revalidating resources")
                try:
                    self.remove(job, index, target)
                except Exception as exc:
                    # Any failure belongs to its own target; the batch continues.
                    self.record(job, index, status="failed", message=str(exc)[-400:] or repr(exc))
        finally:
            with self.lock:
                job["status"] = "complete"
                job["finished_at"] = time.time()
                self.next_poll = 0.0

    def worker_label(self, key):
        """The worker a row key names, by label; None for a key of this machine."""
        if key.startswith("m:"):
            machine_id = key[2:].partition(":")[0]
        elif key.startswith("workspace:") and ":" in key[10:]:
            machine_id = key[10:].partition(":")[0]
        else:
            return None
        try:
            machine = machines.load(self.home).get(machine_id)
        except (OSError, ValueError):
            machine = None
        return machine.label if machine else machine_id

    def key_machine(self, key):
        """The registered, enabled worker a row key names; None for a key of this machine."""
        if self.worker_label(key) is None:
            return None
        machine_id = key.split(":", 2)[1]
        machine = machines.load(self.home).get(machine_id)
        if machine is None:
            raise ValueError(f"Unknown machine {machine_id}")
        if not machine.enabled:
            raise ValueError(f"{machine.label} is disabled")
        return machine

    def resolve(self, key):
        """Re-read herdr and this one checkout's clone; a stale selection is never acted on."""
        if (machine := self.key_machine(key)) is not None:
            return self.resolve_remote(machine, key)
        warnings: list[str] = []
        spaces, agents = self.spaces(warnings), self.agents(warnings)
        if key.startswith("workspace:"):
            space = next((s for s in spaces if s["workspace_id"] == key[len("workspace:") :]), None)
            root = ((space or {}).get("worktree") or {}).get("repo_root")
            found = clone_worktrees(Path(root).resolve()) if root else None
            checkouts = {item["path"]: item for item in found[1]} if found else {}
            rows = self.orphan_rows(spaces, agents, checkouts)
            return next((row for row in rows if row["key"] == key), None)
        try:
            common = git(key, "rev-parse", "--path-format=absolute", "--git-common-dir", timeout=15)
            root = Path(common).resolve().parent
        except (OSError, ValueError, subprocess.SubprocessError):
            return None
        # A target the scan cannot reach is never acted on, however it was requested.
        if root not in self.scanned_roots(spaces):
            raise ValueError("This checkout is outside the scanned clones; refresh the list")
        found = clone_worktrees(root)
        if not found:
            return None
        main, items = found
        item = next((entry for entry in items if entry["path"] == key), None)
        if not item:
            return None
        watched = self.watched_checkouts()
        return self.row(
            item,
            local_state(key, item.get("branch")),
            [],
            True,
            spaces,
            agents,
            watched,
            None,
            key == main["path"],
        )

    def resolve_remote(self, machine, key):
        """A worker's row re-read there: its herdr listings and one probe of the clone.

        The probe reads only a clone among the worker's own scanned roots. A herdr listing
        that fails stops here, rather than leave a working agent's workspace unseen.
        """
        spaces, agents = (
            herdr(noun, "list", machine=machine, timeout=HERDR_TIMEOUT)[field]
            for noun, field in (("workspace", "workspaces"), ("agent", "agents"))
        )
        if not isinstance(spaces, list) or not isinstance(agents, list):
            raise ValueError(f"{machine.label} answered an unreadable herdr listing")
        spaces = [space for space in map(self.remote_space, spaces) if space]
        agents = [agent for agent in map(self.remote_agent, agents) if agent]
        if key.startswith("workspace:"):
            wid = key.split(":", 2)[2]
            spaces = [space for space in spaces if space["workspace_id"] == wid]
            root = ((spaces or [{}])[0].get("worktree") or {}).get("repo_root")
            if not spaces or not root or not root.startswith("/"):
                # Without its clone the checkout is unknown, never taken for missing.
                self.resolved_spaces(spaces, {})
                rows = self.orphan_rows(spaces, agents, {}, machine)
                return rows[0] if rows else None
            path = None
        else:
            path, root = key.split(":", 2)[2], None
        found = workspace_viewer.probe(
            self.home,
            machine,
            "resolve",
            RESOLVE_TIMEOUT,
            src=machine.src,
            repo_roots=self.space_paths(spaces, "repo_root"),
            opened=self.space_paths(spaces, "checkout_path"),
            path=path,
            root=root,
        )
        self.resolved_spaces(spaces, found["paths"])
        checkouts = {item["path"]: item for clone in found["clones"] for item in clone["items"]}
        if path is None:
            rows = self.orphan_rows(spaces, agents, checkouts, machine)
            return rows[0] if rows else None
        if found["root"] is None:
            return None
        if found["root"] not in found["roots"]:
            raise ValueError("This checkout is outside the scanned clones; refresh the list")
        item, state = checkouts.get(path), found["states"].get(path)
        if not item or not state:
            return None
        return self.row(
            item,
            state,
            [],
            True,
            spaces,
            agents,
            self.watched_checkouts(machine),
            None,
            path == found["clones"][0]["main"]["path"],
            machine,
        )

    def remove(self, job, index, target):
        """Act only on blockers the user actually approved, re-read a moment ago."""
        key, approved = target["key"], set(target["approve"])
        machine = self.key_machine(key)
        row = self.resolve(key)
        steps: list[str] = []
        if row is None:
            self.record(
                job, index, status="done", message="Already removed", steps=["Nothing to remove"]
            )
            return
        if not row["removable"]:
            reason = row["blockers"][0]["text"] if row["blockers"] else "This cannot be removed"
            self.record(job, index, status="skipped", message=reason, steps=steps)
            return
        unseen = [item for item in row["blockers"] if item["kind"] not in approved]
        if unseen:
            self.record(
                job,
                index,
                status="skipped",
                message="Skipped: " + "; ".join(item["text"] for item in unseen),
                steps=steps,
            )
            return
        lock_path = self.lock_file(row["repo_root"] or row["key"], machine)
        with lock_path.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.close_workspaces(job, index, row, steps, machine)
            if machine is not None:
                kept = self.remove_remote(job, index, row, steps, approved, machine)
            else:
                self.remove_worktree(job, index, row, steps, bool(approved & CLEAN_OVERRIDES))
                kept = self.delete_branch(job, index, row, steps, approved)
        self.record(job, index, status="done", message=kept or "Cleaned up", steps=steps)

    def lock_file(self, clone, machine=None):
        """The lock of one clone; a worker's, which its probe resolved, is the one its
        launches take."""
        if machine is not None:
            return machines.clone_lock(self.home, machine, str(clone))
        directory = self.home / "workspace-locks"
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        return directory / hashlib.sha256(str(clone).encode()).hexdigest()

    def progress(self, job, index, steps, message):
        steps.append(message)
        self.record(job, index, message=message, steps=list(steps))

    def close_workspaces(self, job, index, row, steps, machine=None):
        """Ask idle agents to exit, then close the workspaces that own this checkout.

        A working agent is never sent keys: approving its removal closes its workspace,
        which ends the pane. Unsent prompt text in any pane is lost either way. A worker's
        agents and workspaces are reached through its herdr.
        """
        idle = [a for a in row["agents"] if a["pane"] and a["status"] not in WORKING]
        for agent in idle:
            self.progress(job, index, steps, f"Exiting {agent['agent']} in {agent['pane']}")
            try:
                herdr("agent", "send-keys", agent["pane"], "ctrl+d", machine=machine)
            except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
                steps.append(f"Graceful exit unavailable for {agent['pane']}: {exc}")
        if idle:
            self.await_exit({agent["pane"] for agent in idle}, machine)
        for workspace_id in row["workspace_ids"]:
            self.progress(job, index, steps, f"Closing herdr workspace {workspace_id}")
            # A worker's rows name its workspaces qualified, as `mini/w1`.
            own = machines.split(workspace_id)[1] if machine else workspace_id
            herdr("workspace", "close", own, machine=machine)

    def await_exit(self, panes, machine=None):
        deadline = time.monotonic() + EXIT_WAIT
        while time.monotonic() < deadline:
            try:
                listed = herdr("agent", "list", machine=machine)["agents"]
                live = {a.get("pane_id") for a in listed if isinstance(a, dict)}
            except (OSError, ValueError, KeyError, subprocess.SubprocessError):
                return
            if not panes & live:
                return
            time.sleep(0.25)

    def remove_worktree(self, job, index, row, steps, force):
        if not row["remove_worktree"] or not row["path"] or not row["repo_root"]:
            return
        if not Path(row["path"]).exists():
            steps.append("Checkout already removed")
            return
        self.progress(job, index, steps, f"Removing worktree {row['path']}")
        if not force:
            # Git's own clean check runs `git status` in that worktree under config the
            # clone's listing never saw, so the same check runs hardened here and Git is
            # told to skip its own. It refuses what Git refuses: tracked changes and
            # untracked files (ignored ones go), and any submodule.
            path = row["path"]
            if git(path, "status", "--porcelain", "-z", "--untracked-files=normal", timeout=300):
                raise ValueError(f"{path} has uncommitted changes or untracked files; kept")
            modules = git(path, "rev-parse", "--path-format=absolute", "--git-path", "modules")
            staged = git(path, "ls-files", "--stage", "-z", timeout=300, strip=False)
            if Path(modules).is_dir() or any(
                entry.startswith("160000 ") for entry in staged.split("\0")
            ):
                raise ValueError(f"{path} contains submodules; kept")
        git(row["repo_root"], "worktree", "remove", "--force", row["path"], timeout=300)

    def delete_branch(self, job, index, row, steps, approved):
        """Delete the branch only when no commit can be lost, and say so when it is kept.

        `unpushed == 0` means a remote ref, another local branch or the base branch's
        own history carries every commit's changes, so a forced delete loses nothing
        even when Git calls the branch unmerged — which is the normal state after a
        squash merge. Otherwise `-d` decides, and a branch Git refuses to delete is
        kept with the worktree already gone.
        """
        branch = row.get("branch")
        if not row["remove_worktree"] or not branch or not BRANCH_NAME.fullmatch(branch):
            return None
        safe = row["unpushed"] == 0 or "unpushed" in approved
        self.progress(job, index, steps, f"Deleting local branch {branch}")
        try:
            git(row["repo_root"], "branch", "-D" if safe else "-d", branch)
            return None
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            kept = f"Kept local branch {branch}: {exc.args[0].splitlines()[0][:160]}"
            steps[-1] = kept
            self.record(job, index, steps=list(steps))
            return f"Cleaned up. {kept}"

    def remove_remote(self, job, index, row, steps, approved, machine):
        """``remove_worktree`` and ``delete_branch`` on a worker, in one probe there.

        The worker checks the path again against its clone's own worktree list, and the
        branch against the one checked out there, before its hardened Git removes either.
        """
        if not row["remove_worktree"] or not row["path"] or not row["repo_root"]:
            return None
        branch = row.get("branch")
        branch = branch if branch and BRANCH_NAME.fullmatch(branch) else None
        self.progress(job, index, steps, f"Removing worktree {row['path']} on {machine.label}")
        done = workspace_viewer.probe(
            self.home,
            machine,
            "remove",
            REMOVE_TIMEOUT,
            root=row["repo_root"],
            path=row["path"],
            force=bool(approved & CLEAN_OVERRIDES),
            branch=branch,
            safe=row["unpushed"] == 0 or "unpushed" in approved,
        )
        if done["worktree"] == "missing":
            steps[-1] = "Checkout already removed"
        if done["branch"] == "deleted":
            self.progress(job, index, steps, f"Deleted local branch {branch}")
        elif done["branch"] == "kept":
            kept = f"Kept local branch {branch}: {done['detail']}"
            self.progress(job, index, steps, kept)
            return f"Cleaned up. {kept}"
        else:
            self.record(job, index, steps=list(steps))
        return None
