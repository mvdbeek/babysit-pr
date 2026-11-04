"""Moving a workspace's agent to a worker: bundle, transcript, resume and cleanup.

This machine is ``<tmp>``, with its clone, its herdr workspace w1 and the agent's
transcript; the worker is ``<tmp>/hosts/<ssh target>``, which the fake ssh uses as HOME,
with a clone of the same repository and its own herdr state in the fake herdr's store.
Real Git runs on both; herdr, ssh, ps, Claude and Codex are fakes.
"""

import io
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import agent_messages
import herdr_handoff
import machines
import pr_supervisor
import pytest
import remote_probe
import workspace_move
import workspace_overview as wso
from test_remote_inventory import git, make_clone, read, register, short_tmp
from test_workspace_overview import FakeGitHub

__all__ = ["short_tmp"]  # A fixture, used by name.

SID = "8f2c6f0e-3b7a-4d34-9a8f-2b8d6c1e9a10"
MOVED = "ab" * 16  # A saved move's ID.
RULE = "─" * 40
CLAUDE_READY = "\n".join(["⏺ Done.", "", RULE, "❯ ", RULE, "  ? for shortcuts"])
CLAUDE_EXIT_TYPED = CLAUDE_READY.replace("❯ ", "❯ /exit")
CLAUDE_DRAFT = CLAUDE_READY.replace("❯ ", "❯ half a thought")
CODEX_READY = "› \x1b[2mAsk Codex to do anything\x1b[0m"
DIALOG = "\n".join([RULE, " Allow this command?", " ❯ 1. Yes", "   2. No", "", " Esc to cancel"])
MINI = {
    "id": "mini",
    "label": "Mac mini",
    "ssh": "mini.test",
    "os": "darwin",
    "labels": ["safehouse"],
    "auto": True,
}
BOX = {
    "id": "box",
    "label": "Linux box",
    "ssh": "box.test",
    "os": "linux",
    "labels": ["bwrap"],
    "auto": True,
    "babysit_root": "~/b",
}
# ps names a pane's root process (an interactive shell) and lists the agent's children.
PS = """import json,os,sys
from pathlib import Path
data=json.loads(Path(os.environ['FAKE_HERDR']).read_text()); a=sys.argv[1:]
if a[:1]==['-A']:
 for row in data.get('ps',[]): print(*row)
else:
 print(data.get('commands',{}).get(a[3],'-zsh'))
"""
# babysit-bwrap on the Linux worker: runs the agent command it is given, unconfined.
BWRAP = '#!/bin/sh\nwhile [ $# -gt 0 ] && [ "$1" != "--" ]; do shift; done\nshift\nexec "$@"\n'
CHILDREN = [
    [4242, 100, "claude --model opus"],
    [5000, 4242, "/bin/zsh -c pytest -x"],
    [5001, 5000, "python -m pytest -x"],
    [6000, 1, "unrelated"],
]


def jsonl(*records):
    return "".join(json.dumps(record) + "\n" for record in records)


def slug(path):
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def lines(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines()]


class Placing:
    """New task's placement as the dashboard has it: the registry and fresh loads."""

    def __init__(self, home, **loads):
        self.home = home
        self.extra = loads
        self.loads = SimpleNamespace(snapshot=self.snapshot)

    def snapshot(self):
        config = machines.load_config(self.home)
        fresh = {
            machine_id: {"agents": 0, "working": 0, "attempted_at": time.time()}
            for machine_id in config["machines"]
        }
        local = {"local": {"agents": 1, "working": 0, "max_agents": 20}}
        return config, {**local, **fresh, **self.extra}

    def reservations(self):
        return {}


def make_rig(
    tmp_path,
    fake_tools,
    short_tmp,
    monkeypatch,
    kind="claude",
    dirty=True,
    worker=MINI,
    precious=False,
    subdir=None,
):
    tmp = tmp_path.resolve()
    monkeypatch.setenv("HOME", str(tmp))
    for name in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "BABYSIT_DASHBOARD_URL"):
        monkeypatch.delenv(name, raising=False)
    clone = tmp / "src" / "repo"
    checkout = make_clone(clone, ["feature"]) / "feature"
    host = tmp / "hosts" / worker["ssh"]
    # The worker's own clone of the repository, made before the branch moved on here.
    subprocess.check_call(["git", "clone", "-q", str(clone), str(host / "src" / "repo")])
    there = host / "src" / "repo"
    git("remote", "set-url", "origin", "https://github.com/fork/repo.git", cwd=there)
    git("remote", "add", "upstream", "https://github.com/base/repo.git", cwd=there)
    if worker is BOX:
        launcher = host / "b" / "scripts" / "babysit-bwrap"
        launcher.parent.mkdir(parents=True)
        launcher.write_text(BWRAP)
        launcher.chmod(0o755)
    # Commits only this machine has: the branch is ahead of every remote branch.
    (checkout / ".gitignore").write_text("*.log\n.venv/\n")
    if subdir:
        (checkout / subdir).mkdir()
        (checkout / subdir / "app.js").write_text("app\n")
    (checkout / "lib.py").write_text("one\n")
    (checkout / "gone.txt").write_text("going\n")
    (checkout / "tool.sh").write_text("#!/bin/sh\n")
    (checkout / "tool.sh").chmod(0o755)
    git("add", "-A", cwd=checkout)
    git("commit", "-q", "-m", "Unpushed work", cwd=checkout)
    git("config", "branch.feature.remote", "origin", cwd=checkout)
    git("config", "branch.feature.merge", "refs/heads/feature", cwd=checkout)
    if dirty:
        (checkout / "lib.py").write_text("one\ntwo\n")
        (checkout / "new.txt").write_text("staged\n")
        git("add", "lib.py", "new.txt", cwd=checkout)
        (checkout / "lib.py").write_text("one\ntwo\nthree\n")  # Unstaged on top.
        (checkout / "gone.txt").unlink()
        (checkout / "notes").mkdir()
        (checkout / "notes" / "todo.md").write_text("untracked\n")
        # A name the nested-repository check once took for a gitlink.
        (checkout / "notes" / "a commit b.md").write_text("untracked\n")
    # Ignored and rebuildable: neither copied nor a reason to keep the checkout.
    (checkout / ".venv" / "bin").mkdir(parents=True)
    (checkout / ".venv" / "bin" / "python").write_text("#!/bin/sh\n")
    if precious:
        (checkout / "debug.log").write_text("ignored, and not rebuildable\n")
    started = checkout / subdir if subdir else checkout
    if kind == "claude":
        folder = tmp / ".claude" / "projects" / slug(started)
        (folder / SID / "subagents").mkdir(parents=True)
        transcript = folder / f"{SID}.jsonl"
        transcript.write_text(
            jsonl(
                {
                    "type": "user",
                    "sessionId": SID,
                    "cwd": str(started),
                    "message": {"role": "user", "content": "Fix the bug"},
                },
                {
                    "type": "assistant",
                    "sessionId": SID,
                    "cwd": str(checkout / "notes"),
                    "message": {
                        "role": "assistant",
                        "model": "claude-fixture",
                        "content": [{"type": "text", "text": f"Done in {checkout}"}],
                    },
                },
            )
        )
        (folder / SID / "subagents" / "agent-a1.jsonl").write_text(
            jsonl({"type": "user", "isSidechain": True, "cwd": str(checkout), "message": {}})
        )
        argv = ["claude", "--dangerously-skip-permissions", "--model", "opus"]
        screens = [CLAUDE_READY, CLAUDE_EXIT_TYPED]
    else:
        day = tmp / ".codex" / "sessions" / "2026" / "10" / "25"
        day.mkdir(parents=True)
        transcript = day / f"rollout-2026-10-25T10-00-00-{SID}.jsonl"
        transcript.write_text(
            jsonl(
                {"type": "session_meta", "payload": {"id": SID, "cwd": str(checkout)}},
                {
                    "type": "turn_context",
                    "payload": {"cwd": str(checkout), "model": "gpt-fixture", "effort": "high"},
                },
                {"type": "event_msg", "payload": {"type": "user_message", "message": "Fix it"}},
            )
        )
        argv = ["codex", "--dangerously-bypass-approvals-and-sandbox"]
        screens = [CODEX_READY]
    store = read(fake_tools)
    store.update(
        workspaces=[
            {
                "workspace_id": "w1",
                "label": "feature",
                "agent_status": "idle",
                "worktree": {"repo_root": str(clone), "checkout_path": str(checkout)},
            }
        ],
        agents=[
            {
                "workspace_id": "w1",
                "pane_id": "w1:p2",
                "agent": kind,
                "agent_status": "idle",
                "agent_session": {"value": SID},
                "cwd": str(checkout),
            }
        ],
        panes=[
            {"pane_id": "w1:p1", "workspace_id": "w1"},
            {"pane_id": "w1:p2", "workspace_id": "w1"},
        ],
        process_info={
            "w1:p2": {
                "shell_pid": 100,
                "foreground_processes": [
                    {"pid": 4242, "argv0": f"/opt/bin/{kind}", "argv": argv, "cwd": str(checkout)}
                ],
            }
        },
        screens={"w1:p2": screens},
        ps=CHILDREN,
        machines={worker["label"]: {"workspaces": [], "agents": [], "calls": []}},
    )
    fake_tools.write_text(json.dumps(store))
    ps = fake_tools.parent / "bin" / "ps"
    ps.write_text(f"#!{sys.executable} -S\n" + PS)
    ps.chmod(0o700)
    monkeypatch.setattr(agent_messages, "PS", str(ps))
    monkeypatch.setattr(workspace_move, "PS", str(ps))
    monkeypatch.setattr(workspace_move.agent_docker, "has_access", lambda pid: False)
    monkeypatch.setattr(workspace_move, "CONFIRM_WAIT", 2)
    monkeypatch.setattr(workspace_move, "CLEANUP_POLL", 0.05)

    def kill(pid, signal):
        assert (pid, signal) == (4242, 0)
        if not any(a.get("pane_id") == "w1:p2" for a in read(fake_tools)["agents"]):
            raise ProcessLookupError()

    monkeypatch.setattr(herdr_handoff.os, "kill", kill)
    home = short_tmp / "state"
    (home / "experiments" / "workspaces").mkdir(parents=True)
    (home / "experiments" / "workspaces" / "config.json").write_text(
        json.dumps({"enabled": True, "src": str(tmp / "src")})
    )
    register(home, worker)
    overview = wso.WorkspaceOverview(home, FakeGitHub())
    return SimpleNamespace(
        tmp=tmp,
        home=home,
        state=fake_tools,
        clone=clone,
        checkout=checkout,
        host=host,
        worker=worker,
        transcript=transcript,
        kind=kind,
        placing=Placing(home),
        overview=overview,
        target=host / "src" / "worktrees" / "repo" / "feature",
        progress=[],
    )


@pytest.fixture
def rig(tmp_path, fake_tools, short_tmp, monkeypatch):
    return make_rig(tmp_path, fake_tools, short_tmp, monkeypatch)


def request(**changes):
    return {
        "workspace": "w1",
        "pane": "w1:p2",
        "session": SID,
        "machine": "auto",
        "confirm_working": False,
        **changes,
    }


def run_move(rig, **changes):
    move = workspace_move.Move(
        rig.home, request(**changes), rig.placing, rig.overview, rig.progress.append
    )
    return move.run()


def worker_store(rig):
    return read(rig.state)["machines"][rig.worker["label"]]


def local_agents(rig):
    return [a for a in read(rig.state)["agents"] if a.get("pane_id") == "w1:p2"]


def files(path):
    """Every file under ``path`` but Git's own, by name, with its content and mode."""
    return {
        str(f.relative_to(path)): (f.read_bytes(), os.stat(f).st_mode & 0o777)
        for f in path.rglob("*")
        if f.is_file() and ".git" not in f.relative_to(path).parts
    }


def watch_exits(rig, monkeypatch):
    """Record, as the agent here is told to exit, what the worker already has."""
    seen = []
    real = agent_messages.exit_agent

    def exit_agent(request, home=None, **options):
        seen.append(rig.target.exists())
        return real(request, home, **options)

    monkeypatch.setattr(agent_messages, "exit_agent", exit_agent)
    return seen


# The move ----------------------------------------------------------------------------


def test_an_idle_claude_moves_with_its_branch_changes_and_conversation(rig, monkeypatch):
    before = files(rig.checkout)
    head = git("rev-parse", "HEAD", cwd=rig.checkout)
    staged = git("diff", "--cached", "--name-status", cwd=rig.checkout)
    exits = watch_exits(rig, monkeypatch)
    result = run_move(rig)

    # The worker's checkout was made and checked before the agent here was told to exit.
    assert exits == [True]
    target = rig.target
    assert result["path"] == str(target) and result["machine"] == "mini"
    assert git("rev-parse", "HEAD", cwd=target) == head
    assert git("symbolic-ref", "--short", "HEAD", cwd=target) == "feature"
    # Unpushed commits, staged, unstaged and untracked changes; ignored files stay here.
    expected = {k: v for k, v in before.items() if not k.startswith(".venv/")}
    assert files(target) == expected
    assert files(target)["tool.sh"][1] & 0o100
    assert git("diff", "--cached", "--name-status", cwd=target) == staged
    assert "?? notes/todo.md" in git("status", "--porcelain", "--untracked-files=all", cwd=target)
    assert git("config", "branch.feature.merge", cwd=target) == "refs/heads/feature"
    # The transcript, with its subagent, under the project folder of the new checkout.
    folder = rig.host / ".claude" / "projects" / slug(target)
    moved = lines(folder / f"{SID}.jsonl")
    assert [item["cwd"] for item in moved] == [str(target), str(target / "notes")]
    # Only where the turns were recorded moves; what was said stays as it was said.
    assert moved[1]["message"]["content"][0]["text"] == f"Done in {rig.checkout}"
    assert lines(folder / SID / "subagents" / "agent-a1.jsonl")[0]["cwd"] == str(target)
    # Resumed there in its sandbox, on the same model, with the move explained first.
    store = worker_store(rig)
    [run] = store["runs"]
    assert f"--model opus --resume {SID} --" in run[1]
    [agent] = store["agents"]
    assert agent["agent_session"] == {"value": SID} and agent["cwd"] == str(target)
    prompt = agent["argv"][-1]
    assert "moved from" in prompt and "Mac mini" in prompt
    assert str(rig.checkout) in prompt and str(target) in prompt
    assert "Background jobs" in prompt and "pytest" in prompt
    assert prompt.rstrip().endswith("Continue the task.")
    assert result["pane"] == agent["pane_id"] and result["workspace_id"] == "mini/w1"
    assert result["stopped"] == ["/bin/zsh -c pytest -x", "python -m pytest -x"]
    # The agent here exited as a quit does, then its workspace, worktree and branch went.
    assert read(rig.state)["typed_text"] == [["w1:p2", "/exit"]]
    assert not rig.checkout.exists()
    assert "feature" not in git("branch", "--format=%(refname:short)", cwd=rig.clone).split()
    assert ["workspace", "close", "w1"] in read(rig.state)["calls"]
    assert result["source"]["removed"] and result["warnings"] == []
    # Staged files are taken there and never left behind.
    assert not list((rig.host / ".local/state/babysit-pr/moves").iterdir())
    assert rig.progress[0].startswith("Packing feature")


def test_an_idle_codex_moves_with_its_rollout(tmp_path, fake_tools, short_tmp, monkeypatch):
    rig = make_rig(tmp_path, fake_tools, short_tmp, monkeypatch, kind="codex")
    result = run_move(rig)
    target = rig.target
    rollout = rig.host / ".codex" / "sessions" / "2026" / "10" / "25" / rig.transcript.name
    meta, context, _ = lines(rollout)
    assert meta["payload"]["cwd"] == str(target) and context["payload"]["cwd"] == str(target)
    [run] = worker_store(rig)["runs"]
    # The model and effort its last turn ran with, which its command line did not name.
    assert "--model gpt-fixture -c 'model_reasoning_effort=\"high\"'" in run[1]
    assert f"--no-daemon resume {SID} --" in run[1]
    assert worker_store(rig)["agents"][0]["agent_session"] == {"value": SID}
    # Codex exits with Ctrl-D at its empty prompt.
    assert ["agent", "send-keys", "w1:p2", "ctrl+d"] in read(rig.state)["calls"]
    assert "typed_text" not in read(rig.state)
    assert result["source"]["removed"] and not rig.checkout.exists()


def test_a_working_agent_is_stopped_only_once_confirmed(rig, monkeypatch):
    # Esc is the first key the pane gets; its composer shows empty again after it.
    set_agent(rig, [CLAUDE_READY, CLAUDE_READY, CLAUDE_EXIT_TYPED], agent_status="working")
    found = workspace_move.plan(rig.home, request(), rig.placing)
    # The confirmation names what exiting it stops, its process tree from ps.
    assert found["confirm"] and found["status"] == "working"
    assert [c["pid"] for c in found["children"]] == [5000, 5001]
    assert found["machine"] == "mini" and found["error"] is None
    moves = workspace_move.Moves(rig.home, lambda: rig.placing, lambda: rig.overview)
    with pytest.raises(workspace_move.Refused) as refused:
        moves.action(request())
    assert refused.value.details["code"] == "confirm_working"
    assert "esc" not in json.dumps(read(rig.state)["calls"])
    real = agent_messages.herdr

    def herdr(*args, machine=None, timeout=30):
        if args[:2] == ("agent", "send-keys") and args[3] == "esc" and machine is None:
            # Esc stops the turn; the agent stays, idle.
            data = read(rig.state)
            data["calls"].append(list(args))
            data["agents"][0]["agent_status"] = "idle"
            rig.state.write_text(json.dumps(data))
            return {}
        return real(*args, machine=machine, timeout=timeout)

    monkeypatch.setattr(agent_messages, "herdr", herdr)
    result = run_move(rig, confirm_working=True)
    keys = [c[3] for c in read(rig.state)["calls"] if c[:3] == ["agent", "send-keys", "w1:p2"]]
    assert keys == ["esc", "enter"]
    assert result["stopped"] == ["/bin/zsh -c pytest -x", "python -m pytest -x"]
    assert rig.progress[0] == "Stopping Claude's turn"
    assert git("rev-parse", "HEAD", cwd=rig.target)


def test_a_move_through_the_dashboard_runs_in_the_background(rig):
    moves = workspace_move.Moves(rig.home, lambda: rig.placing, lambda: rig.overview)
    started = moves.action(request())["move"]
    assert started["status"] == "running"
    deadline = time.time() + 60
    while (op := moves.action({"id": started["id"]})["move"])["status"] == "running":
        assert time.time() < deadline
        time.sleep(0.1)
    assert op["status"] == "moved", op
    assert op["result"]["path"] == str(rig.target)
    assert "Resuming Claude on Mac mini" in op["steps"]
    saved = json.loads((rig.home / "moves" / f"{started['id']}.json").read_text())
    assert saved["status"] == "moved"
    with pytest.raises(ValueError, match="Unknown move"):
        moves.action({"id": "nope"})


# Refusals ------------------------------------------------------------------------------


def set_agent(rig, screens=None, **changes):
    data = read(rig.state)
    data["agents"][0].update(changes)
    if screens:
        data["screens"]["w1:p2"] = screens
    rig.state.write_text(json.dumps(data))


def refusal(rig, **changes):
    with pytest.raises(workspace_move.Refused) as refused:
        run_move(rig, **changes)
    return refused.value


def nothing_happened(rig):
    assert local_agents(rig) and rig.checkout.exists() and not rig.target.exists()
    assert "typed_text" not in read(rig.state)
    assert "runs" not in worker_store(rig)


def test_a_blocked_agent_is_refused(rig):
    set_agent(rig, [DIALOG], agent_status="blocked")
    assert refusal(rig).details["code"] == "blocked"
    nothing_happened(rig)


def test_a_draft_in_the_composer_is_refused(rig):
    set_agent(rig, [CLAUDE_DRAFT])
    assert "draft" in str(refusal(rig))
    nothing_happened(rig)


def test_another_agent_in_the_workspace_is_refused(rig):
    data = read(rig.state)
    data["agents"].append({**data["agents"][0], "pane_id": "w1:p3", "agent": "codex"})
    rig.state.write_text(json.dumps(data))
    assert refusal(rig).details["code"] == "others"
    nothing_happened(rig)


def test_a_full_target_is_refused_even_by_name(rig):
    rig.placing.extra = {"mini": {"agents": 4, "working": 0, "attempted_at": time.time()}}
    for machine in ("auto", "mini"):
        assert refusal(rig, machine=machine).details["code"] == "target"
    found = workspace_move.plan(rig.home, request(), rig.placing)
    assert found["machine"] is None and "full (4/4)" in json.dumps(found)
    nothing_happened(rig)


def test_a_target_without_the_needed_labels_is_refused(rig):
    config = json.loads((rig.home / "machines.json").read_text())
    config["repos"] = {"base/repo": {"requires": ["gpu"]}}
    (rig.home / "machines.json").write_text(json.dumps(config))
    assert "lacks gpu" in str(refusal(rig, machine="mini"))
    [option] = workspace_move.plan(rig.home, request(), rig.placing)["options"]
    assert option["disabled"] and option["note"] == "lacks gpu"
    nothing_happened(rig)


def test_this_machine_is_never_the_target(rig):
    assert "cannot take" in str(refusal(rig, machine="local"))
    with pytest.raises(ValueError, match="Choose a machine"):
        run_move(rig, machine="../x")
    nothing_happened(rig)


def test_a_changed_agent_is_refused(rig):
    assert refusal(rig, session=str(SID).replace("8", "9")).details["code"] == "changed"
    nothing_happened(rig)


# Failures --------------------------------------------------------------------------------


def test_what_the_worker_would_refuse_is_refused_before_any_key(rig):
    # The worker already has a branch of that name: a second, diverging one is refused.
    git("branch", "feature", cwd=rig.host / "src" / "repo")
    set_agent(rig, agent_status="working")
    before = files(rig.checkout)
    found = workspace_move.plan(rig.home, request(), rig.placing)
    assert "Branch feature already exists" in found["error"] and found["machine"] is None
    refused = refusal(rig, confirm_working=True)
    assert refused.details["code"] == "worker" and "Branch feature" in str(refused)
    nothing_happened(rig)
    assert "esc" not in json.dumps(read(rig.state)["calls"])
    assert files(rig.checkout) == before
    assert not (rig.host / ".local/state/babysit-pr/moves").exists()


def test_a_refused_exit_takes_the_workers_checkout_back(rig, monkeypatch):
    def exit_agent(request, home=None, **options):
        raise ValueError("Not quit: the composer holds a draft; clear it in Collie first")

    monkeypatch.setattr(agent_messages, "exit_agent", exit_agent)
    with pytest.raises(workspace_move.Failed, match="holds a draft") as failed:
        run_move(rig)
    assert failed.value.details["stage"] == "before_exit"
    nothing_happened(rig)
    branches = git("branch", "--format=%(refname:short)", cwd=rig.host / "src" / "repo")
    assert "feature" not in branches.split()


def test_a_failure_after_the_exit_keeps_the_source_checkout(rig, monkeypatch):
    def resume(*args, **kwargs):
        raise ValueError("Mac mini's herdr did not answer")

    monkeypatch.setattr(agent_messages, "resume", resume)
    before = files(rig.checkout)
    with pytest.raises(workspace_move.Failed, match="did not answer") as failed:
        run_move(rig)
    details = failed.value.details
    assert details["stage"] == "after_exit" and details["source_kept"]
    assert details["resume_command"] == f"cd {rig.checkout} && claude --resume {SID}"
    assert details["worker_path"] == str(rig.target)
    # The agent here exited; nothing of its checkout or branch went.
    assert not local_agents(rig)
    assert files(rig.checkout) == before
    assert "feature" in git("branch", "--format=%(refname:short)", cwd=rig.clone).split()
    assert ["workspace", "close", "w1"] not in read(rig.state)["calls"]


def test_an_unconfirmed_resume_keeps_the_source_checkout(rig, monkeypatch):
    monkeypatch.setattr(workspace_move.Move, "confirmed", lambda self, *args: None)
    with pytest.raises(workspace_move.Failed, match="check there before resuming") as failed:
        run_move(rig)
    assert failed.value.details["stage"] == "after_exit"
    assert "typed in w1:p1 on Mac mini" in str(failed.value)
    assert rig.checkout.exists() and rig.target.exists()


# Watches ---------------------------------------------------------------------------------


def add_watch(rig, status="awaiting_release"):
    db = pr_supervisor.open_db(rig.home)
    try:
        with db:
            pr_supervisor.save_job(
                db,
                {
                    "id": "0123456789abcdef",
                    "url": "https://github.com/base/repo/pull/7",
                    "repo": "base/repo",
                    "ci_repo": "base/repo",
                    "branch": None,
                    "machine": None,
                    "cwd": str(rig.checkout),
                    "session_id": SID,
                    "agent": "claude",
                    "status": status,
                    "summary": "Exit the original CLI, then release this watch",
                    "epoch": 3,
                    "instructions": "Keep CI green",
                    "on_green": None,
                    "poll_seconds": 120,
                    "max_repairs": 5,
                    "repair_timeout": 1800,
                },
            )
    finally:
        db.close()


def watch(rig):
    db = pr_supervisor.open_db(rig.home)
    try:
        return pr_supervisor.get_job(db, "0123456789abcdef")
    finally:
        db.close()


def test_a_watch_follows_only_to_a_linux_worker_and_a_clean_worktree(rig):
    add_watch(rig)
    found = workspace_move.plan(rig.home, request(), rig.placing)
    assert found["watch"] == {"id": "0123456789abcdef", "status": "awaiting_release"}
    assert "need a Linux worker" in found["error"]
    assert refusal(rig).details["code"] == "watch"
    nothing_happened(rig)


def test_a_watch_running_repairs_refuses_the_move(rig):
    add_watch(rig, status="watching")
    assert refusal(rig).details["code"] == "watch"


@pytest.fixture
def clean_box(tmp_path, fake_tools, short_tmp, monkeypatch):
    rig = make_rig(tmp_path, fake_tools, short_tmp, monkeypatch, dirty=False, worker=BOX)
    add_watch(rig)
    return rig


def test_a_watch_is_held_through_the_move_then_registered_on_the_worker(clean_box, monkeypatch):
    rig = clean_box
    held = []
    real = agent_messages.exit_agent

    def exit_agent(request, home=None, **options):
        held.append(watch(rig)["status"])
        return real(request, home, **options)

    registered = []

    def register_(db, args):
        registered.append(args)
        return {"id": "0123456789abcdef"}

    monkeypatch.setattr(agent_messages, "exit_agent", exit_agent)
    monkeypatch.setattr(pr_supervisor, "register", register_)
    result = run_move(rig)
    # Stopped while the agent left, so nothing resumed the session meanwhile.
    assert held == ["stopped"]
    [args] = registered
    assert (args.machine, args.cwd, args.session, args.pane) == (
        "box",
        str(rig.target),
        SID,
        result["pane"],
    )
    assert args.pr == "https://github.com/base/repo/pull/7" and args.claude_account == "default"
    assert result["watch"] == "0123456789abcdef"
    # babysit-bwrap on the Linux worker ran the resume, with its Claude home.
    [run] = worker_store(rig)["runs"]
    assert "babysit-bwrap" in run[1] and f"--resume {SID}" in run[1]
    assert "babysit watch 0123456789abcdef now follows" in worker_store(rig)["agents"][0]["task"]


def test_a_failed_move_gives_the_watch_back(clean_box, monkeypatch):
    rig = clean_box

    def resume(*args, **kwargs):
        raise ValueError("Linux box's herdr did not answer")

    monkeypatch.setattr(agent_messages, "resume", resume)
    with pytest.raises(workspace_move.Failed):
        run_move(rig)
    job = watch(rig)
    assert job["status"] == "awaiting_release" and job["summary"].startswith("Move stopped")


def test_a_watch_that_cannot_follow_is_left_stopped_and_said(clean_box, monkeypatch):
    rig = clean_box

    def register_(db, args):
        raise ValueError("PR not found or not watchable from this worker")

    monkeypatch.setattr(pr_supervisor, "register", register_)
    result = run_move(rig)
    assert result["watch"] is None
    assert "stays stopped" in result["warnings"][0]
    assert watch(rig)["status"] == "stopped"
    assert result["source"]["removed"]


# Pieces ------------------------------------------------------------------------------------


def test_only_recorded_working_directories_move_in_a_transcript():
    old = ["/a/wt", "/private/a/wt"]
    data = (
        b'{"cwd": "/a/wt/sub", "message": "in /a/wt"}\n'
        b'{"type": "session_meta", "payload": {"cwd": "/private/a/wt"}}\n'
        b'{"cwd": "/a/wtx"}\nnot json\n{"cwd": "/a/wt"}'
    )
    moved = workspace_move.rewrite(data, old, "/b/wt").split(b"\n")
    assert json.loads(moved[0]) == {"cwd": "/b/wt/sub", "message": "in /a/wt"}
    assert json.loads(moved[1])["payload"]["cwd"] == "/b/wt"
    assert moved[2] == b'{"cwd": "/a/wtx"}' and moved[3] == b"not json"
    assert moved[4] == b'{"cwd":"/b/wt"}'


def test_children_are_the_agents_process_tree():
    listing = "\n".join(" ".join(map(str, row)) for row in CHILDREN) + "\n  bad line\n"
    found = workspace_move.descendants(listing, 4242)
    assert found == [
        {"pid": 5000, "command": "/bin/zsh -c pytest -x"},
        {"pid": 5001, "command": "python -m pytest -x"},
    ]
    assert workspace_move.descendants(listing, 6000) == []


def test_launch_settings_come_from_the_command_line_and_are_checked():
    settings = workspace_move.settings
    assert settings(["claude", "--model=opus", "--effort", "high"], "claude") == {
        "model": "opus",
        "effort": "high",
    }
    codex = ["codex", "-m", "o4", "-c", 'model_reasoning_effort="low"']
    assert settings(codex, "codex") == {"model": "o4", "effort": "low"}
    assert settings(["claude", "--model", "$(boom)", "--effort", "x"], "claude") == {
        "model": "",
        "effort": "",
    }


def test_the_receive_probe_refuses_files_not_staged_for_a_move(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    with pytest.raises(ValueError, match="Moves are taken only from"):
        remote_probe._host_move_receive(
            "~/src/repo",
            "base/repo",
            "~/src/worktrees/repo",
            "x",
            "x",
            "/etc/passwd",
            "0" * 32,
            "0" * 40,
            "0" * 40,
            "0" * 40,
            None,
            [],
            "claude",
            "default",
            "safehouse",
        )
    assert Path("/etc/passwd").exists()


def test_the_session_probe_refuses_paths_out_of_the_store(tmp_path, monkeypatch):
    import io
    import tarfile

    monkeypatch.setenv("HOME", str(tmp_path))
    folder = tmp_path / ".local/state/babysit-pr/moves"
    folder.mkdir(parents=True)
    for name in ("../escape.jsonl", f"{SID}/../../x.jsonl", ".hidden.jsonl"):
        archive = folder / "s.tar"
        with tarfile.open(archive, "w") as bundle:
            info = tarfile.TarInfo(name)
            info.size = 2
            bundle.addfile(info, io.BytesIO(b"{}"))
        with pytest.raises(ValueError, match="Not a transcript file"):
            remote_probe._host_move_session(
                str(archive.resolve()), "claude", "default", "safehouse", str(tmp_path), None
            )
        assert not archive.exists()
    assert not (tmp_path / "escape.jsonl").exists()


def test_the_receive_answer_is_validated():
    good = {
        "path": "/w/x",
        "branch": "feature",
        "head": "a" * 40,
        "index_tree": "b" * 40,
        "work_tree": "c" * 40,
        "clone": "/w/repo",
    }
    assert remote_probe.validate_move_receive(good) == good
    for change in ({"path": "/w/\nx"}, {"head": "HEAD"}, {"extra": 1}):
        with pytest.raises(ValueError):
            remote_probe.validate_move_receive({**good, **change})


def test_the_command_line_plans_then_follows_the_move_through_the_dashboard(monkeypatch):
    plan = {
        "agent": "claude",
        "pane": "w1:p2",
        "session": SID,
        "status": "working",
        "branch": "feature",
        "changed": 1,
        "untracked": 0,
        "unpushed": 2,
        "children": [{"pid": 5000, "command": "pytest -x"}],
        "error": None,
        "machine": "box",
        "label": "Linux box",
        "reason": "auto: least busy, 3/4 free",
        "confirm": True,
    }
    sent = []
    follows = [
        {"id": "m1", "status": "running", "message": "Copying"},
        {"id": "m1", "status": "moved", "message": "Moved to Linux box", "result": {}},
    ]

    def post(base, action, body):
        assert (base, action) == ("http://127.0.0.1:9", "workspace-move")
        sent.append(body)
        if body.get("plan"):
            return {"plan": plan}
        if "id" in body:
            return {"move": follows.pop(0)}
        return {"move": {"id": "m1", "status": "running", "message": "Stopping"}}

    monkeypatch.setattr(workspace_move, "post", post)
    monkeypatch.setattr(workspace_move.time, "sleep", lambda seconds: None)
    args = SimpleNamespace(
        workspace="w1", pane=None, machine="auto", yes=False, plan=False, dashboard=None
    )
    monkeypatch.setenv("BABYSIT_DASHBOARD_URL", "http://127.0.0.1:9/")
    said = io.StringIO()
    with pytest.raises(ValueError, match="pass --yes"):
        workspace_move.cli(args, stream=said)
    assert len(sent) == 1 and "stops: pytest -x" in said.getvalue()
    args.yes = True
    done = workspace_move.cli(args, stream=said)
    assert done["move"]["status"] == "moved" and "Copying" in said.getvalue()
    assert sent[2] == {
        "workspace": "w1",
        "pane": "w1:p2",
        "session": SID,
        "machine": "box",
        "confirm_working": True,
    }
    assert sent[3:] == [{"id": "m1"}, {"id": "m1"}]


def test_a_checkout_written_after_its_copy_is_kept(rig, monkeypatch):
    real = workspace_move.Move.confirmed

    def confirmed(self, *args):
        # Something left in the pane writes there while the worker's agent starts.
        (rig.checkout / "late.txt").write_text("written after the copy\n")
        return real(self, *args)

    monkeypatch.setattr(workspace_move.Move, "confirmed", confirmed)
    result = run_move(rig)
    assert not result["source"]["removed"] and rig.checkout.exists()
    assert "changed after it was copied" in result["warnings"][0]
    assert (rig.checkout / "late.txt").exists()
    assert ["workspace", "close", "w1"] not in read(rig.state)["calls"]


def test_an_exit_that_raises_once_the_agent_left_keeps_both_checkouts(rig, monkeypatch):
    real = agent_messages.exit_agent

    def exit_agent(request, home=None, **options):
        real(request, home, **options)
        raise RuntimeError("herdr could not close the pane")

    monkeypatch.setattr(agent_messages, "exit_agent", exit_agent)
    with pytest.raises(workspace_move.Failed, match="could not be confirmed") as failed:
        run_move(rig)
    details = failed.value.details
    assert details["stage"] == "exit" and details["worker_path"] == str(rig.target)
    assert details["resume_command"].endswith(f"claude --resume {SID}")
    assert rig.checkout.exists() and rig.target.exists()


def test_a_nested_repository_is_refused(rig):
    subprocess.check_call(["git", "init", "-q", str(rig.checkout / "vendored")])
    # A fresh repository has no identity of its own, and CI has no global one.
    git("config", "user.name", "Fixture", cwd=rig.checkout / "vendored")
    git("config", "user.email", "fixture@example.test", cwd=rig.checkout / "vendored")
    git("config", "commit.gpgsign", "false", cwd=rig.checkout / "vendored")
    (rig.checkout / "vendored" / "f").write_text("x\n")
    git("add", "f", cwd=rig.checkout / "vendored")
    git("commit", "-q", "-m", "inner", cwd=rig.checkout / "vendored")
    assert refusal(rig).details["code"] == "nested"
    nothing_happened(rig)


def test_the_session_probe_takes_a_longer_copy_of_the_same_conversation(tmp_path, monkeypatch):
    import tarfile

    monkeypatch.setenv("HOME", str(tmp_path))
    folder = tmp_path / ".local/state/babysit-pr/moves"
    folder.mkdir(parents=True)

    def place(data):
        archive = folder / "s.tar"
        with tarfile.open(archive, "w") as bundle:
            info = tarfile.TarInfo(f"{SID}.jsonl")
            info.size = len(data)
            bundle.addfile(info, io.BytesIO(data))
        return remote_probe._host_move_session(
            str(archive.resolve()), "claude", "default", "safehouse", str(tmp_path), None
        )["transcript"]

    first = place(b'{"a":1}\n')
    assert place(b'{"a":1}\n{"b":2}\n') == first
    assert Path(first).read_bytes() == b'{"a":1}\n{"b":2}\n'
    with pytest.raises(ValueError, match="already exists here"):
        place(b'{"other":1}\n')


# Review findings ---------------------------------------------------------------------------


def test_ignored_files_that_are_not_rebuildable_keep_the_worktree(
    tmp_path, fake_tools, short_tmp, monkeypatch
):
    rig = make_rig(tmp_path, fake_tools, short_tmp, monkeypatch, precious=True)
    found = workspace_move.plan(rig.home, request(), rig.placing)
    # Named up front: .venv is rebuildable, debug.log is not.
    assert found["ignored_kept"] == ["debug.log"] and found["ignored_kept_count"] == 1
    result = run_move(rig)
    assert not result["source"]["removed"]
    assert "files Git ignores, not copied: debug.log" in result["warnings"][0]
    # The workspace closed; the worktree, its ignored file and its branch stay.
    assert ["workspace", "close", "w1"] in read(rig.state)["calls"]
    assert (rig.checkout / "debug.log").read_text() == "ignored, and not rebuildable\n"
    assert "feature" in git("branch", "--format=%(refname:short)", cwd=rig.clone).split()
    assert not (rig.target / "debug.log").exists()


def test_the_checkout_is_checked_again_after_its_workspace_closed(rig, monkeypatch):
    real = rig.overview.close_workspaces

    def close_workspaces(*args, **kwargs):
        real(*args, **kwargs)
        # A shell of the closing workspace wrote one last file.
        (rig.checkout / "last.txt").write_text("written as the panes closed\n")

    monkeypatch.setattr(rig.overview, "close_workspaces", close_workspaces)
    result = run_move(rig)
    assert not result["source"]["removed"] and (rig.checkout / "last.txt").exists()
    assert "changed after it was copied" in result["warnings"][0]


def test_a_failing_cleanup_after_the_move_is_a_warning(rig, monkeypatch):
    def clean_source(self, src):
        raise RuntimeError("cleanup thread died")

    monkeypatch.setattr(workspace_move.Move, "clean_source", clean_source)
    result = run_move(rig)
    assert result["machine"] == "mini" and rig.checkout.exists()
    assert "its cleanup failed (cleanup thread died)" in result["warnings"][0]


def test_another_pane_running_a_program_is_refused(rig):
    data = read(rig.state)
    data["process_info"]["w1:p1"] = {
        "shell_pid": 90,
        "foreground_processes": [{"pid": 91, "argv0": "/usr/bin/vim", "argv": ["vim"]}],
    }
    rig.state.write_text(json.dumps(data))
    refused = refusal(rig)
    assert refused.details["code"] == "panes" and refused.details["panes"] == ["vim in w1:p1"]
    nothing_happened(rig)


def test_a_pane_root_that_is_no_shell_is_refused_before_esc(rig):
    data = read(rig.state)
    data["agents"][0]["agent_status"] = "working"
    data["commands"] = {"100": "bash -c claude"}
    rig.state.write_text(json.dumps(data))
    assert "not an interactive shell" in str(refusal(rig, confirm_working=True))
    nothing_happened(rig)
    assert "esc" not in json.dumps(read(rig.state)["calls"])


def test_an_agent_busy_again_is_never_stopped_unconfirmed(rig, monkeypatch):
    real = workspace_move.Move.send_code

    def send_code(self, src, state):
        made = real(self, src, state)
        set_agent(rig, agent_status="working")  # A message woke it meanwhile.
        return made

    monkeypatch.setattr(workspace_move.Move, "send_code", send_code)
    with pytest.raises(workspace_move.Failed, match="may not be stopped") as failed:
        run_move(rig)
    assert failed.value.details["stage"] == "before_exit"
    assert local_agents(rig) and not rig.target.exists()
    assert "esc" not in json.dumps(read(rig.state)["calls"])


def test_actions_on_an_agent_under_a_move_are_refused_at_once():
    import threading

    keys = [("checkout", "/w/x"), ("session", SID), ("pane", "w1:p2")]
    holding, done = threading.Event(), threading.Event()

    def move():
        with agent_messages.reserved(*keys):
            # Its own actions on them go ahead.
            with agent_messages.locked(("pane", "w1:p2")):
                holding.set()
                done.wait(10)

    thread = threading.Thread(target=move)
    thread.start()
    try:
        assert holding.wait(10)
        target = {"pane": "w1:p2", "status": "idle", "agent": "claude"}
        with pytest.raises(ValueError, match="move of this agent"):
            agent_messages.prompt(target, "hello")
        with pytest.raises(ValueError, match="move of this agent"):
            with agent_messages.locked(("checkout", "/w/x"), ("session", SID)):
                pass
        with pytest.raises(ValueError, match="move of this agent"):
            with agent_messages.reserved(*keys):
                pass
        with agent_messages.locked(("pane", "w9:p1")):
            pass  # Another agent's actions are unaffected.
    finally:
        done.set()
        thread.join(10)
    with agent_messages.locked(*keys):
        pass  # Released with the move.


def test_a_lone_surrogate_in_a_transcript_survives_its_copy():
    line = b'{"type":"user","cwd":"/a/b","message":{"content":"x\\ud83d y"}}\n'
    moved = workspace_move.rewrite(line, ["/a/b"], "/c/d")
    item = json.loads(moved)
    assert item["cwd"] == "/c/d" and item["message"]["content"] == "x\ud83d y"
    assert moved.endswith(b"\n") and moved.isascii()


def test_a_session_started_below_the_checkout_keeps_its_project_folder(
    tmp_path, fake_tools, short_tmp, monkeypatch
):
    rig = make_rig(tmp_path, fake_tools, short_tmp, monkeypatch, subdir="frontend")
    result = run_move(rig)
    folder = rig.host / ".claude" / "projects" / (slug(rig.target) + "-frontend")
    moved = lines(folder / f"{SID}.jsonl")
    assert moved[0]["cwd"] == str(rig.target / "frontend")
    # Resumed where it was started, below the new checkout.
    [run] = worker_store(rig)["runs"]
    assert run[1].startswith(f"cd {rig.target / 'frontend'} && ")
    assert worker_store(rig)["agents"][0]["agent_session"] == {"value": SID}
    assert result["source"]["removed"]


def test_a_restarted_dashboard_marks_a_running_move_interrupted(rig):
    add_watch(rig)
    db = pr_supervisor.open_db(rig.home)
    try:
        with db:
            job = pr_supervisor.get_job(db, "0123456789abcdef")
            job.update(status="stopped", summary="Moving with its workspace to Mac mini")
            pr_supervisor.save_job(db, job)
    finally:
        db.close()
    folder = rig.home / "moves"
    folder.mkdir()
    (folder / f"{MOVED}.json").write_text(
        json.dumps(
            {
                "id": MOVED,
                "status": "running",
                "pane": "w1:p2",
                "session": SID,
                "message": "Copying the conversation to Mac mini",
                "steps": [],
                "started_at": 1,
                "stage": "after_exit",
                "watch": "0123456789abcdef",
                "resume_command": f"cd {rig.checkout} && claude --resume {SID}",
            }
        )
    )
    moves = workspace_move.Moves(rig.home, lambda: rig.placing, lambda: rig.overview)
    op = moves.action({"id": MOVED})["move"]
    assert op["status"] == "interrupted"
    assert "at: Copying the conversation to Mac mini" in op["message"]
    assert op["result"]["resume_command"].endswith(f"claude --resume {SID}")
    assert watch(rig)["status"] == "awaiting_release"
    assert json.loads((folder / f"{MOVED}.json").read_text())["status"] == "interrupted"


def test_git_lfs_is_refused(rig):
    (rig.checkout / ".gitattributes").write_text("*.bin filter=lfs diff=lfs merge=lfs -text\n")
    git("add", ".gitattributes", cwd=rig.checkout)
    git("commit", "-q", "-m", "lfs", cwd=rig.checkout)
    assert refusal(rig).details["code"] == "lfs"
    nothing_happened(rig)


def test_a_plan_never_takes_the_live_index_lock(rig):
    index = Path(
        git("rev-parse", "--path-format=absolute", "--git-path", "index", cwd=rig.checkout)
    )
    lock = index.with_name("index.lock")
    lock.write_text("")  # The agent is staging something right now.
    before = index.stat().st_mtime_ns
    try:
        found = workspace_move.plan(rig.home, request(), rig.placing)
    finally:
        lock.unlink()
    assert found["machine"] == "mini" and index.stat().st_mtime_ns == before


def test_the_session_probe_takes_only_a_project_folder_suffix(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    folder = tmp_path / ".local/state/babysit-pr/moves"
    folder.mkdir(parents=True)
    archive = folder / "s.tar"
    for suffix in ("/../x", "-a/b", "x"):
        archive.write_bytes(b"")  # Taken and removed by each refusal.
        with pytest.raises(ValueError, match="project folder suffix"):
            remote_probe._host_move_session(
                str(archive.resolve()),
                "claude",
                "default",
                "safehouse",
                str(tmp_path),
                None,
                suffix,
            )


def test_saved_moves_this_dashboard_did_not_write_are_skipped(rig, capsys, monkeypatch):
    folder = rig.home / "moves"
    folder.mkdir()
    good = {"id": MOVED, "status": "moved", "started_at": 1.0, "pane": "w1:p9", "session": SID}
    (folder / f"{MOVED}.json").write_text(json.dumps(good))
    other = "cd" * 16
    bad = {
        "missing.json": {"id": "ef" * 16},  # No status or start.
        "typed.json": {"id": "01" * 16, "status": 3, "started_at": "now"},
        "escape.json": {"id": "../../escape", "status": "running", "started_at": 1},
        f"{other}.json": {"id": "23" * 16, "status": "moved", "started_at": 1},  # Not its name.
    }
    for name, op in bad.items():
        (folder / name).write_text(json.dumps(op))
    moves = workspace_move.Moves(rig.home, lambda: rig.placing, lambda: rig.overview)
    assert set(moves.ops) == {MOVED}
    assert capsys.readouterr().err.count("Skipped unreadable saved move") == 4
    assert not (rig.home.parent / "escape.json").exists()
    # Starting and pruning still work beside what was read.
    finished = threading.Event()
    run = moves.run

    def complete(*args):
        try:
            run(*args)
        finally:
            finished.set()

    monkeypatch.setattr(moves, "run", complete)
    started = moves.action(request())["move"]
    assert started["status"] == "running" and started["id"] in moves.ops
    deadline = time.time() + 60
    while moves.action({"id": started["id"]})["move"]["status"] == "running":
        assert time.time() < deadline
        time.sleep(0.1)
    # Terminal status precedes the worker's final save; wait before removing its state.
    assert finished.wait(60)
    with pytest.raises(ValueError, match="only under its own ID"):
        moves.save({"id": "../../escape"})
    assert not (rig.home.parent / "escape.json").exists()


def test_a_pane_busy_since_the_move_began_keeps_the_workspace(rig, monkeypatch):
    real = workspace_move.Move.confirmed

    def confirmed(self, *args):
        found = real(self, *args)
        # The reader opened an editor beside the agent while it moved.
        data = read(rig.state)
        data["process_info"]["w1:p1"] = {
            "shell_pid": 90,
            "foreground_processes": [{"pid": 91, "argv0": "/usr/bin/vim", "argv": ["vim"]}],
        }
        rig.state.write_text(json.dumps(data))
        return found

    monkeypatch.setattr(workspace_move.Move, "confirmed", confirmed)
    result = run_move(rig)
    assert result["machine"] == "mini" and not result["source"]["removed"]
    assert "vim in w1:p1 started there during the move" in result["warnings"][0]
    assert ["workspace", "close", "w1"] not in read(rig.state)["calls"]
    assert rig.checkout.exists()
