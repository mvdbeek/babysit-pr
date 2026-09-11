"""Repository/branch routing tests use only fake GitHub responses."""

import argparse

import gh_pr_watch as watch
import pr_supervisor as supervisor
import pytest


def run(run_id=1, **overrides):
    return {
        "id": run_id,
        "workflow_id": 10,
        "head_sha": "abc",
        "head_branch": "feature/fix",
        "name": "CI",
        "event": "push",
        "status": "completed",
        "conclusion": "failure",
        "run_attempt": 1,
        "html_url": f"https://github.com/me/fork/actions/runs/{run_id}",
        **overrides,
    }


@pytest.fixture
def api(monkeypatch):
    state = {"calls": [], "runs": [run()], "sha": "abc"}

    def fake(args, repo=None, cwd=None):
        state["calls"].append((args, repo))
        if args[:2] == ["pr", "view"]:
            assert repo == "upstream/project"
            return {
                "number": 2,
                "url": "https://github.com/upstream/project/pull/2",
                "state": "OPEN",
                "headRefOid": state["sha"],
                "headRefName": "feature/fix",
                "baseRefOid": "base",
                "headRepository": {"name": "fork"},
                "headRepositoryOwner": {"login": "me"},
                "mergeable": "MERGEABLE",
                "mergeStateStatus": "CLEAN",
            }
        assert args[0] == "api", args
        endpoint = args[1]
        if endpoint == "repos/me/fork/branches/feature%2Ffix":
            return {"name": "feature/fix", "commit": {"sha": state["sha"]}}
        if endpoint == "repos/me/fork/actions/runs":
            assert "branch=feature/fix" in args and f"head_sha={state['sha']}" in args
            return {"workflow_runs": state["runs"], "total_count": len(state["runs"])}
        if endpoint.startswith("repos/me/fork/actions/runs/") and endpoint.endswith("/jobs"):
            return {
                "jobs": [
                    {"id": 50, "name": "tests", "conclusion": "failure", "status": "completed"}
                ]
            }
        raise AssertionError(f"Unexpected GitHub request: {args} repo={repo}")

    monkeypatch.setattr(watch, "gh_json", fake)
    return state


def args(branch=None):
    return argparse.Namespace(
        pr="auto" if branch else "2",
        repo="me/fork" if branch else "upstream/project",
        ci_repo=None if branch else "head",
        branch=branch,
        state_file=None,
        max_flaky_retries=3,
    )


def test_fork_ci_uses_upstream_reviews_and_fork_logs(api, monkeypatch):
    monkeypatch.setattr(watch, "get_authenticated_login", lambda: "me")
    reviews = []

    def feedback(pr, state, **kw):
        reviews.append(pr["repo"])
        state["seen_review_ids"] = ["42"]
        return [{"kind": "review", "id": "42"}]

    monkeypatch.setattr(watch, "fetch_new_review_items", feedback)
    state = {}
    packet, _ = watch.collect_snapshot(args(), state=state, persist=False)
    assert reviews == ["upstream/project"]
    assert packet["ci"]["repo"] == "me/fork"
    assert packet["pr"]["repo"] == "upstream/project"
    assert packet["checks"]["failed_count"] == 1
    assert packet["failed_jobs"][0]["logs_endpoint"] == "repos/me/fork/actions/jobs/50/logs"
    assert state["seen_review_ids"] == ["42"]
    assert packet["new_review_items"][0]["id"] == "42"


def test_branch_without_pr_only_calls_branch_and_actions(api):
    packet, _ = watch.collect_snapshot(args("feature/fix"), state={}, persist=False)
    assert packet["pr"]["kind"] == "branch"
    assert packet["new_review_items"] == []
    assert packet["ci"] == {
        "repo": "me/fork",
        "source": "actions",
        "branch": "feature/fix",
        "head_sha": "abc",
    }
    assert packet["failed_jobs"]
    assert all(
        call[0][0] == "api" and call[0][1].startswith("repos/me/fork/") for call in api["calls"]
    )


def test_fork_success_never_claims_pr_merge_readiness(api, monkeypatch):
    monkeypatch.setattr(watch, "get_authenticated_login", lambda: "me")
    monkeypatch.setattr(watch, "fetch_new_review_items", lambda *a, **kw: [])
    api["runs"] = [run(conclusion="success")]
    packet, _ = watch.collect_snapshot(args(), state={}, persist=False)
    assert watch.is_ci_green(packet)
    assert "ready_to_merge" not in packet["actions"]


def test_filters_other_branches_old_shas_and_superseded_dispatches(api):
    api["runs"] = [
        run(),
        run(2, conclusion="success"),
        run(3, head_branch="other"),
        run(4, head_sha="old"),
        run(5, event="workflow_dispatch", conclusion="success"),
    ]
    packet, _ = watch.collect_snapshot(args("feature/fix"), state={}, persist=False)
    assert packet["checks"]["passed_count"] == 2
    assert packet["failed_runs"] == packet["failed_jobs"] == []
    assert watch.is_ci_green(packet)
    assert "ready_to_merge" not in packet["actions"]


@pytest.mark.parametrize(
    "runs",
    [
        [],
        [run(conclusion="skipped")],
        [run(conclusion="neutral")],
        [run(status="queued", conclusion=None)],
        [run(conclusion="unknown")],
    ],
)
def test_empty_skipped_and_unfinished_runs_never_report_green(api, runs):
    api["runs"] = runs
    packet, _ = watch.collect_snapshot(args("feature/fix"), state={}, persist=False)
    assert not watch.is_ci_green(packet)
    assert "ready_to_merge" not in packet["actions"]


def test_failed_job_wakes_before_workflow_completes_and_rerun_changes_identity(api):
    api["runs"] = [run(status="in_progress", conclusion=None)]
    packet, _ = watch.collect_snapshot(args("feature/fix"), state={}, persist=False)
    assert packet["checks"]["pending_count"] == 1
    assert packet["failed_jobs"]
    before = supervisor.event_keys(packet)
    api["runs"][0]["run_attempt"] = 2
    after, _ = watch.collect_snapshot(args("feature/fix"), state={}, persist=False)
    assert supervisor.event_keys(after) != before


def test_rerun_targets_ci_repository(api, monkeypatch, tmp_path):
    options = args("feature/fix")
    options.state_file = str(tmp_path / "state.json")
    calls = []
    monkeypatch.setattr(watch, "gh_text", lambda argv, repo: calls.append((argv, repo)))
    result = watch.retry_failed_now(options)
    assert result["rerun_attempted"]
    assert calls == [(["run", "rerun", "1", "--failed"], "me/fork")]


def test_no_fork_metadata_requires_explicit_ci_repo():
    with pytest.raises(ValueError, match="supply --ci-repo"):
        watch.resolve_ci_repo("head", {})
    assert watch.resolve_ci_repo("me/fork", {}) == "me/fork"


def test_workflow_and_matrix_job_pagination(monkeypatch):
    calls = []

    def fake(argv, **kw):
        calls.append(argv)
        key = "jobs" if argv[1].endswith("jobs") else "workflow_runs"
        if "page=1" in argv:
            return {key: [run(i) for i in range(100)], "total_count": 101}
        assert "page=2" in argv
        return {key: [run(100)], "total_count": 101}

    monkeypatch.setattr(watch, "gh_json", fake)
    assert len(watch.get_workflow_runs_for_sha("me/fork", "abc", "feature/fix")) == 101
    assert len(watch.get_jobs_for_run("me/fork", 1)) == 101
    assert len(calls) == 4


def test_poll_rejects_remote_head_move_during_snapshot(api, monkeypatch):
    job = dict(url="unused", repo="me/fork", branch="feature/fix", watcher_state={})
    original = watch.collect_snapshot

    def moving(*a, **kw):
        packet = original(*a, **kw)
        api["sha"] = "new"
        return packet

    monkeypatch.setattr(watch, "collect_snapshot", moving)
    with pytest.raises(RuntimeError, match="Remote head changed"):
        supervisor.observe(job)
    assert job["watcher_state"] == {}
