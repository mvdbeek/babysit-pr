"""Moving a worker's agent back to this machine: its branch, conversation and cleanup.

The worker is ``<tmp>/hosts/<ssh target>``, which the fake ssh uses as HOME, with a
clone, a worktree on the branch and the agent's transcript, and its herdr state under
its label in the fake herdr's store. This machine is ``<tmp>``, with its own clone of the
same repository made before the branch moved on there. Real Git runs on both; herdr,
ssh, ps, Claude and Codex are fakes.
"""

import gzip
import hashlib
import io
import json
import subprocess
import sys
import tarfile
import time
from types import SimpleNamespace

import agent_messages
import machines
import pr_supervisor
import pytest
import remote_probe
import workspace_move
import workspace_overview as wso
from test_remote_inventory import git, make_clone, read, register, short_tmp
from test_workspace_move import (
    BOX,
    CHILDREN,
    CLAUDE_DRAFT,
    CLAUDE_EXIT_TYPED,
    CLAUDE_READY,
    CODEX_READY,
    DIALOG,
    MOVED,
    PS,
    SID,
    Placing,
    files,
    jsonl,
    lines,
    slug,
)
from test_workspace_overview import FakeGitHub

__all__ = ["short_tmp"]  # A fixture, used by name.

PR = "https://github.com/base/repo/pull/7"
# A watch's ID, as registering it names it: from its pull request.
WATCH = hashlib.sha256(PR.encode()).hexdigest()[:16]
MOVING = agent_messages.MOVING


def make_home_rig(tmp_path, fake_tools, short_tmp, monkeypatch, kind="claude", dirty=True):
    tmp = tmp_path.resolve()
    monkeypatch.setenv("HOME", str(tmp))
    for name in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "BABYSIT_DASHBOARD_URL"):
        monkeypatch.delenv(name, raising=False)
    worker = BOX
    host = tmp / "hosts" / worker["ssh"]
    there = host / "src" / "repo"
    checkout = make_clone(there, ["feature"]) / "feature"
    # This machine's own clone of the repository, made before the branch moved on there.
    clone = tmp / "src" / "repo"
    subprocess.check_call(["git", "clone", "-q", str(there), str(clone)])
    git("remote", "set-url", "origin", "https://github.com/fork/repo.git", cwd=clone)
    git("remote", "add", "upstream", "https://github.com/base/repo.git", cwd=clone)
    # Commits only the worker has: the branch is ahead of every remote branch.
    (checkout / ".gitignore").write_text("*.log\n.venv/\n")
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
    # Ignored and rebuildable: neither copied nor a reason to keep the worker's checkout.
    (checkout / ".venv" / "bin").mkdir(parents=True)
    (checkout / ".venv" / "bin" / "python").write_text("#!/bin/sh\n")
    if kind == "claude":
        folder = host / ".claude" / "projects" / slug(checkout)
        (folder / SID / "subagents").mkdir(parents=True)
        transcript = folder / f"{SID}.jsonl"
        transcript.write_text(
            jsonl(
                {
                    "type": "user",
                    "sessionId": SID,
                    "cwd": str(checkout),
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
        day = host / ".codex" / "sessions" / "2026" / "10" / "25"
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
        # The worker's processes: its pane's login shell, the agent and what it started.
        ps=[[100, 1, "-zsh"], *CHILDREN],
        machines={
            worker["label"]: {
                "workspaces": [
                    {
                        "workspace_id": "w1",
                        "label": "feature",
                        "agent_status": "idle",
                        "worktree": {"repo_root": str(there), "checkout_path": str(checkout)},
                    }
                ],
                "agents": [
                    {
                        "workspace_id": "w1",
                        "pane_id": "w1:p2",
                        "agent": kind,
                        "agent_status": "idle",
                        "agent_session": {"value": SID},
                        "cwd": str(checkout),
                    }
                ],
                "panes": [
                    {"pane_id": "w1:p1", "workspace_id": "w1"},
                    {"pane_id": "w1:p2", "workspace_id": "w1"},
                ],
                "process_info": {
                    "w1:p2": {
                        "shell_pid": 100,
                        "foreground_processes": [{"pid": 4242, "argv": argv, "cwd": str(checkout)}],
                    }
                },
                "screens": {"w1:p2": screens},
                "calls": [],
            }
        },
    )
    fake_tools.write_text(json.dumps(store))
    ps = fake_tools.parent / "bin" / "ps"
    ps.write_text(f"#!{sys.executable} -S\n" + PS)
    ps.chmod(0o700)
    # The worker's probe lists its processes with the system's ps: here, the fake.
    shipped = remote_probe.probe_source()
    assert shipped.count(b'PS = "/bin/ps"') == 1
    patched = shipped.replace(b'PS = "/bin/ps"', f"PS = {str(ps)!r}".encode())
    monkeypatch.setattr(remote_probe, "probe_source", lambda: patched)
    monkeypatch.setattr(remote_probe, "PS", str(ps))
    monkeypatch.setattr(workspace_move, "CONFIRM_WAIT", 2)
    monkeypatch.setattr(workspace_move, "CLEANUP_POLL", 0.05)
    home = short_tmp / "state"
    (home / "experiments" / "workspaces").mkdir(parents=True)
    (home / "experiments" / "workspaces" / "config.json").write_text(
        json.dumps({"enabled": True, "src": str(tmp / "src")})
    )
    register(home, worker)
    return SimpleNamespace(
        tmp=tmp,
        home=home,
        state=fake_tools,
        clone=clone,
        there=there,
        checkout=checkout,
        host=host,
        worker=worker,
        transcript=transcript,
        kind=kind,
        placing=Placing(home),
        overview=wso.WorkspaceOverview(home, FakeGitHub()),
        target=tmp / "src" / "worktrees" / "repo" / "feature",
        progress=[],
    )


@pytest.fixture
def rig(tmp_path, fake_tools, short_tmp, monkeypatch):
    return make_home_rig(tmp_path, fake_tools, short_tmp, monkeypatch)


def request(**changes):
    return {
        "workspace": "box/w1",
        "pane": "w1:p2",
        "session": SID,
        "machine": "local",
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


def worker_agents(rig):
    return [a for a in worker_store(rig)["agents"] if a.get("pane_id") == "w1:p2"]


def set_agent(rig, screens=None, **changes):
    data = read(rig.state)
    there = data["machines"][rig.worker["label"]]
    there["agents"][0].update(changes)
    if screens:
        there["screens"]["w1:p2"] = screens
    rig.state.write_text(json.dumps(data))


def refusal(rig, **changes):
    with pytest.raises(workspace_move.Refused) as refused:
        run_move(rig, **changes)
    return refused.value


def nothing_happened(rig):
    assert worker_agents(rig) and rig.checkout.exists() and not rig.target.exists()
    assert "typed_text" not in worker_store(rig)
    assert "runs" not in read(rig.state)
    assert "feature" not in git("branch", "--format=%(refname:short)", cwd=rig.clone).split()


def staged(rig):
    """What either machine still holds of the move's staged files: this one stages under
    the dashboard's home, never in its HOME's state folder."""
    assert not (rig.tmp / ".local/state/babysit-pr/move-staging").exists()
    found = []
    for folder in (rig.home / "move-staging", rig.host / ".local/state/babysit-pr/move-staging"):
        found += list(folder.iterdir()) if folder.exists() else []
    return found


# The move ----------------------------------------------------------------------------


def test_a_workers_claude_moves_here_with_its_branch_changes_and_conversation(rig):
    before = files(rig.checkout)
    head = git("rev-parse", "HEAD", cwd=rig.checkout)
    staged_before = git("diff", "--cached", "--name-status", cwd=rig.checkout)
    found = workspace_move.plan(rig.home, request(), rig.placing)
    assert (found["source"], found["source_label"]) == ("box", "Linux box")
    assert found["machine"] == "local" and found["error"] is None
    assert [option["id"] for option in found["options"]] == ["local"]
    assert found["worker"]["path"] == str(rig.target)
    assert [c["pid"] for c in found["children"]] == [5000, 5001]

    result = run_move(rig)

    target = rig.target
    assert result["path"] == str(target) and result["machine"] == "local"
    assert git("rev-parse", "HEAD", cwd=target) == head
    assert git("symbolic-ref", "--short", "HEAD", cwd=target) == "feature"
    # Unpushed commits, staged, unstaged and untracked changes; ignored files stay there.
    expected = {k: v for k, v in before.items() if not k.startswith(".venv/")}
    assert files(target) == expected
    assert files(target)["tool.sh"][1] & 0o100
    assert git("diff", "--cached", "--name-status", cwd=target) == staged_before
    assert git("config", "branch.feature.merge", cwd=target) == "refs/heads/feature"
    # The transcript, with its subagent, under this machine's project folder for it.
    folder = rig.tmp / ".claude" / "projects" / slug(target)
    moved = lines(folder / f"{SID}.jsonl")
    assert [item["cwd"] for item in moved] == [str(target), str(target / "notes")]
    assert moved[1]["message"]["content"][0]["text"] == f"Done in {rig.checkout}"
    assert lines(folder / SID / "subagents" / "agent-a1.jsonl")[0]["cwd"] == str(target)
    # Resumed here in a workspace of its own, on the same model, the move explained first.
    store = read(rig.state)
    [run] = store["runs"]
    assert f"--model opus --resume {SID} --" in run[1]
    [agent] = [a for a in store["agents"] if a.get("agent_session") == {"value": SID}]
    assert agent["cwd"] == str(target) and result["pane"] == agent["pane_id"]
    prompt = agent["argv"][-1]
    assert "moved from Linux box to" in prompt and str(rig.checkout) in prompt
    assert "stopped on Linux box" in prompt and "pytest" in prompt
    assert prompt.rstrip().endswith("Continue the task.")
    assert result["workspace_id"] == agent["pane_id"].split(":")[0]
    assert result["stopped"] == ["/bin/zsh -c pytest -x", "python -m pytest -x"]
    # The worker's agent exited as a quit does, then its workspace, worktree and branch went.
    there = worker_store(rig)
    assert there["typed_text"] == [["w1:p2", "/exit"]]
    assert ["agent", "send-keys", "w1:p2", "enter"] in there["calls"]
    assert ["workspace", "close", "w1"] in there["calls"]
    assert not rig.checkout.exists()
    assert "feature" not in git("branch", "--format=%(refname:short)", cwd=rig.there).split()
    assert result["source"] == {
        "machine": "box",
        "label": "Linux box",
        "path": str(rig.checkout),
        "removed": True,
        "message": "Cleaned up",
    }
    assert result["warnings"] == []
    # Staged files are taken on both machines and never left behind.
    assert staged(rig) == []


def test_a_workers_codex_moves_here_with_its_rollout_and_last_settings(
    tmp_path, fake_tools, short_tmp, monkeypatch
):
    rig = make_home_rig(tmp_path, fake_tools, short_tmp, monkeypatch, kind="codex")
    result = run_move(rig)
    rollout = rig.tmp / ".codex" / "sessions" / "2026" / "10" / "25" / rig.transcript.name
    meta, context, _ = lines(rollout)
    assert meta["payload"]["cwd"] == str(rig.target) == context["payload"]["cwd"]
    [run] = read(rig.state)["runs"]
    # The model and effort its last turn ran with, read from the rollout it brought.
    assert "--model gpt-fixture" in run[1] and "model_reasoning_effort" in run[1]
    assert f"--no-daemon resume {SID} --" in run[1]
    # Codex exits with Ctrl-D at its empty prompt, on the worker.
    assert ["agent", "send-keys", "w1:p2", "ctrl+d"] in worker_store(rig)["calls"]
    assert result["source"]["removed"] and not rig.checkout.exists()


def test_a_working_agent_there_is_stopped_only_once_confirmed(rig, monkeypatch):
    set_agent(rig, [CLAUDE_READY, CLAUDE_READY, CLAUDE_EXIT_TYPED], agent_status="working")
    found = workspace_move.plan(rig.home, request(), rig.placing)
    assert found["confirm"] and found["status"] == "working"
    assert refusal(rig).details["code"] == "confirm_working"
    assert "esc" not in json.dumps(worker_store(rig)["calls"])
    real = agent_messages.herdr

    def herdr(*args, machine=None):
        # Esc ends the worker's turn; the fake's agent goes idle rather than away.
        if args[:2] == ("agent", "send-keys") and args[3] == "esc":
            set_agent(rig, agent_status="idle")
            data = read(rig.state)
            data["machines"][rig.worker["label"]]["calls"].append(list(args))
            rig.state.write_text(json.dumps(data))
            return {}
        return real(*args, machine=machine)

    monkeypatch.setattr(agent_messages, "herdr", herdr)
    result = run_move(rig, confirm_working=True)
    keys = [c[3] for c in worker_store(rig)["calls"] if c[:3] == ["agent", "send-keys", "w1:p2"]]
    assert keys == ["esc", "enter"]
    assert rig.progress[0] == "Stopping Claude's turn"
    assert result["path"] == str(rig.target)


def test_actions_on_the_workers_agent_are_refused_while_it_moves(rig, monkeypatch):
    import threading

    refused = []
    real = agent_messages.exit_agent

    def elsewhere(machine):
        # Another dashboard thread, acting on the worker's keys and on this machine's.
        try:
            agent_messages.interrupt({"workspace": "w1", "pane": "w1:p2", "session": SID}, machine)
        except ValueError as exc:
            refused.append(str(exc))
        try:
            with agent_messages.locked(("checkout", f"box:{rig.checkout}")):
                pass
        except ValueError as exc:
            refused.append(str(exc))
        with agent_messages.locked(("pane", "w1:p2")):
            refused.append("this machine's w1:p2 is another pane")

    def exit_agent(request, home=None, **options):
        thread = threading.Thread(target=elsewhere, args=(options["machine"],))
        thread.start()
        thread.join(10)
        return real(request, home, **options)

    monkeypatch.setattr(agent_messages, "exit_agent", exit_agent)
    run_move(rig)
    assert refused == [MOVING, MOVING, "this machine's w1:p2 is another pane"]


def test_a_move_here_runs_in_the_background_through_the_dashboard(rig):
    moves = workspace_move.Moves(rig.home, lambda: rig.placing, lambda: rig.overview)
    started = moves.action(request())["move"]
    assert started["status"] == "running" and started["source_label"] == "Linux box"
    deadline = time.time() + 60
    while (op := moves.action({"id": started["id"]})["move"])["status"] == "running":
        assert time.time() < deadline
        time.sleep(0.1)
    assert op["status"] == "moved", op
    assert op["result"]["path"] == str(rig.target)
    assert "Exiting Claude on Linux box" in op["steps"]
    assert "Removing the workspace on Linux box" in op["steps"]


# Refusals ------------------------------------------------------------------------------


def test_a_blocked_agent_there_is_refused(rig):
    set_agent(rig, [DIALOG], agent_status="blocked")
    assert refusal(rig).details["code"] == "blocked"
    nothing_happened(rig)


def test_a_draft_in_the_workers_composer_is_refused(rig):
    set_agent(rig, [CLAUDE_DRAFT])
    assert refusal(rig).details["code"] == "draft"
    nothing_happened(rig)


def test_another_agent_in_the_workers_workspace_is_refused(rig):
    data = read(rig.state)
    there = data["machines"][rig.worker["label"]]
    there["agents"].append({**there["agents"][0], "pane_id": "w1:p3", "agent": "codex"})
    rig.state.write_text(json.dumps(data))
    assert refusal(rig).details["code"] == "others"
    nothing_happened(rig)


def test_a_pane_root_there_that_is_no_shell_is_refused(rig):
    data = read(rig.state)
    data["ps"][0] = [100, 1, "/bin/zsh -c 'claude'"]
    rig.state.write_text(json.dumps(data))
    assert refusal(rig).details["code"] == "root"
    nothing_happened(rig)


def test_a_detached_head_there_is_refused(rig):
    git("checkout", "-q", "--detach", cwd=rig.checkout)
    assert refusal(rig).details["code"] == "detached"
    nothing_happened(rig)


def test_a_nested_repository_and_lfs_there_are_refused(rig):
    nested = rig.checkout / "vendor" / "lib"
    nested.mkdir(parents=True)
    git("init", "-q", cwd=nested)
    git(
        "-c",
        "user.name=x",
        "-c",
        "user.email=x@x",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "x",
        cwd=nested,
    )
    assert refusal(rig).details["code"] == "nested"
    subprocess.check_call(["rm", "-rf", str(rig.checkout / "vendor")])
    (rig.checkout / ".gitattributes").write_text("*.bin filter=lfs diff=lfs merge=lfs -text\n")
    assert refusal(rig).details["code"] == "lfs"
    nothing_happened(rig)


def test_the_workers_own_clone_checkout_is_refused(rig):
    data = read(rig.state)
    space = data["machines"][rig.worker["label"]]["workspaces"][0]
    space["worktree"]["checkout_path"] = str(rig.there)
    data["machines"][rig.worker["label"]]["agents"][0]["cwd"] = str(rig.there)
    rig.state.write_text(json.dumps(data))
    # Its session is recorded in the clone's own checkout, which is not movable.
    folder = rig.host / ".claude" / "projects" / slug(rig.there)
    folder.mkdir(parents=True)
    (folder / f"{SID}.jsonl").write_text(
        rig.transcript.read_text().replace(str(rig.checkout), str(rig.there))
    )
    assert refusal(rig).details["code"] == "main"


def test_a_branch_already_here_is_refused_before_any_key(rig):
    git("branch", "feature", cwd=rig.clone)
    set_agent(rig, agent_status="working")
    found = workspace_move.plan(rig.home, request(), rig.placing)
    assert "Branch feature already exists" in found["error"] and found["machine"] is None
    refused = refusal(rig, confirm_working=True)
    assert refused.details["code"] == "worker" and "Branch feature" in str(refused)
    assert "esc" not in json.dumps(worker_store(rig)["calls"])
    assert worker_agents(rig) and not rig.target.exists() and staged(rig) == []


def test_a_claude_account_missing_here_is_refused(rig):
    there = rig.host / ".claude" / "accounts" / "work"
    there.mkdir(parents=True)
    old = rig.host / ".claude" / "projects"
    (there / "projects").parent.mkdir(parents=True, exist_ok=True)
    old.rename(there / "projects")
    found = workspace_move.plan(rig.home, request(), rig.placing)
    assert "Unknown Claude account work on this machine" in found["error"]
    assert refusal(rig).details["code"] == "worker"
    nothing_happened(rig)


def test_an_agent_there_only_moves_here(rig):
    with pytest.raises(ValueError, match="moves to this machine only"):
        run_move(rig, machine="box")
    nothing_happened(rig)


def test_a_full_controller_is_refused(rig):
    rig.placing.extra = {"local": {"agents": 20, "working": 0, "max_agents": 20}}
    assert refusal(rig).details["code"] == "target"
    found = workspace_move.plan(rig.home, request(), rig.placing)
    assert found["machine"] is None and "full (20/20)" in found["error"]
    nothing_happened(rig)


def test_a_watch_running_repairs_there_refuses_the_move(rig):
    add_watch(rig, status="watching")
    assert refusal(rig).details["code"] == "watch"
    nothing_happened(rig)


# Failures --------------------------------------------------------------------------------


def test_a_failure_before_the_exit_keeps_the_worker_agent_and_removes_the_copy(rig, monkeypatch):
    def exit_agent(request, home=None, **options):
        assert rig.target.exists()  # Made and checked here before the worker was asked.
        raise ValueError("Not quit: the composer holds a draft; clear it in Collie first")

    monkeypatch.setattr(agent_messages, "exit_agent", exit_agent)
    with pytest.raises(workspace_move.Failed, match="holds a draft") as failed:
        run_move(rig)
    assert failed.value.details["stage"] == "before_exit"
    assert failed.value.details["source_label"] == "Linux box"
    nothing_happened(rig)
    assert staged(rig) == []


def test_a_failure_after_the_exit_keeps_the_workers_checkout(rig, monkeypatch):
    def resume(*args, **kwargs):
        raise ValueError("herdr here did not answer")

    monkeypatch.setattr(agent_messages, "resume", resume)
    before = files(rig.checkout)
    with pytest.raises(workspace_move.Failed, match="did not answer") as failed:
        run_move(rig)
    details = failed.value.details
    assert details["stage"] == "after_exit" and details["source_kept"]
    # babysit-bwrap's default login is in its CLAUDE_CONFIG_DIR.
    assert details["resume_command"] == (
        f'ssh -t box.test \'cd {rig.checkout} && CLAUDE_CONFIG_DIR="$HOME/.claude" '
        f"claude --resume {SID}'"
    )
    # What stays here is named, for a next move to be possible.
    assert f"The new checkout {rig.target} (branch feature) stays on" in str(failed.value)
    assert details["worker_path"] == str(rig.target)
    # The worker's agent exited; nothing of its checkout or branch went.
    assert not worker_agents(rig)
    assert files(rig.checkout) == before
    assert "feature" in git("branch", "--format=%(refname:short)", cwd=rig.there).split()
    assert ["workspace", "close", "w1"] not in worker_store(rig)["calls"]


def test_a_checkout_there_written_after_its_copy_is_kept(rig, monkeypatch):
    real = workspace_move.Move.confirmed

    def confirmed(self, *args):
        (rig.checkout / "late.txt").write_text("written after the copy\n")
        return real(self, *args)

    monkeypatch.setattr(workspace_move.Move, "confirmed", confirmed)
    result = run_move(rig)
    assert not result["source"]["removed"] and rig.checkout.exists()
    assert "changed after it was copied" in result["warnings"][0]
    assert "Linux box" in result["warnings"][0]


def test_an_unconfirmed_resume_here_never_advises_removing_its_checkout(rig, monkeypatch):
    monkeypatch.setattr(workspace_move.Move, "confirmed", lambda self, *args: None)
    saved = {}
    move = workspace_move.Move(
        rig.home, request(), rig.placing, rig.overview, rig.progress.append, saved.update
    )
    with pytest.raises(workspace_move.Failed, match="check there before resuming") as failed:
        move.run()
    assert failed.value.details["stage"] == "after_exit"
    assert "remove it" not in str(failed.value) and rig.target.exists()
    assert saved["resumed"] is True and saved["target_path"] == str(rig.target)


def test_a_restart_after_the_resume_says_the_agent_may_run_in_the_new_checkout(rig):
    folder = rig.home / "moves"
    folder.mkdir()
    for move_id, stage, resumed in (("ab" * 16, "resumed", False), ("cd" * 16, "after_exit", True)):
        (folder / f"{move_id}.json").write_text(
            json.dumps(
                {
                    "id": move_id,
                    "status": "running",
                    "pane": "w1:p2",
                    "session": SID,
                    "message": "Resuming Claude on this Mac",
                    "steps": [],
                    "started_at": 1,
                    "source_label": "Linux box",
                    "stage": stage,
                    "resumed": resumed,
                    "target_path": str(rig.target),
                    "target_label": "this Mac",
                    "branch": "feature",
                }
            )
        )
    moves = workspace_move.Moves(rig.home, lambda: rig.placing, lambda: rig.overview)
    for move_id in ("ab" * 16, "cd" * 16):
        said = moves.action({"id": move_id})["move"]["message"]
        assert f"The agent may be running in {rig.target} on this Mac" in said
        assert "remove it" not in said


def test_ignored_files_there_that_are_not_rebuildable_keep_the_workers_worktree(rig):
    (rig.checkout / "debug.log").write_text("ignored, and not rebuildable\n")
    found = workspace_move.plan(rig.home, request(), rig.placing)
    assert found["ignored_kept"] == ["debug.log"] and found["ignored_kept_count"] == 1
    result = run_move(rig)
    assert files(rig.target)  # Moved all the same.
    assert not result["source"]["removed"] and rig.checkout.exists()
    assert result["warnings"] == [
        "The worktree on Linux box was kept for files Git ignores, not copied: debug.log"
    ]
    # Its workspace closed; the worktree stays for what was not copied.
    assert ["workspace", "close", "w1"] in worker_store(rig)["calls"]
    assert (rig.checkout / "debug.log").exists()


def test_a_change_there_while_the_agent_exits_is_sent_again(rig, monkeypatch):
    real = agent_messages.exit_agent

    def exit_agent(request, home=None, **options):
        done = real(request, home, **options)
        (rig.checkout / "late.txt").write_text("written as it exited\n")
        return done

    monkeypatch.setattr(agent_messages, "exit_agent", exit_agent)
    result = run_move(rig)
    assert "The checkout changed while the agent exited; sending it again" in rig.progress
    # This move's own checkout here was brought up to date, not refused as already there.
    assert (rig.target / "late.txt").read_text() == "written as it exited\n"
    assert result["source"]["removed"] and staged(rig) == []


def test_a_failed_fetch_of_the_conversation_keeps_the_workers_checkout(rig, monkeypatch):
    real = machines.get_file

    def get_file(home, m, directory, name, **options):
        if name.endswith(".tar.gz"):
            raise ValueError("ssh: connection to box.test lost")
        return real(home, m, directory, name, **options)

    monkeypatch.setattr(machines, "get_file", get_file)
    before = files(rig.checkout)
    with pytest.raises(workspace_move.Failed, match="connection to box.test lost") as failed:
        run_move(rig)
    details = failed.value.details
    assert details["stage"] == "after_exit" and details["resume_command"].startswith("ssh -t")
    assert not worker_agents(rig) and files(rig.checkout) == before
    # The pack it could not fetch is not left on the worker for a day.
    assert staged(rig) == []


def test_files_only_this_machine_ignores_still_move(rig, monkeypatch):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    (rig.tmp / ".config" / "git").mkdir(parents=True)
    (rig.tmp / ".config" / "git" / "ignore").write_text("notes/\n")
    result = run_move(rig)
    assert (rig.target / "notes" / "todo.md").read_text() == "untracked\n"
    assert result["source"]["removed"]


def test_a_gitattributes_that_is_not_utf8_there_is_no_lfs(rig):
    (rig.checkout / ".gitattributes").write_bytes(b"# caf\xe9\n*.txt text\n")
    found = workspace_move.plan(rig.home, request(), rig.placing)
    assert found["error"] is None and found["machine"] == "local"


# Watches ---------------------------------------------------------------------------------


def add_watch(rig, status="awaiting_release"):
    db = pr_supervisor.open_db(rig.home)
    try:
        with db:
            pr_supervisor.save_job(
                db,
                {
                    "id": WATCH,
                    "url": PR,
                    "repo": "base/repo",
                    "ci_repo": "base/repo",
                    "branch": None,
                    "machine": "box",
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
        return pr_supervisor.get_job(db, WATCH)
    finally:
        db.close()


@pytest.fixture
def clean_rig(tmp_path, fake_tools, short_tmp, monkeypatch):
    return make_home_rig(tmp_path, fake_tools, short_tmp, monkeypatch, dirty=False)


def test_a_watch_follows_here_only_from_a_clean_worktree(rig):
    add_watch(rig)
    found = workspace_move.plan(rig.home, request(), rig.placing)
    assert found["watch"] == {"id": WATCH, "status": "awaiting_release"}
    assert "needs a clean worktree" in found["error"] and found["machine"] is None
    assert refusal(rig).details["code"] == "watch"
    nothing_happened(rig)


def test_a_workers_watch_is_held_through_the_move_then_registered_here(clean_rig, monkeypatch):
    rig = clean_rig
    add_watch(rig)
    head = git("rev-parse", "HEAD", cwd=rig.checkout)
    # The pull request as GitHub has it: the moved branch at its head.
    monkeypatch.setenv(
        "FAKE_GH_PR",
        json.dumps(
            {
                "url": PR,
                "number": 7,
                "state": "OPEN",
                "headRefName": "feature",
                "headRefOid": head,
                "headRepository": {"name": "repo"},
                "headRepositoryOwner": {"login": "fork"},
            }
        ),
    )
    held = []
    real = agent_messages.exit_agent

    def exit_agent(request, home=None, **options):
        held.append(watch(rig)["status"])
        return real(request, home, **options)

    monkeypatch.setattr(agent_messages, "exit_agent", exit_agent)
    result = run_move(rig)
    assert held == ["stopped"]
    # Registered here for real: this machine's checkout, session and pane.
    job = watch(rig)
    assert result["watch"] == WATCH and result["warnings"] == []
    assert (job["machine"], job["cwd"], job["session_id"]) == (None, str(rig.target), SID)
    assert job["status"] == "awaiting_release" and job["pane"]["pane_id"] == result["pane"]
    [agent] = [a for a in read(rig.state)["agents"] if a.get("agent_session") == {"value": SID}]
    assert f"babysit watch {WATCH} now follows" in agent["task"]


def test_a_failed_move_here_gives_the_workers_watch_back(clean_rig, monkeypatch):
    rig = clean_rig
    add_watch(rig)

    def resume(*args, **kwargs):
        raise ValueError("herdr here did not answer")

    monkeypatch.setattr(agent_messages, "resume", resume)
    with pytest.raises(workspace_move.Failed):
        run_move(rig)
    job = watch(rig)
    assert job["status"] == "awaiting_release" and job["summary"].startswith("Move stopped")
    assert job["machine"] == "box"


def test_a_restarted_dashboard_says_where_to_resume_an_interrupted_move_here(rig):
    add_watch(rig)
    db = pr_supervisor.open_db(rig.home)
    try:
        with db:
            job = pr_supervisor.get_job(db, WATCH)
            job.update(status="stopped", summary="Moving with its workspace to this Mac")
            pr_supervisor.save_job(db, job)
    finally:
        db.close()
    folder = rig.home / "moves"
    folder.mkdir()
    command = f"ssh -t box.test 'cd {rig.checkout} && claude --resume {SID}'"
    (folder / f"{MOVED}.json").write_text(
        json.dumps(
            {
                "id": MOVED,
                "status": "running",
                "workspace": "box/w1",
                "pane": "w1:p2",
                "session": SID,
                "message": "Copying the conversation to this Mac",
                "steps": [],
                "started_at": 1,
                "source_label": "Linux box",
                "stage": "after_exit",
                "watch": WATCH,
                "resume_command": command,
                "target_path": str(rig.target),
                "target_label": "this Mac",
                "branch": "feature",
            }
        )
    )
    moves = workspace_move.Moves(rig.home, lambda: rig.placing, lambda: rig.overview)
    op = moves.action({"id": MOVED})["move"]
    assert op["status"] == "interrupted"
    assert "The agent had left Linux box" in op["message"]
    assert op["result"]["resume_command"] == command
    assert op["result"]["source_label"] == "Linux box"
    assert f"The new checkout {rig.target} (branch feature) stays on this Mac" in op["message"]
    assert watch(rig)["status"] == "awaiting_release"


# The worker's ops ------------------------------------------------------------------------


def test_the_source_probe_answers_the_facts_a_move_checks(rig, monkeypatch):
    monkeypatch.setenv("HOME", str(rig.host))
    facts = remote_probe._host_move_source(str(rig.checkout), str(rig.transcript), SID, "claude")
    assert remote_probe.validate_move_source(json.loads(json.dumps(facts))) == facts
    assert facts["path"] == str(rig.checkout) and not facts["main"]
    assert facts["branch"] == "feature" and facts["changes"]["untracked"] == 1
    assert facts["base"] == ["refs/remotes/origin/main", "fork/repo", "main"]
    assert facts["upstream"][:3] == ["origin", "feature", "fork/repo"]
    assert facts["session_size"] == sum(
        f.stat().st_size for f in rig.transcript.parent.rglob("*") if f.is_file()
    )
    # A transcript outside this machine's stores is never read.
    with pytest.raises(ValueError, match="Not a session transcript"):
        remote_probe._host_move_source(
            str(rig.checkout), str(rig.checkout / "lib.py"), SID, "claude"
        )


def test_the_bundle_and_pack_probes_stage_only_what_they_are_asked(rig, monkeypatch):
    monkeypatch.setenv("HOME", str(rig.host))
    state = remote_probe.move_snapshot(remote_probe.host_git, str(rig.checkout))
    args = {
        "path": str(rig.checkout),
        "move": "ab" * 16,
        "head": state["head"],
        "index_tree": state["index_tree"],
        "work_tree": state["work_tree"],
    }
    for base in ("refs/heads/main", "refs/remotes/upstream/../x", "--all"):
        with pytest.raises(ValueError, match="upstream or origin branch|base branch"):
            remote_probe._host_move_bundle(**args, base=base)
    with pytest.raises(ValueError, match="move ID"):
        remote_probe._host_move_bundle(**{**args, "move": "../x"}, base=None)
    made = remote_probe._host_move_bundle(**args, base="refs/remotes/origin/main")
    assert remote_probe.validate_staged(made) == made
    folder = rig.host / ".local/state/babysit-pr/move-staging"
    assert [f.name for f in folder.iterdir()] == [made["name"]]
    assert git("show-ref", cwd=rig.checkout).count("refs/babysit-move") == 0
    with pytest.raises(ValueError, match="session ID"):
        remote_probe._host_move_pack(str(rig.transcript), "../x", "claude")
    packed = remote_probe._host_move_pack(str(rig.transcript), SID, "claude")
    names = [name for name, _ in workspace_move.unpacked((folder / packed["name"]).read_bytes())]
    assert names == [f"{SID}.jsonl", f"{SID}/subagents/agent-a1.jsonl"]


def test_the_processes_probe_validates_its_ids(monkeypatch):
    for value in (0, 1, -5, "100", True, 2**31):
        with pytest.raises(ValueError, match="process ID"):
            remote_probe._host_processes(pid=value)
    with pytest.raises(ValueError, match="process ID"):
        remote_probe._host_processes()
    bad = [{"pid": 1, "ppid": 0, "command": "x\x1b[2J"}]
    with pytest.raises(ValueError, match="children"):
        remote_probe.validate_processes({"shell": None, "children": bad})


def test_malformed_source_and_staged_answers_are_refused():
    good = {
        **remote_probe.MOVE_NO_FACTS,
        "ignored": [],
        "path": "/w/repo",
        "main": False,
        "session_size": 1,
    }
    assert remote_probe.validate_move_source(dict(good)) == good
    for change in (
        {"path": "relative"},
        {"branch": "feature\x1b"},
        {"head": "zz"},
        {"base": ["refs/heads/main", "base/repo", "main"]},
        {"upstream": ["origin", "../x", "fork/repo", None]},
        {"ignored": ["a\nb"], "ignored_count": 1},
        {"ignored_count": -1},
        {"extra": 1},
    ):
        with pytest.raises(ValueError, match="Malformed"):
            remote_probe.validate_move_source({**good, **change})
    for staged_answer in (
        {"name": "../x", "size": 1, "sha256": "0" * 64},
        {"name": ".hidden", "size": 1, "sha256": "0" * 64},
        {"name": "x.bundle", "size": -1, "sha256": "0" * 64},
        {"name": "x.bundle", "size": 1, "sha256": "nope"},
    ):
        with pytest.raises(ValueError, match="Malformed"):
            remote_probe.validate_staged(staged_answer)


def test_a_staged_file_is_fetched_only_by_its_name_and_within_its_limit(rig):
    machine = machines.resolve(rig.home, "box")
    folder = rig.host / ".local/state/babysit-pr/move-staging"
    folder.mkdir(parents=True, mode=0o700)
    (folder / "big.bundle").write_bytes(b"x" * 100)
    for name in ("../etc/passwd", "-x", ".hidden"):
        with pytest.raises(ValueError, match="Invalid staged file name"):
            machines.get_file(rig.home, machine, remote_probe.MOVE_STATE, name, limit=10)
    with pytest.raises(ValueError, match="larger than 10 bytes"):
        machines.get_file(rig.home, machine, remote_probe.MOVE_STATE, "big.bundle", limit=10)
    (folder / "link.bundle").symlink_to(folder / "big.bundle")
    with pytest.raises(ValueError, match="not a staged file"):
        machines.get_file(rig.home, machine, remote_probe.MOVE_STATE, "link.bundle", limit=200)
    data = machines.get_file(rig.home, machine, remote_probe.MOVE_STATE, "big.bundle", limit=200)
    assert data == b"x" * 100 and not (folder / "big.bundle").exists()


def test_a_pack_with_an_unexpected_member_is_refused():
    import io
    import tarfile

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        info = tarfile.TarInfo("../escape.jsonl")
        info.size = 1
        archive.addfile(info, io.BytesIO(b"x"))
    with pytest.raises(ValueError, match="unexpected file"):
        workspace_move.unpacked(buffer.getvalue())


def test_the_command_line_names_a_workers_workspace(monkeypatch):
    posts = []

    def post(base, action, body):
        posts.append(body)
        if body.get("plan"):
            return {
                "plan": {
                    "agent": "claude",
                    "pane": "w1:p2",
                    "session": SID,
                    "status": "idle",
                    "branch": "feature",
                    "changed": 0,
                    "untracked": 0,
                    "unpushed": 1,
                    "children": [],
                    "error": None,
                    "label": "this Mac",
                    "reason": "this Mac, 19/20 free",
                    "machine": "local",
                    "worker": {"clone": "/c", "path": "/c/f"},
                    "confirm": False,
                }
            }
        return {
            "move": {
                "id": MOVED,
                "status": "failed",
                "message": "x",
                "error": "herdr here did not answer",
                "result": {
                    "resume_command": f"ssh -t box.test 'cd /w && claude --resume {SID}'",
                    "source_label": "Linux box",
                },
            }
        }

    monkeypatch.setattr(workspace_move, "post", post)
    out = []
    args = SimpleNamespace(
        dashboard="http://dash",
        workspace="box/w1",
        pane=None,
        machine="local",
        plan=False,
        yes=False,
    )
    stream = SimpleNamespace(write=out.append, flush=lambda: None)
    with pytest.raises(ValueError, match="did not answer"):
        workspace_move.cli(args, stream)
    assert posts[0] == {"workspace": "box/w1", "pane": None, "machine": "local", "plan": True}
    assert posts[1]["machine"] == "local"
    assert any("Resume it on Linux box: ssh -t box.test" in line for line in out)


def test_the_dashboard_passes_a_workers_workspace_to_the_move(rig):
    """Other agent actions refuse a qualified workspace they cannot act on; a move takes it."""
    import threading

    import dashboard
    from test_dashboard_http import get

    server = dashboard.DashboardServer(rig.home, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    seen = []
    server.moves = SimpleNamespace(action=lambda body: seen.append(body) or {"plan": None})
    try:
        for action in ("workspace-move", "workspace-quit"):
            status, _, body = get(
                server,
                f"/api/{action}",
                {"X-Babysit-Action": action, "Content-Type": "application/json"},
                method="POST",
                body=json.dumps({"workspace": "box/w1", "pane": "w1:p2", "session": SID}),
            )
            if action == "workspace-move":
                assert status == 200, body
            else:
                assert status == 400 and "cannot be opened" in json.loads(body)["error"]
    finally:
        server.shutdown()
        thread.join(5)
        server.server_close()
    assert seen == [{"workspace": "box/w1", "pane": "w1:p2", "session": SID}]


def tarball(*members):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for info, data in members:
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def member(name, size=0, **pax):
    info = tarfile.TarInfo(name)
    info.size = size
    info.pax_headers = pax
    return info


def test_a_pack_is_never_inflated_past_a_conversations_size(monkeypatch):
    monkeypatch.setattr(remote_probe, "MAX_MOVE_SESSION", 4096)
    monkeypatch.setattr(workspace_move, "PACK_MARGIN", 4096)
    # A few kB that inflate to a MB.
    bomb = tarball((member(f"{SID}.jsonl", 1 << 20), b"\0" * (1 << 20)))
    assert len(bomb) < 4096
    with pytest.raises(ValueError, match="more than a conversation"):
        workspace_move.unpacked(bomb)
    # Not inflated whole either when it is no tar at all.
    with pytest.raises(ValueError, match="more than a conversation|unreadable"):
        workspace_move.unpacked(gzip.compress(b"\0" * (1 << 20)))


def test_a_pack_with_too_many_members_or_extended_headers_is_refused(monkeypatch):
    monkeypatch.setattr(workspace_move, "MAX_PACKED", 5)
    many = tarball(*[(member(f"{SID}/f{n}.jsonl"), b"") for n in range(6)])
    with pytest.raises(ValueError, match="unexpected file"):
        workspace_move.unpacked(many)
    extended = tarball((member(f"{SID}.jsonl", comment="x" * 100), b""))
    with pytest.raises(ValueError, match="unexpected file"):
        workspace_move.unpacked(extended)
    assert workspace_move.unpacked(tarball((member(f"{SID}.jsonl", 2), b"{}"))) == [
        (f"{SID}.jsonl", b"{}")
    ]


def test_a_staged_file_swapped_after_its_check_is_never_read(tmp_path, monkeypatch):
    folder = tmp_path / "staging"
    folder.mkdir(mode=0o700)
    monkeypatch.setenv(remote_probe.MOVE_STAGING, str(folder))
    staged_file = folder / "x.tar.gz"
    staged_file.write_bytes(b"staged")
    with remote_probe._staged_move(str(staged_file.resolve())) as opened:
        staged_file.unlink()
        staged_file.write_bytes(b"swapped in")
        assert opened.read() == b"staged"
    (folder / "link.tar.gz").symlink_to(staged_file)
    with pytest.raises(ValueError, match="not a file this user staged"):
        remote_probe._staged_move(str((folder / "link.tar.gz").absolute()))
    with pytest.raises(ValueError, match="Moves are taken only from"):
        remote_probe._staged_move(str(tmp_path / "x.tar.gz"))


def test_this_machines_probe_stages_in_the_home_and_fetches_with_its_token(tmp_path, monkeypatch):
    seen = {}

    def run(home, machine, argv, **options):
        seen.update(machine=machine, env=options["env"])
        answer = {"protocol": remote_probe.PROTOCOL, "result": {"path": "/x"}}
        return subprocess.CompletedProcess(argv, 0, json.dumps(answer).encode(), b"")

    monkeypatch.setattr(machines, "run", run)
    monkeypatch.setenv("GH_TOKEN", "gho_" + "a" * 36)
    remote_probe.call(tmp_path, None, "realpath", 10, path="/x")
    assert seen["machine"] is None
    assert seen["env"][remote_probe.MOVE_STAGING] == str(tmp_path.resolve() / "move-staging")
    # The dashboard's token, for the fetch alone: no other child gets GH_TOKEN.
    assert seen["env"]["BABYSIT_FETCH_TOKEN"] == "gho_" + "a" * 36
    assert "GH_TOKEN" not in seen["env"]
    calls = []

    def host_run(argv, timeout, env=None, **options):
        calls.append(env)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(remote_probe, "_host_run", host_run)
    monkeypatch.setattr(remote_probe, "_host_fetch_refusals", lambda clone, timeout: None)
    monkeypatch.setattr(remote_probe, "host_guard", lambda path, **options: [])
    monkeypatch.setenv("BABYSIT_FETCH_TOKEN", "gho_" + "b" * 36)
    remote_probe._host_fetch("/c", "base/repo", "main", "upstream")
    assert calls == [{"GH_TOKEN": "gho_" + "b" * 36}]
    assert "BABYSIT_FETCH_TOKEN" not in remote_probe.host_environment()


def test_a_worker_drops_only_what_moves_staged_in_its_old_folder(rig, monkeypatch):
    monkeypatch.setenv("HOME", str(rig.host))
    old = rig.host / ".local/state/babysit-pr/moves"
    old.mkdir(parents=True)
    bundle_name, record = f"{'ab' * 16}-0123abcd.bundle", f"{'ab' * 16}.json"
    for name in (bundle_name, f"{'cd' * 16}-session.tar.gz", record, "notes.txt"):
        (old / name).write_text("x")
    state = remote_probe.move_snapshot(remote_probe.host_git, str(rig.checkout))
    args = {k: state[k] for k in ("head", "index_tree", "work_tree")}
    remote_probe._host_move_bundle(str(rig.checkout), "ef" * 16, base=None, **args)
    assert sorted(f.name for f in old.iterdir()) == [record, "notes.txt"]
    # This machine's probe never touches it: there it is the dashboard's record of moves.
    (old / bundle_name).write_text("x")
    monkeypatch.setenv(remote_probe.MOVE_STAGING, str(rig.host / "staging"))
    remote_probe._host_move_bundle(str(rig.checkout), "ef" * 16, base=None, **args)
    assert (old / bundle_name).exists()
