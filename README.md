# PR babysitter

A shared watcher that waits for GitHub CI and PR activity without keeping coding agents alive. Bounded Codex and Claude repairs resume the original conversation in its original herdr pane. The local dashboard shows watches, CI, feedback awaiting approval, repair logs, and cleanup readiness.

The skill instructions and operating details are in [SKILL.md](SKILL.md) and [the supervisor reference](references/supervisor.md). The dashboard frontend lives in `assets/dashboard/`; its HTTP server is `scripts/dashboard.py`. `scripts/pr_supervisor.py` owns the queue and repair lifecycle.

## Your pull requests

The dashboard also discovers open GitHub PRs you authored, are assigned to, or are
involved in reviewing (including team requests and completed reviews). It uses the
active `gh` account on github.com. No registration is needed. These PRs are a
read-only overview and do not start agents or create repair watches.

Search by repository, title, number, or author, and filter by role or CI state.
Each row links to the PR and its checks. CI is GitHub's combined head-ref check
status; “No checks” means GitHub returned no rollup, not a successful build.
“Last updated” is GitHub's PR `updatedAt` (PR activity, not the last poll or CI
completion time). The separate sync timestamp shows how fresh the data is.

While the dashboard is open, one background worker refreshes GitHub at most every
two minutes; browser refreshes and multiple tabs share the cache. Pagination fetches
up to GitHub's search limit of 1,000 PRs per role, with an explicit notice if capped.
Duplicates appear once with all matching roles. Failed requests keep the last
complete snapshot with a visible error. Cached metadata lives in
`pr-overview.json` in the watcher state directory, outside this repository.
The HTTP API is `/api/prs` and uses the same local/tailnet access restrictions as
the watch dashboard. This overview does not require the watcher daemon to be online.

## Development

Requires Python 3.11+, [uv](https://docs.astral.sh/uv/), Node.js 22.13+, npm, Git, and zsh. Python and Node dependencies are pinned in `pyproject.toml`/`uv.lock` and `package.json`/`package-lock.json`. No agent credentials or running herdr server are needed for tests.

Run every local check through tox:

```sh
uv run --locked tox
```

Tox uses `tox-uv` to create isolated Python environments from `uv.lock`. It also
runs `npm ci` from `package-lock.json` and installs the pinned Chromium build.
No separate manual dependency installation is needed beyond uv, Node/npm, Git,
and zsh. The first run requires downloads; subsequent runs reuse tool caches.

The default environments are `lint`, `format`, `types`, `skill`, `frontend`,
`unit`, `browser`, and `coverage`. The coverage report depends on both test
environments. To run independent environments concurrently:

```sh
uv run --locked tox run-parallel
```

Tox runs Ruff lint/format checks, mypy across the Python runtime and development
scripts, ESLint, Prettier, zsh syntax checking, the repository-local skill
validator, the Node interaction test, and pytest including real Chromium tests.
Mypy checks unannotated function bodies; this is gradual typing, not strict typing
of every JSON payload. No automated CI workflow is configured.

Apply formatting and safe lint fixes through the separate opt-in environment:

```sh
uv run --locked tox -e fix
```

For a targeted rerun, select a tox environment, for example
`uv run --locked tox -e types` or `uv run --locked tox -e browser`.

Browser tests use a real Chromium process and a real HTTP server backed by a temporary SQLite queue. They cover comment text rendering, explicit feedback approval, stale approval rejection, cancellation/history, cleanup indicators, and mobile layout. They never start the production watcher or a coding agent. Other integration tests use fake GitHub/agent executables and temporary worktrees, and exercise timeouts, process ownership, and restart recovery.

Coverage includes Python branches and instrumented child Python processes. The `coverage` tox environment writes a terminal summary, HTML at `reports/coverage/index.html`, and XML at `reports/coverage.xml`. The combined Python line/branch coverage gate is 75%. Reports and browser failure artifacts are ignored by Git. JavaScript behavior is exercised by browser and Node tests; the coverage percentage measures Python only.

The Safehouse launcher matches this user's installed configuration. Tests check its command construction, but do not require Safehouse or claim to test macOS sandbox enforcement on every machine.
