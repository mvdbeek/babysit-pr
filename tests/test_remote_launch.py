"""Dashboard launches on a worker machine: its clone, its wt, its herdr, over fake ssh.

The worker is a directory under ``<tmp>/hosts/<ssh target>`` that the fake ssh uses as
HOME. Its babysit-pr is this checkout, so the wt it runs is the one under test; herdr
run there reaches the fake herdr's store for the worker's label.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import attachments
import exit_watch
import machines
import pr_workspaces as pw
import pytest
import remote_probe
import workspace_exit
from test_new_tasks import other_clone
from test_new_tasks import request as new_request
from test_pr_workspaces import finish, helper_calls, local  # noqa: F401 (fixture)
from test_remote_cron import bwrap, git, host, rollout, short_tmp  # noqa: F401 (fixtures)

APP = Path(pw.__file__).resolve().parent.parent
URL = "https://github.com/base/repo.git"
WORKER = {
    "id": "mini",
    "label": "Mac mini",
    "ssh": "mini.test",
    "os": "darwin",
    "labels": ["safehouse"],
    "python": sys.executable,
    "babysit_root": str(APP),
}


def register(home, **changes):
    (home / "machines.json").write_text(
        json.dumps({"version": 1, "machines": [{**WORKER, **changes}]})
    )


@pytest.fixture
def remote(local, short_tmp, monkeypatch):  # noqa: F811
    """The dashboard's workspaces, and the worker Mac mini with its own clone of base/repo."""
    manager, pr, _, state, _ = local
    register(manager.home)
    worker = state.parent / "hosts" / "mini.test"
    clone = worker / "src" / "repo"
    subprocess.check_call(["git", "clone", "-q", os.environ["FAKE_HEAD"], str(clone)])
    git("remote", "set-url", "origin", URL, cwd=clone)
    # The fake ssh passes this environment through: its Codex home must be the worker's.
    shutil.copytree(os.environ["CODEX_HOME"], worker / ".codex")
    monkeypatch.setenv("CODEX_HOME", str(worker / ".codex"))
    monkeypatch.setenv("FAKE_HOST_LABELS", json.dumps({"mini.test": "Mac mini"}))
    monkeypatch.setattr(pw, "REMOTE_WATCH", 0)
    return manager, pr, state, worker


def clone_path(worker):
    return str((worker / "src" / "repo").resolve())


def store(state):
    return json.loads(Path(state).read_text())


def remote_commands(state):
    """The command lines the fake ssh ran, decoded from their octal escapes."""
    found = []
    for call in store(state).get("ssh", []):
        escaped = re.search(r'printf "([^"]*)"', call["command"])[1]
        found.append(bytes(int(code, 8) for code in escaped.split("\\")[1:]).decode())
    return found


def watches(home):
    with exit_watch.connect(home) as db:
        return [json.loads(data) for (data,) in db.execute("SELECT data FROM exit_watches")]


def looked(manager):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        with manager.lock:
            if not manager.remote_looks:
                return
        time.sleep(0.02)
    raise AssertionError("The look at the worker's agent did not finish")


def test_a_review_on_a_worker_starts_there(remote):
    manager, pr, state, worker = remote
    calls = helper_calls(manager)
    request = {"id": pr["id"], "action": "handle", "task": "Review it", "machine": "mini"}
    manager.action({**request, "exit_when_done": True})
    op = finish(manager, pr["id"])
    assert op["status"] == "complete", op
    path = (worker / "src/worktrees/repo/pr-base-7").resolve()
    assert (op["machine"], op["clone"], op["path"]) == ("mini", "~/src/repo", str(path))
    result = op["result"]
    assert result["workspace_id"] == "mini/w1" and result["url"] is None
    assert (result["machine"], result["machine_label"]) == ("mini", "Mac mini")
    assert result["path"] == str(path) and result["branch"] == "pr-base-7"
    # The checkout is the PR's head, made by the worker's wt in the worker's clone.
    assert git("symbolic-ref", "--short", "HEAD", cwd=path) == "pr-base-7"
    assert git("config", "branch.pr-base-7.remote", cwd=path) == "wtpr-fork-repo"
    assert not calls and not (manager.src / "worktrees").exists()
    data = store(state)
    assert not data["workspaces"] and not data["agents"]
    mini = data["machines"]["Mac mini"]
    [opened] = [c for c in mini["calls"] if c[:2] == ["worktree", "open"]]
    assert opened[opened.index("--label") + 1] == "pr-repo-feature"
    [agent] = mini["agents"]
    assert agent["cwd"] == str(path) and agent["agent"] == "codex"
    assert agent["task"].startswith(f"Review it\n\nPull request: {pr['url']}")
    [staged] = (worker / ".local/state/babysit-pr/workspace-prompts").iterdir()
    assert staged.name == op["id"] and staged.stat().st_mode & 0o777 == 0o600
    [launch] = [c for c in remote_commands(state) if "wt.py" in c]
    assert launch.startswith(f"env BABYSIT_WORKER=mini WT_MULTIPLEXER=herdr {sys.executable} ")
    assert "--github-repo base/repo" in launch and "--exit-watch-json" in launch
    assert "--gpg" not in launch
    # The controller records the exit watch, with the machine it is on; the worker's own
    # state home never holds one.
    [watch] = watches(manager.home)
    # Qualified, so no dashboard can take it for a workspace of its own herdr.
    assert (watch["machine"], watch["path"], watch["workspace_id"]) == (
        "mini",
        str(path),
        "mini/w1",
    )
    assert watch["pane_id"] == agent["pane_id"] and watch["agent"] == "codex"
    assert not (worker / ".local/state/babysit-pr/pr-workspaces.sqlite").exists()
    # The clone lock is the worker's clone by its real path there, the lock its cleanup
    # takes, never a same-named one here.
    key = hashlib.sha256(f"mini\0{clone_path(worker)}".encode()).hexdigest()
    assert (manager.home / "workspace-locks" / key).exists()
    mini_machine = machines.load(manager.home)["mini"]
    lock = machines.clone_lock(manager.home, mini_machine, clone_path(worker))
    assert lock.name == key
    assert manager.scheduled_tasks()["launched"][0]["url"] is None
    register(manager.home, collie_url="https://collie.mini.test")
    assert manager.scheduled_tasks()["launched"][0]["url"] == "https://collie.mini.test/space/w1"


EMPTY = "› \x1b[2mAsk Codex to do anything\x1b[0m"


def idle_agent(state, path, screen=None):
    """The worker's agent at rest in its pane, with its shell and process there."""
    data = store(state)
    mini = data["machines"]["Mac mini"]
    [agent] = mini["agents"]
    agent["agent_status"] = "idle"
    argv = ["codex", *agent["argv"]]
    foreground = [{"pid": 4242, "name": "codex", "argv": argv, "cwd": str(path)}]
    mini.setdefault("process_info", {})[agent["pane_id"]] = {
        "shell_pid": 100,
        "foreground_processes": [{"pid": 100}, *bwrap(str(path)), *foreground],
    }
    if screen is not None:
        mini.setdefault("screens", {})[agent["pane_id"]] = [screen]
    Path(state).write_text(json.dumps(data))
    return agent


def finished(worker, path, marker, answer="Looks good."):
    """A Codex session of the worker's checkout that took the brief and confirmed it."""
    sid = str(uuid.uuid4())
    events = [
        {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "t1"}},
        {"type": "event_msg", "payload": {"type": "user_message", "message": f"Do it\n{marker}"}},
        {
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "turn_id": "t1",
                "last_agent_message": f"{answer}\n{marker}",
            },
        },
    ]
    return rollout(worker, path, sid, *events)


def test_a_workers_exit_watch_exits_its_agent_there_once_it_confirms(remote, monkeypatch):
    manager, pr, state, worker = remote
    monkeypatch.setattr(workspace_exit, "SETTLE_SECONDS", 0)
    request = {"id": pr["id"], "action": "handle", "task": "Review it", "machine": "mini"}
    manager.action({**request, "exit_when_done": True})
    op = finish(manager, pr["id"])
    [watch] = watches(manager.home)
    agent = idle_agent(state, op["path"])
    before = len(store(state)["calls"])
    manager.exit_launched()
    looked(manager)
    [watch] = watches(manager.home)
    assert watch["exit"]["state"] == "watching"
    assert watch["exit"]["message"] == "Waiting for the agent's session transcript"
    # Every herdr call went to the worker's server.
    assert all(c[0] == "--machine=Mac mini" for c in store(state)["calls"][before:])
    # An older session of a reused checkout is not the launch's.
    rollout(worker, op["path"], str(uuid.uuid4()))
    finished(worker, op["path"], watch["marker"])
    idle_agent(state, op["path"], f"Looks good.\n{watch['marker']}\n{EMPTY}")
    manager.exit_launched()
    looked(manager)
    [watch] = watches(manager.home)
    assert watch["exit"]["message"] == "Finished; exiting once the agent stays idle"
    manager.exit_launched()
    looked(manager)
    [watch] = watches(manager.home)
    assert watch["exit"]["state"] == "exited", watch["exit"]
    assert watch["exit"]["message"].endswith("; its pane was closed")
    assert watch["exit"]["target"]["machine"] == "mini"
    mini = store(state)["machines"]["Mac mini"]
    pane = agent["pane_id"]
    keys = [c for c in mini["calls"] if c[:2] == ["agent", "send-keys"]]
    assert keys == [["agent", "send-keys", pane, "ctrl+d"]]
    assert ["pane", "close", pane] in mini["calls"]
    assert all(c[0] == "--machine=Mac mini" for c in store(state)["calls"][before:])


def test_two_sessions_that_took_one_brief_leave_the_agent_open(remote):
    manager, pr, state, worker = remote
    request = {"id": pr["id"], "action": "handle", "task": "Review it", "machine": "mini"}
    manager.action({**request, "exit_when_done": True})
    op = finish(manager, pr["id"])
    [watch] = watches(manager.home)
    idle_agent(state, op["path"])
    finished(worker, op["path"], watch["marker"])
    finished(worker, op["path"], watch["marker"])
    manager.exit_launched()
    looked(manager)
    [watch] = watches(manager.home)
    assert watch["exit"]["state"] == "left_open"
    assert watch["exit"]["message"] == "Several sessions ran in this worktree; left open"
    assert store(state)["machines"]["Mac mini"]["agents"]  # Still open there.


def test_a_session_the_worker_will_not_read_is_skipped_not_counted(remote, monkeypatch):
    manager, pr, state, worker = remote
    request = {"id": pr["id"], "action": "handle", "task": "Review it", "machine": "mini"}
    manager.action({**request, "exit_when_done": True})
    op = finish(manager, pr["id"])
    [watch] = watches(manager.home)
    idle_agent(state, op["path"], f"Looks good.\n{watch['marker']}\n{EMPTY}")
    # A transcript with a second name is refused by the probe; the launch's is still found.
    planted = finished(worker, op["path"], watch["marker"])
    os.link(planted, worker / "second-name.jsonl")
    finished(worker, op["path"], watch["marker"])
    manager.exit_launched()
    looked(manager)
    [watch] = watches(manager.home)
    assert watch["exit"]["state"] == "watching", watch["exit"]
    assert watch["exit"]["message"] == "Finished; exiting once the agent stays idle"
    assert "failures" not in watch["exit"]


def test_a_turn_the_worker_cannot_identify_yet_is_waited_for(remote):
    manager, pr, state, worker = remote
    request = {"id": pr["id"], "action": "handle", "task": "Review it", "machine": "mini"}
    manager.action({**request, "exit_when_done": True})
    op = finish(manager, pr["id"])
    [watch] = watches(manager.home)
    idle_agent(state, op["path"])
    prompt = {"type": "event_msg", "payload": {"type": "user_message", "message": watch["marker"]}}
    rollout(worker, op["path"], str(uuid.uuid4()), prompt)
    manager.exit_launched()
    looked(manager)
    [watch] = watches(manager.home)
    assert watch["exit"]["state"] == "watching", watch["exit"]
    assert "Cannot identify the current turn" in watch["exit"]["message"]
    assert "failures" not in watch["exit"]


def test_a_worker_refusing_a_transcript_leaves_the_agent_open_at_once(remote, monkeypatch):
    manager, pr, state, worker = remote
    request = {"id": pr["id"], "action": "handle", "task": "Review it", "machine": "mini"}
    manager.action({**request, "exit_when_done": True})
    op = finish(manager, pr["id"])
    [watch] = watches(manager.home)
    idle_agent(state, op["path"], f"Looks good.\n{watch['marker']}\n{EMPTY}")
    path = finished(worker, op["path"], watch["marker"])
    manager.exit_launched()
    looked(manager)
    # Its transcript gains a second name: the probe refuses it every time from now on.
    os.link(path, worker / "second-name.jsonl")
    manager.exit_launched()
    looked(manager)
    [watch] = watches(manager.home)
    assert watch["exit"]["state"] == "left_open", watch["exit"]
    assert "Not a session transcript" in watch["exit"]["message"]


def test_a_new_task_on_a_worker_branches_in_its_clone(remote):
    manager, _, state, worker = remote
    value = manager.new_task(new_request(manager, name="try", machine="mini"))
    op = finish(manager, value["operation"]["pr"])
    assert op["status"] == "complete", op
    path = (worker / "src/worktrees/repo/try").resolve()
    assert op["result"]["path"] == str(path) and op["result"]["workspace_id"] == "mini/w1"
    main = subprocess.check_output(
        ["git", "-C", os.environ["FAKE_HEAD"], "rev-parse", "main"], text=True
    ).strip()
    assert git("rev-parse", "HEAD", cwd=path) == main
    [agent] = store(state)["machines"]["Mac mini"]["agents"]
    assert agent["task"] == "Explore the parser"
    assert not watches(manager.home)
    # A second task of that name gets the next free one, as here.
    value = manager.new_task(new_request(manager, name="try", machine="mini"))
    op = finish(manager, value["operation"]["pr"])
    assert op["status"] == "complete" and op["path"].endswith("/try-2")


def test_a_task_on_a_worker_gets_its_attached_files_there(remote):
    manager, pr, state, worker = remote
    shot = attachments.save(manager.attachments, "shot.png", b"\x89PNG")["id"]
    request = {**new_request(manager, name="look", machine="mini"), "attachments": [shot]}
    attachments.attach(manager.attachments, "workspace-new", request)
    op = finish(manager, manager.new_task(request)["operation"]["pr"])
    assert op["status"] == "complete", op
    folder = (worker / ".local/state/babysit-pr/attachments").resolve()
    [copy] = folder.iterdir()
    assert copy.name.endswith(f"-{shot}") and copy.read_bytes() == b"\x89PNG"
    assert copy.stat().st_mode & 0o777 == 0o600
    # The staged brief names the worker's copy, never this machine's upload.
    [agent] = store(state)["machines"]["Mac mini"]["agents"]
    assert agent["task"] == f"Explore the parser\n\n{attachments.HEADING}\n- {copy}"
    assert str(manager.attachments) not in agent["task"]
    # Work on a pull request gets a copy of its own: a worker never overwrites one.
    review = {"id": pr["id"], "action": "handle", "task": "Review it", "machine": "mini"}
    review["attachments"] = [shot]
    attachments.attach(manager.attachments, "workspace-action", review)
    manager.action(review)
    assert finish(manager, pr["id"])["status"] == "complete"
    assert len(list(folder.iterdir())) == 2
    tasks = [a["task"] for a in store(state)["machines"]["Mac mini"]["agents"]]
    assert not any(str(manager.attachments) in task for task in tasks)
    assert all(str(folder) in task for task in tasks)


def test_a_task_whose_attached_file_cannot_be_copied_is_not_started(remote):
    manager, _, state, worker = remote
    shot = attachments.save(manager.attachments, "shot.png", b"\x89PNG")["id"]
    request = {**new_request(manager, name="gone", machine="mini"), "attachments": [shot]}
    attachments.attach(manager.attachments, "workspace-new", request)
    (manager.attachments / shot).unlink()
    op = finish(manager, manager.new_task(request)["operation"]["pr"])
    assert op["status"] == "failed" and "attached file is missing" in op["message"], op
    assert not store(state).get("machines", {}).get("Mac mini", {}).get("agents")
    assert not (worker / ".local/state/babysit-pr/workspace-prompts").exists()
    assert not any("wt.py" in c for c in remote_commands(state))


def worker_clone(worker, name):
    """The worker's own clone of base/<name>, with a ``dev`` branch one commit ahead."""
    there = worker / "src" / name
    there.mkdir(parents=True)
    git("init", "-q", "-b", "main", cwd=there)
    for branch in ("main", "dev"):
        git("switch", "-q", "-C", branch, cwd=there)
        git(
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.test",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            branch,
            cwd=there,
        )
    git("remote", "add", "upstream", f"https://github.com/base/{name}.git", cwd=there)
    return there.resolve()


def test_a_new_task_on_a_worker_also_checks_out_its_other_clones(remote):
    manager, _, state, worker = remote
    other = other_clone(manager, git)
    there = worker_clone(worker, "other")
    # The name is taken in the worker's other clone, so every checkout gets the next one.
    git("branch", "both", cwd=there)
    task = new_request(
        manager, name="both", also=[str(other)], also_bases={str(other): "dev"}, machine="mini"
    )
    op = finish(manager, manager.new_task(task)["operation"]["pr"])
    assert op["status"] == "complete", op
    assert op["also"] == ["~/src/other"] and op["also_repos"] == {"~/src/other": "base/other"}
    assert op["also_bases"] == {"~/src/other": "dev"}
    path = (worker / "src/worktrees/other/both-2").resolve()
    assert op["branch"] == "both-2" and op["also_paths"] == [str(path)]
    assert git("symbolic-ref", "--short", "HEAD", cwd=path) == "both-2"
    assert git("rev-parse", "HEAD", cwd=path) == git("rev-parse", "dev", cwd=there)
    [launch] = [c for c in remote_commands(state) if "wt.py" in c]
    assert f" --with {there}:dev " in launch
    [agent] = store(state)["machines"]["Mac mini"]["agents"]
    assert f"you can write and commit there:\n- {path}" in agent["task"]


def test_a_worker_clones_a_missing_other_repository(remote):
    manager, _, state, worker = remote
    other = other_clone(manager, git)
    git(
        "config",
        "--global",
        f"url.{other}.insteadOf",
        "https://github.com/base/other.git",
        cwd=other,
    )
    task = new_request(manager, name="lonely", also=[str(other)], machine="mini")
    op = finish(manager, manager.new_task(task)["operation"]["pr"])
    assert op["status"] == "complete", op
    there = worker / "src/other"
    assert git("config", "remote.origin.url", cwd=there) == "https://github.com/base/other.git"
    assert git("rev-parse", "HEAD", cwd=op["also_paths"][0]) == git("rev-parse", "HEAD", cwd=other)
    # A worker clone named like the task's own repository would share its checkouts.
    named = other_clone(manager, git, "named")
    git("remote", "set-url", "upstream", "https://github.com/base/Repo.git", cwd=named)
    with pytest.raises(ValueError, match="share a clone name on Mac mini"):
        manager.new_task(new_request(manager, name="x", also=[str(named)], machine="mini"))


def test_a_new_task_on_a_worker_keeps_claudes_auto_permission_mode(remote):
    manager, _, state, _ = remote
    task = new_request(manager, name="careful", agent="claude", permission_mode="auto")
    # Typed as `safe claude …`, which the fake worker cannot run: only the launch counts.
    finish(manager, manager.new_task({**task, "machine": "mini"})["operation"]["pr"])
    [launch] = [c for c in remote_commands(state) if "wt.py" in c]
    assert " --claude --permission-mode auto " in launch


def test_a_worker_with_gpg_lets_its_agent_sign(remote):
    manager, pr, state, _ = remote
    register(manager.home, labels=["safehouse", "gpg"])
    manager.action({"id": pr["id"], "action": "handle", "task": "Review", "machine": "mini"})
    assert finish(manager, pr["id"])["status"] == "complete"
    [launch] = [c for c in remote_commands(state) if "wt.py" in c]
    assert " --gpg " in launch


def test_a_worker_running_another_babysit_pr_is_refused(remote, tmp_path):
    manager, pr, state, worker = remote
    register(manager.home, babysit_root=str(worker / "src" / "repo"))
    manager.action({"id": pr["id"], "action": "handle", "task": "Review", "machine": "mini"})
    op = finish(manager, pr["id"])
    head = subprocess.check_output(["git", "-C", str(APP), "rev-parse", "HEAD"], text=True)
    assert op["status"] == "failed"
    assert op["message"].endswith(
        f"update babysit-pr on Mac mini (provision_worker.sh --user-step --sha {head.strip()})"
    )
    # The dashboard offers the update where it shows the refusal.
    assert op["stale_worker"] == {"machine": "mini", "sha": head.strip()}
    assert not [c for c in remote_commands(state) if "wt.py" in c]


@pytest.mark.parametrize("kind", ["task", "pr"])
def test_a_worker_clones_a_missing_repository_before_starting(remote, kind):
    manager, pr, state, worker = remote
    shutil.rmtree(worker / "src")
    if kind == "task":
        value = manager.new_task(new_request(manager, name="fresh", machine="mini"))
        key, branch = value["operation"]["pr"], "main"
    else:
        manager.action({"id": pr["id"], "action": "handle", "task": "Review", "machine": "mini"})
        key, branch = pr["id"], "feature"
    op = finish(manager, key)
    assert op["status"] == "complete", op
    there = worker / "src/repo"
    assert git("config", "remote.origin.url", cwd=there) == URL
    expected = git("rev-parse", branch, cwd=os.environ["FAKE_HEAD"])
    assert git("rev-parse", "HEAD", cwd=op["path"]) == expected
    assert len(store(state)["machines"]["Mac mini"]["agents"]) == 1
    assert not list((worker / "src").glob(".babysit-clone-*"))


def test_work_that_may_need_repairs_stays_off_workers(remote):
    manager, _, state, _ = remote
    issue = manager.issues.value["issues"][0]
    with pytest.raises(ValueError, match="^Mac mini: only takes pull request work and new tasks$"):
        manager.action({"id": issue["id"], "action": "handle", "task": "Fix", "machine": "mini"})
    assert manager.operation(issue["id"]) is None
    with pytest.raises(ValueError, match="^Mac mini: only takes pull request work and new tasks$"):
        manager.new_task(new_request(manager, issue_title="Remote issue", machine="mini"))
    assert not store(state).get("gh")
    assert not store(state).get("ssh")


def test_a_scheduled_review_starts_on_its_worker(remote):
    manager, pr, state, worker = remote
    manager.overview.value["synced_at"] = time.time()
    start = time.time() + 3600
    request = {"id": pr["id"], "action": "handle", "task": "Later", "machine": "mini"}
    task = manager.action({**request, "start_at": start})["scheduled"]
    manager.run_due(now=start)
    op = finish(manager, pr["id"])
    assert op["status"] == "complete" and op["machine"] == "mini", op
    [listed] = manager.scheduled_tasks()["tasks"]
    assert listed["id"] == task["id"] and listed["status"] == "started"
    [agent] = store(state)["machines"]["Mac mini"]["agents"]
    assert op["exit_marker"] in agent["task"]
    # Its exit watch follows the agent on the worker, never here.
    idle_agent(state, op["path"])
    before = len(store(state)["calls"])
    manager.exit_finished()
    looked(manager)
    [listed] = manager.scheduled_tasks()["tasks"]
    assert (listed["exit"]["state"], listed["exit"]["message"]) == (
        "watching",
        "Waiting for the agent's session transcript",
    )
    assert all(c[0] == "--machine=Mac mini" for c in store(state)["calls"][before:])


def test_without_a_registry_a_launch_runs_here_as_before(local):  # noqa: F811
    manager, pr, _, state, _ = local
    calls = helper_calls(manager)
    manager.action({"id": pr["id"], "action": "create", "task": "Local", "machine": "auto"})
    op = finish(manager, pr["id"])
    assert op["status"] == "complete", op
    clone = str((manager.src / "repo").resolve())
    assert calls == [
        [
            "wtpr",
            "--codex",
            "--no-focus",
            "--name",
            "pr-base-7",
            "--label",
            "pr-repo-feature",
            "--repo-path",
            clone,
            "--github-repo",
            "base/repo",
            "--worktree-root",
            str(manager.src / "worktrees" / "repo"),
            "--prompt-file",
            str(manager.home / "workspace-prompts" / op["id"]),
            pr["url"],
        ]
    ]
    assert list(op) == [
        "id",
        "pr",
        "action",
        "status",
        "machine",
        "clone",
        "path",
        "agent",
        "model",
        "effort",
        "claude_account",
        "docker",
        "exit_when_done",
        "message",
        "log",
        "created_at",
        "updated_at",
        "common",
        "branch",
        "result",
    ]
    assert op["machine"] is None
    data = store(state)
    assert "ssh" not in data and "machines" not in data
    assert not any(call[0].startswith("--machine") for call in data["calls"])
    key = hashlib.sha256(clone.encode()).hexdigest()
    assert [p.name for p in (manager.home / "workspace-locks").iterdir()] == [key]


# The worker's side, through the probe ----------------------------------------------


def prepare(home, clone, prefix="pr-base-7", **changes):
    args = {
        "clone": str(clone),
        "repo": "base/repo",
        "root": str(home / "src" / "worktrees" / "repo"),
        "prefix": prefix,
        "agent": "claude",
        "account": None,
        "sandbox": "safehouse",
        **changes,
    }
    return remote_probe.validate_wt_prepare(remote_probe._host_wt_prepare(**args))


@pytest.mark.parametrize("occupied", ["directory", "file", "wrong-repository", "dangling-link"])
def test_a_worker_never_clones_over_an_existing_destination(host, occupied):  # noqa: F811
    home, clone, _ = host
    shutil.rmtree(clone)
    if occupied == "file":
        clone.write_text("keep me")
    elif occupied == "dangling-link":
        clone.symlink_to(home / "not-created")
    else:
        clone.mkdir()
        (clone / "keep").write_text("keep me")
        if occupied == "wrong-repository":
            git("init", "-q", cwd=clone)
            git("remote", "add", "origin", "https://github.com/someone/else.git", cwd=clone)
    with pytest.raises(ValueError, match="clone of base/repo"):
        prepare(home, clone)
    assert os.path.lexists(clone)
    assert not (home / "not-created").exists()
    if clone.is_dir():
        assert (clone / "keep").read_text() == "keep me"
    elif not clone.is_symlink():
        assert clone.read_text() == "keep me"
    assert not list(clone.parent.glob(".babysit-clone-*"))


def test_a_failed_worker_clone_leaves_the_destination_available_for_retry(host):  # noqa: F811
    home, clone, upstream = host
    shutil.rmtree(clone)
    config = Path(os.environ["GIT_CONFIG_GLOBAL"])
    original = config.read_text()
    config.write_text(
        original.replace(str(home.parent.parent / "github.git"), str(home / "missing.git"))
    )
    with pytest.raises(ValueError, match="does not appear to be a git repository"):
        prepare(home, clone)
    assert not clone.exists()
    assert not list(clone.parent.glob(".babysit-clone-*"))
    config.write_text(original)
    assert prepare(home, clone)["clone"] == str(clone.resolve())
    assert git("rev-parse", "HEAD", cwd=clone) == upstream
    assert git("status", "--porcelain", cwd=clone) == ""


def test_a_worker_clone_timeout_removes_staging(host, monkeypatch):  # noqa: F811
    home, clone, _ = host
    shutil.rmtree(clone)
    run = remote_probe._host_run

    def timeout(argv, seconds, env=None):
        if "clone" in argv:
            raise subprocess.TimeoutExpired(argv, seconds)
        return run(argv, seconds, env)

    monkeypatch.setattr(remote_probe, "_host_run", timeout)
    with pytest.raises(subprocess.TimeoutExpired):
        prepare(home, clone)
    assert not clone.exists()
    assert not list(clone.parent.glob(".babysit-clone-*"))


def test_a_worker_clone_preserves_a_destination_created_while_cloning(host, monkeypatch):  # noqa: F811
    home, clone, _ = host
    shutil.rmtree(clone)
    run = remote_probe._host_run

    def appeared(argv, seconds, env=None):
        result = run(argv, seconds, env)
        if "clone" in argv:
            clone.mkdir()
            (clone / "keep").write_text("keep me")
        return result

    monkeypatch.setattr(remote_probe, "_host_run", appeared)
    with pytest.raises(FileExistsError):
        prepare(home, clone)
    assert (clone / "keep").read_text() == "keep me"
    assert not (clone / ".git").exists()
    assert not list(clone.parent.glob(".babysit-clone-*"))


def test_a_worker_clone_checks_out_without_filters_or_template_hooks(host):  # noqa: F811
    home, clone, _ = host
    work = home.parent.parent / "upstream-work"
    (work / ".gitattributes").write_text("f filter=trip\n")
    git("add", ".gitattributes", cwd=work)
    git("commit", "-q", "-m", "filtered file", cwd=work)
    git("push", "-q", "origin", "main", cwd=work)
    marker = home / "ran-program"
    script = f"touch {marker}"
    template = home / "template"
    (template / "hooks").mkdir(parents=True)
    hook = template / "hooks/post-checkout"
    hook.write_text(f"#!/bin/sh\n{script}\n")
    hook.chmod(0o755)
    for key, value in {
        "init.templateDir": str(template),
        "filter.trip.smudge": script,
        "filter.trip.process": script,
        "filter.trip.required": "true",
    }.items():
        git("config", "--global", key, value, cwd=clone)
    shutil.rmtree(clone)
    prepare(home, clone)
    assert (clone / "f").read_text() == "a\n"
    assert (clone / ".gitattributes").read_text() == "f filter=trip\n"
    assert not (clone / ".git/hooks/post-checkout").exists()
    assert not marker.exists()


def test_a_workers_launch_names_a_free_checkout_and_trusts_it(host):  # noqa: F811
    home, clone, _ = host
    found = prepare(home, clone)
    root = (home / "src/worktrees/repo").resolve()
    assert found == {
        "clone": str(clone.resolve()),
        "root": str(root),
        "name": "pr-base-7",
        "path": str(root / "pr-base-7"),
        "also": [],
    }
    projects = json.loads((home / ".claude.json").read_text())["projects"]
    assert projects[found["path"]] == {"hasTrustDialogAccepted": True}
    (root / "pr-base-7").mkdir(parents=True)
    git("branch", "pr-base-7-2", cwd=clone)
    assert prepare(home, clone)["name"] == "pr-base-7-3"
    for change in ({"prefix": "a/b"}, {"prefix": "-x"}, {"repo": "base/.."}, {"agent": "sh"}):
        with pytest.raises(ValueError):
            prepare(home, clone, **change)
    with pytest.raises(ValueError, match="lies inside"):
        prepare(home, clone, root=str(clone / ".git" / "w"))
    git("remote", "set-url", "origin", "https://github.com/bob/repo.git", cwd=clone)
    with pytest.raises(ValueError, match="is not a clone of base/repo"):
        prepare(home, clone)


def test_a_workers_launch_names_a_checkout_free_in_its_other_clones(host):  # noqa: F811
    home, clone, _ = host
    other = worker_clone(home, "other")
    git("branch", "pr-base-7", cwd=other)
    found = prepare(home, clone, also=[[str(other), "base/other"]])
    assert found["name"] == "pr-base-7-2"
    path = (home / "src/worktrees/other/pr-base-7-2").resolve()
    assert found["also"] == [{"clone": str(other), "path": str(path)}]
    with pytest.raises(ValueError, match="is not a clone of base/else"):
        prepare(home, clone, also=[[str(other), "base/else"]])
    with pytest.raises(ValueError, match="share another repository"):
        prepare(home, clone, also=[[str(clone), "base/repo"]])
    # Checked out under worktrees/repo, beside the task's own.
    named = worker_clone(home / "elsewhere", "repo")
    with pytest.raises(ValueError, match="share another repository"):
        prepare(home, clone, also=[[str(named), "base/repo"]])
    (home / "src/worktrees").mkdir(parents=True, exist_ok=True)
    (home / "src/worktrees/other").symlink_to(home)
    with pytest.raises(ValueError, match="is a symbolic link"):
        prepare(home, clone, also=[[str(other), "base/other"]])
    (home / "src/worktrees/other").unlink()
    for bad in ([["relative", "base/other"]], [[str(other)]], [[str(other), "x"]], "x"):
        with pytest.raises(ValueError):
            prepare(home, clone, also=bad)
    with pytest.raises(ValueError, match="at most"):
        prepare(home, clone, also=[[str(other), "base/other"]] * 5)


def test_a_workers_checkout_is_read_from_its_own_layout(host, tmp_path):  # noqa: F811
    home, clone, _ = host
    path = home / "src/worktrees/repo/x"
    git("worktree", "add", "-q", "-b", "x", str(path), cwd=clone)
    git("config", "branch.x.remote", "origin", cwd=clone)
    git("config", "branch.x.merge", "refs/heads/main", cwd=clone)
    found = remote_probe.validate_wt_checkout(remote_probe._host_wt_checkout(str(clone), str(path)))
    assert found == {"path": str(path.resolve()), "branch": "x", "upstream": ["base/repo", "main"]}
    other = tmp_path / "other"
    git("clone", "-q", str(clone), str(other), cwd=tmp_path)
    with pytest.raises(ValueError, match="not a worktree of"):
        remote_probe._host_wt_checkout(str(other), str(path))


def test_the_babysit_pr_a_worker_runs_is_its_deployed_commit(host):  # noqa: F811
    home, clone, upstream = host
    head = git("rev-parse", "HEAD", cwd=clone)
    assert remote_probe.validate_version(remote_probe._host_version(str(clone))) == {"head": head}
    with pytest.raises(ValueError, match="Malformed probe answer"):
        remote_probe.validate_version({"head": "main"})


@pytest.mark.parametrize("bad", ["\x15", "\x1b[201~", "\n", "\x9b"])
def test_worker_launch_paths_with_control_characters_are_refused(bad):
    found = {"clone": "/h/src/r", "root": "/h/src/worktrees/r", "name": "n"}
    found["path"] = "/h/src/worktrees/r/n"
    other = {"clone": "/h/src/o", "path": "/h/src/worktrees/o/n"}
    remote_probe.validate_wt_prepare({**found, "also": [other]})
    for key in found:
        with pytest.raises(ValueError, match="Malformed probe answer"):
            remote_probe.validate_wt_prepare({**found, key: f"{found[key]}{bad}", "also": []})
    for key in other:
        with pytest.raises(ValueError, match="Malformed probe answer"):
            remote_probe.validate_wt_prepare(
                {**found, "also": [{**other, key: f"{other[key]}{bad}"}]}
            )
    # Never the task's own checkout, nor one clone twice, nor a clone named "..".
    for others in (
        [{"clone": "/h/src/r", "path": "/h/src/worktrees/r/n"}],
        [other, other],
        [{"clone": "/h/src/..", "path": "/h/src/worktrees/../n"}],
    ):
        with pytest.raises(ValueError, match="Malformed probe answer: also"):
            remote_probe.validate_wt_prepare({**found, "also": others})
    # Only ever beside the task's checkout, in the folder named after the clone.
    for path in ("/h/src/worktrees/r/n", "/elsewhere/o/n", "/h/src/worktrees/o/m"):
        with pytest.raises(ValueError, match="Malformed probe answer: also"):
            remote_probe.validate_wt_prepare({**found, "also": [{**other, "path": path}]})


def test_a_long_pr_branch_keeps_its_launch_and_exit_watch(remote, monkeypatch):
    manager, pr, state, worker = remote
    # The fork names the branch: as long as Git allows, and the label grows with it.
    branch = "b" * 230
    subprocess.check_call(["git", "-C", os.environ["FAKE_HEAD"], "branch", branch, "feature"])
    pr["head_branch"] = branch
    reply = {"headRefName": branch, "headRepository": {"name": "repo"}}
    monkeypatch.setenv(
        "FAKE_GH_PR", json.dumps({**reply, "headRepositoryOwner": {"login": "fork"}})
    )
    request = {"id": pr["id"], "action": "handle", "task": "Review it", "machine": "mini"}
    manager.action({**request, "exit_when_done": True})
    op = finish(manager, pr["id"])
    assert op["status"] == "complete", op
    [watch] = watches(manager.home)
    assert watch["label"] == f"pr-repo-{branch}"


def old_operation(manager, key, **fields):
    """An operation as saved long ago, without save_operation's fresh timestamp."""
    old = time.time() - pw.HISTORY_SECONDS - 60
    op = {"id": str(uuid.uuid4()), "pr": key, "created_at": old, "updated_at": old, **fields}
    with manager.db() as db:
        db.execute("INSERT OR REPLACE INTO operations VALUES (?,?)", (key, json.dumps(op)))
    return op


def test_a_workers_paths_are_never_looked_up_here(remote, tmp_path):
    manager, pr, state, _ = remote
    # A worker laid out like this machine names a path that also exists here.
    here = tmp_path / "same-layout"
    here.mkdir()
    old_operation(manager, pr["id"], status="complete", machine="mini", path=str(here))
    manager.prune_history()
    assert manager.operation(pr["id"]) is None
    # Nor does a local workspace at that path recover a worker's uncertain launch.
    manager.action({"id": pr["id"], "action": "create", "task": "Here"})
    op = finish(manager, pr["id"])
    assert op["status"] == "complete" and op["machine"] is None
    old_operation(
        manager, pr["id"], status="uncertain", machine="mini", path=op["path"], agent="codex"
    )
    assert manager.describe(pr, manager.scan())["operation"]["status"] == "uncertain"


def worker_watch(home, machine="mini", **changes):
    now = time.time()
    watch = {
        "id": uuid.uuid4().hex,
        "label": "pr-repo-feature",
        "path": "/h/src/worktrees/repo/pr-base-7",
        "workspace_id": f"{machine}/w1",
        "pane_id": "w1:p1",
        "agent": "codex",
        "claude_account": None,
        "marker": exit_watch.new_marker(),
        "created_at": now,
        "exit": exit_watch.watching(now),
        "machine": machine,
        **changes,
    }
    exit_watch.register(home, watch)
    return watch


def test_a_workers_exit_watch_ends_when_it_cannot_be_looked_at(remote, monkeypatch):
    manager, *_ = remote
    gone = worker_watch(manager.home, machine="box")
    manager.exit_launched()
    looked(manager)
    [found] = [w for w in watches(manager.home) if w["id"] == gone["id"]]
    assert found["exit"]["state"] == "left_open"
    assert "box is no longer registered" in found["exit"]["message"]
    # An unreachable worker is given a few looks, then left.
    monkeypatch.setenv("FAKE_HERDR_DOWN", "Mac mini")
    monkeypatch.setattr(workspace_exit, "REMOTE_FAILURES", 2)
    started = time.time() - pw.LAUNCH_START_SECONDS - 1
    down = worker_watch(manager.home, created_at=started)
    for state in ("watching", "left_open"):
        manager.exit_launched()
        looked(manager)
        [found] = [w for w in watches(manager.home) if w["id"] == down["id"]]
        assert found["exit"]["state"] == state
        assert found["exit"]["message"].startswith("Could not look at the agent on Mac mini")
    # So is one watched for longer than any watch is kept.
    old = time.time() - workspace_exit.WATCH_LIMIT - 60
    stale = worker_watch(manager.home, created_at=old, exit=exit_watch.watching(old))
    manager.exit_launched()
    looked(manager)
    [found] = [w for w in watches(manager.home) if w["id"] == stale["id"]]
    assert found["exit"]["state"] == "left_open"


def test_an_exit_watch_a_worker_hands_back_is_checked(remote):
    manager, *_ = remote
    worker = machines.resolve(manager.home, "mini")
    op = {"id": "o", "pr": "x", "agent": "codex", "claude_account": None}
    watch = {
        "id": uuid.uuid4().hex,
        "label": "pr-repo-feature",
        "path": "/h/w",
        "workspace_id": "w3",
        "pane_id": "w3:p1",
        "marker": exit_watch.new_marker(),
    }
    for change in (
        {"path": "/h/other"},
        {"workspace_id": "w3/../x"},
        {"pane_id": "p\x15"},
        {"marker": "done"},
        {"id": "x"},
        {"label": "pr-repo-other"},
    ):
        with pytest.raises(ValueError, match="did not hand back"):
            manager.record_remote_watch(
                worker, op, "/h/w", "pr-repo-feature", json.dumps({**watch, **change})
            )
    with pytest.raises(ValueError, match="did not hand back"):
        manager.record_remote_watch(worker, op, "/h/w", "pr-repo-feature", "not json")
    assert not watches(manager.home)
