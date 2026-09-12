# Shared watcher operations

Requires Python's standard library, `gh`, `git`, and an authenticated Codex or Claude CLI. Supports
macOS/Linux (POSIX locks/process groups). State lives in
`~/.local/state/babysit-pr`. Every command accepts `--home PATH` before the
subcommand; use one home to share the global concurrency limit.

## Ownership

Registration is inactive. Automatic exit and release is the default for Codex
in herdr when handing off a watch; no separate automatic-exit request is needed.
Respect an explicit manual-exit or live foreground monitoring preference.
Using the shared `--home`, `handoff WATCH_ID --pane PANE` schedules a detached
helper from the registered conversation. Include its unique marker in
the final response and finish the turn. The helper verifies the saved turn has
completed, the marker is visible in the target pane, the original foreground
process is unchanged, and the composer is empty. It sends Ctrl-D once, preserves
the shell/pane, and releases only after the original process has exited.

Automatic handoff supports the Codex TUI's faint “Ask Codex to do anything”
placeholder. Draft input, queued questions, dialogs, scrollback, a new turn, or an
unknown layout cancels the handoff. Checks immediately before sending reduce the
window for input races; herdr does not offer an atomic conditional key send.
After turn completion, a missing final marker or blank/missing input area gets
up to five seconds to render, bounded by the handoff timeout. Each retry rechecks
ownership, process identity, and unchanged idle session state. Draft input and
other cancellation conditions still stop immediately. The audit retains the
first and last failed ANSI screens and the latest screen for diagnosis.
Avoid typing into the pane during the brief handoff. Automatic initial exit is Codex-only; Claude uses manual Ctrl-D and release.

Otherwise, exit the original CLI with Ctrl-D and run the returned `release_argv`
command. A headless caller may release after its original agent process exits.
Release transfers session/worktree ownership to the service. Service locks protect
its workers; they cannot lock a manually launched TUI. Never manually release a
live conversation.

For Codex, the guardian runs `codex exec ... resume SESSION_UUID -` for each repair and exits.
With `--pane PANE`, herdr submits the bounded runner in the original shell and
streams agent output both to that terminal and the dashboard log. This applies
to repairs and successful-CI continuations. No interactive harness stays alive
between wakes. Pause before using the owned shell prompt; leave it empty while
watching. A busy/missing/replaced pane blocks the watch without a hidden fallback.
Pane moves are followed by stable terminal ID. Replaying an attempt command from
shell history cannot repeat the agent work. `--headless` is an explicit opt-out.

For an existing watch, use `--home STATE_DIR bind-pane WATCH_ID --pane PANE`.
Registration now requires `--pane PANE` or explicitly requested `--headless`.
Select `--agent claude` to resume Claude instead of Codex; the pane must match
that agent kind. Claude uses `claude --print --resume SESSION_UUID`, streams
readable text/tool output into the original pane, and saves its raw JSON stream
in `agent.log`. A final structured result must match the original session UUID.
Permission denials, CLI errors, or missing/invalid results block the watch.
The shared watcher handles CI waiting without LLM calls. Repairs keep the saved
conversation and existing worktree.

## Controls

For the local dashboard:

```sh
python3 <skill-dir>/scripts/pr_supervisor.py dashboard --open
```

It serves `http://127.0.0.1:8765` and refreshes saved watcher state every five
seconds. Search/filter watches, select one for CI details and the latest repair
logs, or expand the watcher service log. Select a watch and click **Cancel watch**
to stop it. A running repair finishes first; the dashboard shows cancellation
pending until it exits. Ended watches remain available under the Ended filter.
Cancellation uses the same stop action as the CLI. The dashboard uses Python's
standard library; no GitHub polling or agent runs are added. Ctrl-C stops the
dashboard while monitoring continues. `--port PORT` changes the port;
`--home PATH` before `dashboard` selects an existing watcher state directory.
An absent database shows an empty dashboard; access failures show an error.

Private tailnet access on this machine uses a Tailscale Service hosted by the
Mac mini, which is tagged `tag:server` (service hosts must be tagged nodes):

```sh
python3 <skill-dir>/scripts/pr_supervisor.py --home /private/tmp/babysit-pr-mvandenb dashboard --allow-host babysitter.tailfb45be.ts.net
tailscale serve --service=svc:babysitter --https=443 http://127.0.0.1:8765
```

The current running watcher uses `/private/tmp/babysit-pr-mvandenb`; use the same
`--home` for its dashboard and controls. This temporary location may be removed
by system cleanup; do not start an empty default queue and mistake it for this one.
Open `https://babysitter.tailfb45be.ts.net/` from the tailnet. The Python server
stays bound to loopback and accepts only the listed Host header; the former node
route on `mac-mini:8443` has been removed. Services are tailnet-only and cannot be
exposed through Funnel; do not enable Funnel on this node either. The policy file's
`autoApprovers.services` approves `svc:babysitter` and `svc:collie` for `tag:server`;
a service must be defined on the admin console Services page before the host
advertisement registers. Remove the route with `tailscale serve clear svc:babysitter`.
The dashboard process must remain running.

Collie is published the same way as `svc:collie` at `https://collie.tailfb45be.ts.net/`
(`tailscale serve --service=svc:collie --https=443 http://127.0.0.1:8787`), with
`COLLIE_PUBLIC_HOSTS=collie.tailfb45be.ts.net`, `COLLIE_SKIP_SERVE=1`, and
`COLLIE_PUBLIC_URL=https://collie.tailfb45be.ts.net` in
`~/.config/collie/.env` so Collie accepts that Host and no longer publishes its own
`mac-mini` route on restart.

```sh
python3 <skill-dir>/scripts/pr_supervisor.py status
python3 <skill-dir>/scripts/pr_supervisor.py handoff WATCH_ID --pane PANE
python3 <skill-dir>/scripts/pr_supervisor.py pause WATCH_ID
python3 <skill-dir>/scripts/pr_supervisor.py release WATCH_ID
python3 <skill-dir>/scripts/pr_supervisor.py stop WATCH_ID
python3 <skill-dir>/scripts/pr_supervisor.py start
```

Pause/stop let an in-flight repair finish. Wait for `paused`/`stopped` before
editing or manually resuming the conversation. Release resumes a paused/blocked
watch after its cause is resolved, preserving history and the remaining budget.
A stopped/closed watch can be registered again with fresh explicit scope.

Pause/stop also cancel a pending automatic handoff. Its default timeout is ten
minutes (`handoff --timeout SECONDS`). Failures return to `awaiting_release` with
a reason in `status`; inspect `handoffs/TOKEN.audit.json` and `.log`. A crashed
handoff may remain `handoff`: pause it and inspect the pane before manual exit
and release. An attempted handoff is never replayed, because a repeated Ctrl-D
could exit the shell. The final response reports scheduling, not confirmed exit.

`start` restarts the shared daemon after logout/reboot/failure. No login service
is installed. `release` starts it too. The daemon lock prevents duplicate services.
`serve --max-workers 2` runs it in the foreground; `start --max-workers N` sets the
limit when launching a new daemon. An existing daemon keeps its current limit.

Repair guardians survive watcher restarts and enforce their own timeouts. The
restarted service reconciles durable results without replaying ambiguous pushes.
A missing result after timeout plus a grace period blocks for inspection.

## Fork and branch CI

To continue the original task after green CI, add `--on-green 'AUTHORIZED_TASK'`
when registering. For an existing watch:

```sh
python3 <skill-dir>/scripts/pr_supervisor.py --home STATE_DIR on-green WATCH_ID \
  --instructions 'After verifying CI, finish the already requested draft PR.'
```

This continuation runs once, even if CI is already green when armed. A completed
continuation is not repeated on subsequent commits or service restarts. It shares
the repair budget/timeout. Missing checks, pending work, failures, conflicts, or
unprocessed reviews defer it. Blocked outcomes keep it pending for intervention.
Ordinary monitoring still continues afterward. Upgrading a running supervisor
requires restarting that service; existing repair guardians survive the restart.

Select the CI source at registration (also supported by `gh_pr_watch.py --once`):

```sh
# Upstream reviews/conflicts, Actions from the PR's source fork:
python3 <skill-dir>/scripts/pr_supervisor.py register --pr PR_URL --ci-repo head \
  --cwd /absolute/worktree --session SESSION_UUID --pane PANE --instructions 'AUTHORIZED_SCOPE'

# Branch on a fork, with no PR required:
python3 <skill-dir>/scripts/pr_supervisor.py register --repo OWNER/FORK --branch BRANCH \
  --cwd /absolute/worktree --session SESSION_UUID --pane PANE --instructions 'AUTHORIZED_SCOPE'
```

`--ci-repo OWNER/REPO` explicitly selects another Actions repository for a PR.
`head` resolves and pins the source repository at registration. `--repo` alone
selects the PR repository; it does not redirect a PR's CI to a fork.
Branch mode requires `--repo` and excludes `--pr`/`--ci-repo`.

Actions queries filter by exact SHA and branch, with pagination. The newest run
of each workflow/event supersedes older dispatches for that commit; rerun attempts
remain distinct failure events. Failed matrix jobs can wake repairs before the
whole workflow completes. Logs and authorized reruns target the selected CI repo.
No matching runs stays waiting; skipped/neutral runs alone do not mean green.
Only Actions is monitored in these modes. Fork green is not PR merge readiness.

Branch mode follows the remote head on every poll and rechecks it before repairs.
Unexpected local/remote differences block repairs. A deleted or inaccessible
branch eventually blocks after the normal poll-error budget. There is no PR
closure to end the watch: it continues after green until stop or a blocker.
To change a registered watch's source, stop it and register the intended scope
again. Existing registrations keep their source; no watch starts during a skill
update. See [GitHub's workflow-runs API](https://docs.github.com/en/rest/actions/workflow-runs)
for branch/SHA filters and run metadata.

## Permissions and scope

For Codex, registration records the rollout's latest model and sandbox mode. Repairs use
noninteractive approval policy `never`; permission failures become blockers.
Workspace-write adds the linked worktree's git common directory for commits.
Start the watcher under the same authorized external sandbox as the original
session. For a verified existing launcher, use a JSON argv prefix:

```sh
--codex-command '["/absolute/existing-launcher", "codex"]'
```

Use this machine's actual authorized invocation. Do not invent wrapper arguments,
copy bypass flags, or escape a denied sandbox. Shell functions are not executable
launchers. The service inherits startup environment; restart to pick up changes.

For Claude, registration validates a top-level saved transcript and preserves its
model. This user's default executable launcher is `scripts/claude_safehouse.zsh`.
It sources the existing `~/.config/safehouse/safe.env`, uses the verified
`--enable=playwright-chrome` and Python IPC profile, and executes Claude with the
same `--dangerously-skip-permissions` option as the user's shell function.
Safehouse supplies containment; `dontAsk` must not override that existing setup.
Missing configuration or a Safehouse failure aborts; there is no unsandboxed
fallback. The launcher passes the repair marker and existing cmux environment
names, without widening directory grants. A saved plan mode is still preserved.

`--claude-command` permits an explicit alternative argv prefix; those jobs keep
`dontAsk` unless separately configured. The `safehouse` permission setting is
accepted only with the verified launcher. Never pass bypass options to a raw
Claude executable as a workaround. Existing jobs store their launcher and need
a deliberate migration; changing the default does not silently rewrite them.

The helper checks the exact PR branch/SHA and clean worktree before launch. It
rejects another active registration for the same session/worktree/PR. Preserve
task scope in `--instructions`, including authorization for pushes, reruns,
responses, and rebases. The supervisor only reads GitHub; agents handle authorized
mutations. No real watch is started merely by installing/updating the skill.

## Events and diagnostics

- Default polling: 120 seconds. Five consecutive poll errors block with backoff.
- Review cursors and pending feedback commit together so crashes/queue delays
  cannot consume unseen comments. Pending reviews remain ignored; upstream author
  filtering still applies (trusted collaborators and Codex bots).
- CI signatures include SHA, failure identities, and rerun attempts. Unchanged
  acknowledged failures do not repeatedly wake agents. All wakes share a total
  repair budget, including subsequent SHAs. At most one rerun per repair wake.
- CI green is a local milestone; monitoring continues until closure/stop/blocker.
  Zero checks is not reported as green.
- `status` returns state; `supervisor.log` records transitions. `daemon.json` holds
  daemon settings. Per-attempt files under `runs/ATTEMPT/`: `agent.log`,
  `guardian.log`, `prompt.txt`, `reply.json`, `result.json`. `queue.sqlite` is durable
  state. These may contain private content and are created with private modes.
- No desktop/chat notifications or automatic log pruning are configured.

## Upstream provenance

Watcher, tests, and reference notes imported from
[`openai/codex` at 02a8f038b87ad34d4a1dc5058eda26972ed7aa6c](https://github.com/openai/codex/tree/02a8f038b87ad34d4a1dc5058eda26972ed7aa6c/.codex/skills/babysit-pr).
Local additions: shared supervisor, transactional snapshot collection, bounded gh
calls, failed/pending check exit-code handling, and event identity fields.

## Feedback approval and cleanup readiness

PR comments, inline review comments, and published reviews are collected with
existing author filters, then queued for human inspection. They do not themselves
wake an agent. In the dashboard, **Needs attention** includes pending feedback;
read it and click **Handle feedback** while the watch is watching. Approval is
bound to the displayed batch content. A stale batch is rejected; new or edited
comments need fresh approval. One click permits one bounded attempt, consuming
the ordinary repair budget. A failed attempt needs another approval after the
watch is deliberately released. Cancellation and pause keep their normal behavior.

CI repairs continue automatically, but their prompts contain only feedback
explicitly approved for that attempt. The gate does not eliminate injection
risk from approved comments, CI logs, repository files, or old conversation
history; existing sandbox and tool grants still bound the agent's capabilities.
Do not use an agent to click its own approval button.

Merge/closure comes from structured GitHub PR fields. It marks the watch closed
and **Ready for cleanup**, without fetching comments or CI and without launching
an agent. Dashboard **Needs attention** includes these ended watches. This means
PR monitoring is finished, not that local files have been checked for safe removal.
No session, pane, branch, worktree, or historical watch is automatically deleted.
Branch-only watches have no PR closure event; register a PR watch when appropriate.
