"""Temporary repositories and fake herdr/GitHub/agents; never touch live workspaces."""

import http.client
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import dashboard
import pr_workspaces as pw
import pytest
from pr_overview import Overview


@pytest.fixture
def local(tmp_path, monkeypatch):
    src = tmp_path / "src"
    src.mkdir()
    clone = src / "repo"
    clone.mkdir()

    def git(*args, cwd=clone):
        return subprocess.check_output(["git", "-C", str(cwd), *args], text=True).strip()

    git("init", "-b", "main")
    git("config", "user.name", "Fixture")
    git("config", "user.email", "fixture@example.test")
    git("commit", "--allow-empty", "-m", "base")
    git("branch", "feature")
    git("remote", "add", "origin", "https://github.com/base/repo.git")
    head = tmp_path / "head.git"
    git("clone", "--bare", str(clone), str(head))
    config = tmp_path / "gitconfig"
    config.write_text(f'[url "{head}"]\n\tinsteadOf = https://github.com/fork/repo.git\n')
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    state = tmp_path / "herdr.json"
    state.write_text(json.dumps({"workspaces": [], "agents": [], "calls": []}))
    monkeypatch.setenv("FAKE_HERDR", str(state))
    monkeypatch.setenv("FAKE_HEAD", str(head))
    monkeypatch.setenv(
        "WORKSPACE_HELPER", str(Path(__file__).resolve().parents[1] / "scripts/worktree.zsh")
    )
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

    def executable(name, body):
        p = bin_dir / name
        p.write_text(f"#!{sys.executable}\n" + body)
        p.chmod(0o700)

    executable(
        "gh",
        """import os,sys,subprocess
if sys.argv[1:3] == ['repo','clone']:
 subprocess.check_call(['git','clone',os.environ['FAKE_HEAD'],sys.argv[4]])
 subprocess.check_call(['git','-C',sys.argv[4],'remote','set-url','origin','https://github.com/base/repo.git'])
else:
 print('fork\\trepo\\tfeature')
""",
    )
    executable(
        "herdr",
        """import json,os,sys,subprocess
from pathlib import Path
p=Path(os.environ['FAKE_HERDR']); data=json.loads(p.read_text()); a=sys.argv[1:]
data['calls'].append(a)
result={}
if a[:2] == ['workspace','list']: result={'workspaces':data['workspaces']}
elif a[:2] == ['agent','list']: result={'agents':data['agents']}
elif a[:2] == ['worktree','open']:
 path=a[a.index('--path')+1]; root=a[a.index('--cwd')+1]
 existing=next((w for w in data['workspaces'] if w['worktree']['checkout_path']==path),None)
 if existing: wid=existing['workspace_id']
 else:
  wid='w'+str(len(data['workspaces'])+1)
  data['workspaces'].append({'workspace_id':wid,'label':a[a.index('--label')+1] if '--label' in a else 'Reopened','agent_status':'unknown','worktree':{'repo_root':root,'checkout_path':path}})
 result={'already_open':bool(existing),'root_pane':{'pane_id':wid+':p1'}}
elif a[:2] == ['pane','run']:
 w=next(w for w in data['workspaces'] if a[2].startswith(w['workspace_id']+':'))
 p.write_text(json.dumps(data))
 env={**os.environ,'FAKE_WORKSPACE':w['workspace_id']}
 subprocess.check_call(['/bin/zsh','-fc',a[3]],cwd=w['worktree']['checkout_path'],env=env)
 sys.exit(0)
p.write_text(json.dumps(data)); print(json.dumps({'result':result}))
""",
    )
    for agent in ["codex", "claude"]:
        executable(
            agent,
            """import json,os,sys
from pathlib import Path
p=Path(os.environ['FAKE_HERDR']); data=json.loads(p.read_text())
data['agents'].append({'workspace_id':os.environ['FAKE_WORKSPACE'],'agent':Path(sys.argv[0]).name,'cwd':os.getcwd(),'task':sys.argv[1]})
p.write_text(json.dumps(data))
""",
        )
    executable(
        "zsh",
        """import os,sys
assert sys.argv[1]=='-lic'
assert sys.argv[2]=='export WT_MULTIPLEXER=herdr; wtpr "$@"'
os.execv('/bin/zsh',['zsh','-fc','source "$WORKSPACE_HELPER"; export WT_MULTIPLEXER=herdr; wtpr "$@"','fixture',*sys.argv[4:]])
""",
    )
    overview = Overview(tmp_path / "state")
    overview.next_poll = float("inf")
    pr = {
        "id": "PR_one",
        "repo": "base/repo",
        "number": 7,
        "url": "https://github.com/base/repo/pull/7",
        "head_repo": "fork/repo",
        "head_branch": "feature",
        "head_sha": git("rev-parse", "HEAD"),
    }
    overview.value["prs"] = [pr]
    jobs = []
    manager = pw.Workspaces(tmp_path / "state", overview, lambda: jobs, src)
    return manager, pr, git, state, jobs


def finish(manager, key):
    worker = manager.workers.get(key)
    if worker:
        worker.join(20)
        assert not worker.is_alive()
    return manager.operation(key)


def make_checkout(local, branch="feature", name="checkout"):
    manager, pr, git, state, _ = local
    path = manager.src / "worktrees" / name
    git("worktree", "add", str(path), branch)
    return path


def space(state, path, clone, wid="w99", name="Renamed pane"):
    data = json.loads(state.read_text())
    data["workspaces"].append(
        {
            "workspace_id": wid,
            "label": name,
            "agent_status": "working",
            "worktree": {"checkout_path": str(path), "repo_root": str(clone)},
        }
    )
    state.write_text(json.dumps(data))


def test_fork_branch_is_only_a_suggestion_without_provenance(local):
    manager, pr, git, _, _ = local
    path = make_checkout(local)
    info = manager.describe(pr, manager.scan())
    assert not info["matches"] and str(path) in info["suggestions"]
    git("remote", "add", "fork", "git@github.com:fork/repo.git")
    matches, _ = manager.matches(pr, manager.scan())
    assert matches[0]["path"] == str(path)


def test_watch_binding_dirty_unpushed_renamed_and_ambiguous_workspaces(local):
    manager, pr, git, state, jobs = local
    path = make_checkout(local)
    jobs.append(
        {"url": pr["url"], "cwd": str(path), "snapshot": {"pr": {"head_branch": "feature"}}}
    )
    git("commit", "--allow-empty", "-m", "unpushed", cwd=path)
    (path / "dirty").write_text("local changes")
    space(state, path, manager.src / "repo")
    space(state, path, manager.src / "repo", "w100", "Second workspace")
    matches, _ = manager.matches(pr, manager.scan())
    assert len(matches) == 2 and matches[0]["name"] == "Renamed pane"
    before = json.loads(state.read_text())["agents"]
    result = manager.action(
        {
            "id": pr["id"],
            "action": "open",
            "path": str(path),
            "workspace_id": "w99",
            "task": "must not submit",
        }
    )
    assert result["result"]["url"].endswith("/space/w99")
    assert json.loads(state.read_text())["agents"] == before
    assert not any(
        c[:2] in [["pane", "run"], ["workspace", "focus"]]
        for c in json.loads(state.read_text())["calls"]
    )
    jobs.clear()
    restarted = pw.Workspaces(manager.home, manager.overview, lambda: [], manager.src)
    assert len(restarted.matches(pr, restarted.scan())[0]) == 2
    git("switch", "-c", "unrelated", cwd=path)
    assert not restarted.matches(pr, restarted.scan())[0]


def test_reopen_preserves_checkout_without_agent(local):
    manager, pr, git, state, jobs = local
    path = make_checkout(local)
    jobs.append(
        {"url": pr["url"], "cwd": str(path), "snapshot": {"pr": {"head_branch": "feature"}}}
    )
    op = manager.action({"id": pr["id"], "action": "reopen", "path": str(path)})["operation"]
    result = finish(manager, pr["id"])
    assert result["status"] == "complete", result
    assert result["id"] == op["id"]
    data = json.loads(state.read_text())
    assert not data["agents"]
    assert "--no-focus" in next(c for c in data["calls"] if c[:2] == ["worktree", "open"])


@pytest.mark.parametrize("agent", ["codex", "claude"])
def test_creation_selected_agent_task_quoting_no_focus_and_duplicate(local, agent):
    manager, pr, _, state, _ = local
    task = "Fix quotes ' \" $() `touch SHOULD_NOT_EXIST`\nsecond line; echo no"
    request = {"id": pr["id"], "action": "create", "agent": agent, "task": task}
    first = manager.action(request)["operation"]
    second = manager.action(request)["operation"]
    assert first["id"] == second["id"]
    op = finish(manager, pr["id"])
    assert op["status"] == "complete", op
    data = json.loads(state.read_text())
    assert len(data["agents"]) == 1
    assert data["agents"][0]["agent"] == agent
    assert data["agents"][0]["task"] == task + "\n\nPull request: " + pr["url"]
    assert not (Path(op["path"]) / "SHOULD_NOT_EXIST").exists()
    assert Path(op["path"]).name == "pr-base-7"
    assert "--no-focus" in next(c for c in data["calls"] if c[:2] == ["worktree", "open"])
    assert not any("--focus" in c or c[:2] == ["workspace", "focus"] for c in data["calls"])
    prompt = manager.home / "workspace-prompts" / op["id"]
    assert prompt.stat().st_mode & 0o777 == 0o600
    assert manager.path.stat().st_mode & 0o777 == 0o600


def test_collision_allocates_suffix_and_never_reuses_directory(local):
    manager, pr, _, _, _ = local
    collision = manager.src / "worktrees/repo/pr-base-7"
    collision.mkdir(parents=True)
    (collision / "keep").write_text("unrelated")
    manager.action({"id": pr["id"], "action": "create", "task": "Fix"})
    op = finish(manager, pr["id"])
    assert op["status"] == "complete", op
    assert Path(op["path"]).name == "pr-base-7-2"
    assert (collision / "keep").read_text() == "unrelated"


def test_clone_destination_conflicts_and_clone_create(local):
    manager, pr, git, _, _ = local
    git("remote", "set-url", "origin", "https://github.com/unrelated/repo.git")
    destination = str(manager.src / "base--repo")
    assert manager.destination(pr) == destination
    with pytest.raises(ValueError, match="destination changed"):
        manager.action(
            {
                "id": pr["id"],
                "action": "clone-and-create",
                "destination": str(manager.src / "repo"),
                "task": "Fix",
            }
        )
    manager.action(
        {"id": pr["id"], "action": "clone-and-create", "destination": destination, "task": "Fix"}
    )
    op = finish(manager, pr["id"])
    assert op["status"] == "complete", op
    assert manager.destination(pr) is None


def test_fetch_failure_can_retry_without_launch(local):
    manager, pr, _, state, _ = local
    head = Path(os.environ["FAKE_HEAD"])
    head.rename(head.with_suffix(".moved"))
    request = {"id": pr["id"], "action": "create", "task": "Fix"}
    manager.action(request)
    op = finish(manager, pr["id"])
    assert op["status"] == "failed", op
    assert not json.loads(state.read_text())["agents"]
    assert manager.action(request)["operation"]["id"] == op["id"]
    head.with_suffix(".moved").rename(head)
    request["retry"] = True
    manager.action(request)
    assert finish(manager, pr["id"])["status"] == "complete"


def test_uncertain_restart_never_resubmits(local):
    manager, pr, _, state, _ = local
    op = {
        "id": "interrupted",
        "pr": pr["id"],
        "status": "running",
        "agent": "codex",
        "path": "/missing",
        "message": "launching",
        "log": "",
    }
    manager.save_operation(op)
    info = manager.describe(pr, manager.scan())
    assert info["operation"]["status"] == "uncertain"
    assert (
        manager.action({"id": pr["id"], "action": "create", "task": "Retry", "retry": True})[
            "operation"
        ]["id"]
        == "interrupted"
    )
    assert not json.loads(state.read_text())["agents"]


def test_completed_launch_recovered_after_restart(local):
    manager, pr, _, _, _ = local
    manager.action({"id": pr["id"], "action": "create", "task": "Fix"})
    op = finish(manager, pr["id"])
    manager.save_operation(op, status="running")
    restarted = pw.Workspaces(manager.home, manager.overview, lambda: [], manager.src)
    assert restarted.describe(pr, restarted.scan())["operation"]["status"] == "complete"


def test_focus_and_copy_are_explicit_and_revalidated(local):
    manager, pr, _, state, jobs = local
    path = make_checkout(local)
    jobs.append(
        {"url": pr["url"], "cwd": str(path), "snapshot": {"pr": {"head_branch": "feature"}}}
    )
    space(state, path, manager.src / "repo")
    request = {"id": pr["id"], "path": str(path), "workspace_id": "w99"}
    assert manager.action({**request, "action": "copy"})["command"] == "herdr workspace focus w99"
    manager.action({**request, "action": "focus"})
    assert ["workspace", "focus", "w99"] in json.loads(state.read_text())["calls"]
    data = json.loads(state.read_text())
    data["workspaces"] = []
    state.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="Workspace changed"):
        manager.action({**request, "action": "focus"})


@pytest.mark.parametrize(
    "extra",
    [
        {"command": "touch /tmp/no"},
        {"agent": "sh"},
        {"task": " "},
        {"clone": "/tmp/arbitrary"},
        {"agent": []},
    ],
)
def test_reject_arbitrary_parameters(local, extra):
    manager, pr, _, _, _ = local
    with pytest.raises(ValueError):
        manager.action({"id": pr["id"], "action": "create", "task": "Fix", **extra})


def test_multiple_clones_require_choice_and_remember_it(local):
    manager, pr, git, _, _ = local
    other = manager.src / "second"
    git("clone", str(manager.src / "repo"), str(other))
    git("remote", "set-url", "origin", "https://github.com/base/repo.git", cwd=other)
    with pytest.raises(ValueError, match="Choose"):
        manager.action({"id": pr["id"], "action": "create", "task": "Fix"})
    manager.action({"id": pr["id"], "action": "create", "clone": str(other), "task": "Fix"})
    assert finish(manager, pr["id"])["status"] == "complete"
    assert manager.describe(pr, manager.scan())["preferred_clone"] == str(other)


def test_workspace_http_protections_and_read_only_discovery(local):
    manager, pr, _, state, _ = local
    with dashboard.DashboardServer(
        manager.home, 0, overview=manager.overview, workspaces=manager
    ) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:

            def request(method="POST", headers=None, body=None):
                conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=15)
                conn.request(
                    method,
                    "/api/workspace-action" if method == "POST" else "/api/workspaces",
                    json.dumps(body or {"id": pr["id"], "action": "create", "task": "Fix"})
                    if method == "POST"
                    else None,
                    {
                        "Content-Type": "application/json",
                        "X-Babysit-Action": "workspace-action",
                        **(headers or {}),
                    },
                )
                response = conn.getresponse()
                result = response.status, json.loads(response.read())
                conn.close()
                return result

            for headers in [
                {"Host": "evil.test"},
                {"Origin": "https://evil.test"},
                {"X-Babysit-Action": ""},
                {"Sec-Fetch-Site": "cross-site"},
            ]:
                assert request(headers=headers)[0] == 403
            assert request(body={"id": "missing", "action": "create", "task": "Fix"})[0] == 400
            assert request(method="GET")[0] == 200
            assert not json.loads(state.read_text())["agents"]
        finally:
            server.shutdown()
            thread.join()


def test_reopen_after_completed_creation_does_not_start_second_agent(local):
    manager, pr, _, state, _ = local
    manager.action({"id": pr["id"], "action": "create", "task": "Fix"})
    op = finish(manager, pr["id"])
    assert op["status"] == "complete"
    data = json.loads(state.read_text())
    data["workspaces"] = []
    state.write_text(json.dumps(data))
    manager.action({"id": pr["id"], "action": "reopen", "path": op["path"]})
    reopened = finish(manager, pr["id"])
    assert reopened["status"] == "complete", reopened
    assert reopened["id"] != op["id"]
    assert len(json.loads(state.read_text())["agents"]) == 1
    with manager.db() as db:
        assert db.execute("SELECT count(*) FROM operation_history").fetchone()[0] == 2


def test_recover_reservation_before_association_was_saved(local):
    manager, pr, _, _, _ = local
    manager.action({"id": pr["id"], "action": "create", "task": "Fix"})
    op = finish(manager, pr["id"])
    assert op["status"] == "complete"
    manager.save_operation(op, status="running")
    with manager.db() as db:
        db.execute("DELETE FROM associations")
    restarted = pw.Workspaces(manager.home, manager.overview, lambda: [], manager.src)
    assert restarted.describe(pr, restarted.scan())["operation"]["status"] == "complete"
