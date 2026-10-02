# PR babysitter

A shared watcher that waits for GitHub CI and PR activity without keeping coding agents alive. Bounded Codex and Claude repairs resume the original conversation in its original herdr pane. The local dashboard shows watches, CI, feedback awaiting approval, repair logs, and cleanup readiness.

The skill instructions and operating details are in [SKILL.md](SKILL.md) and [the supervisor reference](references/supervisor.md). The dashboard frontend lives in `assets/dashboard/`; its HTTP server is `scripts/dashboard.py`. `scripts/pr_supervisor.py` owns the queue and repair lifecycle.

On mobile, selecting a watch card scrolls to its details and actions. Background
refreshes leave the scroll position alone; reduced-motion settings are respected.

The header bell counts unseen items across the watcher, PR, and issue views. Its
log shows the 10 most recently updated items, combining repeated updates to the
same PR or issue into one entry, including updates seen in both the watcher and
PR overview. Older unseen items still contribute to the count. Opening the bell
does not clear it: open an entry, visit its PR/issue list (or watch details), or
choose **Mark all seen**. History uses the existing per-account visit baseline and
is saved in this browser, with seen state shared between tabs. The first snapshot
without prior history starts quietly. Updates come from existing dashboard
snapshots, including changes discovered on reopening; no extra GitHub polling is
added, and intermediate changes while the dashboard is closed may not be captured.

Notification entries open their latest source inside Babysitter: a selected watch
or a highlighted PR/issue row. Links reveal the item even when filters or pagination
would hide it. Phone alerts also open the source of the latest update. **Mark all
seen** is above the notification log.

Notifications skip the immediate effects of cancelling watches, approving feedback,
and marking feedback addressed. Your own newly added review comments and GitHub
activity are also skipped when the available timeline identifies you and covers
the interval since the last snapshot. CI results, repair outcomes, other people's
activity, and changes with uncertain attribution still notify. Commit authorship
alone does not prove who pushed a change. Existing unread updates are retained;
the suppression applies to the new change, rather than marking the whole item seen.

PR rows have a bell button to **Silence notifications** for that PR, including its
watcher. This preference persists on the dashboard server and applies to every
device on the account until you unsilence it. Silencing clears that PR's unread
notification. Updates observed while silenced are not replayed when you unsilence
it. PR list change highlights still work independently.

CI alerts summarize the completed checks for a commit; individual check progress
and repair starts/retries are quiet. Repair outcomes still notify. Push delivery
waits for 30 seconds without another update to the same PR/issue, combining nearby
updates into one alert and one inbox item.

For **iPhone Home Screen badges and background alerts**, run the dashboard with
the optional Web Push dependency:

```sh
uv run --locked --extra push python scripts/pr_supervisor.py --home /path/to/state dashboard --allow-host your-dashboard.ts.net
```

Open the dashboard over HTTPS, add it to the Home Screen, then open that app and
choose **bell → Enable notifications → Allow**. If an older Home Screen shortcut
opens in Safari, remove the shortcut and add it again. The cat icon's badge uses
the same grouped unseen count as the bell; opening the bell alone does not clear
it. **Disable notifications** removes this device's server subscription.

Subscribed devices keep their inbox and seen state on the dashboard server, so
updates continue while the phone app is closed. Each device has its own seen
state. The dashboard process must remain running and connected to GitHub and the
push service. Its worker checks the existing snapshots every 15 seconds and keeps
the existing overview refresh schedule active; updates can arrive after the next
GitHub refresh. The ten most recent items are displayed, while older unread items
still count. Failed deliveries retry with backoff; expired subscriptions can be
enabled again. Apple requires background pushes to display an alert as well as
updating the badge. Alerts replace the previous Babysitter alert in browsers that
support notification tags.

Push subscriptions, device inboxes, and VAPID signing keys are stored in private
`dashboard-push.json` and `dashboard-vapid.pem` files under the state directory.
Keep both files across restarts. Payloads are encrypted for the subscribed device;
only Apple, Google, and Mozilla push endpoints are accepted. No Apple developer
account or third-party notification service account is needed. Browser-only
notifications continue to work without installing the `push` extra.

Feedback handled by a successful repair stays handled when code moves to another
line or the same PR watch is re-registered. Changes to the comment text still need
fresh approval. Resolved GitHub review threads and empty reviews are removed from
pending feedback on the next successful poll. For feedback already addressed in
code but still unresolved on GitHub, use **Mark addressed** on the individual item.
This clears that item locally without starting a repair or changing GitHub; it
requires the displayed batch to still be current. Blocked repairs retain their
feedback until handled successfully or explicitly marked addressed.

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
Dashboard filter pickers, mobile sort pickers, and workspace clone, model, and
effort pickers are searchable: focus a picker and type to narrow its options by label (case-insensitive).
Use arrow keys and Enter to select, or click an option. Escape, Tab, and clicking
outside dismiss the list without changing the selection; reopening starts a fresh
search. The × button clears the option search; choose an **All** option to reset a
filter. No matches leaves the current selection intact. Refreshes preserve both
active filters and an unfinished option search, including filters in hidden tabs.
Long choices wrap in the list and show their full selected label beneath the input.
The two-choice Agent and repair-log source pickers retain native selects; sort
direction remains a button. These short controls do not need a search field.

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

## Watcher workspace actions

PR watches show **Draft** or **Ready for review** beside their monitoring status,
in both the watch list and details. The value follows the regular watcher poll;
older snapshots show **PR status pending** until refreshed. Completed PRs show
**Merged** or **Closed** instead. Branch watches have no PR review status.

Select a watch to **Open workspace** from its details, including branch watches
and completed PR watches. This uses the watch's recorded checkout, independently
of the open PR overview. **More actions** offers native focus and command copying;
**Reopen workspace** restores a closed workspace without starting an agent.
Missing or unrelated checkouts show **Workspace unavailable**.

## PR workspace actions

The **Actions** column opens a verified workspace in Collie, at `COLLIE_PUBLIC_URL`
from the dashboard's environment (default `http://127.0.0.1:8787`). Multiple matches open a chooser with
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

PRs with failing CI also offer **Handle failing tests** in the Actions column and
CI details. Issues offer **Handle issue** through the same dialog. PRs you authored
offer **Handle review comments** when a reviewer requested changes or review threads
remain unresolved; the Review status column shows both, and the **Needs changes**
filter lists them. Clicking either badge opens the review summaries and every
unresolved thread's comments, fetched on demand and cached like CI details until the
PR next changes. Its task asks the agent to address the feedback without replying
to or resolving threads on GitHub. Each Handle action prefills an
editable task and reuses the agent, model, and reasoning-effort selectors. **Handle**
starts a new workspace, including when a matching checkout already exists, so an
existing agent keeps its task. An in-progress launch is reused; after completion,
a later Handle submission can start another task in a separate checkout.

### Scheduled tasks

Tick **Start later** in the Create workspace, Clone and create, or Handle dialog to
choose a start time (in the browser's time zone, up to 30 days ahead) and press
**Schedule**. The request is checked as if it started now, then saved in the
workspace database with the chosen clone, agent, model, effort and task; nothing is
cloned or checked out until its time. A launch already running for the same item
does not block scheduling: the task waits for it to finish, then starts.

The **Scheduled** tab lists waiting tasks soonest first with **Cancel**, and the 50
most recent outcomes: started (with the launch's progress and **Open in Collie**),
cancelled, or not started with the reason, such as the item no longer being listed
or a workspace already created for it. Rows with a waiting task show its start
time. The dashboard process starts tasks, so it must be running: a task due while
it was stopped starts when it returns, and one more than a day overdue is marked
missed instead. If the dashboard stops while starting a task, the task is marked
**Check workspace** and never started again automatically. At most 100 tasks can
wait at once.

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
with an unused numeric suffix for collisions. That name lets the Workspaces tab link
the checkout back to its PR; herdr instead labels the workspace
`pr-<repo>-<head-branch>` (plus the same suffix) so it is recognizable. The adapter
runs the repository's portable worktree helper, `scripts/wt.py` (see [Worktree helper](#worktree-helper)),
as `wtpr` directly with `WT_MULTIPLEXER=herdr` and selects the agent explicitly. No
login shell wraps the helper: the user's interactive-shell Safehouse wrappers still
apply because the helper types the agent command into the herdr pane's interactive
shell rather than running the agent itself. Task text plus the canonical PR URL is
stored in a private file under `workspace-prompts/` outside the checkout and passed
with `--prompt-file`. No PR comments or CI watches are added.

**Previous prompts**, under the Task field, searches the tasks you started earlier
workspaces with (every word you type must appear, in any order and case) and copies a
chosen one into the Task field; **Forget** removes one. Tasks are kept in
`pr-workspaces.sqlite` and the picker offers the 500 most recently used; repeating a
task counts another use rather than adding a duplicate. A Handle prefill submitted
unedited is left out, because it is regenerated for each item. The first start after
upgrading seeds the history from the briefs already in `workspace-prompts/`.

**Model (optional)** and **Reasoning effort (optional)** each start at **Default**.
Blank fields are omitted from the launch command; the dashboard never writes agent
configuration or adds model instructions to the task. Model and effort choices reset
when switching agents, and effort choices follow the selected model. Requests can
also set either field independently. With the default model, effort compatibility
is resolved by the agent, since shell wrappers, provider settings and organization
policies can determine the effective model. The operation records the requested
`model` and `effort` (null for Default), not a claim about the provider's effective
configuration. Agent startup/provenance verification and duplicate-launch protection
remain in place. Open/reopen actions do not apply these settings to existing sessions.

Codex choices come from picker-visible entries and supported reasoning levels in
`$CODEX_HOME/models_cache.json` (normally `~/.codex/models_cache.json`). This is a
read-only, local cache: the dashboard never starts a Codex session to discover
models. Codex fetches its catalog per client version and every Codex on the machine,
including an auto-updated app-server daemon, rewrites the same cache, so the
dashboard compares the cache's `client_version` with `codex --version` for the
first `codex` on its PATH (re-checked every 10 minutes, sooner when that file is
replaced).
A matching list is offered and remembered in `codex-models.json` in the state
directory; while a different version owns the cache, the remembered list for the
installed CLI is offered instead, or only Default with a note when none is known.
This keeps a model only a newer Codex accepts away from an older CLI, which rejects
it as "not supported when using Codex with a ChatGPT account". When the version
cannot be determined, the last remembered list is offered, or the cache as is. A missing or incompatible cache
leaves Default available; use Codex normally to populate its cache. Cache entries
may become stale, and account/provider access is still enforced by Codex. No
default model is inferred from cache ordering. Claude
choices use the documented `fable`, `opus`, `sonnet`, and `haiku` aliases; Haiku has
no explicit effort choices. Aliases and supported levels require a current Claude
Code CLI and may be restricted or remapped by local/provider configuration.

Overrides are passed to the helper as `--model`/`--effort` next to `--name`,
`--no-focus`, `--repo-path`, `--worktree-root` and `--prompt-file`. The helper
validates both values syntactically before any Git or GitHub call and applies them
only when starting a new session. Codex receives `--model` and
`-c 'model_reasoning_effort="…"'`; Claude receives `--model` and `--effort`.
CLI configuration precedence and organization policies still apply, including
Codex managed new-thread defaults that can change when either override is supplied.
Keep `wt.py` alongside `pr_workspaces.py` when distributing the scripts; there is
no separate installed helper to update.

References (verified against installed CLI help and official documentation):

- [Codex configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference)
  describes model/effort overrides and managed defaults.
- [Codex model discovery](https://learn.chatgpt.com/docs/app-server#list-models-modellist)
  documents model-specific effort metadata; the local cache is a private CLI format,
  parsed defensively here to avoid starting an app server during dashboard reads.
- [Claude model configuration](https://code.claude.com/docs/en/model-config) documents
  aliases, model-specific efforts, and fallback/organization restrictions.
- [Claude CLI reference](https://code.claude.com/docs/en/cli-usage) documents session
  `--model` and `--effort` flags.

## Worktree helper

`scripts/wt.py` is a portable, stdlib-only Python implementation of the `wt`, `wti`
and `wtpr` shell helpers (with `wri`/`wtissue` as `wti` aliases). The `scripts/wt`,
`scripts/wti` and `scripts/wtpr` symlinks dispatch on their own name; `wt.py wtpr …`
with a leading subcommand does the same. It creates (or reattaches to) a worktree
under `~/src/worktrees/<repo>/` for a branch, a GitHub issue or a pull request, then
opens it in a multiplexer with the selected agent started in the left pane:

```sh
wt [--codex|--claude] [-r repo] [-p text|-F file] [--model id] [--effort level] <base> [branch]
wt [opts] <pr-number>                 # same as wtpr
wt [opts] issue <number|url> [branch] # same as wti
wti [opts] [--name n] [--label l] [--no-focus] [--repo-path p] [--worktree-root p] <number|url> [branch]
wtpr [opts] [--name n] [--label l] [--no-focus] [--repo-path p] [--worktree-root p] <number|url>
```

For branch worktrees, `wt` fetches the base from `origin` when that remote exists.
Without `origin`, it uses the requested local base instead: for example,
`wt -r repo main my-feature` creates `my-feature` from local `main` without network
access. Existing local branches are reused, and existing worktrees are reopened.

A branch that is already checked out is opened where it already lives -- the main
clone for `wt main`, or a worktree created under a different directory name --
instead of failing on git's refusal to check the same branch out twice. The reused
path is reported on stderr.

To use it from a shell, put `scripts/` on `PATH` or symlink the three launchers into
a `bin` directory; no `source` is needed. `WT_MULTIPLEXER` selects `herdr`, `tmux`,
`cmux`, `none` or `auto` (the default) from the environment or from `KEY=VALUE` lines
in `~/.config/worktree/config` (`$XDG_CONFIG_HOME` is honoured; the environment wins).
`auto` uses herdr when `herdr` is installed and either `HERDR_ENV=1` is set or
`herdr status server --json` reports a running server, then cmux when
`CMUX_SURFACE_ID` is set, then tmux, then `none`. A program cannot change its
caller's directory, so with `none` the helper prints the worktree path on stdout
(everything else goes to stderr); a shell function turns that into a `cd`:

```sh
wt() { local d; d="$(WT_MULTIPLEXER=none command wt "$@")" && cd "$d"; }
```

Prompts (`-p`, `-F`) are staged in a private `$TMPDIR/wt-prompt.*` file and the
typed agent command reads it back with `"$(cat '…')"`, so the line the multiplexer
types stays short whatever the prompt's size (canonical tty input silently drops
lines over 1024 bytes on macOS). A prompt only applies to a session being created;
reattaching to an existing worktree warns on stderr that the prompt was ignored.
Warnings and errors are prefixed `wt:`, `wti:` or `wtpr:`. `tests/test_wt.py` covers
the planning code without processes and the whole tool against temporary
repositories with fake `gh`, `herdr`, `tmux`, `cmux` and agent executables.

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
and needs no head metadata. The adapter runs the same helper as `wti` with
`--codex|--claude --no-focus --name <name> --repo-path <clone> --worktree-root
<root> --prompt-file <file> <issue-url>`. The worktree and branch name is `wti`'s
own default, `issue-<number>-<title-slug>` (or `issue-<number>` when the title yields
no slug), with a numeric suffix for collisions; the branch starts from the clone's
`origin/HEAD`, so no upstream check applies. The prompt file contains the task and
`Issue: <canonical URL>`. Associations, operations and logs share
`pr-workspaces.sqlite`; the `pr` column keeps its name and stores both kinds of id.

## GitHub credentials and prompts

Every `gh` process normally reads its token back from the system keyring, which on macOS means waiting on the login Keychain; pinentry-mac fetches the GPG signing passphrase from that same Keychain. A polling supervisor and dashboard start many `gh` processes at once, and one stalled keyring read used to leave dozens of them parked there, with your own `gh` and `git commit` queued behind them. The supervisor and dashboard therefore resolve the token once per process with `gh auth token`, hand it to each `gh` and `git` child through `GH_TOKEN` (so the keyring is never opened again), re-read it only when GitHub answers 401 or after six hours, and run at most four `gh` processes at a time per Python process. Children also get `GH_PROMPT_DISABLED`, `GH_NO_UPDATE_NOTIFIER`, `GIT_TERMINAL_PROMPT=0`, and `GCM_INTERACTIVE=never`, so an unattended command fails instead of waiting for a terminal or a dialog. When no token can be read, no `gh` process is started at all: `gh` would otherwise fall back to unauthenticated requests, still opening the keyring each time and burning the per-IP rate limit. The dashboard and watcher instead report why (`gh auth token` output) and retry after a minute. A launchd service or sandbox cannot answer Keychain prompts, so `gh auth token` fails there with "no oauth token found"; run `gh auth login --insecure-storage` (the token then lives in `~/.config/gh/hosts.yml`, mode 0600) or provide `GH_TOKEN` in that service's environment. The token stays in memory and child environments only; it is never written to state files or logs. Repair agents receive the non-interactive settings but not the token: they authenticate the same way your interactive sessions do. Set `GH_TOKEN` yourself to skip the lookup entirely.

## Development

Requires Python 3.11+, [uv](https://docs.astral.sh/uv/), Node.js 22.13+, npm, Git, and zsh (only to syntax-check the Safehouse launcher and as the fake herdr pane shell in tests; the worktree helper itself is plain Python). Python and Node dependencies are pinned in `pyproject.toml`/`uv.lock` and `package.json`/`package-lock.json`. No agent credentials or running herdr server are needed for tests.

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

An opt-in [workspace overview experiment](references/workspaces.md) adds a separate dashboard tab listing your local worktrees and herdr workspaces, the pull request or issue each one belongs to, whether that item is still open or already closed or merged, and the agents running in it. Selected workspaces can be cleaned up in batches: idle agents are asked to exit, the herdr workspace is closed, the worktree is removed, and the local branch is deleted when no commit can be lost. Uncommitted changes, commits only that branch has, and working agents are shown as blockers and skipped unless you override them per row in the confirmation dialog; a checkout an unfinished watch is using is never removed at all (watches marked `closed` or `stopped` no longer protect it), and a clone's own checkout only ever has its herdr workspace closed. Enable it with `{"enabled": true}` in `experiments/workspaces/config.json` under the dashboard state directory, then restart the dashboard; delete that configuration or set `enabled` to `false` to disable it. It has isolated scanning, cache, and errors, and everything is revalidated immediately before removal.

An opt-in [upstream test overview experiment](references/upstream-tests.md) adds a separate dashboard tab for confirmed failing tests on Galaxy dev and currently supported release branches, with grouping by test or branch/workflow. Enable it with `{"enabled": true}` in `experiments/upstream-tests/config.json` under the dashboard state directory, then restart the dashboard; delete that configuration or set `enabled` to `false` to disable it. It has isolated collection, cache, and errors. The first adapter reads pytest-html 4 reports from Galaxy-style test artifacts; missing, expired, unsupported, or oversized reports are labeled incomplete, not counted as failing tests. Collection uses bounded recent runs and does not imply complete branch coverage.

An opt-in [Sentry overview experiment](references/sentry.md) adds a dashboard tab with recent Sentry issues from mapped projects. The same error on several servers is grouped into one row and ranked by an explained severity score. Each group offers **Handle**, which starts a Codex or Claude agent in a new `sentry-<short id>` worktree (Claude gets a private Sentry MCP config for that launch only), and **Publish to GitHub**, which creates an issue only after an LLM sanitizer, a fixed pattern check and your review, then writes the link back to Sentry. Optional LLM triage runs in the supervisor, capped per day and paused when the remaining Claude quota drops below a set percentage. Enable it in `experiments/sentry/config.json` under the dashboard state directory with a `0600` token file, then restart the dashboard.
