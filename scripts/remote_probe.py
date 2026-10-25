"""Read-only Git and session-store inventory, run here in-process or streamed to a worker.

Each op takes its ``git`` runner (and session-store readers) as arguments: this machine
passes its own wrappers, so local behaviour is exactly the code that ran before. A
worker gets this file's source on ``python3 -I -`` over ssh with the op and its
arguments as base64 JSON, so nothing is installed there and no shell ever sees a path.
It answers one JSON document, which ``call`` bounds and validates before anyone reads it.

Stdlib only, and Python 3.9 compatible: it runs on whatever ``python3`` a worker has.
"""

from __future__ import annotations

import base64
import concurrent.futures
import json
import math
import os
import re
import selectors
import signal
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

PROTOCOL = "babysit-probe-1"
MAX_STDOUT = 8 * 1024 * 1024
MAX_STDERR = 4096
MAX_CHANGES = 1000
MAX_UNPUSHED = 100
MAX_TEXT = 4096  # Longest path, branch or config value an answer may carry.
MAX_DEADLINE = 3600
GIT_TIMEOUT = 30  # Each Git a viewer op runs on a worker.
# Bytes of one transcript a single ``tail`` returns; base64 keeps that under MAX_STDOUT.
MAX_TAIL = 4 * 1024 * 1024
# Timestamps a worker reports: after 1970 and before 2100, whatever its clock says.
LATEST_TIME = 4102444800
EXPIRED = 124  # The probe's exit status once its deadline passed.
SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+")
CONFIG_KEYS = r"^(remote\..*\.url|branch\..*\.(remote|merge))$"
ACCOUNT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
# A worker's own Git must not run repository-supplied programs while it is only read.
# Kept in step with this machine's Git hardening.
HOST_GIT = [
    "git",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.hooksPath=/dev/null",
    "--no-optional-locks",
]
HOST_ENV = {
    "GH_PROMPT_DISABLED": "1",
    "GIT_TERMINAL_PROMPT": "0",
    "GCM_INTERACTIVE": "never",
}


def remote_slug(value):
    match = re.fullmatch(
        r"(?:https://github.com/|git@github.com:|ssh://git@github.com/)([^/]+/[^/]+?)(?:\.git)?/?",
        value,
    )
    if not match or not SLUG.fullmatch(match[1]) or match[1].split("/")[1] in {".", ".."}:
        return None
    return match[1].lower()


def remote_slugs(config):
    return {
        key[7:-4]: slug
        for key, value in config.items()
        if key.startswith("remote.") and key.endswith(".url") and (slug := remote_slug(value))
    }


def repo_config(git, path):
    """Remote URLs and branch upstreams of one repository; empty when none are set."""
    try:
        return dict(
            line.split(" ", 1)
            for line in git(path, "config", "--get-regexp", CONFIG_KEYS).splitlines()
            if " " in line
        )
    except ValueError:
        return {}


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


def clone_worktrees(git, root, wanted=None, config=None):
    """The clone at ``root`` and its attached worktrees, or None when not a wanted clone.

    Three Git calls per clone replace five per worktree: the shared config supplies
    remotes and branch upstreams, ``worktree list`` supplies every path, head and
    branch. Bare, detached and prunable entries are skipped, as ``checkout`` skips them.
    ``wanted`` of None accepts every clone, for callers that inventory all of them.
    """
    config = repo_config(git, root) if config is None else config
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


def scanned_roots(src, roots, repo_roots):
    """Clone roots a scan may touch: herdr's own, the configured ones, and those in ``src``."""
    found = {Path(root).resolve() for root in repo_roots}
    found.update(root.resolve() for root in roots if (root / ".git").exists())
    if src.exists():
        found.update(p.resolve() for p in src.iterdir() if (p / ".git").is_dir())
    return found


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


def landed(git, path, missing, merges):
    """Of commits no remote carries by name, those whose content is upstream anyway.

    A branch rebased before it merged keeps every change and loses every commit name,
    so the local copy looks entirely unpushed while the base carries the same patches.
    `git cherry` compares patch ids rather than names and marks those `-`. It skips
    merge commits, which hold nothing of their own beyond a conflict resolution, so a
    merge is landed exactly when its combined diff against all parents is empty.

    Unknowable is not landed, and the error is always towards keeping work: without a
    base branch, when Git declines to answer, or when a squash folded several commits
    into one patch equal to none of them, every commit stays counted.
    """
    try:
        base = git(path, "rev-parse", "--abbrev-ref", "origin/HEAD")
        equivalent = {
            line[2:].strip()
            for line in git(path, "cherry", base, "HEAD").splitlines()
            if line.startswith("-")
        }
    except (OSError, ValueError, subprocess.SubprocessError):
        equivalent = set()
    found = {sha for sha in missing if sha in equivalent}
    for sha in merges - found:
        try:
            # -U0 keeps an evil merge's combined diff small; only its emptiness matters.
            if not git(path, "show", "--format=", "--cc", "-U0", sha, strip=False).strip():
                found.add(sha)
        except (OSError, ValueError, subprocess.SubprocessError):
            continue
    return found


def local_state(git, path, branch=None):
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
        missing, merges = set(), set()
        for record in git(
            path,
            "--no-optional-locks",
            "rev-list",
            f"--max-count={MAX_UNPUSHED}",
            "--parents",
            "HEAD",
            "--not",
            "--remotes",
            *exclude,
            "--branches",
            "--tags",
        ).splitlines():
            sha, *parents = record.split()
            missing.add(sha)
            if len(parents) > 1:
                merges.add(sha)
        # Carrying a name no remote knows is not the same as carrying lost work, and
        # reading the whole set is only affordable while it stays under the ceiling.
        if missing and len(missing) < MAX_UNPUSHED:
            missing -= landed(git, path, missing, merges)
        return {
            **checkout_times(path, changes),
            "changes": counted(changes, MAX_CHANGES),
            "unpushed": len(missing),
            "error": None,
        }
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        detail = (str(exc).splitlines() or [""])[0][:120]
        return {"changes": None, "unpushed": None, "error": f"Local state unavailable: {detail}"}


def transcript_updates(paths, src, files, cwd_of):
    """Scan session stores once per inventory, matching the most specific checkout.

    ``files(root)`` lists (agent, file) candidates and ``cwd_of(file, agent)`` reads the
    working directory a session recorded, None when it has none.
    """
    roots = [src, *(Path(path) for path in paths if not Path(path).is_relative_to(src))]
    seen = set()
    updates: dict[str, float] = {}
    for root in roots:
        for agent, file in files(root):
            if file in seen:
                continue
            seen.add(file)
            recorded = cwd_of(file, agent)
            if not recorded:
                continue
            cwd = Path(recorded).resolve()
            checkout = next((str(p) for p in (cwd, *cwd.parents) if str(p) in paths), None)
            if checkout:
                try:
                    updates[checkout] = max(updates.get(checkout, 0), file.stat().st_mtime)
                except OSError:
                    pass
    return updates


def inventory(git, files, cwd_of, src, roots, repo_roots, opened):
    """One machine's whole scan: clones, worktrees, their state and session activity.

    ``opened`` are the checkout paths herdr has workspaces on, as herdr reports them;
    the answer maps each to its real path and whether it exists, so the controller never
    resolves a worker's path on its own filesystem.
    """
    src = Path(os.path.expanduser(src))
    roots = [Path(os.path.expanduser(root)) for root in roots]
    found = sorted(scanned_roots(src, roots, repo_roots))

    def survey(root):
        config = repo_config(git, root)
        return config, clone_worktrees(git, root, None, config)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        clones, checkouts = [], {}
        for config, result in pool.map(survey, found):
            if not result:
                continue
            main, items = result
            clones.append({"main": main, "items": items, "config": config})
            for item in items:
                checkouts[item["path"]] = item
        paths = {}
        for path in opened:
            real = os.path.realpath(path)
            paths[path] = {"path": real, "exists": os.path.exists(real)}
        mains = {clone["main"]["path"] for clone in clones}
        shown = {value["path"] for value in paths.values()}
        # A clone's own checkout is only a workspace while herdr holds it open.
        selected = [path for path in checkouts if path not in mains or path in shown]
        pending = {
            path: pool.submit(local_state, git, path, checkouts[path].get("branch"))
            for path in selected
        }
        states = {path: future.result() for path, future in pending.items()}
    updates = transcript_updates(set(selected), src.resolve(), files, cwd_of)
    return {
        "roots": [str(root) for root in found],
        "clones": clones,
        "states": states,
        "updates": updates,
        "paths": paths,
    }


# The diff and session details the viewer shows -------------------------------
#
# ``output(path, *args, limit=None)`` runs Git in a checkout and returns (text, truncated),
# keeping at most ``limit`` bytes (MAX_DIFF by default): the controller's own reader
# locally, ``host_output`` on a worker.

# Branch names a topic branch is cut from, on the remotes pull requests target.
BASE_BRANCH = re.compile(
    r"^(main|master|dev|develop|trunk|next|release[_-]v?\d+(\.\d+)*|\d+\.\d+)$"
)
BASE_PATTERNS = (
    "main",
    "master",
    "dev",
    "develop",
    "trunk",
    "next",
    "release_*",
    "release-*",
    "[0-9]*",
)
BASE_REMOTES = ("upstream", "origin")
MAX_DIFF = 3 * 1024 * 1024
MAX_FILE_LINES = 4000
MAX_UNTRACKED = 200
MAX_COMMITS = 200
SESSION = re.compile(r"^[0-9A-Za-z][0-9A-Za-z-]{7,63}$")
MAX_SESSIONS = 50

# Context Codex injects as user messages ahead of the typed prompt.
CODEX_CONTEXT = re.compile(r"^\s*(# AGENTS\.md instructions|<[a-z_]+>)")
# Claude records slash commands, `!` shell runs, their output and background-task
# notifications as user turns wrapped in tags; reminders it injects ride along inside them.
CLAUDE_WRAPPED = re.compile(r"<(command-|bash-|local-command-|task-notification>)")
CLAUDE_TAG = re.compile(r"<([a-z][a-z-]*)>(.*?)</\1>", re.S)
SYSTEM_REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)


def git_text(output, path, *args):
    return output(path, *args, limit=256 * 1024)[0].strip()


def bases(output, path):
    """Remote base branches by how few commits this checkout has beyond each.

    One for-each-ref call counts them all (``ahead-behind`` needs Git 2.41): its second
    number is the commits HEAD has that the ref does not.
    """
    listing = git_text(
        output,
        path,
        "for-each-ref",
        "--format=%(refname:short) %(ahead-behind:HEAD)",
        *[f"refs/remotes/{remote}/{name}" for remote in BASE_REMOTES for name in BASE_PATTERNS],
    )
    found = []
    for line in listing.splitlines():
        parts = line.split(" ")
        if (
            len(parts) != 3
            or not (parts[1].isdigit() and parts[2].isdigit())
            or "/" not in parts[0]
        ):
            continue
        remote, name = parts[0].split("/", 1)
        if remote in BASE_REMOTES and BASE_BRANCH.match(name):
            # On a tie, the base that moved on least since, then the pull request target.
            found.append((int(parts[2]), int(parts[1]), BASE_REMOTES.index(remote), parts[0]))
    found.sort()
    return [{"ref": ref, "ahead": ahead} for ahead, _, _, ref in found]


def split_patch(text, truncated, max_lines=MAX_FILE_LINES):
    """One entry per file of a unified diff, each bounded in lines."""
    files: list[dict] = []
    for block in re.split(r"(?m)^(?=diff --git )", text):
        if not block.startswith("diff --git "):
            continue
        # Only newlines end a diff line; splitlines would also split on form feeds.
        lines = block.removesuffix("\n").split("\n")
        header = lines[0]
        old = new = None
        status = "modified"
        binary = False
        for line in lines[1:]:
            if line.startswith("@@"):
                break
            if line.startswith(("--- ", "+++ ")):
                # Git ends a name that contains a space with a tab in these two lines.
                name = line[4:].removesuffix("\t")
                name = (
                    None if name == "/dev/null" else name[2:] if name[:2] in {"a/", "b/"} else name
                )
                if line.startswith("---"):
                    old = name
                else:
                    new = name
            elif line.startswith("new file mode"):
                status = "added"
            elif line.startswith("deleted file mode"):
                status = "deleted"
            elif line.startswith("rename from "):
                status, old = "renamed", line[len("rename from ") :]
            elif line.startswith("rename to "):
                new = line[len("rename to ") :]
            elif line.startswith("Binary files "):
                binary = True
        if old is None and new is None:
            # A pure mode change or binary file: take the names from the header.
            match = re.match(r"diff --git a/(.*) b/(.*)$", header)
            old = new = match[2] if match else header[len("diff --git ") :]
        start = next((i for i, line in enumerate(lines) if line.startswith("@@")), len(lines))
        body = lines[start:]
        added = sum(1 for line in body if line.startswith("+"))
        removed = sum(1 for line in body if line.startswith("-"))
        files.append(
            {
                "path": new or old,
                "old_path": old if status == "renamed" else None,
                "status": status,
                "binary": binary,
                "added": added,
                "removed": removed,
                "lines": body[:max_lines],
                "truncated": len(body) > max_lines,
            }
        )
    if truncated and files:
        files[-1]["truncated"] = True
    return files


def diff(output, path, scope="branch", base=None, max_lines=MAX_FILE_LINES):
    """Changes in a checkout: against its base branch (with uncommitted work) or HEAD."""
    if scope not in {"branch", "uncommitted"}:
        raise ValueError("Choose the branch or uncommitted changes")
    found = bases(output, path) if scope == "branch" else []
    commits: list[dict] = []
    note = None
    if scope == "branch" and not found and base is None:
        scope = "uncommitted"
        note = "No base branch on the upstream or origin remote; showing uncommitted changes."
    if scope == "branch":
        if base is None:
            base = found[0]["ref"]
        elif base not in {b["ref"] for b in found}:
            raise ValueError("Choose one of the listed base branches")
        since = git_text(output, path, "merge-base", base, "HEAD")
        log = git_text(
            output,
            path,
            "log",
            "-z",
            f"--max-count={MAX_COMMITS}",
            "--format=%H%x00%an%x00%at%x00%s%x00%b",
            f"{since}..HEAD",
        )
        # NUL-delimited fields keep multiline bodies separate from other commits.
        fields = log.split("\0")
        for offset in range(0, len(fields) - 4, 5):
            sha, author, when, subject, body = fields[offset : offset + 5]
            commits.append(
                {
                    "sha": sha[:12],
                    "author": author,
                    "time": int(when or 0),
                    "subject": subject,
                    "body": body.rstrip("\n"),
                }
            )
    else:
        since = "HEAD"
    # The working tree against the starting point: committed and uncommitted together.
    text, truncated = output(
        path,
        "diff",
        "--no-color",
        "--no-ext-diff",
        "--no-textconv",
        "--find-renames",
        # Fixed prefixes, whatever diff.noprefix or diff.mnemonicPrefix say.
        "--src-prefix=a/",
        "--dst-prefix=b/",
        since,
        "--",
    )
    # An untracked directory is listed once, not file by file.
    untracked = git_text(
        output,
        path,
        "ls-files",
        "--others",
        "--exclude-standard",
        "--directory",
        "--no-empty-directory",
    ).splitlines()
    files = split_patch(text, truncated, max_lines)
    return {
        "scope": scope,
        "note": note,
        "base": base,
        "bases": found[:10],
        "commits": commits,
        "files": files,
        "untracked": untracked[:MAX_UNTRACKED],
        "untracked_more": max(0, len(untracked) - MAX_UNTRACKED),
        "added": sum(f["added"] for f in files),
        "removed": sum(f["removed"] for f in files),
        "truncated": truncated,
    }


def block_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            b.get("text", "") if isinstance(b, dict) else str(b)
            for b in content
            if not isinstance(b, dict) or b.get("type") in {"text", "input_text", "output_text"}
        )
    return ""


def claude_prompt(text):
    """How a Claude user turn reads, as (kind, text), or None when it shows nothing.

    Kinds: "prompt" as typed, "command" for a slash command or `!` shell run, "output"
    for what one printed, and "notification" for a background task's report.
    """
    text = SYSTEM_REMINDER.sub("", text).strip()
    # Tags are parsed only in Claude's own wrappers: in a long typed prompt, unclosed
    # tags would each be searched for to its end.
    if not CLAUDE_WRAPPED.match(text):
        return ("prompt", text) if text else None
    tags = {m.group(1): m.group(2).strip() for m in CLAUDE_TAG.finditer(text)}
    if "command-name" in tags:
        return "command", " ".join(filter(None, (tags["command-name"], tags.get("command-args"))))
    if "bash-input" in tags:
        return "command", f"! {tags['bash-input']}"
    printed = [tags.get(k) for k in ("local-command-stdout", "bash-stdout", "bash-stderr")]
    if any(k in tags for k in ("local-command-stdout", "bash-stdout", "bash-stderr")):
        return ("output", "\n".join(filter(None, printed))) if any(printed) else None
    if "local-command-caveat" in tags:
        return None
    if "task-notification" in tags:
        inner = {
            m.group(1): m.group(2).strip() for m in CLAUDE_TAG.finditer(tags["task-notification"])
        }
        return "notification", inner.get(
            "summary"
        ) or f"Background task {inner.get('status', 'ended')}"
    return "prompt", text


def claude_text(item):
    content = (item.get("message") or {}).get("content")
    if isinstance(content, list) and any(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in content
    ):
        return ""
    return block_text(content).strip()


def records(stream):
    for line in stream:
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


def claude_details(path):
    """Session ID, working directory and first prompt of a Claude transcript."""
    cwd = title = command = None
    with path.open(encoding="utf-8", errors="replace") as stream:
        for item in records(stream):
            if item.get("type") not in {"user", "assistant"} or item.get("isSidechain"):
                continue
            cwd = cwd or item.get("cwd")
            if item.get("type") == "user" and not item.get("isMeta"):
                view = claude_prompt(claude_text(item))
                if view and not item.get("isCompactSummary"):
                    # A typed prompt names the session; failing one, its first command.
                    if view[0] == "prompt":
                        title = view[1]
                        break
                    if view[0] == "command":
                        command = command or view[1]
    if not cwd:
        return None
    return {
        "agent": "claude",
        "id": path.stem,
        "cwd": str(Path(cwd).resolve()),
        "title": title or command,
    }


def codex_details(path):
    """Session ID, working directory and first typed prompt of a Codex rollout."""
    with path.open(encoding="utf-8", errors="replace") as stream:
        first = stream.readline()
        if not first.endswith("\n"):
            raise BlockingIOError  # Still being written; look again next time.
        try:
            meta = json.loads(first)
        except json.JSONDecodeError:
            return None
        payload = meta.get("payload") or {}
        if meta.get("type") != "session_meta" or not payload.get("id") or not payload.get("cwd"):
            return None
        title = None
        for line in stream:
            if '"user' not in line:  # user_message events and user-role items
                continue
            try:
                item = json.loads(line).get("payload") or {}
            except json.JSONDecodeError:
                continue
            if item.get("type") == "user_message":
                text = str(item.get("message") or "").strip()
            elif item.get("type") == "message" and item.get("role") == "user":
                text = block_text(item.get("content")).strip()
                text = "" if CODEX_CONTEXT.match(text) else text
            else:
                continue
            if text:
                title = text
                break
    return {
        "agent": "codex",
        "id": str(payload["id"]),
        "cwd": str(Path(payload["cwd"]).resolve()),
        "title": title,
    }


# The worker side -------------------------------------------------------------


class Expired(Exception):
    """The probe's deadline passed; deliberately none of the errors a scan step absorbs."""


# Monotonic time after which no further Git starts; None when no deadline was given.
DEADLINE = None


def _within_deadline(timeout):
    if DEADLINE is not None:
        left = DEADLINE - time.monotonic()
        if left <= 0:
            raise Expired("Probe deadline expired")
        timeout = min(timeout, left)
    return timeout


def host_git(path, *args, timeout=60, strip=True):
    """Git in one checkout on the machine this probe runs on, hardened and non-interactive."""
    result = subprocess.run(
        [*HOST_GIT, "-C", str(path), *args],
        env={**os.environ, **HOST_ENV},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=_within_deadline(timeout),
    )
    if result.returncode:
        raise ValueError((result.stderr or result.stdout or "Git failed")[-4000:].strip())
    return result.stdout.strip() if strip else result.stdout


def host_output(path, *args, limit=None, timeout=GIT_TIMEOUT):
    """``host_git`` keeping at most ``limit`` bytes of its output; returns (text, truncated).

    The viewer's reader on a worker: Git stops once the limit is read, and names come
    unquoted, as the controller's own reader shows them.
    """
    limit = MAX_DIFF if limit is None else limit
    timeout = _within_deadline(timeout)
    deadline = time.monotonic() + timeout
    chunks: list[bytes] = []
    size = 0
    truncated = False
    with tempfile.TemporaryFile() as errors:
        proc = subprocess.Popen(
            [*HOST_GIT, "-c", "core.quotePath=false", "-C", str(path), *args],
            env={**os.environ, **HOST_ENV},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=errors,
        )
        assert proc.stdout is not None
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(proc.stdout, selectors.EVENT_READ)
                while not truncated:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ValueError(f"git {args[0]} took longer than {timeout:.0f} seconds")
                    if not selector.select(remaining):
                        continue
                    data = os.read(proc.stdout.fileno(), 65536)
                    if not data:
                        break
                    chunks.append(data[: limit - size])
                    size += len(chunks[-1])
                    truncated = size >= limit
            if not truncated and proc.wait(timeout=max(0.1, deadline - time.monotonic())):
                errors.seek(0)
                detail = errors.read()[-4000:].decode("utf-8", errors="replace").strip()
                raise ValueError(detail[-400:] or f"git {args[0]} failed")
        finally:
            if proc.poll() is None:
                proc.kill()  # Enough was read, or too slowly.
            proc.wait()
            proc.stdout.close()
    return b"".join(chunks).decode("utf-8", errors="replace"), truncated


def host_claude_homes():
    default = Path(os.path.expanduser("~/.claude")).resolve()
    current = Path(os.environ.get("CLAUDE_CONFIG_DIR") or default).expanduser().resolve()
    homes = [current, default]
    try:
        entries = sorted((default / "accounts").iterdir())
    except OSError:
        entries = []
    homes += [e.resolve() for e in entries if ACCOUNT.fullmatch(e.name) and e.is_dir()]
    return list(dict.fromkeys(homes))


def host_session_files(root):
    """This machine's transcript candidates for ``root``, as the controller lists its own."""
    # Claude names a project folder after its cwd, every other character a dash.
    folder = re.sub(r"[^A-Za-z0-9]", "-", str(root))
    for home in host_claude_homes():
        try:
            folders = [
                entry
                for entry in (home / "projects").iterdir()
                if entry.name == folder or entry.name.startswith(folder + "-")
            ]
        except OSError:
            folders = []
        for entry in folders:
            for path in entry.glob("*.jsonl"):
                yield "claude", path
    codex = Path(os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")) / "sessions"
    for path in codex.glob("*/*/*/rollout-*.jsonl"):
        yield "codex", path


def host_cwd(path, agent):
    """The working directory a session recorded: Claude's first turn, Codex's metadata."""
    try:
        with path.open(encoding="utf-8", errors="replace") as stream:
            if agent == "codex":
                first = stream.readline()
                if not first.endswith("\n"):
                    return None  # Still being written.
                meta = json.loads(first)
                payload = meta.get("payload") or {}
                if meta.get("type") == "session_meta" and payload.get("id"):
                    return payload.get("cwd") or None
                return None
            for line in stream:
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                if (
                    isinstance(item, dict)
                    and item.get("type") in {"user", "assistant"}
                    and not item.get("isSidechain")
                    and item.get("cwd")
                ):
                    return item["cwd"]
    except (OSError, ValueError, AttributeError):
        pass
    return None


def _arg_paths(value, home=False):
    """Worker-side check of path arguments: absolute, or under ``~`` where allowed."""
    prefixes = ("/", "~/") if home else ("/",)
    if not isinstance(value, list) or not all(
        isinstance(path, str) and "\0" not in path and path.startswith(prefixes) for path in value
    ):
        raise ValueError("Expected a list of absolute paths")
    return value


def _host_inventory(src, roots, repo_roots, opened):
    if src != "~":
        _arg_paths([src], home=True)
    return inventory(
        host_git,
        host_session_files,
        host_cwd,
        src,
        _arg_paths(roots, home=True),
        _arg_paths(repo_roots),
        _arg_paths(opened),
    )


def host_session_roots():
    """Where this machine's transcripts live; ``tail`` reads nothing outside them."""
    codex = Path(os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")) / "sessions"
    return [(home / "projects").resolve() for home in host_claude_homes()] + [codex.resolve()]


def host_accounts():
    """Claude homes by their account name, as ``claude-account`` names them."""
    default = Path(os.path.expanduser("~/.claude")).resolve()
    names = {default: "default"}
    try:
        entries = sorted((default / "accounts").iterdir())
    except OSError:
        entries = []
    for entry in entries:
        if entry.name != "default" and ACCOUNT.fullmatch(entry.name) and entry.is_dir():
            names.setdefault(entry.resolve(), entry.name)
    return names


def _host_checkout(path):
    """The real path of a checkout the controller names, which must be a checkout's top."""
    _arg_paths([path])
    real = os.path.realpath(path)
    if os.path.realpath(host_git(real, "rev-parse", "--show-toplevel")) != real:
        raise ValueError("Not at the top of a Git checkout")
    return real


def _host_diff(path, scope, base):
    if base is not None and not isinstance(base, str):
        raise ValueError("Expected a base branch name")
    return diff(host_output, _host_checkout(path), scope, base)


def _host_bases(path):
    return bases(host_output, _host_checkout(path))


def _host_sessions(root):
    """Sessions recorded in ``root`` or below it, newest first, as the viewer lists them."""
    _arg_paths([root])
    root = Path(os.path.realpath(root))
    accounts = host_accounts()
    listed = []
    for agent, file in host_session_files(root):
        try:
            found = claude_details(file) if agent == "claude" else codex_details(file)
            info = file.stat()
        except (OSError, ValueError):
            continue  # Unreadable, or a rollout still writing its first line.
        if (
            not found
            or not SESSION.match(found["id"])
            or not Path(found["cwd"]).is_relative_to(root)
        ):
            continue
        title = (found["title"] or "").strip()
        # A Claude transcript sits in HOME/projects/FOLDER/, under a home already resolved.
        home = file.parents[2] if agent == "claude" else None
        listed.append(
            {
                "id": found["id"],
                "agent": agent,
                "cwd": found["cwd"],
                "cwd_exists": os.path.isdir(found["cwd"]),
                "title": title.splitlines()[0][:160] if title else None,
                "updated": info.st_mtime,
                "size": info.st_size,
                "file": str(file),
                "home": str(home) if home else None,
                "account": accounts.get(home) if home else None,
            }
        )
    listed.sort(key=lambda s: s["updated"], reverse=True)
    return listed[:MAX_SESSIONS]


def _host_tail(file, inode, offset, limit):
    """Complete lines of one listed transcript from ``offset``, at most ``limit`` bytes.

    A file replaced since (another ``inode``) or truncated below ``offset`` is read from
    the start, which the answer's offset says. A full read inside one line longer than
    ``limit`` is returned as it is, for the controller to join with the next.
    """
    _arg_paths([file])
    if not (inode is None or (type(inode) is int and inode >= 0)):
        raise ValueError("Expected an inode number")
    if not (type(offset) is int and offset >= 0 and type(limit) is int and 0 < limit <= MAX_TAIL):
        raise ValueError("Expected a byte offset and limit")
    path = Path(os.path.realpath(file))
    if path.suffix != ".jsonl" or not any(path.is_relative_to(r) for r in host_session_roots()):
        raise ValueError("Not a session transcript of this machine")
    # Non-blocking, so a FIFO planted in a session store cannot hold the probe open.
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("Not a session transcript of this machine")
        start = offset if info.st_ino == inode and info.st_size >= offset else 0
        stream.seek(start)
        data = stream.read(limit)
    end = data.rfind(b"\n")
    if end >= 0:
        data = data[: end + 1]
    elif len(data) < limit:
        data = b""  # A line still being written is read next time.
    return {
        "inode": info.st_ino,
        "size": info.st_size,
        "offset": start,
        "data": base64.b64encode(data).decode(),
    }


OPS: dict[str, Callable[..., Any]] = {
    "inventory": _host_inventory,
    "diff": _host_diff,
    "bases": _host_bases,
    "sessions": _host_sessions,
    "tail": _host_tail,
}


def _expire(signum, frame):
    # A probe leading its own process group (see __main__) stops its Git children too;
    # it ignores the signal it sends them, then unwinds to report and exit.
    if os.getpgrp() == os.getpid():
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        os.killpg(0, signal.SIGTERM)
    raise Expired("Probe deadline expired")


def main(argv):
    """Answer one request; a ``deadline`` in seconds bounds the whole run on this worker.

    A killed ssh client leaves the probe running, so it carries its own lifeline: an
    alarm at the deadline, and no Git started after it. Expiry exits with EXPIRED.
    """
    global DEADLINE
    code = 0
    previous = None
    try:
        if len(argv) != 3 or argv[1] != PROTOCOL:
            raise ValueError(f"Probe protocol mismatch; this worker speaks {PROTOCOL}")
        request = json.loads(base64.b64decode(argv[2], validate=True))
        if not isinstance(request, dict) or request.get("op") not in OPS:
            raise ValueError("Unknown probe op")
        if not isinstance(request.get("args"), dict):
            raise ValueError("Probe arguments must be an object")
        deadline = request.get("deadline")
        if deadline is not None:
            if not (
                isinstance(deadline, (int, float))
                and not isinstance(deadline, bool)
                and 0 < deadline <= MAX_DEADLINE
            ):
                raise ValueError("Probe deadline must be a number of seconds")
            DEADLINE = time.monotonic() + deadline
            previous = signal.signal(signal.SIGALRM, _expire)
            signal.setitimer(signal.ITIMER_REAL, deadline)
        answer = {"protocol": PROTOCOL, "result": OPS[request["op"]](**request["args"])}
    except Expired as exc:
        code = EXPIRED
        answer = {"protocol": PROTOCOL, "error": str(exc)}
    except Exception as exc:
        answer = {"protocol": PROTOCOL, "error": f"{type(exc).__name__}: {exc}"[:400]}
    finally:
        if previous is not None:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)
        DEADLINE = None
    # UTF-8 rather than \u escapes keeps a diff's answer close to its own size. A lone
    # surrogate (an undecodable file name) comes out as the escape JSON would have used.
    sys.stdout.flush()
    sys.stdout.buffer.write(
        json.dumps(answer, ensure_ascii=False).encode("utf-8", "backslashreplace")
    )
    sys.stdout.flush()
    return code


# The controller side ---------------------------------------------------------


def _text(value):
    return isinstance(value, str) and len(value) <= MAX_TEXT


def _absolute(value):
    return _text(value) and value.startswith("/") and "\0" not in value


def _expect(ok, what):
    if not ok:
        raise ValueError(f"Malformed probe answer: {what}")


def _time(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and 0 <= value < LATEST_TIME
    )


def _count(value, limit):
    return value is None or (type(value) is int and 0 <= value <= limit)


def _constant(name):
    raise ValueError(f"non-finite number {name}")


def validate_inventory(value):
    """Check a worker's inventory answer field by field before the controller reads it."""
    _expect(isinstance(value, dict), "not an object")
    _expect(set(value) == {"roots", "clones", "states", "updates", "paths"}, "fields")
    _expect(isinstance(value["roots"], list) and all(map(_absolute, value["roots"])), "roots")
    _expect(isinstance(value["clones"], list), "clones")
    for clone in value["clones"]:
        _expect(isinstance(clone, dict) and set(clone) == {"main", "items", "config"}, "clone")
        config = clone["config"]
        _expect(
            isinstance(config, dict) and all(_text(k) and _text(v) for k, v in config.items()),
            "config",
        )
        _expect(isinstance(clone["items"], list) and clone["main"] in clone["items"], "items")
        for item in clone["items"]:
            _expect(isinstance(item, dict), "item")
            _expect(_absolute(item.get("path")) and _absolute(item.get("common")), "item path")
            _expect(_text(item.get("branch")), "branch")
            _expect(_text(item.get("sha")), "sha")
            remotes = item.get("remotes")
            _expect(isinstance(remotes, list) and all(map(_text, remotes)), "remotes")
            upstream = item.get("upstream")
            _expect(
                upstream is None
                or (
                    isinstance(upstream, list)
                    and len(upstream) == 2
                    and all(part is None or _text(part) for part in upstream)
                ),
                "upstream",
            )
    paths = [item["path"] for clone in value["clones"] for item in clone["items"]]
    listed = set(paths)
    _expect(len(listed) == len(paths), "item listed twice")
    _expect(isinstance(value["states"], dict), "states")
    for path, state in value["states"].items():
        _expect(path in listed and isinstance(state, dict), "state")
        _expect(
            all(state.get(k) is None or _time(state[k]) for k in ("created_at", "updated_at"))
            and _count(state.get("changes"), MAX_CHANGES)
            and _count(state.get("unpushed"), MAX_UNPUSHED),
            "state values",
        )
        _expect(state.get("error") is None or _text(state["error"]), "state error")
        _expect({"changes", "unpushed", "error"} <= set(state), "state fields")
    _expect(isinstance(value["updates"], dict), "updates")
    for path, when in value["updates"].items():
        _expect(path in value["states"] and _time(when), "update")
    _expect(isinstance(value["paths"], dict), "paths")
    for path, info in value["paths"].items():
        _expect(
            _text(path)
            and isinstance(info, dict)
            and set(info) == {"path", "exists"}
            and _absolute(info["path"])
            and isinstance(info["exists"], bool),
            "resolved path",
        )
    return value


def _int(value):
    return type(value) is int and value >= 0


def _optional_text(value):
    return value is None or _text(value)


def validate_bases(value):
    _expect(isinstance(value, list), "bases")
    for item in value:
        _expect(
            isinstance(item, dict)
            and set(item) == {"ref", "ahead"}
            and _text(item["ref"])
            and _int(item["ahead"]),
            "base",
        )
    return value


def validate_diff(value):
    """Check a worker's diff, field by field, against what ``diff`` returns."""
    _expect(isinstance(value, dict), "not an object")
    _expect(
        set(value)
        == {
            "scope",
            "note",
            "base",
            "bases",
            "commits",
            "files",
            "untracked",
            "untracked_more",
            "added",
            "removed",
            "truncated",
        },
        "fields",
    )
    _expect(value["scope"] in {"branch", "uncommitted"}, "scope")
    _expect(_optional_text(value["note"]) and _optional_text(value["base"]), "note or base")
    _expect(len(validate_bases(value["bases"])) <= 10, "bases")
    commits = value["commits"]
    _expect(isinstance(commits, list) and len(commits) <= MAX_COMMITS, "commits")
    for commit in commits:
        _expect(
            isinstance(commit, dict)
            and set(commit) == {"sha", "author", "time", "subject", "body"}
            and all(isinstance(commit[k], str) for k in ("sha", "author", "subject", "body"))
            and _int(commit["time"]),
            "commit",
        )
    _expect(isinstance(value["files"], list), "files")
    for item in value["files"]:
        _expect(
            isinstance(item, dict)
            and set(item)
            == {"path", "old_path", "status", "binary", "added", "removed", "lines", "truncated"},
            "file",
        )
        _expect(_text(item["path"]) and _optional_text(item["old_path"]), "file path")
        _expect(item["status"] in {"modified", "added", "deleted", "renamed"}, "file status")
        _expect(_int(item["added"]) and _int(item["removed"]), "file counts")
        _expect(isinstance(item["binary"], bool) and isinstance(item["truncated"], bool), "flags")
        lines = item["lines"]
        _expect(
            isinstance(lines, list)
            and len(lines) <= MAX_FILE_LINES
            and all(isinstance(line, str) for line in lines),
            "file lines",
        )
    untracked = value["untracked"]
    _expect(
        isinstance(untracked, list)
        and len(untracked) <= MAX_UNTRACKED
        and all(map(_text, untracked)),
        "untracked",
    )
    _expect(all(_int(value[k]) for k in ("untracked_more", "added", "removed")), "counts")
    _expect(isinstance(value["truncated"], bool), "truncated")
    return value


def validate_sessions(value):
    """Check a worker's session listing; its files are the only ones ``tail`` is asked for."""
    _expect(isinstance(value, list) and len(value) <= MAX_SESSIONS, "sessions")
    for item in value:
        _expect(
            isinstance(item, dict)
            and set(item)
            == {
                "id",
                "agent",
                "cwd",
                "cwd_exists",
                "title",
                "updated",
                "size",
                "file",
                "home",
                "account",
            },
            "session",
        )
        _expect(isinstance(item["id"], str) and SESSION.match(item["id"]), "session id")
        _expect(item["agent"] in {"claude", "codex"}, "session agent")
        _expect(_absolute(item["cwd"]) and isinstance(item["cwd_exists"], bool), "session cwd")
        _expect(
            item["title"] is None or (_text(item["title"]) and len(item["title"]) <= 160), "title"
        )
        _expect(_time(item["updated"]) and _int(item["size"]), "session file facts")
        _expect(_absolute(item["file"]) and item["file"].endswith(".jsonl"), "session file")
        _expect(item["home"] is None or _absolute(item["home"]), "session home")
        _expect(
            item["account"] is None
            or (isinstance(item["account"], str) and ACCOUNT.fullmatch(item["account"])),
            "session account",
        )
    return value


def validate_tail(value):
    """Check a worker's transcript read; its bytes come back decoded."""
    _expect(isinstance(value, dict) and set(value) == {"inode", "size", "offset", "data"}, "fields")
    _expect(all(_int(value[k]) for k in ("inode", "size", "offset")), "positions")
    _expect(isinstance(value["data"], str), "data")
    try:
        data = base64.b64decode(value["data"], validate=True)
    except ValueError as exc:
        raise ValueError("Malformed probe answer: data") from exc
    _expect(len(data) <= MAX_TAIL, "data size")
    return {**value, "data": data}


VALIDATORS = {
    "inventory": validate_inventory,
    "diff": validate_diff,
    "bases": validate_bases,
    "sessions": validate_sessions,
    "tail": validate_tail,
}


def call(home, machine, op, timeout, **args):
    """Run ``op`` on a worker: this file's source on stdin, the arguments as base64 JSON.

    Raises ValueError with the worker's own words when it is unreachable, the probe
    fails or the answer is oversized or malformed; ``subprocess.TimeoutExpired`` when
    the worker takes longer than ``timeout``, which is also the probe's own deadline
    there. Its stdout is read only up to MAX_STDOUT before the ssh client is stopped.
    """
    import machines  # The controller's transport; workers never import it.
    import owned_process

    request = {"op": op, "args": args, "deadline": max(1, min(timeout, MAX_DEADLINE))}
    payload = base64.b64encode(json.dumps(request).encode()).decode()
    python = machine.python
    if python.startswith(("/", "~")):
        python = machines.remote_path(python)
    try:
        result = machines.run(
            home,
            machine,
            [python, "-I", "-", PROTOCOL, payload],
            timeout=timeout,
            input=Path(__file__).read_bytes(),
            max_output=MAX_STDOUT,
        )
    except owned_process.OutputLimitExceeded as exc:
        raise ValueError(f"{machine.label} probe answer exceeded {MAX_STDOUT} bytes") from exc
    stderr = result.stderr[-MAX_STDERR:].decode(errors="replace").strip()
    if result.returncode:
        reason = "unreachable" if result.returncode == 255 else f"exit {result.returncode}"
        raise ValueError(f"{machine.label} probe failed ({reason}): {stderr or 'no output'}")
    if len(result.stdout) > MAX_STDOUT:
        raise ValueError(f"{machine.label} probe answer exceeded {MAX_STDOUT} bytes")
    try:
        answer = json.loads(result.stdout, parse_constant=_constant)
    except ValueError as exc:
        raise ValueError(f"{machine.label} probe answered no JSON: {stderr or exc}") from exc
    if not isinstance(answer, dict) or answer.get("protocol") != PROTOCOL:
        raise ValueError(f"{machine.label} probe speaks another protocol")
    if "error" in answer:
        raise ValueError(f"{machine.label} probe failed: {str(answer['error'])[:400]}")
    return VALIDATORS[op](answer.get("result"))


if __name__ == "__main__":
    try:
        os.setpgid(0, 0)  # Its own group, so an expired deadline stops its Git children.
    except OSError:
        pass  # Already leading its session, and so its group.
    raise SystemExit(main(sys.argv))
