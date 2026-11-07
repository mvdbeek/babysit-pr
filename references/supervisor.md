# Shared watcher operations

Requires Python's standard library, `gh`, `git`, and an authenticated Codex or Claude CLI. Supports
macOS/Linux (POSIX locks/process groups). State lives in
`~/.local/state/babysit-pr`. Every command accepts `--home PATH` before the
subcommand; use one home to share the global concurrency limit.

## Ownership

Registration is inactive. Automatic exit and release is the default for Codex
and Claude in herdr when handing off a watch; no separate automatic-exit
request is needed.
Respect an explicit manual-exit or live foreground monitoring preference.
Using the shared `--home`, `handoff WATCH_ID --pane PANE` schedules a detached
helper from the registered conversation. Include its unique marker in
the final response and finish the turn. The helper verifies the saved turn has
completed, the marker is visible in the target pane, the original foreground
process is unchanged, and the composer is empty. It sends Ctrl-D, confirms Claude’s exit prompt if needed, preserves
the shell/pane, and releases only after the original process has exited.

Automatic handoff recognizes the Codex TUI's faint “Ask Codex to do anything”
placeholder and Claude's empty “❯” prompt between its two horizontal rules. A
Codex turn is complete at its rollout `task_complete` event, a Claude turn at
the transcript's `turn_duration` record. Draft input, queued questions, dialogs,
scrollback, a new turn, or an unknown layout cancels the handoff. Checks
immediately before sending reduce the window for input races; herdr does not
offer an atomic conditional key send. After turn completion, a missing final
marker or blank/missing input area gets up to five seconds to render, bounded
by the handoff timeout. Each retry rechecks ownership, process identity, and
unchanged idle session state. Draft input and other cancellation conditions
still stop immediately. The audit retains the first and last failed ANSI
screens and the latest screen for diagnosis. Avoid typing into the pane during
the brief handoff. Claude asks for a second Ctrl-D within about a second; the
helper repeats the key only while the original Claude process is still the pane
foreground and shows that hint. Before confirming, it rechecks ownership,
process identity, transcript state, and the empty composer; it never repeats
a key after observing the shell. As with the initial key, these checks cannot
eliminate the race between inspection and key delivery.

Otherwise, exit the original CLI with Ctrl-D and run the returned `release_argv`
command. A headless caller may release after its original agent process exits.
Release transfers session/worktree ownership to the service. Service locks protect
its workers; they cannot lock a manually launched TUI. Never manually release a
live conversation. Each repair holds a flock (`scripts/session_locks.py`), one lease
per machine and session owned by the controller, which every supervisor home there
contends for: a local session's lock lives in `CLAUDE_CONFIG_DIR`/`CODEX_HOME` under
`babysit-pr-locks/`, a worker machine's in
`~/.local/state/babysit-pr/session-locks/<machine>/` on the controller. The same
session id on two machines takes two leases. A `<session>.orphan` file beside a
worker session's lock keeps it held until cleared.

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
For another subscription login, add `--claude-account NAME` at registration.
Names refer to `~/.claude/accounts/NAME` (`default` selects `~/.claude`). The
recorded transcript store and literal `CLAUDE_CONFIG_DIR` value are retained on
the watch and restored for every repair, before acquiring the session lock.
Without a name, registration searches the known stores and infers the directory
from the transcript; `--rollout` resolves an ambiguous session. Create a named
login with `scripts/claude-account login NAME`. Existing watches without a saved
directory continue to use their original environment behavior.
While a dashboard is open, the daemon also reads subscription usage for Codex and
each Claude login (at most every 5 minutes, off its loop thread) into
`llm-usage.json`; the dashboard uses it to default new tasks to the login with
the most quota left.
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
pending until it exits. Ended watches remain available under the Ended filter;
the five-second poll carries only their overall CI result, and selecting one loads
its check and failed-job lists. Cancellation uses the same stop action as the CLI. The dashboard uses Python's
standard library; no GitHub polling or agent runs are added. Ctrl-C stops the
dashboard while monitoring continues. `--port PORT` changes the port;
`--home PATH` before `dashboard` selects an existing watcher state directory.
An absent database shows an empty dashboard; access failures show an error.

For private tailnet access, publish the dashboard as a Tailscale Service from a
host tagged, for example, `tag:server` (service hosts must be tagged nodes). With
`TAILNET` standing for your tailnet's DNS name:

```sh
python3 <skill-dir>/scripts/pr_supervisor.py dashboard --allow-host babysitter.TAILNET.ts.net
tailscale serve --service=svc:babysitter --https=443 http://127.0.0.1:8765
```

The watcher's default home is `~/.local/state/babysit-pr`, so commands need no
`--home`. Keep the home out of `/tmp`: macOS `tmp_cleaner` deletes `/tmp` files
that go unread for three days.
Open `https://babysitter.TAILNET.ts.net/` from the tailnet. The Python server
stays bound to loopback and accepts only the listed Host header. Services are
tailnet-only and cannot be exposed through Funnel; do not enable Funnel on the
host either. A policy file's `autoApprovers.services` can approve `svc:babysitter`
and `svc:collie` for `tag:server`; a service must be defined on the admin console
Services page before the host advertisement registers. Remove the route with
`tailscale serve clear svc:babysitter`. The dashboard process must remain running.

Every device the tailnet policy lets reach the service can use the whole dashboard,
including worker machines, which reach it for `/api/worker/` (see "Watches on
workers"). The Host check is no boundary against them: `tailscale serve` picks the
service by TLS server name and passes the client's Host through. What it does replace
is `X-Forwarded-For`, which it sets to the tailnet address the request came from. A
request whose `X-Forwarded-For` is any registered machine's `tailnet_ip` (enabled or
not) may only POST `/api/worker/register` or `/api/worker/handoff`, exactly those
paths; every other method and route, static files included, answers 403. A request
with more than one `X-Forwarded-For`, or one that is not a bare address (a port,
brackets, a zone, a list), did not come from the proxy and is refused the same way.
When `machines.json` cannot be read, every request carrying `X-Forwarded-For` is
refused too, except those two worker POSTs, which the token and address checks below
then refuse. Requests without that header come from this machine directly (the
controller's own tools) and are not fenced. `tailscale serve` answers on IPv4 and
IPv6, so record both of a worker's addresses: one recorded without the other leaves
the other family unrecognised. The dashboard prints a warning at startup naming
workers without an IPv4 and an IPv6 `tailnet_ip`, `/api/machines` shows them with
`fenced: false`, and `worker-token` warns when it issues them a token.

`--require-tailscale-user` (off by default) also refuses, through the proxy, every
request but `/api/worker/` that lacks `Tailscale-User-Login`. `tailscale serve`
(checked against 1.102.4, `ipn/ipnlocal/serve.go`) deletes any `Tailscale-User-*`
header the client sent, then sets them only for a device owned by a user, never for
a tagged node, so this keeps every tagged node out, a worker without `tailnet_ip`
included. It also keeps out tagged browsers, such as the controller's own browser
when it opens the tailnet URL (it is `tag:server`); use `http://127.0.0.1:8765`
there.

Collie can be published the same way as `svc:collie` at `https://collie.TAILNET.ts.net/`
(`tailscale serve --service=svc:collie --https=443 http://127.0.0.1:8787`), with
`COLLIE_PUBLIC_HOSTS=collie.TAILNET.ts.net`, `COLLIE_SKIP_SERVE=1`, and
`COLLIE_PUBLIC_URL=https://collie.TAILNET.ts.net` in Collie's `.env` so Collie
accepts that Host and does not publish its own node route on restart.

Dashboard workspace links point at `COLLIE_PUBLIC_URL` from the dashboard's own
environment, falling back to Collie's loopback default `http://127.0.0.1:8787`.
Set the same value for the dashboard process, for example in its launchd plist's
`EnvironmentVariables`.

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
is installed. `release` starts it too. The dashboard's Services dialog does the same
restart from the page, and can also restart a launchd-owned dashboard onto the
checkout's current code. The daemon lock prevents duplicate services.
`serve --max-workers 2` runs it in the foreground; `start --max-workers N` sets the
limit when launching a new daemon. An existing daemon keeps its current limit.

Repair guardians survive watcher restarts and enforce their own timeouts. The
restarted service reconciles durable results without replaying ambiguous pushes.
A missing result after timeout plus a grace period blocks for inspection.

Repair guardians clean up their agent's process group on completion, timeout,
SIGTERM, and SIGHUP, including headless repairs. Bounded workspace and herdr
commands likewise clean up their own process groups on exit or errors. This
does not cover children that deliberately detach into another process group,
or a guardian killed with SIGKILL. A process parented by launchd is not by itself
evidence of a leak: terminal prompt plugins can intentionally detach workers.

A repair of a watch on a worker keeps its guardian here and runs only the agent
there (`scripts/remote_repair.py`). The guardian holds an ssh connection to the
worker half (`pr_supervisor.py _repair_worker`), whose stdin is its lifeline: when it
ends, the worker half stops the agent's process group (5 s grace). SIGTERM or SIGHUP
to the guardian ends the lifeline first, waits up to 15 s for the worker half to
exit, stops the ssh client and then asks the worker to cancel (`_repair_cancel`); a
guardian killed with SIGKILL ends the lifeline too. So, unlike a local repair, a
remote one does not survive a controller restart, reboot or network loss: it is
killed there, its watch blocked, and a later release retries it; nothing reattaches.
The worker enforces the repair timeout; the guardian gives ssh 60 s more, and the
service's own missing-result grace remains the backstop. When ssh fails mid-repair
(exit 255 or that timeout), the guardian cannot tell whether the agent still runs:
the watch is blocked as "unreachable mid-repair", and a tombstone beside the
session's lease keeps the session held (no repair, no dashboard resume) until the
service, once a minute, finds on the worker (`_repair_status`) that the run has a
result or its worker half is gone. A worker's sshd with `ClientAliveInterval 15`
and `ClientAliveCountMax 4` (provisioning sets it) ends a partitioned session's
lifeline within about a minute.

### Dashboard sign-in

`dashboard` takes `--auth report|enforce|off` (default `report`), `--auth-home DIR`
(default `~/.config/babysit-pr-auth`) and `--oidc-config FILE`. Report mode turns on
only once `AUTH_HOME/oidc.json` exists, and it refuses nothing. It counts each request
that enforce mode would refuse, by method, route and credential kind, and logs a line
at most once a minute per route and kind, never with a key, cookie or query string.
Enforce answers 401 without a credential and 403 when a key's scope does not cover
the route. It refuses to start until Phase 2 moves the worker, extension and push tokens
out of the sandbox-writable state home. The static shell, `/auth/*` and `/api/session` stay public. Worker,
extension and push-device routes keep checking their own tokens.

- **Login:** `GET /auth/login` → Keycloak (confidential client, PKCE S256, state and nonce) → `GET /auth/callback`. The callback validates the RS256 ID token and requires the `babysitter-admin` client role. It then sets an `HttpOnly; SameSite=Strict` session cookie, `Secure` everywhere but plain-http loopback. Sessions last 7 days idle and 30 days absolute on https, and 1 h idle and 8 h absolute on plain-http loopback. `POST /auth/logout` with `X-Babysit-Action: logout` ends the session and returns Keycloak's end-session URL.
- **Keys:** `bsk_<kind>_…`, stored as SHA-256 with scopes, sent as `Authorization: Bearer`. Manage them with `auth keys create --kind read|admin|worker|ext|push [--scope …] [--expires-days N]`, `auth keys list` and `auth keys revoke ID`. `auth admin-key [--rotate]` writes `AUTH_HOME/admin-key` (0600). `auth launch-key --agent A --cwd D [--origin user|cron|subtask]` mints a 7-day launch key whose binding records who started the agent.
- **Break-glass:** `auth login-link [--port 8765]` prints `…/auth/break-glass#t=…` links. Each is valid once for 10 minutes and gives a 12-hour session, with Keycloak up or down.
- **Report:** `auth report [--reset]`, `GET /api/auth-report` and `auth` in `/api/status`.
- **Sessions:** `auth sessions list` and `auth sessions revoke ID|--all`. A revocation reaches a running dashboard within a minute, or at its next idle-time renewal.

Every `auth` command refuses to run inside a Seatbelt sandbox. The Keycloak client
settings and `oidc.json` format are in [auth.md](auth.md).

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
A launcher that runs through Agent Safehouse always gets `danger-full-access`:
Seatbelt profiles cannot nest, so Codex's own sandbox fails every command with
`sandbox_apply: Operation not permitted`, and Safehouse already contains the
process, just as the user's `safe codex` shell function does. A launcher that
runs through `scripts/babysit-bwrap` gets the same, for the same reason: the
external sandbox is the boundary.
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
fallback. The launcher passes the repair marker, existing cmux environment
names, and the non-interactive git/gh settings (`GIT_TERMINAL_PROMPT=0`,
`GH_PROMPT_DISABLED`, `GH_NO_UPDATE_NOTIFIER`, `GCM_INTERACTIVE=never`) that
every repair receives, without widening directory grants. The shared GitHub
token the supervisor uses for its own polling is not passed to agents. A saved
plan mode is still preserved.

`scripts/sandbox.py` owns this launcher choice and every other sandbox detail
(typed launch prefixes, account environment, launcher detection). The sandbox is
external to the agent harness: agents run with their own permissions off inside
it, so a new harness only supplies its launch argv. The one exception is a Claude agent
started with `wt --permission-mode auto`, which runs in Claude's auto mode inside the
same sandbox; repairs, cron jobs and dashboard launches never pass it. The backend follows the
platform (`safehouse` on macOS, `bwrap` on Linux). Nothing in the environment
selects it, since an agent sets the environment of the launches it starts; the
tests set `sandbox.DEFAULT`.

On Linux the default Claude launcher is `scripts/babysit-bwrap` (bash 4.1 or
later), a bubblewrap wrapper taking Safehouse's `--add-dirs`, `--env-pass` and `--enable=docker,playwright`
options (and `--enable=gpg`, below), plus `--claude-config-dir` or `--codex-home`, before `--` and the command.
It mounts the system read-only, gives the agent an empty `$HOME`, and allows
writes only to `--add-dirs`, the one agent configuration directory it is given
(exported as `CLAUDE_CONFIG_DIR` or `CODEX_HOME`; Claude always gets an explicit
one, so its `.claude.json` lives inside it; a missing one is refused rather than
exported into the empty home) and `~/.cache/babysit-sandbox/<hash of that
configuration directory and the enabled grants>`, a cache used only by sandboxed
agents of that configuration (`XDG_CACHE_HOME`, `UV_CACHE_DIR`, `PIP_CACHE_DIR`,
`npm_config_cache`, `PLAYWRIGHT_BROWSERS_PATH`). Each set of grants that reaches
beyond the sandbox (`--enable=docker`) gets its own, so a launch with Docker never
runs downloads that a launch without it could have replaced. Host caches are
never writable: code planted there would run outside the sandbox.

What a harness runs from its configuration directory is bound read-only on top of
it in every launch, granted or not, so no agent leaves code there for the next
launch of the same account: for Claude `settings.json` (hooks, status line, helper
commands), `CLAUDE.md`, `agents`, `commands`, `hooks`, `output-styles`, `plugins`,
`rules`, `session-env` (scripts Claude sources before Bash commands), `skills` and
`workflows`; for Codex `config.toml`, `hooks.json`, `AGENTS.md`,
`AGENTS.override.md`, `packages`, `plugins`, `prompts`, `rules` and `skills`. A
missing one is first created empty (`{}` for JSON), since an agent could otherwise
create it for the next launch; a link there is refused, since binding it would
follow it outside the sandbox (copy a shared `settings.json` into each account
instead of linking it). Install plugins and skills, and change these settings,
outside the sandbox. When launching `codex` directly with its working directory
explicitly included in `--add-dirs`, the wrapper passes that exact directory as a
trusted project in a launch-only `-c` override. This avoids Codex's folder-trust
dialog trying to save to the read-only `config.toml`, including for linked
worktrees whose main repository has not been trusted. It does not persist trust
or trust sibling checkouts; other settings still need to be changed outside the
sandbox. Claude's `.claude.json` stays writable, since Claude rewrites
it constantly, and any agent of the account can add MCP servers to it: a launch
with `--enable=docker` and a Claude configuration must run `claude` itself, which
babysit-bwrap gives `--strict-mcp-config`, so it starts no MCP servers at all.
Each sandbox gets its own empty shell snapshot directory (Claude's
`shell-snapshots`, Codex's `shell_snapshots`, sourced before every command), an
`--add-dirs` grant that holds, equals or lies inside the configuration directory
is refused (it could move the directory, read-only entries and all), and a link
at the default account's `accounts` is refused. The wrapper
discovers nothing from its working directory or git, whose files the agent can
rewrite; babysit passes the worktree and its git common directory in `--add-dirs`
from its own records (the worktree `wt` created, the job `cwd` that the repair
just verified), and takes that common directory only when it registers the
worktree back. A main checkout whose `.git` is also the common directory of
other registered worktrees is refused (use a linked worktree): its agent could
swap that directory for a link just before another worktree's sandbox binds it.
Every writable path must be absolute and is resolved first; `/`, `$HOME` and its
ancestors, and anything equal to, inside or holding `~/.ssh`, `~/.gnupg`,
`~/.config/gh`, `~/.sentryclirc`, `~/.config/matrix-commander`, `~/.config/babysit-pr`, `~/.local/state/babysit-pr`,
`~/.local/bin`, the toolchains that also run outside (`~/.local/share/uv`,
`~/.local/share/claude`, `~/.nvm`, `~/.npm-global`), `~/.docker` (credential
helpers and CLI plugins), `~/.config/herdr` (its socket drives panes outside the
sandbox), the whole sandbox cache root, a shell or session startup file
(`.bashrc`, `.bashrc.d`, `.bash_aliases`, `.profile`, `.bash_profile`,
`.bash_login`, `.inputrc`, the zsh files, `~/.config/fish`, tmux's configuration,
`.pam_environment`, `~/.config/autostart`, `~/.config/environment.d`), a user
service definition (`~/.config/systemd`, `~/.local/share/systemd`,
`~/.local/share/dbus-1`) or git's user configuration (`.gitconfig`,
`~/.config/git`) are refused, as are newlines. The agent homes `~/.claude` and
`~/.codex` are refused too, except as the matching configuration directory:
`--claude-config-dir` may be `~/.claude` itself or one of its
`accounts/<name>`, and `--codex-home` may be `~/.codex`. When the default
account `~/.claude` is bound, its `accounts` directory is always covered by an
empty tmpfs, so that agent never reaches the named accounts nor plants a link
where they will be. babysit-bwrap also
refuses its own directory and, when it runs from a checkout's `scripts/`, that
checkout and its git common directory (read from the `.git` file or directory,
without running git there), since an agent writing either would change what the
next launch runs. Run babysit from a deployed checkout that agents never work in;
they can still work on babysit-pr in any other clone and its worktrees. Protected entries
are resolved whether or not they exist, through linked parents and dangling
dotfile links. Where bwrap has `--bind-fd` (bubblewrap 0.10.0 and later, detected
from `bwrap --help`), each writable directory is opened once after it is checked,
its descriptor must still resolve to the checked path, and bwrap binds that
descriptor, so a path swapped after the check cannot redirect the bind. Read-only binds (system, toolchains, `~/.config/gh`, `~/.sentryclirc`, `~/.config/matrix-commander`) follow the
writable ones, so none can be shadowed. It unshares PID, IPC and UTS
namespaces, dies with its parent, and clears the environment except `PATH`,
`HOME`, `TERM`, `LANG`, `USER`, the variables above and the `--env-pass` names.
It starts a new session (which blocks `TIOCSTI` terminal injection) unless stdin
is a terminal and `/proc/sys/dev/tty/legacy_tiocsti` reads `0`, so interactive
agents keep job control only where the kernel already blocks injection.
`--print-argv` prints the bwrap argv, NUL-separated, without running it. Typed
launches on Linux invoke the wrapper directly; there are no shell functions there
to rely on.

Only the selected agent's account is mounted. A Codex sandbox has no host Claude
login, even when `HOME`, the username and `/usr/bin/claude` match the host. Signing in
again on the host cannot change an existing mount namespace. The other CLI's command
name on the sandbox's `PATH` gives an isolation diagnostic instead of misleading
login advice. An explicit path nested inside a shell can bypass that diagnostic,
but still sees the same isolated HOME. This helper is guidance; authorization stays
with the launcher and broker. A host update requires a fresh sandbox launch to get
the helper and environment below.

Save reports in `$BABYSIT_ARTIFACT_DIR`, created under the persistent sandbox cache
as `artifacts/`, or in an explicitly granted worktree. For example:

```sh
printf '%s\n' 'Review findings for COMMIT' > "$BABYSIT_ARTIFACT_DIR/review.txt"
printf '%s\n' "$BABYSIT_ARTIFACT_DIR/review.txt"
```

The printed absolute path also names the file on the worker host. Other home paths,
including `~/src/agent-artefacts/outbox`, are temporary unless explicitly granted.
The artifact directory inherits the cache's agent configuration and grant separation:
another account or a launch with different grants cannot use it as a shared inbox.
Keep secrets out of reports and transcripts; hand another reviewer the repository's
exact commit/diff or an explicitly granted artifact path rather than copying account
stores or widening access to a private cache.

Provisioning a Linux worker (bubblewrap under AppArmor, rootless Docker, accounts)
is in [the worker setup checklist](worker-setup.md).

Known gaps of the Linux sandbox:

- The worktree's git directory, including its hooks and config, stays writable
  so the agent can commit. git run outside the sandbox in that checkout later
  (by you or by babysit) can execute what the agent left there.
- The network namespace is the host's: the agent reaches services on localhost
  and abstract Unix sockets, including an X11 display and a D-Bus session bus
  that listen on them, which can start programs outside the sandbox. Accepted
  for now; run the worker headless, without a desktop session for its user.
  Egress isolation is a later step.
- The rest of the agent configuration directory stays writable: `.claude.json`
  (MCP servers, which a granted launch ignores but an unsandboxed one starts),
  transcripts and auto-memory under `projects/`, and any script there that a
  read-only hook or status line command names. Hooks and commands must name
  files the sandbox cannot write (inside `hooks/`, or outside the account).
  Accepted: keep accounts used sandboxed separate from accounts used unsandboxed.
  Agents of one account can also write each other's transcripts and memory, so a
  launch with a grant trusts every agent of its account not to steer it through
  them.
- The git common directory of a repository is writable by every agent working in
  one of its worktrees, and its `config` and `hooks` run code in whichever
  sandbox runs git next (`core.hooksPath`, `core.fsmonitor`, filters, aliases,
  `gpg.program`). Enabling Docker for one worktree therefore trusts every agent
  sharing that clone. Making `hooks` read-only would not help: `config` must stay
  writable for `git branch`, `git push -u` and `git worktree add`, and alone can
  run anything, while read-only hooks would break `pre-commit install`. Give
  workspaces that need Docker a clone of their own.
- With a bwrap older than 0.10.0 writable directories are bound by path, so a
  process that can write a bound directory's parent could swap it for a link
  between the check and the bind. Refusing shared main checkouts closes the
  case babysit creates itself; extra `--add-dirs` grants whose parents another
  sandbox can write keep that race. Prefer bubblewrap 0.10.0 or later.
- `--enable=docker` refuses the system daemon's socket, which is root on the
  machine, and binds only `$XDG_RUNTIME_DIR/docker.sock`. Even a rootless daemon
  gives up the filesystem boundary (`docker run -v $HOME:/h` reaches every file
  of the user), so enabling Docker trusts the agent with them.
- `--enable=gpg` mounts a throwaway `~/.gnupg` (copies of `pubring.kbx` and
  `trustdb.gpg`, a `gpg.conf` with `no-autostart`) and, at the gpg-agent socket
  path, a per-launch proxy (`scripts/gpg_agent_proxy.py`) that passes on only the
  commands gpg signs with, so git signs through the agent; the private keys, any
  passphrase file, the agent's configuration and its real socket stay outside. The
  agent is still a signing oracle: while it runs, the sandboxed agent can have it
  sign arbitrary data with the unlocked key, not only its own commits, as on
  macOS. Signing is a grant: it keys its own sandbox cache, and a granted Claude
  gets `--strict-mcp-config`. Setup is in
  [the worker checklist](worker-setup.md#signing-with-a-gpg-subkey).
- The dashboard's Docker toggle is macOS-only for now: it inspects Seatbelt, and
  its restart looks for the launcher in the agent's parent process, which under
  babysit-bwrap is `bwrap` itself, so a restart fails closed.

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

### Host Git in agent checkouts

A sandboxed agent can write its worktree and the clone's git common directory, so the
repository config, hooks and `info/attributes` are the agent's. Every Git command the
supervisor, dashboard, cron jobs and `wt` run on the host in such a checkout goes
through `scripts/safe_git.py`. It disables hooks, fsmonitor, pagers, external diffs and
textconv, the repository's ssh/askpass/proxy programs and credential helpers, every
transport but https, ssh and local paths, and those too outside a fetch (so no
`git-remote-*` helper starts, whatever `protocol.*.allow` the repository sets, and no
command but a fetch reaches a remote), submodule recursion and status/diff looks into
submodules' working trees (a new commit checked out in one still counts), auto gc and
lazy fetches (`GIT_NO_LAZY_FETCH=1` whatever the environment says; on Git before 2.44,
which ignores it, every command lists the repository's config and overrides each
`protocol.*.allow` it sets). Filter drivers the repository itself defines are
blanked; a definition from the user's own config, such as Git LFS, is kept. Commands
that read files are refused when the repository sets Git LFS's extensions, custom transfer
agents or object store (`lfs.extension.*`, `lfs.customtransfer.*`,
`lfs.standalonetransferagent`, `lfs.storage`); its endpoints (`lfs.url`,
`remote.*.lfsurl`) are left alone, since no token or credential helper reaches them
outside a fetch, and filters never run in one.

A host fetch never follows a remote's URL, which the agent can rewrite to a private
local repository, another host or another GitHub repository the token reads: it names
`https://github.com/<owner>/<repo>.git` and explicit refs, and `safe_git` refuses any
other fetch, and any fetch while the repository's own config rewrites URLs
(`url.*.insteadOf`) or configures a remote named by a URL. The repository is a host-side
record: a cron job's repository, the one a dashboard launch is for (passed to `wt` as
`--github-repo`), or one a `wti`/`wtpr` URL names. Its base lands in the refs of the
remote that tracks that repository (`origin/<base>`, or `upstream/<base>` when origin is
a fork of it). Only those fetches get the GitHub token, through `gh auth git-credential`
alone (no other credential helper), with the repository's `remote.*.uploadpack`
replaced; a fetch with the token is refused when the repository's own config changes TLS
verification, CAs, proxies, name resolution or cookie files. Without such a record (`wt <branch>`,
`--with` clones, `wtpr <number>` without a URL), `wt` fetches the GitHub repository
origin's URL names. Typed in a terminal (stdin a TTY, no `--github-repo`, no
`BABYSIT_HOME`, which dashboard launches always set), that fetch gets the token, so the
user's own private repositories keep working. That leaves one gap: an agent that
rewrote origin to another private GitHub repository the token reads has it fetched,
into the common dir it reads, the next time the user runs `wt` in that clone. Any other
run (dashboard, cron, agents) fetches it without the token, so a redirected origin
reaches nothing private there and a private origin fails to fetch. An origin that is no
GitHub repository is never fetched, and the worktree starts from the local base. With
no base named and no recorded remote `HEAD`, `wt` asks `gh api repos/<repo>` for the
default branch only of a repository whose fetch would get the token, and checks the
reply, like a remote `HEAD` the agent may have planted, as a branch name; `git worktree
add` takes its positionals after `--end-of-options` either way. The user's own
URL rewrites still apply (to ssh, or a local mirror), so https, ssh and local paths stay
allowed during a fetch. System and global config stay trusted.

Config also depends on the worktree (`config.worktree`, `includeIf` on branch or git
dir), so new worktrees are added with `--no-checkout` and filled by a reset run inside
them (when that reset fails, the worktree and any branch just created for it are removed
again, so no later run reuses an empty checkout), and the dashboard removes a worktree with `--force` only after its own hardened
status there finds no changes, untracked files or submodules, as Git's check would
(approved blockers skip that check, as before). `safe_git` refuses either command
without those flags. Every command in a checkout with its own `.git` gets that checkout
as `--work-tree`, so a planted `core.worktree` or `core.bare` cannot send status, reset,
switch or that clean check to another directory.

The git common dir granted to an agent (Codex resume `--add-dir`, `wt --with`) and the
one `wtpr` checks provenance against come from the checkout's layout, never from
`rev-parse --git-common-dir`, which follows agent-written `commondir` files (so does the
directory babysit-bwrap binds for a Linux launch): a clone's
own `.git` directory, or the `.git/worktrees/<id>` a worktree's `.git` file names when
that entry registers the worktree back. Any other layout is refused, including a
worktree of a bare repository (`repo.git/worktrees/<id>`) and a `.git` that is a
symbolic link or whose common dir has its own `commondir` file. A watch records its common dir at registration
and a resume refuses one that has moved. `wtpr` refuses to write the clone's config
when `.git/config` is a symbolic link.

Still open: config the agent rewrites between the driver listing and the command it
guards, filter drivers in a submodule's own config for commands that do recurse,
and remote helpers for the allowed transports installed on PATH (`git-remote-https`)
during a fetch. Fetches
skip `FETCH_HEAD`, which Git would otherwise write through a planted symbolic link.

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
an agent. Watches that are not polled for CI (blocked, paused, or awaiting release) have
their PR state checked every 10 minutes, and on restart. A merge or close ends them the
same way; running and actively watched PRs are left to their own observation. Dashboard
**Needs attention** includes these ended watches. This means
PR monitoring is finished, not that local files have been checked for safe removal.
No session, pane, branch, worktree, or historical watch is automatically deleted.
Branch-only watches have no PR closure event; register a PR watch when appropriate.

## Worker machines

`--home/machines.json` registers other machines. Agent cron jobs placed on a worker run
there (see the Cron jobs section of the README); every other command still runs on
this machine. Paths a worker reports are refused if they hold a control character, and
so is any line a multiplexer is asked to type, here or there: in a terminal those are
keystrokes, whatever the quoting. A worker's job checkout must be a linked worktree of
the job's clone, outside its `.git`, so an agent cannot point the paths its sandbox is
given at a repository of its own.
`scripts/machines.py` loads and validates it; a malformed file is an error, a
missing one means local only.

### Watches on workers

A watch on a worker lives on this machine (the controller) like any other, with
`machine` set. `register --machine ID` reads everything about it on that worker, never
at a same-named path here: the checkout (probe op `worktree`: its top, branch, head,
changes and Git common dir) and the session (probe op `session_info`, which checks the
worker's rollout or Claude transcript as `register` checks its own, and records the
worker's account directory). The PR is named (`--pr URL`, or a number with `--repo`,
or `--branch`); `--pr auto` would need the worker's checkout. The checkout must be a
clean linked worktree under the worker's `<src>/worktrees` (resolved there), never a
main clone, whose `.git` other tasks share. Session ids are normalised before they are
compared. Once the worker's own checks pass, any failure to resolve the PR, a closed PR
or one whose branch and head the checkout does not match is reported only as "PR not
found or not watchable from this worker", so registrations cannot probe what this
machine's GitHub token can see. A worker watch's
launcher is that worker's own babysit-bwrap (`<babysit_root>/scripts/babysit-bwrap`,
`~` meaning the worker's home, with `--enable=gpg` when the machine has the `gpg`
label); `--codex-command`/`--claude-command` are refused, so a registration never
chooses what runs outside the sandbox there. Only Linux workers take watches.

Repairs run on the worker (see the guardian notes under Controls): the guardian
checks that the worker deploys this machine's babysit-pr commit (probe op `version`;
otherwise the watch is blocked with the command to update it), checks the PR and the
worker checkout as for a local repair, stages a spec (the session, account, timeout,
expected branch and head, and the prompt) in
`~/.local/state/babysit-pr/repair-specs/` there with `machines.put_file`, and runs the
worker half from the worker's deployed copy. That half never opens a queue: it
refuses a spec from anywhere else and a run folder that already exists (a spec runs
once), checks the checkout again with that machine's hardened Git, builds the agent's
environment with the worker's own Claude account directory, and runs the agent in its
own process group inside the worker's babysit-bwrap (`--enable=gpg` with the machine's
`gpg` label), keeping `~/.local/state/babysit-pr/runs/<attempt>/` (`agent.log`,
`prompt.txt`, `reply.json`, `result.json`, `pid`) there. Codex gets its output schema
and writes its reply in `~/.cache/babysit-repairs/<attempt>/`, the one extra folder its
sandbox is given (the run folder lies under the protected state directory, and an agent
able to write it could forge `result.json` or `pid`); the worker half copies the reply
out, as a regular file read with a size limit, and removes the folder. Its output streams back into
the guardian's `agent.log`, so a Claude result is read as for a local repair; a
Codex `reply.json` is fetched by a separate `_repair_status` call after the run. The
prompt names the agent's own copy of the skill. The session's lease is the
controller's (`session_locks`, keyed by machine).

A pane-bound watch on a worker (its pane captured through `herdr --machine`) gets the
worker half's command line typed into that pane, once, by the guardian here. In the
pane there is no lifeline: the worker half first checks, with the worker's own herdr,
that it runs in the bound terminal's foreground, then runs the agent in view, bounded
by the repair timeout, and parses a Claude result itself. The guardian polls
`_repair_status` every 5 s until the timeout plus 60 s, then copies the last MB of the
worker's `agent.log`; no result by then leaves the session held as for a lost worker.
A stop request, even one that lands while the line is being typed, asks the worker to
cancel (`_repair_cancel`), and leaves the session held when it cannot confirm the agent
stopped. A cancel of a run that has not started takes its run folder (exclusive
`mkdir`, result "Cancelled before start"), so a line that runs late fails at its own
exclusive `mkdir` instead of starting a second agent on the session; clearing an
orphan tombstone for a run that never started cancels it the same way first.

On a worker, `register` forwards to the controller's dashboard instead of keeping a
watch of its own, which would act where nobody looks. A worker is one with
`~/.config/babysit-pr/worker.json` (written by `provision_worker.sh --user-step`) or
with `BABYSIT_WORKER` set. A launch sets it for the `wt` it runs, not for the agent's
pane, whose shell herdr's server starts; inside babysit-bwrap, where `$HOME` is empty,
the wrapper sets `BABYSIT_WORKER=1` whenever the host has that file. The worker resolves
`--pr auto` with its own `gh` in its checkout. Outside a sandbox it then POSTs the
registration to `<controller>/api/worker/register` with `Authorization: Bearer
<token>`, both from `worker.json` (`provision_worker.sh --controller`); it follows no
redirect and uses no proxy from the environment, so the token goes only to that URL.
Without a controller and token there, `register` refuses.

Inside babysit-bwrap the sandbox never sees `worker.json`. For each launch with an
agent configuration on a worker, the wrapper starts a registration broker
(`scripts/worker_broker.py`) outside the sandbox, with the gpg proxy's lifecycle (a
private directory under `$XDG_RUNTIME_DIR`, its own session, gone with bwrap), binds
its socket in and names it last in `BABYSIT_REGISTER_SOCKET`. `register` inside sends
the request there. The broker forwards only requests for the launch's agent whose
`cwd` is one of the launch's granted directories and whose session's transcript, once
resolved, lies exactly in the launch's own store (`--claude-config-dir`'s
`projects/<project>/<id>.jsonl`, under `--codex-home`'s `sessions/`), so no link leads
to another account's. It tells the controller to check exactly that transcript and
passes the configuration directory, which the controller's probe checks again on the
path it opens (a link swapped in meanwhile is refused); no Claude account is passed,
so the account is where the transcript lies. One request is forwarded at a time, at
most one every 20 s per launch, with at most 4 connections open and 30 s to send a
request.
The wrapper also shows the deployed babysit-pr read-only inside on a worker, so the
skill and `pr_supervisor.py` are at `<babysit_root>` for sandboxed agents. The skill sync
never copies babysit-pr itself, so before each launch the wrapper links `<babysit_root>` as
`skills/babysit-pr` in the launch's Claude or Codex configuration, which makes
`/babysit-pr` and `$babysit-pr` resolve there. `handoff` on
a worker goes the same way (`action: handoff` in a broker request; registrations carry
no `action`, as brokers started before handoff expect; at most one of each action every
20 s per launch): it sends the watch ID, pane and timeout with both conversations it
may run in (`CLAUDE_CODE_SESSION_ID`, `CODEX_THREAD_ID`) and its worktree. The broker
keeps only its launch's agent's session, checks it and the worktree like a
registration's, and adds the transcript it found. The controller's
`/api/worker/handoff` schedules the handoff only for a watch registered on the token's
machine with that worktree and its agent's session, whose transcript is exactly the
broker's, from the watch's bound pane (terminal and shell; a watch registered without
its pane is not handed off from a worker), and answers every mismatch
of watch the same way. At most 4 handoffs per worker run at once. It then runs the
detached handoff itself, as for a local watch: the agent is followed through `herdr
--machine` every 4 s (its screen gets 30 s to settle), its transcript's turn and
fingerprint are read together through one probe op (`turn_state`), and worker paths are
compared as strings. Codex gets its one Ctrl-D; Claude gets a typed `/exit`, since its
second Ctrl-D would have to come sooner than the checks before it can read the worker,
and Enter only once its composer holds exactly `/exit`; otherwise the `/exit` typed
here is taken back with backspaces when the composer shows it at its end, and no key is
sent to anything else (a dialog, say). The exit is confirmed once only
the pane's shell is in its foreground, herdr no longer sees an agent there, and the
worker no longer runs the agent's command line at its pid (probe op `alive`, given
the SHA-256 of its NUL-joined arguments, which may hold a whole brief, and reading them
from /proc or macOS's kernel rather than `ps`), so a
stopped or backgrounded agent has not exited. Failures to start on the controller reach
the worker without this machine's paths. A local `handoff` refuses a worker's watch,
and a worker's refuses a local one.

`wt` inside a worker's sandbox starts a sub-task the same way (`action: launch`, see
Sub-tasks from a sandbox below): exactly `wt`'s launch fields (`LAUNCH_FIELDS`: repo,
base, new branch name, agent, model, effort, Claude account, brief, Docker, exit when
done, permission mode, machine) under `launch`, and both conversations it may run in
under `sessions`. The broker keeps its launch's agent's session, checked as a handoff's,
and adds, as the sub-task's parent, what the sandbox cannot forge: the launch's working
directory (babysit-bwrap passes its own `pwd -P` as `--cwd`), its agent and
configuration directory, the transcript it found, and whether the launch was granted
Docker (`--granted`, babysit-bwrap's `--enable`
grants). At most one launch every 20 s per sandbox; the answer waits for the task to
start. A launch whose broker was not given `--cwd` starts nothing.

The dashboard's `/api/worker/register`, `/api/worker/handoff` and `/api/worker/launch` take only that: the Host must be one the
dashboard accepts (`--allow-host`, for the tailnet name), and a request carrying
`Origin` or `Sec-Fetch-Site` (a browser) is refused. They are all a worker with
`tailnet_ip` may reach: the dashboard refuses it every other route (see the tailnet
dashboard section). The token decides the machine; the
body is exactly the registration fields (PR, repo, CI repo or branch, absolute `cwd`,
session UUID, agent, Claude account, pane or none, rollout, instructions, continuation,
poll interval, budget and timeout), checked by type, form and length, with no control
characters (the instructions may span lines); a handoff's body is exactly the watch
ID, pane, timeout (at most an hour), a session or none per agent, absolute `cwd` and
transcript or none; a launch's is exactly `launch` (checked by `Workspaces.subtask`) and
`parent`: absolute `cwd`, configuration directory and transcript, session UUID, agent,
Docker as a boolean. For
a launch the worktree is the parent's directory. One request per machine runs at a time per worktree, at most 6 a
minute per machine and worktree and 30 a minute per machine (a failed one costs 2
more, so repeated failures back off; answered 429), so one sandbox cannot hold up the
others; at most 256 worktree buckets are kept, a new one refused while that many are
still refilling; and a worker watch is polled no more often than every 30 s. With a machine's
optional `tailnet_ip` in `machines.json` (one address, or a list of up to four, such as
its IPv4 and IPv6 ones), a registration is refused unless `X-Forwarded-For` is exactly
one of them: `tailscale serve` sets it to the address the
request came from, replacing any the client sent. A process on this machine that
reaches the dashboard directly could still send that header, but it needs the token
too. `pr_supervisor.py worker-token ID` issues a machine's token: 32 random bytes,
printed once into a pipe (never to a terminal), and stored on the controller only as
its SHA-256 in `--home/worker-tokens.json` (mode 600), not in `machines.json`, which
may be shared. Issuing again replaces it; `--revoke` removes it; a disabled or
unregistered machine's token is refused. Tokens are compared in constant time against
every entry.

Residual: a sandboxed agent can register watches only for sessions in its own
configuration directory, recorded in its own checkout, with instructions of its
choosing (as it could ask its user to). Its configuration directory is shared with
every other launch of the same Claude account or Codex home, which can read and write
those sessions: an agent can register (squat) a session another launch of that account
recorded in this worktree, or plant one, and a repair then resumes that conversation
here. That gives it no access the account and its worktree do not already have. Anything
running unsandboxed as the worker user can read `worker.json` and register any linked
worktree and session there; a later release turns that into repairs of the session, in
that worktree's sandbox. Neither can name another machine or choose a launcher. A
worker watch awaiting release shows its machine, worktree and session id in the
dashboard and its attention item: release only the session you handed off. Revoke a
lost worker's token with `worker-token ID --revoke`. Record every worker's
`tailnet_ip`: without it, any process there, a sandboxed agent included (the sandbox
shares the network), can use the rest of the dashboard through the tailnet, from
launching and typing into agents to saving and running cron jobs unsandboxed on the
controller, and read every transcript and diff.

### Sub-tasks from a sandbox

`wt` inside an agent's sandbox cannot make worktrees (other clones' Git directories and
herdr's socket are outside it), so it hands a new task to the controller instead
(README, `wt`). Inside a worker's babysit-bwrap (it names `BABYSIT_REGISTER_SOCKET`) the
request goes through the launch's broker to `/api/worker/launch`, above. Inside a
macOS Seatbelt sandbox (Safehouse; `wt` asks libsandbox about itself) that cannot write
`~/src/<repo>/.git` it goes to this machine's dashboard as `POST /api/workspace-subtask` (the usual Host, Origin and
`X-Babysit-Action` guard) with the same `launch` and a `parent` the run claims: its
directory, its conversation (`CLAUDE_CODE_SESSION_ID` or `CODEX_THREAD_ID`, without
which it asks for nothing), its agent's configuration directory (`CLAUDE_CONFIG_DIR` or
`~/.claude`, `CODEX_HOME` or `~/.codex`) and whether Seatbelt lets it use Docker's
sockets. With a grant for the clone, `wt` works in
Safehouse as it always has, outside these rules. Runs the dashboard makes (any dashboard
option) never take this path.

Both end in `Workspaces.subtask`, which records the parent on the operation with its
lineage (shown as "Started by …" in its dialog and on its Workspaces row) and starts a
New task only when:

- the fields are exactly the launch fields, of their types, without control characters
  outside the brief; New task then checks them as for a person;
- it runs the parent's agent and, for Claude, the parent's account, named by the
  parent's configuration directory (`.claude` is `default`, `.claude/accounts/<name>`
  that name; filled in when the request names none). A Codex sub-task uses its machine's
  own Codex home;
- Docker is asked for only by a parent that has it;
- it lies at most 2 levels below the launch that started its lineage (a parent running
  in a sub-task's checkout continues that sub-task's lineage), the lineage started fewer
  than 6 sub-tasks in the last hour, and launches on the parent's machine fewer than 24
  (answered 429 from a worker);
- placement (Auto, sticking to the parent's machine, or the machine named) lands on a
  machine Auto would use now. A worker's parent never gets this machine, whose own
  agents' sandboxes reach the dashboard and herdr: named, it is refused, and Auto that
  finds no worker that could take it answers "No worker can take this sub-task now";
- that machine has room: it is not full, its load is known, it is not this machine past
  its capacity (which a person's launch still gets), and it runs fewer than 4 working
  sub-task agents: launches under way, and earlier sub-tasks whose checkout herdr lists
  with a working or blocked agent (an idle one left open does not count; sub-task
  operations are kept a week). Otherwise the sub-task is recorded as waiting for room
  (below), checked as if placed on the machine named, else the parent's;
- the repository has a clone on this machine (`<src>/<name>` if it is one, else the
  first listed); a worker uses its own clone, creating it when missing as for New task.

A sub-task always runs sandboxed: New task has no other launch, here or on a worker.
Ordinary sub-tasks retain the same-agent/account rule. A worker's broker supports a
narrow exception through `--review`, for either Codex to Claude or Claude to Codex:

```sh
wt --review --claude --claude-account default -r OWNER/REPO \
  -F /path/to/review-brief.md FULL_COMMIT_SHA REVIEW_BRANCH
```

Install the same updated babysit-pr version on the controller and worker, and start
a fresh worker sandbox: an already running broker retains its old request protocol.
This does not require copying a launch command into a host terminal. The agent
submits the request and the user approves it in the dashboard.

Use `--codex` for a Codex reviewer. Claude defaults explicitly to `default`, never
the controller's active account; named accounts are resolved on the selected worker.
Codex uses that worker's configured `CODEX_HOME`, else `~/.codex`; arbitrary account
paths cannot be supplied. Auto pins the parent worker, and an explicit `--machine`
can choose another eligible Linux worker. Reviewers require Linux babysit-bwrap
so only their selected provider/account configuration is mounted. The full 40-character
commit must already exist in its clone, including local unpushed commits on the parent worker. The new review
checkout starts at that exact commit without fetching a moving branch tip.

This records a request with status `approval`, not a launch or an authorization claim.
The **Scheduled** tab displays the complete brief and launch fields, plus the broker's
parent lineage. A signed-in user must approve that stored request. The approval
endpoint accepts only its ID, requires an authenticated session or admin key with
access to that endpoint even in report mode, and rejects worker/launch keys.
With dashboard auth off, approval is unavailable until sign-in is configured.
Unapproved requests expire after the normal waiting interval or can be cancelled.
A brief claiming user permission has no authority; repeating it does not bypass approval.

Approval is consumed once, recorded with the request and approving identity, and
enters the existing constrained sub-task launcher. Docker still requires the parent's
grant; placement, rate, depth and capacity constraints still apply. An approved launch
may wait for room and resumes with the same stored request. It creates a separate
sandbox mounting only its selected provider configuration, never private account
caches from the asking sandbox. A provider auth-status check on the selected worker
refuses missing executables or unauthenticated accounts before launch; it does not
provision a login or copy credentials. A provisioned account can still fail later if
its credentials expire or the provider is unreachable.

The brief should identify the exact commit, review scope and output path and any
restrictions on edits or posting. Save results under the reviewer's
`BABYSIT_ARTIFACT_DIR` or a granted worktree; private sandbox caches are separate.
Verify the resulting provider/account and receipt of the brief. A ready workspace is
not a completed review. Direct execution of another provider binary can be possible,
but does not gain its authentication in the current isolated HOME. Logging in on the
host cannot alter the mounts of an existing sandbox.

A launch waiting for room is an operation with status `waiting` and `wait_until`;
the `waiting` table keeps what starts it again: the Handle or New task request (and a
sub-task's parent and lineage, which already counted towards its rates). The scheduler
loop places waiting launches again every 15 s, longest waiting first, through the same
checks; with room it starts the same operation (its id and creation time kept, so a
scheduled task's exit watch follows it), and without it updates the reason. Starting,
cancelling (`POST /api/waiting-cancel` with the operation's key) and failing compare and
swap on the waiting operation, so a cancelled launch never starts. One that waited 6
hours, or whose request no longer passes, fails with the reason; a Handle waits until
its item's list has loaded since the dashboard started. `/api/scheduled-tasks` lists
them under `waiting`. `wt` reports a waiting launch and returns without following it.

A worker's request is answered once the operation is recorded and the worker's request
limits are released, after waiting up to 240 s for the launch (not for room); it gets
the operation without its log. From then on nothing is a refusal: an operation that cannot be read is
answered as uncertain, and a 5xx from the controller or a proxy, or a timeout, reaches
`wt` as "may have started; do not start it again", as does "still launching". From
Safehouse `wt` follows the operation through `/api/workspaces`.

Residual, on a worker: a sandboxed agent can now start sandboxed agents of its own agent
and account, up to those limits, with briefs, branches and models of its choosing, in
any repository cloned on a worker placement may use, including workers its own launch
does not run on, and with Docker when its own launch has it. Each runs as a New task
would, with its own grants, so it gets no access a person's New task with that brief
would not; but it is code of the agent's choosing in another sandbox. It cannot start
an unsandboxed agent, choose a launcher, command or working directory outside New
task's, add `--with` clones, reach this machine, name the machine it asks from, or ask
for another launch (the directory is babysit-bwrap's, the session must be in its own
store). Anything unsandboxed as the worker user can read `worker.json` and claim any
parent. These limits hold only for requests that come through the broker: a worker
sandbox that reaches the dashboard's other actions over the tailnet directly is not held
to them until the worker fence of #130 (which keeps worker machines off the dashboard's
local actions) is in place.

Residual, on this machine: the parent a run inside Safehouse names cannot be checked.
Safehouse leaves the network open, and this user's Safehouse grants (`safe.env`) make
the state home and herdr's configuration and socket writable, so any sandboxed agent
here can already reach every dashboard action (`/api/workspace-new`,
a cron job outside its sandbox) and type into unsandboxed panes; nothing at the
dashboard can tell it from the person. The sub-task rules here only hold for `wt`'s own
requests: they keep a well-behaved agent's sub-tasks sandboxed, counted and labelled,
not a hostile one.

Watches record `machine` (null for this machine), and a worktree path is a
duplicate only on the same machine. The dashboard reads the diff and
transcripts of a workspace ID qualified as `ID/w1` on that machine (see the
workspaces reference), refuses every other action on it, and refuses views of
watches recorded on another machine.

```json
{
  "version": 1,
  "machines": [
    {
      "id": "mini",
      "label": "Mac mini",
      "ssh": "mini.tail1234.ts.net",
      "os": "darwin",
      "src": "~/src",
      "babysit_root": "~/src/babysit-pr",
      "python": "python3",
      "max_agents": 4,
      "labels": ["docker"],
      "enabled": true
    }
  ],
  "repos": { "owner/repo": { "requires": ["docker"], "prefer": "mini" } },
  "docker": { "prefer": "mini" }
}
```

`id` (lowercase, at most 24 characters, not `local`) is the stable key; `label`
is the herdr machine label passed as `herdr --machine=<label>` (labels cannot start with `-`). `id`, `label`, `ssh` and
`os` (`darwin` or `linux`) are required. Paths are remote: absolute or under `~`.
`max_agents` is 1–64, and unknown keys are rejected. `auto` (default `false`) lets Auto
place launches there; without it the worker only runs what is sent to it by name, and
setting it back to `false` takes effect on the next launch. `collie_url`, optional, is that
machine's own Collie (an `http` or `https` URL with a host): its workspaces link
`<collie_url>/space/<id>`. `collie_host`, optional, is the machine's name in a Collie
crew this machine leads (`collie crew add <ssh>`, then `collie crew status`): its
workspaces open through this machine's Collie, or the lead `collie_url` names, as
`<lead>/space/<id>?h=<collie_host>`. With neither, a worker's workspaces link no Collie
at all, since this machine's Collie cannot open them.
Ids, labels and `ssh` targets must be unique, though two spellings of one host
(`user@host` and `host`) are not detected. The top-level `docker`, optional, is
`{"prefer": "<id>"}`: a registered machine, where Auto sends launches that use Docker
(see below). One that lacks the `docker` label, is disabled or is not used by Auto
keeps the registry valid; Auto passes it over and its reason says why.
Disabled machines stay valid for records already pinned to them.
`skills`, optional, lists personal skills the worker keeps in sync with this machine's
(see Skills on workers): up to 64 unique folder names (letters, digits, `.`, `_`, `-`,
not starting with `.` or `-`), never `synced` (claude.ai's own bucket) or `babysit-pr`
(the worker's deployed copy provides it). Without the key the worker's skills are left alone; an empty
list removes the copies an earlier sync installed.

Remote commands use `ssh` with `BatchMode`, a 5 s connect timeout, agent and port
forwarding off whatever the ssh config says, and a shared
master connection whose socket lives in `--home/ssh/` (or a short temporary
directory when that path is too long), with at most four concurrent commands
per ssh target in one process. They run with the worker's own environment and
credentials, under `sh -c`, so the worker's login shell need not be POSIX: it
sees only octal escapes that `sh` decodes, and no controller stdin.
A timeout kills only the local ssh client; the remote process may keep running,
so long-running remote work needs its own lifeline.

With a registry, the dashboard's launch dialogs (Handle, Create, New task, batch
review) offer a Machine select, and the Workspaces heading shows each machine's
health and load from `herdr --machine=<label> workspace list`, cached for 15 s and
refreshed in the background so no request waits on a worker. Auto only considers a
worker registered with `"auto": true`; any worker can still be chosen by name. Either
way the worker is checked against the launch (disabled, unreachable, missing `labels`
such as `docker`, `safehouse`/`bwrap` for the sandbox, `claude-account:<name>`, or a
repo's `requires`; a launch kind workers cannot start yet, listed below) and recorded on
the operation or scheduled task. A `placement_state` probe lists the clones directly under
each worker's `src` with their remotes and worktrees. It runs in the background when a
placement or the machines' loads are asked for and the last list is over 5 minutes old;
a failed refresh keeps the last list, and one older than 10 minutes counts as unknown, so
after a quiet spell the first launch places as if no worker had a clone. A worker whose load has not come back yet, or whose refresh has not finished
for 30 s, shows "checking…" and is left out of Auto. Auto ranks the machines that can
take a launch by stickiness first: a prior workspace for the same target, then a clone of
its repository, on every machine. New tasks and cron jobs always count this machine's
clone, and pull request and issue work count it when this machine has one. A worker
counts the clone its launches use (`<src>/<repo name>`, with the repository among its
remotes), so a repository cloned only here stays here until it is full, one cloned only
on a worker goes there, and one cloned on both goes to the repo's `prefer`, else to the
machine with the lower share of its capacity in use (this one on a tie). Sub-tasks and
moves do not count workers' clones. For Handle
on a pull request, a worker with a checkout at the pull request's head commit, tracking
its head branch (or on that branch with the head repository among the clone's remotes),
counts as a prior workspace there; Handle still starts a fresh checkout beside it, and
any machine can still be chosen by name. A launch that uses Docker is the exception:
with `"docker": {"prefer": "<id>"}` it goes to that worker ahead of a clone here ("auto:
Docker prefers <label>"), so Docker runs on the worker rather than on this machine. Only
a prior workspace, a pin (Handle on a verified checkout) or a machine chosen by name
beats it. The preferred worker must be registered with `"auto": true`, since `auto:
false` promises that it runs only what is sent to it by name; one that cannot take the
launch (not used by Auto, full, unreachable, checking, a kind it cannot start) leaves
it to the usual ranking, and the reason says why ("…; <label>: full (6/6)"). Placement
does not require a clone on the worker: task and pull request launches clone a missing
repository when they start. Cron jobs still require an existing clone, so Auto
leaves a worker without it out for a cron job ("no clone of owner/name"; chosen by name
it is only noted, since the list may be minutes old, and the run checks the clone), and
one whose clones are not listed yet shows "checking…".
Then come the repo's `prefer`, then load:
agents plus launches placed but not yet listed, over the machine's capacity. This
machine's capacity is **Agents on this machine** in Settings (default 20, shown once
workers are registered): at that many agents Auto prefers a worker, and comes back here
when no worker can take the launch. Until this machine's first workspace scan after a
restart, its free slots read "still counting agents". Chosen by name, a full machine,
or one not checked yet, has a launch from a dialog or `wt --machine` wait for room
there (below); a scheduled launch's choice is only noted when it is scheduled, and
it waits when it starts. A cron job's run is only noted. Placing a launch, as
opposed to previewing it, first waits up to 7 s for the load refreshes it starts:
a worker's load is refreshed only when something asks for it, so without the wait a
launch after a quiet spell would find every worker still "checking…". A cron
job outside its sandbox never runs on a worker. The preview uses the same stickiness, the
dialog's issue, other-repository and sandbox choices, and workers' clones, but cannot see
a Handle's verified checkout, which pins the launch to this machine or, on a worker, ranks
that worker first.

A launch placed on a worker (Handle or Create on a pull request, a new task, from the
dialog or from `wt --machine` on this machine, batch review, a scheduled launch) starts
there. Until repairs run on workers, only work that never needs them goes there: pull
request reviews and new tasks on a branch, including tasks with other clones (`--with`).
Issues, Sentry issues and new tasks that file an issue are refused with the reason.
The launch first compares the worker's deployed
babysit-pr commit (`git rev-parse HEAD` in its `babysit_root`) with this machine's and
refuses a mismatch, naming the `provision_worker.sh --user-step --sha <sha>` that fixes
it, then syncs the worker's skills when it has `skills` (see Skills on workers). For a Linux worker whose `babysit_root` is the copy that script deploys
(`~/.local/share/babysit-pr/app`), the refusal also offers **Update <worker>**: the
dashboard runs that user step, from this machine's commit, over the worker's ssh
(`/api/worker-update`, one update per worker at a time), then reads the worker's commit
back; a checkout the script left alone (local changes) is reported with its output. Start
the task again once it reports the new commit. It works in the worker's clone
`<src>/<repo name>`, which must name the repository in a remote. If that path is missing,
the probe clones `https://github.com/<owner>/<repo>.git` with the worker's own GitHub
login before starting the task. This also applies to each missing `--with` clone.
An existing destination belonging to another repository is refused. Clones are staged
beside the destination, with hooks, filters and submodule recursion disabled; a failed
clone leaves the destination available for retry. A probe op names
the new checkout under `<src>/worktrees/<repo name>` and trusts it for Claude in the
config its sandbox reads (a config that is a symlink or over 64 MiB is left alone, and
the rewritten one keeps only its permission bits); the brief is staged in `~/.local/state/babysit-pr/workspace-prompts`
there. Files attached in the dashboard are copied first, each under a new name, into
`~/.local/state/babysit-pr/attachments` there (a private folder, pruned like the
uploads here), and the brief names those copies; a failed copy fails the launch before
anything is typed. Every agent on the worker can read every copy, as every agent here
can read the uploads: babysit-bwrap makes the folder and binds it read-only, so on a
Linux worker an agent started by an older babysit-bwrap cannot see it (start it again);
Agent Safehouse grants all of `~/.local/state/babysit-pr`, so on a macOS worker any
agent there can also change a copy. Only lines after the brief's last attached-files
heading that name an upload in the attachments folder are copied, never any other file.
Messages to a worker's agent copy theirs the same way, once the message is otherwise
accepted. The worker's own `wt.py` then runs over ssh with `BABYSIT_WORKER=<id>`, `--gpg`
when the machine has the `gpg` label, and `--exit-watch-json` for **Quit the agent when
it is done**: it prints the exit watch instead of recording it, and the dashboard
records it with the machine. Clone locks hash the machine with the clone. The checkout
and agent are verified through probe ops and `herdr --machine`. Exit watches on workers
(these, scheduled launches there, and worker cron runs) are followed every minute, in
the background, the way local ones are (`workspace_exit`), through the worker's herdr:
the agent's own pane is pinned, its process is named by herdr's `argv0` where it
reports one (macOS) or else its first argument (herdr on Linux, where babysit-bwrap's
`bwrap` processes share the foreground until the agent exits), worker paths are
compared as strings, and the
transcript is read there through probe ops (`sessions`, `first_turn`, `turn`,
`fingerprint`). Its session is the one in the checkout, among the newest five of the
agent's kind, whose first prompt carries the brief's marker; the worker's clock is
never compared with this one's. Once it confirms and stays idle the agent is exited
there (Codex with Ctrl-D, Claude with a typed `/exit` and Enter), and an exited `wt`
agent's pane or workspace is closed as here. A watch stops after a week, once the
worker leaves the registry, or after 30 looks in a row the worker did not answer (ssh
or the probe's deadline failed, or its herdr failed: from here those cannot be told
apart). Anything else the worker answers leaves the agent open at once, but a
transcript it will not read is skipped while looking for the session, and a turn it
cannot identify yet is waited for. A worker cron run's pane is closed only while it
still holds the agent's shell.
Their records name the workspace qualified (`<id>/w1`), so no dashboard takes it for
one of its own.

Handle on a verified checkout stays on that checkout's machine;
choosing another is refused. `GET /api/placement?repo=&agent=&account=&docker=1`
previews where Auto would place a launch and which machines can take it;
`GET /api/machines` lists the machines with their cached loads and placement verdict,
which the Workspaces badges poll once `/api/status` reports `workers` (absent without
a registry, so a single machine never asks). The experimental Workspaces tab lists each
enabled worker's worktrees read-only (see the workspaces reference).

### Skills on workers

A worker with `skills` gets each from this user's chezmoi source, never from the applied
copies in `~/.claude/skills` or `~/.codex/skills`: the dashboard runs outside any
sandbox, but agents here may write those (Safehouse grants `~/.claude` and `~/.codex`),
while `~/.local/share/chezmoi` is read-only to them. So a skill edited in place reaches
workers only once it is in the chezmoi source (`chezmoi add` or `chezmoi re-add`, or a
commit pulled there). A skill is the target `~/.claude/skills/<name>`, else
`~/.codex/skills/<name>`, which `chezmoi archive --format=tar` renders (`dot_`,
`executable_` and other source prefixes resolved, templates executed; a `run_` script
inside a skill's target is synced as a plain file, never run). chezmoi runs by absolute
path (`/opt/homebrew/bin/chezmoi`, or `BABYSIT_CHEZMOI` in the dashboard's environment),
with stdin closed, only this user's home (from the password database) and the system
`PATH` in its environment, no TTY or pager, externals not refreshed and a 30 s deadline;
a template needing a password manager fails. `~/.config/chezmoi` is not read, since its
config could name another source or add hooks: chezmoi gets `--source` set to
`~/.local/share/chezmoi` under that home, and an empty `--config`, its persistent state
and its cache in `--home/chezmoi/` (private), so neither the user's config nor a cache
an agent could write here steers what is rendered. Templates therefore see no `data` from
the user's config, and an external under a skill's target would be downloaded into that
cache rather than taken from the user's. A target chezmoi does not manage is refused: "not in your chezmoi source;
add it with chezmoi add". A target that is a chezmoi symlink (as `~/.claude/skills/merge-forward`
links to `~/.codex/skills/merge-forward`) is followed by asking chezmoi for the target
it names, at most twice and only to another skill target, never through the file system.

`scripts/skill_sync.py` takes only the target's folders and regular files (a link,
hard link, device or entry outside the target refuses the whole bundle; `.DS_Store`,
`__pycache__` and `.git` are left out) and re-packs them into one tar, sorted, dated
1970, owned by nobody and keeping only the executable bit, so the same skills always give
the same bytes; at most 8 MiB of files and 2000 entries, and the folder must hold a
`SKILL.md`. The bundle's SHA-256 is its version, and a packing is reused for 30 s, so a
burst of launches packs once.

Before every launch on such a worker (a scheduled launch or cron run, Handle, Create, a
new task, batch review, a sub-task, a resume, a watch's repair there) and after
**Update <worker>** deploys, the dashboard asks the worker (probe op `skills_status`)
for the bundle its manifest records and, for each skills folder, whether it still holds
that bundle's copies. Only when the bundle differs or a folder does not does it stage the
bundle in `~/.local/state/babysit-pr/skill-bundles/` there (`machines.put_file`) and
install it (probe op `skills_install`). The probe is this machine's `remote_probe.py`, as
for every op, so what installs never depends on the worker's deployed copy. Workers
without `skills` are never asked.

A sync fails when it cannot run (a skill missing or refused here, the worker unreachable,
the install refused) or leaves the skills folder the launch's own agent reads (its Claude
account's, the default one's or Codex's) without the bundle, with the reason the worker
gave (`Permission denied`, say). Other folders it could not write are reported, not a
failure. A failed sync stops cron runs, scheduled launches and sub-tasks: nobody watches
those start, and an agent told to run `/merge-forward` without it, or with last week's
copy, would improvise rather than stop. A launch a person starts (Handle, Create, a new
task) and a resume go ahead with the reason shown on the operation or the resume's
warning, where that person can stop the agent; a repair goes ahead with it in the run's
`worker.log`, since it needs only babysit-pr's own skill.

The install takes the staged file only from that folder, removes it whatever it holds,
checks its SHA-256, and checks every entry again (no absolute path, `..`, control
character, link or device; each skill's folder before its contents, with a `SKILL.md`;
`synced` and `babysit-pr` refused in any case; the same limits). Its targets are every
Claude account's `~/.claude/accounts/<name>/skills`, the default configuration's
`~/.claude/skills` and Codex's `$CODEX_HOME/skills` (or `~/.codex/skills`):
babysit-bwrap binds an account's `skills` read-only and refuses a link there, and a link
inside it to `~/.claude/skills` would dangle in the sandbox, so each gets real folders.
A missing `skills` folder is made private (0700). `~/.claude` and Codex's home may be
links a dotfile manager made; nothing below them is followed. babysit-bwrap binds each
skills folder read-only and hides `~/.claude/accounts` from a default-account agent under
a tmpfs, but on a Mac worker Safehouse may grant `~/.claude` and `~/.codex`, and an agent
given Docker reaches every file of the user (see Residual below): an account folder or `skills` folder that is a link is
left alone and reported, and every file is created and renamed relative to opened
folders, without following links. Each skill is written as a new private copy beside the
old one; on Linux the two are exchanged in one step (`renameat2` with
`RENAME_EXCHANGE`), so a reader sees one whole copy or the other, and elsewhere (or on a
kernel without it) the old copy is renamed aside and the new one renamed in, leaving the
name missing for that moment. The old copy is then removed; a copy that already matches is
left as it is. Leftovers of an interrupted sync (`.babysit-skill-*`) go first.

`~/.local/state/babysit-pr/skills.json` there, in the state directory no sandbox can
write, records the bundle, each skill's content digest and, per skills folder, the
skills the sync put there. Only those are ever replaced or removed, plus names that are
free or already hold exactly the bundle's copy, which it adopts: a folder or link of the
same name that it did not make is left alone and reported, and skills dropped from
`skills` are removed only where it put them. Before swapping anything the install records
every name it may touch, with no bundle, so an install cut short (killed, or failing to
write the manifest) leaves nothing unrecorded and the next launch finishes it; records of
folders that no longer exist are dropped. The manifest is flushed to disk before it
replaces the old one. `skills_status` digests the installed copies again, so a copy an
agent or a person changed is reinstalled at the next launch; a damaged manifest stops the
sync until it is moved aside. `GET /api/machines` gives each worker's `skills` as last
seen (`state`: `in_sync`, `partial` with `problems` by folder, `out_of_date`, `error`
with `error`, or `unknown` before the first check since the dashboard started; `skipped`
lists folders left alone), and the Workspaces machine badge shows it in its tooltip.

Residual: a worker agent granted Docker (`--enable=docker`) reaches every file of the
worker's user, as the Linux sandbox section says, the skills, the manifest and the staged
bundles included, so it can change what the next launch of every account runs from its
skills and, by rewriting the manifest, which copies the sync treats as its own.
