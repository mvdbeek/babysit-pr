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
import owned_process
import workspace_viewer
from pr_workspaces import COLLIE_URL, SLUG, clone_worktrees, herdr, remote_slugs, repo_config

INTERVAL = 300  # Local rescan cadence; GitHub state has its own freshness window.
MIN_REFRESH = 60  # Floor between requested rescans, however often the tab asks.
LINK_TTL = 900  # Open items move; the other states are rechecked far less often.
SETTLED_TTL = 21600
NONE_TTL = 3600
BASE_TTL = 86400
CALL_BUDGET = 100
MAX_WORKTREES = 300
MAX_TARGETS = 50
MAX_LINKS = 500
MAX_STATE = 4 * 1024 * 1024
MAX_RESPONSE = 4 * 1024 * 1024
MAX_CHANGES = 1000
MAX_UNPUSHED = 100
EXIT_WAIT = 5
CACHE_VERSION = 1
WORKING = {"working", "blocked"}
# A cleanup override approves these by kind, so a changed count is not a new blocker
# but a newly appeared condition is, and is never swept along by one tick.
OVERRIDABLE = {"main", "dirty", "unpushed", "agent", "unknown", "detached"}
CLOSED = {"closed", "merged"}
ISSUE_NAME = re.compile(r"^issue-(\d+)(?:-|$)")
PR_NAME = re.compile(r"^pr-([A-Za-z0-9][A-Za-z0-9-]*?)-(\d+)(?:-\d+)?$")
BRANCH_NAME = re.compile(r"^[^\s:^~?*\\[]{1,255}$")
BASE_BRANCH = re.compile(r"^(main|master|dev|develop|trunk|next|release[_-].+|\d+\.\d+)$")


def git(path, *args, timeout=60, strip=True):
    """Git in one checkout. Large repositories need more room than a quick query."""
    result = owned_process.run(
        ["git", "-C", str(path), *args],
        env=github_cli.environment(),
        text=True,
        timeout=timeout,
    )
    if result.returncode:
        raise ValueError((result.stderr or result.stdout or "Git failed")[-4000:].strip())
    return result.stdout.strip() if strip else result.stdout


def gh_json(endpoint, timeout=20):
    """One bounded, read-only GitHub REST call through the authenticated `gh`."""
    result = github_cli.run(
        ["gh", "api", "--hostname", "github.com", endpoint],
        text=True,
        timeout=timeout,
    )
    if result.returncode:
        raise ValueError((result.stderr or result.stdout or "GitHub request failed")[-400:].strip())
    if len(result.stdout) > MAX_RESPONSE:
        raise ValueError("GitHub response exceeded byte limit")
    return json.loads(result.stdout)


class Budget:
    """A per-refresh ceiling on GitHub calls; exhaustion leaves links stale, not wrong."""

    def __init__(self, fetch, limit=None):
        self.fetch = fetch
        self.limit = CALL_BUDGET if limit is None else limit
        self.calls = 0

    def get(self, endpoint):
        if self.calls >= self.limit:
            raise ValueError("GitHub request budget exhausted for this refresh")
        self.calls += 1
        return self.fetch(endpoint)


def counted(value, limit):
    return min(len(value), limit)


def checkout_times(path, changed=()):
    """Worktree creation and activity; inspection must never advance these times."""
    root = Path(path)
    created = None
    updated = []
    try:
        stat = root.stat()
        created = getattr(stat, "st_birthtime", None)
        updated.append(stat.st_mtime)
        git_dir = root / ".git"
        if git_dir.is_file():
            pointer = git_dir.read_text().strip().removeprefix("gitdir: ")
            git_dir = (root / pointer).resolve()
        log = git_dir / "logs" / "HEAD"
        try:
            # The first reflog entry records worktree creation, even on filesystems
            # without birth times. Its mtime follows commits, resets and checkouts.
            with log.open() as stream:
                created = float(stream.readline().split("\t", 1)[0].split()[-2])
            updated.append(log.stat().st_mtime)
        except (OSError, ValueError, IndexError):
            updated.append((git_dir / "HEAD").stat().st_mtime)
        for name in changed[:MAX_CHANGES]:
            if name:
                try:
                    updated.append((root / name).lstat().st_mtime)
                except OSError:
                    pass  # A deleted file has no remaining timestamp.
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return {"created_at": created, "updated_at": max(updated, default=created)}


def transcript_updates(paths, src):
    """Scan session stores once per inventory, matching the most specific checkout."""
    roots = [src, *(Path(path) for path in paths if not Path(path).is_relative_to(src))]
    seen = set()
    updates: dict[str, float] = {}
    for root in roots:
        for agent, file in workspace_viewer.session_files(root):
            if file in seen:
                continue
            seen.add(file)
            session = workspace_viewer.details(file, agent)
            if not session:
                continue
            cwd = Path(session["cwd"]).resolve()
            checkout = next((str(p) for p in (cwd, *cwd.parents) if str(p) in paths), None)
            if checkout:
                try:
                    updates[checkout] = max(updates.get(checkout, 0), file.stat().st_mtime)
                except OSError:
                    pass
    return updates


def local_state(path, branch=None):
    """Uncommitted changes and commits only this branch has, without touching either."""
    try:
        status = git(path, "--no-optional-locks", "status", "--porcelain", "-z", strip=False)
        changes = []
        records = iter(status.split("\0"))
        for record in records:
            if not record:
                continue
            # NUL-delimited porcelain leaves paths unquoted, including newlines.
            changes.append(record[3:])
            if "R" in record[:2] or "C" in record[:2]:
                next(records, None)  # Renames/copies carry the old path separately.
        # Only a commit that no remote ref and no other local branch carries would be
        # lost with this worktree, so those are the ones worth blocking on.
        # `--exclude` matches `--branches` with the refs/heads/ prefix already stripped.
        exclude = [f"--exclude={branch}"] if branch else []
        unpushed = git(
            path,
            "--no-optional-locks",
            "rev-list",
            f"--max-count={MAX_UNPUSHED}",
            "--count",
            "HEAD",
            "--not",
            "--remotes",
            *exclude,
            "--branches",
            "--tags",
        )
        return {
            **checkout_times(path, changes),
            "changes": counted(changes, MAX_CHANGES),
            "unpushed": int(unpushed or 0),
            "error": None,
        }
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        detail = (str(exc).splitlines() or [""])[0][:120]
        return {"changes": None, "unpushed": None, "error": f"Local state unavailable: {detail}"}


def candidates(item, base, origin_owner):
    """Every GitHub item this checkout may belong to, from Git provenance and wt naming.

    Names are a hint the repository must agree with: a `pr-<owner>-<number>` directory
    only names a pull request when `<owner>` owns the resolved base repository. Tracking
    configuration usually names the *base* branch a topic branch was cut from, so only
    an upstream tracking the same branch name is treated as a pull request head.
    """
    found = []
    branch = item.get("branch") or ""
    directory = Path(item["path"]).name
    upstream = item.get("upstream") or [None, None]
    if branch and not BASE_BRANCH.match(branch):
        if upstream[0] and upstream[1] == branch:
            found.append(("head", f"{upstream[0].split('/')[0]}:{branch}"))
        if origin_owner:
            found.append(("head", f"{origin_owner}:{branch}"))
    for name in dict.fromkeys([branch, directory]):
        issue = ISSUE_NAME.match(name)
        if issue:
            found.append(("issue", issue[1]))
        pull = PR_NAME.match(name)
        if pull and base and pull[1].lower() == base.split("/")[0].lower():
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


def fetch_link(budget, base, kind, key):
    if kind == "issue":
        return link_record("issue", budget.get(f"repos/{base}/issues/{key}"), base)
    if kind == "pr":
        return link_record("pr", budget.get(f"repos/{base}/pulls/{key}"), base)
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
        self.job: dict | None = None
        self.value: dict = {"workspaces": [], "warnings": [], "synced_at": None}
        self.partial: dict | None = None
        self.src = Path(src).expanduser() if src else Path.home() / "src"
        self.roots: list[Path] = []
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
        return {
            "src": str(self.src.resolve()),
            "roots": sorted({str(root.resolve()) for root in self.roots}),
        }

    def snapshot(self):
        with self.lock:
            if self.enabled and not self.loading and time.time() >= self.next_poll:
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
            value = self.collect(publish=self.publish_row)
            value["synced_at"] = time.time()
            with self.lock:
                self.value, self.error = value, None
                self.partial = None
                if self.next_poll:
                    self.next_poll = value["synced_at"] + INTERVAL
            self.save()
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
            recent = sorted(self.links.items(), key=lambda kv: kv[1].get("checked_at", 0))
            self.links = dict(recent[-MAX_LINKS:])
            # Clones come and go; a base repository nobody asked about expires with its TTL.
            cutoff = time.time() - BASE_TTL
            self.bases = {
                path: value
                for path, value in self.bases.items()
                if value.get("checked_at", 0) >= cutoff
            }
            payload = json.dumps(
                {
                    "version": CACHE_VERSION,
                    "links": self.links,
                    "bases": self.bases,
                    "scope": self.inventory_scope(),
                    "value": self.value,
                }
            )
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

    def spaces(self, warnings):
        try:
            return herdr("workspace", "list")["workspaces"]
        except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
            warnings.append(f"herdr workspaces unavailable: {exc}")
            return []

    def agents(self, warnings):
        try:
            return herdr("agent", "list")["agents"]
        except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
            warnings.append(f"herdr agents unavailable: {exc}")
            return []

    def scanned_roots(self, spaces):
        """Clone roots this experiment is allowed to touch: its own scan, nothing else."""
        roots = {Path(w["worktree"]["repo_root"]).resolve() for w in spaces if w.get("worktree")}
        roots.update(root.resolve() for root in self.roots if (root / ".git").exists())
        if self.src.exists():
            roots.update(p.resolve() for p in self.src.iterdir() if (p / ".git").is_dir())
        return roots

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

    def base_repo(self, clone, budget, warnings):
        """The repository pull requests target: an `upstream` remote, or origin's parent."""
        remotes = remote_slugs(repo_config(clone["path"]))
        saved = self.bases.get(clone["path"])
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
        self.bases[clone["path"]] = {
            "repo": base,
            "origin_owner": owner,
            "checked_at": time.time(),
        }
        return base, owner

    def resolve_links(self, item, base, origin_owner, budget, warnings):
        """Resolve each candidate once, newest cache first, and never twice per refresh."""
        links: list[dict] = []
        stale = head_found = False
        for kind, key in candidates(item, base, origin_owner):
            if kind == "head" and head_found:
                continue  # One head reference is enough; the rest would repeat it.
            cache_key = f"{base}#{kind}#{key}"
            saved = self.links.get(cache_key)
            if saved and time.time() - saved.get("checked_at", 0) < link_ttl(saved["link"]):
                link = saved["link"]
            else:
                try:
                    link = fetch_link(budget, base, kind, key)
                    self.links[cache_key] = {"link": link, "checked_at": time.time()}
                except (OSError, ValueError, subprocess.SubprocessError) as exc:
                    stale = True
                    if not saved:
                        warnings.append(f"{base} {kind} {key}: {exc}")
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

    def watched_checkouts(self):
        """Ended watches remain in history but no longer reserve their checkout."""
        return {
            str(Path(job["cwd"]).resolve())
            for job in self.jobs()
            if job.get("cwd") and job.get("status") not in {"closed", "stopped"}
        }

    def collect(self, publish=None):
        warnings: list[str] = []
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
        if len(selected) > MAX_WORKTREES:
            warnings.append(f"{len(selected)} checkouts found; showing the first {MAX_WORKTREES}.")
            selected = selected[:MAX_WORKTREES]
        budget = Budget(self.fetch)
        updates = transcript_updates(set(selected), self.src.resolve())
        rows = self.orphan_rows(spaces, agents, checkouts)
        for row in rows:
            if publish:
                publish(row)
        # Resolve each clone once per scan, sharing its answer across all its worktrees.
        bases = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            pending = {
                pool.submit(local_state, path, checkouts[path].get("branch")): path
                for path in selected
            }
            for future in concurrent.futures.as_completed(pending):
                path = pending[future]
                item = checkouts[path]
                main = path in clones
                root = str(Path(item["common"]).parent)
                clone = clones.get(root)
                if root not in bases:
                    bases[root] = self.base_repo(clone, budget, warnings) if clone else (None, None)
                base, owner = bases[root]
                if main:
                    links: list[dict] = []
                    stale = False
                elif base:
                    links, stale = self.resolve_links(item, base, owner, budget, warnings)
                else:
                    links, stale = [], True
                row = self.row(
                    item, future.result(), links, stale, spaces, agents, watched, base, main
                )
                if path in updates:
                    row["updated_at"] = max(row.get("updated_at") or 0, updates[path])
                rows.append(row)
                if publish:
                    publish(row)
        return {
            "workspaces": self.sorted_rows(rows),
            "warnings": warnings,
            "calls": budget.calls,
            "src": str(self.src),
        }

    @staticmethod
    def workspace_records(spaces, agents, path):
        found = [
            space
            for space in spaces
            if space.get("worktree")
            and str(Path(space["worktree"]["checkout_path"]).resolve()) == path
        ]
        ids = {space["workspace_id"] for space in found}
        return found, [agent for agent in agents if agent.get("workspace_id") in ids]

    def row(self, item, state, links, stale, spaces, agents, watched, base, main):
        found, attached = self.workspace_records(spaces, agents, item["path"])
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
            "workspace_url": (
                f"{COLLIE_URL}/space/{quote(found[0]['workspace_id'], safe='')}" if found else None
            ),
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
        return row

    def orphan_rows(self, spaces, agents, checkouts):
        """herdr workspaces with no known checkout: closeable, with nothing to remove."""
        rows = []
        for space in spaces:
            worktree = space.get("worktree") or {}
            path = worktree.get("checkout_path")
            resolved = str(Path(path).resolve()) if path else None
            if resolved in checkouts:
                continue
            attached = [a for a in agents if a.get("workspace_id") == space["workspace_id"]]
            missing = resolved is not None and not Path(resolved).exists()
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
                    "workspace_url": f"{COLLIE_URL}/space/{quote(space['workspace_id'], safe='')}",
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
        return rows

    def open_workspace(self, request):
        """Open a listed workspace, creating only its herdr container when absent."""
        key = request.get("key")
        if set(request) != {"key"} or not isinstance(key, str) or not 0 < len(key) <= 1024:
            raise ValueError("Supply a workspace key")
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
        if row is None or row["path"] != key or row["missing"] or not Path(key).is_dir():
            raise ValueError("Unknown workspace; refresh the list")
        return key

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

    def resolve(self, key):
        """Re-read herdr and this one checkout's clone; a stale selection is never acted on."""
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

    def remove(self, job, index, target):
        """Act only on blockers the user actually approved, re-read a moment ago."""
        key, approved = target["key"], set(target["approve"])
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
        lock_path = self.lock_file(row["repo_root"] or row["key"])
        with lock_path.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.close_workspaces(job, index, row, steps)
            self.remove_worktree(job, index, row, steps, bool(approved))
            kept = self.delete_branch(job, index, row, steps, approved)
        self.record(job, index, status="done", message=kept or "Cleaned up", steps=steps)

    def lock_file(self, clone):
        directory = self.home / "workspace-locks"
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        return directory / hashlib.sha256(str(clone).encode()).hexdigest()

    def progress(self, job, index, steps, message):
        steps.append(message)
        self.record(job, index, message=message, steps=list(steps))

    def close_workspaces(self, job, index, row, steps):
        """Ask idle agents to exit, then close the workspaces that own this checkout.

        A working agent is never sent keys: approving its removal closes its workspace,
        which ends the pane. Unsent prompt text in any pane is lost either way.
        """
        idle = [a for a in row["agents"] if a["pane"] and a["status"] not in WORKING]
        for agent in idle:
            self.progress(job, index, steps, f"Exiting {agent['agent']} in {agent['pane']}")
            try:
                herdr("agent", "send-keys", agent["pane"], "ctrl+d")
            except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
                steps.append(f"Graceful exit unavailable for {agent['pane']}: {exc}")
        if idle:
            self.await_exit({agent["pane"] for agent in idle})
        for workspace_id in row["workspace_ids"]:
            self.progress(job, index, steps, f"Closing herdr workspace {workspace_id}")
            herdr("workspace", "close", workspace_id)

    def await_exit(self, panes):
        deadline = time.monotonic() + EXIT_WAIT
        while time.monotonic() < deadline:
            try:
                live = {a.get("pane_id") for a in herdr("agent", "list")["agents"]}
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
        arguments = ["worktree", "remove"]
        if force:
            arguments.append("--force")
        git(row["repo_root"], *arguments, row["path"], timeout=300)

    def delete_branch(self, job, index, row, steps, approved):
        """Delete the branch only when no commit can be lost, and say so when it is kept.

        `unpushed == 0` means a remote ref or another local branch carries every commit,
        so a forced delete loses nothing even when Git calls the branch unmerged — which
        is the normal state after a squash merge. Otherwise `-d` decides, and a branch
        Git refuses to delete is kept with the worktree already gone.
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
