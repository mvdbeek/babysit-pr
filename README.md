# PR babysitter

A shared watcher that waits for GitHub CI and PR activity without keeping coding agents alive. Bounded Codex and Claude repairs resume the original conversation in its original herdr pane. The local dashboard shows watches, CI, feedback awaiting approval, repair logs, and cleanup readiness.

The skill instructions and operating details are in [SKILL.md](SKILL.md) and [the supervisor reference](references/supervisor.md). The dashboard frontend lives in `assets/dashboard/`; its HTTP server is `scripts/dashboard.py`. `scripts/pr_supervisor.py` owns the queue and repair lifecycle.

## Your pull requests

The dashboard opens on the **Watcher** tab. Use **Pull requests** to switch to the
PR overview and **Issues** for the issue overview; tabs support keyboard navigation,
browser history, and direct links with `#watcher`, `#prs` or `#issues`. The PR table has separate repository, author, and draft/ready-for-review columns,
plus opened and last-updated timestamps. Click a metadata column heading to sort; click
again to reverse it. Mobile layouts provide equivalent sort controls. Dates sort
chronologically, other columns by their displayed text; missing metadata stays
at the bottom. Last updated (newest first) is the default. Sort selection survives
refreshes and filtering within the page. Ready for review means the PR is not a
draft; it does not imply reviewer approval.

Both tables render 50 rows at a time. Scrolling to the end of the table, or clicking
**Show 50 more**, appends the next 50 rows. Changing the search, a filter, or the sort
restarts at the first 50; background refreshes keep the rows already shown. The match
count and the since-your-last-visit counts cover the whole filtered list, not only the
rendered rows.

Use **Pin** beside a PR or issue number to keep it at the top of its overview;
**Unpin** restores its normal position. Pinned items still obey search and filters,
and the selected sort direction applies within both pinned and unpinned groups.
Pins apply before the 50-row window, so a matching pin from a later page appears
at the top. Pinning keeps the current window size and does not count as GitHub
activity, register a repair watch, or create a workspace.

Pins are saved in this browser and dashboard origin, separately for each GitHub
account and item kind, under `babysit-pr:pins-prs:v1:<login>` and
`babysit-pr:pins-issues:v1:<login>`, using GitHub node IDs. Closed, deleted, or
otherwise absent items stay out of the overview; their pins remain saved in case
they reappear. If browser storage is unavailable, pins work for the current tab
and a notice explains that they cannot persist. Pin controls support Enter and
Space, retain keyboard focus when rows move, and have touch-sized targets on mobile.

The dashboard also discovers open GitHub PRs you authored, are assigned to, or are
involved in reviewing (including team requests and completed reviews). It uses the
active `gh` account on github.com. No registration is needed. These PRs are a
read-only discovery results. Workspace actions require an explicit click and never register repair watches.

Search by repository, title, number, or author. The labeled filter row combines
repository, CI state, review status (Draft or Ready for review), and your role.
The Review status column shows a green **✓ Approved** badge when GitHub reports
the PR as approved. It uses the existing five-minute refresh, with no extra API
requests, and keeps the Draft badge visible for approved drafts.
Repository choices come from all discovered PRs; selections survive refreshes
even if a repository no longer has matching PRs. CI states have individual options,
including Error, Expected, No checks, and Unknown.
Each row links to the PR and its checks. CI is GitHub's combined head-ref check
status; “No checks” means GitHub returned no rollup, not a successful build.
“Last updated” is GitHub's PR `updatedAt` (PR activity, not the last poll or CI
completion time). The separate sync timestamp shows how fresh the data is.

Both PR and issue rows combine **Opened** and **Updated** in one compact
**Dates / activity** column, with relative ages and exact local timestamps. Use its
Opened or Updated header buttons to sort by either date (click again to reverse),
or choose Opened or Last updated in the mobile sort menu. Last updated, newest
first, remains the default. Missing dates show “Unknown” and sort last in either
direction. Since-visit activity highlighting targets the Updated section, leaving
the Opened timestamp unhighlighted.

Rows show **Latest activity** beneath Updated: the person
or bot, an action, its own timestamp (hover for the exact time), and a direct link
when GitHub supplies one. The existing queries fetch the last five timeline items
and the latest description edit; there are no additional HTTP requests or polls.
Comments, reviews, labels, assignees, common state/branch changes, and PR commits
are supported. Commit attribution uses the committer and commit time, not the
pusher or push time. This is best-effort activity, not a definitive explanation of
`updatedAt`: edits to older comments outside the window and other event types may
be missing. An unsupported or inaccessible final event shows “Unavailable”; a
missing actor shows “Unknown actor”. Old caches work and gain activity on refresh.

While the dashboard is open, one background worker refreshes GitHub at most every
five minutes; browser refreshes and multiple tabs share the cache. Pagination fetches
up to GitHub's search limit of 1,000 PRs per role, with an explicit notice if capped.
Duplicates appear once with all matching roles. Failed requests keep the last
complete snapshot with a visible error. Cached metadata lives in
`pr-overview.json` in the watcher state directory, outside this repository.
The HTTP API is `/api/prs` and uses the same local/tailnet access restrictions as
the watch dashboard. This overview does not require the watcher daemon to be online.

## Your issues

The **Issues** tab lists open GitHub issues that involve the active `gh` account in
any capacity: created (**Author**), **Assignee**, **Mentioned**, or **Participant**
(you commented). It mirrors the PR tab: the same sortable columns, labeled filters,
search, mobile sort controls, and “since your last visit” highlighting, stored
separately under `babysit-pr:seen-issues:v1:<login>`. Columns are repository,
issue title with its labels, author, assignees, your roles, comment count, linked
pull requests, opened and last-updated times, and **Actions**. Filters combine
repository, role, label (a select populated from the discovered labels) and
whether the issue has a linked PR. Search covers repository, title, number,
author, assignees and label names.

**Linked PRs** are the pull requests GitHub reports as closing the issue
(`closedByPullRequestsReferences` without closed, unmerged PRs; merged PRs still
appear, in green), with a Draft marker where relevant. When a linked
PR is already in your PR overview, its CI badge appears next to the link and opens
the same CI details dialog; otherwise no CI is shown and nothing extra is fetched.
Issues have no CI columns of their own.

Discovery runs four searches (`author`, `assignee`, `mentions`, `commenter`) and
merges duplicates with all matching roles, with the same 1,000-result cap notice as
PRs. It shares the five-minute refresh cadence and the GitHub request machinery with
the PR overview (`scripts/pr_overview.py`; the issue definition is
`scripts/issue_overview.py`). Cached metadata lives in `issue-overview.json` next to
`pr-overview.json`. The HTTP API is `GET /api/issues`, with the same access restrictions
as `/api/prs`.

## PR workspace actions

The **Actions** column opens a verified workspace in
[Collie](https://collie.tailfb45be.ts.net). Multiple matches open a chooser with
workspace names and agent status. **More actions** offers **Focus in herdr** and
**Copy command**. Opening never sends a task to an existing agent. Only the explicit
native focus action changes herdr focus; creation finishes with an **Open in Collie** link.

When there is no workspace, **Reopen workspace** restores a verified checkout
without starting an agent. **Create workspace** requires a task and selects Codex
by default, with Claude also available. Multiple local clones require a choice;
the choice is remembered. If no clone exists, **Clone and create** shows its
destination before submission. It clones the base repository using the existing
`gh` authentication into `~/src/<repo>`, or `~/src/<owner>--<repo>` when the first
path is occupied. Existing content is never overwritten.

One shared inventory refreshes every 15 seconds while the PR tab is visible.
GitHub metadata, including head repository, branch and SHA, retains its five-minute
refresh. Matches use saved associations, watcher bindings, or verified Git head
provenance. Branch names alone appear only as suggestions. Every action rechecks
Git repository, branch and current workspace identity. Dirty files and unpushed
commits do not prevent opening. Discovery searches main clones directly under
`~/src` and the repository roots recorded by herdr.
Each clone is resolved with three Git calls (config, `rev-parse`, `worktree list`)
rather than five per worktree, and clones are resolved concurrently, so a scan of a
few hundred worktrees takes seconds rather than a minute. Detached and prunable
worktrees are skipped.

`scripts/pr_workspaces.py` owns this integration. `GET /api/workspaces` discovers
resources; `POST /api/workspace-action` accepts a known PR `id`, an `action`
(`open`, `reopen`, `focus`, `copy`, `create`, or `clone-and-create`), and explicit
selection parameters. Requests use `Content-Type: application/json` and
`X-Babysit-Action: workspace-action`, with the same host/origin protections as
watch actions. Repository URLs and commands are resolved on the server.
No browser writes to Collie's API are involved.

Associations, remembered clones, operation status and bounded logs live in
`pr-workspaces.sqlite` under the configured watcher state directory, separate from
`queue.sqlite`. Creation is asynchronous, deduplicated per PR, and serialized per
clone with file locks inherited by helper processes. Verification after launch
rechecks only the new checkout and herdr, not the whole inventory. The dialog shows
progress, logs and errors, polling every two seconds while an operation is queued or
running; it closes with Escape, the Close button, or a click on the backdrop, like
the CI details dialog. Failed operations can be retried explicitly; interrupted or
uncertain launches are inspected and never automatically resubmitted. A verified
existing checkout can be reopened without another agent. Previous operations stay
in the database's `operation_history` table.

Dashboard-created checkouts use `~/src/worktrees/<repo>/pr-<base-owner>-<number>`,
with an unused numeric suffix for collisions. The adapter invokes `wtpr` through
`zsh -lic`, forces herdr, and selects the agent explicitly, retaining the user's
interactive-shell Safehouse wrappers. Task text plus the canonical PR URL is stored
in a private file under `workspace-prompts/` outside the checkout and passed with
`--prompt-file`. No PR comments or CI watches are added.

The chezmoi-managed `~/.config/zsh/worktree.zsh` helper accepts `wtpr --name`,
`--no-focus`, `--repo-path`, and `--worktree-root`, and `wti` accepts the same four
options. Existing terminal defaults remain unchanged. `scripts/worktree.zsh` is the
matching helper snapshot used by isolated integration tests; keep it synchronized with
`~/.local/share/chezmoi/dot_config/zsh/worktree.zsh` when changing the helper.
After updating that source, apply the individual helper with chezmoi and restart
the dashboard process. Live rollout checks should use read-only `curl` requests to
`/`, `/api/prs`, `/api/issues`, and `/api/workspaces`; they must not create a
workspace or launch an agent.

## Issue workspace actions

The **Actions** column on the Issues tab offers the same open, reopen, focus, copy,
create and clone-and-create actions as the PR tab, through the same dialog and the
same `POST /api/workspace-action` endpoint. Requests carry the issue's GraphQL `id`;
GraphQL node ids are globally unique, so the server dispatches on the id and needs no
separate kind parameter. `GET /api/workspaces` returns an `issues` map beside `prs`,
keyed by id, with the same `matches`, `suggestions`, `clones`, `preferred_clone`,
`destination` and `operation` fields. One local inventory scan serves both tabs and
refreshes every 15 seconds while either tab is visible.

An issue's workspaces are checkouts that match one of these rules:

- a saved association or a watcher job bound to the canonical issue URL, as for PRs;
- a checkout whose branch is `issue-<number>` or `issue-<number>-…` (what `wti`
  creates) in a clone of the issue's repository; the same branch name in another
  repository is listed only as an unverified suggestion;
- a checkout carrying the verified head of one of the issue's **linked PRs** (the
  existing PR rule: upstream or branch plus the PR head commit). These count as the
  issue's workspace and are labelled **via PR #n** in the Actions cell and chooser.

**Create workspace** for an issue requires a task and an agent, exactly like PRs,
and needs no head metadata. The adapter invokes `wti` through `zsh -lic` with
`--codex|--claude --no-focus --name <name> --repo-path <clone> --worktree-root
<root> --prompt-file <file> <issue-url>`. The worktree and branch name is `wti`'s
own default, `issue-<number>-<title-slug>` (or `issue-<number>` when the title yields
no slug), with a numeric suffix for collisions; the branch starts from the clone's
`origin/HEAD`, so no upstream check applies. The prompt file contains the task and
`Issue: <canonical URL>`. Associations, operations and logs share
`pr-workspaces.sqlite`; the `pr` column keeps its name and stores both kinds of id.

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

## CI details and automatic failure logs

Click a PR's CI badge to see failing checks first, including workflow/matrix job
names, pending and cancelled checks, and links to each job. Ctrl/Cmd-click still
opens GitHub directly. Expand **Reported failure details** on a failing check to
read its published summary and annotations, including test names where the CI
provider reports them. Expand **Downloaded test log** to see detected failing tests,
failed steps, and continuously scrolling output from the background log cache. Test name extraction
recognizes pytest and unittest summaries; other output is available as plain text.
The job link always opens the original CI provider.

Regular PR polling is unchanged. Opening details requests only that PR's check
metadata, with at most three GraphQL pages of 100 checks. Failure summaries and
up to 20 annotations load only when expanded. Large results are visibly marked as
incomplete. A separate background worker automatically collects failing Actions
job logs, even when the browser is closed.

The shared cache is keyed by account, PR and head commit, lasts five minutes and
survives dashboard restarts in the private `pr-ci-cache.json` state file. Requests
for the same data share one worker; at most two CI requests run concurrently, and
only the latest 128 cache entries are retained. Failures are cached for one minute
to avoid repeated rate-limit/authentication requests. Detail loading stops when
its dialog closes, and the browser makes no requests while the page is hidden. Open dialogs
do not keep polling GitHub after loading. A new head commit uses a new cache key;
a head change during pagination is rejected rather than mixing results.

`GET /api/pr-ci?id=<known-pr-id>` and the optional `check=<known-failing-check-id>`
use the dashboard's existing host and cross-site protections. The implementation
uses GitHub's [check runs and annotations](https://docs.github.com/en/graphql/reference/checks)
and [status rollups](https://docs.github.com/en/graphql/reference/commits).

The background worker uses the existing five-minute PR overview poll. It shares
CI metadata with the details cache and starts at most **12 PR check refreshes per
hour**, revisiting each unchanged failing PR at most hourly. Each refresh has the
same three-page limit. One worker downloads at most **20 failed-job logs per hour**,
including failed attempts; each attempt first reads that job's metadata. These
rolling limits persist across restarts. Large backlogs fill gradually, with at most
500 pending jobs. On-demand check metadata and annotations keep their existing
separate limits; opening a cached log causes no GitHub requests.

Only failed GitHub Actions jobs with matching repository, run, check and commit
identities are downloaded. The collector requests an
[individual job's plain-text log](https://docs.github.com/en/rest/actions/workflow-jobs#download-job-logs-for-a-workflow-run),
never workflow archives or artifacts. Each download stops after **8 MiB** or 90
seconds; capped output is marked incomplete. Logs are compressed in the private
`ci-job-logs/` state directory, with status and request budgets in `pr-ci-logs.sqlite`.
Logs expire after **seven days**, with a maximum of **200 cached jobs / 128 MiB
compressed**. Evictions are remembered until expiry to avoid repeated downloads.
Failed downloads retry at most twice, at least an hour apart. New job IDs from
reruns get their own cache entries; queued work for superseded PR heads is skipped.
A process lock prevents two dashboard instances sharing state from downloading
in parallel, and interrupted downloads are recovered on restart. This collector
never starts agents, changes workspaces, or registers watches.

`GET /api/pr-ci-log?id=<known-pr-id>&check=<known-failing-check-id>` reads status and
bounded failure excerpts. Adding `download=1` returns the cached plain-text log.
Both use the existing host/cross-site protections and resolve jobs only from the
PR's current cached checks. Non-Actions providers remain accessible through their
job links.

Expanding **Downloaded test log** opens a single scrolling log view. It loads the
first cached page automatically and appends more as you approach the bottom,
keeping earlier output and your scroll position. There are no page controls,
view switches or download links. Output remains plain text with terminal color
escapes removed. Short pages fill the viewport automatically; only one request
runs at a time. Loading pauses when the log is collapsed or the page is hidden,
and closing the modal cancels pending reads. Failed reads keep the loaded output
and retry automatically, up to three attempts; reopening the log allows another
try. The end of the cached output is clearly marked.

`GET /api/pr-ci-log?id=<known-pr-id>&check=<known-failing-check-id>&page=1` returns a
numbered page from the same compressed local cache. Pages contain at most 65,536
characters, prefer complete lines, and split very long lines without skipping
content. Invalid page numbers and unrecognized checks are rejected with the same
host/cross-site protections as other log reads. Paging never fetches GitHub logs
again. The existing 8 MiB collection limit remains visible for incomplete cached
logs; the original job link still provides output beyond that limit.

## Changes since your last visit

The PR overview highlights **New** PRs and **Updated** rows compared with your
previous visit in this browser. Changed CI, review approval, draft status,
repository, title, author, roles and head commit are highlighted in their columns;
other activity is marked in Last updated. Labels explain the highlighted fields.
The count follows the current filters. Highlights stay visible through refreshes,
sorting and filtering for the rest of the visit; reloading starts a new visit.

The first visit establishes a baseline. Snapshots are saved in browser local
storage, separately for each GitHub account and dashboard origin, only while the
PR tab is visible. Opening the Watcher tab alone does not mark PRs as seen. Failed
or in-progress syncs do not overwrite the last saved snapshot, and an older tab
cannot replace a newer snapshot. If browser storage is unavailable, comparisons
still work during the current visit. This uses the existing overview data and
adds no server storage or GitHub requests. Closed PRs leave the open-PR overview.
