"""Explicit reviews: broker request, human approval, then the existing isolated launcher.

All homes, accounts, providers, ssh and dashboard servers are test fixtures.
"""

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import dashboard
import dashboard_auth
import pr_workspaces as pw
import pytest
import remote_probe
import worker_broker
from test_machine_loads import settle
from test_pr_workspaces import finish, local  # noqa: F401 (fixture)
from test_remote_cron import git, short_tmp  # noqa: F401 (fixture)
from test_remote_launch import register, remote, remote_commands, store  # noqa: F401 (fixture)
from test_subtasks import launch, parent


def review(**changes):
    return launch(
        **{
            "review": True,
            "agent": "claude",
            "base": "a" * 40,
            "task": "Review this commit; save findings in BABYSIT_ARTIFACT_DIR",
            **changes,
        }
    )


def worker_parent(**changes):
    return parent(machine="mini", machine_label="Mac mini", sandbox="bwrap", **changes)


@pytest.fixture
def review_worker(remote, monkeypatch, tmp_path):  # noqa: F811
    """Real Linux wrapper with a fake bwrap recording argv and running fake providers.

    Kernel namespace enforcement is covered separately by the Linux sandbox tests.
    """
    manager, _, _, _ = remote
    register(manager.home, os="linux", labels=["bwrap", "claude-account:oss"])
    site = tmp_path / "review-site"
    site.mkdir()
    scripts = str(Path(pw.__file__).parent)
    (site / "sitecustomize.py").write_text(
        f"import sys\nsys.path.append({scripts!r})\nimport sandbox\nsandbox.DEFAULT='bwrap'\n"
    )
    monkeypatch.setenv("PYTHONPATH", f"{site}:{os.environ['PYTHONPATH']}")
    binary = tmp_path / "bin/bwrap"
    binary.write_text(
        f"#!{sys.executable} -S\n"
        + """import json, os, sys
from pathlib import Path
args = sys.argv[1:]
if args == ['--help']:
    sys.exit(0)
(Path(os.environ['HOME']) / 'review-sandbox-argv.json').write_text(json.dumps(args))
env = dict(os.environ)
env.pop('CLAUDE_CONFIG_DIR', None)
env.pop('CODEX_HOME', None)
for i, word in enumerate(args):
    if word == '--setenv':
        env[args[i+1]] = args[i+2]
# This fake never mounts /tmp; do not use the enclosing worker's PATH diagnostic.
env['PATH'] = env['PATH'].removeprefix('/tmp/babysit-bin:')
command = args[args.index('--')+1:]
os.execvpe(command[0], command, env)
"""
    )
    binary.chmod(0o700)
    return remote


@pytest.mark.parametrize(
    "target,account", [("claude", "default"), ("claude", "oss"), ("codex", "")]
)
def test_approved_review_launches_the_requested_provider_and_exact_local_commit(
    review_worker,
    target,
    account,  # noqa: F811
):  # noqa: F811
    manager, _, state, worker = review_worker
    selected = worker / ".claude" / "accounts" / "oss" if account == "oss" else worker / ".claude"
    selected.mkdir(parents=True)
    (selected / ".claude.json").write_text("{}")
    (worker / ".claude/accounts/unselected").mkdir(parents=True)
    clone = worker / "src/repo"
    # Unpushed work: the requested SHA exists only on this worker, not GitHub.
    git("commit", "--allow-empty", "-m", "local review target", cwd=clone)
    sha = git("rev-parse", "HEAD", cwd=clone)
    manager.loads.snapshot()
    settle(manager.loads)
    source = "codex" if target == "claude" else "claude"
    asked = worker_parent(agent=source, config_dir=str(worker / f".{source}"))
    request = review(agent=target, claude_account=account, base=sha)
    op = manager.subtask(request, asked)["operation"]
    assert op["status"] == "approval" and op["machine"] == "mini"
    assert not manager.workers and not store(state).get("machines", {}).get("Mac mini", {}).get(
        "agents"
    )
    manager.run_waiting()
    assert manager.operation(op["pr"])["status"] == "approval"
    assert manager.scheduled_tasks()["waiting"][0]["review_request"]["base"] == sha
    # Mutation of the caller's copy cannot change what approval launches.
    request.update(claude_account="unapproved", task="Different task")
    manager.approve_review(op["pr"], "reviewer@example.test")
    done = finish(manager, op["pr"])
    assert done["status"] == "complete", done
    assert done["id"] == op["id"] and done["parent"]["session"] == asked["session"]
    assert done["parent"]["root"] == f"mini:{asked['cwd']}" and done["parent"]["depth"] == 1
    assert done["parent"]["review_approval"]["by"] == "reviewer@example.test"
    assert done["parent"]["review_approval"]["request"] == op["review_request"]
    assert git("rev-parse", "HEAD", cwd=done["path"]) == sha
    [agent] = store(state)["machines"]["Mac mini"]["agents"]
    assert agent["agent"] == target and agent["task"] == op["review_request"]["task"]
    [command] = [c for c in remote_commands(state) if "wt.py" in c]
    assert f"--{target}" in command and sha in command
    if target == "claude":
        assert f"--claude-account {account}" in command
        if account == "oss":
            assert agent["claude_config_dir"] == str(selected)
    else:
        assert "--claude-account" not in command
    argv = json.loads((worker / "review-sandbox-argv.json").read_text())
    binds = [argv[i + 2] for i, word in enumerate(argv) if word == "--bind"]
    expected = str(selected) if target == "claude" else str(worker / ".codex")
    assert expected in binds
    assert str(worker / (".codex" if target == "claude" else ".claude")) not in binds
    if account == "default":
        assert ["--tmpfs", str(worker / ".claude/accounts")] in [
            argv[i : i + 2] for i in range(len(argv) - 1)
        ]
    assert any("babysit-bwrap" in run[-1] for run in store(state)["machines"]["Mac mini"]["runs"])
    # A replay has no authority and launches no second agent.
    with pytest.raises(ValueError, match="no longer awaiting approval"):
        manager.approve_review(op["pr"], "reviewer@example.test")
    assert len(store(state)["machines"]["Mac mini"]["agents"]) == 1


@pytest.mark.parametrize(
    "change,message",
    [
        ({"base": "main"}, "full commit SHA"),
        ({"docker": True}, "Only a launch with Docker"),
        ({"machine": "local"}, "never on the controller"),
        ({"review": "yes"}, "Invalid review flag"),
        ({"agent": "codex"}, "different provider"),
        ({"claude_account": "../private"}, "Invalid Claude account"),
        ({"permission_mode": "bypassPermissions"}, "only auto"),
    ],
)
def test_review_requests_do_not_bypass_scope_or_grants(local, change, message):  # noqa: F811
    manager, *_ = local
    request = {**review(), **change}
    with pytest.raises(ValueError, match=message):
        manager.subtask(request, worker_parent())
    assert not manager.state()["operations"]


def test_review_cannot_use_local_parent_claims_or_switch_codex_account(local):  # noqa: F811
    manager, *_ = local
    with pytest.raises(ValueError, match="worker's broker"):
        manager.subtask(review(), parent())
    with pytest.raises(ValueError, match="only be selected for Claude"):
        manager.subtask(review(agent="codex", claude_account="oss"), worker_parent(agent="claude"))


def test_review_requests_expire_cancel_and_count_against_lineage_limits(local):  # noqa: F811
    manager, *_ = local
    op = manager.subtask(review(), worker_parent())["operation"]
    manager.run_waiting(now=op["wait_until"] + 1)
    assert manager.operation(op["pr"])["status"] == "failed"
    with pytest.raises(ValueError, match="no longer awaiting approval"):
        manager.approve_review(op["pr"], "user")
    op = manager.subtask(review(), worker_parent())["operation"]
    manager.cancel_waiting(op["pr"])
    with pytest.raises(ValueError, match="no longer awaiting approval"):
        manager.approve_review(op["pr"], "user")
    for _ in range(pw.SUBTASK_RATE - 2):
        manager.subtask(review(), worker_parent())
    with pytest.raises(ValueError, match="lineage started"):
        manager.subtask(review(), worker_parent())


def test_approved_review_waits_for_room_and_survives_restart_with_its_scope(review_worker):
    from test_waiting_launches import busy, reloaded

    manager, _, state, worker = review_worker
    (worker / ".claude").mkdir()
    register(manager.home, os="linux", labels=["bwrap"], max_agents=1)
    busy(state, 1)
    manager.loads.snapshot()
    settle(manager.loads)
    sha = git("rev-parse", "HEAD", cwd=worker / "src/repo")
    op = manager.subtask(review(base=sha), worker_parent())["operation"]
    # A restart before approval must not turn a request into a launch.
    manager = pw.Workspaces(manager.home, manager.overview, manager.jobs, src=manager.src)
    manager.run_waiting()
    assert manager.operation(op["pr"])["status"] == "approval"
    approved = manager.approve_review(op["pr"], "user")["operation"]
    assert approved["status"] == "waiting"
    busy(state, 0)
    manager = pw.Workspaces(manager.home, manager.overview, manager.jobs, src=manager.src)
    reloaded(manager)
    manager.loads.snapshot()
    settle(manager.loads)
    manager.run_waiting()
    done = finish(manager, op["pr"])
    assert done["status"] == "complete", done
    assert done["parent"]["review_approval"]["request"]["base"] == sha
    assert git("rev-parse", "HEAD", cwd=done["path"]) == sha
    manager.run_waiting()
    assert len(store(state)["machines"]["Mac mini"]["agents"]) == 1


@pytest.mark.parametrize(
    "failure",
    [
        "unavailable-account",
        "unauthenticated",
        "missing-commit",
        "account-label",
        "machine",
        "unsafe-backend",
    ],
)
def test_approved_review_refuses_unavailable_resources_without_substitution(
    review_worker,
    monkeypatch,
    failure,  # noqa: F811
):  # noqa: F811
    manager, _, state, worker = review_worker
    manager.loads.snapshot()
    settle(manager.loads)
    (worker / ".claude").mkdir()
    if failure == "unauthenticated":
        monkeypatch.setenv("FAKE_AGENT_AUTH", "0")
    request = review(base=git("rev-parse", "HEAD", cwd=worker / "src/repo"))
    if failure == "unavailable-account":
        register(manager.home, os="linux", labels=["bwrap", "claude-account:missing"])
        request["claude_account"] = "missing"
    elif failure == "account-label":
        request["claude_account"] = "missing"
    elif failure == "machine":
        request["machine"] = "unknown"
    elif failure == "missing-commit":
        request["base"] = "f" * 40
    elif failure == "unsafe-backend":
        register(manager.home, os="darwin", labels=["safehouse"])
    op = manager.subtask(request, worker_parent())["operation"]
    manager.approve_review(op["pr"], "user")
    done = finish(manager, op["pr"])
    assert done["status"] == "failed", done
    expected = {
        "unavailable-account": "Unknown Claude account",
        "unauthenticated": "not authenticated",
        "missing-commit": "not a commit in this worker's clone",
        "account-label": "claude-account:missing",
        "machine": "Unknown machine",
        "unsafe-backend": "require a Linux worker",
    }
    assert expected[failure] in done["message"]
    assert not store(state).get("machines", {}).get("Mac mini", {}).get("agents")


def test_broker_forwards_review_but_supplies_its_own_parent(tmp_path):
    # The broker proves session ownership by finding its transcript. It cannot
    # accept an authorization or a parent identity from the request body.
    config = tmp_path / ".codex"
    sessions = config / "sessions/2026/11/06"
    sessions.mkdir(parents=True)
    sid = parent()["session"]
    rollout = sessions / f"rollout-{sid}.jsonl"
    rollout.write_text(
        json.dumps({"type": "session_meta", "payload": {"id": sid, "cwd": str(tmp_path)}}) + "\n"
    )
    request = {"action": "launch", "launch": review(), "sessions": {"codex": sid, "claude": None}}
    action, forwarded = worker_broker.check(
        request, "codex", str(config), [str(tmp_path)], {"cwd": str(tmp_path), "docker": False}
    )
    assert action == "launch" and forwarded["launch"]["review"] is True
    assert forwarded["parent"]["agent"] == "codex" and forwarded["parent"]["config_dir"] == str(
        config
    )
    assert forwarded["parent"]["rollout"] == str(rollout)
    request.update(action="launch", approval=True)
    with pytest.raises(ValueError, match="exactly the launch fields"):
        worker_broker.check(
            request, "codex", str(config), [str(tmp_path)], {"cwd": str(tmp_path), "docker": False}
        )


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_provider_status_uses_only_selected_worker_configuration(tmp_path, monkeypatch, agent):
    monkeypatch.setenv("HOME", str(tmp_path))
    account = tmp_path / ".claude/accounts/oss"
    account.mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/unselected")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex-custom"))
    (tmp_path / ".codex-custom").mkdir()
    for name in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
        monkeypatch.setenv(name, "fake-unselected")
    monkeypatch.setattr(remote_probe.shutil, "which", lambda _: "/fake/provider")
    seen = []

    def status(args, **kwargs):
        seen.append((args, kwargs["env"]))
        return subprocess.CompletedProcess(args, 0, '{"loggedIn":false}', "")

    monkeypatch.setattr(remote_probe.subprocess, "run", status)
    if agent == "codex":
        # Stop before repository operations after checking the selected env.
        monkeypatch.setattr(
            remote_probe,
            "_host_clone",
            lambda *a, **k: (_ for _ in ()).throw(ValueError("end of status test")),
        )
    with pytest.raises(ValueError, match="not authenticated|end of status test"):
        remote_probe._host_wt_prepare(
            "/clone",
            "base/repo",
            "/root",
            "review",
            agent,
            "oss" if agent == "claude" else None,
            "bwrap",
            review=True,
        )
    [(args, env)] = seen
    assert args[1:] == (["auth", "status"] if agent == "claude" else ["login", "status"])
    if agent == "claude":
        assert env["CLAUDE_CONFIG_DIR"] == str(account)
        assert "ANTHROPIC_API_KEY" not in env and "CLAUDE_CODE_OAUTH_TOKEN" not in env
    else:
        assert env["CODEX_HOME"] == str(tmp_path / ".codex-custom")


@pytest.mark.parametrize("approver", ["admin", "session"])
def test_approval_endpoint_requires_authoritative_authentication_even_in_report_mode(
    local,  # noqa: F811
    tmp_path,
    approver,
):  # noqa: F811
    manager, *_ = local
    server = dashboard.DashboardServer(manager.home, 0, workspaces=manager)
    server.auth = dashboard_auth.Auth(tmp_path / "auth", "report", log=lambda _: None)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    from test_remote_registration import post

    op = manager.subtask(review(), worker_parent())["operation"]
    try:
        headers = {"X-Babysit-Action": "review-approve"}
        for kind in (None, "worker", "launch", "read"):
            auth = dict(headers)
            if kind:
                _, token = server.auth.store.create_key(kind)
                auth["Authorization"] = f"Bearer {token}"
            assert post(server, "/api/review-approve", {"id": op["pr"]}, auth)[0] == 403
        assert manager.operation(op["pr"])["status"] == "approval"
        _, narrow = server.auth.store.create_key("admin", scopes=["GET /api/*"])
        assert (
            post(
                server,
                "/api/review-approve",
                {"id": op["pr"]},
                {**headers, "Authorization": f"Bearer {narrow}"},
            )[0]
            == 403
        )
        if approver == "admin":
            _, token = server.auth.store.create_key("admin")
            headers["Authorization"] = f"Bearer {token}"
        else:
            token = server.auth.store.create_session("user-id", "test-user", "oidc")
            headers["Cookie"] = f"babysit_session={token}"
        assert (
            post(
                server,
                "/api/review-approve",
                {"id": op["pr"]},
                {**headers, "Origin": "https://untrusted.test"},
            )[0]
            == 403
        )
        # Even an admin cannot change the stored account/scope at approval time.
        assert (
            post(
                server, "/api/review-approve", {"id": op["pr"], "claude_account": "other"}, headers
            )[0]
            == 400
        )
        # No registered mini here: reaches placement and fails, proving authenticated
        # approval used the saved request rather than rejecting provider selection.
        status, answer = post(server, "/api/review-approve", {"id": op["pr"]}, headers)
        assert status == 200 and answer["operation"]["status"] == "failed"
        assert "Unknown machine mini" in answer["operation"]["message"]
        assert post(server, "/api/review-approve", {"id": op["pr"]}, headers)[0] == 400
        server.auth = None
        assert post(server, "/api/review-approve", {"id": op["pr"]}, headers)[0] == 403
    finally:
        server.shutdown()
        thread.join(5)
        server.server_close()


@pytest.mark.parametrize("agent", ["claude", "codex"])
@pytest.mark.parametrize("unavailable", ["executable", "configuration"])
def test_missing_review_provider_reports_the_actual_unavailable_resource(
    tmp_path, monkeypatch, agent, unavailable
):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
    monkeypatch.setattr(
        remote_probe.shutil,
        "which",
        lambda _: None if unavailable == "executable" else "/fake/provider",
    )
    expected = (
        "executable.*unavailable"
        if unavailable == "executable"
        else "no configured account directory"
    )
    with pytest.raises(ValueError, match=expected):
        remote_probe._host_wt_prepare(
            "/clone",
            "base/repo",
            "/root",
            "review",
            agent,
            "default" if agent == "claude" else None,
            "bwrap",
            review=True,
        )


@pytest.mark.parametrize("failure", ["timeout", "malformed-status"])
def test_unverified_authentication_is_reported_without_claiming_logout_or_exposing_output(
    tmp_path, monkeypatch, failure
):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".claude").mkdir()
    monkeypatch.setattr(remote_probe.shutil, "which", lambda _: "/fake/claude")

    def status(args, **kwargs):
        if failure == "timeout":
            raise subprocess.TimeoutExpired(args, 30, output="private fixture output")
        return subprocess.CompletedProcess(args, 0, "private fixture output", "")

    monkeypatch.setattr(remote_probe.subprocess, "run", status)
    with pytest.raises(ValueError, match="authentication could not be verified") as error:
        remote_probe._host_wt_prepare(
            "/clone", "base/repo", "/root", "review", "claude", "default", "bwrap", review=True
        )
    assert "private fixture output" not in str(error.value)
    assert "not authenticated" not in str(error.value)
