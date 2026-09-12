# Experimental upstream test overview

This opt-in internal module adds an **Upstream tests** dashboard tab for failing and likely flaky tests. It only reads GitHub Actions data. It does not register watches, invoke agents, rerun workflows, or repair anything. It has its own API, background worker, bounded cache, JavaScript, and stylesheet. Collection and rendering failures stay inside this experiment.

## Activation and branch selection

Create `experiments/upstream-tests/config.json` under the dashboard's `--home` directory, then start your dashboard normally:

```json
{
  "enabled": true,
  "repo": "galaxyproject/galaxy"
}
```

The existing authenticated `gh` executable needs read access to repository Actions runs and artifacts. The experiment does no network work when disabled. Configuration is read at dashboard startup. No existing services are restarted automatically.

Without `branches`, the Galaxy adapter checks the current upstream `SECURITY.md`, then derives branches from **initial stable release** publication dates within the past 12 calendar months. Patch dates never extend support. The policy URL, selection mode, and actual tracked branches appear in the tab. A policy change or truncated release discovery produces an error instead of a guessed list. Release discovery reads at most three pages of 100 releases.

Research on September 12, 2026 confirmed the local Galaxy checkout's `upstream` remote is `galaxyproject/galaxy`. The [security policy](https://github.com/galaxyproject/galaxy/blob/dev/SECURITY.md) and [published releases](https://github.com/galaxyproject/galaxy/releases) currently select `dev`, `release_25.1`, `release_26.0`, and `release_26.1`. This list is an example of the result, not a hardcoded default. Here “supported” means security support, as defined by that policy.

For another repository or a deliberate branch selection, configure one to eight explicit branches:

```json
{
  "enabled": true,
  "repo": "example/project",
  "branches": ["dev", "release_current"]
}
```

Explicit selections are labeled as configuration, without claiming they follow Galaxy's support policy. The report adapter and artifact naming requirements still apply.

## What the observation means

The collector reads the newest 100 runs per branch within **14 days**, retaining the newest run for each workflow as its current status and up to **five recent runs per workflow** as history. It includes push, schedule, and manual dispatch events, excluding PR events. It considers at most 30 workflows per branch. Reports are sampled for workflows with a failure in this history or whose name contains `test`, `integration`, or `selenium`, including successful runs. Other workflows remain visible in the branch overview. The UI reports truncation, incomplete history, and branches with no eligible runs. Path filters, schedules, and manual inputs can differ; this is not proof that the branch head has been fully tested.

A newer passing, pending, cancelled, or otherwise superseding run replaces the previous run's **current** failures. Historical flake evidence is retained within the selected window and explicitly distinguished from failures in the latest run. Rerunning an older run does not make it newer than its replacement. Only artifacts created during the current attempt are eligible after a rerun. Metadata is checked again after report collection; changed or unverifiable runs contribute no test outcomes. Timestamps, commits, run IDs, and attempts are available in each finding's evidence.

Group by **Test** to compare exact pytest node IDs across branches, workflows, and artifacts. Parameters stay part of the ID. Each assessment compares only one branch, workflow ID, artifact name (matrix/shard), and report path:

- **Likely flaky**: the report records a retry followed by a pass, or comparable reports show both a pass and a failure on the same nonempty commit SHA.
- **Likely broken**: the test fails in the latest run and at least two distinct sampled runs, with no observed pass in that context.
- **Mixed results**: a current failure and earlier passes occur on different commits. A code fix or regression can explain this, so it is not labeled flaky.
- **Insufficient history**: a current failure has too little comparable evidence.

These are heuristics, not proof. Missing reports, absent tests, skipped tests, and a green workflow never count as individual passes. Different contexts can produce different assessments for the same test. **Show** filters findings by assessment. Expand **Pass / fail evidence** for linked runs and individual outcomes, including passes after retry. Only failures in the newest workflow run are labeled current; historical failures alone do not remain as open problems after a replacement.

Group by **Branch / workflow** for current run outcomes, report gaps, job links, and confirmed failures. This view always shows all latest workflows. GitHub does not provide a reliable artifact-to-job association; job links list all jobs in that run attempt and say so. No job association is guessed.

## Supported report adapter

The adapter reads **pytest-html 4 embedded JSON** from `div#data-container[data-jsonblob]` in `run_*_tests.html` files inside artifacts whose name contains `test results`. This matches Galaxy's [API workflow](https://github.com/galaxyproject/galaxy/blob/dev/.github/workflows/api.yaml), [unit workflow](https://github.com/galaxyproject/galaxy/blob/dev/.github/workflows/unit.yaml), and related Python workflows. An actual API-test artifact was inspected to verify the schema. The checked-in fixture is synthetic and contains no downloaded logs or report content.

Only explicit `Failed` / `Error` outcomes with an individual pytest node ID containing `::` are failures. Explicit `Passed` outcomes provide pass evidence. `Rerun` alone is not a final outcome; a retry followed by a pass is shown as likely flaky. A setup/teardown error remains a failure even when the call phase passed. Expected failures and skips do not count. Collection errors, build failures, missing/expired artifacts, old HTML formats, JUnit XML, Playwright reports, and arbitrary log messages are not converted into individual test failures. Missing or unsupported evidence is shown as incomplete, with a link to the run. There is no log fallback. Full report HTML, embedded scripts, and log bodies are never served or executed.

## Refresh, limits, and failure states

Opening the dashboard requests the experiment snapshot and schedules collection in a separate daemon thread when due. While this tab is visible, the browser checks the snapshot every five seconds. **Refresh** reads that cache; it does not bypass the minimum 15-minute collection interval. One collection runs at a time. A refresh can use at most 120 GitHub requests, 12 artifact downloads, 64 MiB downloaded archive data, and 180 seconds before starting another request. Each request has a 30-second timeout and an 8 MiB response limit, so the total can exceed 180 seconds by one request. The collector is demand-driven, not a separately managed service.

Archives are read in memory without filesystem extraction. Limits include 100 members, 64 MiB total expanded size, 32 MiB per report, and 20,000 normalized outcomes per report/artifact/run and 2,000 failing records per run. Duplicate archive member paths are rejected. Absolute paths, traversal, backslash paths, and symlinks are rejected. Only supported report filenames are read. Limits produce visible incomplete states. Large Galaxy reports can exceed these limits. No unbounded history traversal occurs.

`experiments/upstream-tests/cache.json` stores normalized evidence, never full report HTML or logs. Up to 200 evidence cache entries occupy at most 4 MiB; the total state remains capped at 8 MiB. Completed run evidence is reused for up to one hour only when fresh run-list metadata matches its ID, attempt, status, conclusion, and update time. It remains an observation from when the artifact was available; later artifact expiry does not erase already observed outcomes. A changed attempt invalidates it. Partially downloaded artifacts can be reused after revalidation, letting later refreshes advance through the history within the download budget. Current failures across branches are sampled first, followed by deeper history. Cache limits or expired/missing reports can leave history incomplete; this is shown with links to the affected runs. Old cache formats are discarded safely, and cache reuse is scoped to the exact configuration.

Loading, disabled, stale saved results, empty observations, incomplete coverage, and API errors have separate messages. If overall refresh or cache persistence fails, the last saved observation remains explicitly stale. If a single branch/run/artifact fails, that observation is incomplete and other collection proceeds; old branch failures are not silently carried forward. Cache writes use a private (`0600`) temporary file and atomic replacement. Failed final run revalidation discards the parsed failures instead of presenting unverified results as current. Run one dashboard per state directory; this experiment does not coordinate budgets between multiple dashboard processes.

## Disable or remove

Set `"enabled": false` (or remove its config file) and restart the dashboard. This hides the tab and stops collection. The experiment directory can then be deleted without touching watcher, PR, or issue state.

To remove the code, delete `scripts/upstream_tests.py`, `assets/dashboard/upstream-tests.js`, `assets/dashboard/upstream-tests.css`, its dedicated tests/fixture, and this document. Remove the small integration hooks in `scripts/dashboard.py` (construction, injected server field, endpoint, two assets), `assets/dashboard/index.html` (scripts/style, tab, panel), and the `upstream` page entry/visibility event in `app.js`. There is no general plugin framework or dependency on this module from watcher or overview code.

Validation uses fake GitHub responses and executables, isolated temporary state, and temporary browser servers. Screenshots cover desktop/mobile test and branch views. Run all checks with `uv run --locked tox`.
