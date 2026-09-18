"""Opt-in upstream test experiment. No watcher/overview imports or repair operations."""

import calendar
import copy
import io
import json
import os
import re
import selectors
import subprocess
import tempfile
import threading
import time
import zipfile
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from urllib.parse import quote

import github_cli

POLICY = "https://github.com/galaxyproject/galaxy/blob/dev/SECURITY.md"
MAX_ARCHIVE = 8 * 1024 * 1024
MAX_REPORT = 32 * 1024 * 1024
MAX_STATE = 8 * 1024 * 1024
MAX_CASES = 2000
MAX_OUTCOMES = 20000
HISTORY_RUNS = 5
CACHE_VERSION = 2
INTERVAL = 900
WINDOW = 14


def timestamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def gh_bytes(endpoint, limit=MAX_ARCHIVE, timeout=30, raw=False):
    """Bound both stdout and stderr while streaming; never extract to disk."""
    args = ["gh", "api", "--hostname", "github.com", endpoint]
    if raw:
        args += ["-H", "Accept: application/vnd.github.raw+json"]
    data = bytearray()
    started = time.monotonic()
    with (
        tempfile.TemporaryFile() as errors,
        github_cli.command(args, stdout=subprocess.PIPE, stderr=errors) as process,
    ):
        assert process.stdout is not None
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while True:
                    if time.monotonic() - started > timeout or errors.tell() > 65536:
                        raise ValueError("GitHub request exceeded time/error output limit")
                    if selector.select(0.1):
                        chunk = os.read(process.stdout.fileno(), min(65536, limit + 1 - len(data)))
                        if not chunk:
                            break
                        data.extend(chunk)
                        if len(data) > limit:
                            raise ValueError("GitHub response exceeded byte limit")
            if process.wait(timeout=max(0.1, timeout - (time.monotonic() - started))):
                raise ValueError(
                    "GitHub request failed (permission, expired artifact, or API error)"
                )
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
    return bytes(data)


class Budget:
    def __init__(self, fetch=gh_bytes):
        self.fetch = fetch
        self.calls = 0
        self.downloads = 0
        self.bytes = 0
        self.started = time.monotonic()

    def get(self, endpoint, *, archive=False, raw=False):
        if self.calls >= 120 or time.monotonic() - self.started > 180:
            raise ValueError("Refresh API/time budget exhausted")
        if archive and (self.downloads >= 12 or self.bytes > 64 * 1024 * 1024 - MAX_ARCHIVE):
            raise ValueError("Refresh artifact budget exhausted")
        self.calls += 1
        if archive:
            self.downloads += 1
        data = self.fetch(endpoint, raw=raw)
        if archive:
            self.bytes += len(data)
            return data
        return data.decode() if raw else json.loads(data)


class ReportHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blob = None

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "div" and values.get("id") == "data-container":
            self.blob = values.get("data-jsonblob")


def report_outcomes(data):
    """Read pytest-html 4 embedded JSON, never render report HTML or execute scripts."""
    parser = ReportHTML()
    parser.feed(data.decode("utf-8"))
    if not parser.blob:
        raise ValueError("Unsupported report: requires pytest-html 4 embedded JSON")
    tests = json.loads(parser.blob).get("tests")
    if not isinstance(tests, dict):
        raise ValueError("Invalid pytest-html tests")
    outcomes = []
    for nodeid, results in tests.items():
        if not isinstance(nodeid, str) or "::" not in nodeid or len(nodeid) > 2048:
            continue  # Collection errors are not individual test failures.
        if not isinstance(results, list) or not results:
            raise ValueError("Invalid pytest-html result list")
        # pytest-rerunfailures emits Rerun followed by the final outcome. Preserve
        # setup/teardown errors even if a separate call-phase result passed.
        final = [r for r in results if r.get("result") != "Rerun"]
        failed = [r for r in final if r.get("result") in {"Failed", "Error"}]
        passed = any(r.get("result") == "Passed" for r in final)
        if failed or passed:
            outcomes.append(
                {
                    "test": nodeid,
                    "summary": failed[-1]["result"] if failed else "Passed",
                    "retried": any(r.get("result") == "Rerun" for r in results),
                }
            )
        if len(outcomes) > MAX_OUTCOMES:
            raise ValueError("Report outcome count exceeds limit")
    return outcomes


def report_failures(data):
    return [
        {"test": r["test"], "summary": r["summary"]}
        for r in report_outcomes(data)
        if r["summary"] in {"Failed", "Error"}
    ]


def archive_outcomes(data):
    outcomes = []
    reports = 0
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        members = archive.infolist()
        if len(members) > 100 or sum(m.file_size for m in members) > 64 * 1024 * 1024:
            raise ValueError("Archive expanded size/member limit exceeded")
        if len({m.filename for m in members}) != len(members):
            raise ValueError("Duplicate archive member path")
        for member in members:
            path = PurePosixPath(member.filename)
            if path.is_absolute() or ".." in path.parts or "\\" in member.filename:
                raise ValueError("Unsafe archive member path")
            if member.file_size > MAX_REPORT or (member.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError("Oversized report or archive symlink")
            if not re.fullmatch(r"run_[A-Za-z0-9_]+_tests\.html", path.name):
                continue
            reports += 1
            for result in report_outcomes(archive.read(member)):
                outcomes.append({**result, "report": member.filename})
                if len(outcomes) > MAX_OUTCOMES:
                    raise ValueError("Artifact outcome count exceeds limit")
    if not reports:
        raise ValueError("No supported pytest-html report in artifact")
    return outcomes


def archive_failures(data):
    failures = {
        r["test"]: {"test": r["test"], "summary": r["summary"]}
        for r in archive_outcomes(data)
        if r["summary"] in {"Failed", "Error"}
    }
    if len(failures) > MAX_CASES:
        raise ValueError("Report failure count exceeds limit")
    return list(failures.values())


def supported_branches(api, repo, now):
    if repo != "galaxyproject/galaxy":
        raise ValueError("Set explicit branches for repositories other than galaxyproject/galaxy")
    policy = api.get(f"repos/{repo}/contents/SECURITY.md?ref=dev", raw=True)
    if "Releases within the past 12 months" not in policy:
        raise ValueError("Galaxy support policy changed; configure explicit branches after review")
    cutoff = now.replace(
        year=now.year - 1, day=min(now.day, calendar.monthrange(now.year - 1, now.month)[1])
    )
    releases = []
    for page in range(1, 4):
        batch = api.get(f"repos/{repo}/releases?per_page=100&page={page}")
        releases.extend(batch)
        if len(batch) < 100:
            break
    else:
        raise ValueError("Release discovery truncated; configure explicit branches")
    branches = set()
    for release in releases:
        # Patch publication must never extend the support lifetime of a series.
        match = re.fullmatch(r"v?(\d{2}\.\d+)(?:\.0)?", release.get("tag_name", ""))
        if match and not release.get("draft") and not release.get("prerelease"):
            if cutoff <= timestamp(release["published_at"]) <= now:
                branches.add("release_" + match[1])
    return ["dev", *sorted(branches)]


def run_row(run, branch, repo):
    return {
        "branch": branch,
        "workflow": run.get("name", str(run["workflow_id"])),
        "workflow_id": run["workflow_id"],
        "run_id": run["id"],
        "attempt": run.get("run_attempt", 1),
        "url": f"https://github.com/{repo}/actions/runs/{run['id']}",
        "updated_at": run["updated_at"],
        "sha": run.get("head_sha", ""),
        "state": run.get("conclusion") or run["status"],
        "failures": [],
        "outcomes": [],
        "jobs": [],
        "notes": [],
        "sampled": False,
    }


def observe_run(run, row, repo, api, cache, used_cache, now):
    """Only explicit individual report outcomes count, including in green runs."""
    fingerprint = [run.get(k) for k in ("id", "run_attempt", "status", "conclusion", "updated_at")]
    run_key = f"run:{run['id']}"
    cached = cache.get(run_key, {})
    if (
        isinstance(cached, dict)
        and cached.get("version") == CACHE_VERSION
        and cached.get("fingerprint") == fingerprint
        and 0 <= now.timestamp() - cached.get("observed_at", 0) < 4 * INTERVAL
    ):
        row.update(copy.deepcopy(cached["row"]))
        used_cache[run_key] = cached
        return
    root = f"repos/{repo}/actions/runs/{run['id']}"
    reusable = True
    pending_cache = {}
    try:
        artifacts = api.get(f"{root}/artifacts?per_page=100")
        if artifacts.get("total_count", 0) > 100:
            row["notes"].append("Artifact list truncated")
        candidates = [
            a for a in artifacts["artifacts"] if "test results" in a.get("name", "").lower()
        ]
        for artifact in candidates[:12]:
            try:
                if artifact.get("expired"):
                    raise ValueError("Report artifact expired")
                if artifact.get("size_in_bytes", 0) > MAX_ARCHIVE:
                    raise ValueError("Report artifact exceeds download limit")
                if row["attempt"] > 1 and timestamp(artifact["created_at"]) < timestamp(
                    run["run_started_at"]
                ):
                    raise ValueError("Earlier-attempt report ignored")
                key = str(artifact["id"])
                saved = cache.get(key, {})
                outcomes = (
                    saved.get("outcomes")
                    if isinstance(saved, dict) and saved.get("version") == CACHE_VERSION
                    else None
                )
                if outcomes is None:
                    outcomes = archive_outcomes(
                        api.get(
                            f"repos/{repo}/actions/artifacts/{artifact['id']}/zip", archive=True
                        )
                    )
                pending_cache[key] = {"version": CACHE_VERSION, "outcomes": outcomes}
                if len(row["outcomes"]) + len(outcomes) > MAX_OUTCOMES:
                    raise ValueError("Run outcome count exceeds limit")
                row["outcomes"].extend(
                    {**result, "artifact": artifact["name"]} for result in outcomes
                )
            except Exception as exc:
                reusable = False
                row["notes"].append(f"{artifact['name']}: {exc}")
        if len(candidates) > 12:
            row["notes"].append("Artifact count limit reached")
        if candidates:
            jobs = api.get(f"{root}/attempts/{row['attempt']}/jobs?per_page=100")
            row["jobs"] = [
                {
                    "name": j["name"],
                    "url": f"{row['url']}/job/{j['id']}",
                    "state": j.get("conclusion") or j["status"],
                }
                for j in jobs["jobs"]
            ]
            if jobs.get("total_count", 0) > 100:
                row["notes"].append("Job list truncated")
        current = api.get(root)
        if any(
            current.get(k) != run.get(k)
            for k in ("run_attempt", "status", "conclusion", "updated_at")
        ):
            raise ValueError("Run changed during collection; awaiting next refresh")
        row["sampled"] = bool(row["outcomes"])
        row["failures"] = [r for r in row["outcomes"] if r["summary"] in {"Failed", "Error"}]
        if len(row["failures"]) > MAX_CASES:
            raise ValueError("Run failure count exceeds limit")
        if not row["sampled"]:
            row["notes"].append(
                "No confirmed individual test outcomes; infrastructure/build failure or unavailable/unsupported reports"
            )
        # Revalidate before caching: a changed attempt must not contribute history.
        if reusable:
            used_cache[run_key] = {
                "version": CACHE_VERSION,
                "fingerprint": fingerprint,
                "observed_at": now.timestamp(),
                "row": copy.deepcopy(row),
            }
        else:
            used_cache.update(pending_cache)
    except Exception as exc:
        row["outcomes"], row["failures"], row["sampled"] = [], [], False
        row["notes"].append(f"Run evidence could not be verified: {exc}")


def test_groups(rows, samples):
    """Compare exact test/matrix/report identities; a green workflow is not a test pass."""
    current = {(r["branch"], r["workflow_id"]): r["run_id"] for r in rows}
    contexts: dict[tuple, list[dict]] = {}
    for row in samples:
        # Duplicate reports must not inflate the number of observations.
        seen = set()
        for result in row["outcomes"]:
            key = (
                result["test"],
                row["branch"],
                row["workflow_id"],
                result["artifact"],
                result["report"],
            )
            if key in seen:
                continue
            seen.add(key)
            contexts.setdefault(key, []).append(
                {
                    **{
                        k: row[k]
                        for k in (
                            "branch",
                            "workflow",
                            "workflow_id",
                            "run_id",
                            "attempt",
                            "url",
                            "sha",
                            "updated_at",
                            "jobs",
                        )
                    },
                    **result,
                    "current": current.get((row["branch"], row["workflow_id"])) == row["run_id"],
                }
            )
    groups: dict[str, dict] = {}
    for key, observations in contexts.items():
        observations.sort(key=lambda o: o["run_id"], reverse=True)
        failures = [o for o in observations if o["summary"] in {"Failed", "Error"}]
        passes = [o for o in observations if o["summary"] == "Passed"]
        retries = [o for o in passes if o["retried"]]
        failed_shas = {o["sha"] for o in failures if o["sha"]}
        mixed_sha = bool(failed_shas & {o["sha"] for o in passes if o["sha"]})
        currently_failing = any(o["current"] for o in failures)
        if retries or mixed_sha:
            classification = "likely_flaky"
            reason = (
                "Passed after a test retry."
                if retries
                else "Passed and failed on the same commit in the same test context."
            )
        elif currently_failing and len(failures) >= 2 and not passes:
            classification = "likely_broken"
            reason = "Fails in the latest run and at least two sampled runs, with no observed pass in this context."
        elif currently_failing and passes:
            classification = "mixed"
            reason = "Passes and failures are on different commits; a fix or regression could explain the change."
        elif currently_failing:
            classification = "insufficient"
            reason = "Current failure, but too little comparable history to distinguish a breakage from a flake."
        else:
            continue  # Superseded failures alone are not current failures or flake evidence.
        group = groups.setdefault(key[0], {"test": key[0], "occurrences": [], "assessments": []})
        group["occurrences"].extend(observations)
        group["assessments"].append(
            {
                "branch": key[1],
                "workflow": observations[0]["workflow"],
                "workflow_id": key[2],
                "artifact": key[3],
                "report": key[4],
                "classification": classification,
                "reason": reason,
                "failures": len(failures),
                "passes": len(passes),
                "retry_passes": len(retries),
                "currently_failing": currently_failing,
                "sampled_runs": len(observations),
            }
        )
    priority = {"likely_flaky": 0, "likely_broken": 1, "mixed": 2, "insufficient": 3}
    return sorted(
        groups.values(),
        key=lambda g: (min(priority[a["classification"]] for a in g["assessments"]), g["test"]),
    )


def collect(config, api, now, artifact_cache):
    repo = config.get("repo", "galaxyproject/galaxy")
    if (
        not isinstance(repo, str)
        or not re.fullmatch(r"[\w.-]+/[\w.-]+", repo)
        or any(p in {".", ".."} for p in repo.split("/"))
    ):
        raise ValueError("Invalid upstream repository")
    branches = config.get("branches")
    automatic = branches is None
    if automatic:
        branches = supported_branches(api, repo, now)
    if (
        not isinstance(branches, list)
        or not 1 <= len(branches) <= 8
        or any(not isinstance(b, str) or not b or len(b) > 200 for b in branches)
    ):
        raise ValueError("Configure between 1 and 8 branch names")
    branches = list(dict.fromkeys(branches))
    rows, warnings, histories = [], [], []
    since = (now - timedelta(days=WINDOW)).isoformat()
    for branch in branches:
        try:
            response = api.get(
                f"repos/{repo}/actions/runs?branch={quote(branch, safe='')}&created={quote('>=' + since, safe='')}&per_page=100"
            )
            runs = response["workflow_runs"]
            if response.get("total_count", len(runs)) > len(runs):
                warnings.append(
                    f"{branch}: only the newest 100 runs inspected; workflows or older history may be missing"
                )
            by_workflow: dict[int, list[dict]] = {}
            seen_runs = set()
            for run in sorted(runs, key=lambda r: r["id"], reverse=True):
                if (
                    run.get("head_branch") != branch
                    or run.get("event") not in {"push", "schedule", "workflow_dispatch"}
                    or timestamp(run["created_at"]) < now - timedelta(days=WINDOW)
                ):
                    continue
                if run["id"] in seen_runs:
                    continue
                seen_runs.add(run["id"])
                history = by_workflow.setdefault(run["workflow_id"], [])
                if len(history) < HISTORY_RUNS:
                    history.append(run)
            if not by_workflow:
                warnings.append(f"{branch}: no eligible runs in the observation window")
            for history in list(by_workflow.values())[:30]:
                latest = run_row(history[0], branch, repo)
                rows.append(latest)
                # Inspect failures and test workflows, including green runs with retry evidence.
                if any(r.get("conclusion") == "failure" for r in history) or re.search(
                    r"test|integration|selenium", latest["workflow"], re.I
                ):
                    histories.append(
                        [
                            (run, latest if i == 0 else run_row(run, branch, repo))
                            for i, run in enumerate(history)
                        ]
                    )
            if len(by_workflow) > 30:
                warnings.append(f"{branch}: workflow limit reached")
        except Exception as exc:
            warnings.append(f"{branch}: {exc}")
    used_cache: dict = {}
    samples = []
    # Current failures across all branches first; then expand one history level at a time.
    histories.sort(key=lambda h: h[0][1]["state"] != "failure")
    for depth in range(HISTORY_RUNS):
        for run_history in histories:
            if depth >= len(run_history):
                continue
            run, row = run_history[depth]
            if run["status"] != "completed" or run.get("conclusion") in {
                "skipped",
                "neutral",
                "cancelled",
            }:
                continue
            observe_run(run, row, repo, api, artifact_cache, used_cache, now)
            samples.append(row)
    groups = test_groups(rows, samples)
    # Passing tests are only returned when they support a displayed assessment.
    public_rows = [{k: v for k, v in r.items() if k != "outcomes"} for r in rows]
    history_gaps = [
        {
            "branch": r["branch"],
            "workflow": r["workflow"],
            "run_id": r["run_id"],
            "url": r["url"],
            "notes": r["notes"],
        }
        for r in samples
        if r["notes"]
    ]
    # Limit persisted evidence without failing an otherwise useful observation.
    retained: dict = {}
    size = 0
    for key, value in used_cache.items():
        entry_size = len(json.dumps(value).encode())
        if len(retained) < 200 and size + entry_size <= MAX_STATE // 2:
            retained[key] = value
            size += entry_size
    return {
        "repo": repo,
        "branches": branches,
        "selection": "Galaxy security support: initial releases within 12 months"
        if automatic
        else "Explicit branch configuration",
        "policy_url": POLICY if automatic else None,
        "window_days": WINDOW,
        "runs": public_rows,
        "groups": groups,
        "warnings": warnings,
        "history": {
            "runs_per_workflow": HISTORY_RUNS,
            "sampled_runs": sum(r["sampled"] for r in samples),
            "selected_runs": len(samples),
            "gaps": history_gaps,
        },
        "incomplete": bool(warnings or history_gaps),
        "budget": {"requests": api.calls, "downloads": api.downloads, "bytes": api.bytes},
    }, retained


class UpstreamTests:
    """Lazy, single-flight refresh with its own bounded cache and failure boundary."""

    def __init__(self, home: Path, fetch=gh_bytes):
        self.directory = home / "experiments" / "upstream-tests"
        self.fetch = fetch
        self.lock = threading.Lock()
        self.next_poll = 0.0
        self.loading = False
        self.value: dict = {}
        self.artifacts: dict = {}
        self.config: dict = {}
        self.error = None
        self.enabled = False
        try:
            config_path = self.directory / "config.json"
            if not config_path.exists():
                return
            if config_path.stat().st_size > 65536:
                raise ValueError("Experiment configuration exceeds size limit")
            self.config = json.loads(config_path.read_text())
            self.enabled = self.config.get("enabled") is True
            cache_path = self.directory / "cache.json"
            if self.enabled and cache_path.exists() and cache_path.stat().st_size <= MAX_STATE:
                cache = json.loads(cache_path.read_text())
                if cache.get("version") == CACHE_VERSION and cache.get("config") == self.config:
                    self.value = cache["value"]
                    self.artifacts = cache.get("artifacts", {})
                    self.next_poll = self.value.get("synced_at", 0) + INTERVAL
        except Exception as exc:
            self.error = f"Experiment configuration/cache unavailable: {exc}"

    def snapshot(self):
        with self.lock:
            if self.enabled and not self.loading and time.time() >= self.next_poll:
                self.loading = True
                self.next_poll = time.time() + INTERVAL
                threading.Thread(target=self._refresh, daemon=True).start()
            return {
                **copy.deepcopy(self.value),
                "enabled": self.enabled,
                "loading": self.loading,
                "error": self.error,
                "stale": bool(
                    self.value
                    and (self.error or time.time() - self.value.get("synced_at", 0) >= INTERVAL)
                ),
            }

    def _refresh(self):
        try:
            value, artifacts = collect(
                self.config, Budget(self.fetch), datetime.now(UTC), self.artifacts
            )
            value["synced_at"] = time.time()
            payload = json.dumps(
                {
                    "version": CACHE_VERSION,
                    "config": self.config,
                    "value": value,
                    "artifacts": artifacts,
                }
            )
            if len(payload.encode()) > MAX_STATE:
                raise ValueError("Experiment cache size limit exceeded")
            self.directory.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w", dir=self.directory, suffix=".tmp", delete=False
            ) as stream:
                temporary = Path(stream.name)
                try:
                    os.fchmod(stream.fileno(), 0o600)
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                    temporary.replace(self.directory / "cache.json")
                finally:
                    temporary.unlink(missing_ok=True)
            with self.lock:
                self.value, self.artifacts, self.error = value, artifacts, None
        except Exception as exc:
            with self.lock:
                self.error = f"Upstream test refresh failed: {exc}"
        finally:
            with self.lock:
                self.loading = False
