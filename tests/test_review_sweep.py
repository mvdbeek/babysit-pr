"""The review sweep against a temporary dashboard: its settings, its record of reviews, and
its scripts run as the cron job's agent runs them, with plain python3 and no GitHub."""

import http.client
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import dashboard
import pytest
import sweep_settings

SWEEP = Path(__file__).resolve().parents[1] / "scripts" / "sweep"
SETTINGS = {
    "owner": "galaxyproject",
    "default_repo": "galaxy",
    "repos": ["galaxy", "planemo", "iwc"],
    "later_repos": ["iwc"],
    "bots": ["gxydevbot"],
    "parallel": 3,
    "per_run": 10,
    "notes": {"galaxyproject/galaxy": {"verify": "Run pytest from the worktree."}},
}
REPORT = """# Review: galaxyproject/galaxy#25013

- PR: https://github.com/galaxyproject/galaxy/pull/25013
- Base: `release_26.2`
- Reviewed head: `c44befd502b4863c199f7c2b02379412b0bfa303` (1 commit)
- Verdict: **ok, minors**. One sentence.

## Findings

### Minor
**m1. Something is off.**
"""


@pytest.fixture
def server(tmp_path):
    home = tmp_path / "dashboard"
    out = tmp_path / "out"
    out.mkdir()
    sweep_settings.save(home, {**SETTINGS, "out": str(out)})
    with dashboard.DashboardServer(home, 0) as running:
        thread = threading.Thread(target=running.serve_forever, daemon=True)
        thread.start()
        running.out = out
        yield running
        running.shutdown()
        thread.join(timeout=5)


def call(server, path, body=None, action=None):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
    headers = {}
    if body is not None:
        headers = {"Content-Type": "application/json", "X-Babysit-Action": action}
    conn.request(
        "POST" if body is not None else "GET",
        path,
        json.dumps(body) if body is not None else None,
        headers,
    )
    response = conn.getresponse()
    value = json.loads(response.read() or b"null")
    conn.close()
    return response.status, value


def script(server, name, *args, home=None, cwd=None):
    """Run a sweep script with plain python3, as the orchestrator does."""
    env = {**os.environ, "BABYSIT_DASHBOARD_URL": f"http://127.0.0.1:{server.server_port}"}
    if home:
        env["HOME"] = str(home)
    return subprocess.run(
        [sys.executable, str(SWEEP / name), *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=cwd,
        timeout=60,
    )


def test_settings_are_served_and_not_changed_through_the_api(server):
    code, value = call(server, "/api/review-sweep")
    assert code == 200 and value["repos"] == ["galaxy", "planemo", "iwc"]
    assert value["review_requests"] is True  # A default filled in.
    # Agents reading untrusted PRs can reach the API; the notes are reviewers' instructions.
    code, _ = call(server, "/api/review-sweep", {"owner": "o", "repos": ["a"]}, "review-sweep")
    assert code == 403
    assert call(server, "/api/review-sweep")[1]["repos"] == ["galaxy", "planemo", "iwc"]


@pytest.mark.parametrize(
    "change,error",
    [
        ({"parallel": 0}, "parallel"),
        ({"out": "relative/path"}, "out"),
        ({"repos": ["../x"]}, "repos"),
        ({"notes": {"o/r": {"other": "x"}}}, "notes"),
        ({"surprise": 1}, "Unknown"),
    ],
)
def test_settings_are_checked(tmp_path, change, error):
    with pytest.raises(ValueError, match=error):
        sweep_settings.save(tmp_path, {**SETTINGS, **change})


def test_without_settings_nothing_is_swept(tmp_path):
    assert sweep_settings.load(tmp_path)["repos"] == []


def test_a_review_is_recorded_and_answered(server):
    (server.out / "2026-11-23-pr25013.md").write_text(REPORT)
    review = {
        "source": "sweep",
        "repo": "galaxyproject/galaxy",
        "number": 25013,
        "head": "c44befd502b",
        "report": str(server.out / "2026-11-23-pr25013.md"),
    }
    code, value = call(server, "/api/agent-reviews", review, "agent-reviews")
    assert code == 200 and value["review"]["verdict"] == "ok, minors"
    code, value = call(
        server, "/api/agent-reviews?url=https://github.com/galaxyproject/galaxy/pull/25013"
    )
    assert code == 200 and value["reviews"][0]["findings"][0]["title"] == "Something is off"
    assert call(server, "/api/agent-reviews?heads=1")[1] == {
        "heads": {"galaxyproject/galaxy#25013": ["c44befd502b"]}
    }
    assert "galaxyproject/galaxy#25013" in call(server, "/api/agent-reviews")[1]["reviews"]
    # A browser on another site cannot record one.
    assert call(server, "/api/agent-reviews", review, "settings")[0] == 404
    assert call(server, "/api/agent-reviews?url=https://example.com/x")[0] == 400


def test_track_records_finished_reviews_and_names_missing_ones(server, tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    (run / "prs.json").write_text(
        json.dumps(
            [
                {
                    "owner": "galaxyproject",
                    "repo": "galaxy",
                    "number": 25013,
                    "baseRefName": "release_26.2",
                },
                {"owner": "galaxyproject", "repo": "planemo", "number": 7, "baseRefName": "master"},
            ]
        )
    )
    (run / "sessions.tsv").write_text(
        "25013\tc44befd502b\tsession-1\t/src/worktrees/galaxy/review-pr-25013\n"
        "planemo#7\t1234567890a\tsession-2\t/src/worktrees/planemo/review-planemo-pr-7\n"
    )
    (server.out / "2026-11-23-run2-pr25013.md").write_text(REPORT)
    result = script(server, "track.py", str(run), "2026-11-23-run2")
    assert result.returncode == 0, result.stderr
    assert "recorded 25013 c44befd502b ok, minors session-1" in result.stdout
    assert "MISSING planemo#7: no report 2026-11-23-run2-planemo-pr7.md" in result.stdout
    review = call(
        server, "/api/agent-reviews?url=https://github.com/galaxyproject/galaxy/pull/25013"
    )[1]["reviews"][0]
    assert review["label"] == "2026-11-23-run2" and review["session"] == "session-1"
    assert review["checkout"] == "/src/worktrees/galaxy/review-pr-25013"
    # Running it again records nothing new.
    assert script(server, "track.py", str(run), "2026-11-23-run2").returncode == 0
    assert len(call(server, "/api/agent-reviews?heads=1")[1]["heads"]) == 1


def test_a_brief_uses_the_settings_notes_and_the_last_review(server, tmp_path):
    (server.out / "2026-11-20-pr25013.md").write_text(REPORT)
    call(
        server,
        "/api/agent-reviews",
        {
            "source": "sweep",
            "repo": "galaxyproject/galaxy",
            "number": 25013,
            "head": "aaaaaaa1111",
            "date": "2026-11-20",
            "report": str(server.out / "2026-11-20-pr25013.md"),
        },
        "agent-reviews",
    )
    run = tmp_path / "run"
    run.mkdir()
    prs = [
        {
            "owner": "galaxyproject",
            "repo": "galaxy",
            "number": 25013,
            "headRefOid": "c44befd502b4863c199f7c2b02379412b0bfa303",
            "baseRefName": "release_26.2",
            "title": "A fix",
            "author": {"login": "someone"},
            "additions": 3,
            "deletions": 1,
            "changedFiles": 2,
        },
        {
            "owner": "someone",
            "repo": "thing",
            "number": 7,
            "headRefOid": "b" * 40,
            "baseRefName": "main",
            "title": "Elsewhere",
            "author": {"login": "x"},
            "additions": 1,
            "deletions": 0,
            "changedFiles": 1,
            "review_requested": True,
        },
    ]
    (run / "prs.json").write_text(json.dumps(prs))
    result = script(
        server, "mkbrief.py", str(run), "2026-11-23", "25013", "someone/thing#7", home=tmp_path
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == [
        "briefs/review-pr-25013.md",
        "briefs/review-someone-thing-pr-7.md",
    ]
    galaxy = (run / "briefs" / "review-pr-25013.md").read_text()
    assert "Run pytest from the worktree." in galaxy  # The repository's own note.
    assert "Head `aaaaaaa1111` was reviewed on 2026-11-20" in galaxy
    assert f"`{server.out}/2026-11-23-pr25013.md`" in galaxy
    other = (run / "briefs" / "review-someone-thing-pr-7.md").read_text()
    assert "project's own test and lint commands" in other  # Generic notes.
    assert "The user's review is requested on this PR" in other


def test_the_scripts_stop_without_the_dashboard(tmp_path):
    env = {**os.environ, "BABYSIT_DASHBOARD_URL": "http://127.0.0.1:9"}
    (tmp_path / "prs.json").write_text("[]")
    (tmp_path / "sessions.tsv").write_text("25013\tabc1234\ts\t/x\n")
    result = subprocess.run(
        [sys.executable, str(SWEEP / "track.py"), str(tmp_path), "2026-11-23"],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert result.returncode != 0 and "cannot reach the dashboard" in result.stderr


def test_keys_names_and_queue_order_follow_the_settings(server):
    code = """
import json, prkeys, collect
pr = prkeys.parse("planemo#1612")
other = prkeys.parse("someone/thing#7")
print(json.dumps({
    "galaxy": [prkeys.parse("24191").key, prkeys.parse("24191").name, prkeys.parse("24191").report_file("L")],
    "planemo": [pr.key, pr.name, pr.slug],
    "other": [other.key, other.name, other.report_file("L")],
    "groups": [collect.group(p) for p in (
        {"review_requested": True, "repo": "iwc", "author": {"login": "x"}},
        {"review_requested": False, "repo": "galaxy", "author": {"login": "x"}},
        {"review_requested": False, "repo": "iwc", "author": {"login": "x"}},
        {"review_requested": False, "repo": "iwc", "author": {"login": "gxydevbot"}},
    )],
}))
"""
    env = {**os.environ, "BABYSIT_DASHBOARD_URL": f"http://127.0.0.1:{server.server_port}"}
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env=env, cwd=SWEEP, timeout=60
    )
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    assert value["galaxy"] == ["24191", "review-pr-24191", "L-pr24191.md"]
    assert value["planemo"] == ["planemo#1612", "review-planemo-pr-1612", "galaxyproject/planemo"]
    assert value["other"] == [
        "someone/thing#7",
        "review-someone-thing-pr-7",
        "L-someone-thing-pr7.md",
    ]
    # Review requests, then swept repositories, then later repositories, bots last.
    assert value["groups"] == [0, 1, 3, 4]
