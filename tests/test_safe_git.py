"""Host Git never runs what a sandboxed agent planted in a checkout's config or hooks."""

import io
import os
import shutil
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

URL = "https://github.com/base/repo.git"
# A host fetch: its repository named by URL, its refs explicit.
FETCH = ("fetch", "-q", URL, "+refs/heads/main:refs/remotes/origin/main")


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
    # GitHub, offline: the user's own config sends its URL to a local bare clone.
    config.write_text(f'[url "{origin}"]\n\tinsteadOf = {URL}\n')
    git("remote", "add", "origin", URL)
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
    pr_workspaces.git(clone, *FETCH)
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
        safe_git.prepare(str(clone), *FETCH)


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


@pytest.mark.parametrize(
    "key",
    [
        "lfs.extension.planted.clean",
        "lfs.extension.planted.smudge",
        "lfs.customtransfer.planted.path",
        "lfs.standalonetransferagent",
        "lfs.storage",
    ],
)
def test_lfs_programs_and_storage_the_repository_names_are_refused(key):
    lfs = [("global", "filter.lfs.process", "git-lfs filter-process")]
    with pytest.raises(ValueError, match="Git was not run"):
        safe_git.overrides([*lfs, ("worktree", key, "planted")], credentials=False)
    # The user's own git-lfs settings are theirs.
    assert safe_git.overrides([*lfs, ("global", key, "mine")], credentials=False) == []
    # Only the endpoint: no token or credential helper reaches it outside a fetch.
    assert safe_git.overrides([*lfs, ("local", "lfs.url", "x")], credentials=False) == []


@pytest.mark.skipif(not shutil.which("git-lfs"), reason="git-lfs is not installed")
def test_git_lfs_runs_no_extension_the_repository_names(planted):
    clone, marker, git = planted
    script = marker.parent / "planted.sh"
    global_config = Path(os.environ["GIT_CONFIG_GLOBAL"])
    global_config.write_text(
        '[filter "lfs"]\n\tprocess = git-lfs filter-process\n\trequired = true\n'
        "\tclean = git-lfs clean -- %f\n\tsmudge = git-lfs smudge -- %f\n"
    )
    with (clone / ".git" / "info" / "attributes").open("a") as attributes:
        attributes.write("*.txt filter=lfs\n")
    git("config", "lfs.extension.planted.clean", f"{script} clean %f")
    git("config", "lfs.extension.planted.smudge", f"{script} smudge %f")
    git("config", "lfs.extension.planted.priority", "0")
    with pytest.raises(ValueError, match="lfs.extension"):
        workspace_overview.git(clone, "status", "--porcelain")
    with pytest.raises(ValueError, match="lfs.extension"):
        workspace_viewer.diff(clone)
    assert not marker.exists()


def test_diff_commands_never_run_external_programs():
    argv, env = safe_git.prepare("/x", "-c", "core.quotePath=false", "show", "HEAD")
    assert argv[-5:] == [
        "show",
        "--no-ext-diff",
        "--no-textconv",
        "--ignore-submodules=dirty",
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


def test_host_git_works_in_the_checkout_whatever_core_worktree_names(planted, tmp_path):
    clone, marker, git = planted
    path = linked(planted, "topic")
    (path / "file.txt").write_text("uncommitted\n")
    # A clean copy of the branch elsewhere, which the worktree's own config points Git at.
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    (decoy / "file.txt").write_text("aaaa\n")
    git("config", "extensions.worktreeConfig", "true")
    admin = clone / ".git" / "worktrees" / "topic"
    (admin / "config.worktree").write_text(f"[core]\n\tworktree = {decoy}\n")
    assert quiet(git, "status", "--porcelain", cwd=path) == ""
    marker.unlink(missing_ok=True)
    top = workspace_overview.git(path, "rev-parse", "--show-toplevel")
    assert Path(top).resolve() == path.resolve()
    row = {"remove_worktree": True, "path": str(path), "repo_root": str(clone)}
    with pytest.raises(ValueError, match="uncommitted changes"):
        remove(row, force=False)
    assert (path / "file.txt").read_text() == "uncommitted\n"
    # core.bare would make Git refuse the checkout as having no work tree at all.
    (admin / "config.worktree").write_text("[core]\n\tbare = true\n")
    assert workspace_overview.local_state(path, "topic")["changes"] == 1
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


@pytest.fixture
def refused_on_branch(planted):
    """Config only a worktree on ``topic`` or ``feature`` sees, which safe_git refuses."""
    clone, marker, git = planted
    include = clone / ".git" / "refused.inc"
    include.write_text(f"[lfs]\n\tstorage = {marker.parent / 'elsewhere'}\n")
    for branch in ("topic", "feature"):
        git("config", f"includeIf.onbranch:{branch}.path", str(include))
    return planted


def assert_no_checkout_left(git, directory, branch, kept):
    assert not directory.exists()
    assert str(directory) not in git("worktree", "list", "--porcelain")
    assert bool(git("branch", "--list", branch)) == kept


def test_wt_leaves_no_empty_worktree_when_its_checkout_fails(refused_on_branch, tmp_path):
    clone, marker, git = refused_on_branch
    tool = wt.Tool(wt.SubprocessRunner(os.environ), dict(os.environ))
    for branch, kept in (("topic", False), ("feature", True)):
        directory = tmp_path / "worktrees" / branch
        with pytest.raises(wt.WtError, match="lfs.storage"):
            tool.add_worktree(str(clone), str(directory), branch, "main")
        assert_no_checkout_left(git, directory, branch, kept)
    assert not marker.exists()


def test_cron_leaves_no_empty_worktree_when_its_checkout_fails(refused_on_branch, tmp_path):
    clone, marker, git = refused_on_branch
    for branch, kept in (("topic", False), ("feature", True)):
        job = {"clone": str(clone), "repo": "base/repo", "branch": branch, "base": "main"}
        with pytest.raises(ValueError, match="lfs.storage"):
            cron_agents.checkout(job, tmp_path / "src")
        assert_no_checkout_left(git, tmp_path / "src" / "worktrees" / "repo" / branch, branch, kept)
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
    # A new commit checked out in the submodule is still a change of the clone's.
    subprocess.check_call(
        ["git", "-C", str(clone / "sub"), "-c", "user.name=F", "-c", "user.email=f@e.test"]
        + ["-c", "core.hooksPath=/dev/null", "-c", "filter.inner.clean="]
        + ["commit", "-q", "--allow-empty", "-m", "moved"]
    )
    os.utime(clone / "sub" / "inner.txt", (1, 1))
    assert workspace_overview.local_state(clone, "main")["changes"] == 2
    assert "sub" in workspace_viewer.git_text(clone, "status", "--porcelain")
    assert not marker.exists()


def test_a_read_fetches_no_missing_object_even_without_the_lazy_fetch_switch(planted, tmp_path):
    """GIT_NO_LAZY_FETCH needs Git 2.44 and can be turned off: no transport is allowed."""
    clone, marker, git = planted
    blob = git("rev-parse", "HEAD:file.txt")
    for pack in (clone / ".git" / "objects" / "pack").glob("*.pack"):
        moved = tmp_path / pack.name
        pack.rename(moved)
        pack.with_suffix(".idx").unlink()
        with moved.open("rb") as stream:
            subprocess.run(
                ["git", "-C", str(clone), "unpack-objects", "-q"], stdin=stream, check=True
            )
    loose = clone / ".git" / "objects" / blob[:2] / blob[2:]
    if loose.exists():
        loose.unlink()
    git("config", "core.repositoryformatversion", "1")
    git("config", "extensions.partialClone", "origin")
    git("config", "remote.origin.promisor", "true")
    for command in (["show", "HEAD"], ["diff", "HEAD"]):
        argv, env = safe_git.prepare(
            str(clone), *command, env={**os.environ, "GIT_NO_LAZY_FETCH": "0"}
        )
        subprocess.run(argv, env=env, capture_output=True, timeout=30)
    assert "upload-pack" not in (marker.read_text() if marker.exists() else "")


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
    # A rewrite, or a remote named by the very URL fetched, would reach the helper.
    for key in ("url.planted::elsewhere.insteadOf", f"remote.{URL}.vcs"):
        if key.startswith("remote."):
            git("config", key, "planted")
        with pytest.raises(ValueError, match="Git was not run"):
            safe_git.prepare(str(clone), *FETCH)
        git("config", "--unset", key)
    pr_workspaces.git(clone, *FETCH)
    assert not marker.exists()


def test_fetch_never_writes_through_a_planted_fetch_head(planted, tmp_path):
    clone, marker, git = planted
    target = tmp_path / "outside"
    target.write_text("kept\n")
    fetch_head = clone / ".git" / "FETCH_HEAD"
    fetch_head.unlink(missing_ok=True)
    fetch_head.symlink_to(target)
    pr_workspaces.git(clone, *FETCH)
    assert target.read_text() == "kept\n"


@pytest.mark.parametrize(
    "key", ["http.cookieFile", "http.https://github.com.saveCookies", "http.curloptResolve"]
)
def test_credentials_are_refused_where_the_repository_reroutes_or_records_http(planted, key):
    clone, _, git = planted
    git("config", key, "true" if "save" in key.lower() else "/tmp/x")
    with pytest.raises(ValueError, match="Git was not run"):
        safe_git.prepare(str(clone), *FETCH)


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


@pytest.mark.parametrize(
    "fetch",
    [
        ("fetch", "origin", "main"),
        ("fetch", "-q", "/elsewhere/private.git", "main:main"),
        ("fetch", "git@github.com:base/repo.git", "main"),
        ("fetch", URL),
        ("fetch", "https://github.com/base/..git", "main"),
    ],
)
def test_a_host_fetch_names_its_github_repository_and_refs(fetch):
    with pytest.raises(ValueError, match="Git was not run"):
        safe_git.prepare("/x", *fetch)


@pytest.mark.parametrize(
    "key,value",
    [
        ("url./elsewhere/private.git.insteadOf", URL),
        ("url.https://github.com/other/secret.git.insteadOf", URL),
        (f"remote.{URL}.url", "/elsewhere/private.git"),
    ],
)
def test_a_fetch_is_refused_where_the_repository_redirects_its_url(planted, tmp_path, key, value):
    clone, _, git = planted
    private = tmp_path / "private.git"
    subprocess.check_call(["git", "init", "-q", "--bare", str(private)])
    git("config", key, value.replace("/elsewhere/private.git", str(private)))
    with pytest.raises(ValueError, match="Git was not run"):
        safe_git.prepare(str(clone), *FETCH)


def test_a_fetch_gets_github_credentials_from_gh_alone(planted, hermetic_helper):
    clone, _, _ = planted
    argv, env = safe_git.prepare(str(clone), *FETCH, env={**os.environ, "GH_TOKEN": "t"})
    assert env["GH_TOKEN"] == "t"
    assert f"{safe_git.GITHUB}=!gh auth git-credential" in argv
    assert not [arg for arg in argv if "user-helper" in arg]  # The user's other helpers.
    argv, env = safe_git.prepare(
        str(clone), *FETCH, env={**os.environ, "GH_TOKEN": "t"}, credentials=False
    )
    assert "GH_TOKEN" not in env and env["GIT_TERMINAL_PROMPT"] == "0"
    assert not [arg for arg in argv if "gh auth" in arg or "user-helper" in arg]


@pytest.fixture
def hermetic_helper(monkeypatch, tmp_path):
    """A credential helper in the user's own config, for another host."""
    config = Path(os.environ["GIT_CONFIG_GLOBAL"])
    config.write_text(
        config.read_text() + '[credential "https://example.test"]\n\thelper = user-helper\n'
    )


def test_reads_never_reach_a_remote_whatever_the_environment_says(planted, tmp_path, monkeypatch):
    clone, marker, git = planted
    helpers = tmp_path / "helpers"
    helpers.mkdir()
    (helpers / "git-remote-foo").symlink_to(marker.parent / "planted.sh")
    monkeypatch.setenv("PATH", f"{helpers}{os.pathsep}{os.environ['PATH']}")
    blob = git("rev-parse", "HEAD:file.txt")
    for pack in (clone / ".git" / "objects" / "pack").glob("*.pack"):
        moved = tmp_path / pack.name
        pack.rename(moved)
        pack.with_suffix(".idx").unlink()
        with moved.open("rb") as stream:
            subprocess.run(
                ["git", "-C", str(clone), "unpack-objects", "-q"], stdin=stream, check=True
            )
    (clone / ".git" / "objects" / blob[:2] / blob[2:]).unlink(missing_ok=True)
    git("config", "core.repositoryformatversion", "1")
    git("config", "extensions.partialClone", "promised")
    git("config", "remote.promised.url", "foo::somewhere")
    git("config", "remote.promised.promisor", "true")
    git("config", "protocol.foo.allow", "always")
    for command in (["show", "HEAD"], ["log", "-p", "-1"], ["cat-file", "-p", blob]):
        argv, env = safe_git.prepare(
            str(clone), *command, env={**os.environ, "GIT_NO_LAZY_FETCH": "0"}
        )
        assert env["GIT_NO_LAZY_FETCH"] == "1"
        subprocess.run(argv, env=env, capture_output=True, timeout=30)
    assert not marker.exists()
    # Git before 2.44 ignores the switch: every read then overrides the repository's own
    # protocol permissions, which the listing finds.
    monkeypatch.setattr(safe_git, "git_version", lambda: (2, 43))
    argv, _ = safe_git.prepare(str(clone), "cat-file", "-p", blob)
    assert "protocol.foo.allow=never" in argv
    for name in ("file", "git", "ssh", "http", "https", "ftp", "ftps", "ext"):
        assert f"protocol.{name}.allow=never" in argv


@pytest.fixture
def private(planted, tmp_path):
    """A repository the agent may not read, with a branch named like the clone's base."""
    clone, _, git = planted
    private = tmp_path / "private"
    subprocess.check_call(["git", "init", "-q", "-b", "main", str(private)])
    (private / "secret.txt").write_text("secret\n")
    subprocess.check_call(["git", "-C", str(private), "add", "."])
    subprocess.check_call(
        ["git", "-C", str(private), "-c", "user.name=F", "-c", "user.email=f@e.test"]
        + ["commit", "-q", "-m", "secret"]
    )
    secret = subprocess.check_output(["git", "-C", str(private), "rev-parse", "HEAD"], text=True)
    # Also reachable as another GitHub repository the babysitter's token could read.
    config = Path(os.environ["GIT_CONFIG_GLOBAL"])
    config.write_text(
        config.read_text()
        + f'[url "{private}"]\n\tinsteadOf = https://github.com/other/secret.git\n'
    )
    return private, secret.strip()


def fetched(git, sha):
    return not subprocess.run(
        ["git", "-C", git("rev-parse", "--show-toplevel"), "cat-file", "-e", sha],
        capture_output=True,
    ).returncode


@pytest.mark.parametrize("planted_url", ["path", "https://github.com/other/secret.git"])
def test_cron_fetches_the_jobs_repository_whatever_origin_names(
    planted, private, tmp_path, planted_url
):
    clone, marker, git = planted
    path, secret = private
    git("config", "remote.origin.url", str(path) if planted_url == "path" else planted_url)
    git("config", "remote.upstream.url", URL)
    job = {"clone": str(clone), "repo": "base/repo", "branch": "job", "base": "main"}
    directory = cron_agents.checkout(job, tmp_path / "src")
    assert git("symbolic-ref", "--short", "HEAD", cwd=directory) == "job"
    assert not fetched(git, secret)
    # The job's repository is upstream now; its base lands where that remote's refs live.
    assert git("rev-parse", "upstream/main") == git("rev-parse", "job")
    assert not marker.exists()


def test_cron_starts_from_the_local_base_when_no_remote_tracks_the_jobs_repository(
    planted, private, tmp_path
):
    clone, _, git = planted
    path, secret = private
    git("config", "remote.origin.url", str(path))
    job = {"clone": str(clone), "repo": "base/repo", "branch": "job", "base": "main"}
    directory = cron_agents.checkout(job, tmp_path / "src")
    assert git("rev-parse", "HEAD", cwd=directory) == git("rev-parse", "main")
    assert not fetched(git, secret)


class Spy(wt.SubprocessRunner):
    def __init__(self, env):
        super().__init__(env)
        self.seen = []

    def run(self, argv, **kwargs):
        self.seen.append((list(argv), dict(kwargs.get("env") or {})))
        return super().run(argv, **kwargs)


def wt_fetches(spy):
    return [(argv, env) for argv, env in spy.seen if "fetch" in argv]


def test_wt_never_fetches_a_path_origin_names(planted, private, tmp_path):
    clone, _, git = planted
    path, secret = private
    git("config", "remote.origin.url", str(path))
    spy = Spy({**os.environ, "GH_TOKEN": "t"})
    stderr = io.StringIO()
    tool = wt.Tool(spy, {**os.environ, "GH_TOKEN": "t"}, stderr=stderr)
    directory = str(tmp_path / "worktrees" / "topic")
    tool.add_worktree(str(clone), directory, "topic", "main")
    assert not wt_fetches(spy) and not fetched(git, secret)
    assert "not a GitHub repository" in stderr.getvalue()
    assert git("rev-parse", "HEAD", cwd=Path(directory)) == git("rev-parse", "main")


def test_wt_fetches_another_github_repository_origin_names_only_without_the_token(
    planted, private, tmp_path
):
    clone, _, git = planted
    git("config", "remote.origin.url", "https://github.com/other/secret.git")
    spy = Spy({**os.environ, "GH_TOKEN": "t"})
    tool = wt.Tool(spy, {**os.environ, "GH_TOKEN": "t"})
    tool.add_worktree(str(clone), str(tmp_path / "worktrees" / "a"), "a", "main")
    [(argv, env)] = wt_fetches(spy)
    assert "https://github.com/other/secret.git" in argv
    assert "GH_TOKEN" not in env and not [arg for arg in argv if "gh auth" in arg]
    # The repository babysit was asked for gets the token, whatever origin names.
    spy.seen.clear()
    git("config", "remote.upstream.url", URL)
    tool.add_worktree(str(clone), str(tmp_path / "worktrees" / "b"), "b", "main", "base/repo")
    [(argv, env)] = wt_fetches(spy)
    assert URL in argv and "+refs/heads/main:refs/remotes/upstream/main" in argv
    assert env["GH_TOKEN"]


@pytest.mark.parametrize(
    "tty,env,trusted",
    [
        (True, {}, True),  # The user typing wt in a terminal, in their own clone.
        (False, {}, False),  # An agent or any other non-interactive run.
        (True, {"BABYSIT_HOME": "/state"}, False),  # A dashboard launch, whatever stdin is.
    ],
)
def test_wt_in_a_terminal_trusts_origins_github_repository_with_the_token(
    planted, private, tmp_path, tty, env, trusted
):
    clone, _, git = planted
    git("config", "remote.origin.url", "https://github.com/other/secret.git")
    spy = Spy(dict(os.environ))
    tool = wt.Tool(spy, {**os.environ, **env}, isatty=lambda: tty)
    options = wt.Options()
    tool.add_worktree(
        str(clone),
        str(tmp_path / "worktrees" / "a"),
        "a",
        "main",
        trust=tool.trusts_origin(options),
    )
    [(argv, fetch_env)] = wt_fetches(spy)
    assert "https://github.com/other/secret.git" in argv
    assert bool(fetch_env.get("GH_TOKEN")) is trusted
    assert any("gh auth" in arg for arg in argv) is trusted
    # Named by the dashboard, the repository is the host's record, never origin's.
    options.github_repo = "base/repo"
    assert not tool.trusts_origin(options)


def test_new_worktrees_start_from_the_remote_tracking_ref_not_a_lookalike(planted, tmp_path):
    clone, _, git = planted
    fetched = git("rev-parse", "refs/remotes/origin/main")
    # A local branch and a tag named origin/main would win `origin/main` by DWIM.
    quiet(git, "commit", "-q", "--allow-empty", "-m", "planted", "--", ".")
    planted_sha = git("rev-parse", "HEAD")
    quiet(git, "branch", "origin/main", planted_sha)
    quiet(git, "tag", "origin/main", planted_sha)
    quiet(git, "reset", "-q", "--soft", "HEAD~1")
    job = {"clone": str(clone), "repo": "base/repo", "branch": "job", "base": "main"}
    directory = cron_agents.checkout(job, tmp_path / "src")
    assert git("rev-parse", "HEAD", cwd=directory) == fetched
    tool = wt.Tool(wt.SubprocessRunner(os.environ), dict(os.environ))
    other = str(tmp_path / "worktrees" / "topic")
    tool.add_worktree(str(clone), other, "topic", "main", "base/repo")
    assert git("rev-parse", "HEAD", cwd=Path(other)) == fetched
