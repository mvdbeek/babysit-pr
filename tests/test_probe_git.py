"""A worker's Git, run by the probe on checkouts agents can write, never runs their programs.

Each test plants one way a repository's own config or attributes can name a program,
in a real temporary repository, and checks the probe's reads leave it unrun.
"""

import shutil
import subprocess
from pathlib import Path

import pytest
import remote_probe


@pytest.fixture(autouse=True)
def hermetic(tmp_path, monkeypatch):
    """No user or system Git config, and no signing for the fixtures' own commits."""
    config = tmp_path / "gitconfig"
    config.write_text('[commit]\n\tgpgsign = false\n[protocol "file"]\n\tallow = always\n')
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("HOME", str(tmp_path))
    return config


def git(*args, cwd):
    return subprocess.check_output(
        [
            "git",
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=f@example.test",
            "-C",
            str(cwd),
            *args,
        ],
        text=True,
    ).strip()


@pytest.fixture
def evil(tmp_path):
    """A program that records each run and passes its input through."""
    marker = tmp_path / "ran"
    script = tmp_path / "evil"
    script.write_text(f"#!/bin/sh\necho \"$0 $*\" >> '{marker}'\ncat\n")
    script.chmod(0o700)
    return script, marker


def repo(path):
    path.mkdir(parents=True)
    git("init", "-q", "-b", "main", cwd=path)
    (path / "f").write_text("a\n")
    git("add", "f", cwd=path)
    git("commit", "-q", "-m", "base", cwd=path)
    return path


def stat_dirty(path):
    """Git hashes a file again only when its stat data no longer matches the index."""
    subprocess.run(["touch", "-m", "-t", "200001010000", str(path)], check=True)


def scan(path):
    return remote_probe.local_state(remote_probe.host_git, str(path), "main")


@pytest.mark.parametrize("kind", ["clean", "process"])
@pytest.mark.parametrize("required", [False, True])
def test_status_runs_no_filter_driver(tmp_path, evil, kind, required):
    script, marker = evil
    clone = repo(tmp_path / "clone")
    (clone / ".git" / "info" / "attributes").write_text("* filter=x\n")
    git("config", f"filter.x.{kind}", str(script), cwd=clone)
    if required:
        git("config", "filter.x.required", "true", cwd=clone)
    stat_dirty(clone / "f")
    state = scan(clone)
    assert not marker.exists()
    assert state["error"] is None


def test_a_driver_only_the_user_config_defines_is_blanked_too(tmp_path, evil, hermetic):
    script, marker = evil
    hermetic.write_text(hermetic.read_text() + f'[filter "x"]\n\tclean = {script}\n')
    clone = repo(tmp_path / "clone")
    (clone / ".gitattributes").write_text("* filter=x\n")
    stat_dirty(clone / "f")
    assert scan(clone)["error"] is None and not marker.exists()


@pytest.mark.skipif(shutil.which("git-lfs") is None, reason="git-lfs is not installed")
def test_git_lfs_never_runs_the_extensions_a_checkout_configures(tmp_path, evil, hermetic):
    script, marker = evil
    # What `git lfs install` writes to the user's config.
    hermetic.write_text(
        hermetic.read_text()
        + '[filter "lfs"]\n\tclean = git-lfs clean -- %f\n\tsmudge = git-lfs smudge -- %f\n'
        + "\tprocess = git-lfs filter-process\n\trequired = true\n"
    )
    clone = repo(tmp_path / "clone")
    (clone / ".gitattributes").write_text("f filter=lfs\n")
    git("config", "lfs.extension.x.clean", f"{script} %f", cwd=clone)
    git("config", "lfs.extension.x.smudge", f"{script} %f", cwd=clone)
    git("config", "lfs.extension.x.priority", "0", cwd=clone)
    git("config", "lfs.standalonetransferagent", "x", cwd=clone)
    git("config", "lfs.customtransfer.x.path", str(script), cwd=clone)
    stat_dirty(clone / "f")
    scan(clone)
    assert not marker.exists()


def test_a_submodules_own_filter_never_runs(tmp_path, evil):
    script, marker = evil
    sub = repo(tmp_path / "sub")
    clone = repo(tmp_path / "clone")
    git("submodule", "-q", "add", str(sub), "s", cwd=clone)
    git("commit", "-q", "-m", "submodule", cwd=clone)
    # The ignore setting a status of the parent would otherwise follow into it.
    git("config", "submodule.s.ignore", "none", cwd=clone)
    inner = Path(git("rev-parse", "--absolute-git-dir", cwd=clone / "s"))
    (inner / "info").mkdir(exist_ok=True)
    (inner / "info" / "attributes").write_text("* filter=x\n")
    git("config", "filter.x.clean", str(script), cwd=clone / "s")
    stat_dirty(clone / "s" / "f")
    assert scan(clone)["error"] is None and not marker.exists()
    # A new commit checked out in it is still a change of the parent's.
    git("commit", "-q", "--allow-empty", "-m", "moved", cwd=clone / "s")
    marker.unlink(missing_ok=True)  # That commit's own refresh ran it, not the scan.
    stat_dirty(clone / "s" / "f")
    assert scan(clone)["changes"] == 1 and not marker.exists()


def evil_merge(clone):
    """A merge whose result differs from both parents, so its combined diff shows ``f``."""
    git("switch", "-q", "-c", "side", cwd=clone)
    (clone / "g").write_text("side\n")
    git("add", "g", cwd=clone)
    git("commit", "-q", "-m", "side", cwd=clone)
    git("switch", "-q", "main", cwd=clone)
    (clone / "f").write_text("main\n")
    git("commit", "-q", "-am", "main", cwd=clone)
    git("merge", "-q", "--no-commit", "side", cwd=clone)
    (clone / "f").write_text("evil\n")
    git("add", "f", cwd=clone)
    git("commit", "-q", "-m", "merge", cwd=clone)
    return git("rev-parse", "HEAD", cwd=clone)


def test_a_merge_is_compared_without_textconv_or_an_external_diff(tmp_path, evil):
    script, marker = evil
    clone = repo(tmp_path / "clone")
    sha = evil_merge(clone)
    (clone / ".git" / "info" / "attributes").write_text("* diff=t\n")
    git("config", "diff.t.textconv", str(script), cwd=clone)
    git("config", "diff.t.command", str(script), cwd=clone)
    git("config", "diff.external", str(script), cwd=clone)
    git("config", "core.pager", str(script), cwd=clone)  # Never used without a terminal.
    assert remote_probe.landed(remote_probe.host_git, str(clone), {sha}, {sha}) == set()
    assert not marker.exists()


ARMOR = {
    "gpg.program": "PGP SIGNATURE",
    "gpg.openpgp.program": "PGP SIGNATURE",
    "gpg.ssh.program": "SSH SIGNATURE",
    "gpg.x509.program": "SIGNED MESSAGE",
}


def signed(clone, sha, armor):
    """``sha`` rewritten with a signature header, as a signed commit carries one."""
    body = git("cat-file", "commit", sha, cwd=clone).split("\n")
    at = next(i for i, line in enumerate(body) if line.startswith("committer ")) + 1
    body[at:at] = [f"gpgsig -----BEGIN {armor}-----", " ", f" -----END {armor}-----"]
    return subprocess.run(
        ["git", "-C", str(clone), "hash-object", "-t", "commit", "-w", "--stdin"],
        input="\n".join(body) + "\n",
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


@pytest.mark.parametrize("program", list(ARMOR))
def test_no_signature_is_checked_with_a_configured_program(tmp_path, evil, program):
    script, marker = evil
    clone = repo(tmp_path / "clone")
    sha = signed(clone, evil_merge(clone), ARMOR[program])
    git("update-ref", "refs/heads/main", sha, cwd=clone)
    git("config", "log.showSignature", "true", cwd=clone)
    git("config", program, str(script), cwd=clone)
    # ssh verification asks its program only once a signers file is named.
    git("config", "gpg.ssh.allowedSignersFile", str(script), cwd=clone)
    remote_probe.landed(remote_probe.host_git, str(clone), {sha}, {sha})
    assert not marker.exists()


def test_a_missing_object_is_never_fetched_from_a_promisor(tmp_path, evil):
    script, marker = evil
    origin = repo(tmp_path / "origin")
    sha = evil_merge(origin)
    clone = tmp_path / "clone"
    git("clone", "-q", "--no-local", str(origin), str(clone), cwd=tmp_path)
    blob = git("rev-parse", f"{sha}:f", cwd=clone)
    # Every object loose, then one blob dropped, as a partial clone leaves it out.
    packs = clone / ".git" / "objects" / "pack"
    moved = tmp_path / "packs"
    packs.rename(moved)
    for pack in moved.glob("*.pack"):
        with pack.open("rb") as stream:
            subprocess.run(
                ["git", "-C", str(clone), "unpack-objects", "-q"], stdin=stream, check=True
            )
    (clone / ".git" / "objects" / blob[:2] / blob[2:]).unlink()
    git("config", "core.repositoryformatversion", "1", cwd=clone)
    git("config", "extensions.partialClone", "origin", cwd=clone)
    git("config", "remote.origin.promisor", "true", cwd=clone)
    git("config", "remote.origin.uploadpack", str(script), cwd=clone)
    remote_probe.landed(remote_probe.host_git, str(clone), {sha}, {sha})
    assert not marker.exists()


def test_a_worktree_config_pointing_git_elsewhere_is_refused(tmp_path):
    clone = repo(tmp_path / "clone")
    elsewhere = repo(tmp_path / "elsewhere")
    (elsewhere / "secret").write_text("not this checkout's\n")
    checkout = tmp_path / "checkout"
    git("worktree", "add", "-q", "-b", "work", str(checkout), cwd=clone)
    git("config", "extensions.worktreeConfig", "true", cwd=clone)
    git("config", "--worktree", "core.worktree", str(elsewhere), cwd=checkout)
    state = remote_probe.local_state(remote_probe.host_git, str(checkout), "work")
    assert state["changes"] is None and "core.worktree" in state["error"]


# The viewer's diff and log, on a worker (``host_output``) and on this machine -------


def runners():
    import workspace_viewer

    return {"worker": remote_probe.host_output, "local": workspace_viewer.git_output}


def branch(tmp_path):
    """A checkout one commit past origin/main, with an uncommitted change to ``f``."""
    clone = repo(tmp_path / "clone")
    git("update-ref", "refs/remotes/origin/main", "HEAD", cwd=clone)
    (clone / "f").write_text("b\n")
    git("commit", "-q", "-am", "work", cwd=clone)
    (clone / "f").write_text("c\n")
    return clone


@pytest.mark.parametrize("runner", ["worker", "local"])
def test_the_viewer_diff_runs_no_filter_driver(tmp_path, evil, runner):
    script, marker = evil
    clone = branch(tmp_path)
    (clone / ".git" / "info" / "attributes").write_text("* filter=x\n")
    git("config", "filter.x.clean", str(script), cwd=clone)
    git("config", "filter.x.required", "true", cwd=clone)
    found = remote_probe.diff(runners()[runner], str(clone), "branch")
    assert [f["path"] for f in found["files"]] == ["f"] and not marker.exists()


@pytest.mark.parametrize("runner", ["worker", "local"])
def test_the_viewer_log_checks_no_signature(tmp_path, evil, runner):
    script, marker = evil
    clone = branch(tmp_path)
    git("update-ref", "refs/heads/main", signed(clone, "HEAD", ARMOR["gpg.program"]), cwd=clone)
    git("config", "log.showSignature", "true", cwd=clone)
    git("config", "gpg.program", str(script), cwd=clone)
    git("config", "core.pager", str(script), cwd=clone)
    git("config", "diff.external", str(script), cwd=clone)
    found = remote_probe.diff(runners()[runner], str(clone), "branch")
    assert [c["subject"] for c in found["commits"]] == ["work"] and not marker.exists()


@pytest.mark.parametrize("runner", ["worker", "local"])
def test_the_viewer_diff_fetches_no_missing_object(tmp_path, evil, runner):
    script, marker = evil
    clone = branch(tmp_path)
    blob = git("rev-parse", "origin/main:f", cwd=clone)
    (clone / ".git" / "objects" / blob[:2] / blob[2:]).unlink()
    git("config", "core.repositoryformatversion", "1", cwd=clone)
    git("config", "extensions.partialClone", "origin", cwd=clone)
    git("config", "remote.origin.url", str(tmp_path / "clone"), cwd=clone)
    git("config", "remote.origin.promisor", "true", cwd=clone)
    git("config", "remote.origin.uploadpack", str(script), cwd=clone)
    with pytest.raises(ValueError):
        remote_probe.diff(runners()[runner], str(clone), "branch")
    assert not marker.exists()


def test_the_viewer_never_reads_where_core_worktree_points(tmp_path):
    clone = branch(tmp_path)
    elsewhere = repo(tmp_path / "elsewhere")
    (elsewhere / "f").write_text("elsewhere\n")
    git("config", "core.worktree", str(elsewhere), cwd=clone)
    # A worker refuses the checkout; this machine pins Git to it (safe_git's --work-tree).
    with pytest.raises(ValueError, match="core.worktree"):
        remote_probe.diff(runners()["worker"], str(clone), "uncommitted")
    found = remote_probe.diff(runners()["local"], str(clone), "uncommitted")
    [changed] = found["files"]
    assert changed["path"] == "f" and "+c" in changed["lines"]
