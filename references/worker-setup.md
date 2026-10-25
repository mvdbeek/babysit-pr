# Linux worker setup

Checklist for adding an Ubuntu 24.04 machine as a babysit-pr worker. The controller
(the Mac running the dashboard and supervisor) keeps the queue, watches and locks;
the worker runs herdr, agents and clones under `~/src`, as the dedicated `babysit`
user, behind `scripts/babysit-bwrap` (see the Linux sandbox part of
[the supervisor reference](supervisor.md#permissions-and-scope)).

`scripts/provision_worker.sh` does the machine side in two idempotent steps. Rerun
either one after changing a pin or fixing a reported problem. It exits 0 when every
check passed, 1 when it printed a `PROBLEM`, 2 on misuse. `--dry-run` prints the
planned actions without changing anything or touching the network; `--check` (as
root) only runs the host checks.

## 1. Before you start

- Ubuntu 24.04 server, amd64 (arm64 works with the same script), no desktop session.
- Tailscale up on the box, and `ssh <admin>@<box>` with sudo working from the Mac.
  If you use `tailscale up --ssh`, Tailscale SSH replaces sshd for tailnet
  connections and the sshd keepalive drop-in below does not apply to them; set
  `ServerAliveInterval 15` in the Mac's `~/.ssh/config` entry for the box either way.
- The controller's babysit-pr commit is pushed (the worker checks out that SHA).
- No personal secrets on the box. The `babysit` user is defence in depth, not the
  boundary.

## 2. System step (root)

From a babysit-pr checkout on the Mac:

```sh
ssh <admin>@<box> "sudo BABYSIT_SSH_KEY='$(cat ~/.ssh/id_ed25519.pub)' bash -s" \
  < scripts/provision_worker.sh
```

It:

1. Creates the `babysit` user (bash, own group, non-overlapping `/etc/subuid` and
   `/etc/subgid` ranges) and adds `BABYSIT_SSH_KEY` to its `authorized_keys`. The key file is written as `babysit` (umask 077, via `runuser`) and
   refused if `~/.ssh` or `authorized_keys` is a symlink.
2. Installs git, bubblewrap, socat, build-essential, curl, jq, uidmap,
   dbus-user-session, slirp4netns, python3 (3.12 on 24.04), python3-venv, AppArmor
   tools and `apparmor-profiles`.
3. Adds apt repositories, each keyed only by the one pinned primary fingerprint: the
   key is imported into a scratch GNUPGHOME, must be exactly that key, and only it is
   exported (binary) to `/etc/apt/keyrings/<name>.gpg` for `signed-by`; an older `.asc`
   keyring from earlier runs is removed. Repositories: GitHub CLI (`gh`; skipped when
   the distro's `gh` is installed), Docker (`docker-ce`, `docker-ce-rootless-extras`,
   buildx; skipped when Ubuntu's `docker.io` is installed, `DOCKER_SOURCE` overrides)
   and Claude Code (Anthropic's
   signed repository, `stable` channel; `CLAUDE_CHANNEL=latest` or `INSTALL_CLAUDE=0`).
   apt-installed Claude does not auto-update; upgrade with `apt upgrade claude-code`.
   It lives in `/usr`, which the sandbox mounts read-only, so an agent cannot replace it.
4. Downloads pinned releases and checks each against a pinned SHA-256 before use:
   Node.js LTS (official tarball, checksum from `SHASUMS256.txt`) into `/opt` with
   links in `/usr/local/bin`; uv and uvx into `/usr/local/bin`; herdr (GitHub release
   asset digest) into `/usr/local/bin`. Override with `NODE_VERSION`/`NODE_SHA256`,
   `UV_VERSION`/`UV_SHA256`, `HERDR_VERSION`/`HERDR_SHA256`. Archives are extracted with
   `--no-same-owner --no-same-permissions` and installed `root:root`, so an archive's
   uid (often `babysit`'s) never survives. Each verified install leaves a root-owned
   marker `/usr/local/share/babysit-provision/<tool>-<version>-<arch>.sha256`; reruns
   re-check the installed files against it (and root ownership, no group/world write)
   and reinstall on any difference, never executing the binaries as root. Earlier
   agent-owned installs are chowned back to root first. To refresh a pin, take the
   digest from the vendor (`gh release view <tag> -R herdrdev/herdr --json assets`,
   `https://nodejs.org/dist/<v>/SHASUMS256.txt`), never from the download itself.
5. Writes `/etc/ssh/sshd_config.d/60-babysit-worker.conf` with
   `ClientAliveInterval 15` and `ClientAliveCountMax 4`, validates with `sshd -t` and
   reloads ssh (`systemctl try-reload-or-restart`; a drop-in `sshd -t` rejects is removed), so a vanished controller's sessions (and their remote repairs) end
   within about a minute.
6. Makes bubblewrap work under AppArmor (below).
7. Leaves the machine's rootful Docker and containerd running (the operator may use
   them; babysit-bwrap refuses their socket and the host checks verify `babysit` cannot
   write it), adds the rootlesskit AppArmor profile Docker documents for
   Ubuntu 24.04 when none exists, enables lingering for `babysit` and runs
   `dockerd-rootless-setuptool.sh install` as that user. `babysit` is never added to
   the `docker` group.
8. Runs the host checks and prints a summary.

### AppArmor and unprivileged user namespaces

Ubuntu 24.04 sets `kernel.apparmor_restrict_unprivileged_userns=1`: only programs
with an AppArmor profile granting `userns` may create user namespaces, which bwrap
needs. The script keeps that restriction and never sets the sysctl to 0.

It first runs `bwrap --ro-bind / / true` as `babysit`. Only if that fails does it add
a profile, chosen by `BWRAP_APPARMOR`:

- `restrict` (default): copies `bwrap-userns-restrict` from
  `/usr/share/apparmor/extra-profiles/` (package `apparmor-profiles`) to
  `/etc/apparmor.d/` and loads it. bwrap may create the namespace, and everything it
  starts is stacked into `unpriv_bwrap`, which has no capabilities inside it. That
  keeps bwrap from becoming a general way around the restriction. Ubuntu's main
  `apparmor` package once enabled this profile and then dropped it (LP #2072811,
  Flatpak breakage), so on 24.04 it may or may not be present in `extra-profiles`;
  if it is missing the script reports a problem instead of guessing.
- `unconfined`: writes `/etc/apparmor.d/bwrap`,
  `profile bwrap /usr/bin/bwrap flags=(unconfined) { userns, }`. bwrap and everything
  it starts then run unconfined, so any local user can use bwrap to get a fully capable
  user namespace, which is the attack surface the restriction removes. The script
  refuses it unless `BWRAP_APPARMOR_I_UNDERSTAND=1` is also set.

On Ubuntu 26.04 bwrap already works and AppArmor ships `bwrap-userns-restrict`
enforced (the sysctl may not exist at all); nothing is installed and absence of the
sysctl is not a failure. The script targets 24.04 and 26.04.

Consequence of `restrict`: programs inside the sandbox cannot create their own user
namespaces. Chromium's sandbox (Playwright) and nested bwrap (Claude Code's or
Codex's internal sandboxes) fail there. babysit runs agents with their internal
sandboxes off, so only Chromium is affected; launch it with `--no-sandbox` /
`chromiumSandbox: false` inside babysit-bwrap, or choose `unconfined`.

### bubblewrap version

babysit-bwrap binds writable directories by descriptor with `--bind-fd`, which needs
bubblewrap 0.10.0. Ubuntu 24.04 ships 0.9.0, where binds go by path and the race
described in the supervisor reference remains. The script reports this as a problem
unless `BWRAP_ACCEPT_OLD=1`. Options:

- Accept 0.9.0 (`BWRAP_ACCEPT_OLD=1`) while no extra `--add-dirs` grant has a parent
  that another sandbox can write; babysit's own grants are covered by its refusal of
  shared main checkouts.
- Build a pinned release from source, for example bubblewrap 0.13.1
  (`bubblewrap-0.13.1.tar.xz`, sha256
  `0e85c8702248b8dd64144f594147553650cdd0e4c951690345faace79c217c4d` from the GitHub
  release) with `meson setup build --prefix=/usr/local && ninja -C build install`
  (needs `meson ninja-build libcap-dev pkg-config`). `/usr/local/bin` comes first in
  `PATH`, so babysit-bwrap uses it; the AppArmor profile must then attach to
  `/usr/local/bin/bwrap` too (edit the path in the profile line and reload it).
  You own its security updates.
- Check `apt-cache policy bubblewrap` on the box for a `noble-backports` candidate
  (none was known when this was written). A third-party PPA is a package source
  trusted with root, so prefer the source build to one. Newer Ubuntu releases ship
  0.11 or later.

### Other host checks

- `dev.tty.legacy_tiocsti`: reported. `0` lets babysit-bwrap keep job control for
  interactive agents; otherwise it always starts a new session. Set
  `dev.tty.legacy_tiocsti=0` in `/etc/sysctl.d/` if you want that.
- Desktop session: a warning when a graphical session, display server or X11
  abstract socket exists. The sandbox shares the host network namespace, so abstract
  Unix sockets (X11, a D-Bus session bus) are reachable from it and can start
  programs outside it. Keep the worker headless.
- Rootless Docker socket at `/run/user/<uid>/docker.sock`; `babysit` in none of
  `sudo`, `admin`, `lxd`, `disk`, `adm`, `docker`, with no sudoers entry
  (`sudo -l -U babysit`), and unable to write `/var/run/docker.sock`; Tailscale up, `/etc/subuid` entry, sshd keepalive.

## 3. User step (as `babysit`)

```sh
ssh babysit@<box> "bash -s -- --user-step --sha $(git rev-parse HEAD)" \
  < scripts/provision_worker.sh
```

If the babysit-pr repository is private, run `gh auth login` and `gh auth setup-git`
as `babysit` first (or pass `--repo` with a URL the box can read).

It clones babysit-pr to `~/src/babysit-pr` and checks out that SHA detached (a
checkout with local changes is left alone), creates `~/.cache/babysit-sandbox`
(mode 700), sets npm's prefix to `~/.npm-global` (so Codex updates never need root),
and prepends `~/.npm-global/bin`, `~/.local/bin` and `~/src/babysit-pr/scripts` to
`PATH` at the top of `~/.bashrc`, above Ubuntu's early return, so non-interactive
`ssh box <command>` finds the same tools. With `GIT_USER_NAME` and `GIT_USER_EMAIL`
it sets git's global identity. It then checks bwrap and rootless Docker and prints the
interactive steps:

```sh
. ~/.bashrc
git config --global user.name  "Your Name"
git config --global user.email "you@example.com"
gh auth login --hostname github.com --git-protocol https
gh auth setup-git
npm install -g @openai/codex@<version>     # npm verifies the registry's sha512 integrity
codex login --device-auth
claude-account login <name>                # a named account; see "Known gaps"
babysit-bwrap --claude-config-dir="$HOME/.claude/accounts/<name>" -- true
```

The worker's `gh` login is used only by agents and git on the worker; the controller
keeps polling GitHub with its own. Prefer a fine-grained token or a separate GitHub
account limited to the repositories babysit works on.

**Commit signing.** There is no 1Password SSH agent on the box, and `~/.ssh` is never
visible inside the sandbox, so a sandboxed agent cannot sign. Set
`git config --global commit.gpgsign false` (with `gpgsign true` every sandboxed commit
fails) and accept unsigned worker commits where branch protection allows. If a
repository requires signatures, create a worker-only key outside the sandbox
(`ssh-keygen -t ed25519 -f ~/.ssh/babysit_signing`, `gpg.format ssh`,
`user.signingkey`, registered on GitHub as a signing key) and re-sign the agent's
commits from an unsandboxed shell before pushing, or push from the controller.

**Installers.** herdr's documented `curl -fsSL https://herdr.dev/install.sh | sh` and
Claude Code's `curl -fsSL https://claude.ai/install.sh | bash` are not used: the
script installs herdr from a pinned, checksummed release and Claude from Anthropic's
signed apt repository. If you prefer Claude's native installer, verify the release's
signed `manifest.json` (key fingerprint `31DD DE24 DDFA B679 F42D 7BD2 BAA9 29FF 1A7E
CACE`) and the binary's SHA-256 as Claude Code's setup docs describe. `herdr update`
self-updates outside these checks; reinstall via the script with a new pin instead.

## 4. Controller (the Mac)

1. Add an ssh config entry for the box (`Host <box>`, `User babysit`,
   `HostName <tailscale name>`, `ServerAliveInterval 15`) and check
   `ssh <box> herdr --version`.
2. In an interactive terminal: `herdr machine add <box> --label <label>`. Setup starts
   the remote herdr server. Then verify without the TUI:

   ```sh
   herdr machine status <label>
   herdr --machine=<label> workspace list
   ```

   Always address the machine by label (`--machine=<label>`), as babysit does.

3. Add the worker to `machines.json` in the dashboard's `--home` (the registry from
   the multi-machine plan; until it lands this entry is inert):

   ```json
   {
     "machines": [
       {
         "id": "ubuntu-1",
         "label": "<label>",
         "ssh": "<box>",
         "os": "linux",
         "src": "/home/babysit/src",
         "babysit_root": "/home/babysit/src/babysit-pr",
         "python": "/usr/bin/python3",
         "max_agents": 6,
         "labels": ["bwrap", "docker", "claude-account:<name>"],
         "enabled": true
       }
     ]
   }
   ```

   `labels` are the capabilities placement filters on: `bwrap` (the Linux sandbox
   works), `docker` (rootless Docker is set up and the agent may be given it),
   `claude-account:<name>` for each named Claude login on that machine. Paths are the
   worker's own; never copy `/Users/...` paths into worker records.

4. Keep the worker's babysit-pr at the controller's SHA: rerun the user step with the
   new `--sha` after updating the controller. A mismatch blocks remote work.

## 5. Architecture

The worker is amd64, the Mac arm64. Images built on the Mac are arm64 and will not
run on the worker (and vice versa) unless built multi-arch
(`docker buildx build --platform linux/amd64,linux/arm64`) or for the worker's
platform (`--platform linux/amd64`). The provisioning pins cover both architectures;
`ARCH` overrides detection.

## 6. Residual risks

From the Linux sandbox section of the supervisor reference, restated for operators:

- **Headless only.** The sandbox shares the host network namespace; a desktop
  session's abstract sockets would let an agent start programs outside it.
- **Separate accounts for sandboxed use.** An agent can write its configuration
  directory (hooks, MCP servers, notify commands). Never use a Claude or Codex
  account that sandboxed agents use outside the sandbox on this machine.
- **Rootless Docker gives up the filesystem boundary.** With `--enable=docker`,
  `docker run -v $HOME:/h` reaches every file `babysit` owns. Grant Docker only to
  agents you would trust with that, and keep nothing personal in the account.
- **Git directories stay writable**, so hooks an agent leaves in a worktree's git
  directory run when git is later used there outside the sandbox.
- **bubblewrap 0.9.0** keeps the bind-by-path race unless upgraded (above).
- **Egress is unrestricted**; network isolation is a later step.

## 7. Known gaps and follow-ups

- **Default Claude account on Linux.** babysit-bwrap always exports an explicit
  `CLAUDE_CONFIG_DIR`, so a sandboxed Claude using the default account (`~/.claude`)
  keeps its global config in `~/.claude/.claude.json`. babysit's login detection and
  folder-trust pre-acceptance (`claude_accounts.global_config`) read
  `~/.claude.json` for the default account, which on a worker is the file an
  unsandboxed Claude writes, so the default account shows the wrong login and new
  worktrees stop at Claude's trust dialog. Workaround: always use a named account on a
  worker (`claude-account login <name>`, label `claude-account:<name>`), whose
  `.claude.json` lives inside its own directory where both sides look. Follow-up: make
  `global_config` follow the explicit `CLAUDE_CONFIG_DIR` babysit passes for the
  default account on Linux, or have babysit-bwrap give the default account a
  dedicated directory.
- Writing a `worker.json` (plan D6.6) and installing Playwright's system libraries
  (`npx playwright install-deps chromium` as root, pinned to the repository's
  Playwright version) are not automated yet.
- The dashboard's Docker toggle is macOS-only (supervisor reference).
