"""Cron jobs on a worker machine: its checkout, prompt, launch line and run, over fake ssh.

The worker is a directory under ``<tmp>/hosts/<ssh target>`` that the fake ssh uses as
HOME; its herdr state lives in the fake herdr's per-label store.
"""

import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

import claude_accounts
import cron_agents
import machines
import pytest
import remote_probe
import workspace_exit
from cron_jobs import CronJobs
from test_pr_workspaces import local  # noqa: F401 (fixture)

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


# A job on the worker, run by the dashboard's cron runner ----------------------------


@pytest.fixture
def remote(local, short_tmp, monkeypatch):  # noqa: F811
    """The dashboard's cron runner, and the worker Mac mini with its own clone of base/repo."""
    manager, _, git_local, state, _ = local
    (manager.home / "machines.json").write_text(
        json.dumps({"version": 1, "machines": [{**WORKER, "labels": ["safehouse"]}]})
    )
    host = state.parent / "hosts" / "mini.test"
    clone = host / "src" / "repo"
    subprocess.check_call(["git", "clone", "-q", os.environ["FAKE_HEAD"], str(clone)])
    git("remote", "set-url", "origin", URL, cwd=clone)
    # The fake ssh passes this environment through: its Codex home must be the worker's.
    shutil.copytree(os.environ["CODEX_HOME"], host / ".codex")
    monkeypatch.setenv("CODEX_HOME", str(host / ".codex"))
    monkeypatch.setattr(cron_agents, "REMOTE_WATCH", 0)
    jobs = CronJobs(manager.home, shell=["/bin/sh", "-c"], workspaces=manager)
    yield jobs, manager, git_local, state, host
    jobs.close()


def job_request(manager, **changes):
    return {
        "kind": "agent",
        "name": "Nightly triage",
        "repo": "base/repo",
        "clone": str((manager.src / "repo").resolve()),
        "branch": "nightly",
        "base": "feature",
        "agent": "codex",
        "prompt": "Triage new issues",
        "schedule": {"cron": "0 6 * * *"},
        "timeout": 7200,
        "machine": "mini",
        **changes,
    }


def settled(jobs, statuses=("starting",)):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        run = jobs.snapshot()["jobs"][0]["runs"][0]
        if run["status"] not in statuses:
            return run
        time.sleep(0.05)
    raise AssertionError("The run did not settle")


def store(state):
    return json.loads(Path(state).read_text())


def test_a_job_on_a_worker_starts_its_agent_there(remote):
    jobs, manager, git_local, state, host = remote
    saved = jobs.save(job_request(manager))
    assert saved["machine"] == "mini" and saved["clone"] == "~/src/repo"
    jobs.run_now(saved["id"])
    run = settled(jobs)
    assert run["status"] == "running", run
    launched = run["agent_run"]
    path = (host / "src/worktrees/repo/nightly").resolve()
    assert launched["path"] == str(path)
    assert (launched["machine"], launched["machine_label"]) == ("mini", "Mac mini")
    assert launched["workspace_id"] == "mini/w1" and launched["url"] is None
    feature = subprocess.check_output(
        ["git", "-C", os.environ["FAKE_HEAD"], "rev-parse", "feature"], text=True
    ).strip()
    assert git("rev-parse", "HEAD", cwd=path) == feature
    data = store(state)
    # Every herdr call went to the worker's server; nothing was opened here.
    assert all(call[0] == "--machine=Mac mini" for call in data["calls"])
    assert not data["workspaces"] and not data["agents"]
    worker = data["machines"]["Mac mini"]
    [opened] = [c for c in worker["calls"] if c[:2] == ["worktree", "open"]]
    assert opened[opened.index("--cwd") + 1] == str((host / "src/repo").resolve())
    assert opened[opened.index("--path") + 1] == str(path)
    [typed] = worker["runs"]
    assert typed[0] == launched["pane"] and typed[1].startswith("codex ")
    [staged] = (host / ".cache/babysit/cron-prompts").iterdir()
    assert f'"$(cat {staged})"' in typed[1]
    assert staged.read_text() == "Triage new issues\n" + workspace_exit.brief(
        launched["exit_marker"]
    )
    [agent] = worker["agents"]
    assert agent["pane_id"] == launched["pane"] and agent["cwd"] == str(path)
    # The worker's agent keeps the job busy, as a local one does.
    jobs.tick()
    with pytest.raises(ValueError, match="already running"):
        jobs.run_now(saved["id"])


def rollout(host, cwd, sid, *events):
    day = host / ".codex" / "sessions" / "2026" / "10" / "25"
    day.mkdir(parents=True, exist_ok=True)
    path = day / f"rollout-2026-10-25T10-00-00-{sid}.jsonl"
    records = [{"type": "session_meta", "payload": {"id": sid, "cwd": str(cwd)}}, *events]
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    return path


def test_a_workers_run_keeps_the_agents_answer_once_it_confirms(remote):
    jobs, manager, _, state, host = remote
    saved = jobs.save(job_request(manager))
    jobs.run_now(saved["id"])
    run = settled(jobs)
    launched = run["agent_run"]
    sid = str(uuid.uuid4())
    started = [
        {"type": "event_msg", "payload": {"type": "user_message", "message": "Triage"}},
        {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "t1"}},
    ]
    path = rollout(host, launched["path"], sid, *started)
    jobs.watch()
    run = jobs.snapshot()["jobs"][0]["runs"][0]
    assert run["status"] == "running" and "Mac mini" in run["message"]
    done = {
        "type": "event_msg",
        "payload": {
            "type": "task_complete",
            "turn_id": "t1",
            "last_agent_message": f"Triaged 3 issues.\n{launched['exit_marker']}",
        },
    }
    with path.open("a") as stream:
        stream.write(json.dumps(done) + "\n")
    jobs.watch()
    run = jobs.snapshot()["jobs"][0]["runs"][0]
    assert run["status"] == "attention", run["message"]
    assert run["message"].startswith("Finished on Mac mini") and "still open" in run["message"]
    assert "Triaged 3 issues." in jobs.log(run["id"])["text"]
    assert launched["exit_marker"] not in jobs.log(run["id"])["text"]


def test_a_workers_agent_that_leaves_without_confirming_needs_attention(remote):
    jobs, manager, _, state, host = remote
    saved = jobs.save(job_request(manager))
    jobs.run_now(saved["id"])
    settled(jobs)
    data = store(state)
    data["machines"]["Mac mini"]["agents"] = []
    Path(state).write_text(json.dumps(data))
    jobs.watch()
    run = jobs.snapshot()["jobs"][0]["runs"][0]
    assert run["status"] == "attention" and "left its pane on Mac mini" in run["message"]


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"machine": "box"}, "Unknown machine box"),
        ({"unsandboxed": True}, "always run in their sandbox"),
        ({"machine": 3}, "Choose a machine"),
    ],
)
def test_jobs_a_worker_cannot_run_are_refused(remote, changes, message):
    jobs, manager, *_ = remote
    with pytest.raises(ValueError, match=message):
        jobs.save(job_request(manager, **changes))


def test_shell_jobs_and_disabled_workers_stay_off_workers(remote):
    jobs, manager, *_ = remote
    with pytest.raises(ValueError, match="Only agent jobs"):
        jobs.save({"name": "x", "command": "true", "schedule": {"every": 60}, "machine": "mini"})
    (manager.home / "machines.json").write_text(
        json.dumps({"version": 1, "machines": [{**WORKER, "enabled": False}]})
    )
    with pytest.raises(ValueError, match="disabled"):
        jobs.save(job_request(manager))


def test_auto_is_resolved_once_at_save(remote):
    jobs, manager, *_ = remote
    saved = jobs.save(job_request(manager, machine="auto"))
    # Auto never picks a worker yet: the job is pinned here.
    assert saved["machine"] is None and saved["clone"] == str((manager.src / "repo").resolve())
    local_job = jobs.save(job_request(manager, name="Here", branch="here", machine="local"))
    assert local_job["machine"] is None


def test_a_linux_workers_launch_line_runs_its_babysit_bwrap():
    worker = machines.Machine(
        "box", "Box", "box", "linux", "~/src", "~/b", "python3", 4, ("bwrap", "gpg"), None, True
    )
    found = {
        "path": "/home/b/src/worktrees/repo/n",
        "common": "/home/b/src/repo/.git",
        "home": "/home/b",
        "clone": "/home/b/src/repo",
        "claude_home": "/home/b/.claude/accounts/work",
        "codex_home": "/home/b/.codex",
        "launcher": "/home/b/b/scripts/babysit-bwrap",
    }
    job = {
        "agent": "claude",
        "prompt": "Go",
        "model": "",
        "effort": "",
        "docker": True,
        "claude_account": "work",
    }
    line = cron_agents.remote_command(job, worker, found, "/home/b/.cache/p", "")
    unset = " ".join(claude_accounts.AUTH_ENV)
    assert line == (
        f"(unset {unset}; CLAUDE_CONFIG_DIR=/home/b/.claude/accounts/work "
        "/home/b/b/scripts/babysit-bwrap --enable=docker,gpg "
        "--claude-config-dir=/home/b/.claude/accounts/work "
        "--add-dirs=/home/b/src/worktrees/repo/n:/home/b/src/repo/.git -- "
        'claude --dangerously-skip-permissions "$(cat /home/b/.cache/p)")'
    )
    # A Mac worker's line relies on its own shell functions, as this machine's does.
    mac = worker._replace(os="darwin", labels=("safehouse",))
    codex = {**job, "agent": "codex", "claude_account": ""}
    assert cron_agents.remote_command(codex, mac, found, "/p", "") == (
        'SAFE_ENABLE=docker codex -c check_for_update_on_startup=false "$(cat /p)"'
    )
    with pytest.raises(ValueError, match="babysit-bwrap is not installed under ~/b"):
        cron_agents.remote_command(job, worker, {**found, "launcher": None}, "/p", "")


def test_without_a_registry_a_job_runs_here_as_before(local, short_tmp):  # noqa: F811
    manager, _, _, state, _ = local
    jobs = CronJobs(manager.home, shell=["/bin/sh", "-c"], workspaces=manager)
    try:
        request = job_request(manager)
        del request["machine"]
        saved = jobs.save(request)
        assert saved["machine"] is None and saved["clone"] == request["clone"]
        jobs.run_now(saved["id"])
        run = settled(jobs)
        assert run["status"] == "running", run
        assert list(run["agent_run"]) == [
            "machine",
            "workspace_id",
            "pane",
            "path",
            "url",
            "exit_marker",
            "launched_at",
        ]
        data = store(state)
        assert "ssh" not in data and "machines" not in data
        assert not any(call[0].startswith("--machine") for call in data["calls"])
    finally:
        jobs.close()
