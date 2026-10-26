"""Read-only Git and session-store inventory, run here in-process or streamed to a worker.

Each op takes its ``git`` runner (and session-store readers) as arguments: this machine
passes its own wrappers, so local behaviour is exactly the code that ran before. A
worker gets this file's source on ``python3 -I -S -`` over ssh with the op and its
arguments as base64 JSON, so nothing is installed there and no shell ever sees a path.
It answers one JSON document, which ``call`` bounds and validates before anyone reads it.

Stdlib only, and Python 3.9 compatible: it runs on whatever ``python3`` a worker has.
"""

from __future__ import annotations

import base64
import concurrent.futures
import hashlib
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
import threading
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
# A worker's own Git must not run repository-supplied programs while it is only read:
# agents write these checkouts, and the probe runs unsandboxed as the worker's user.
# ``host_guard`` adds the overrides that depend on each repository's config.
HOST_CONFIG = [
    "core.fsmonitor=false",
    "core.hooksPath=/dev/null",
    # No signature is ever checked, so no configured program is asked to.
    "log.showSignature=false",
    "gpg.program=false",
    "gpg.openpgp.program=false",
    "gpg.ssh.program=false",
    "gpg.x509.program=false",
    # A submodule's diff stays one commit line rather than a diff run inside it.
    "diff.submodule=short",
    # Nothing is fetched, not even a promisor remote's missing object: no transport,
    # and so no configured upload-pack or ssh command, is allowed.
    *[
        f"protocol.{name}.allow=never"
        for name in ("file", "git", "ssh", "http", "https", "ftp", "ftps", "ext")
    ],
    "protocol.allow=never",
]
HOST_GIT = [
    "git",
    *[arg for setting in HOST_CONFIG for arg in ("-c", setting)],
    "--no-optional-locks",
]
HOST_ENV = {
    "GH_PROMPT_DISABLED": "1",
    "GIT_TERMINAL_PROMPT": "0",
    "GCM_INTERACTIVE": "never",
    "GIT_NO_LAZY_FETCH": "1",
}
# Subcommands that look into a submodule's own repository, whose config is not guarded.
SUBMODULE_READS = {"status", "diff"}
# Reads need no GitHub token, and a checkout's filters or hooks must not inherit one.
TOKENS = ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN")
# The one fetch a cron checkout makes: its job's repository by GitHub URL, the worker's
# own gh login for credentials, and no transport but those (https, or what the worker's
# own config rewrites it to); the repository's own rewrites and TLS settings refuse it.
FETCH_CONFIG = [
    *[f"protocol.{name}.allow=always" for name in ("https", "ssh", "file")],
    "credential.helper=",
    "credential.https://github.com.helper=",
    "credential.https://github.com.helper=!gh auth git-credential",
    "core.askPass=",
    "core.sshCommand=ssh",
    "core.alternateRefsCommand=",
    "gc.auto=0",
    "maintenance.auto=false",
    "submodule.recurse=false",
    "fetch.recurseSubmodules=false",
]
TRUSTED_SCOPES = {"system", "global", "command"}
REDIRECT = re.compile(r"url\..+\.insteadof|remote\..*://.*")
TLS = re.compile(
    r"http\.(.+\.)?(sslverify|sslcainfo|sslcapath|sslbackend|proxy|cookiefile|savecookies"
    r"|curloptresolve)|remote\..+\.proxy"
)
# A job's branch: what a cron job may name, checked again where it is used.
BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}")
MAX_TURN = 20000  # Characters of an agent's final response a ``turn`` answer carries.
# What a terminal acts on: never in a path that reaches a typed line.
CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
# A session listing reads at most this much of each transcript, and skips longer lines.
MAX_SCAN = 32 * 1024 * 1024
MAX_SCAN_LINE = 4 * 1024 * 1024


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
        info = root.stat()
        created = getattr(info, "st_birthtime", None)
        updated.append(info.st_mtime)
        git_dir = root / ".git"
        if git_dir.is_file():
            pointer = git_dir.read_text().strip().removeprefix("gitdir: ")
            git_dir = (root / pointer).resolve()
        log = git_dir / "logs" / "HEAD"
        try:
            # The first reflog entry records worktree creation, even on filesystems
            # without birth times. Its mtime follows commits, resets and checkouts.
            # Only a regular file is read, and only its first line's start: a FIFO
            # planted there must not block, nor a reflog without newlines be read whole.
            descriptor = os.open(log, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "rb") as stream:
                found = os.fstat(descriptor)
                if not stat.S_ISREG(found.st_mode):
                    raise OSError(f"Not a regular file: {log}")
                line = stream.readline(4096).decode("utf-8", errors="replace")
            created = float(line.split("\t", 1)[0].split()[-2])
            updated.append(found.st_mtime)
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
            # A checkout's textconv or external diff could make it look empty, or run.
            shown = git(
                path,
                "show",
                "--format=",
                "--cc",
                "-U0",
                "--no-textconv",
                "--no-ext-diff",
                sha,
                strip=False,
            )
            if not shown.strip():
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

    pool = concurrent.futures.ThreadPoolExecutor(max_workers=8)
    try:
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
    finally:
        # An expired deadline must not wait for a step that is stuck.
        pool.shutdown(wait=False, cancel_futures=True)
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
DIGEST = re.compile(r"[0-9a-f]{64}")
# Bytes of a process's command line read to compare it: more than any argument limit.
MAX_CMDLINE = 16 * 1024 * 1024
# The line a scheduled brief asks its agent to end with once done (``exit_watch``).
MARKER = re.compile(r"\[babysit-done:[0-9a-f]{16}\]")

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


def open_transcript(path):
    """A session file opened for binary reading, refused unless it is a regular file.

    Not through a symlink of its own, and without blocking on a FIFO planted in a store.
    """
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError(f"Not a regular file: {path}")
        return os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise


def open_beneath(path):
    """``open_transcript`` of an absolute, already resolved ``path``, one folder at a time.

    No folder on the way may be a symlink, even one swapped in after the path was
    checked, and the file may have no other name: a hard link would let a store name
    any file of its owner's.
    """
    parts = Path(path).parts
    folder = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts[1:-1]:
            inner = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=folder)
            os.close(folder)
            folder = inner
        descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=folder)
    finally:
        os.close(folder)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise OSError(f"Not a regular file with one name: {path}")
        return os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise


def transcript_lines(stream):
    """Decoded lines of a binary ``stream``: within MAX_SCAN bytes, none over MAX_SCAN_LINE."""
    limit, longest = MAX_SCAN, MAX_SCAN_LINE
    read = 0
    while read < limit:
        line = stream.readline(longest + 1)
        if not line:
            return
        read += len(line)
        if len(line) > longest and not line.endswith(b"\n"):
            # Too long to parse: read past the rest of it, a bounded piece at a time.
            while read < limit:
                rest = stream.readline(longest + 1)
                read += len(rest)
                if not rest or rest.endswith(b"\n"):
                    break
            continue
        yield line.decode("utf-8", errors="replace")


def codex_turn(lines, session_id):
    """The current turn of a Codex rollout's ``lines`` and its final message once complete."""
    meta, turn, complete = None, None, None
    for line in lines:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue  # the writer may still be appending the final line
        payload = item.get("payload") or {}
        if item.get("type") == "session_meta":
            meta = payload.get("id")
        elif item.get("type") == "event_msg":
            if payload.get("type") == "task_started":
                turn, complete = payload.get("turn_id"), None
            elif payload.get("type") == "task_complete" and payload.get("turn_id") == turn:
                complete = payload.get("last_agent_message", "")
            elif payload.get("type") in {"turn_aborted", "user_message"}:
                complete = None
    if meta != session_id or not turn:
        raise RuntimeError("Cannot identify the current turn in the registered rollout")
    return turn, complete


def claude_turn(lines, session_id):
    """A turn is the last typed prompt; it is complete once its turn_duration record lands.

    The completed text is the assistant text after the turn's final tool result, i.e. the
    response the user sees, which must carry the handoff marker.
    """
    turn, complete, seen = None, None, False
    texts: list[str] = []
    for line in lines:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue  # the writer may still be appending the final line
        if item.get("isSidechain"):
            continue  # subagent traffic never ends the main conversation's turn
        kind = item.get("type")
        if kind in {"user", "assistant"}:
            if item.get("sessionId") != session_id:
                raise RuntimeError("Claude transcript session ID does not match")
            seen = True
            complete = None
        content = (item.get("message") or {}).get("content")
        blocks = content if isinstance(content, list) else []
        if kind == "user":
            if any(b.get("type") == "tool_result" for b in blocks):
                texts = []  # tool output: the final response restarts after it
            elif not item.get("isMeta"):  # injected context is not a new prompt
                turn, complete, texts = item.get("uuid"), None, []
        elif kind == "assistant":
            texts.extend(b.get("text", "") for b in blocks if b.get("type") == "text")
        elif kind == "system" and item.get("subtype") == "turn_duration" and turn:
            complete = "\n".join(texts)
    if not seen or not turn:
        raise RuntimeError("Cannot identify the current turn in the registered transcript")
    return turn, complete


def first_turn(lines, agent, marker):
    """A transcript's first prompt turn, a scheduled brief, and whether it names ``marker``.

    Codex may record the prompt on either side of its turn's start; Claude's prompt is
    its turn.
    """
    turn, marked = None, False
    for item in records(lines):
        payload = item.get("payload") or {}
        if agent == "claude":
            content = (item.get("message") or {}).get("content")
            blocks = content if isinstance(content, list) else []
            if (
                item.get("type") == "user"
                and not item.get("isSidechain")
                and not item.get("isMeta")
                and not any(isinstance(b, dict) and b.get("type") == "tool_result" for b in blocks)
            ):
                return item.get("uuid"), marker in block_text(content)
            continue
        kind = (item.get("type"), payload.get("type"))
        if kind == ("event_msg", "task_started"):
            if turn is not None:
                break
            turn = payload.get("turn_id")
        elif kind == ("event_msg", "user_message"):
            marked = marked or marker in str(payload.get("message") or "")
        elif kind == ("response_item", "message") and payload.get("role") == "user":
            marked = marked or marker in block_text(payload.get("content"))
    return turn, marked


def records(lines):
    for line in lines:
        try:
            item = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if isinstance(item, dict):
            yield item


def claude_details(path):
    """Session ID, working directory and first prompt of a Claude transcript."""
    cwd = title = command = None
    with open_transcript(path) as stream:
        for item in records(transcript_lines(stream)):
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
    with open_transcript(path) as stream:
        lines = transcript_lines(stream)
        first = next(lines, "")
        if not first.endswith("\n"):
            raise BlockingIOError  # Still being written; look again next time.
        try:
            meta = json.loads(first)
        except (ValueError, RecursionError):
            return None
        if not isinstance(meta, dict):
            return None
        payload = meta.get("payload") or {}
        if not isinstance(payload, dict):
            return None
        if meta.get("type") != "session_meta" or not payload.get("id") or not payload.get("cwd"):
            return None
        title = None
        for line in lines:
            if '"user' not in line:  # user_message events and user-role items
                continue
            try:
                item = json.loads(line).get("payload") or {}
            except (ValueError, RecursionError, AttributeError):
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
# Set once the alarm fired, so expiry is reported even if it lands mid-cleanup.
EXPIRED_AT_ALARM = False
# Set once an expired probe stopped its group. A scan thread still running then starts
# no Git: DEADLINE is reset as the answer goes out, and a Git started after the group
# was stopped would outlive the probe. Starting one holds _spawning, so the last stop
# (see __main__) sees every Git started before it.
STOPPED = False
_spawning = threading.Lock()


def _within_deadline(timeout):
    if STOPPED:
        raise Expired("Probe deadline expired")
    if DEADLINE is not None:
        left = DEADLINE - time.monotonic()
        if left <= 0:
            raise Expired("Probe deadline expired")
        timeout = min(timeout, left)
    return timeout


def _past_deadline():
    # Cut short by the deadline itself, which the alarm may not have reported yet.
    return DEADLINE is not None and time.monotonic() >= DEADLINE


def host_environment():
    env = {k: v for k, v in os.environ.items() if k not in TOKENS}
    env.update(HOST_ENV)
    return env


def _host_run(argv, timeout):
    with _spawning:
        timeout = _within_deadline(timeout)
        proc = subprocess.Popen(
            argv,
            env=host_environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    with proc:
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            proc.kill()
            proc.wait()
            if _past_deadline():
                raise Expired("Probe deadline expired") from exc
            raise
    return subprocess.CompletedProcess(argv, proc.returncode, stdout, stderr)


# Overrides by repository path, read once per probe run.
_guards: dict[str, list[str]] = {}


def host_guard(path, timeout=60, cache=_guards):
    """``-c`` overrides for the programs ``path``'s own config could name.

    Attributes (``.gitattributes``, ``.git/info/attributes``) pick a filter driver by
    name, so blanking every driver the config defines covers them all, git-lfs's
    included: git-lfs, and the extensions and transfer agents it would run, never
    starts. ``core.worktree`` would point Git at another directory and is refused.
    A long-lived caller passes a fresh ``cache``, since a checkout's config can change.
    """
    key = str(path)
    if key not in cache:
        result = _host_run(
            [*HOST_GIT, "-C", key, "config", "-z", "--get-regexp", r"^filter\.|^core\.worktree$"],
            timeout,
        )
        if result.returncode not in (0, 1):  # 1: no such keys.
            raise ValueError((result.stderr or "Git config unreadable")[-4000:].strip())
        drivers: dict[str, None] = {}  # Ordered, without repeats.
        for entry in result.stdout.split("\0"):
            name = entry.split("\n", 1)[0]
            if name == "core.worktree":
                raise ValueError("core.worktree points Git at another directory; not read")
            if name.startswith("filter.") and name.count(".") >= 2:
                drivers[name[len("filter.") : name.rindex(".")]] = None
        if any("=" in driver for driver in drivers):
            raise ValueError("A filter driver's name cannot be overridden; not read")
        cache[key] = [
            arg
            for driver in drivers
            for setting in ("clean=", "smudge=", "process=", "required=false")
            for arg in ("-c", f"filter.{driver}.{setting}")
        ]
    return cache[key]


def host_argv(path, args, options=(), cache=_guards):
    """The hardened Git command line for ``args`` in ``path``, after Git ``options``."""
    command = next((arg for arg in args if not arg.startswith("-")), None)
    # Looking into a submodule's working tree would run Git there, under its own
    # filters; a new commit checked out in it still shows.
    if command in SUBMODULE_READS:
        at = args.index(command) + 1
        args = (*args[:at], "--ignore-submodules=dirty", *args[at:])
    return [*HOST_GIT, *options, *host_guard(path, cache=cache), "-C", str(path), *args]


def host_git(path, *args, timeout=60, strip=True):
    """Git in one checkout on the machine this probe runs on, hardened and non-interactive."""
    result = _host_run(host_argv(path, args), timeout)
    if result.returncode:
        raise ValueError((result.stderr or result.stdout or "Git failed")[-4000:].strip())
    return result.stdout.strip() if strip else result.stdout


def host_output(path, *args, limit=None, timeout=GIT_TIMEOUT):
    """``host_git`` keeping at most ``limit`` bytes of its output; returns (text, truncated).

    The viewer's reader on a worker: Git stops once the limit is read, and names come
    unquoted, as the controller's own reader shows them.
    """
    limit = MAX_DIFF if limit is None else limit
    chunks: list[bytes] = []
    size = 0
    truncated = False
    with tempfile.TemporaryFile() as errors:
        argv = host_argv(path, args, ("-c", "core.quotePath=false"))
        with _spawning:
            timeout = _within_deadline(timeout)
            deadline = time.monotonic() + timeout
            proc = subprocess.Popen(
                argv,
                env=host_environment(),
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
                        if _past_deadline():
                            raise Expired("Probe deadline expired")
                        raise ValueError(f"git {args[0]} took longer than {timeout:.0f} seconds")
                    if not selector.select(remaining):
                        continue
                    data = os.read(proc.stdout.fileno(), 65536)
                    if not data:
                        break
                    chunks.append(data[: limit - size])
                    size += len(chunks[-1])
                    truncated = size >= limit
            try:
                failed = not truncated and proc.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired as exc:
                if _past_deadline():
                    raise Expired("Probe deadline expired") from exc
                raise
            if failed:
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


def host_codex_sessions():
    return Path(os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")) / "sessions"


def host_session_root(path):
    """A session store's real path; None when it is a symlink or lies outside ``$HOME``."""
    home = Path(os.path.realpath(os.path.expanduser("~")))
    real = Path(os.path.realpath(path))
    if os.path.islink(path) or not real.is_relative_to(home):
        return None
    return real


def host_session_files(root):
    """This machine's transcript candidates for ``root``, as the controller lists its own.

    Only files that really lie in a session store (``host_session_roots``) are listed:
    a symlinked folder inside one leads nowhere else.
    """
    stores = host_session_roots()

    def stored(path):
        real = Path(os.path.realpath(path))
        return any(real.is_relative_to(store) for store in stores)

    # Claude names a project folder after its cwd, every other character a dash.
    folder = re.sub(r"[^A-Za-z0-9]", "-", str(root))
    for home in host_claude_homes():
        if host_session_root(home / "projects") is None:
            continue
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
                if stored(path):
                    yield "claude", path
    codex = host_codex_sessions()
    if host_session_root(codex) is None:
        return
    for path in codex.glob("*/*/*/rollout-*.jsonl"):
        if stored(path):
            yield "codex", path


def host_cwd(path, agent):
    """The working directory a session recorded: Claude's first turn, Codex's metadata."""
    try:
        with open_transcript(path) as stream:
            lines = transcript_lines(stream)
            if agent == "codex":
                first = next(lines, "")
                if not first.endswith("\n"):
                    return None  # Still being written.
                meta = json.loads(first)
                payload = meta.get("payload") or {}
                if meta.get("type") == "session_meta" and payload.get("id"):
                    return payload.get("cwd") or None
                return None
            for item in records(lines):
                if (
                    item.get("type") in {"user", "assistant"}
                    and not item.get("isSidechain")
                    and item.get("cwd")
                ):
                    return item["cwd"]
    except (OSError, ValueError, AttributeError, RecursionError):
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
    """Where this machine's transcripts live; ``tail`` reads nothing outside them.

    A store that is itself a symlink, or that lies outside ``$HOME``, is no store.
    """
    candidates = [home / "projects" for home in host_claude_homes()] + [host_codex_sessions()]
    return [root for root in map(host_session_root, candidates) if root is not None]


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
    root = Path(_host_checkout(root))
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


def _host_transcript(file):
    """One of this machine's session transcripts, opened; refused for any other file."""
    _arg_paths([file])
    path = Path(os.path.realpath(file))
    if path.suffix != ".jsonl" or not any(path.is_relative_to(r) for r in host_session_roots()):
        raise ValueError("Not a session transcript of this machine")
    try:
        stream = open_beneath(path)
    except OSError as exc:
        raise ValueError("Not a session transcript of this machine") from exc
    try:
        info = os.fstat(stream.fileno())
        # What was opened must still be the file at the checked path: nothing on the way
        # there was swapped for a symlink in between.
        try:
            now = os.lstat(path)
        except OSError:
            now = None
        if (
            now is None
            or (now.st_dev, now.st_ino) != (info.st_dev, info.st_ino)
            or os.path.realpath(path) != str(path)
        ):
            raise ValueError("Not a session transcript of this machine")
    except BaseException:
        stream.close()
        raise
    return stream


def _host_turn(file, session, agent):
    """The current turn of one of this machine's transcripts, and its final response."""
    if not isinstance(session, str) or not SESSION.match(session):
        raise ValueError("Expected a session ID")
    if agent not in {"claude", "codex"}:
        raise ValueError("Expected claude or codex")
    with _host_transcript(file) as stream:
        lines = _ending_lines(stream)
        turn, complete = (claude_turn if agent == "claude" else codex_turn)(lines, session)
    return {
        "turn": str(turn)[:MAX_TEXT],
        "complete": None if complete is None else str(complete)[:MAX_TURN],
    }


def _host_first_turn(file, agent, marker):
    """The first turn of one of this machine's transcripts, and whether it took ``marker``."""
    if agent not in {"claude", "codex"}:
        raise ValueError("Expected claude or codex")
    if not isinstance(marker, str) or not MARKER.fullmatch(marker):
        raise ValueError("Expected a completion marker")
    with _host_transcript(file) as stream:
        turn, marked = first_turn(transcript_lines(stream), agent, marker)
    return {"turn": None if turn is None else str(turn)[:MAX_TEXT], "marked": marked}


def _host_turn_state(file, session, agent):
    """``turn`` and ``fingerprint`` of one transcript, read together in one call."""
    with _host_transcript(file) as stream:
        info = os.fstat(stream.fileno())
    found = _host_turn(file, session, agent)
    return {**found, "inode": info.st_ino, "size": info.st_size, "mtime_ns": info.st_mtime_ns}


def _host_alive(pid, digest):
    """Whether process ``pid`` still runs the agent's command line: an agent stopped, or
    moved out of its terminal's foreground, has not exited. Another program at that pid
    is not it.

    The command line is named by the SHA-256 of its arguments joined by NULs: a brief
    may be one long argument. They are read as the kernel keeps them, which ps would
    render lossily.
    """
    if not (type(pid) is int and 1 < pid < 2**31):
        raise ValueError("Expected a process ID")
    if not (isinstance(digest, str) and DIGEST.fullmatch(digest)):
        raise ValueError("Expected a command line digest")
    running = _proc_arguments(pid) if os.path.isdir("/proc/self") else _mac_arguments(pid)
    return {"alive": running is not None and hashlib.sha256(running).hexdigest() == digest}


def _proc_arguments(pid):
    """A process's arguments, NUL-joined, from /proc; None once it is gone."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as stream:
            running = stream.read(MAX_CMDLINE)
    except (FileNotFoundError, ProcessLookupError):
        return None
    # Each argument ends in a NUL; a zombie's command line is empty.
    return running[:-1] if running else None


def _mac_arguments(pid):
    """A process's arguments, NUL-joined, as macOS keeps them (KERN_PROCARGS2: argc, the
    executable's path and its padding, then the arguments); None once it is gone."""
    import ctypes

    libc = ctypes.CDLL(None)
    # The buffer is exactly kern.argmax long (CTL_KERN, KERN_ARGMAX); any other is refused.
    argmax = ctypes.c_int(0)
    size = ctypes.c_size_t(ctypes.sizeof(argmax))
    limit = (ctypes.c_int * 2)(1, 8)
    if libc.sysctl(limit, 2, ctypes.byref(argmax), ctypes.byref(size), None, ctypes.c_size_t(0)):
        raise OSError("Cannot read kern.argmax")
    mib = (ctypes.c_int * 3)(1, 49, pid)  # CTL_KERN, KERN_PROCARGS2
    size = ctypes.c_size_t(argmax.value)
    found = ctypes.create_string_buffer(argmax.value)
    if libc.sysctl(mib, 3, found, ctypes.byref(size), None, ctypes.c_size_t(0)) != 0:
        return None
    data = found.raw[: size.value]
    argc = int.from_bytes(data[:4], sys.byteorder)
    rest = data[4:]
    rest = rest[rest.find(b"\0") :].lstrip(b"\0")
    return b"\0".join(rest.split(b"\0")[:argc])


def _host_fingerprint(file):
    """Which file one of this machine's transcripts is, and how far it was written."""
    with _host_transcript(file) as stream:
        info = os.fstat(stream.fileno())
    return {"inode": info.st_ino, "size": info.st_size, "mtime_ns": info.st_mtime_ns}


def _ending_lines(stream):
    """A transcript's first line and its last MAX_SCAN bytes, from a line start.

    The latest turn is at the end; a Codex rollout names its session in its first line.
    """
    size = os.fstat(stream.fileno()).st_size
    if size <= MAX_SCAN:
        yield from transcript_lines(stream)
        return
    first = stream.readline(MAX_SCAN_LINE + 1)
    if first.endswith(b"\n"):
        yield first.decode("utf-8", errors="replace")
    stream.seek(size - MAX_SCAN)
    stream.readline()  # The rest of the line the seek landed in.
    yield from transcript_lines(stream)


def _host_fetch_refusals(clone, timeout):
    """Refuse a fetch the clone's own config would redirect, or expose credentials on."""
    result = _host_run([*HOST_GIT, "-C", clone, "config", "--list", "-z", "--show-scope"], timeout)
    if result.returncode:
        raise ValueError((result.stderr or "Git config unreadable")[-400:].strip())
    fields = result.stdout.split("\0")
    for index in range(0, len(fields) - 1, 2):  # zip(strict=) needs Python 3.10.
        scope, key = fields[index], fields[index + 1].partition("\n")[0]
        if scope in TRUSTED_SCOPES:
            continue
        if REDIRECT.fullmatch(key):
            raise ValueError(f"The repository's own {key} redirects a fetch; Git was not run")
        if TLS.fullmatch(key):
            raise ValueError(
                f"The repository's own {key} would expose GitHub credentials; Git was not run"
            )


def _host_fetch(clone, repo, base, remote, timeout=120):
    """``base`` of github.com/``repo`` into ``refs/remotes/<remote>/<base>`` of ``clone``."""
    _host_fetch_refusals(clone, timeout)
    options = [arg for setting in FETCH_CONFIG for arg in ("-c", setting)]
    result = _host_run(
        [
            *HOST_GIT,
            *options,
            *host_guard(clone),
            "-C",
            clone,
            "fetch",
            "--upload-pack=git-upload-pack",
            "--no-write-fetch-head",
            f"https://github.com/{repo}.git",
            f"+refs/heads/{base}:refs/remotes/{remote}/{base}",
        ],
        timeout,
    )
    if result.returncode:
        raise ValueError((result.stderr or "git fetch failed")[-4000:].strip())


def _host_common_dir(checkout):
    """A linked worktree's Git common dir, read from its layout as safe_git.common_dir does.

    Never what an agent-written ``commondir`` file names: ``.git`` must point at
    ``<common>/worktrees/<id>``, whose ``gitdir`` registers this very checkout.
    """
    dot = os.path.join(checkout, ".git")

    def refuse(reason):
        return ValueError(f"The Git common dir of {checkout} cannot be verified: {reason}")

    def read(path):
        with open(path) as handle:
            return handle.read(4096).strip()

    if os.path.islink(dot) or not os.path.isfile(dot):
        raise refuse(".git is not a worktree's gitdir file")
    pointer = read(dot)
    if not pointer.startswith("gitdir: "):
        raise refuse(".git is not a gitdir file")
    admin = os.path.realpath(os.path.join(checkout, pointer[len("gitdir: ") :]))
    worktrees = os.path.dirname(admin)
    common = os.path.dirname(worktrees)
    if os.path.basename(worktrees) != "worktrees" or os.path.basename(common) != ".git":
        raise refuse(f"{admin} is not a worktree of a clone's .git")
    try:
        registered = os.path.realpath(os.path.join(admin, read(os.path.join(admin, "gitdir"))))
        named = os.path.realpath(os.path.join(admin, read(os.path.join(admin, "commondir"))))
    except OSError as exc:
        raise refuse(f"{admin} is incomplete ({exc.strerror})") from exc
    if registered != os.path.realpath(dot):
        raise refuse(f"{admin} does not register this checkout")
    if named != common or os.path.lexists(os.path.join(common, "commondir")):
        raise refuse(f"{admin}/commondir names another common dir")
    if not os.path.isdir(os.path.join(common, "objects")):
        raise refuse(f"{common} holds no objects")
    return common


def _host_trust(config, directory):
    """Accept Claude's folder trust dialog for ``directory``, as claude_accounts.trust does.

    ``--dangerously-skip-permissions`` does not skip that dialog. A missing or unreadable
    config is left alone; ``BABYSIT_CLAUDE_TRUST=0`` turns it off.
    """
    if os.environ.get("BABYSIT_CLAUDE_TRUST") == "0":
        return
    path = Path(os.path.realpath(config))
    temporary = None
    try:
        mode = path.stat().st_mode & 0o7777
        value = json.loads(path.read_text())
        projects = value.setdefault("projects", {})
        if not isinstance(projects, dict):
            return
        folder = Path(directory)
        if any(
            isinstance(projects.get(str(f)), dict)
            and projects[str(f)].get("hasTrustDialogAccepted") is True
            for f in (folder, *folder.parents)
        ):
            return
        entry = projects.setdefault(str(folder), {})
        if not isinstance(entry, dict):
            return
        entry["hasTrustDialogAccepted"] = True
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(json.dumps(value, indent=2, ensure_ascii=False))
        os.replace(temporary, path)
    except (OSError, ValueError, AttributeError, TypeError):
        if temporary:
            Path(temporary).unlink(missing_ok=True)


def _branch(value, what):
    if (
        not isinstance(value, str)
        or not BRANCH.fullmatch(value)
        or ".." in value
        or "//" in value
        or value.endswith(("/", ".", ".lock"))
    ):
        raise ValueError(f"Expected a {what} branch name")
    return value


def _repo(value):
    if (
        not isinstance(value, str)
        or not SLUG.fullmatch(value)
        or value.split("/")[1] in {".", ".."}
    ):
        raise ValueError("Expected an owner/repo")
    return value


def _host_claude(agent, account, sandbox):
    """The agent's configuration here: Claude's home (real, for the sandbox), the
    ``CLAUDE_CONFIG_DIR`` to type (literal, as Claude names its Keychain item after it;
    None for the default login under Safehouse) and the config its trust goes in.

    babysit-bwrap (``sandbox``) always sets ``CLAUDE_CONFIG_DIR``, so there the default
    login reads ``~/.claude/.claude.json``. Codex has none of these.
    """
    if agent not in {"claude", "codex"}:
        raise ValueError("Expected claude or codex")
    if account is not None and (not isinstance(account, str) or not ACCOUNT.fullmatch(account)):
        raise ValueError("Expected a Claude account name")
    if sandbox not in {"safehouse", "bwrap"}:
        raise ValueError("Expected safehouse or bwrap")
    if agent != "claude":
        return None, None, None
    literal_home = os.path.expanduser("~")
    named = account not in (None, "default")
    default = os.path.join(literal_home, ".claude")
    literal = os.path.join(default, "accounts", str(account)) if named else default
    if named and not os.path.isdir(literal):
        raise ValueError(f"Unknown Claude account {account} on this machine")
    claude_home = os.path.realpath(literal)
    claude_env = literal if named or sandbox == "bwrap" else None
    config = (
        os.path.join(claude_home, ".claude.json")
        if claude_env
        else os.path.join(os.path.realpath(literal_home), ".claude.json")
    )
    return claude_home, claude_env, config


def _host_clone(clone, repo):
    """``clone``'s real path, its remotes that name ``repo`` and its own ``.git``.

    Nothing clones a repository on a worker: a missing clone is refused.
    """
    clone = os.path.realpath(os.path.expanduser(clone))
    if not os.path.isdir(clone):
        raise ValueError(f"No clone of {repo} at {clone}; clone it on this machine first")
    remotes = remote_slugs(repo_config(host_git, clone))
    tracking = [
        name
        for name in ("origin", "upstream", *sorted(remotes))
        if remotes.get(name) == repo.lower()
    ]
    if not tracking:
        raise ValueError(f"{clone} is not a clone of {repo}: none of its remotes names it")
    common = os.path.join(clone, ".git")
    if os.path.islink(common) or not os.path.isdir(common):
        raise ValueError(f"{clone} has no .git directory of its own")
    return clone, tracking, os.path.realpath(common)


def _host_version(babysit_root):
    """The babysit-pr commit deployed here; launches need the controller's own."""
    _arg_paths([babysit_root], home=True)
    root = os.path.realpath(os.path.expanduser(babysit_root))
    if not os.path.isdir(root):
        raise ValueError(f"No babysit-pr at {root}; deploy it (provision_worker.sh --user-step)")
    return {"head": host_git(root, "rev-parse", "HEAD")}


def _host_worktree(path, root):
    """A watched checkout here: its top, branch (None when detached), head, whether it has
    changes, its Git common dir when it is a linked worktree (None otherwise), and the
    real path of ``root``, where this machine's worktrees live."""
    _arg_paths([root], home=True)
    real = _host_checkout(path)
    try:
        branch = host_git(real, "symbolic-ref", "--short", "HEAD")
    except ValueError:
        branch = None
    try:
        common = _host_common_dir(real)
    except ValueError:
        common = None
    return {
        "path": real,
        "branch": branch,
        "head": host_git(real, "rev-parse", "HEAD"),
        "dirty": bool(host_git(real, "status", "--porcelain")),
        "common": common,
        "root": os.path.realpath(os.path.expanduser(root)),
    }


def _session_items(stream):
    for line in transcript_lines(stream):
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            yield item


def _outside(path, store):
    return ValueError(f"{path} is not in the launch's configuration directory ({store})")


def _codex_session_info(session, cwd, rollout, expected_home=None):
    """``pr_supervisor.session_info`` for a rollout of this machine; with
    ``expected_home``, one in that Codex home's ``sessions``."""
    if rollout is None:
        store = host_session_root(host_codex_sessions())
        found = list(store.rglob(f"*{session}*.jsonl")) if store is not None else []
        if len(found) != 1:
            raise ValueError("Specify --rollout: could not identify exactly one saved session")
        rollout = str(found[0])
    rollout = os.path.realpath(rollout)
    if expected_home is not None:
        store = Path(os.path.realpath(expected_home)) / "sessions"
        if not Path(rollout).is_relative_to(store):
            raise _outside(rollout, store)
    meta, context = None, {}
    # Opened beneath its resolved path: a link swapped in since is refused, not followed.
    with _host_transcript(rollout) as stream:
        for item in _session_items(stream):
            if item.get("type") == "session_meta":
                meta = item.get("payload")
            elif item.get("type") == "turn_context" and isinstance(item.get("payload"), dict):
                context = item["payload"]
    if not isinstance(meta, dict) or meta.get("id") != session:
        raise ValueError("Rollout session ID does not match")
    if os.path.realpath(str(meta.get("cwd") or "")) != os.path.realpath(cwd):
        raise ValueError("Rollout cwd does not match the worktree")
    policy = (context.get("sandbox_policy") or {}).get("type")
    if policy not in {"read-only", "workspace-write", "danger-full-access"}:
        raise ValueError("Cannot preserve this session's sandbox policy automatically")
    model = context.get("model")
    return {
        "session_id": session,
        "rollout": rollout,
        "sandbox": policy,
        "model": model if isinstance(model, str) else None,
    }


def _claude_session_info(session, cwd, transcript, account, expected_home=None):
    """``claude_runner.session_info`` for a transcript in one of this machine's Claude homes.

    It reads at most the transcript's first MAX_SCAN bytes, as every probe does.
    """
    default = Path(os.path.expanduser("~/.claude")).resolve()
    selected = None
    if account == "default":
        selected = default
    elif account is not None:
        if not os.path.isdir(os.path.join(default, "accounts", account)):
            raise ValueError(f"Unknown Claude account {account} on this machine")
        selected = (default / "accounts" / account).resolve()
    if transcript is None:
        found = [
            path
            for home in ([selected] if selected else host_claude_homes())
            if host_session_root(home / "projects") is not None
            for path in (home / "projects").glob(f"*/{session}.jsonl")
        ]
        if len(found) != 1:
            raise ValueError("Supply --rollout: could not identify one saved Claude transcript")
        transcript = str(found[0])
    real = Path(os.path.realpath(transcript))
    # Top-level transcripts live directly in HOME/projects/PROJECT/.
    if real.parent.parent.name != "projects":
        raise ValueError("Use the original Claude session, not a subagent transcript")
    home = real.parents[2]
    if selected and home != selected:
        raise ValueError("Claude transcript belongs to a different account")
    # The launch that registered it: its own store, checked on the path opened below.
    if expected_home is not None and home != Path(os.path.realpath(expected_home)):
        raise _outside(real, os.path.realpath(expected_home))
    root = os.path.realpath(cwd)
    seen, model, permission_mode = False, None, None
    with _host_transcript(str(real)) as stream:
        for item in _session_items(stream):
            if item.get("type") not in {"user", "assistant"}:
                continue
            if item.get("isSidechain"):
                raise ValueError("Use the original Claude session, not a subagent transcript")
            if item.get("sessionId") != session:
                raise ValueError("Claude transcript session ID does not match")
            # A session may cd into a subdirectory of its worktree without changing identity.
            where = os.path.realpath(str(item.get("cwd") or "/nonexistent"))
            if not item.get("cwd") or not (where == root or where.startswith(root + os.sep)):
                raise ValueError("Claude transcript cwd does not match the worktree")
            seen = True
            message = item.get("message")
            candidate = message.get("model") if isinstance(message, dict) else None
            if isinstance(candidate, str) and candidate and candidate != "<synthetic>":
                model = candidate
            if item.get("permissionMode"):
                permission_mode = item["permissionMode"]
    if not seen or not model:
        raise ValueError("Cannot validate the Claude conversation and its model")
    name = host_accounts().get(home)
    named = name not in (None, "default")
    # The literal path a launch types (see _host_claude); on Linux Claude keeps its
    # credentials in the directory itself.
    literal = os.path.join(os.path.expanduser("~"), ".claude", "accounts", str(name))
    return {
        "session_id": session,
        "rollout": str(real),
        "model": model,
        "claude_permission_mode": "plan" if permission_mode == "plan" else "dontAsk",
        "claude_config_dir": str(home),
        "claude_config_env": None if home == default else literal if named else str(home),
        "claude_account": name,
        "claude_subscription": account is not None or named,
    }


UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _host_session_info(agent, session, cwd, rollout, account, expected_home=None):
    """A watch's session here, checked as ``register`` checks one of the controller's.

    ``expected_home`` is the configuration directory of the sandbox that registered it,
    which its transcript must lie in."""
    if not isinstance(session, str) or not UUID.fullmatch(session):
        raise ValueError("Expected a session UUID")
    _arg_paths([cwd] if rollout is None else [cwd, rollout])
    if expected_home is not None:
        _arg_paths([expected_home])
    if account is not None and (not isinstance(account, str) or not ACCOUNT.fullmatch(account)):
        raise ValueError("Expected a Claude account name")
    if agent == "codex":
        if account is not None:
            raise ValueError("A Claude account can only be selected for Claude")
        return _codex_session_info(session, cwd, rollout, expected_home)
    if agent == "claude":
        return _claude_session_info(session, cwd, rollout, account, expected_home)
    raise ValueError("Expected claude or codex")


def _host_wt_prepare(clone, repo, root, prefix, agent, account, sandbox):
    """Where a dashboard launch's ``wt`` makes its checkout here, before it runs.

    ``clone`` must be a clone of ``repo``, as for a cron job. The checkout is named
    ``prefix``, or ``prefix-<n>`` while a directory or branch of that name exists, and
    Claude is trusted for it in the config its sandbox reads.
    """
    _arg_paths([clone, root], home=True)
    _repo(repo)
    _branch(prefix, "checkout")
    if "/" in prefix:
        raise ValueError("Expected a checkout name without '/'")
    _, _, config = _host_claude(agent, account, sandbox)
    clone, _, common = _host_clone(clone, repo)
    root = os.path.realpath(os.path.expanduser(root))
    if root == common or root.startswith(common + os.sep):
        raise ValueError(f"{root} lies inside {clone}'s .git")
    name, n = prefix, 1
    while os.path.lexists(os.path.join(root, name)) or _host_branch_exists(clone, name):
        n += 1
        name = f"{prefix}-{n}"
    path = os.path.join(root, name)
    if config is not None:
        _host_trust(config, path)
    return {"clone": clone, "root": root, "name": name, "path": path}


def _host_branch_exists(clone, branch):
    try:
        host_git(clone, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}")
    except ValueError:
        return False
    return True


def _host_wt_checkout(clone, path):
    """What ``wt`` made at ``path``: a worktree of ``clone``, its branch and upstream."""
    _arg_paths([clone, path])
    path = os.path.realpath(path)
    common = _host_common_dir(path)
    if common != os.path.realpath(os.path.join(clone, ".git")):
        raise ValueError(f"{path} is not a worktree of {clone}")
    branch = host_git(path, "symbolic-ref", "--quiet", "--short", "HEAD")
    config = repo_config(host_git, path)
    found = provenance(path, common, branch, None, config, remote_slugs(config))
    return {"path": path, "branch": branch, "upstream": found["upstream"]}


def _host_cron_checkout(
    clone, repo, branch, base, directory, agent, account, babysit_root, sandbox
):
    """A cron job's checkout on this machine, made on first use as cron_agents does locally.

    ``clone`` must be a clone of ``repo``: one of its remotes names that GitHub
    repository. Every Git call is hardened (``host_git``), its one fetch names the job's
    GitHub repository (``_host_fetch``), and the checkout must be a linked worktree of
    that very clone, outside its ``.git``: an agent can rewrite the checkout's ``.git``
    file to name any repository it made, but not that.

    Returns the paths a launch line needs: the checkout, the clone, its common dir,
    this machine's home, the agent's configuration directory (``claude_home``, real, for
    the sandbox) and the ``CLAUDE_CONFIG_DIR`` to type (``claude_env``: literal, as
    Claude names its Keychain item after it; None for the default login under
    Safehouse), and babysit-pr's ``babysit-bwrap`` here. A Claude checkout is trusted in
    the configuration the sandboxed Claude reads: babysit-bwrap (``sandbox``) always
    sets ``CLAUDE_CONFIG_DIR``, so the default login reads ``~/.claude/.claude.json``.
    """
    _arg_paths([clone, directory, babysit_root], home=True)
    _repo(repo)
    _branch(branch, "job")
    if base:
        _branch(base, "base")
    home = os.path.realpath(os.path.expanduser("~"))
    claude_home, claude_env, config = _host_claude(agent, account, sandbox)
    clone, tracking, common_expected = _host_clone(clone, repo)
    directory = os.path.expanduser(directory)
    if os.path.isdir(directory):
        path = os.path.realpath(directory)
    else:
        listing = host_git(clone, "worktree", "list", "--porcelain")
        existing = _worktree_branches(listing).get(branch)
        if existing and os.path.isdir(existing):
            # An unattended agent never works in the user's own clone.
            if os.path.realpath(existing) == clone:
                raise ValueError(f"{branch} is checked out in the clone itself")
            path = os.path.realpath(existing)
        else:
            if os.path.realpath(directory).startswith(common_expected + os.sep):
                raise ValueError(f"{directory} lies inside {clone}'s .git")
            os.makedirs(os.path.dirname(directory), exist_ok=True)
            try:
                host_git(clone, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
            except ValueError:
                start = base or "HEAD"
                # The job's own repository, never a URL the clone's config names; it lands
                # in the refs of the remote that tracks it, named in full so no local
                # branch or tag called <remote>/<base> wins.
                if base:
                    _host_fetch(clone, repo, base, tracking[0])
                    start = f"refs/remotes/{tracking[0]}/{base}"
                add = ["-b", branch, directory, start]
            else:
                add = [directory, branch]
            # The new worktree's branch and git dir can bring config the clone's listing
            # never sees, so its files are checked out by a reset run inside it.
            host_git(clone, "worktree", "add", "--no-checkout", *add, timeout=120)
            try:
                host_git(directory, "reset", "--hard", "--quiet", timeout=120)
            except Exception:
                # Left behind, the empty checkout would be reused with every file deleted.
                host_git(clone, "worktree", "remove", "--force", directory, timeout=120)
                if add[0] == "-b":
                    host_git(clone, "branch", "-D", branch)
                raise
            path = os.path.realpath(directory)
    # Whatever route found it, the checkout is a worktree of this clone, outside its .git.
    common = _host_common_dir(path)
    if common != common_expected:
        raise ValueError(f"{path} is not a worktree of {clone}")
    if path == common or path.startswith(common + os.sep):
        raise ValueError(f"{path} lies inside {clone}'s .git")
    current = host_git(path, "rev-parse", "--abbrev-ref", "HEAD")
    if current != branch:
        stated = f"{path} has {current} checked out instead of {branch}"
        if host_git(path, "status", "--porcelain", "--untracked-files=no"):
            raise ValueError(f"{stated}, with uncommitted changes")
        try:
            host_git(path, "switch", "--quiet", branch)
        except ValueError as exc:
            raise ValueError(f"{stated}, and switching back failed: {exc}") from exc
    if config is not None:
        _host_trust(config, path)
    root = os.path.realpath(os.path.expanduser(babysit_root))
    launcher = os.path.join(root, "scripts", "babysit-bwrap")
    return {
        "path": path,
        "clone": clone,
        "common": common,
        "home": home,
        "claude_home": claude_home,
        "claude_env": claude_env,
        "codex_home": os.path.realpath(
            os.environ.get("CODEX_HOME") or os.path.join(home, ".codex")
        ),
        "launcher": launcher if os.access(launcher, os.X_OK) else None,
    }


def _worktree_branches(listing):
    """``branch -> checkout path`` from ``git worktree list --porcelain``, as wt reads it."""
    found: dict[str, str] = {}
    path = ""
    for line in listing.splitlines():
        key, _, value = line.partition(" ")
        if key == "worktree":
            path = value
        elif key == "branch" and path:
            found.setdefault(value.removeprefix("refs/heads/"), path)
        elif not key:
            path = ""
    return found


def _host_tail(file, inode, offset, limit):
    """Complete lines of one listed transcript from ``offset``, at most ``limit`` bytes.

    A file replaced since (another ``inode``) or truncated below ``offset`` is read from
    the start, which the answer's offset says. A full read inside one line longer than
    ``limit`` is returned as it is, for the controller to join with the next.
    """
    if not (inode is None or (type(inode) is int and inode >= 0)):
        raise ValueError("Expected an inode number")
    if not (type(offset) is int and offset >= 0 and type(limit) is int and 0 < limit <= MAX_TAIL):
        raise ValueError("Expected a byte offset and limit")
    stream = _host_transcript(file)
    with stream:
        info = os.fstat(stream.fileno())
        start = offset if info.st_ino == inode and info.st_size >= offset else 0
        stream.seek(start)
        # No further than the size reported, though the file may grow meanwhile.
        data = stream.read(min(limit, info.st_size - start))
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
    "turn": _host_turn,
    "first_turn": _host_first_turn,
    "fingerprint": _host_fingerprint,
    "turn_state": _host_turn_state,
    "alive": _host_alive,
    "cron_checkout": _host_cron_checkout,
    "version": _host_version,
    "wt_prepare": _host_wt_prepare,
    "wt_checkout": _host_wt_checkout,
    "worktree": _host_worktree,
    "session_info": _host_session_info,
}


def _stop_group():
    # A probe leading its own process group (see __main__) stops its Git children too;
    # it ignores the signal it sends them, and starts no more.
    global STOPPED
    if os.getpgrp() == os.getpid():
        STOPPED = True
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        os.killpg(0, signal.SIGTERM)


def _expire(signum, frame):
    global EXPIRED_AT_ALARM
    EXPIRED_AT_ALARM = True
    _stop_group()
    raise Expired("Probe deadline expired")


def main(argv):
    """Answer one request; a ``deadline`` in seconds bounds the whole run on this worker.

    A killed ssh client leaves the probe running, so it carries its own lifeline: an
    alarm at the deadline, and no Git started after it. Expiry exits with EXPIRED.
    """
    code = 0
    try:
        answer = _answer(argv)
    except Expired as exc:
        # The one-shot alarm can land while _answer is already handling an error or
        # cleaning up, and then escapes it; expiry is still reported as expiry.
        answer = {"protocol": PROTOCOL, "error": str(exc)}
    if EXPIRED_AT_ALARM or answer.get("error") == "Probe deadline expired":
        code = EXPIRED
        answer = {"protocol": PROTOCOL, "error": "Probe deadline expired"}
    # UTF-8 rather than \u escapes keeps a diff's answer close to its own size. A lone
    # surrogate (an undecodable file name) comes out as the escape JSON would have used.
    sys.stdout.flush()
    sys.stdout.buffer.write(
        json.dumps(answer, ensure_ascii=False).encode("utf-8", "backslashreplace")
    )
    sys.stdout.flush()
    return code


def _answer(argv):
    global DEADLINE, EXPIRED_AT_ALARM, STOPPED
    EXPIRED_AT_ALARM = STOPPED = False
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
        answer = {"protocol": PROTOCOL, "error": str(exc)}
        _stop_group()  # Expiry seen first by a Git timeout, before the alarm.
    except Exception as exc:
        answer = {"protocol": PROTOCOL, "error": f"{type(exc).__name__}: {exc}"[:400]}
    finally:
        if previous is not None:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)
        DEADLINE = None
    return answer


# The controller side ---------------------------------------------------------


def _text(value):
    return isinstance(value, str) and len(value) <= MAX_TEXT


def _absolute(value):
    return _text(value) and value.startswith("/") and "\0" not in value


def _expect(ok, what):
    if not ok:
        raise ValueError(f"Malformed probe answer: {what}")


# Control and bidirectional formatting characters, which could disguise worker text.
UNSHOWN = re.compile("[\x00-\x1f\x7f-\x9f\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]")


def display_text(value):
    """A worker's title or label for showing, without characters that reorder or hide text."""
    return UNSHOWN.sub("", value) if isinstance(value, str) else value


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
    _expect(value["offset"] + len(data) <= value["size"], "data beyond the file's size")
    return {**value, "data": data}


def validate_turn(value):
    """Check a worker's answer for the current turn of a transcript."""
    _expect(isinstance(value, dict) and set(value) == {"turn", "complete"}, "fields")
    _expect(_text(value["turn"]) and value["turn"], "turn")
    complete = value["complete"]
    _expect(complete is None or (isinstance(complete, str) and len(complete) <= MAX_TURN), "text")
    return value


def validate_first_turn(value):
    """Check a worker's answer for the first turn of a transcript."""
    _expect(isinstance(value, dict) and set(value) == {"turn", "marked"}, "fields")
    _expect(value["turn"] is None or (_text(value["turn"]) and value["turn"]), "turn")
    _expect(isinstance(value["marked"], bool), "marked")
    return value


def validate_fingerprint(value):
    """Check a worker's answer for which file a transcript is and how far it was written."""
    fields = ("inode", "size", "mtime_ns")
    _expect(isinstance(value, dict) and set(value) == set(fields), "fields")
    _expect(all(_int(value[k]) for k in fields), "file facts")
    return value


def validate_turn_state(value):
    """Check a worker's answer for a transcript's current turn and fingerprint together."""
    _expect(isinstance(value, dict), "fields")
    turn = {k: value[k] for k in ("turn", "complete") if k in value}
    fingerprint = {k: value[k] for k in ("inode", "size", "mtime_ns") if k in value}
    _expect(len(value) == len(turn) + len(fingerprint), "fields")
    validate_turn(turn)
    validate_fingerprint(fingerprint)
    return value


def validate_alive(value):
    """Check a worker's answer for whether a process still runs."""
    _expect(isinstance(value, dict) and set(value) == {"alive"}, "fields")
    _expect(isinstance(value["alive"], bool), "alive")
    return value


def validate_cron_checkout(value):
    """Check a worker's cron checkout: absolute paths for the launch line, nothing else."""
    fields = {
        "path",
        "clone",
        "common",
        "home",
        "claude_home",
        "claude_env",
        "codex_home",
        "launcher",
    }
    _expect(isinstance(value, dict) and set(value) == fields, "fields")
    # These end up in a line typed into the worker's shell: a control character there
    # is a keystroke.
    for key in ("path", "clone", "common", "home", "codex_home"):
        _expect(_absolute(value[key]) and not CONTROL.search(value[key]), key)
    for key in ("claude_home", "claude_env", "launcher"):
        _expect(
            value[key] is None or (_absolute(value[key]) and not CONTROL.search(value[key])), key
        )
    return value


def validate_version(value):
    _expect(isinstance(value, dict) and set(value) == {"head"}, "fields")
    _expect(
        isinstance(value["head"], str)
        and re.fullmatch("[0-9a-f]{40}([0-9a-f]{24})?", value["head"]),
        "head",
    )
    return value


def validate_wt_prepare(value):
    """The paths and name a worker's ``wt`` gets on its command line, nothing else."""
    _expect(isinstance(value, dict) and set(value) == {"clone", "root", "name", "path"}, "fields")
    for key in ("clone", "root", "path"):
        _expect(_absolute(value[key]) and not CONTROL.search(value[key]), key)
    _expect(
        isinstance(value["name"], str)
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}", value["name"])
        and value["path"] == f"{value['root'].rstrip('/')}/{value['name']}",
        "name",
    )
    return value


def validate_wt_checkout(value):
    _expect(isinstance(value, dict) and set(value) == {"path", "branch", "upstream"}, "fields")
    _expect(_absolute(value["path"]), "path")
    _expect(_text(value["branch"]), "branch")
    upstream = value["upstream"]
    _expect(
        upstream is None
        or (
            isinstance(upstream, list)
            and len(upstream) == 2
            and (upstream[0] is None or _text(upstream[0]))
            and _text(upstream[1])
        ),
        "upstream",
    )
    return value


def _shown_path(value):
    # Recorded on a watch, and typed into a worker's shell by pane repairs.
    return _absolute(value) and not CONTROL.search(value)


def validate_worktree(value):
    """A watched checkout's state on a worker: what registration and repairs compare."""
    _expect(
        isinstance(value, dict)
        and set(value) == {"path", "branch", "head", "dirty", "common", "root"},
        "fields",
    )
    _expect(_shown_path(value["path"]), "path")
    _expect(value["branch"] is None or (_text(value["branch"]) and value["branch"]), "branch")
    _expect(
        isinstance(value["head"], str)
        and re.fullmatch("[0-9a-f]{40}([0-9a-f]{24})?", value["head"]),
        "head",
    )
    _expect(isinstance(value["dirty"], bool), "dirty")
    _expect(value["common"] is None or _shown_path(value["common"]), "common")
    _expect(_shown_path(value["root"]), "root")
    return value


CODEX_SESSION = {"session_id", "rollout", "sandbox", "model"}
CLAUDE_SESSION = {
    "session_id",
    "rollout",
    "model",
    "claude_permission_mode",
    "claude_config_dir",
    "claude_config_env",
    "claude_account",
    "claude_subscription",
}


def validate_session_info(value):
    """A worker's session as a watch records it; its model and paths reach repair argv."""
    _expect(isinstance(value, dict) and set(value) in (CODEX_SESSION, CLAUDE_SESSION), "fields")
    _expect(isinstance(value["session_id"], str) and UUID.fullmatch(value["session_id"]), "id")
    _expect(_shown_path(value["rollout"]) and value["rollout"].endswith(".jsonl"), "rollout")
    model = value["model"]
    _expect(
        model is None or (isinstance(model, str) and 0 < len(model) <= 200 and model.isprintable()),
        "model",
    )
    if set(value) == CODEX_SESSION:
        _expect(
            value["sandbox"] in {"read-only", "workspace-write", "danger-full-access"}, "sandbox"
        )
        return value
    _expect(model is not None, "model")
    _expect(value["claude_permission_mode"] in {"plan", "dontAsk"}, "permission mode")
    _expect(_shown_path(value["claude_config_dir"]), "config dir")
    _expect(
        value["claude_config_env"] is None or _shown_path(value["claude_config_env"]), "config env"
    )
    _expect(
        value["claude_account"] is None
        or (
            isinstance(value["claude_account"], str) and ACCOUNT.fullmatch(value["claude_account"])
        ),
        "account",
    )
    _expect(isinstance(value["claude_subscription"], bool), "subscription")
    return value


VALIDATORS = {
    "inventory": validate_inventory,
    "diff": validate_diff,
    "bases": validate_bases,
    "sessions": validate_sessions,
    "tail": validate_tail,
    "turn": validate_turn,
    "first_turn": validate_first_turn,
    "fingerprint": validate_fingerprint,
    "turn_state": validate_turn_state,
    "alive": validate_alive,
    "cron_checkout": validate_cron_checkout,
    "version": validate_version,
    "wt_prepare": validate_wt_prepare,
    "wt_checkout": validate_wt_checkout,
    "worktree": validate_worktree,
    "session_info": validate_session_info,
}


class Unreachable(ValueError):
    """The worker did not answer: ssh failed, or the probe ran out of time there."""


class Failed(ValueError):
    """The op failed on the worker; ``kind`` names the error it raised there."""

    def __init__(self, message, kind):
        super().__init__(message)
        self.kind = kind


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
            # -S: no site hooks (.pth files, sitecustomize) run before the probe.
            [python, "-I", "-S", "-", PROTOCOL, payload],
            timeout=timeout,
            input=Path(__file__).read_bytes(),
            max_output=MAX_STDOUT,
        )
    except owned_process.OutputLimitExceeded as exc:
        raise ValueError(f"{machine.label} probe answer exceeded {MAX_STDOUT} bytes") from exc
    stderr = result.stderr[-MAX_STDERR:].decode(errors="replace").strip()
    if result.returncode:
        reason = "unreachable" if result.returncode == 255 else f"exit {result.returncode}"
        # ssh's own failure, or a worker too slow to answer in time, may pass.
        error = Unreachable if result.returncode in {255, EXPIRED} else ValueError
        raise error(f"{machine.label} probe failed ({reason}): {stderr or 'no output'}")
    if len(result.stdout) > MAX_STDOUT:
        raise ValueError(f"{machine.label} probe answer exceeded {MAX_STDOUT} bytes")
    try:
        answer = json.loads(result.stdout, parse_constant=_constant)
    except ValueError as exc:
        raise ValueError(f"{machine.label} probe answered no JSON: {stderr or exc}") from exc
    if not isinstance(answer, dict) or answer.get("protocol") != PROTOCOL:
        raise ValueError(f"{machine.label} probe speaks another protocol")
    if "error" in answer:
        said = str(answer["error"])[:400]
        if said == "Probe deadline expired":
            raise Unreachable(f"{machine.label} probe failed: {said}")
        raise Failed(f"{machine.label} probe failed: {said}", said.partition(":")[0])
    return VALIDATORS[op](answer.get("result"))


if __name__ == "__main__":
    try:
        os.setpgid(0, 0)  # Its own group, so an expired deadline stops its Git children.
    except OSError:
        pass  # Already leading its session, and so its group.
    status = main(sys.argv)
    sys.stdout.flush()
    if status == EXPIRED:
        # Once more, after any Git a scan thread was starting as the alarm came.
        _spawning.acquire(timeout=5)
        _stop_group()
    # Answered: a thread still stuck in a read must not keep the process alive.
    os._exit(status)
