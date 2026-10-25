"""Watches on a worker machine: registered there, forwarded to the controller's dashboard.

The worker is a directory under ``<tmp>/hosts/<ssh target>`` that the fake ssh uses as
HOME; the controller reads its checkout and session store there through the probe.
"""

import http.client
import http.server
import json
import os
import subprocess
import sys
import threading
import uuid
from pathlib import Path

import dashboard
import pr_supervisor as supervisor
import pytest
import remote_probe
import worker_api
from test_remote_cron import git, rollout, short_tmp  # noqa: F401 (fixture)

WORKER = {
    "id": "box",
    "label": "Box",
    "ssh": "box.test",
    "os": "linux",
    "python": sys.executable,
    "babysit_root": "~/.local/share/babysit-pr/app",
}
URL = "https://github.com/base/repo/pull/7"


def registry(home, **changes):
    home.mkdir(parents=True, exist_ok=True)
    (home / "machines.json").write_text(
        json.dumps({"version": 1, "machines": [{**WORKER, **changes}]})
    )


def claude_transcript(home, cwd, sid, account=None, **changes):
    config = home / ".claude" / ("accounts/" + account if account else "")
    folder = config / "projects" / str(cwd).replace("/", "-")
    folder.mkdir(parents=True, exist_ok=True)
    item = {
        "type": "assistant",
        "sessionId": sid,
        "cwd": str(cwd),
        "message": {"model": "claude-fixture"},
        **changes,
    }
    path = folder / f"{sid}.jsonl"
    path.write_text(json.dumps(item) + "\n")
    return path


@pytest.fixture
def box(tmp_path, short_tmp, fake_tools, monkeypatch):  # noqa: F811
    """The controller's home and the worker's: a clone with a linked worktree and a session."""
    config = tmp_path / "gitconfig"
    config.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("BABYSIT_WORKER", raising=False)
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    home = tmp_path / "controller"
    registry(home)
    worker = tmp_path / "hosts" / "box.test"
    clone = worker / "src" / "repo"
    clone.mkdir(parents=True)
    git("init", "-q", "-b", "main", cwd=clone)
    git("commit", "-q", "--allow-empty", "-m", "base", cwd=clone)
    tree = worker / "src" / "worktrees" / "repo" / "feature"
    git("worktree", "add", "-q", "-b", "feature", str(tree), cwd=clone)
    git("commit", "-q", "--allow-empty", "-m", "change", cwd=tree)
    sha = git("rev-parse", "HEAD", cwd=tree)
    sid = str(uuid.uuid4())
    rollout(
        worker,
        tree.resolve(),
        sid,
        {
            "type": "turn_context",
            "payload": {"model": "gpt-fixture", "sandbox_policy": {"type": "workspace-write"}},
        },
    )
    monkeypatch.setenv(
        "FAKE_GH_PR",
        json.dumps(
            {
                "number": 7,
                "url": URL,
                "state": "OPEN",
                "headRefName": "feature",
                "headRefOid": sha,
                "baseRefOid": "b" * 40,
                "headRepository": {"name": "repo"},
                "headRepositoryOwner": {"login": "base"},
            }
        ),
    )
    return {"home": home, "worker": worker, "tree": tree.resolve(), "sha": sha, "sid": sid}


def args(box, **changes):
    values = dict(
        home=box["home"],
        machine="box",
        pr=URL,
        repo=None,
        ci_repo=None,
        branch=None,
        cwd=str(box["tree"]),
        session=box["sid"],
        agent="codex",
        claude_account=None,
        pane=None,
        headless=True,
        rollout=None,
        instructions="Fix CI only",
        on_green=None,
        codex_command=None,
        claude_command=None,
        poll_seconds=120,
        max_repairs=3,
        repair_timeout=600,
    )
    return supervisor.argparse.Namespace(**{**values, **changes})


def register(box, **changes):
    db = supervisor.open_db(box["home"])
    try:
        return supervisor.register(db, args(box, **changes))
    finally:
        db.close()


def test_a_workers_checkout_is_read_there(box):
    tree = box["tree"]
    state = remote_probe.validate_worktree(remote_probe._host_worktree(str(tree)))
    assert state == {
        "path": str(tree),
        "branch": "feature",
        "head": box["sha"],
        "dirty": False,
        "common": str((box["worker"] / "src/repo/.git").resolve()),
    }
    (tree / "new").write_text("x")
    assert remote_probe._host_worktree(str(tree))["dirty"] is True
    main = remote_probe._host_worktree(str(box["worker"] / "src/repo"))
    assert main["common"] is None and main["branch"] == "main"
    (box["tree"] / "sub").mkdir()
    with pytest.raises(ValueError, match="top of a Git checkout"):
        remote_probe._host_worktree(str(box["tree"] / "sub"))
    for bad in ({**state, "path": "/a\x1bb"}, {**state, "extra": 1}, {**state, "head": "x"}):
        with pytest.raises(ValueError, match="Malformed"):
            remote_probe.validate_worktree(bad)


def test_a_workers_codex_session_is_checked_there(box, monkeypatch):
    monkeypatch.setenv("HOME", str(box["worker"]))
    tree, sid = str(box["tree"]), box["sid"]
    info = remote_probe.validate_session_info(
        remote_probe._host_session_info("codex", sid, tree, None, None)
    )
    assert info["session_id"] == sid and info["sandbox"] == "workspace-write"
    assert info["model"] == "gpt-fixture"
    assert info["rollout"].startswith(str(box["worker"].resolve() / ".codex" / "sessions"))
    with pytest.raises(ValueError, match="cwd does not match"):
        remote_probe._host_session_info("codex", sid, str(box["worker"]), None, None)
    with pytest.raises(ValueError, match="exactly one"):
        remote_probe._host_session_info("codex", str(uuid.uuid4()), tree, None, None)
    # A file outside the session stores is never read, whatever it holds.
    stray = box["worker"] / f"stray-{sid}.jsonl"
    stray.write_text(Path(info["rollout"]).read_text())
    with pytest.raises(ValueError, match="Not a session transcript"):
        remote_probe._host_session_info("codex", sid, tree, str(stray), None)
    with pytest.raises(ValueError, match="only be selected for Claude"):
        remote_probe._host_session_info("codex", sid, tree, None, "oss")


def test_a_workers_claude_session_records_its_account_there(box, monkeypatch):
    worker = box["worker"]
    monkeypatch.setenv("HOME", str(worker))
    tree, sid = box["tree"], str(uuid.uuid4())
    path = claude_transcript(worker, tree, sid, "oss", permissionMode="plan")
    info = remote_probe.validate_session_info(
        remote_probe._host_session_info("claude", sid, str(tree), None, None)
    )
    account = (worker / ".claude/accounts/oss").resolve()
    assert info == {
        "session_id": sid,
        "rollout": str(path.resolve()),
        "model": "claude-fixture",
        "claude_permission_mode": "plan",
        "claude_config_dir": str(account),
        # The literal path a launch types there.
        "claude_config_env": str(worker / ".claude/accounts/oss"),
        "claude_account": "oss",
        "claude_subscription": True,
    }
    assert remote_probe._host_session_info("claude", sid, str(tree), None, "oss") == info
    (worker / ".claude/accounts/psu").mkdir()
    with pytest.raises(ValueError, match="could not identify"):
        remote_probe._host_session_info("claude", sid, str(tree), None, "psu")
    with pytest.raises(ValueError, match="different account"):
        remote_probe._host_session_info("claude", sid, str(tree), str(path), "psu")
    with pytest.raises(ValueError, match="Unknown Claude account"):
        remote_probe._host_session_info("claude", sid, str(tree), None, "nope")
    with pytest.raises(ValueError, match="Malformed"):
        remote_probe.validate_session_info({**info, "model": "a\nb"})


def test_a_watch_on_a_worker_is_recorded_from_what_the_worker_shows(box, fake_tools):
    job = register(box)
    worker, tree = box["worker"], str(box["tree"])
    assert (job["machine"], job["cwd"], job["session_id"]) == ("box", tree, box["sid"])
    assert job["rollout"].startswith(str(worker.resolve()))
    assert job["git_common_dir"] == str((worker / "src/repo/.git").resolve())
    assert (job["sandbox"], job["model"], job["status"]) == (
        "workspace-write",
        "gpt-fixture",
        "awaiting_release",
    )
    # Its own babysit-bwrap there, whatever this machine's launcher is.
    assert job["codex_command"] == [
        "~/.local/share/babysit-pr/app/scripts/babysit-bwrap",
        "--env-pass=" + ",".join(supervisor.sandbox.REPAIR_ENV),
        "--enable=playwright",
        "--",
        "codex",
    ]
    commands = [c["command"] for c in json.loads(fake_tools.read_text())["ssh"]]
    assert len(commands) == 2  # The checkout, then the session.
    # The same path on two machines is two checkouts; on one machine it is one.
    with pytest.raises(ValueError, match="already owns"):
        register(box)
    registry(box["home"], labels=["gpg"])
    db = supervisor.open_db(box["home"])
    with db:
        db.execute("DELETE FROM jobs")
    db.close()
    job = register(box, agent="codex")
    assert "--enable=playwright,gpg" in job["codex_command"]


def test_a_workers_claude_watch_runs_its_sandbox_launcher(box, monkeypatch):
    sid = str(uuid.uuid4())
    claude_transcript(box["worker"], box["tree"], sid, "oss")
    job = register(box, agent="claude", session=sid, claude_account="oss")
    assert job["claude_command"][-2:] == ["claude", "--dangerously-skip-permissions"]
    assert job["claude_permission_mode"] == supervisor.sandbox.LAUNCHER_PERMISSIONS
    assert job["claude_account"] == "oss" and job["claude_subscription"] is True


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"pr": "auto"}, "names its PR"),
        ({"pr": "7"}, "names its PR"),
        ({"codex_command": '["sh"]'}, "its own sandbox launcher"),
        ({"session": None}, "exact --session"),
        ({"machine": "nope"}, "Unknown machine nope"),
        ({"cwd": str(Path("/nonexistent"))}, "probe failed"),
    ],
)
def test_worker_registrations_are_refused_without_what_the_controller_checks(box, changes, message):
    with pytest.raises(ValueError, match=message):
        register(box, **changes)
    assert supervisor.jobs(supervisor.open_db(box["home"])) == []


def test_a_worker_watch_is_refused_on_a_mac_or_disabled_worker(box):
    registry(box["home"], os="darwin")
    with pytest.raises(ValueError, match="need a Linux worker"):
        register(box)
    registry(box["home"], enabled=False)
    with pytest.raises(ValueError, match="disabled"):
        register(box)


def test_a_dirty_or_moved_worker_checkout_is_refused(box):
    (box["tree"] / "new").write_text("x")
    with pytest.raises(RuntimeError, match="uncommitted changes"):
        register(box)
    state = supervisor.checkout_state(box["home"], "box", str(box["tree"]))
    pr = {"head_branch": "feature", "head_sha": box["sha"]}
    job = {"cwd": "/elsewhere", "machine": "box"}
    with pytest.raises(RuntimeError, match="no longer at the registered path"):
        supervisor.verify_worktree(job, pr, box["home"], state)


def test_a_worker_watch_bound_to_a_pane_is_not_repaired_yet(box):
    job = register(box)
    db = supervisor.open_db(box["home"])
    try:
        job.update(status="watching", attempts=0, pane={"pane_id": "w1:p1"})
        supervisor.start_repair(db, box["home"], job)
        assert job["status"] == "blocked" and "Pane repairs on box" in job["summary"]
        assert not (box["home"] / "runs").exists()
    finally:
        db.close()


@pytest.fixture
def served(box):
    server = dashboard.DashboardServer(box["home"], 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    thread.join(5)
    server.server_close()


def post(server, path, body, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=60)
    data = body if isinstance(body, bytes) else json.dumps(body).encode()
    try:
        conn.request(
            "POST",
            path,
            data,
            {
                "Host": f"127.0.0.1:{server.server_port}",
                "Content-Type": "application/json",
                **(headers or {}),
            },
        )
        response = conn.getresponse()
        return response.status, json.loads(response.read())
    finally:
        conn.close()


def request_body(box, **changes):
    body = {key: getattr(args(box), key) for key in worker_api.REGISTER}
    return {**body, **changes}


def test_the_dashboard_registers_a_workers_watch_on_the_machine_its_token_names(box, served):
    token = worker_api.issue(box["home"], "box")
    stored = (box["home"] / worker_api.TOKENS).read_text()
    assert token not in stored and (box["home"] / worker_api.TOKENS).stat().st_mode & 0o777 == 0o600
    auth = {"Authorization": f"Bearer {token}"}
    assert post(served, "/api/worker/register", request_body(box))[0] == 403
    assert (
        post(
            served,
            "/api/worker/register",
            request_body(box),
            {"Authorization": "Bearer " + "x" * 43},
        )[0]
        == 403
    )
    # Never from a browser, even with the token.
    browser = {**auth, "Origin": f"http://127.0.0.1:{served.server_port}"}
    assert post(served, "/api/worker/register", request_body(box), browser)[0] == 403
    assert (
        post(served, "/api/worker/register", request_body(box), {**auth, "Host": "evil.test"})[0]
        == 403
    )
    assert post(served, "/api/worker/frobnicate", request_body(box), auth)[0] == 403
    # The token decides the machine: a request naming one is refused.
    status, answer = post(
        served, "/api/worker/register", {**request_body(box), "machine": "x"}, auth
    )
    assert status == 400 and "registration fields" in answer["error"]
    status, answer = post(served, "/api/worker/register", request_body(box, pr="auto"), auth)
    assert status == 400 and "Invalid PR" in answer["error"]
    status, answer = post(served, "/api/worker/register", request_body(box, cwd="~/x"), auth)
    assert status == 400 and "absolute" in answer["error"]
    status, answer = post(
        served, "/api/worker/register", request_body(box, instructions="a\x1b[2Jb"), auth
    )
    assert status == 400 and "control" in answer["error"]
    status, answer = post(served, "/api/worker/register", request_body(box), auth)
    assert status == 200, answer
    assert (answer["machine"], answer["status"], answer["session"]) == (
        "box",
        "awaiting_release",
        box["sid"],
    )
    [job] = supervisor.jobs(supervisor.open_db(box["home"]))
    assert job["id"] == answer["id"] and job["cwd"] == str(box["tree"])
    worker_api.revoke(box["home"], "box")
    assert post(served, "/api/worker/register", request_body(box), auth)[0] == 403


def worker_cli(box, *argv, **env):
    return subprocess.run(
        [sys.executable, str(supervisor.SCRIPT), *argv],
        env={**os.environ, "HOME": str(box["worker"]), **env},
        cwd=box["tree"],
        text=True,
        capture_output=True,
        timeout=120,
    )


def provision(box, url, token):
    marker = box["worker"] / ".config/babysit-pr/worker.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"version": 1, "controller": url, "token": token}))


def test_register_on_a_worker_is_forwarded_to_the_controller(box, served):
    command = ["register", "--headless", "--session", box["sid"], "--instructions", "Fix CI"]
    marker = box["worker"] / ".config/babysit-pr/worker.json"
    marker.parent.mkdir(parents=True)
    marker.write_text(json.dumps({"version": 1}))
    refused = worker_cli(box, *command)
    assert refused.returncode == 1 and "controller's URL and token" in refused.stdout
    provision(box, f"http://127.0.0.1:{served.server_port}", worker_api.issue(box["home"], "box"))
    # --pr auto: the worker's own gh names the PR from its checkout.
    done = worker_cli(box, *command)
    assert done.returncode == 0, done.stdout + done.stderr
    answer = json.loads(done.stdout)
    assert answer["machine"] == "box" and answer["release_argv"][-2:] == ["release", answer["id"]]
    [job] = supervisor.jobs(supervisor.open_db(box["home"]))
    assert (job["machine"], job["cwd"], job["url"]) == ("box", str(box["tree"]), URL)
    # Nothing is kept on the worker.
    assert not (box["worker"] / ".local/state/babysit-pr").exists()
    again = worker_cli(box, *command, "--pr", URL)
    assert again.returncode == 1 and "already owns" in json.loads(again.stdout)["error"]
    machine = worker_cli(box, *command, "--machine", "box")
    assert "drop --machine" in json.loads(machine.stdout)["error"]


def test_a_worker_never_follows_a_redirect_with_its_token(box, monkeypatch):
    seen = []

    class Redirect(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            seen.append((self.path, self.headers.get("Authorization")))
            self.send_response(307)
            self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/stolen")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setenv("HOME", str(box["worker"]))
        provision(box, f"http://127.0.0.1:{server.server_port}", "t" * 43)
        with pytest.raises(ValueError, match="HTTP 307"):
            worker_api.forward("register", {})
        assert [path for path, _ in seen] == ["/api/worker/register"]
    finally:
        server.shutdown()
        thread.join(5)
        server.server_close()


def test_worker_tokens_are_printed_only_into_a_pipe(box):
    done = subprocess.run(
        [sys.executable, str(supervisor.SCRIPT), "--home", str(box["home"]), "worker-token", "box"],
        text=True,
        capture_output=True,
    )
    assert done.returncode == 0, done.stdout
    token = done.stdout.strip()
    assert worker_api.authenticate(box["home"], f"Bearer {token}").id == "box"
    unknown = subprocess.run(
        [sys.executable, str(supervisor.SCRIPT), "--home", str(box["home"]), "worker-token", "x"],
        text=True,
        capture_output=True,
    )
    assert unknown.returncode == 1 and "Unknown machine x" in unknown.stdout

    class Terminal:
        def isatty(self):
            return True

    with pytest.raises(ValueError, match="not printed to a terminal"):
        worker_api.print_token(token, Terminal())


def test_a_worker_watch_binds_its_pane_through_that_machines_herdr(box, fake_tools):
    store = json.loads(fake_tools.read_text())
    agent = {"pane_id": "w1:p1", "agent": "codex", "cwd": str(box["tree"])}
    store["machines"] = {"Box": {"workspaces": [], "agents": [agent], "calls": []}}
    fake_tools.write_text(json.dumps(store))
    job = register(box, pane="w1:p1", headless=False)
    assert job["pane"] == {"pane_id": "w1:p1", "terminal_id": "terminal", "shell_pid": 1}
    calls = json.loads(fake_tools.read_text())["calls"]
    assert calls and all(call[0] == "--machine=Box" for call in calls)
    # The worker's path is compared as it is; this machine's same-named path is no match.
    agent["cwd"] = str(box["tree"]) + "/"
    store["machines"]["Box"]["agents"] = [agent]
    fake_tools.write_text(json.dumps(store))
    db = supervisor.open_db(box["home"])
    with db:
        db.execute("DELETE FROM jobs")
    db.close()
    with pytest.raises(RuntimeError, match="Pane worktree does not match"):
        register(box, pane="w1:p1", headless=False)
