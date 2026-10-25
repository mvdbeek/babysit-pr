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
import json
import math
import os
import re
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

PROTOCOL = "babysit-probe-1"
MAX_STDOUT = 8 * 1024 * 1024
MAX_STDERR = 4096
MAX_CHANGES = 1000
MAX_UNPUSHED = 100
MAX_TEXT = 4096  # Longest path, branch or config value an answer may carry.
MAX_DEADLINE = 3600
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


# The worker side -------------------------------------------------------------


class Expired(Exception):
    """The probe's deadline passed; deliberately none of the errors a scan step absorbs."""


# Monotonic time after which no further Git starts; None when no deadline was given.
DEADLINE = None


def _host_run(argv, timeout):
    if DEADLINE is not None:
        left = DEADLINE - time.monotonic()
        if left <= 0:
            raise Expired("Probe deadline expired")
        timeout = min(timeout, left)
    try:
        return subprocess.run(
            argv,
            env={**os.environ, **HOST_ENV},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        # Cut short by the deadline itself, which the alarm may not have reported yet.
        if DEADLINE is not None and time.monotonic() >= DEADLINE:
            raise Expired("Probe deadline expired") from exc
        raise


# Overrides by repository path, read once per probe run.
_guards: dict[str, list[str]] = {}


def host_guard(path, timeout=60):
    """``-c`` overrides for the programs ``path``'s own config could name.

    Attributes (``.gitattributes``, ``.git/info/attributes``) pick a filter driver by
    name, so blanking every driver the config defines covers them all, git-lfs's
    included: git-lfs, and the extensions and transfer agents it would run, never
    starts. ``core.worktree`` would point Git at another directory and is refused.
    """
    key = str(path)
    if key not in _guards:
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
        _guards[key] = [
            arg
            for driver in drivers
            for setting in ("clean=", "smudge=", "process=", "required=false")
            for arg in ("-c", f"filter.{driver}.{setting}")
        ]
    return _guards[key]


def host_git(path, *args, timeout=60, strip=True):
    """Git in one checkout on the machine this probe runs on, hardened and non-interactive."""
    command = next((arg for arg in args if not arg.startswith("-")), None)
    # A submodule's status or diff would run Git in it, under its own filters.
    extra = ["--ignore-submodules=all"] if command in SUBMODULE_READS else []
    if extra:
        at = args.index(command) + 1
        args = (*args[:at], *extra, *args[at:])
    result = _host_run([*HOST_GIT, *host_guard(path), "-C", str(path), *args], timeout)
    if result.returncode:
        raise ValueError((result.stderr or result.stdout or "Git failed")[-4000:].strip())
    return result.stdout.strip() if strip else result.stdout


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


OPS = {"inventory": _host_inventory}


def _stop_group():
    # A probe leading its own process group (see __main__) stops its Git children too;
    # it ignores the signal it sends them.
    if os.getpgrp() == os.getpid():
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        os.killpg(0, signal.SIGTERM)


def _expire(signum, frame):
    _stop_group()
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
        _stop_group()  # Expiry seen first by a Git timeout, before the alarm.
    except Exception as exc:
        answer = {"protocol": PROTOCOL, "error": f"{type(exc).__name__}: {exc}"[:400]}
    finally:
        if previous is not None:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)
        DEADLINE = None
    sys.stdout.write(json.dumps(answer))
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


VALIDATORS = {"inventory": validate_inventory}


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
    status = main(sys.argv)
    sys.stdout.flush()
    # Answered: a thread still stuck in a read must not keep the process alive.
    os._exit(status)
