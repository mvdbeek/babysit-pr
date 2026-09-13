# Experimental workspace overview

This opt-in internal module adds a **Workspaces** dashboard tab listing the local Git worktrees and herdr workspaces you are working in, the pull request or issue each one belongs to, and whether that item is still open. It can clean up selected workspaces in batches: exit their agents, close their herdr workspaces, remove their worktrees, and delete their local branches. It registers no watches, starts no agents, and pushes nothing. Its own API, background scan, bounded cache, JavaScript, and stylesheet keep its failures inside this experiment.

## Activation

Create `experiments/workspaces/config.json` under the dashboard's `--home` directory, then start your dashboard normally:

```json
{
  "enabled": true
}
```

Optional keys: `src` (the directory scanned for clones, `~/src` by default) and `roots` (extra clone paths outside it). Configuration is read at dashboard startup. The experiment does no scanning and no network work when disabled. The existing authenticated `gh` executable needs read access to the repositories you work in; `herdr` is read through its socket API.

## What is listed

Every **linked worktree** of a scanned clone is a workspace, joined with any herdr workspace whose checkout is that path and the agents running in it. A clone's own checkout is listed only while herdr holds it open, and can never be removed here — cleaning it up closes its herdr workspace and nothing else. A herdr workspace with no Git checkout, or one whose checkout no longer exists, is listed as well so it can be closed. Discovery is Git-first: paths, branches, heads, remotes, and branch upstreams come from `git worktree list` and the clone's shared config, never from a workspace label or a directory name.

The base repository is the `upstream` remote when one exists, otherwise `origin` or, when `origin` is a fork, its parent. That answer is cached for a day; a failed or budget-exhausted lookup is never cached as an answer.

## Opening a workspace

The **Actions** column offers **Open workspace** for an existing herdr workspace and **Create workspace** for a checkout without one. Both open Collie in a new tab, with an **Open in Collie** fallback link if popups are blocked. Creation attaches a herdr workspace to the listed checkout without starting an agent or changing native focus. Existing agents are preserved. The action revalidates the listed checkout and shares the cleanup/creation lock so repeated clicks reuse the workspace instead of duplicating it. Errors appear beside the button and can be retried.

`POST /api/workspace-open` accepts a listed workspace `key` with the `X-Babysit-Action: workspace-open` header and the dashboard's usual origin protections. The server resolves the checkout and Collie URL; the client supplies no command or destination.

## How pull requests and issues are matched

A checkout is matched to the pull request and issue it belongs to, from evidence the repository agrees with (a name-derived pull request and a branch-derived one can both appear when they differ):

- The branch pushed to a fork: `head=<owner>:<branch>` against the base repository. Tracking configuration usually names the **base** branch a topic branch was cut from, so an upstream only counts as a head when it tracks the same branch name. The fork owner from `origin` is always tried as a head. Branches named like a base or release branch (`main`, `dev`, `release_26.1`, `25.0`) are never matched by head.
- A `wti`-style `issue-<number>` branch or directory name.
- A `wtpr`-style `pr-<owner>-<number>` directory or branch name, only when `<owner>` owns the resolved base repository.

Each result is shown with its state: **Open**, **Draft**, **Closed**, or **Merged**, linked to GitHub. A checkout with no match shows _None found_, and one whose lookup has not completed yet shows _Checking GitHub…_. Matching is a strong hint, not proof: a branch can be reused, and a fork can carry several pull requests for one branch (the newest wins).

Row status combines that with local state. **Ready to clean up** means every matched item is closed or merged and nothing blocks removal. **Open work** means at least one matched item is still open. **Needs an override** means something would be lost. **Protected** means the workspace can never be removed here.

## What blocks a cleanup

Local state is read without touching it: `git status --porcelain` for uncommitted changes, and `git rev-list HEAD --not --remotes --branches --tags` (excluding this branch) for commits no remote ref and no other local branch carries. Blockers are shown in the row and skipped during a batch unless the confirmation dialog's per-row **Remove anyway** override is ticked. That override approves exactly the conditions listed beside it: a condition that appears between confirming and removing — an agent that started writing, a change made in another terminal — stops the cleanup and is named in the result instead of being swept along with the tick. Counts may change freely; kinds may not. The blockers are:

- uncommitted changes, including untracked files
- commits only this branch has, which removal really would lose (100+ is a ceiling, not an exact count)
- an agent still `working` or `blocked` in that workspace
- local state that could not be verified
- a clone's own checkout, where only the herdr workspace is closed

Two conditions can never be overridden: a checkout a **registered watch** is using (cancel the watch in the Watcher tab first) and a herdr workspace whose checkout lies outside the scanned clones. A cleanup target whose clone is not one the scan reaches is refused outright, however the request arrives, so the blast radius is never wider than the list. Everything recorded here is re-read immediately before anything is removed, so a stale selection is never acted on; a workspace that changed in the meantime is skipped or fails rather than removed on old evidence.

## What a cleanup does

Selected workspaces are cleaned up one at a time, under the same per-clone lock the PR workspace actions take, so a cleanup cannot race a workspace creation in that clone. For each one:

1. Every idle agent in the workspace is asked to exit (`herdr agent send-keys … ctrl+d`) and given up to five seconds. A working agent is never sent keys: approving its removal closes its workspace, which ends the pane. Agent transcripts are written as the agent runs and survive either way, but text typed into a pane and never submitted does not.
2. Each herdr workspace owning that checkout is closed, which ends its panes.
3. The worktree is removed with `git worktree remove` (`--force` when overridden).
4. The local branch is deleted with `git branch -D` when no commit can be lost — every commit is on a remote ref or another local branch, which is the normal state after a squash merge, where `git branch -d` would refuse. When local state could not be verified, the safe `git branch -d` decides instead, and a branch Git refuses to delete is **kept**: the result says so and the worktree stays removed.

Clones, remote branches, transcripts, and the watcher queue are never touched. A failure stops that workspace and the batch continues with the next one; each result keeps its own status, message, and steps. Progress is in memory only: a dashboard restart during a batch loses the report, not the work, and the next scan shows what actually remains. One batch runs at a time, with at most 50 workspaces per batch; requesting another while one runs is refused, reported in the dialog, and leaves the selection intact. An unexpected failure is recorded against its own workspace and the batch continues.

## Refresh, limits, and failure states

The inventory is rescanned in a background daemon thread at most every five minutes, and **Refresh** brings the next scan forward by at most once a minute. While the tab is visible the browser reads the snapshot every five seconds. A scan runs `git status` and `rev-list` per checkout across eight threads, at most 300 checkouts, and spends at most 100 GitHub requests. An exhausted budget leaves links stale rather than wrong, reports it, and fills them in on later scans. An open pull request or issue is cached for 15 minutes, a closed or merged one for six hours, and "no pull request found" for one hour, at most 500 entries in a state file capped at 4 MiB, written `0600` through a temporary file and an atomic replace. Base repositories expire with their own day-long window, an old cache format is discarded safely, and a cache too large to persist is reported rather than dropped silently.

Loading, stale results, an empty inventory, per-repository warnings, and API errors have separate messages. An unavailable herdr still lists worktrees, without workspace or agent information. A failed scan keeps the last inventory and marks it stale. Run one dashboard per state directory.

## Disable or remove

Set `"enabled": false` (or remove its config file) and restart the dashboard. This hides the tab and stops all scanning. The experiment directory can then be deleted without touching watcher, PR, or issue state.

To remove the code, delete `scripts/workspace_overview.py`, `assets/dashboard/workspaces.js`, `assets/dashboard/workspaces.css`, its two test modules, and this document. Remove the small integration hooks in `scripts/dashboard.py` (construction, injected server field, one endpoint, two POST actions, two assets), `assets/dashboard/index.html` (scripts/style, tab, panel, dialog), and the `workspaces` page entry/visibility event in `app.js`. It reuses the Git discovery helpers in `scripts/pr_workspaces.py` in one direction only; nothing in the watcher, overview, or workspace-action code depends on this module.

Validation uses temporary clones with real worktrees, fake herdr and agent executables, fake GitHub responses, isolated state directories, and temporary browser servers. Screenshots cover the desktop and mobile listing and the confirmation dialog. Run all checks with `uv run --locked tox`.
