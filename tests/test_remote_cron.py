"""Cron jobs on a worker machine: its checkout, prompt, launch line and run, over fake ssh.

The worker is a directory under ``<tmp>/hosts/<ssh target>`` that the fake ssh uses as
HOME; its herdr state lives in the fake herdr's per-label store.
"""

import json
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

import machines
import pytest
import remote_probe

WORKER = {"id": "mini", "label": "Mac mini", "ssh": "mini.test", "os": "darwin"}
URL = "https://github.com/base/repo.git"


def git(*args, cwd):
    return subprocess.check_output(
        ["git", "-c", "user.name=F", "-c", "user.email=f@example.test", "-C", str(cwd), *args],
        text=True,
    ).strip()


@pytest.fixture
def short_tmp(monkeypatch):
    """ssh control sockets need a short directory; never the real /tmp's shared one."""
    path = tempfile.mkdtemp(prefix="bs", dir="/tmp")
    monkeypatch.setattr(tempfile, "tempdir", path)
    machines.control_dir.cache_clear()
    yield Path(path)
    machines.control_dir.cache_clear()
    shutil.rmtree(path)


@pytest.fixture
def host(tmp_path, short_tmp, monkeypatch):
    """A worker's home, its clone of base/repo, and GitHub's copy reachable offline."""
    config = tmp_path / "gitconfig"
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    home = tmp_path / "hosts" / "mini.test"
    clone = home / "src" / "repo"
    clone.mkdir(parents=True)
    git("init", "-q", "-b", "main", cwd=clone)
    (clone / "f").write_text("a\n")
    git("add", "f", cwd=clone)
    git("commit", "-q", "-m", "base", cwd=clone)
    github = tmp_path / "github.git"
    git("clone", "-q", "--bare", str(clone), str(github), cwd=tmp_path)
    work = tmp_path / "upstream-work"
    git("clone", "-q", str(github), str(work), cwd=tmp_path)
    git("commit", "-q", "--allow-empty", "-m", "upstream moved on", cwd=work)
    git("push", "-q", "origin", "main", cwd=work)
    config.write_text(
        f'[url "{github}"]\n\tinsteadOf = {URL}\n[protocol "file"]\n\tallow = always\n'
    )
    git("remote", "add", "origin", URL, cwd=clone)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("BABYSIT_CLAUDE_TRUST")  # Only ever the fake worker's config here.
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    (home / ".claude").mkdir()
    (home / ".claude.json").write_text(json.dumps({"projects": {}}))
    upstream = git("rev-parse", "HEAD", cwd=work)
    return home, clone, upstream


def checkout(home, clone, branch="nightly", base="main", **changes):
    args = {
        "clone": str(clone),
        "repo": "base/repo",
        "branch": branch,
        "base": base,
        "directory": str(home / "src" / "worktrees" / "repo" / branch),
        "agent": "claude",
        "account": None,
        "babysit_root": str(home / "src" / "babysit-pr"),
        **changes,
    }
    return remote_probe.validate_cron_checkout(remote_probe._host_cron_checkout(**args))


def test_a_workers_job_checkout_starts_from_its_repository_on_github(host):
    home, clone, upstream = host
    found = checkout(home, clone)
    path = Path(found["path"])
    assert path == (home / "src/worktrees/repo/nightly").resolve()
    assert git("symbolic-ref", "--short", "HEAD", cwd=path) == "nightly"
    assert git("rev-parse", "HEAD", cwd=path) == upstream
    assert found["common"] == str((clone / ".git").resolve())
    assert found["home"] == str(home.resolve())
    assert found["claude_home"] == str((home / ".claude").resolve())
    assert found["codex_home"] == str((home / ".codex").resolve())
    # Claude's folder trust dialog would hold up a run nobody watches.
    projects = json.loads((home / ".claude.json").read_text())["projects"]
    assert projects[str(path)]["hasTrustDialogAccepted"] is True
    # The next run uses the same checkout, back on the job's branch.
    git("switch", "-q", "-c", "elsewhere", cwd=path)
    assert checkout(home, clone)["path"] == str(path)
    assert git("symbolic-ref", "--short", "HEAD", cwd=path) == "nightly"


def test_a_workers_checkout_never_runs_what_its_clone_names_or_follows_its_urls(host, tmp_path):
    home, clone, upstream = host
    marker = tmp_path / "ran"
    script = tmp_path / "evil"
    script.write_text(f"#!/bin/sh\necho \"$0 $*\" >> '{marker}'\ncat\n")
    script.chmod(0o700)
    (clone / ".git" / "info" / "attributes").write_text("* filter=x\n")
    for key, value in {
        "filter.x.clean": script,
        "filter.x.smudge": script,
        "core.fsmonitor": script,
        "core.hooksPath": tmp_path,
        "credential.helper": f"!{script}",
        "remote.origin.uploadpack": script,
    }.items():
        git("config", key, str(value), cwd=clone)
    private = tmp_path / "private"
    git("init", "-q", "-b", "main", str(private), cwd=tmp_path)
    git("commit", "-q", "--allow-empty", "-m", "secret", cwd=private)
    secret = git("rev-parse", "HEAD", cwd=private)
    git("config", "remote.origin.url", str(private), cwd=clone)
    git("config", "remote.upstream.url", URL, cwd=clone)
    found = checkout(home, clone)
    assert git("rev-parse", "HEAD", cwd=Path(found["path"])) == upstream
    assert subprocess.run(["git", "-C", str(clone), "cat-file", "-e", secret]).returncode
    assert not marker.exists()
    # A rewrite of the GitHub URL in the clone's own config refuses the fetch.
    git("config", f"url.{private}.insteadOf", URL, cwd=clone)
    with pytest.raises(ValueError, match="redirects a fetch"):
        checkout(home, clone, branch="other")


def test_a_branch_checked_out_in_the_workers_clone_is_refused(host):
    home, clone, _ = host
    with pytest.raises(ValueError, match="checked out in the clone itself"):
        checkout(home, clone, branch="main")
    for change in ({"branch": "-x"}, {"repo": "base/.."}, {"agent": "sh"}, {"account": "../x"}):
        with pytest.raises(ValueError):
            checkout(home, clone, **change)


def test_a_named_claude_account_on_the_worker(host):
    home, clone, _ = host
    with pytest.raises(ValueError, match="Unknown Claude account work"):
        checkout(home, clone, account="work")
    account = home / ".claude" / "accounts" / "work"
    account.mkdir(parents=True)
    (account / ".claude.json").write_text("{}")
    found = checkout(home, clone, account="work")
    assert found["claude_home"] == str(account.resolve())
    assert json.loads((account / ".claude.json").read_text())["projects"][found["path"]] == {
        "hasTrustDialogAccepted": True
    }


def claude_session(home, cwd, sid, *records):
    folder = home / ".claude" / "projects" / "".join(c if c.isalnum() else "-" for c in str(cwd))
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{sid}.jsonl"
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    return path


def test_the_latest_turn_of_a_workers_transcript(host, tmp_path):
    home, clone, _ = host
    sid = str(uuid.uuid4())
    turn = [
        {"type": "user", "sessionId": sid, "uuid": "u1", "message": {"content": "Do it"}},
        {
            "type": "assistant",
            "sessionId": sid,
            "message": {"content": [{"type": "text", "text": "Done.\nTASK-DONE"}]},
        },
    ]
    path = claude_session(home, clone, sid, *turn)
    found = remote_probe.validate_turn(remote_probe._host_turn(str(path), sid, "claude"))
    assert found == {"turn": "u1", "complete": None}
    with path.open("a") as stream:
        stream.write(json.dumps({"type": "system", "subtype": "turn_duration"}) + "\n")
    found = remote_probe.validate_turn(remote_probe._host_turn(str(path), sid, "claude"))
    assert found == {"turn": "u1", "complete": "Done.\nTASK-DONE"}
    outside = tmp_path / "outside.jsonl"
    outside.write_text(path.read_text())
    with pytest.raises(ValueError, match="Not a session transcript"):
        remote_probe._host_turn(str(outside), sid, "claude")
    with pytest.raises(RuntimeError, match="session ID does not match"):
        remote_probe._host_turn(str(path), str(uuid.uuid4()), "claude")
