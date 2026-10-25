# Linux worker setup

Checklist for adding an Ubuntu 24.04 machine as a babysit-pr worker. The controller
(the Mac running the dashboard and supervisor) keeps the queue, watches and locks;
the worker runs herdr, agents and clones under `~/src`, as the dedicated `babysit`
user, behind `scripts/babysit-bwrap` (see the Linux sandbox part of
[the supervisor reference](supervisor.md#permissions-and-scope)).

`scripts/provision_worker.sh` does the machine side in two idempotent steps. Rerun
either one after changing a pin or fixing a reported problem. It exits 0 when every
check passed, 1 when it printed a `PROBLEM`, 2 on misuse. `--dry-run` prints the
planned actions without changing anything or touching the network; `--check` only
runs the checks: the host checks as root, the user step's checks (bwrap, rootless
Docker, commit signing when set up) as `babysit`.

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

It deploys babysit-pr to `~/.local/share/babysit-pr/app` and checks out that SHA
detached (a checkout with local changes is left alone), creates
`~/.cache/babysit-sandbox` (mode 700), sets npm's prefix to `~/.npm-global` (so
Codex updates never need root), and prepends `~/.npm-global/bin`, `~/.local/bin`
and `~/.local/share/babysit-pr/app/scripts` to `PATH` at the top of `~/.bashrc`,
above Ubuntu's early return, so non-interactive `ssh box <command>` finds the same
tools.

The deployed copy is kept apart from `~/src`, where agents work: babysit-bwrap
refuses to grant `~/.local/share/babysit-pr`, its own checkout and that checkout's
git directory, so no agent can change the launcher or what git runs in it. Agents
can still work on babysit-pr itself in a clone under `~/src` and its worktrees.
git in the deployed copy always runs with `-c core.fsmonitor=false
-c core.hooksPath=/dev/null -c protocol.ext.allow=never`. Never work in the
deployed copy or add worktrees to it. The user step's `--check` reports its commit,
refuses local changes in it, warns when `babysit-bwrap` on `PATH` is not the
deployed one, and reports a `~/.bashrc` that still puts `~/src/babysit-pr/scripts`
on `PATH`.

babysit-bwrap needs bash 4.1 or later (Ubuntu ships 5.x) and, for `--enable=gpg`,
python3; the system step installs python3. With `GIT_USER_NAME` and `GIT_USER_EMAIL`
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

**Commit signing.** There is no 1Password SSH agent on the box, and `~/.ssh` and
`~/.gnupg` are never visible inside the sandbox. Either set
`git config --global commit.gpgsign false` (with `gpgsign true` and no signing set
up, every sandboxed commit fails) and accept unsigned worker commits where branch
protection allows, or give the worker a GPG signing subkey as below.

### Signing with a GPG subkey

The worker holds only a dedicated signing subkey of your key; the primary stays on the
Mac, so the worker can sign but cannot certify, add or revoke keys. gpg-agent keeps
the subkey unlocked, and `babysit-bwrap --enable=gpg` lets a sandboxed agent sign
through a proxy in front of that agent, without seeing `~/.gnupg`.

1. On the Mac, add a signing subkey valid for a year:

   ```sh
   gpg --list-secret-keys --keyid-format long you@example.com   # PRIMARY: its fingerprint
   gpg --quick-add-key PRIMARY ed25519 sign 1y
   gpg --list-keys --with-subkey-fingerprint PRIMARY             # SUB: the new [S] subkey
   ```

2. Export that subkey's secret alone; the trailing `!` limits the export to exactly
   `SUB`, and the primary goes along only as a stub (`sec#`). gpg asks for the key's
   passphrase, and the exported subkey keeps it:

   ```sh
   gpg --armor --export-secret-subkeys 'SUB!' > worker-subkey.asc
   scp worker-subkey.asc babysit@<box>: && rm worker-subkey.asc
   ```

3. Import it in an interactive session: the worker's gpg-agent asks for the passphrase
   through pinentry to store the key, so it needs a terminal and `GPG_TTY`:

   ```sh
   ssh -t babysit@<box>
   export GPG_TTY=$(tty)
   gpg --import ~/worker-subkey.asc && shred -u ~/worker-subkey.asc
   gpg -K --with-keygrip     # sec# (stub primary), ssb [S]: note the subkey's Keygrip
   ```

4. Keep the passphrase and unlock the subkey at startup. Removing it does not work
   here: `gpg --passwd` (like `passwd` in `gpg --edit-key`) starts with the primary
   key, whose secret is only a stub on the worker, and fails before reaching the
   subkey; and the confirmation gpg should show for an empty passphrase never appeared
   in practice. Instead gpg-agent accepts a preset passphrase. On Ubuntu
   `/usr/bin/pinentry` points (through alternatives) at `pinentry-gnome3`, which needs a
   desktop session, so name the curses one; `~/.gnupg/gpg-agent.conf`:

   ```text
   pinentry-program /usr/bin/pinentry-curses
   allow-preset-passphrase
   ```

   Then `gpgconf --reload gpg-agent` (`apt install pinentry-curses` if it is missing).
   Store the passphrase, mode 600, without echoing it or leaving it in shell history:

   ```sh
   (umask 077 && read -rs pass && printf '%s' "$pass" > ~/.gnupg/signing-subkey.pass)
   ```

   and add `~/.config/systemd/user/babysit-gpg-preset.service`, with the subkey's
   keygrip from step 3:

   ```ini
   [Unit]
   Description=Unlock the worker signing subkey in gpg-agent
   Requires=gpg-agent.socket
   After=gpg-agent.socket
   ConditionPathExists=%h/.gnupg/signing-subkey.pass

   [Service]
   Type=oneshot
   ExecStart=/bin/sh -c "/usr/lib/gnupg/gpg-preset-passphrase --preset KEYGRIP < %h/.gnupg/signing-subkey.pass"

   [Install]
   WantedBy=default.target
   ```

   ```sh
   systemctl --user daemon-reload
   systemctl --user enable --now babysit-gpg-preset.service
   ```

   The system step enables lingering, so the unit runs when `babysit`'s user manager
   starts at boot. A preset passphrase lasts until gpg-agent exits; after
   `gpgconf --kill gpg-agent` or an agent restart, run
   `systemctl --user restart babysit-gpg-preset.service`.

5. Point git at the subkey (the `!` again forces exactly that subkey):

   ```sh
   git config --global user.signingkey 'SUB!'
   git config --global commit.gpgsign true
   git config --global tag.gpgsign true
   git config --global gpg.program gpg
   ```

6. GitHub verifies signatures against the public key as uploaded, which does not yet
   contain the new subkey. On the Mac, `gpg --armor --export PRIMARY`, then delete the
   old key in GitHub's SSH and GPG keys settings and add the new export. Until then the
   worker's commits show as unverified.

7. Check, as `babysit`:
   `ssh babysit@<box> 'bash -s -- --check' < scripts/provision_worker.sh`. With
   `~/.gnupg/signing-subkey.pass` and `user.signingkey` present it verifies that the
   preset unit is enabled, that gpg signs without a prompt, and that a throwaway commit
   made inside `babysit-bwrap --enable=gpg` passes `git verify-commit`. It prints no key
   material.

Sandboxed agents sign only with `--enable=gpg`. babysit-bwrap then mounts a throwaway
`~/.gnupg` on the sandbox's tmpfs holding copies of `pubring.kbx` and `trustdb.gpg`
and a `gpg.conf` with `no-autostart`, and at the agent's standard socket path (from
`gpgconf --list-dirs agent-socket`), where gpg inside looks, the socket of
`scripts/gpg_agent_proxy.py` (python3). babysit-bwrap starts one proxy per launch in
a private directory; it exits with the sandbox. It passes on only what gpg sends to
sign (`RESET`, `GETINFO version`, `HAVEKEY`, `KEYINFO`, `SIGKEY`, `SETHASH`,
`PKSIGN`, two harmless options) and refuses the rest, among it `EXPORT_KEY`,
`DELETE_KEY`, `GET_PASSPHRASE`, `PRESET_PASSPHRASE`, `CLEAR_PASSPHRASE`,
`KILLAGENT` and `UPDATESTARTUPTTY`. Terminal, display and loopback-pinentry options
and the key description are acknowledged but never reach the agent, so a sandbox
cannot show a passphrase prompt on someone's terminal or answer one itself. The
sandbox never sees the agent's own socket, the private keys, the passphrase file,
`gpg-agent.conf` or the extra, browser and ssh sockets. The agent needs no new
options; the preset unit above keeps the subkey unlocked as before. `~/.gitconfig` is already mounted read-only, so the signing configuration
applies inside. In babysit this is the `gpg` feature of a launch's grants
(`sandbox.Grants(features=("gpg",))`); once the machine registry lands, the worker's
`gpg` label in `machines.json` turns it on for that machine's launches.

**Renewal.** Before the subkey expires, on the Mac:
`gpg --quick-set-expire PRIMARY 1y SUB`. The expiry lives in the public key, so
re-upload `gpg --armor --export PRIMARY` to GitHub as in step 6 and refresh the
worker's copy: `gpg --export PRIMARY | ssh babysit@<box> gpg --import`. Sandboxes copy
the public keyring at each launch.

**Revocation.** If the worker is lost or retired, on the Mac run
`gpg --edit-key PRIMARY`, select the subkey (`key N`), `revkey`, `save`; re-upload the
public key to GitHub and import it wherever the key is verified. On a worker you still
control, also `systemctl --user disable --now babysit-gpg-preset.service`,
`shred -u ~/.gnupg/signing-subkey.pass`, `gpg --delete-secret-keys 'SUB!'` and
`git config --global --unset user.signingkey`.

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
         "babysit_root": "/home/babysit/.local/share/babysit-pr/app",
         "python": "/usr/bin/python3",
         "max_agents": 6,
         "labels": ["bwrap", "docker", "claude-account:<name>"],
         "enabled": true
       }
     ]
   }
   ```

   `labels` are the capabilities placement filters on: `bwrap` (the Linux sandbox
   works), `docker` (rootless Docker is set up and the agent may be given it), `gpg`
   (the signing subkey is set up and agents get `--enable=gpg`),
   `claude-account:<name>` for each named Claude login on that machine. Paths are the
   worker's own; never copy `/Users/...` paths into worker records.

4. Keep the worker's babysit-pr at the controller's SHA: rerun the user step with the
   new `--sha` after updating the controller. A mismatch blocks remote work.

**Migrating a worker provisioned before the deployed copy** (babysit-pr in
`~/src/babysit-pr`, for example chonk pinned at `d55bebe`): rerun the user step with
the current SHA. It clones the deployed copy to `~/.local/share/babysit-pr/app` and
rewrites the `PATH` line under `# babysit-pr worker PATH` in `~/.bashrc`; then
`. ~/.bashrc` (or log in again), restart herdr panes started with the old `PATH`,
change `babysit_root` in `machines.json` to `/home/babysit/.local/share/babysit-pr/app`,
and confirm with `--check`. The old `~/src/babysit-pr` is no longer used by babysit:
remove it, or keep it as an ordinary clone for agents to work in (babysit-bwrap
would refuse it only if babysit ran from it). Inspect it before running anything
from it outside the sandbox: agents working in its worktrees could write its git
directory.

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
- **gpg-agent is a signing oracle.** With `--enable=gpg` an agent can have the agent
  sign arbitrary data, not only its own commits, for as long as it runs, as on macOS.
  Through the proxy it cannot export the key, read, preset or clear passphrases,
  delete keys, stop the agent or show a pinentry; anyone who runs code as `babysit`
  outside the sandbox can. Revoke the subkey (above) if the worker is compromised.
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
