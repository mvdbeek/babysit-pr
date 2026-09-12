# Experimental upstream test overview

This opt-in internal module adds an **Upstream tests** dashboard tab. It only reads GitHub Actions data. It does not register watches, invoke agents, rerun workflows, or repair anything. It has its own API, background worker, bounded cache, JavaScript, and stylesheet. Collection and rendering failures stay inside this experiment.

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

The collector reads the newest 100 runs per branch within **14 days**, retaining the newest run ID for each workflow. It includes push, schedule, and manual dispatch events, excluding PR events. It considers at most 30 workflows per branch. The UI reports truncation and branches with no eligible runs. This is a view of selected workflow observations, not proof that the branch head has been fully tested: path filters, schedules, and manual inputs can differ.

A newer passing, pending, cancelled, or otherwise superseding run replaces the previous run's failures. Rerunning an older run does not make it newer than its replacement. Only artifacts created during the current attempt are eligible after a rerun; successful jobs retained from earlier attempts are deliberately not interpreted as current reports. Metadata is checked again after artifact collection to discard results if the attempt or status changed during collection. A replacement after that check is reflected on the next refresh. Timestamps and attempts are shown.

Group by **Failing test** to compare exact pytest node IDs across branches, workflows, and artifacts. Parameters stay part of the ID. Group by **Branch / workflow** for run outcomes, report gaps, job links, and the reported test failures. Artifact names preserve matrix/shard context. GitHub does not provide a reliable artifact-to-job association; links list all jobs in that run attempt and say so. No job association is guessed.

## Supported report adapter

The adapter reads **pytest-html 4 embedded JSON** from `div#data-container[data-jsonblob]` in `run_*_tests.html` files inside artifacts whose name contains `test results`. This matches Galaxy's [API workflow](https://github.com/galaxyproject/galaxy/blob/dev/.github/workflows/api.yaml), [unit workflow](https://github.com/galaxyproject/galaxy/blob/dev/.github/workflows/unit.yaml), and related Python workflows. An actual API-test artifact was inspected to verify the schema. The checked-in fixture is synthetic and contains no downloaded logs or report content.

Only explicit `Failed` / `Error` outcomes with an individual pytest node ID containing `::` are failures. `Rerun` outcomes alone do not count; a retry that ultimately passes does not appear. Expected failures and skips do not count. Collection errors, build failures, missing/expired artifacts, old HTML formats, JUnit XML, Playwright reports, and arbitrary log messages are not converted into individual test failures. Missing or unsupported evidence is shown as incomplete, with a link to the run. There is no log fallback. Full report HTML, embedded scripts, and log bodies are never served or executed.

## Refresh, limits, and failure states

Opening the dashboard requests the experiment snapshot and schedules collection in a separate daemon thread when due. While this tab is visible, the browser checks the snapshot every five seconds. **Refresh** reads that cache; it does not bypass the minimum 15-minute collection interval. One collection runs at a time. A refresh can use at most 120 GitHub requests, 12 artifact downloads, 64 MiB downloaded archive data, and 180 seconds before starting another request. Each request has a 30-second timeout and an 8 MiB response limit, so the total can exceed 180 seconds by one request. The collector is demand-driven, not a separately managed service.

Archives are read in memory without filesystem extraction. Limits include 100 members, 64 MiB total expanded size, 32 MiB per report, and 2,000 failing records per report/run. Absolute paths, traversal, backslash paths, and symlinks are rejected. Only supported report filenames are read. Limits produce visible incomplete states. Large Galaxy reports can exceed these limits. No unbounded history traversal occurs.

`experiments/upstream-tests/cache.json` stores normalized failures and up to 200 parsed artifact entries, at most 8 MiB total; raw archives and report logs are not retained. Only artifacts used in the current observation survive a successful refresh. Cache reuse is scoped to the exact configuration. Successful, pending, and expired observations do not keep old failures current. Artifact reuse allows later refreshes to advance past already downloaded reports when download budgets are reached.

Loading, disabled, stale saved results, empty observations, incomplete coverage, and API errors have separate messages. If overall refresh or cache persistence fails, the last saved observation remains explicitly stale. If a single branch/run/artifact fails, that observation is incomplete and other collection proceeds; old branch failures are not silently carried forward. Cache writes use a private (`0600`) temporary file and atomic replacement. Failed final run revalidation discards the parsed failures instead of presenting unverified results as current. Run one dashboard per state directory; this experiment does not coordinate budgets between multiple dashboard processes.

## Disable or remove

Set `"enabled": false` (or remove its config file) and restart the dashboard. This hides the tab and stops collection. The experiment directory can then be deleted without touching watcher, PR, or issue state.

To remove the code, delete `scripts/upstream_tests.py`, `assets/dashboard/upstream-tests.js`, `assets/dashboard/upstream-tests.css`, its dedicated tests/fixture, and this document. Remove the small integration hooks in `scripts/dashboard.py` (construction, injected server field, endpoint, two assets), `assets/dashboard/index.html` (scripts/style, tab, panel), and the `upstream` page entry/visibility event in `app.js`. There is no general plugin framework or dependency on this module from watcher or overview code.

Validation uses fake GitHub responses and executables, isolated temporary state, and temporary browser servers. Screenshots cover desktop/mobile test and branch views. Run all checks with `uv run --locked tox`.
