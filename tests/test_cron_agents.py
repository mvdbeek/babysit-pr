"""Agent cron jobs against temporary clones, fake herdr and fake agents."""

import json
import time

import cron_agents
import cron_jobs
import pytest
import workspace_exit
import wt
from cron_jobs import CronJobs
from test_pr_workspaces import local  # noqa: F401 (fixture)


@pytest.fixture
def setup(local):  # noqa: F811
    manager, _, git, state, _ = local
    jobs = CronJobs(manager.home, shell=["/bin/sh", "-c"], workspaces=manager)
    yield jobs, manager, git, state
    jobs.close()


def herdr_state(state):
    return json.loads(state.read_text())


def request(manager, **changes):
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
        **changes,
    }


def settle(jobs, job_id, statuses=("starting",)):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        run = jobs.snapshot()["jobs"][0]["runs"][0]
        if run["status"] not in statuses:
            return run
        time.sleep(0.05)
    raise AssertionError("The run did not settle")


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"repo": "nope"}, "Choose a repository"),
        ({"clone": "/elsewhere"}, "local clone of base/repo"),
        ({"branch": "-bad"}, "Name the job's branch"),
        ({"base": "main@{1}"}, "base branch"),
        ({"agent": "gpt"}, "Codex or Claude"),
        ({"effort": "extreme"}, "effort"),
        ({"prompt": " "}, "prompt of 1"),
        ({"docker": "yes"}, "docker"),
        ({"unsandboxed": "yes"}, "unsandboxed"),
        ({"unsandboxed": 1}, "unsandboxed"),
        ({"docker": True, "unsandboxed": True}, "only applies inside Safehouse"),
        ({"kind": "robot"}, "shell command or an agent task"),
    ],
)
def test_invalid_agent_jobs_are_refused(setup, changes, message):
    jobs, manager, _, _ = setup
    with pytest.raises(ValueError, match=message):
        jobs.save(request(manager, **changes))


def test_agent_jobs_need_workspace_actions(tmp_path):
    jobs = CronJobs(tmp_path, shell=["/bin/sh", "-c"])
    with pytest.raises(ValueError, match="workspace actions"):
        jobs.save({"kind": "agent", "name": "x", "schedule": {"every": 60}})
    assert jobs.snapshot()["agents"] is False
    jobs.close()


def test_a_run_makes_the_worktree_and_starts_the_agent_in_a_new_pane(setup):
    jobs, manager, git, state = setup
    saved = jobs.save(request(manager, model="fixture-codex", effort="high", docker=True))
    assert saved["kind"] == "agent" and "command" not in saved
    assert jobs.snapshot()["agents"] is True
    run = jobs.run_now(saved["id"])
    assert run["status"] == "starting"
    run = settle(jobs, saved["id"])
    assert run["status"] == "running", run
    assert run["exit"]["state"] == "watching"
    path = manager.src / "worktrees" / "repo" / "nightly"
    launched = run["agent_run"]
    assert launched["path"] == str(path.resolve())
    assert git("rev-parse", "HEAD", cwd=path) == git("rev-parse", "feature")
    assert git("rev-parse", "--abbrev-ref", "HEAD", cwd=path) == "nightly"
    data = herdr_state(state)
    [space] = data["workspaces"]
    assert launched["workspace_id"] == space["workspace_id"]
    assert launched["url"].endswith(f"/space/{space['workspace_id']}")
    [opened] = [c for c in data["calls"] if c[:2] == ["worktree", "open"]]
    assert opened[opened.index("--label") + 1] == "cron-Nightly-triage"
    [split] = data["splits"]
    assert split == [
        f"{space['workspace_id']}:p1",
        "--direction",
        "right",
        "--cwd",
        str(path.resolve()),
        "--no-focus",
    ]
    [typed] = data["runs"]
    assert typed[0] == launched["pane"]
    assert typed[1].startswith(
        "SAFE_ENABLE=docker codex -c check_for_update_on_startup=false --model fixture-codex -c "
    )
    # The agent got the prompt with the request to confirm completion.
    [agent] = data["agents"]
    assert agent["pane_id"] == launched["pane"]
    assert agent["task"] == "Triage new issues\n" + workspace_exit.brief(
        launched["exit_marker"]
    ).rstrip("\n")
    assert jobs.log(run["id"])["text"].startswith(f"Codex started in {path.resolve()}\n")
    snapshot = jobs.snapshot()["jobs"][0]
    assert snapshot["running"] is True
    # A started agent keeps the job busy: the next start is skipped, not stacked.
    jobs.tick()
    with pytest.raises(ValueError, match="already running"):
        jobs.run_now(saved["id"])
    with pytest.raises(ValueError, match="finish in Collie|not running"):
        jobs.stop(saved["id"])


def test_a_job_left_on_default_effort_uses_the_saved_default(setup):
    import workspace_agents

    jobs, manager, _, state = setup
    workspace_agents.save_effort_default(manager.home, {"repo": "base/repo", "effort": "ultra"})
    saved = jobs.save(request(manager, model="fixture-codex"))
    assert saved["effort"] == ""
    jobs.run_now(saved["id"])
    assert settle(jobs, saved["id"])["status"] == "running"
    [typed] = herdr_state(state)["runs"]
    assert typed[1].startswith(
        "codex -c check_for_update_on_startup=false --model fixture-codex"
        " -c 'model_reasoning_effort=\"ultra\"'"
    )


@pytest.mark.parametrize(
    ("changes", "typed", "flag"),
    [
        ({"agent": "claude"}, "command claude --dangerously-skip-permissions ", None),
        (
            {"model": "fixture-codex"},
            "command codex --dangerously-bypass-approvals-and-sandbox "
            "-c check_for_update_on_startup=false --model fixture-codex ",
            None,
        ),
        (
            {"agent": "claude", "claude_account": "work"},
            "(unset ANTHROPIC_API_KEY ",
            "CLAUDE_CONFIG_DIR=",
        ),
    ],
)
def test_an_unsandboxed_job_types_the_agent_without_its_shell_wrapper(setup, changes, typed, flag):
    import claude_accounts

    jobs, manager, _, state = setup
    account = claude_accounts.account_home("work", create=True)
    saved = jobs.save(request(manager, unsandboxed=True, **changes))
    assert saved["unsandboxed"] is True and saved["docker"] is False
    jobs.run_now(saved["id"])
    run = settle(jobs, saved["id"])
    assert run["status"] == "running" and run["unsandboxed"] is True
    [(_, command)] = herdr_state(state)["runs"]
    assert command.startswith(typed)
    agent = changes.get("agent", "codex")
    assert f"command {agent} {wt.UNSANDBOXED_FLAGS[agent]} " in command
    if flag:
        assert f"{flag}{account} " in command
    # The typed line really runs the agent, with the flag the wrapper would have added.
    [started] = herdr_state(state)["agents"]
    assert started["agent"] == agent
    assert started["argv"][0] == wt.UNSANDBOXED_FLAGS[agent]
    if flag:
        assert started["claude_config_dir"] == str(account)


def test_a_job_saved_before_the_option_existed_stays_in_safehouse(setup):
    jobs, manager, _, state = setup
    saved = jobs.save(request(manager, agent="claude"))
    assert saved["unsandboxed"] is False
    # As stored by an older dashboard: no unsandboxed key at all.
    with jobs.db() as db:
        job = jobs.jobs(db)[0]
        del job["unsandboxed"]
        db.execute("UPDATE jobs SET data=? WHERE id=?", (json.dumps(job), job["id"]))
    assert "unsandboxed" not in jobs.snapshot()["jobs"][0]
    jobs.run_now(saved["id"])
    run = settle(jobs, saved["id"])
    assert run["status"] == "running" and run["unsandboxed"] is False
    [(_, command)] = herdr_state(state)["runs"]
    assert command.startswith("claude ")
    assert "command" not in command and "dangerously" not in command


def test_finished_agents_keep_their_answer_and_close_their_pane(setup, monkeypatch):
    jobs, manager, _, state = setup
    saved = jobs.save(request(manager))
    jobs.run_now(saved["id"])
    run = settle(jobs, saved["id"])
    marker = run["agent_run"]["exit_marker"]
    pane = run["agent_run"]["pane"]
    seen = {}

    def step(record, op, persist, now):
        seen["op"] = op
        # The agent confirmed and was exited: its pane is back to a plain shell.
        data = herdr_state(state)
        data["agents"] = []
        state.write_text(json.dumps(data))
        return {
            **record,
            "state": "exited",
            "message": "done",
            "target": {"rollout": "r", "session_id": "s"},
        }

    monkeypatch.setattr(workspace_exit, "step", step)
    monkeypatch.setattr(
        cron_agents.handoff, "latest_turn", lambda *a: ("t", f"Filed 2 issues.\n\n{marker}\n")
    )
    assert jobs.watch() is True
    assert seen["op"]["exit_marker"] == marker and seen["op"]["agent"] == "codex"
    run = jobs.snapshot()["jobs"][0]["runs"][0]
    assert run["status"] == "succeeded"
    assert run["message"] == "The agent finished and exited"
    assert run["finished_at"] is not None
    assert jobs.log(run["id"])["text"].endswith("\nFinal response:\nFiled 2 issues.\n")
    assert ["pane", "close", pane] in herdr_state(state)["calls"]
    assert jobs.snapshot()["jobs"][0]["running"] is False
    assert jobs.watch() is False


def test_an_agent_that_stops_to_ask_is_left_open(setup, monkeypatch):
    jobs, manager, _, state = setup
    saved = jobs.save(request(manager))
    jobs.run_now(saved["id"])
    run = settle(jobs, saved["id"])
    monkeypatch.setattr(
        workspace_exit,
        "step",
        lambda record, *a: {**record, "state": "left_open", "message": "Asked a question"},
    )
    jobs.watch()
    run = jobs.snapshot()["jobs"][0]["runs"][0]
    assert run["status"] == "attention" and run["message"] == "Asked a question"
    assert not any(c[:2] == ["pane", "close"] for c in herdr_state(state)["calls"])
    # Its agent is still open, so the next run skips instead of starting a second one.
    jobs.run_now(saved["id"])
    run = settle(jobs, saved["id"])
    assert run["status"] == "skipped"
    assert run["message"] == "An agent is still open in the job's workspace"
    assert len(herdr_state(state)["runs"]) == 1


def test_a_reused_workspace_gets_a_fresh_pane_for_the_next_run(setup, monkeypatch):
    jobs, manager, _, state = setup
    saved = jobs.save(request(manager))
    jobs.run_now(saved["id"])
    first = settle(jobs, saved["id"])

    def exited(record, *a):
        data = herdr_state(state)
        data["agents"] = []
        state.write_text(json.dumps(data))
        return {**record, "state": "exited", "message": "done"}

    monkeypatch.setattr(workspace_exit, "step", exited)
    jobs.watch()
    data = herdr_state(state)
    data["panes"] = [{"pane_id": "w1:p1", "workspace_id": "w1", "cwd": "/"}]
    state.write_text(json.dumps(data))
    jobs.run_now(saved["id"])
    second = settle(jobs, saved["id"])
    assert second["status"] == "running"
    assert second["agent_run"]["workspace_id"] == first["agent_run"]["workspace_id"]
    assert second["agent_run"]["pane"] != first["agent_run"]["pane"]
    data = herdr_state(state)
    assert len([c for c in data["calls"] if c[:2] == ["worktree", "open"]]) == 1
    assert data["splits"][-1][0] == "w1:p1"


def test_the_time_limit_leaves_a_working_agent_open(setup, monkeypatch):
    clock = [time.time()]
    jobs, manager, _, _ = setup
    jobs.clock = lambda: clock[0]
    saved = jobs.save(request(manager, timeout=600))
    jobs.run_now(saved["id"])
    settle(jobs, saved["id"])
    monkeypatch.setattr(workspace_exit, "step", lambda record, *a: {**record, "message": "Working"})
    jobs.watch()
    assert jobs.snapshot()["jobs"][0]["runs"][0]["message"] == "Working"
    clock[0] += 601
    jobs.watch()
    run = jobs.snapshot()["jobs"][0]["runs"][0]
    assert run["status"] == "attention"
    assert run["message"] == "Still working at the 10 min time limit; left open in Collie"


def test_the_time_limit_reports_why_a_finished_agent_was_not_exited(setup, monkeypatch):
    clock = [time.time()]
    jobs, manager, _, _ = setup
    jobs.clock = lambda: clock[0]
    saved = jobs.save(request(manager, timeout=600))
    jobs.run_now(saved["id"])
    settle(jobs, saved["id"])
    confirmed = clock[0] + 60
    monkeypatch.setattr(
        workspace_exit,
        "step",
        lambda record, *a: {
            **record,
            "confirmed_at": confirmed,
            "blocked": "Empty composer is not yet visible",
            "message": "Finished, but not exiting yet: Empty composer is not yet visible",
        },
    )
    clock[0] += 601
    jobs.watch()
    run = jobs.snapshot()["jobs"][0]["runs"][0]
    assert run["status"] == "attention"
    finished = time.strftime("%H:%M", time.localtime(confirmed))
    assert run["message"] == (
        f"Finished at {finished}, but not exited by the 10 min time limit: "
        "Empty composer is not yet visible; left open in Collie"
    )


def test_a_branch_checked_out_elsewhere_is_used_where_it_is(setup):
    jobs, manager, git, _ = setup
    elsewhere = manager.src / "worktrees" / "mine"
    git("worktree", "add", "-q", str(elsewhere), "feature")
    saved = jobs.save(request(manager, branch="feature", base=""))
    jobs.run_now(saved["id"])
    run = settle(jobs, saved["id"])
    assert run["agent_run"]["path"] == str(elsewhere.resolve())


def test_the_clones_own_checkout_is_never_used(setup):
    jobs, manager, git, _ = setup
    with pytest.raises(ValueError, match="checked out in the clone itself"):
        jobs.save(request(manager, branch="main"))
    # Also when the clone switched to the job's branch after it was saved.
    saved = jobs.save(request(manager, branch="later", base=""))
    git("checkout", "-q", "-b", "later")
    jobs.run_now(saved["id"])
    run = settle(jobs, saved["id"])
    assert run["status"] == "error"
    assert "checked out in the clone itself" in run["message"]


def test_two_jobs_cannot_share_a_branch(setup):
    jobs, manager, _, _ = setup
    jobs.save(request(manager))
    with pytest.raises(ValueError, match="already works on nightly"):
        jobs.save(request(manager, name="Another"))


def test_an_interrupted_exit_needs_attention_instead_of_staying_busy(setup):
    jobs, manager, _, _ = setup
    saved = jobs.save(request(manager))
    jobs.run_now(saved["id"])
    run = settle(jobs, saved["id"])
    # A previous process claimed the exit, then stopped before confirming it.
    jobs.update_run(run, exit={**run["exit"], "state": "exiting", "message": "Exiting the agent"})
    jobs.watch()
    run = jobs.snapshot()["jobs"][0]["runs"][0]
    assert run["status"] == "attention"
    assert run["message"] == "Exit unconfirmed; check its pane"
    assert jobs.snapshot()["jobs"][0]["running"] is False


def test_skips_behind_an_open_agent_are_merged(setup, monkeypatch):
    jobs, manager, _, _ = setup
    saved = jobs.save(request(manager))
    jobs.run_now(saved["id"])
    settle(jobs, saved["id"])
    monkeypatch.setattr(
        workspace_exit,
        "step",
        lambda record, *a: {**record, "state": "left_open", "message": "Asked a question"},
    )
    jobs.watch()
    for _ in range(3):
        jobs.run_now(saved["id"])
        settle(jobs, saved["id"])
    skipped, attention = jobs.snapshot()["jobs"][0]["runs"]
    assert attention["status"] == "attention"
    assert skipped["count"] == 3
    assert skipped["message"] == "Skipped 3 times: an agent is still open in the job's workspace"


def test_a_deleted_job_sends_nothing_to_its_agent(setup, monkeypatch):
    jobs, manager, _, _ = setup
    saved = jobs.save(request(manager))
    jobs.run_now(saved["id"])
    settle(jobs, saved["id"])
    [run] = jobs.snapshot()["jobs"][0]["runs"]
    jobs.delete(saved["id"])
    persisted = []

    def step(record, op, persist, now):
        try:
            persist({**record, "state": "exiting"})
        except workspace_exit.Leave as exc:
            persisted.append(str(exc))
            return {**record, "state": "left_open"}
        persisted.append("sent")
        return {**record, "state": "exited"}

    monkeypatch.setattr(workspace_exit, "step", step)
    jobs.watch_one(run)
    assert persisted == ["The job was deleted; nothing was sent"]
    assert not jobs.log_path(run["id"]).exists()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Done.\nMARK", "Done."),
        ("Done.\n\n```\nMARK\n```\n", "Done."),
        ("Done.\n**MARK**", "Done."),
        ("```py\ncode\n```\nMARK", "```py\ncode\n```"),
        ("```py\ncode\n```", "```py\ncode\n```"),
        ("No marker here", "No marker here"),
    ],
)
def test_the_marker_is_removed_from_the_final_response(monkeypatch, text, expected):
    monkeypatch.setattr(cron_agents.handoff, "latest_turn", lambda *a: ("t", text))
    record = {"target": {"rollout": "r", "session_id": "s"}}
    assert cron_agents.final_response(record, "codex", "MARK") == expected


def test_a_pane_where_nothing_was_typed_is_closed(setup, monkeypatch):
    jobs, manager, _, state = setup
    original = cron_agents.run

    def failing(*args, **kwargs):
        if args[:3] == ("herdr", "pane", "run"):
            raise ValueError("pane is gone")
        return original(*args, **kwargs)

    monkeypatch.setattr(cron_agents, "run", failing)
    saved = jobs.save(request(manager))
    jobs.run_now(saved["id"])
    run = settle(jobs, saved["id"])
    assert run["status"] == "error"
    [split] = herdr_state(state)["splits"]
    pane = [p for p in herdr_state(state)["panes"]][0]["pane_id"]
    assert ["pane", "close", pane] in herdr_state(state)["calls"]


def test_old_staged_prompts_are_removed(setup, tmp_path):
    jobs, manager, _, _ = setup
    prompts = manager.home / "cron-prompts"
    prompts.mkdir()
    old = prompts / "1-old"
    old.write_text("x")
    cron_agents.os.utime(old, (1, 1))
    saved = jobs.save(request(manager))
    jobs.run_now(saved["id"])
    settle(jobs, saved["id"])
    assert not old.exists()
    [staged] = prompts.iterdir()
    assert staged.stat().st_mode & 0o777 == 0o600


def test_launch_failures_are_reported(setup, monkeypatch):
    jobs, manager, _, _ = setup
    monkeypatch.setenv("FAKE_HERDR_OPEN_ERROR", "herdr is not running")
    saved = jobs.save(request(manager))
    jobs.run_now(saved["id"])
    run = settle(jobs, saved["id"])
    assert run["status"] == "error"
    assert run["message"].startswith("Could not start the agent:")
    assert jobs.snapshot()["jobs"][0]["running"] is False


def test_an_agent_that_never_shows_up_needs_attention(setup, monkeypatch):
    jobs, manager, _, _ = setup
    monkeypatch.setattr(cron_agents, "START_WAIT", 0)
    saved = jobs.save(request(manager))
    jobs.run_now(saved["id"])
    run = settle(jobs, saved["id"])
    assert run["status"] == "attention"
    assert (
        run["message"]
        == "The agent did not show up in its pane; check the job's workspace in Collie"
    )


def test_a_restart_keeps_following_started_agents(setup):
    jobs, manager, _, _ = setup
    saved = jobs.save(request(manager))
    jobs.run_now(saved["id"])
    settle(jobs, saved["id"])
    # One being started when the dashboard stopped: its agent may or may not be there.
    with jobs.db() as db:
        jobs.record(db, saved, kind="agent", status="starting", started_at=1)
    jobs.owner.close()
    restarted = CronJobs(manager.home, workspaces=manager)
    statuses = [run["status"] for run in restarted.snapshot()["jobs"][0]["runs"]]
    assert statuses == ["attention", "running"]
    restarted.close()


def test_switching_kinds_drops_the_other_kinds_settings(setup):
    jobs, manager, _, _ = setup
    saved = jobs.save(request(manager))
    shell = jobs.save(
        {"id": saved["id"], "name": "Now a script", "command": "true", "schedule": {"every": 60}}
    )
    assert shell["kind"] == "shell" and shell["command"] == "true"
    assert not {"prompt", "repo", "agent"} & set(shell)
    back = jobs.save({**request(manager), "id": saved["id"]})
    assert back["kind"] == "agent" and "command" not in back


def test_shell_runs_are_unaffected_by_agent_support(setup):
    jobs, _, _, _ = setup
    saved = jobs.save({"name": "Echo", "command": "echo hi", "schedule": {"every": 60}})
    jobs.run_now(saved["id"])
    run = settle(jobs, saved["id"], ("running",))
    assert run["status"] == "succeeded" and run.get("kind") is None
    assert (
        cron_jobs.validate({"name": "x", "command": "true", "schedule": {"every": 60}})["kind"]
        == "shell"
    )
