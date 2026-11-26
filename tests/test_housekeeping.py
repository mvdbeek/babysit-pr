"""Worker disk housekeeping against a temporary home, with fake docker, bwrap and uv."""

import fcntl
import json
import os
import socket
import subprocess
import time
from pathlib import Path

import housekeeping
import pytest
import remote_probe

DAY = 86400
OLD = time.time() - 40 * DAY


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(remote_probe, "SANDBOX_CACHE", "~/.cache/babysit-sandbox")
    # No rootless Docker unless a test makes its socket, and tool caches pruned just now,
    # so that only the tests for them run either.
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    state = home / ".local/state/babysit-pr"
    state.mkdir(parents=True)
    (state / "housekeeping.json").write_text(json.dumps({"tools_pruned_at": time.time()}))
    return home


def root(home):
    return home / ".cache/babysit-sandbox"


def cache(home, key, configuration="-", used=None):
    """A sandbox cache as babysit-bwrap leaves it, recorded as last launched at ``used``."""
    path = root(home) / key
    path.mkdir(parents=True)
    index = root(home) / "index/keys"
    index.mkdir(parents=True, exist_ok=True)
    (index / key).write_text(f"configuration {configuration}\ngrants -\n")
    if used is not None:
        os.utime(index / key, (used, used))
    return path


def age(path, when=OLD):
    """Set the times of ``path`` and everything beneath it, without following links."""
    for top, folders, files in os.walk(path):
        for name in folders + files:
            os.utime(os.path.join(top, name), (when, when), follow_symlinks=False)
    os.utime(path, (when, when), follow_symlinks=False)


def scratch(home, key, worktree):
    """``worktree``'s scratch directory in cache ``key``, recorded as babysit-bwrap does."""
    name = remote_probe.scratch_id(worktree)
    path = root(home) / key / "scratch" / name
    (path / "galaxy-venv").mkdir(parents=True)
    index = root(home) / "index/scratch"
    index.mkdir(parents=True, exist_ok=True)
    (index / name).write_text(f"{os.path.realpath(worktree)}\n")
    return path


def live_run(home, key):
    """A running sandbox of cache ``key``: its run's lock held, as bwrap's pid 1 holds it.

    The lock is held by a child process, since flock locks of one process never conflict.
    """
    run = root(home) / "runs" / "live"
    run.mkdir(parents=True)
    info = "pid 1\ncwd /\nconfiguration -\n" + (f"cache {key}\n" if key else "")
    (run / "info").write_text(info)
    (run / "lock").touch()
    holder = subprocess.Popen(["flock", str(run / "lock"), "sleep", "60"], stdin=subprocess.DEVNULL)
    for _ in range(100):
        with open(run / "lock") as probe:
            try:
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(probe, fcntl.LOCK_UN)
            except BlockingIOError:
                return holder
        time.sleep(0.05)
    holder.kill()
    pytest.fail("the run's lock was never taken")


def report(home):
    return json.loads((home / ".local/state/babysit-pr/disk-usage.json").read_text())


def test_unused_caches_go_and_used_or_running_ones_stay(home, tmp_path):
    agent = tmp_path / "claude-account"
    agent.mkdir()
    used = cache(home, "a" * 16, agent, time.time())
    unused = cache(home, "b" * 16, agent, OLD)
    orphaned = cache(home, "c" * 16, tmp_path / "removed-account", time.time())
    running = cache(home, "d" * 16, agent, OLD)
    # From before launches were recorded: it gets its time now, and stays.
    unrecorded = root(home) / ("e" * 16)
    unrecorded.mkdir()
    (unrecorded / "uv").mkdir()
    holder = live_run(home, "d" * 16)
    try:
        assert housekeeping.main(["--src", str(tmp_path / "src")]) == 0
    finally:
        holder.kill()
    assert used.is_dir() and running.is_dir() and unrecorded.is_dir()
    assert not unused.exists() and not orphaned.exists()
    assert (root(home) / "index/keys" / ("e" * 16)).read_text() == "configuration -\ngrants -\n"
    assert not (root(home) / "index/keys" / ("b" * 16)).exists()
    assert list((root(home) / "trash").iterdir()) == []
    removed = {item["path"]: item["why"] for item in report(home)["removed"]}
    assert removed == {
        str(unused): "no launch used it for 30 days",
        str(orphaned): f"its agent configuration {tmp_path / 'removed-account'} is gone",
    }
    listed = {entry["key"] for entry in report(home)["caches"]}
    assert listed == {"a" * 16, "d" * 16, "e" * 16}


def test_a_running_sandbox_that_does_not_name_its_cache_keeps_every_cache(home, tmp_path):
    unused = cache(home, "b" * 16, "-", OLD)
    holder = live_run(home, None)
    try:
        housekeeping.main(["--src", str(tmp_path / "src")])
    finally:
        holder.kill()
    assert unused.is_dir()


def test_scratch_of_gone_checkouts_and_stale_runtime_data_go(home, tmp_path):
    key = "a" * 16
    path = cache(home, key, "-", time.time())
    kept, gone = tmp_path / "kept", tmp_path / "gone"
    kept.mkdir()
    kept_scratch, gone_scratch = scratch(home, key, kept), scratch(home, key, gone)
    unrecorded = path / "scratch/0123456789abcdef"
    unrecorded.mkdir()
    # A tool cache stays however old; runtime data goes once nothing in it changed for a week.
    (path / "uv/archive").mkdir(parents=True)
    (path / "bacass-runtime/work").mkdir(parents=True)
    (path / "bacass-runtime/work/data").write_text("x" * 4096)
    (path / "taxprofiler-runtime/work").mkdir(parents=True)
    (path / "artifacts").mkdir()
    for entry in ("uv", "bacass-runtime", "taxprofiler-runtime", "artifacts"):
        age(path / entry)
    age(unrecorded)
    age(kept_scratch)
    (path / "taxprofiler-runtime/work/new").write_text("written today")
    os.utime(path / "taxprofiler-runtime/work", (OLD, OLD))
    # An agent's link out of the cache goes as a link; what it leads to stays.
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious").write_text("keep")
    (path / "escape").symlink_to(outside)
    age(outside)
    os.utime(path / "escape", (OLD, OLD), follow_symlinks=False)
    housekeeping.main(["--src", str(tmp_path / "src")])
    assert kept_scratch.is_dir()
    assert not gone_scratch.exists() and not unrecorded.exists()
    assert not (path / "bacass-runtime").exists() and not (path / "escape").is_symlink()
    assert (path / "uv/archive").is_dir() and (path / "artifacts").is_dir()
    assert (path / "taxprofiler-runtime/work/new").exists()
    assert (outside / "precious").read_text() == "keep"
    index = root(home) / "index/scratch"
    assert sorted(entry.name for entry in index.iterdir()) == [remote_probe.scratch_id(kept)]
    removed = {item["path"]: item for item in report(home)["removed"]}
    assert removed[str(gone_scratch)]["why"] == f"its checkout {gone} is gone"
    assert removed[str(path / "bacass-runtime")]["bytes"] >= 4096
    (listed,) = report(home)["caches"]
    assert listed["scratch"] == [{"worktree": str(kept), "bytes": listed["scratch"][0]["bytes"]}]


def test_a_dry_run_reports_and_removes_nothing(home, tmp_path, capsys):
    unused = cache(home, "b" * 16, "-", OLD)
    (unused / "bacass-runtime").mkdir()
    age(unused / "bacass-runtime")
    assert housekeeping.main(["--dry-run", "--src", str(tmp_path / "src")]) == 0
    assert (unused / "bacass-runtime").is_dir()
    assert f"Would remove {unused} " in capsys.readouterr().out
    assert not (home / ".local/state/babysit-pr/disk-usage.json").exists()
    assert not (root(home) / "trash").exists()


def fake(folder, name, body):
    folder.mkdir(exist_ok=True)
    path = folder / name
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)


@pytest.fixture
def docker_daemon(home, tmp_path, monkeypatch):
    """A rootless Docker socket where babysit-bwrap finds it, and a fake docker CLI."""
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(runtime / "docker.sock"))
    calls = tmp_path / "docker-calls"
    usage = '{"Type":"Images","Size":"48.2GB","Reclaimable":"40GB (83%)"}'
    body = f'echo "$DOCKER_HOST $*" >> "{calls}"\n[ "$1" = system ] && echo \'{usage}\'\nexit 0'
    fake(tmp_path / "bin", "docker", body)
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:{os.environ['PATH']}")
    yield calls
    listener.close()


def test_docker_prunes_stopped_containers_and_unused_images(home, tmp_path, docker_daemon):
    housekeeping.main(["--src", str(tmp_path / "src")])
    host = f"unix://{tmp_path / 'runtime/docker.sock'}"
    assert docker_daemon.read_text().splitlines() == [
        f"{host} container prune -f --filter until=72h",
        f"{host} image prune -af --filter until=168h --filter label!=keep",
        f"{host} system df --format {{{{json .}}}}",
    ]
    assert report(home)["docker"] == [
        {"type": "Images", "size": "48.2GB", "reclaimable": "40GB (83%)"}
    ]


def test_without_rootless_docker_nothing_is_pruned(home, tmp_path, monkeypatch):
    calls = tmp_path / "docker-calls"
    fake(tmp_path / "bin", "docker", f'echo "$*" >> "{calls}"')
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:{os.environ['PATH']}")
    housekeeping.main(["--src", str(tmp_path / "src")])
    assert not calls.exists()
    assert report(home)["docker"] is None


def test_tool_caches_are_pruned_weekly_in_a_sandbox_that_writes_only_their_cache(
    home, tmp_path, monkeypatch
):
    (home / ".local/state/babysit-pr/housekeeping.json").unlink()
    path = cache(home, "a" * 16, "-", time.time())
    (path / "uv").mkdir()
    calls = tmp_path / "calls"
    # Runs the command after `--`, as bwrap would, recording the sandbox it was given.
    fake(
        tmp_path / "bin",
        "bwrap",
        f'echo "bwrap $*" >> "{calls}"\nwhile [ "$1" != -- ]; do shift; done\nshift\nexec "$@"',
    )
    fake(tmp_path / "bin", "uv", f'echo "uv $*" >> "{calls}"')
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:/usr/bin:/bin")
    housekeeping.main(["--src", str(tmp_path / "src")])
    # Only uv: npm has no cache there, and pnpm is not installed.
    sandboxed, pruned = calls.read_text().splitlines()
    assert pruned == "uv cache prune"
    words = sandboxed.split()
    assert "--unshare-all" in words and "--clearenv" in words
    assert f"--tmpfs {home} " in sandboxed and "--tmpfs /run " in sandboxed
    binds = [words[i + 1 : i + 3] for i, word in enumerate(words) if word == "--bind"]
    assert binds == [[str(path), str(path)]]
    housekeeping.main(["--src", str(tmp_path / "src")])
    assert len(calls.read_text().splitlines()) == 2  # Not again within the week.


def test_the_shared_conda_cleans_its_package_cache(home, tmp_path, monkeypatch):
    (home / ".local/state/babysit-pr/housekeeping.json").unlink()
    path = cache(home, "a" * 16, "-", time.time())
    (path / "conda-pkgs").mkdir()
    (path / "tool-conda/bin").mkdir(parents=True)
    calls = tmp_path / "calls"
    fake(
        tmp_path / "bin",
        "bwrap",
        f'echo "bwrap $*" >> "{calls}"\nwhile [ "$1" != -- ]; do shift; done\nshift\nexec "$@"',
    )
    fake(path / "tool-conda/bin", "conda", f'echo "conda $*" >> "{calls}"')
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:/usr/bin:/bin")
    housekeeping.main(["--src", str(tmp_path / "src")])
    sandboxed, pruned = calls.read_text().splitlines()
    assert pruned == "conda clean --all --yes"
    assert f"--setenv CONDA_PKGS_DIRS {path / 'conda-pkgs'}" in sandboxed


def test_the_report_lists_the_largest_checkouts(home, tmp_path):
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    src = tmp_path / "src"
    clone = src / "repo"
    subprocess.run([*git, "init", "-q", "-b", "main", str(clone)], check=True)
    subprocess.run([*git, "-C", str(clone), "commit", "-q", "--allow-empty", "-m", "x"], check=True)
    worktree = tmp_path / "worktrees/repo/feature"
    subprocess.run(
        [*git, "-C", str(clone), "worktree", "add", "-q", "-b", "feature", str(worktree)],
        check=True,
    )
    (worktree / "big").write_bytes(b"x" * 1024 * 1024)
    housekeeping.main(["--src", str(src)])
    listed = report(home)["worktrees"]
    assert [item["path"] for item in listed] == [str(worktree.resolve()), str(clone.resolve())]
    assert listed[0]["bytes"] >= 1024 * 1024
    assert remote_probe.validate_disk_report(report(home))


def test_remove_scratch_removes_a_checkouts_scratch_from_every_cache(home, tmp_path):
    worktree, other = tmp_path / "worktree", tmp_path / "other"
    worktree.mkdir()
    other.mkdir()
    first = scratch(home, "a" * 16, worktree)
    second = scratch(home, "b" * 16, worktree)
    kept = scratch(home, "a" * 16, other)
    (first / "galaxy-venv/lib").mkdir()
    (first / "galaxy-venv/lib").chmod(0o500)  # Read-only, like Go's module cache.
    # A cache whose scratch an agent replaced with a link out of it is skipped.
    outside = tmp_path / "outside"
    (outside / remote_probe.scratch_id(worktree)).mkdir(parents=True)
    (root(home) / ("c" * 16)).mkdir()
    (root(home) / ("c" * 16) / "scratch").symlink_to(outside)
    assert remote_probe.remove_scratch(str(worktree)) == 2
    assert not first.exists() and not second.exists() and kept.is_dir()
    assert (outside / remote_probe.scratch_id(worktree)).is_dir()
    assert not (root(home) / "index/scratch" / remote_probe.scratch_id(worktree)).exists()
    assert (root(home) / "index/scratch" / remote_probe.scratch_id(other)).exists()


def test_remove_scratch_without_a_sandbox_cache_does_nothing(home, tmp_path):
    assert remote_probe.remove_scratch(str(tmp_path)) == 0


def test_scratch_id_matches_babysit_bwraps():
    worktree = Path("/home/agent/src/worktrees/galaxy/fix")
    found = subprocess.run(
        ["sha256sum"], input=str(worktree).encode(), capture_output=True, check=True
    )
    assert remote_probe.scratch_id(worktree) == found.stdout.decode()[:16]
