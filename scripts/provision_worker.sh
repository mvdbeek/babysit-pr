#!/usr/bin/env bash
# Provision an Ubuntu 24.04 machine as a babysit-pr worker. Idempotent; rerun after
# changing a pin or fixing a reported problem. See references/worker-setup.md.
#
# System step, as root over ssh (from a babysit-pr checkout on the controller):
#
#   ssh box 'sudo bash -s' < scripts/provision_worker.sh
#   ssh box 'sudo BABYSIT_SSH_KEY="ssh-ed25519 AAAA... you@mac" bash -s' < scripts/provision_worker.sh
#   ssh box 'sudo bash -s -- --check' < scripts/provision_worker.sh
#
# User step, as the babysit user once the system step passes:
#
#   ssh babysit@box 'bash -s -- --user-step --sha <controller babysit-pr SHA>' \
#     < scripts/provision_worker.sh
#
# Options:
#   --check       change nothing; run only the host checks and report
#   --dry-run     change nothing and touch no network; print the planned actions
#   --user-step   run the non-root step as the babysit user (needs --sha)
#   --sha SHA     babysit-pr commit to check out in ~/src/babysit-pr (user step)
#   --repo URL    babysit-pr clone URL (user step; default $BABYSIT_REPO)
#
# Environment (system step):
#   BABYSIT_USER        worker account (default babysit)
#   BABYSIT_SSH_KEY     public key line(s) added to that account's authorized_keys
#   INSTALL_CLAUDE      1 (default) installs Claude Code from Anthropic's signed apt
#                       repository; 0 skips it
#   CLAUDE_CHANNEL      stable (default) or latest
#   BWRAP_APPARMOR      restrict (default): Ubuntu's packaged bwrap-userns-restrict
#                       profile, whose children keep no capabilities in their user
#                       namespace; unconfined: a minimal profile granting bwrap
#                       `userns` only. Used only if bwrap does not already work.
#   BWRAP_ACCEPT_OLD    1 accepts a bubblewrap older than 0.10.0 (no --bind-fd)
#   NODE_VERSION, NODE_SHA256, UV_VERSION, UV_SHA256, HERDR_VERSION, HERDR_SHA256
#                       override the pinned downloads below (the SHA256 must match
#                       the machine's architecture)
#   ARCH                amd64 or arm64 (default: dpkg --print-architecture)
#
# Environment (user step):
#   BABYSIT_REPO        default https://github.com/mvdbeek/babysit-pr.git
#   GIT_USER_NAME, GIT_USER_EMAIL   set git's global identity when given
#   CODEX_VERSION       Codex npm version printed in the next steps
#
# Exit status: 0 when every check passed, 1 when a problem was reported, 2 on misuse.
set -euo pipefail

# Pinned downloads. Node's checksums come from nodejs.org's SHASUMS256.txt, uv's and
# herdr's from the GitHub release asset digests.
NODE_DEFAULT=v24.22.1
NODE_SHA256_x64=6c172a9ab6718f7b801fb5a7bd486af2ee5e32758086a04b84fcce2aa77095e6
NODE_SHA256_arm64=dbabf7a1cbf947e6059ffad2c27b80041db97dea1e553b62afdee7f13fca336e
UV_DEFAULT=0.13.5
UV_SHA256_x86_64=f68d291c75783122dad58ab77def0e13f3233f358731db5d6d7e8715603b928f
UV_SHA256_aarch64=4bb69cf95ed13f23713bb7bc09f241896ea0c75b7c557e615a20604b82f9c403
HERDR_DEFAULT=v0.9.5
HERDR_SHA256_x86_64=14d9e8bc8b4a9e0118d77c32266f76d461f09b40342eb23752960bd89f59929d
HERDR_SHA256_aarch64=17d7536f2da50859523680ef279dffcac8e4a235f4e46fe8da6c2ec44141897c
CODEX_DEFAULT=0.164.0
# Primary key fingerprints of the apt repositories' signing keys.
GH_KEY_FPR=2C6106201985B60E6C7AC87323F3D4EA75716059
DOCKER_KEY_FPR=9DC858229FC7DD38854AE2D88D81803C0EBFCD88
CLAUDE_KEY_FPR=31DDDE24DDFAB679F42D7BD2BAA929FF1A7ECACE
BWRAP_MIN=0.10.0

mode=system
dry=0
sha=
repo=${BABYSIT_REPO:-https://github.com/mvdbeek/babysit-pr.git}
user_name=${BABYSIT_USER:-babysit}
problems=()
warnings=()

usage() {
  sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" 2>/dev/null | sed '$d' | sed 's/^# \{0,1\}//' >&2 ||
    printf 'see the header of scripts/provision_worker.sh\n' >&2
  exit 2
}

while (($#)); do
  case $1 in
    --check) mode=check ;;
    --dry-run) dry=1 ;;
    --user-step) mode=user ;;
    --sha | --repo)
      (($# > 1)) || usage
      [[ $1 == --sha ]] && sha=$2 || repo=$2
      shift
      ;;
    -h | --help) usage ;;
    *)
      printf 'provision_worker: unknown argument %s\n' "$1" >&2
      exit 2
      ;;
  esac
  shift
done

say() { printf '==> %s\n' "$*"; }
ok() { printf '  ok: %s\n' "$*"; }
warn() {
  printf '  WARNING: %s\n' "$*" >&2
  warnings+=("$*")
}
problem() {
  printf '  PROBLEM: %s\n' "$*" >&2
  problems+=("$*")
}

# Every change goes through run (or write_file); a dry run only prints it.
run() {
  if ((dry)); then
    printf '  would run: %s\n' "$*"
  else
    "$@"
  fi
}

# write_file PATH MODE CONTENT: replace PATH only when its content differs.
write_file() {
  local path=$1 mode=$2 content=$3
  if ((dry)); then
    printf '  would write %s (mode %s)\n' "$path" "$mode"
    return 0
  fi
  if [[ -f $path ]] && [[ $(cat "$path") == "$content" ]]; then
    return 1
  fi
  printf '%s\n' "$content" >"$path.tmp.$$"
  chmod "$mode" "$path.tmp.$$"
  mv -f "$path.tmp.$$" "$path"
}

finish() {
  echo
  if ((${#warnings[@]})); then
    say "Warnings"
    printf '  - %s\n' "${warnings[@]}"
  fi
  if ((${#problems[@]})); then
    say "Problems (fix and rerun)"
    printf '  - %s\n' "${problems[@]}"
    exit 1
  fi
  say "Done"
}

# --- downloads -------------------------------------------------------------------------

# fetch_verified URL SHA256 DEST: download into a private directory, verify, then move.
fetch_verified() {
  local url=$1 want=$2 dest=$3 tmp
  if ((dry)); then
    printf '  would download %s and verify sha256 %s\n' "$url" "$want"
    return 0
  fi
  tmp=$(mktemp -d)
  curl -fsSL --proto '=https' --tlsv1.2 -o "$tmp/download" "$url"
  if ! printf '%s  %s\n' "$want" "$tmp/download" | sha256sum -c --status; then
    rm -rf "$tmp"
    printf 'provision_worker: checksum mismatch for %s\n' "$url" >&2
    exit 1
  fi
  mv -f "$tmp/download" "$dest"
  rm -rf "$tmp"
}

# apt_repo NAME KEY_URL FINGERPRINT DEB_LINE: trust a signing key only after its primary
# fingerprint matches, then register the repository with that key alone.
apt_repo() {
  local name=$1 key_url=$2 fpr=$3 line=$4 ext tmp got
  ext=${key_url##*.}
  [[ $ext == asc || $ext == gpg ]] || ext=asc
  local keyring=/etc/apt/keyrings/$name.$ext
  line=${line//@KEYRING@/$keyring}
  if ((dry)); then
    printf '  would trust %s (fingerprint %s) and add: %s\n' "$key_url" "$fpr" "$line"
    return 0
  fi
  install -d -m 0755 /etc/apt/keyrings
  tmp=$(mktemp -d)
  curl -fsSL --proto '=https' --tlsv1.2 -o "$tmp/key" "$key_url"
  got=$(GNUPGHOME=$tmp gpg --batch --show-keys --with-colons "$tmp/key" 2>/dev/null |
    awk -F: '$1 == "fpr" { print $10; exit }')
  if [[ $got != "$fpr" ]]; then
    rm -rf "$tmp"
    printf 'provision_worker: %s has fingerprint %s, expected %s\n' "$key_url" "${got:-none}" "$fpr" >&2
    exit 1
  fi
  install -m 0644 "$tmp/key" "$keyring"
  rm -rf "$tmp"
  write_file "/etc/apt/sources.list.d/$name.list" 0644 "$line" || true
}

# --- checks ----------------------------------------------------------------------------

check_userns_sysctl() {
  local restrict=/proc/sys/kernel/apparmor_restrict_unprivileged_userns
  if [[ -r $restrict ]] && [[ $(cat "$restrict") == 1 ]]; then
    ok "kernel.apparmor_restrict_unprivileged_userns=1 (kept; bwrap gets its own profile)"
  else
    ok "unprivileged user namespaces are not restricted by AppArmor"
  fi
}

bwrap_works() {
  (cd / && sudo -u "$user_name" -H bwrap --ro-bind / / true) >/dev/null 2>&1
}

check_bwrap() {
  local version
  if ! command -v bwrap >/dev/null; then
    problem "bwrap is not installed"
    return
  fi
  if bwrap_works; then
    ok "bwrap --ro-bind / / true works as $user_name"
  else
    problem "bwrap --ro-bind / / true fails as $user_name (see the AppArmor section of references/worker-setup.md)"
  fi
  version=$(bwrap --version 2>/dev/null | awk '{ print $2 }')
  if [[ $(printf '%s\n%s\n' "$BWRAP_MIN" "$version" | sort -V | head -n1) == "$BWRAP_MIN" ]]; then
    ok "bubblewrap $version has --bind-fd"
  elif [[ ${BWRAP_ACCEPT_OLD:-0} == 1 ]]; then
    warn "bubblewrap $version predates $BWRAP_MIN: babysit-bwrap binds writable directories by path (accepted with BWRAP_ACCEPT_OLD=1)"
  else
    problem "bubblewrap $version predates $BWRAP_MIN (no --bind-fd); see 'bubblewrap version' in references/worker-setup.md, or set BWRAP_ACCEPT_OLD=1"
  fi
}

check_tiocsti() {
  local file=/proc/sys/dev/tty/legacy_tiocsti
  if [[ ! -r $file ]]; then
    warn "dev.tty.legacy_tiocsti is absent: babysit-bwrap always starts a new session"
  elif [[ $(cat "$file") == 0 ]]; then
    ok "dev.tty.legacy_tiocsti=0: interactive agents keep job control in babysit-bwrap"
  else
    warn "dev.tty.legacy_tiocsti=$(cat "$file"): babysit-bwrap starts a new session, so interactive agents lose job control (echo dev.tty.legacy_tiocsti=0 > /etc/sysctl.d/60-tiocsti.conf to change)"
  fi
}

check_headless() {
  local session type found=
  if command -v loginctl >/dev/null; then
    for session in $(loginctl list-sessions --no-legend 2>/dev/null | awk '{ print $1 }'); do
      type=$(loginctl show-session "$session" -p Type --value 2>/dev/null || true)
      case $type in x11 | wayland | mir) found="$found session $session ($type)" ;; esac
    done
  fi
  if pgrep -x 'Xorg|Xwayland|gnome-shell|kwin_wayland|sddm|gdm3' >/dev/null 2>&1; then
    found="$found display-server-process"
  fi
  if ss -xl 2>/dev/null | grep -q '@/tmp/\.X11-unix'; then
    found="$found X11-abstract-socket"
  fi
  if [[ -n $found ]]; then
    warn "a desktop session is running ($found): its abstract sockets are reachable from the sandbox; run the worker headless"
  else
    ok "no desktop session"
  fi
}

check_docker_rootless() {
  local uid sock
  uid=$(id -u "$user_name")
  sock=/run/user/$uid/docker.sock
  if [[ -S $sock ]]; then
    ok "rootless docker socket at $sock"
  else
    problem "no rootless docker socket at $sock"
  fi
  if id -nG "$user_name" | tr ' ' '\n' | grep -qx docker; then
    problem "$user_name is in the docker group, which is root on this machine"
  fi
  if systemctl is-active --quiet docker.service 2>/dev/null; then
    warn "the system docker daemon is running; babysit-bwrap refuses its socket, but nothing here needs it"
  fi
}

check_misc() {
  if command -v tailscale >/dev/null && tailscale status >/dev/null 2>&1; then
    ok "tailscale is up"
  else
    warn "tailscale is not installed or not up (the controller reaches workers over Tailscale)"
  fi
  if grep -q "^$user_name:" /etc/subuid 2>/dev/null; then
    ok "$user_name has subordinate uids"
  else
    problem "$user_name has no /etc/subuid entry (rootless docker needs one)"
  fi
  if sshd -T 2>/dev/null | grep -qx 'clientaliveinterval 15'; then
    ok "sshd ClientAliveInterval 15"
  else
    problem "sshd does not report ClientAliveInterval 15"
  fi
}

run_checks() {
  say "Host checks"
  if ((dry)); then
    printf '  would check: AppArmor userns sysctl, bwrap as %s, bubblewrap >= %s, dev.tty.legacy_tiocsti, desktop sessions, rootless docker, tailscale, subuid, sshd\n' "$user_name" "$BWRAP_MIN"
    return 0
  fi
  if ! id "$user_name" >/dev/null 2>&1; then
    problem "no user $user_name; run the system step"
    return 0
  fi
  check_userns_sysctl
  check_bwrap
  check_tiocsti
  check_headless
  check_docker_rootless
  check_misc
}

# --- system step -----------------------------------------------------------------------

load_profile() {
  run apparmor_parser -r "$1"
}

ensure_bwrap_profile() {
  local restrict=/proc/sys/kernel/apparmor_restrict_unprivileged_userns
  local packaged=/usr/share/apparmor/extra-profiles/bwrap-userns-restrict
  if ((dry)); then
    printf '  would verify bwrap as %s and, only if it fails, install the %s AppArmor profile\n' "$user_name" "${BWRAP_APPARMOR:-restrict}"
    return 0
  fi
  if bwrap_works; then
    return 0
  fi
  if [[ ! -r $restrict ]] || [[ $(cat "$restrict") != 1 ]]; then
    return 0 # Not AppArmor; the check below reports the failure.
  fi
  case ${BWRAP_APPARMOR:-restrict} in
    restrict)
      if [[ ! -f $packaged ]]; then
        problem "$packaged is missing (apparmor-profiles); rerun with BWRAP_APPARMOR=unconfined or install the upstream profile by hand"
        return 0
      fi
      say "Installing Ubuntu's bwrap-userns-restrict AppArmor profile"
      install -m 0644 "$packaged" /etc/apparmor.d/bwrap-userns-restrict
      load_profile /etc/apparmor.d/bwrap-userns-restrict
      ;;
    unconfined)
      say "Installing a minimal bwrap AppArmor profile (userns only)"
      write_file /etc/apparmor.d/bwrap 0644 "$(
        cat <<'EOF'
# babysit-pr worker: lets /usr/bin/bwrap create user namespaces under
# kernel.apparmor_restrict_unprivileged_userns=1. Written by provision_worker.sh.
abi <abi/4.0>,
include <tunables/global>

profile bwrap /usr/bin/bwrap flags=(unconfined) {
  userns,

  include if exists <local/bwrap>
}
EOF
      )" || true
      load_profile /etc/apparmor.d/bwrap
      ;;
    *)
      printf 'provision_worker: BWRAP_APPARMOR must be restrict or unconfined\n' >&2
      exit 2
      ;;
  esac
}

ensure_rootlesskit_profile() {
  local restrict=/proc/sys/kernel/apparmor_restrict_unprivileged_userns
  local profile=/etc/apparmor.d/usr.bin.rootlesskit
  if ((dry)); then
    printf '  would write %s if user namespaces are restricted and no profile exists\n' "$profile"
    return 0
  fi
  [[ -r $restrict ]] && [[ $(cat "$restrict") == 1 ]] || return 0
  grep -rqs '/usr/bin/rootlesskit' /etc/apparmor.d/ && return 0
  # Docker's documented profile for rootlesskit on Ubuntu 24.04, for the packaged path.
  write_file "$profile" 0644 "$(
    cat <<'EOF'
# babysit-pr worker: rootless docker under kernel.apparmor_restrict_unprivileged_userns=1.
abi <abi/4.0>,
include <tunables/global>

"/usr/bin/rootlesskit" flags=(unconfined) {
  userns,

  include if exists <local/usr.bin.rootlesskit>
}
EOF
  )" || true
  load_profile "$profile"
}

system_step() {
  local arch codename node_arch tool_arch uid home
  if ((!dry)); then
    ((EUID == 0)) || {
      printf 'provision_worker: run the system step as root (ssh box '\''sudo bash -s'\'' < ...)\n' >&2
      exit 2
    }
    # shellcheck disable=SC1091
    . /etc/os-release
    [[ ${ID:-} == ubuntu && ${VERSION_ID:-} == 24.04 ]] ||
      warn "written for Ubuntu 24.04, found ${PRETTY_NAME:-unknown}"
    codename=${VERSION_CODENAME:-noble}
    arch=${ARCH:-$(dpkg --print-architecture)}
    cd /
  else
    codename=noble
    arch=${ARCH:-amd64}
  fi
  case $arch in
    amd64) node_arch=x64 tool_arch=x86_64 ;;
    arm64) node_arch=arm64 tool_arch=aarch64 ;;
    *)
      printf 'provision_worker: unsupported architecture %s\n' "$arch" >&2
      exit 2
      ;;
  esac
  local node_version=${NODE_VERSION:-$NODE_DEFAULT}
  local node_sha_var=NODE_SHA256_$node_arch uv_sha_var=UV_SHA256_$tool_arch
  local herdr_sha_var=HERDR_SHA256_$tool_arch
  local node_sha=${NODE_SHA256:-${!node_sha_var}}
  local uv_version=${UV_VERSION:-$UV_DEFAULT} uv_sha=${UV_SHA256:-${!uv_sha_var}}
  local herdr_version=${HERDR_VERSION:-$HERDR_DEFAULT} herdr_sha=${HERDR_SHA256:-${!herdr_sha_var}}
  export DEBIAN_FRONTEND=noninteractive
  if ((dry)); then
    work=/tmp/provision-worker.XXXXXX
  else
    work=$(mktemp -d)
    trap 'rm -rf "$work"' EXIT
  fi

  say "Account $user_name"
  if ((dry)) || ! id "$user_name" >/dev/null 2>&1; then
    run useradd --create-home --shell /bin/bash --user-group "$user_name"
  else
    ok "$user_name exists"
  fi
  if ((!dry)) && ! grep -q "^$user_name:" /etc/subuid; then
    # The next 65536 ids after every existing range, so no two accounts overlap.
    local first
    first=$(awk -F: 'BEGIN { n = 100000 } $2 + $3 > n { n = $2 + $3 } END { print n }' \
      /etc/subuid /etc/subgid)
    run usermod --add-subuids "$first-$((first + 65535))" \
      --add-subgids "$first-$((first + 65535))" "$user_name"
  fi
  if [[ -n ${BABYSIT_SSH_KEY:-} ]]; then
    if ((dry)); then
      printf '  would add BABYSIT_SSH_KEY to ~%s/.ssh/authorized_keys\n' "$user_name"
    else
      home=$(getent passwd "$user_name" | cut -d: -f6)
      install -d -m 0700 -o "$user_name" -g "$user_name" "$home/.ssh"
      touch "$home/.ssh/authorized_keys"
      while IFS= read -r key; do
        [[ -z $key ]] || grep -qxF "$key" "$home/.ssh/authorized_keys" ||
          printf '%s\n' "$key" >>"$home/.ssh/authorized_keys"
      done <<<"$BABYSIT_SSH_KEY"
      chown "$user_name:$user_name" "$home/.ssh/authorized_keys"
      chmod 0600 "$home/.ssh/authorized_keys"
    fi
  fi

  say "Base packages"
  run apt-get update -q
  run apt-get install -y -q --no-install-recommends ca-certificates curl gnupg git jq \
    bubblewrap socat build-essential uidmap dbus-user-session slirp4netns python3 \
    python3-venv xz-utils apparmor apparmor-utils apparmor-profiles iproute2

  say "Signed apt repositories"
  apt_repo githubcli https://cli.github.com/packages/githubcli-archive-keyring.gpg "$GH_KEY_FPR" \
    "deb [arch=$arch signed-by=@KEYRING@] https://cli.github.com/packages stable main"
  apt_repo docker https://download.docker.com/linux/ubuntu/gpg "$DOCKER_KEY_FPR" \
    "deb [arch=$arch signed-by=@KEYRING@] https://download.docker.com/linux/ubuntu $codename stable"
  local packages=(gh docker-ce docker-ce-cli containerd.io docker-ce-rootless-extras docker-buildx-plugin)
  if [[ ${INSTALL_CLAUDE:-1} == 1 ]]; then
    local channel=${CLAUDE_CHANNEL:-stable}
    apt_repo claude-code https://downloads.claude.ai/keys/claude-code.asc "$CLAUDE_KEY_FPR" \
      "deb [signed-by=@KEYRING@] https://downloads.claude.ai/claude-code/apt/$channel $channel main"
    packages+=(claude-code)
  fi
  run apt-get update -q
  run apt-get install -y -q --no-install-recommends "${packages[@]}"

  say "Node.js $node_version (official tarball, pinned sha256)"
  local node_dir=/opt/node-$node_version-linux-$node_arch
  if ((!dry)) && [[ -x $node_dir/bin/node ]]; then
    ok "$node_dir exists"
  else
    fetch_verified "https://nodejs.org/dist/$node_version/node-$node_version-linux-$node_arch.tar.xz" \
      "$node_sha" "$work/node.tar.xz"
    run tar -xJf "$work/node.tar.xz" -C /opt
  fi
  for tool in node npm npx corepack; do
    run ln -sfn "$node_dir/bin/$tool" "/usr/local/bin/$tool"
  done

  say "uv $uv_version (pinned sha256)"
  if ((!dry)) && [[ $(/usr/local/bin/uv --version 2>/dev/null) == "uv $uv_version"* ]]; then
    ok "uv $uv_version installed"
  else
    fetch_verified "https://github.com/astral-sh/uv/releases/download/$uv_version/uv-$tool_arch-unknown-linux-gnu.tar.gz" \
      "$uv_sha" "$work/uv.tar.gz"
    run tar -xzf "$work/uv.tar.gz" -C /usr/local/bin --strip-components=1 \
      "uv-$tool_arch-unknown-linux-gnu/uv" "uv-$tool_arch-unknown-linux-gnu/uvx"
  fi

  say "herdr $herdr_version (pinned sha256)"
  if ((!dry)) && [[ $(/usr/local/bin/herdr --version 2>/dev/null) == *"${herdr_version#v}"* ]]; then
    ok "herdr $herdr_version installed"
  else
    fetch_verified "https://github.com/herdrdev/herdr/releases/download/$herdr_version/herdr-linux-$tool_arch" \
      "$herdr_sha" "$work/herdr"
    run install -m 0755 "$work/herdr" /usr/local/bin/herdr
  fi

  say "sshd keepalive"
  if write_file /etc/ssh/sshd_config.d/60-babysit-worker.conf 0644 "$(
    printf '%s\n' '# babysit-pr worker: drop dead controller connections within a minute.' \
      'ClientAliveInterval 15' 'ClientAliveCountMax 4'
  )"; then
    run sshd -t
    run systemctl reload ssh.service
  fi

  say "AppArmor and bubblewrap"
  ensure_bwrap_profile

  say "Rootless docker for $user_name"
  # Rootful docker is root-equivalent; babysit uses only its own rootless daemon.
  run systemctl disable --now docker.service docker.socket
  ensure_rootlesskit_profile
  run loginctl enable-linger "$user_name"
  if ((dry)); then
    uid=UID
  else
    uid=$(id -u "$user_name")
    for _ in $(seq 1 20); do
      [[ -S /run/user/$uid/bus ]] && break
      sleep 0.5
    done
  fi
  if ((!dry)) && [[ -f $(getent passwd "$user_name" | cut -d: -f6)/.config/systemd/user/docker.service ]]; then
    ok "rootless docker service exists"
  else
    run sudo -u "$user_name" -H env XDG_RUNTIME_DIR="/run/user/$uid" \
      DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" \
      dockerd-rootless-setuptool.sh install
  fi

  run_checks
  cat <<EOF

Next, as $user_name (see references/worker-setup.md):
  ssh $user_name@<box> 'bash -s -- --user-step --sha <SHA>' < scripts/provision_worker.sh
EOF
  finish
}

# --- user step -------------------------------------------------------------------------

user_step() {
  local dest=$HOME/src/babysit-pr codex=${CODEX_VERSION:-$CODEX_DEFAULT}
  [[ -n $sha ]] || {
    printf 'provision_worker: --user-step needs --sha <babysit-pr commit>\n' >&2
    exit 2
  }
  [[ $sha =~ ^[0-9a-f]{7,40}$ ]] || {
    printf 'provision_worker: --sha must be a hexadecimal commit id\n' >&2
    exit 2
  }
  if ((!dry)) && ((EUID == 0)); then
    printf 'provision_worker: run the user step as %s, not root\n' "$user_name" >&2
    exit 2
  fi
  [[ $(id -un) == "$user_name" ]] || ((dry)) || warn "running as $(id -un), not $user_name"

  say "babysit-pr at $sha"
  if ((!dry)) && [[ -d $dest/.git ]]; then
    [[ -z $(git -C "$dest" status --porcelain) ]] || {
      problem "$dest has local changes; leaving it alone"
      finish
    }
  else
    run mkdir -p "$HOME/src"
    run git clone "$repo" "$dest"
  fi
  run git -C "$dest" fetch --quiet origin
  run git -C "$dest" checkout --quiet --detach "$sha"

  say "Directories and PATH"
  run mkdir -p "$HOME/.cache/babysit-sandbox" "$HOME/.npm-global" "$HOME/.local/bin"
  run chmod 700 "$HOME/.cache/babysit-sandbox"
  # Codex updates run `npm install -g`; a user prefix keeps them out of /opt.
  run npm config set prefix "$HOME/.npm-global"
  # First in ~/.bashrc, above Ubuntu's early return for non-interactive shells, so that
  # `ssh box codex ...` and herdr panes find the same tools as a login shell.
  local marker='# babysit-pr worker PATH'
  # shellcheck disable=SC2016
  local line='export PATH="$HOME/.npm-global/bin:$HOME/.local/bin:$HOME/src/babysit-pr/scripts:$PATH"'
  if ((dry)); then
    printf '  would prepend the npm prefix, ~/.local/bin and babysit-pr/scripts to PATH in ~/.bashrc\n'
  elif ! grep -qF "$marker" "$HOME/.bashrc" 2>/dev/null; then
    { printf '%s\n%s\n\n' "$marker" "$line" && cat "$HOME/.bashrc" 2>/dev/null; } >"$HOME/.bashrc.tmp.$$"
    mv -f "$HOME/.bashrc.tmp.$$" "$HOME/.bashrc"
  fi

  say "git identity"
  if [[ -n ${GIT_USER_NAME:-} && -n ${GIT_USER_EMAIL:-} ]]; then
    run git config --global user.name "$GIT_USER_NAME"
    run git config --global user.email "$GIT_USER_EMAIL"
  elif ((!dry)) && git config --global user.email >/dev/null; then
    ok "git identity: $(git config --global user.name) <$(git config --global user.email)>"
  else
    warn "no git identity; set one (below) before agents commit"
  fi

  if ((!dry)); then
    say "Checks"
    if (cd / && bwrap --ro-bind / / true) >/dev/null 2>&1; then
      ok "bwrap works"
    else
      problem "bwrap --ro-bind / / true fails; rerun the system step"
    fi
    if docker info --format '{{.SecurityOptions}}' 2>/dev/null | grep -q rootless; then
      ok "docker is rootless"
    else
      problem "docker info does not report a rootless daemon"
    fi
  fi

  cat <<EOF

Next steps on this machine (interactive; nothing here pipes an installer into a shell):
  . ~/.bashrc
  git config --global user.name  "Your Name"          # if not set above
  git config --global user.email "you@example.com"
  git config --global commit.gpgsign false            # no 1Password agent here; or set up a worker key
  gh auth login --hostname github.com --git-protocol https
  gh auth setup-git
  npm install -g @openai/codex@$codex                 # npm checks the registry's sha512 integrity
  codex login --device-auth
  claude-account login <name>                         # always a named account on a worker
  herdr --version && claude --version && codex --version
  babysit-bwrap --claude-config-dir="\$HOME/.claude/accounts/<name>" -- true

Then on the controller: herdr machine add, machines.json (references/worker-setup.md).
EOF
  finish
}

case $mode in
  user) user_step ;;
  check)
    if ((!dry)) && ((EUID != 0)); then
      printf 'provision_worker: --check runs as root (it inspects other users)\n' >&2
      exit 2
    fi
    run_checks
    finish
    ;;
  *) system_step ;;
esac
