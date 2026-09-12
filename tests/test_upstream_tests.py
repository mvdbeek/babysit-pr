"""Isolated experimental collector with fake responses and a fake gh executable."""

import copy
import io
import json
import os
import sys
import threading
import time
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
import upstream_tests as upstream

NOW = datetime(2026, 9, 12, tzinfo=UTC)
FIXTURE = Path(__file__).parent / "fixtures/upstream-tests/run_api_tests.html"


def archive(body=None, name="run_api_tests.html"):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as output:
        output.writestr(name, FIXTURE.read_bytes() if body is None else body)
    return buffer.getvalue()


def run(run_id=10, branch="dev", **changes):
    return {
        "id": run_id,
        "workflow_id": 1,
        "name": "API tests",
        "head_branch": branch,
        "event": "push",
        "created_at": "2026-09-11T00:00:00Z",
        "updated_at": "2026-09-11T01:00:00Z",
        "run_started_at": "2026-09-11T00:00:00Z",
        "run_attempt": 1,
        "head_sha": "a" * 40,
        "status": "completed",
        "conclusion": "failure",
        **changes,
    }


class FakeGitHub:
    def __init__(self, runs=None):
        self.runs = runs if runs is not None else [run(), run(20, "release_26.1")]
        self.artifact_changes = {}
        self.current_changes = {}
        self.no_artifacts = False
        self.calls = []
        self.report = archive()

    def __call__(self, endpoint, raw=False):
        self.calls.append(endpoint)
        if endpoint.endswith("/zip"):
            return self.report
        path = urlsplit(endpoint).path
        if path.endswith("/actions/runs"):
            branch = parse_qs(urlsplit(endpoint).query)["branch"][0]
            result = {"workflow_runs": [r for r in self.runs if r["head_branch"] == branch]}
        elif path.endswith("/jobs"):
            result = {
                "jobs": [
                    {
                        "id": 42,
                        "name": "Test (3.10, 0)",
                        "status": "completed",
                        "conclusion": "failure",
                    }
                ]
            }
        elif path.endswith("/artifacts"):
            run_id = int(path.split("/")[-2])
            result = {
                "artifacts": []
                if self.no_artifacts
                else [
                    {
                        "id": run_id * 10,
                        "name": "API test results (3.10, 0)",
                        "size_in_bytes": len(self.report),
                        "created_at": "2026-09-11T00:50:00Z",
                        **self.artifact_changes,
                    }
                ]
            }
        else:
            result = {
                **next(r for r in self.runs if r["id"] == int(path.split("/")[-1])),
                **self.current_changes,
            }
        return json.dumps(result).encode()


def collect(fake, cache=None, branches=None):
    return upstream.collect(
        {"branches": branches or ["dev", "release_26.1"]}, upstream.Budget(fake), NOW, cache or {}
    )


def test_structured_report_preserves_parameter_identity_and_retry_outcome():
    failures = upstream.report_failures(FIXTURE.read_bytes())
    assert [f["test"].split("::")[-1] for f in failures] == [
        "test_case[a]",
        "test_case[b]",
        "test_retry_failed",
    ]
    assert [f["summary"] for f in failures] == ["Failed", "Error", "Failed"]
    with pytest.raises(ValueError, match="Unsupported report"):
        upstream.report_failures(b"<p>FAILED tests/test.py::test_a</p>")


def test_grouping_across_branches_and_cache_reuses_only_current_artifacts():
    fake = FakeGitHub()
    value, cache = collect(fake)
    assert len(value["groups"]) == 3
    assert {o["branch"] for o in value["groups"][0]["occurrences"]} == {"dev", "release_26.1"}
    assert value["groups"][0]["occurrences"][0]["jobs"][0]["url"].endswith("/job/42")
    assert value["budget"]["downloads"] == 2
    second, _ = collect(fake, cache)
    assert second["budget"]["downloads"] == 0
    assert second["groups"] == value["groups"]
    fake.runs = [run(conclusion="success")]
    cleared, retained = collect(fake, cache, ["dev"])
    assert cleared["groups"] == [] and retained == {}


@pytest.mark.parametrize(
    "replacement",
    [
        {"conclusion": "success"},
        {"status": "in_progress", "conclusion": None},
        {"conclusion": "cancelled"},
    ],
)
def test_newer_runs_replace_failures_even_if_old_run_updated_later(replacement):
    fake = FakeGitHub([run(updated_at="2026-09-12T00:00:00Z"), run(11, **replacement)])
    fake.no_artifacts = True
    value, _ = collect(fake, branches=["dev"])
    assert value["groups"] == []
    assert [r["run_id"] for r in value["runs"]] == [11]


def test_excludes_pr_old_window_and_uses_workflow_identity():
    fake = FakeGitHub(
        [
            run(),
            run(11, workflow_id=2),
            run(12, event="pull_request"),
            run(13, created_at="2026-08-01T00:00:00Z"),
        ]
    )
    value, _ = collect(fake, branches=["dev"])
    assert {r["run_id"] for r in value["runs"]} == {10, 11}


@pytest.mark.parametrize(
    "artifact,reason",
    [
        ({"expired": True}, "expired"),
        ({"size_in_bytes": upstream.MAX_ARCHIVE + 1}, "download limit"),
        ({"created_at": "2026-09-10T00:00:00Z"}, "Earlier-attempt"),
    ],
)
def test_rerun_never_reuses_old_expired_or_oversized_reports(artifact, reason):
    fake = FakeGitHub([run(run_attempt=2)])
    fake.artifact_changes = artifact
    value, _ = collect(fake, branches=["dev"])
    assert value["groups"] == []
    assert value["incomplete"]
    assert reason in " ".join(value["runs"][0]["notes"])
    assert value["budget"]["downloads"] == 0


def test_rerun_current_artifacts_and_mid_collection_change():
    fake = FakeGitHub([run(run_attempt=2)])
    value, _ = collect(fake, branches=["dev"])
    assert value["groups"][0]["occurrences"][0]["attempt"] == 2
    fake.current_changes = {"run_attempt": 3}
    value, _ = collect(fake, branches=["dev"])
    assert value["groups"] == []
    assert "changed during" in " ".join(value["runs"][0]["notes"])


@pytest.mark.parametrize("kind", ["missing", "unsupported", "expired_download", "build"])
def test_unconfirmed_infrastructure_and_missing_reports_are_not_test_failures(kind):
    fake = FakeGitHub([run()])
    if kind in {"missing", "build"}:
        fake.no_artifacts = True
    elif kind == "unsupported":
        fake.report = archive(b"<html>build failed</html>")
    else:
        fake.report = b"GitHub artifact unavailable"
    value, _ = collect(fake, branches=["dev"])
    assert not value["groups"]
    assert value["incomplete"]
    assert "No confirmed" in " ".join(value["runs"][0]["notes"])


@pytest.mark.parametrize(
    "filename", ["../run_api_tests.html", "/run_api_tests.html", "x\\run_api_tests.html"]
)
def test_archive_paths_are_rejected_without_extraction(filename):
    with pytest.raises(ValueError, match="Unsafe"):
        upstream.archive_failures(archive(name=filename))


def test_archive_members_size_and_symlink_limits(monkeypatch):
    monkeypatch.setattr(upstream, "MAX_REPORT", 10)
    with pytest.raises(ValueError, match="Oversized"):
        upstream.archive_failures(archive())
    monkeypatch.setattr(upstream, "MAX_REPORT", 32 * 1024 * 1024)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        info = zipfile.ZipInfo("run_api_tests.html")
        info.external_attr = 0o120777 << 16
        z.writestr(info, "target")
    with pytest.raises(ValueError, match="symlink"):
        upstream.archive_failures(buffer.getvalue())
    with pytest.raises(ValueError, match="No supported"):
        upstream.archive_failures(archive(name="other.html"))


def test_policy_uses_initial_release_not_patch_and_checks_current_policy():
    releases = [
        {"tag_name": "v26.1.0", "published_at": "2026-08-02T00:00:00Z"},
        {"tag_name": "v25.0.0", "published_at": "2025-06-01T00:00:00Z"},
        {"tag_name": "v25.0.4", "published_at": "2026-08-01T00:00:00Z"},
        {"tag_name": "v26.2.0", "published_at": "2026-09-01T00:00:00Z", "prerelease": True},
        {"tag_name": "v25.1", "published_at": "2025-12-17T00:00:00Z"},
    ]

    def fetch(endpoint, raw=False):
        return b"Releases within the past 12 months" if raw else json.dumps(releases).encode()

    assert upstream.supported_branches(upstream.Budget(fetch), "galaxyproject/galaxy", NOW) == [
        "dev",
        "release_25.1",
        "release_26.1",
    ]
    with pytest.raises(ValueError, match="explicit branches"):
        upstream.supported_branches(upstream.Budget(fetch), "other/repo", NOW)
    with pytest.raises(ValueError, match="policy changed"):
        upstream.supported_branches(
            upstream.Budget(lambda *a, **k: b"Changed"), "galaxyproject/galaxy", NOW
        )


def test_api_download_and_time_budgets(monkeypatch):
    api = upstream.Budget(lambda *a, **kw: b"{}")
    for _ in range(120):
        api.get("fake")
    with pytest.raises(ValueError, match="budget"):
        api.get("fake")
    api = upstream.Budget(lambda *a, **kw: b"zip")
    for _ in range(12):
        api.get("fake", archive=True)
    with pytest.raises(ValueError, match="artifact budget"):
        api.get("fake", archive=True)
    api.started -= 181
    with pytest.raises(ValueError, match="time budget"):
        api.get("fake")
    api = upstream.Budget(lambda *a, **kw: b"zip")
    # Reserve the maximum response before starting a transfer so a nearly full
    # byte budget cannot overshoot by one archive.
    api.bytes = 64 * 1024 * 1024 - 1
    with pytest.raises(ValueError, match="artifact budget"):
        api.get("fake", archive=True)


def test_branch_errors_do_not_keep_obsolete_failures_or_abort_other_branches():
    fake = FakeGitHub()

    def fetch(endpoint, **kwargs):
        if "branch=dev" in endpoint:
            raise ValueError("rate limited")
        return fake(endpoint, **kwargs)

    value, _ = collect(fetch)
    assert value["incomplete"]
    assert value["warnings"] == ["dev: rate limited"]
    assert {o["branch"] for g in value["groups"] for o in g["occurrences"]} == {"release_26.1"}


def configured(tmp_path, fetch):
    directory = tmp_path / "experiments/upstream-tests"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "config.json").write_text(json.dumps({"enabled": True, "branches": ["dev"]}))
    return upstream.UpstreamTests(tmp_path, fetch)


def wait_for_refresh(plugin):
    deadline = time.monotonic() + 3
    while plugin.loading and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not plugin.loading


def test_background_single_flight_cache_restart_and_error_isolation(tmp_path):
    gate = threading.Event()
    fake = FakeGitHub([run(created_at=datetime.now(UTC).isoformat())])

    def fetch(endpoint, **kwargs):
        assert gate.wait(2)
        return fake(endpoint, **kwargs)

    plugin = configured(tmp_path, fetch)
    assert plugin.snapshot()["loading"]
    assert plugin.snapshot()["loading"]
    gate.set()
    wait_for_refresh(plugin)
    assert len(plugin.value["groups"]) == 3
    assert len(fake.calls) == 5
    original = copy.deepcopy(plugin.value)
    second = upstream.UpstreamTests(tmp_path, fake)
    assert second.value == original

    def fail(*args, **kwargs):
        raise ValueError("offline")

    second.config = {"repo": "invalid"}
    second.fetch = fail
    second.snapshot()
    wait_for_refresh(second)
    state = second.snapshot()
    assert state["stale"] and state["error"]
    assert second.value == original
    assert not (tmp_path / "queue.sqlite").exists()
    assert not (tmp_path / "prs.json").exists()


def test_disabled_bad_config_and_corrupt_cache_are_isolated(tmp_path):
    def never(*args, **kwargs):
        pytest.fail("Disabled experiment called GitHub")

    plugin = upstream.UpstreamTests(tmp_path, never)
    assert plugin.snapshot()["enabled"] is False
    assert not (tmp_path / "experiments").exists()
    plugin = configured(tmp_path, never)
    (plugin.directory / "config.json").write_text("{")
    assert upstream.UpstreamTests(tmp_path, never).snapshot()["error"]
    (plugin.directory / "config.json").write_text('{"enabled":false}')
    assert not upstream.UpstreamTests(tmp_path, never).snapshot()["enabled"]
    (plugin.directory / "config.json").write_text('{"enabled":true}')
    (plugin.directory / "cache.json").write_text("invalid")
    broken = upstream.UpstreamTests(tmp_path, never)
    assert broken.error


@pytest.mark.parametrize(
    "config", [{"repo": "../repo"}, {"branches": []}, {"branches": "dev"}, {"branches": ["a"] * 9}]
)
def test_invalid_config_never_calls_github(config):
    with pytest.raises(ValueError):
        upstream.collect(
            config, upstream.Budget(lambda *a, **k: pytest.fail("API called")), NOW, {}
        )


def test_fake_gh_transport_byte_limit_failure_timeout_and_raw_header(tmp_path, monkeypatch):
    executable = tmp_path / "gh"
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    executable.write_text(
        f"#!{sys.executable}\nimport sys\nprint('raw' if 'Accept: application/vnd.github.raw+json' in sys.argv else 'ok')\n"
    )
    executable.chmod(0o755)
    assert upstream.gh_bytes("fake", raw=True) == b"raw\n"
    with pytest.raises(ValueError, match="byte limit"):
        upstream.gh_bytes("fake", limit=1)
    executable.write_text(f"#!{sys.executable}\nimport sys\nsys.exit(1)\n")
    with pytest.raises(ValueError, match="GitHub request failed"):
        upstream.gh_bytes("fake")
    executable.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(2)\n")
    with pytest.raises(ValueError, match="time/error"):
        upstream.gh_bytes("fake", timeout=0.05)


def test_failed_final_revalidation_discards_already_parsed_failures():
    fake = FakeGitHub([run(run_attempt=2)])

    def fetch(endpoint, **kwargs):
        if endpoint.endswith("/actions/runs/10"):
            raise ValueError("revalidation rate limited")
        return fake(endpoint, **kwargs)

    value, _ = collect(fetch, branches=["dev"])
    assert value["budget"]["downloads"] == 1
    assert value["groups"] == []
    assert value["runs"][0]["failures"] == []
    assert value["incomplete"]
    assert "revalidation rate limited" in " ".join(value["runs"][0]["notes"])


def test_cache_is_private_and_atomic_even_replacing_public_existing_file(tmp_path):
    fake = FakeGitHub([run(created_at=datetime.now(UTC).isoformat())])
    plugin = configured(tmp_path, fake)
    path = plugin.directory / "cache.json"
    path.write_text("old")
    path.chmod(0o644)
    original_inode = path.stat().st_ino
    plugin._refresh()
    assert plugin.error is None
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.stat().st_ino != original_inode
    assert json.loads(path.read_text())["value"]["groups"]
    assert list(plugin.directory.glob("*.tmp")) == []
