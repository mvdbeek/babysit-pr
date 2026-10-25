"""Host Git never runs what a sandboxed agent planted in a checkout's config or hooks."""

import os
import subprocess
from pathlib import Path

import cron_agents
import pr_supervisor
import pr_workspaces
import pytest
import safe_git
import workspace_overview
import workspace_viewer
import wt


@pytest.fixture
def planted(tmp_path, monkeypatch):
    """A clone whose own config, hooks and attributes run a marker-writing script."""
    config = tmp_path / "gitconfig"
    config.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    marker = tmp_path / "marker"
    script = tmp_path / "planted.sh"
    # Filters pass content through; anything else exits, so a regression fails fast.
    script.write_text(
        f'#!/bin/sh\necho "$1 ${{GH_TOKEN:+token}}" >> {marker}\n'
        'case "$1" in clean|smudge|textconv) exec cat ;; esac\nexit 1\n'
    )
    script.chmod(0o755)
    clone = tmp_path / "src" / "repo"
    clone.mkdir(parents=True)

    def git(*args, cwd=clone):
        return subprocess.check_output(["git", "-C", str(cwd), *args], text=True).strip()

    git("init", "-b", "main")
    git("config", "user.name", "Fixture")
    git("config", "user.email", "fixture@example.test")
    (clone / "file.txt").write_text("aaaa\n")
    git("add", ".")
    git("commit", "-m", "base")
    git("branch", "feature")
    origin = tmp_path / "origin.git"
    git("clone", "--bare", str(clone), str(origin))
    git("remote", "add", "origin", str(origin))
    git("fetch", "-q", "origin")
    # What a sandboxed agent can write: the common dir's config, hooks and attributes.
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    for directory in (hooks, clone / ".git" / "hooks"):
        for name in ("post-checkout", "reference-transaction", "post-index-change"):
            (directory / name).symlink_to(script)
    (clone / ".git" / "info" / "attributes").write_text("* filter=planted diff=planted\n")
    for key, value in {
        "core.fsmonitor": f"{script} fsmonitor",
        "core.hooksPath": str(hooks),
        "core.sshCommand": f"{script} ssh",
        "core.askPass": f"{script} askpass",
        "credential.helper": f"!{script} helper",
        "credential.https://github.com.helper": f"!{script} github-helper",
        "filter.planted.clean": f"{script} clean",
        "filter.planted.smudge": f"{script} smudge",
        "filter.planted.required": "true",
        "diff.planted.textconv": f"{script} textconv",
        "diff.planted.command": f"{script} diff-command",
        "diff.external": f"{script} external",
        "remote.origin.uploadpack": f"{script} upload-pack",
    }.items():
        git("config", key, value)
    # Same size, other content and mtime: Git has to read the file to tell.
    (clone / "file.txt").write_text("bbbb\n")
    os.utime(clone / "file.txt", (1, 1))
    return clone, marker, git


def test_unhardened_git_runs_the_planted_commands(planted):
    clone, marker, _ = planted
    subprocess.run(["git", "-C", str(clone), "status"], capture_output=True)
    assert "fsmonitor" in marker.read_text()


def test_supervisor_worktree_check_runs_nothing_planted(planted):
    clone, marker, git = planted
    pr = {"head_branch": "main", "head_sha": git("rev-parse", "HEAD")}
    with pytest.raises(RuntimeError, match="uncommitted changes"):
        pr_supervisor.verify_worktree({"cwd": str(clone)}, pr)
    assert not marker.exists()


def test_inventory_and_viewer_run_nothing_planted(planted):
    clone, marker, _ = planted
    assert workspace_overview.local_state(clone, "main")["changes"] == 1
    diff = workspace_viewer.diff(clone)
    assert [f["path"] for f in diff["files"]] == ["file.txt"]
    assert not marker.exists()


def test_workspace_git_keeps_the_token_from_local_commands(planted, monkeypatch):
    clone, marker, _ = planted
    seen = []
    original = pr_workspaces.run

    def run(*args, env=None, **kwargs):
        seen.append((args, env))
        return original(*args, env=env, **kwargs)

    monkeypatch.setattr(pr_workspaces, "run", run)
    assert pr_workspaces.git(clone, "status", "--porcelain") == "M file.txt"
    pr_workspaces.git(clone, "fetch", "-q", "origin", "main")
    status, fetch = seen
    assert "GH_TOKEN" not in status[1] and fetch[1]["GH_TOKEN"]
    assert f"{safe_git.GITHUB}=!gh auth git-credential" in fetch[0]
    assert not marker.exists()


def test_cron_checkout_fetches_and_adds_a_worktree_running_nothing_planted(planted, tmp_path):
    clone, marker, git = planted
    job = {"clone": str(clone), "repo": "base/repo", "branch": "job", "base": "main"}
    directory = cron_agents.checkout(job, tmp_path / "src")
    assert git("symbolic-ref", "--short", "HEAD", cwd=directory) == "job"
    # A later run finds the checkout on another branch and switches it back.
    git("-c", "core.hooksPath=/dev/null", "switch", "-q", "-c", "elsewhere", cwd=directory)
    marker.unlink(missing_ok=True)  # The fixture's own switch may start the fsmonitor.
    assert cron_agents.checkout(job, tmp_path / "src") == directory
    assert not marker.exists()


def test_wt_fetches_and_adds_a_worktree_running_nothing_planted(planted, tmp_path):
    clone, marker, git = planted
    tool = wt.Tool(wt.SubprocessRunner(os.environ), dict(os.environ))
    directory = str(tmp_path / "worktrees" / "topic")
    tool.add_worktree(str(clone), directory, "topic", "main")
    assert git("symbolic-ref", "--short", "HEAD", cwd=Path(directory)) == "topic"
    assert not marker.exists()


def test_planted_credential_helpers_are_never_asked(planted):
    clone, marker, _ = planted
    argv, env = safe_git.prepare(str(clone), "credential", "fill")
    env["GIT_TERMINAL_PROMPT"] = "0"
    for host in ("github.com", "example.test"):
        subprocess.run(argv, env=env, input=f"protocol=https\nhost={host}\n\n", text=True)
    assert not marker.exists()


def test_credentials_are_refused_where_the_repository_weakens_tls(planted):
    clone, _, git = planted
    git("config", "http.https://github.com.sslVerify", "false")
    safe_git.prepare(str(clone), "status")
    with pytest.raises(ValueError, match="sslverify"):
        safe_git.prepare(str(clone), "fetch", "origin")


def test_filters_from_the_users_own_config_are_kept():
    config = [
        ("global", "filter.lfs.process", "git-lfs filter-process"),
        ("global", "filter.lfs.required", "true"),
        ("local", "filter.lfs.process", "planted"),
        ("local", "filter.lfs.required", "false"),
        ("local", "filter.planted.clean", "planted"),
        ("worktree", "filter.planted.required", "true"),
    ]
    assert safe_git.overrides(config, credentials=False) == [
        "-c",
        "filter.lfs.process=git-lfs filter-process",
        "-c",
        "filter.lfs.required=true",
        "-c",
        "filter.planted.clean=",
        "-c",
        "filter.planted.required=false",
    ]


def test_diff_commands_never_run_external_programs():
    argv, env = safe_git.prepare("/x", "-c", "core.quotePath=false", "show", "HEAD")
    assert argv[-4:] == ["show", "--no-ext-diff", "--no-textconv", "HEAD"]
    assert env["GIT_NO_LAZY_FETCH"] == "1"
