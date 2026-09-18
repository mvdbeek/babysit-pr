"""Local PR/issue checkout discovery and durable, asynchronous workspace operations.

A *target* is a PR or issue record from the overviews plus a ``kind`` field. PRs are
matched to checkouts by verified head provenance; issues by a ``wti``-style
``issue-<number>`` branch in the issue's repository or by a linked PR's verified head.
"""

import concurrent.futures
import contextlib
import copy
import fcntl
import hashlib
import json
import os
import re
import selectors
import shlex
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import quote

import github_cli
import owned_process
import workspace_agents
from issue_overview import branch_number

COLLIE_URL = "https://collie.tailfb45be.ts.net"
# The portable wt/wti/wtpr implementation shipped next to this module.
WORKTREE_HELPER = str(Path(__file__).resolve().with_name("wt.py"))
POLL_SECONDS = 15
SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+")


def run(*args, cwd=None, timeout=30, pass_fds=()):
    argv = list(map(str, args))
    # git may call the gh credential helper; both get the shared token and never prompt.
    if argv[0] == "gh":
        result = github_cli.run(argv, cwd=cwd, text=True, timeout=timeout, pass_fds=pass_fds)
    else:
        env = github_cli.environment() if argv[0] == "git" else None
        result = owned_process.run(
            argv, cwd=cwd, env=env, text=True, timeout=timeout, pass_fds=pass_fds
        )
    if result.returncode:
        raise ValueError((result.stderr or result.stdout or "Command failed")[-4000:].strip())
    return result.stdout.strip()


def herdr(*args):
    value = json.loads(run("herdr", *args))
    if value.get("error"):
        raise ValueError(str(value["error"]))
    return value["result"]


def remote_slug(value):
    match = re.fullmatch(
        r"(?:https://github.com/|git@github.com:|ssh://git@github.com/)([^/]+/[^/]+?)(?:\.git)?/?",
        value,
    )
    return match[1].lower() if match and SLUG.fullmatch(match[1]) else None


def git(path, *args):
    return run("git", "-C", path, *args, timeout=15)


CONFIG_KEYS = r"^(remote\..*\.url|branch\..*\.(remote|merge))$"


def repo_config(path):
    """Remote URLs and branch upstreams of one repository; empty when none are set."""
    try:
        return dict(
            line.split(" ", 1)
            for line in git(path, "config", "--get-regexp", CONFIG_KEYS).splitlines()
            if " " in line
        )
    except ValueError:
        return {}


def remote_slugs(config):
    return {
        key[7:-4]: slug
        for key, value in config.items()
        if key.startswith("remote.") and key.endswith(".url") and (slug := remote_slug(value))
    }


def provenance(path, common, branch, sha, config, remotes):
    remote = config.get(f"branch.{branch}.remote")
    ref = config.get(f"branch.{branch}.merge")
    return {
        "path": str(path),
        "common": str(common),
        "branch": branch,
        "sha": sha,
        "remotes": list(remotes.values()),
        "upstream": [remotes.get(remote), ref.removeprefix("refs/heads/")]
        if remote and ref
        else None,
    }


def checkout(path):
    """Resolve provenance from Git, never from a workspace label or directory name."""
    path = Path(path).resolve()
    try:
        root = Path(git(path, "rev-parse", "--show-toplevel")).resolve()
        if root != path:
            return None
        common = Path(
            git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
        ).resolve()
        branch = git(path, "symbolic-ref", "--quiet", "--short", "HEAD")
        sha = git(path, "rev-parse", "HEAD")
        config = repo_config(path)
        return provenance(path, common, branch, sha, config, remote_slugs(config))
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def clone_worktrees(root, wanted=None):
    """The clone at ``root`` and its attached worktrees, or None when not a wanted clone.

    Three Git calls per clone replace five per worktree: the shared config supplies
    remotes and branch upstreams, ``worktree list`` supplies every path, head and
    branch. Bare, detached and prunable entries are skipped, as ``checkout`` skips them.
    ``wanted`` of None accepts every clone, for callers that inventory all of them.
    """
    config = repo_config(root)
    remotes = remote_slugs(config)
    if wanted is not None and not wanted.intersection(remotes.values()):
        return None
    try:
        toplevel, common = git(
            root, "rev-parse", "--show-toplevel", "--path-format=absolute", "--git-common-dir"
        ).splitlines()
        records = git(root, "worktree", "list", "--porcelain", "-z").split("\0\0")
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if Path(toplevel).resolve() != root:
        return None
    common = Path(common).resolve()
    items = []
    for record in records:
        fields = dict(
            line.split(" ", 1) if " " in line else (line, "") for line in record.split("\0") if line
        )
        if (
            "worktree" not in fields
            or "bare" in fields
            or "prunable" in fields
            or not fields.get("branch", "").startswith("refs/heads/")
        ):
            continue
        path = Path(fields["worktree"]).resolve()
        if not path.is_dir():
            continue
        branch = fields["branch"].removeprefix("refs/heads/")
        items.append(provenance(path, common, branch, fields.get("HEAD", ""), config, remotes))
    main = next((item for item in items if item["path"] == str(root)), None)
    return (main, items) if main else None


def is_issue(target):
    return target.get("kind") == "issue"


def canonical(target):
    if (
        not SLUG.fullmatch(target.get("repo", ""))
        or target["repo"].split("/")[1] in {".", ".."}
        or not isinstance(target.get("number"), int)
        or target["number"] < 1
    ):
        raise ValueError("Invalid repository or number")
    path = "issues" if is_issue(target) else "pull"
    return f"https://github.com/{target['repo']}/{path}/{target['number']}"


def repositories(target):
    """Every repository slug a checkout for this target may legitimately track."""
    slugs = {target["repo"].lower(), (target.get("head_repo") or "").lower()}
    for pr in target.get("linked_prs") or []:
        slugs.update({pr["repo"].lower(), (pr.get("head_repo") or "").lower()})
    return slugs - {""}


def same_repository(target, item):
    return bool(repositories(target) & set(item["remotes"]))


def verified_issue(issue, item):
    """A `wti`-style branch for this issue number, in a checkout of the issue's repository."""
    return branch_number(item["branch"]) == issue["number"] and issue["repo"].lower() in set(
        item["remotes"]
    )


def linked_checkout(issue, item):
    """The linked PR whose verified head this checkout carries, if any."""
    return next((pr for pr in issue.get("linked_prs") or [] if verified_head(pr, item)), None)


def issue_name(issue):
    """The `wti` default branch name: issue-<number>-<title slug>."""
    slug = re.sub(r"[^a-z0-9]+", "-", str(issue.get("title") or "").lower()).strip("-")
    slug = slug[:60].rstrip("-")
    return f"issue-{issue['number']}" + (f"-{slug}" if slug else "")


def verified_head(pr, item):
    head_repo = (pr.get("head_repo") or "").lower()
    branch = pr.get("head_branch")
    if not head_repo or not branch or not same_repository(pr, item):
        return False
    if not (
        item["upstream"] == [head_repo, branch]
        or (item["branch"] == branch and head_repo in item["remotes"])
    ):
        return False
    sha = pr.get("head_sha", "")
    if not re.fullmatch(r"[a-fA-F0-9]{40}", sha):
        return False
    try:
        git(item["path"], "merge-base", "--is-ancestor", sha, "HEAD")
        return True
    except ValueError:
        return False


class Workspaces:
    def __init__(self, home, overview, jobs, src=None, issues=None):
        self.home = Path(home)
        self.src = Path(src) if src else Path.home() / "src"
        self.overview = overview
        self.issues = issues
        self.jobs = jobs
        self.lock = threading.RLock()
        self.inventory_lock = threading.Lock()
        self.inventory: dict = {
            "checkouts": [],
            "workspaces": [],
            "clones": [],
            "error": None,
            "synced_at": None,
        }
        self.next_poll = 0.0
        self.refreshing = False
        self.home.mkdir(parents=True, exist_ok=True)
        self.path = self.home / "pr-workspaces.sqlite"
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        with self.db() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS associations (pr TEXT, path TEXT, data TEXT, PRIMARY KEY(pr,path))"
            )
            db.execute("CREATE TABLE IF NOT EXISTS clones (repo TEXT PRIMARY KEY, path TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS operations (pr TEXT PRIMARY KEY, data TEXT)")
            db.execute(
                "CREATE TABLE IF NOT EXISTS operation_history (id TEXT PRIMARY KEY, data TEXT)"
            )
        # A fresh process must inspect resources; it never resubmits an uncertain launch.
        self.workers: dict[str, threading.Thread] = {}

    @contextlib.contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def targets(self):
        """Overview items and registered watches, with separate watch identifiers."""
        found: list[dict] = []
        for kind, overview in (("pr", self.overview), ("issue", self.issues)):
            if overview is not None:
                found.extend(
                    {**item, "kind": kind} for item in overview.snapshot()[overview.kind.key]
                )
        found.extend(
            {**job, "id": f"watch:{job['id']}", "kind": "watch", "head_repo": job.get("ci_repo")}
            for job in self.jobs()
            if job.get("id") and job.get("repo") and job.get("cwd")
        )
        return found

    def target(self, key):
        # Overview node ids are global; watch ids use their own prefix.
        target = next((t for t in self.targets() if t["id"] == key), None)
        if target is None:
            raise ValueError("Unknown PR or issue or watch; refresh the dashboard")
        if target.get("kind") != "watch":
            canonical(target)
        return target

    def scan(self):
        with self.inventory_lock:
            spaces = herdr("workspace", "list")["workspaces"]
            roots = {
                Path(w["worktree"]["repo_root"]).resolve() for w in spaces if w.get("worktree")
            }
            if self.src.exists():
                roots.update(p.resolve() for p in self.src.iterdir() if (p / ".git").is_dir())
            items = {}
            clones = []
            wanted: set[str] = set()
            for target in self.targets():
                wanted.update(repositories(target))
            # Git spawns dominate; several clones resolve concurrently, merged in path order.
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                found = pool.map(lambda root: clone_worktrees(root, wanted), sorted(roots))
            for result in found:
                if not result:
                    continue
                main, worktrees = result
                clones.append(main)
                for item in worktrees:
                    items[item["path"]] = item
            # Headless watches may use checkouts outside the normal clone inventory.
            for path in {
                str(Path(j["cwd"]).resolve()) for j in self.jobs() if j.get("cwd")
            } - items.keys():
                item = checkout(path)
                if item:
                    items[path] = item
            self.inventory = {
                "checkouts": list(items.values()),
                "clones": clones,
                "workspaces": spaces,
                "error": None,
                "synced_at": time.time(),
            }
            self.next_poll = time.monotonic() + POLL_SECONDS
            return copy.deepcopy(self.inventory)

    def refresh(self):
        try:
            self.scan()
        except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
            self.inventory = {**self.inventory, "error": f"Cannot discover workspaces: {exc}"}
            self.next_poll = time.monotonic() + POLL_SECONDS
        finally:
            with self.lock:
                self.refreshing = False

    def association(self, pr, item):
        data = {**item, "head_repo": pr.get("head_repo"), "head_branch": pr.get("head_branch")}
        with self.db() as db:
            db.execute(
                "INSERT OR REPLACE INTO associations VALUES (?,?,?)",
                (pr["id"], item["path"], json.dumps(data)),
            )
        return data

    def state(self):
        """One read of the durable tables and watcher queue, shared across a whole snapshot."""
        associations: dict[str, dict] = {}
        with self.db() as db:
            for key, path, data in db.execute("SELECT pr,path,data FROM associations"):
                associations.setdefault(key, {})[path] = json.loads(data)
            clones = dict(db.execute("SELECT repo,path FROM clones"))
            operations = {
                key: json.loads(data) for key, data in db.execute("SELECT pr,data FROM operations")
            }
        return {
            "associations": associations,
            "clones": clones,
            "operations": operations,
            "watches": self.jobs(),
        }

    def matches(self, pr, inventory, state=None):
        if state is None:
            state = self.state()
        saved = state["associations"].get(pr["id"], {})
        watches = state["watches"]
        matches, suggestions = [], []
        issue = is_issue(pr)
        watch = pr.get("kind") == "watch"
        repos = repositories(pr)  # Hoisted: snapshots compare every target with every checkout.
        linked_prs = (pr.get("linked_prs") or []) if issue else []
        for item in inventory["checkouts"]:
            if watch and str(Path(pr["cwd"]).resolve()) != item["path"]:
                continue
            same = bool(repos & set(item["remotes"]))
            if not same and not issue:
                continue
            prior = saved.get(item["path"], {})
            associated = same and (
                prior.get("common") == item["common"]
                and prior.get("branch") == item["branch"]
                and prior.get("head_repo") == pr.get("head_repo")
                and prior.get("head_branch") == pr.get("head_branch")
            )
            bound = (
                same
                and not watch
                and any(
                    w.get("url", "").rstrip("/") == canonical(pr)
                    and str(Path(w.get("cwd") or "/missing").resolve()) == item["path"]
                    and (w.get("snapshot", {}).get("pr", {}).get("head_branch") or w.get("branch"))
                    == item["branch"]
                    for w in watches
                )
            )
            linked = None
            if watch:
                verified = same
                suggested = False
            elif issue:
                verified = same and verified_issue(pr, item)
                if not verified and linked_prs and same:
                    linked = linked_checkout(pr, item)
                    verified = linked is not None
                # A branch name alone is only a hint when the repository does not agree.
                suggested = not same and branch_number(item["branch"]) == pr["number"]
            else:
                verified = verified_head(pr, item)
                suggested = item["branch"] == pr.get("head_branch")
            if associated or bound or verified:
                workspaces = [
                    w
                    for w in inventory["workspaces"]
                    if w.get("worktree")
                    and str(Path(w["worktree"]["checkout_path"]).resolve()) == item["path"]
                ]
                for workspace in workspaces or [None]:
                    wid = workspace["workspace_id"] if workspace else None
                    matches.append(
                        {
                            **item,
                            "workspace_id": wid,
                            "name": workspace["label"] if workspace else Path(item["path"]).name,
                            "agent_status": workspace.get("agent_status", "unknown")
                            if workspace
                            else "No workspace",
                            "url": f"{COLLIE_URL}/space/{quote(wid, safe='')}" if wid else None,
                            "linked_pr": linked["number"] if linked else None,
                        }
                    )
            elif suggested:
                suggestions.append(item["path"])
        return matches, suggestions

    def destination(self, pr):
        owner, repo = pr["repo"].split("/")
        first = self.src / repo
        second = self.src / f"{owner}--{repo}"
        if not os.path.lexists(first):
            return str(first)
        if not os.path.lexists(second):
            return str(second)
        return None

    def operation(self, key):
        with self.db() as db:
            row = db.execute("SELECT data FROM operations WHERE pr=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def save_operation(self, op, **changes):
        op.update(changes, updated_at=time.time())
        with self.db() as db:
            db.execute("INSERT OR REPLACE INTO operations VALUES (?,?)", (op["pr"], json.dumps(op)))
            db.execute(
                "INSERT OR REPLACE INTO operation_history VALUES (?,?)", (op["id"], json.dumps(op))
            )

    def describe(self, pr, inventory, state=None):
        if state is None:
            state = self.state()
        op = state["operations"].get(pr["id"])
        # Recover the durable resource reservation even if the server exited before
        # recording its association. Name alone is never sufficient provenance.
        if op and op.get("path") and op.get("branch") and op.get("common"):
            item = next((c for c in inventory["checkouts"] if c["path"] == op["path"]), None)
            if (
                item
                and item["common"] == op["common"]
                and item["branch"] == op["branch"]
                and (is_issue(pr) or item["upstream"] == self.expected_upstream(pr))
            ):
                state["associations"].setdefault(pr["id"], {})[item["path"]] = self.association(
                    pr, item
                )
        matches, suggestions = self.matches(pr, inventory, state)
        clones = [c["path"] for c in inventory["clones"] if pr["repo"].lower() in c["remotes"]]
        choice = state["clones"].get(pr["repo"].lower())
        if (
            op
            and op["status"] in {"queued", "running", "uncertain"}
            and pr["id"] not in self.workers
        ):
            result = next(
                (m for m in matches if m["path"] == op.get("path") and m["workspace_id"]), None
            )
            if result and self.agent_started(result, op.get("agent")):
                self.save_operation(
                    op, status="complete", result=result, message="Recovered existing workspace"
                )
            else:
                self.save_operation(
                    op,
                    status="uncertain",
                    message="Delivery was interrupted. Recorded resources were inspected; automatic relaunch is disabled. Reopen an existing checkout or inspect the operation log.",
                )
        return {
            "matches": matches,
            "suggestions": suggestions,
            "clones": clones,
            "preferred_clone": choice if choice in clones else None,
            "destination": None if pr.get("kind") == "watch" else self.destination(pr),
            "operation": op,
        }

    @staticmethod
    def expected_upstream(pr):
        return [(pr.get("head_repo") or "").lower(), pr.get("head_branch")]

    def snapshot(self):
        with self.lock:
            if time.monotonic() >= self.next_poll and not self.refreshing:
                self.refreshing = True
                threading.Thread(target=self.refresh, daemon=True).start()
            inventory = copy.deepcopy(self.inventory)
        described: dict[str, dict] = {"prs": {}, "issues": {}, "watches": {}}
        state = self.state()
        for target in self.targets():
            key = (
                "watches"
                if target.get("kind") == "watch"
                else "issues"
                if is_issue(target)
                else "prs"
            )
            described[key][target["id"]] = self.describe(target, inventory, state)
        return {
            **described,
            "agent_choices": workspace_agents.catalog(),
            "error": inventory["error"],
            "refreshing": self.refreshing,
            "synced_at": inventory["synced_at"],
        }

    def agent_started(self, target, agent):
        if not agent:
            return True
        return any(
            a["workspace_id"] == target["workspace_id"]
            and a.get("agent") == agent
            and str(Path(a.get("cwd") or "/missing").resolve()) == target["path"]
            for a in herdr("agent", "list")["agents"]
        )

    def action(self, request):
        allowed = {
            "id",
            "action",
            "workspace_id",
            "path",
            "clone",
            "destination",
            "agent",
            "model",
            "effort",
            "task",
            "retry",
        }
        if (
            set(request) - allowed
            or not isinstance(request.get("action"), str)
            or request.get("action")
            not in {
                "open",
                "reopen",
                "focus",
                "copy",
                "create",
                "clone-and-create",
            }
        ):
            raise ValueError("Invalid workspace action parameters")
        for field in allowed - {"retry"}:
            if field in request and not isinstance(request[field], str):
                raise ValueError(f"Expected text for {field}")
        if "retry" in request and not isinstance(request["retry"], bool):
            raise ValueError("Expected a retry flag")
        pr = self.target(request["id"])
        action = request["action"]
        if pr.get("kind") == "watch" and action in {"create", "clone-and-create"}:
            raise ValueError("Watch actions can only open or reopen the registered checkout")
        with self.lock:
            if action in {"create", "clone-and-create", "reopen"}:
                existing = self.operation(pr["id"])
                if existing and (
                    existing["status"] in {"queued", "running"}
                    or (
                        action != "reopen"
                        and not (existing["status"] == "failed" and request.get("retry"))
                    )
                ):
                    return {"operation": existing}
            inventory = self.scan()  # A failed inventory must never authorize creation.
            info = self.describe(pr, inventory)
            if action in {"open", "focus", "copy", "reopen"}:
                candidates = [
                    m
                    for m in info["matches"]
                    if m["path"] == request.get("path")
                    and m["workspace_id"] == request.get("workspace_id")
                ]
                if len(candidates) != 1:
                    raise ValueError("Workspace changed; refresh and choose a verified checkout")
                target = candidates[0]
                fresh = checkout(target["path"])
                current, _ = self.matches(
                    pr,
                    {
                        "checkouts": [fresh] if fresh else [],
                        "workspaces": herdr("workspace", "list")["workspaces"],
                    },
                )
                target = next(
                    (m for m in current if m["workspace_id"] == target["workspace_id"]), None
                )
                if not target:
                    raise ValueError("Workspace changed; refresh before opening")
                self.association(pr, target)
                if action == "focus":
                    if not target["workspace_id"]:
                        raise ValueError("Workspace is missing")
                    run("herdr", "workspace", "focus", target["workspace_id"])
                if action == "copy":
                    args = (
                        ["herdr", "workspace", "focus", target["workspace_id"]]
                        if target["workspace_id"]
                        else [
                            "herdr",
                            "worktree",
                            "open",
                            "--cwd",
                            str(Path(target["common"]).parent),
                            "--path",
                            target["path"],
                            "--no-focus",
                        ]
                    )
                    return {"command": shlex.join(args)}
                if target["workspace_id"]:
                    return {"result": target}
                if action != "reopen":
                    raise ValueError("Use Reopen workspace for this checkout")
                clone = str(Path(target["common"]).parent)
            else:
                if info["matches"]:
                    raise ValueError("A verified checkout already exists; open or reopen it")
                if request.get("agent", "codex") not in {"codex", "claude"}:
                    raise ValueError("Select Codex or Claude")
                workspace_agents.validate(
                    request.get("agent", "codex"),
                    request.get("model", ""),
                    request.get("effort", ""),
                )
                if (
                    not request.get("task", "").strip()
                    or len(request["task"]) > 32000
                    or "\0" in request["task"]
                ):
                    raise ValueError("Supply a task of 1–32,000 characters")
                if not is_issue(pr) and not (
                    pr.get("head_repo") and pr.get("head_branch") and pr.get("head_sha")
                ):
                    raise ValueError("Refresh GitHub metadata before creating a workspace")
                if action == "create":
                    clone = request.get("clone") or info["preferred_clone"]
                    if not clone and len(info["clones"]) == 1:
                        clone = info["clones"][0]
                    if clone not in info["clones"]:
                        raise ValueError("Choose a verified local clone")
                else:
                    clone = info["destination"]
                    if info["clones"] or not clone or request.get("destination") != clone:
                        raise ValueError("Clone destination changed; refresh before confirming")
            op = {
                "id": str(uuid.uuid4()),
                "pr": pr["id"],
                "action": action,
                "status": "queued",
                "clone": clone,
                "path": target["path"] if action == "reopen" else None,
                "agent": None if action == "reopen" else request.get("agent", "codex"),
                "model": None if action == "reopen" else request.get("model") or None,
                "effort": None if action == "reopen" else request.get("effort") or None,
                "message": "Queued",
                "log": "",
                "created_at": time.time(),
            }
            # Reserve the PR in SQLite before starting a thread (also across server instances).
            with self.db() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("SELECT data FROM operations WHERE pr=?", (pr["id"],)).fetchone()
                if row:
                    previous = json.loads(row[0])
                    if previous["status"] in {"queued", "running"} or (
                        action != "reopen"
                        and (previous["status"] != "failed" or not request.get("retry"))
                    ):
                        return {"operation": previous}
                db.execute(
                    "INSERT OR REPLACE INTO operations VALUES (?,?)", (pr["id"], json.dumps(op))
                )
                worker = threading.Thread(
                    target=self.perform, args=(pr, op, request.get("task", "")), daemon=True
                )
                # Registered before the reservation commits, so no snapshot sees a queued
                # operation without an owner and marks it uncertain.
                self.workers[pr["id"]] = worker
            worker.start()
            return {"operation": copy.deepcopy(op)}

    def run_logged(self, op, *args, pass_fds=(), timeout=600, env=None):
        """Stream bounded command output into the operation database while it runs."""
        started = time.monotonic()
        # Clones and the worktree helper talk to GitHub: shared token, no prompts.
        with github_cli.command(
            list(map(str, args)),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            pass_fds=pass_fds,
            env=env,
        ) as proc:
            assert proc.stdout is not None
            with selectors.DefaultSelector() as selector:
                selector.register(proc.stdout, selectors.EVENT_READ)
                while True:
                    if time.monotonic() - started > timeout:
                        raise subprocess.TimeoutExpired(args, timeout)
                    if selector.select(0.2):
                        chunk = os.read(proc.stdout.fileno(), 4096)
                        if not chunk:
                            break
                        self.save_operation(
                            op, log=(op["log"] + chunk.decode("utf-8", errors="replace"))[-16000:]
                        )
                    elif proc.poll() is not None:
                        break
            remaining = max(0.1, timeout - (time.monotonic() - started))
            code = proc.wait(timeout=remaining)
            if code:
                raise ValueError(op["log"][-4000:] or f"Command exited with status {code}")

    def perform(self, pr, op, task):
        launching = False
        try:
            lock_dir = self.home / "workspace-locks"
            lock_dir.mkdir(mode=0o700, exist_ok=True)
            key = hashlib.sha256(op["clone"].encode()).hexdigest()
            with (lock_dir / key).open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                self.save_operation(op, status="running", message="Revalidating local resources")
                info = self.describe(pr, self.scan())
                if op["action"] != "reopen" and info["matches"]:
                    raise ValueError("A checkout appeared while queued; open or reopen it")
                clone = Path(op["clone"])
                if op["action"] == "clone-and-create":
                    if os.path.lexists(clone):
                        raise ValueError("Clone destination now exists; nothing was overwritten")
                    clone.parent.mkdir(parents=True, exist_ok=True)
                    self.save_operation(op, message=f"Cloning {pr['repo']} into {clone}")
                    self.run_logged(
                        op,
                        "gh",
                        "repo",
                        "clone",
                        pr["repo"],
                        clone,
                        timeout=600,
                        pass_fds=(lock.fileno(),),
                    )
                main = checkout(clone)
                if not main or not (
                    same_repository(pr, main)
                    if pr.get("kind") == "watch"
                    else pr["repo"].lower() in main["remotes"]
                ):
                    raise ValueError("Local clone no longer belongs to the base repository")
                with self.db() as db:
                    db.execute(
                        "INSERT OR REPLACE INTO clones VALUES (?,?)",
                        (pr["repo"].lower(), str(clone)),
                    )
                if op["action"] == "reopen":
                    targets = [m for m in info["matches"] if m["path"] == op["path"]]
                    if not targets:
                        raise ValueError("Checkout changed before reopening")
                    self.save_operation(op, message="Reopening checkout without starting an agent")
                    herdr(
                        "worktree", "open", "--cwd", str(clone), "--path", op["path"], "--no-focus"
                    )
                else:
                    owner, repo = pr["repo"].split("/")
                    if is_issue(pr):
                        helper, subject = "wti", "Issue"
                        prefix = issue_name(pr)
                    else:
                        helper, subject = "wtpr", "Pull request"
                        prefix = f"pr-{owner}-{pr['number']}"
                    name = prefix
                    base = self.src / "worktrees" / repo
                    n = 1
                    while os.path.lexists(base / name) or self.branch_exists(clone, name):
                        n += 1
                        name = f"{prefix}-{n}"
                    self.save_operation(
                        op,
                        common=main["common"],
                        path=str(base / name),
                        branch=name,
                        message=f"Fetching {subject.lower()} and starting the selected agent",
                    )
                    overrides = []
                    for key in ("model", "effort"):
                        if op.get(key):
                            overrides.extend([f"--{key}", op[key]])
                    prompts = self.home / "workspace-prompts"
                    prompts.mkdir(mode=0o700, exist_ok=True)
                    prompt = prompts / op["id"]
                    fd = os.open(prompt, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(fd, "w") as stream:
                        stream.write(f"{task}\n\n{subject}: {canonical(pr)}\n")
                    args = [
                        f"--{op['agent']}",
                        *overrides,
                        "--no-focus",
                        "--name",
                        name,
                        "--repo-path",
                        str(clone),
                        "--worktree-root",
                        str(base),
                        "--prompt-file",
                        str(prompt),
                        canonical(pr),
                    ]
                    # The helper is a plain Python program run directly: no login shell
                    # wraps it. The user's interactive agent wrappers (for example the
                    # Safehouse `claude` function) still apply, because the helper never
                    # runs the agent itself: it types the agent command into the herdr
                    # pane's interactive shell, which loads the user's own startup files.
                    launching = True
                    self.run_logged(
                        op,
                        sys.executable,
                        WORKTREE_HELPER,
                        helper,
                        *args,
                        timeout=600,
                        pass_fds=(lock.fileno(),),
                        env={**os.environ, "WT_MULTIPLEXER": "herdr"},
                    )
                    self.save_operation(op, message="Verifying checkout and agent startup")
                    item = checkout(op["path"])
                    if not item or item["common"] != main["common"] or item["branch"] != name:
                        raise ValueError(
                            "Resulting checkout did not match the recorded repository and branch"
                        )
                    # wtpr records head provenance after checkout, before submitting the task.
                    # wti branches from the default branch, so only name and repository apply.
                    if not is_issue(pr) and item["upstream"] != self.expected_upstream(pr):
                        raise ValueError("Resulting checkout has unexpected PR head provenance")
                    self.association(pr, item)
                for _ in range(30):
                    # Verify the one checkout this operation owns; a full inventory scan
                    # of every local clone is not needed to confirm it.
                    fresh = checkout(op["path"])
                    matches, _ = self.matches(
                        pr,
                        {
                            "checkouts": [fresh] if fresh else [],
                            "workspaces": herdr("workspace", "list")["workspaces"],
                        },
                    )
                    target = next(
                        (m for m in matches if m["path"] == op["path"] and m["workspace_id"]), None
                    )
                    if target and self.agent_started(target, op["agent"]):
                        self.association(pr, target)
                        self.save_operation(
                            op,
                            status="complete",
                            result=target,
                            message="Workspace ready — Open in Collie",
                        )
                        return
                    time.sleep(1)
                raise ValueError(
                    "Could not verify the selected agent; inspect the workspace before retrying"
                )
        except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
            # A checkout or timeout after delivery could contain a live agent. Never resubmit.
            uncertain = launching and (
                isinstance(exc, subprocess.TimeoutExpired)
                or (op.get("path") and os.path.lexists(op["path"]))
            )
            self.save_operation(
                op,
                status="uncertain" if uncertain else "failed",
                message=str(exc),
                log=(op["log"] + "\n" + str(exc))[-16000:],
            )
        finally:
            with self.lock:
                self.workers.pop(pr["id"], None)
                self.next_poll = 0

    @staticmethod
    def branch_exists(clone, branch):
        try:
            git(clone, "show-ref", "--verify", f"refs/heads/{branch}")
            return True
        except ValueError:
            return False
