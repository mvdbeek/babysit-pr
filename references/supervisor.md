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
it, so a new harness only supplies its launch argv. The backend follows the
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
outside the sandbox. Claude's `.claude.json` stays writable, since Claude rewrites
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
`~/.config/gh`, `~/.config/babysit-pr`, `~/.local/state/babysit-pr`,
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
descriptor, so a path swapped after the check cannot redirect the bind. Read-only binds (system, toolchains, `~/.config/gh`) follow the
writable ones, so none can be shadowed. It unshares PID, IPC and UTS
namespaces, dies with its parent, and clears the environment except `PATH`,
`HOME`, `TERM`, `LANG`, `USER`, the variables above and the `--env-pass` names.
It starts a new session (which blocks `TIOCSTI` terminal injection) unless stdin
is a terminal and `/proc/sys/dev/tty/legacy_tiocsti` reads `0`, so interactive
agents keep job control only where the kernel already blocks injection.
`--print-argv` prints the bwrap argv, NUL-separated, without running it. Typed
launches on Linux invoke the wrapper directly; there are no shell functions there
to rely on.

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
GitHub repository is never fetched, and the worktree starts from the local base. The user's own
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
missing one means local only. `register --machine ID` checks the ID against the
registry and then refuses: watches on other machines are not supported yet. On a
worker `register` and `handoff` refuse: watch registration from worker machines isn't
supported yet. A worker is one with `~/.config/babysit-pr/worker.json` (written by
`provision_worker.sh --user-step`) or with `BABYSIT_WORKER` set. A launch sets it for
the `wt` it runs, not for the agent's pane, whose shell herdr's server starts; inside
babysit-bwrap, where `$HOME` is empty, the wrapper sets `BABYSIT_WORKER=1` whenever the
host has that file. This is a guard against mistakes, not a boundary: an agent outside
babysit-bwrap, or one that unsets the variable, is not stopped by it.
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
  "repos": { "owner/repo": { "requires": ["docker"], "prefer": "mini" } }
}
```

`id` (lowercase, at most 24 characters, not `local`) is the stable key; `label`
is the herdr machine label passed as `herdr --machine=<label>` (labels cannot start with `-`). `id`, `label`, `ssh` and
`os` (`darwin` or `linux`) are required. Paths are remote: absolute or under `~`.
`max_agents` is 1–64, and unknown keys are rejected. `collie_url`, optional, is that
machine's own Collie (an `http` or `https` URL with a host): its workspaces link
`<collie_url>/space/<id>`; without one a worker's workspaces link no Collie at all,
since this machine's Collie cannot open them.
Ids, labels and `ssh` targets must be unique, though two spellings of one host
(`user@host` and `host`) are not detected.
Disabled machines stay valid for records already pinned to them.

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
refreshed in the background so no request waits on a worker. Auto always places on
this machine for now. Choosing a worker by name checks it against the launch
(disabled, unreachable, missing `labels` such as `docker`, `safehouse`/`bwrap` for
the sandbox, `claude-account:<name>`, or a repo's `requires`) and records it on the
operation or scheduled task. A worker whose load has not come back yet, or whose refresh has
not finished for 30 s, shows "checking…" and is left out of Auto.

A launch placed on a worker (Handle or Create on a pull request, a new task, batch
review, a scheduled launch) starts there. Until repairs run on workers, only work that
never needs them goes there: pull request reviews and new tasks on a branch. Issues,
Sentry issues, new tasks that file an issue and new tasks with other clones (`--with`)
are refused with the reason. The launch first compares the worker's deployed
babysit-pr commit (`git rev-parse HEAD` in its `babysit_root`) with this machine's and
refuses a mismatch, naming the `provision_worker.sh --user-step --sha <sha>` that fixes
it. It works in the worker's clone `<src>/<repo name>`, which must name the repository
in a remote; nothing clones on a worker, so a missing clone is refused. A probe op names
the new checkout under `<src>/worktrees/<repo name>` and trusts it for Claude in the
config its sandbox reads; the brief is staged in `~/.local/state/babysit-pr/workspace-prompts`
there. The worker's own `wt.py` then runs over ssh with `BABYSIT_WORKER=<id>`, `--gpg`
when the machine has the `gpg` label, and `--exit-watch-json` for **Quit the agent when
it is done**: it prints the exit watch instead of recording it, and the dashboard
records it with the machine. Clone locks hash the machine with the clone. The checkout
and agent are verified through probe ops and `herdr --machine`. Exit watches on workers
are looked at there every minute but never exited: the watch says when the agent
finished, or left without confirming, and leaves it open. It also stops after a week,
once the worker leaves the registry, or after 30 failed looks in a row. Their records
name the workspace qualified (`<id>/w1`), so no dashboard takes it for one of its own.

Handle on a verified checkout stays on that checkout's machine;
choosing another is refused. `GET /api/placement?repo=&agent=&account=&docker=1`
previews where Auto would place a launch and which machines can take it;
`GET /api/machines` lists the machines with their cached loads and placement verdict,
which the Workspaces badges poll once `/api/status` reports `workers` (absent without
a registry, so a single machine never asks). The experimental Workspaces tab lists each
enabled worker's worktrees read-only (see the workspaces reference).
