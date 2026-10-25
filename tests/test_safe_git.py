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
    assert argv[-5:] == [
        "show",
        "--no-ext-diff",
        "--no-textconv",
        "--ignore-submodules=all",
        "HEAD",
    ]
    assert env["GIT_NO_LAZY_FETCH"] == "1"


def quiet(git, *args, cwd=None):
    """Fixture Git that leaves the planted fsmonitor and hooks alone."""
    hardened = ("-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false")
    return git(*hardened, *args) if cwd is None else git(*hardened, *args, cwd=cwd)


def linked(planted, name):
    """A linked worktree of the planted clone, made by the fixture itself."""
    clone, marker, git = planted
    path = clone.parent / name
    quiet(git, "-c", "protocol.file.allow=always", "worktree", "add", "-q", "-b", name, str(path))
    marker.unlink(missing_ok=True)  # The fixture's own checkout ran the planted smudge.
    return path


def plant_driver(clone, marker, name, config):
    """A filter driver named only in ``config``, which every file now uses."""
    with (clone / ".git" / "info" / "attributes").open("a") as attributes:
        attributes.write(f"* filter={name}\n")
    script = marker.parent / "planted.sh"
    config.write_text(f'[filter "{name}"]\n\tclean = {script} clean\n\tsmudge = {script} smudge\n')


def remove(row, force):
    stub = type("Stub", (), {"progress": lambda *args: None})()
    workspace_overview.WorkspaceOverview.remove_worktree(stub, {}, 0, row, [], force)


def test_worktree_removal_runs_nothing_from_that_worktrees_own_config(planted):
    clone, marker, git = planted
    path = linked(planted, "topic")
    # extensions.worktreeConfig reads $common/worktrees/<id>/config.worktree for that
    # worktree alone, which a listing in the clone itself never sees.
    git("config", "extensions.worktreeConfig", "true")
    plant_driver(
        clone, marker, "wtonly", clone / ".git" / "worktrees" / "topic" / "config.worktree"
    )
    os.utime(path / "file.txt", (1, 1))
    row = {"remove_worktree": True, "path": str(path), "repo_root": str(clone)}
    remove(row, force=False)
    assert not path.exists()
    assert not marker.exists()


def test_worktree_removal_keeps_unapproved_changes(planted):
    clone, marker, git = planted
    path = linked(planted, "topic")
    row = {"remove_worktree": True, "path": str(path), "repo_root": str(clone)}
    (path / "new.txt").write_text("untracked\n")
    with pytest.raises(ValueError, match="uncommitted changes or untracked files"):
        remove(row, force=False)
    (path / "new.txt").unlink()
    (path / "file.txt").write_text("changed\n")
    with pytest.raises(ValueError, match="uncommitted changes or untracked files"):
        remove(row, force=False)
    assert (path / "file.txt").read_text() == "changed\n"
    # Ignored files go with the checkout, as Git's own clean check lets them.
    quiet(git, "checkout", "--", "file.txt", cwd=path)
    (clone / ".git" / "info" / "exclude").write_text("*.log\n")
    (path / "build.log").write_text("ignored\n")
    marker.unlink(missing_ok=True)
    remove(row, force=False)
    assert not path.exists()
    assert not marker.exists()


def test_worktree_removal_keeps_a_worktree_holding_submodules(planted, tmp_path):
    clone, marker, git = planted
    path = linked(planted, "topic")
    sub = tmp_path / "sub"
    subprocess.check_call(["git", "init", "-q", "-b", "main", str(sub)])
    subprocess.check_call(
        ["git", "-C", str(sub), "-c", "user.name=F", "-c", "user.email=f@e.test"]
        + ["commit", "-q", "--allow-empty", "-m", "sub"]
    )
    quiet(
        git, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub), "sub", cwd=path
    )
    quiet(git, "commit", "-q", "-m", "submodule", cwd=path)
    marker.unlink(missing_ok=True)
    row = {"remove_worktree": True, "path": str(path), "repo_root": str(clone)}
    with pytest.raises(ValueError, match="submodules"):
        remove(row, force=False)
    assert path.exists()
    remove(row, force=True)
    assert not path.exists()
    assert not marker.exists()


def test_unforced_worktree_commands_are_refused():
    with pytest.raises(ValueError, match="--force"):
        safe_git.prepare("/x", "worktree", "remove", "/x/y")
    with pytest.raises(ValueError, match="--no-checkout"):
        safe_git.prepare("/x", "worktree", "add", "/x/y", "main")


@pytest.fixture
def branch_driver(planted):
    """A driver only a linked worktree's branch or git dir brings into the config."""
    clone, marker, git = planted
    include = clone / ".git" / "linked.inc"
    plant_driver(clone, marker, "linked", include)
    git("config", "includeIf.onbranch:topic.path", str(include))
    git("config", "includeIf.gitdir:**/worktrees/**.path", str(include))
    assert "filter.linked" not in git("config", "--list")
    return planted


def test_wt_checks_out_new_worktrees_running_nothing_their_config_names(branch_driver, tmp_path):
    clone, marker, git = branch_driver
    tool = wt.Tool(wt.SubprocessRunner(os.environ), dict(os.environ))
    for branch in ("topic", "feature"):
        directory = tmp_path / "worktrees" / branch
        tool.add_worktree(str(clone), str(directory), branch, "main")
        assert git("symbolic-ref", "--short", "HEAD", cwd=directory) == branch
        assert (directory / "file.txt").read_text() == "aaaa\n"
        assert workspace_overview.git(directory, "status", "--porcelain") == ""
    # A branch made from the remote base tracks it, as a plain `worktree add -b` does.
    assert git("rev-parse", "--abbrev-ref", "topic@{upstream}") == "origin/main"
    assert not marker.exists()


def test_cron_checks_out_new_worktrees_running_nothing_their_config_names(branch_driver, tmp_path):
    clone, marker, git = branch_driver
    for branch in ("job", "feature"):
        job = {"clone": str(clone), "repo": "base/repo", "branch": branch, "base": "main"}
        directory = cron_agents.checkout(job, tmp_path / "src")
        assert git("symbolic-ref", "--short", "HEAD", cwd=directory) == branch
        assert (directory / "file.txt").read_text() == "aaaa\n"
    assert not marker.exists()


def test_status_and_diff_never_look_inside_submodules(planted, tmp_path):
    clone, marker, git = planted
    sub = tmp_path / "sub"
    subprocess.check_call(["git", "init", "-q", "-b", "main", str(sub)])
    (sub / "inner.txt").write_text("cccc\n")
    subprocess.check_call(["git", "-C", str(sub), "add", "."])
    subprocess.check_call(
        ["git", "-C", str(sub), "-c", "user.name=F", "-c", "user.email=f@e.test"]
        + ["commit", "-q", "-m", "sub"]
    )
    quiet(git, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub), "sub")
    quiet(git, "commit", "-q", "-m", "submodule", "--", ".gitmodules", "sub")
    # The submodule's own git dir is as writable as the clone's.
    modules = clone / ".git" / "modules" / "sub"
    (modules / "info").mkdir(exist_ok=True)
    plant_driver(clone, marker, "inner", modules / "inner.inc")
    (modules / "info" / "attributes").write_text("* filter=inner\n")
    subprocess.check_call(
        ["git", "-C", str(clone / "sub"), "config", "include.path", str(modules / "inner.inc")]
    )
    os.utime(clone / "sub" / "inner.txt", (1, 1))
    marker.unlink(missing_ok=True)
    assert workspace_overview.local_state(clone, "main")["changes"] == 1
    workspace_viewer.diff(clone)
    pr = {"head_branch": "main", "head_sha": git("rev-parse", "HEAD")}
    with pytest.raises(RuntimeError, match="uncommitted changes"):
        pr_supervisor.verify_worktree({"cwd": str(clone)}, pr)
    assert not marker.exists()


def test_common_dir_comes_from_the_layout_not_from_commondir_files(planted, tmp_path):
    clone, _, git = planted
    path = linked(planted, "topic")
    common = str((clone / ".git").resolve())
    assert safe_git.common_dir(str(clone)) == common
    assert safe_git.common_dir(str(path)) == common
    victim = tmp_path / "victim"
    subprocess.check_call(["git", "init", "-q", str(victim)])
    # What an agent can write: the worktree's admin dir, and the clone's own .git.
    admin = clone / ".git" / "worktrees" / "topic"
    (admin / "commondir").write_text(f"{victim}/.git\n")
    assert git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=path).startswith(
        str(victim.resolve())
    )
    with pytest.raises(ValueError, match="commondir"):
        safe_git.common_dir(str(path))
    (admin / "commondir").write_text("../..\n")
    (path / ".git").write_text(f"gitdir: {victim}/.git\n")
    with pytest.raises(ValueError, match="cannot be verified"):
        safe_git.common_dir(str(path))
    (path / ".git").write_text(f"gitdir: {admin}\n")
    (admin / "gitdir").write_text(f"{tmp_path}/elsewhere/.git\n")
    with pytest.raises(ValueError, match="does not register"):
        safe_git.common_dir(str(path))
    (clone / ".git" / "commondir").write_text(f"{victim}/.git\n")
    with pytest.raises(ValueError, match="commondir"):
        safe_git.common_dir(str(clone))


def test_codex_resume_grants_only_the_verified_common_dir(planted, tmp_path):
    clone, _, git = planted
    path = linked(planted, "topic")
    job = {
        "cwd": str(path),
        "sandbox": "workspace-write",
        "codex_command": ["codex"],
        "session_id": "s",
    }
    command = pr_supervisor.command_for(job, tmp_path)
    assert command[command.index("--add-dir") + 1] == str((clone / ".git").resolve())
    victim = tmp_path / "victim"
    subprocess.check_call(["git", "init", "-q", str(victim)])
    (clone / ".git" / "worktrees" / "topic" / "commondir").write_text(f"{victim}/.git\n")
    with pytest.raises(RuntimeError, match="commondir"):
        pr_supervisor.command_for(job, tmp_path)
    # A common dir recorded when the watch was registered must still be the one found.
    (clone / ".git" / "worktrees" / "topic" / "commondir").write_text("../..\n")
    with pytest.raises(RuntimeError, match="registered"):
        pr_supervisor.command_for({**job, "git_common_dir": f"{victim}/.git"}, tmp_path)


def test_fetch_never_runs_a_remote_helper_the_repository_names(planted, tmp_path, monkeypatch):
    clone, marker, git = planted
    helpers = tmp_path / "helpers"
    helpers.mkdir()
    (helpers / "git-remote-planted").symlink_to(marker.parent / "planted.sh")
    monkeypatch.setenv("PATH", f"{helpers}{os.pathsep}{os.environ['PATH']}")
    git("config", "remote.evil.url", "planted::somewhere")
    git("config", "protocol.planted.allow", "always")
    git("config", "url.planted::elsewhere.insteadOf", "https://example.test/")
    for remote in ("evil", "https://example.test/repo.git"):
        argv, env = safe_git.prepare(str(clone), "fetch", remote)
        env["GIT_TERMINAL_PROMPT"] = "0"
        assert subprocess.run(argv, env=env, capture_output=True).returncode
    assert not marker.exists()
    # Local clones still fetch from a path.
    pr_workspaces.git(clone, "fetch", "-q", "origin", "main")


@pytest.mark.parametrize(
    "key", ["http.cookieFile", "http.https://github.com.saveCookies", "http.curloptResolve"]
)
def test_credentials_are_refused_where_the_repository_reroutes_or_records_http(planted, key):
    clone, _, git = planted
    git("config", key, "true" if "save" in key.lower() else "/tmp/x")
    with pytest.raises(ValueError, match="Git was not run"):
        safe_git.prepare(str(clone), "fetch", "origin")


def test_config_writes_refuse_a_symlinked_common_config(tmp_path):
    common = tmp_path / ".git"
    common.mkdir()
    (common / "config").write_text("")
    safe_git.writable_config(str(common))
    target = tmp_path / "target"
    target.write_text("")
    (common / "config").unlink()
    (common / "config").symlink_to(target)
    with pytest.raises(ValueError, match="symbolic link"):
        safe_git.writable_config(str(common))
