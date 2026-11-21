"""Keep a Linux worker's disk from filling up, and report what uses it.

A systemd user timer (provision_worker.sh) runs this once a day as the worker's user,
outside every sandbox:

- Docker: stopped containers older than DOCKER_CONTAINERS and images unused for
  DOCKER_IMAGES go, from the rootless daemon sandboxed agents use. An image labelled
  ``keep`` stays.
- Scratch directories (babysit-bwrap's ``$BABYSIT_SCRATCH``) of checkouts that no longer
  exist go from every cache. Workspaces cleanup removes them with the checkout already;
  these are the checkouts removed some other way.
- In each cache, anything that is not a known tool cache, the artefacts or the scratch
  directories goes once nothing in it changed for STALE_ENTRY: agents keep large runtime
  data in $XDG_CACHE_HOME, the one place that outlived their session before scratch did.
- A cache no launch used for STALE_CACHE, or whose agent configuration is gone, goes.
  A cache a running sandbox uses never does.
- Once a week each remaining cache's uv, npm and pnpm caches are pruned by their own
  tools, run in a sandbox that can write only that cache: what they read there was
  written by agents.

Then it writes remote_probe.DISK_REPORT, which the dashboard shows for the machine: the
size of each cache, scratch directory and checkout, Docker's use, and what this run
removed. ``--dry-run`` reports what would go and removes nothing.

    python3 scripts/housekeeping.py [--dry-run]
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import remote_probe

DAY = 86400
STALE_ENTRY = 7 * DAY
STALE_CACHE = 30 * DAY
TOOLS_EVERY = 7 * DAY
DOCKER_CONTAINERS = "72h"
DOCKER_IMAGES = "168h"
# What each cache holds that its own tool prunes, or that is small, or the user's.
KNOWN = {
    "artifacts",
    "scratch",
    "npm",
    "node",
    "node-gyp",
    "uv",
    "uv-python",
    "uv-tools",
    "uv-tools-bin",
    "pip",
    "pnpm",
    ".pnpm-store",
    "ms-playwright",
    "conda",
    "black",
    "fissix",
    "gh",
    "claude",
    "matrix-commander",
    "pre-commit",
}
STATE = "~/.local/state/babysit-pr/housekeeping.json"
MAX_WORKTREES = 50
MAX_REMOVED = 200
DU_TIMEOUT = 1800
TOOL_TIMEOUT = 1800


class Run:
    """One housekeeping run: what it removed, what failed, and whether it only reports."""

    def __init__(self, dry_run=False, now=None):
        self.dry_run = dry_run
        self.now = time.time() if now is None else now
        self.removed: list[dict] = []
        self.errors: list[str] = []

    def remove(self, parent, name, path, why):
        """Remove ``name`` in directory descriptor ``parent``, shown as ``path``."""
        size = du(path)
        if not self.dry_run:
            try:
                remote_probe.remove_at(parent, name)
            except OSError as exc:
                self.error(f"Could not remove {path}: {exc}")
                return
        self.removed.append({"path": path, "bytes": size, "why": why})
        verb = "Would remove" if self.dry_run else "Removed"
        print(f"{verb} {path} ({human(size)}): {why}")

    def error(self, text):
        self.errors.append(text[: remote_probe.MAX_TEXT])
        print(text, file=sys.stderr)


def human(size):
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024:
            return f"{size:.0f} {unit}"
        size /= 1024
    return f"{size:.1f} TiB"


def du(path):
    """Bytes ``path`` uses on its own filesystem, as ``du`` counts them; 0 when unknown."""
    try:
        found = subprocess.run(
            ["nice", "-n", "19", "du", "-skx", "--", path],
            capture_output=True,
            text=True,
            timeout=DU_TIMEOUT,
        )
        return int(found.stdout.split()[0]) * 1024
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return 0


def changed_since(path, cutoff):
    """Whether anything at or beneath ``path`` changed after ``cutoff``; links are not
    followed. Stops at the first such entry."""
    try:
        if os.lstat(path).st_mtime > cutoff:
            return True
    except OSError:
        return False
    for top, folders, files in os.walk(path):
        for name in folders + files:
            try:
                if os.lstat(os.path.join(top, name)).st_mtime > cutoff:
                    return True
            except OSError:
                continue
    return False


def live_caches(runs):
    """The caches of sandboxes running now, by their launch's lock and info; None when a
    running one does not say (it started before babysit-bwrap recorded its cache)."""
    keys = set()
    for info in glob.glob(os.path.join(glob.escape(runs), "*", "info")):
        lock = os.path.join(os.path.dirname(info), "lock")
        try:
            with open(lock) as held:
                try:
                    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    continue  # Free: its sandbox is gone.
                except BlockingIOError:
                    pass
        except OSError:
            continue
        fields = index_fields(info)
        if fields is None:
            continue
        if not remote_probe.CACHE_KEY.fullmatch(fields.get("cache", "")):
            return None
        keys.add(fields["cache"])
    return keys


def index_fields(path):
    try:
        with open(path) as stream:
            return dict(line.rstrip("\n").split(" ", 1) for line in stream if " " in line)
    except OSError:
        return None


def scratch_worktree(index, name):
    try:
        with open(os.path.join(index, "scratch", name)) as stream:
            return stream.readline().rstrip("\n") or None
    except OSError:
        return None


@contextlib.contextmanager
def launches_held(root):
    """babysit-bwrap's lock on new launches, held while caches are judged and moved aside:
    a launch takes it before it records or makes its cache."""
    runs = os.path.join(root, "runs")
    os.makedirs(runs, mode=0o700, exist_ok=True)
    with open(os.path.join(runs, ".lock"), "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def sweep_caches(run, root):
    """Remove unused caches, scratch directories of gone checkouts, and stale entries."""
    index = os.path.join(root, "index")
    trash = os.path.join(root, "trash")
    if not run.dry_run:
        for folder in (os.path.join(index, "keys"), os.path.join(index, "scratch"), trash):
            os.makedirs(folder, mode=0o700, exist_ok=True)
    fd = remote_probe.open_directory(root)
    try:
        with launches_held(root):
            live = live_caches(os.path.join(root, "runs"))
            doomed = []
            for key in remote_probe.sandbox_keys(fd):
                recorded = os.path.join(index, "keys", key)
                fields = index_fields(recorded)
                if fields is None:
                    # A cache from before launches were recorded: its time starts now.
                    if not run.dry_run:
                        with open(recorded, "w") as stream:
                            stream.write("configuration -\ngrants -\n")
                    continue
                if live is None or key in live:
                    continue
                configuration = fields.get("configuration", "-")
                if configuration != "-" and not os.path.isdir(configuration):
                    doomed.append((key, f"its agent configuration {configuration} is gone"))
                elif run.now - os.stat(recorded).st_mtime > STALE_CACHE:
                    days = STALE_CACHE // DAY
                    doomed.append((key, f"no launch used it for {days} days"))
            # Moved aside under the lock, so no launch starts in one meanwhile; removed after.
            moved = []
            for key, why in doomed:
                if run.dry_run:
                    run.remove(fd, key, os.path.join(root, key), why)  # Only reported.
                    continue
                aside = f"{key}.{os.getpid()}.{int(run.now)}"
                try:
                    os.rename(key, os.path.join(trash, aside), src_dir_fd=fd)
                    os.unlink(os.path.join(index, "keys", key))
                except OSError as exc:
                    run.error(f"Could not set {key} aside: {exc}")
                    continue
                moved.append((key, aside, why))
        if not run.dry_run:
            trash_fd = remote_probe.open_directory("trash", fd)
            try:
                for key, aside, why in moved:
                    run.remove(trash_fd, aside, os.path.join(root, key), why)
                # Anything set aside by a run that did not finish.
                for name in os.listdir(trash_fd):
                    run.remove(trash_fd, name, os.path.join(trash, name), "left set aside")
            finally:
                os.close(trash_fd)
        gone = {key for key, _ in doomed}
        for key in remote_probe.sandbox_keys(fd):
            if key not in gone:
                sweep_cache(run, root, fd, key)
        # Index entries of scratch directories that are gone everywhere.
        if not run.dry_run:
            for name in os.listdir(os.path.join(index, "scratch")):
                worktree = scratch_worktree(index, name)
                if worktree and not os.path.isdir(worktree):
                    with contextlib.suppress(OSError):
                        os.unlink(os.path.join(index, "scratch", name))
    finally:
        os.close(fd)


def sweep_cache(run, root, fd, key):
    """One cache's scratch directories of gone checkouts and its stale unknown entries."""
    index = os.path.join(root, "index")
    path = os.path.join(root, key)
    cutoff = run.now - STALE_ENTRY
    try:
        cache = remote_probe.open_directory(key, fd)
    except OSError:
        return
    try:
        with contextlib.suppress(OSError):
            scratch = remote_probe.open_directory("scratch", cache)
            try:
                for name in sorted(os.listdir(scratch)):
                    shown = os.path.join(path, "scratch", name)
                    worktree = scratch_worktree(index, name)
                    if worktree is None:
                        if not changed_since(shown, cutoff):
                            run.remove(scratch, name, shown, "no checkout recorded for it")
                    elif not os.path.isdir(worktree):
                        run.remove(scratch, name, shown, f"its checkout {worktree} is gone")
            finally:
                os.close(scratch)
        for name in sorted(os.listdir(cache)):
            shown = os.path.join(path, name)
            if name in KNOWN or changed_since(shown, cutoff):
                continue
            days = STALE_ENTRY // DAY
            run.remove(cache, name, shown, f"not a tool cache, and unchanged for {days} days")
    finally:
        os.close(cache)


def tool_path(home):
    """PATH with the toolchains a worker's agents use, newest Node first."""
    nodes = sorted(glob.glob(os.path.join(glob.escape(home), ".nvm/versions/node/*/bin")))
    extra = [os.path.join(home, ".local/bin"), os.path.join(home, ".npm-global/bin")]
    return os.pathsep.join([*reversed(nodes), *extra, os.environ.get("PATH", "/usr/bin:/bin")])


def confined(home, cache, argv, path):
    """``argv`` in bubblewrap, writing only ``cache``: no home but the read-only
    toolchains, no network, no runtime sockets, no other process."""
    wrapped = [
        "bwrap",
        "--die-with-parent",
        "--new-session",
        "--unshare-all",
        "--clearenv",
        "--ro-bind",
        "/",
        "/",
        "--dev",
        "/dev",
        "--proc",
        "/proc",
        "--tmpfs",
        "/tmp",
        "--tmpfs",
        "/run",
        "--tmpfs",
        home,
    ]
    for name in (".local/bin", ".local/share/uv", ".nvm", ".npm-global"):
        wrapped += ["--ro-bind-try", os.path.join(home, name), os.path.join(home, name)]
    wrapped += ["--bind", cache, cache]
    for name, value in (
        ("PATH", path),
        ("HOME", home),
        ("XDG_CACHE_HOME", cache),
        ("UV_CACHE_DIR", os.path.join(cache, "uv")),
        ("npm_config_cache", os.path.join(cache, "npm")),
    ):
        wrapped += ["--setenv", name, value]
    return [*wrapped, "--", *argv]


def prune_tools(run, root, home, state):
    """Each cache's uv, npm and pnpm caches, by their own tools, once every TOOLS_EVERY."""
    if run.now - (state.get("tools_pruned_at") or 0) < TOOLS_EVERY:
        return
    path = tool_path(home)
    if not shutil.which("bwrap", path=path):
        run.error("bwrap is not installed; tool caches were not pruned")
        return
    fd = remote_probe.open_directory(root)
    try:
        keys = remote_probe.sandbox_keys(fd)
    finally:
        os.close(fd)
    for key in keys:
        cache = os.path.join(root, key)
        commands = (
            ("uv", ["uv", "cache", "prune"]),
            ("npm", ["npm", "cache", "verify"]),
            (".pnpm-store", ["pnpm", "store", "prune", "--store-dir", f"{cache}/.pnpm-store"]),
        )
        for folder, argv in commands:
            target = os.path.join(cache, folder)
            if not os.path.isdir(target) or os.path.islink(target):
                continue
            if not shutil.which(argv[0], path=path):
                continue
            print(f"{'Would run' if run.dry_run else 'Running'} {' '.join(argv)} in {cache}")
            if run.dry_run:
                continue
            try:
                done = subprocess.run(
                    confined(home, cache, argv, path),
                    capture_output=True,
                    text=True,
                    timeout=TOOL_TIMEOUT,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                run.error(f"{argv[0]} in {cache}: {exc}")
                continue
            if done.returncode:
                detail = (done.stderr.strip().splitlines() or [""])[-1][:200]
                run.error(f"{' '.join(argv[:3])} in {cache} failed: {detail}")
    if not run.dry_run:
        state["tools_pruned_at"] = run.now


def docker_host():
    """The rootless daemon's socket sandboxed agents use, as babysit-bwrap finds it."""
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    socket = os.path.join(runtime, "docker.sock")
    return socket if os.path.exists(socket) else None


def docker(argv, socket, timeout=600):
    return subprocess.run(
        ["docker", *argv],
        env={**os.environ, "DOCKER_HOST": f"unix://{socket}"},
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def prune_docker(run):
    """Stopped containers and unused images, from the rootless daemon; its use after."""
    socket = docker_host()
    if socket is None or not shutil.which("docker"):
        return None
    prunes = (
        ["container", "prune", "-f", "--filter", f"until={DOCKER_CONTAINERS}"],
        ["image", "prune", "-af", "--filter", f"until={DOCKER_IMAGES}", "--filter", "label!=keep"],
    )
    for argv in prunes:
        print(f"{'Would run' if run.dry_run else 'Running'} docker {' '.join(argv)}")
        if run.dry_run:
            continue
        try:
            done = docker(argv, socket)
        except (OSError, subprocess.SubprocessError) as exc:
            run.error(f"docker {argv[0]} prune: {exc}")
            continue
        if done.returncode:
            detail = (done.stderr.strip().splitlines() or [""])[-1][:200]
            run.error(f"docker {argv[0]} prune failed: {detail}")
    try:
        done = docker(["system", "df", "--format", "{{json .}}"], socket, timeout=120)
        rows = [json.loads(line) for line in done.stdout.splitlines() if line.strip()]
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        run.error(f"docker system df: {exc}")
        return None
    return [
        {
            "type": str(row.get("Type", ""))[:100],
            "size": str(row.get("Size", ""))[:100],
            "reclaimable": str(row.get("Reclaimable", ""))[:100],
        }
        for row in rows[:10]
        if isinstance(row, dict)
    ]


def report_caches(root):
    index = os.path.join(root, "index")
    caches: list[dict] = []
    try:
        fd = remote_probe.open_directory(root)
    except OSError:
        return caches
    try:
        keys = remote_probe.sandbox_keys(fd)
    finally:
        os.close(fd)
    for key in keys[:200]:
        path = os.path.join(root, key)
        recorded = os.path.join(index, "keys", key)
        fields = index_fields(recorded) or {}
        configuration = fields.get("configuration", "-")
        try:
            last_used = os.stat(recorded).st_mtime
        except OSError:
            last_used = None
        scratch = []
        folder = os.path.join(path, "scratch")
        if os.path.isdir(folder) and not os.path.islink(folder):
            for name in sorted(os.listdir(folder))[:100]:
                worktree = scratch_worktree(index, name)
                scratch.append(
                    {
                        "worktree": worktree if worktree and worktree.startswith("/") else None,
                        "bytes": du(os.path.join(folder, name)),
                    }
                )
        caches.append(
            {
                "key": key,
                "bytes": du(path),
                "configuration": configuration if configuration.startswith("/") else None,
                "grants": fields.get("grants", "-")[:100],
                "last_used": last_used,
                "scratch": scratch,
            }
        )
    return caches


def report_worktrees(src):
    """The largest checkouts of the clones under ``src``, as placement lists them."""
    try:
        clones = remote_probe._host_placement_state(src)["clones"]
    except (OSError, ValueError, subprocess.SubprocessError):
        return []
    paths = {item["path"] for clone in clones for item in clone["worktrees"]}
    sized = [{"path": path, "bytes": du(path)} for path in sorted(paths)]
    return sorted(sized, key=lambda item: -item["bytes"])[:MAX_WORKTREES]


def write_json(path, value):
    path = Path(os.path.expanduser(path))
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(value, stream)
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def read_state():
    try:
        value = json.loads(Path(os.path.expanduser(STATE)).read_text())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dry-run", action="store_true", help="report, remove nothing")
    parser.add_argument("--src", default="~/src", help="where the clones are (~/src)")
    args = parser.parse_args(argv)
    run = Run(args.dry_run)
    home = os.path.expanduser("~")
    root = os.path.expanduser(remote_probe.SANDBOX_CACHE)
    state = read_state()
    docker_use = None
    try:
        docker_use = prune_docker(run)
    except Exception as exc:  # One failing step never stops the others.
        run.error(f"Docker housekeeping failed: {exc}")
    if os.path.isdir(root):
        for step in (sweep_caches, lambda run, root: prune_tools(run, root, home, state)):
            try:
                step(run, root)
            except Exception as exc:
                run.error(f"Cache housekeeping failed: {exc}")
    report = {
        "generated_at": run.now,
        "caches": report_caches(root) if os.path.isdir(root) else [],
        "worktrees": report_worktrees(args.src),
        "docker": docker_use,
        "removed": run.removed[:MAX_REMOVED],
        "errors": run.errors[:50],
    }
    remote_probe.validate_disk_report(report)
    if not run.dry_run:
        write_json(remote_probe.DISK_REPORT, report)
        write_json(STATE, state)
    return 1 if run.errors else 0


if __name__ == "__main__":
    sys.exit(main())
