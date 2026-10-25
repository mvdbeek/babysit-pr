#!/usr/bin/env bash
# Provision an Ubuntu 24.04 or 26.04 machine as a babysit-pr worker. Idempotent; rerun after
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
#   ssh babysit@box 'bash -s -- --check' < scripts/provision_worker.sh
#
# Options:
#   --check       change nothing; run only the host checks (as root) or the user
#                 checks, including commit signing when set up (as the babysit user)
#   --dry-run     change nothing and touch no network; print the planned actions
#   --user-step   run the non-root step as the babysit user (needs --sha)
#   --sha SHA     full 40-hex babysit-pr commit to deploy in ~/.local/share/babysit-pr/app
#                 (user step)
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
#                       namespace; unconfined: leaves bwrap AND everything it starts
#                       unconfined, so any local user can get a fully capable user
#                       namespace through bwrap. Needs BWRAP_APPARMOR_I_UNDERSTAND=1.
#                       Used only if bwrap does not already work.
#   DOCKER_SOURCE       auto (default): keep Docker's docker-ce repository if this script
#                       added it earlier, else use the distro's docker.io when it is
#                       already installed, else docker-ce; or force docker-ce / docker.io.
#                       The machine's rootful docker is never stopped or masked.
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
# Test hook: the root the apt sources are read and written under. Empty on a machine.
ROOT=${PROVISION_ROOT:-}
# The babysit-pr this worker runs, apart from ~/src where agents work on clones: an agent
# able to write it would change babysit-bwrap itself, which refuses to grant it.
APP=.local/share/babysit-pr/app
# git in that checkout runs no hooks, fsmonitor or ext:: transport, whatever it holds.
SAFE_GIT=(git -c core.fsmonitor=false -c core.hooksPath=/dev/null -c protocol.ext.allow=never)
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
  tmp=$(mktemp -d -p "$work")
  curl -fsSL --proto '=https' --tlsv1.2 -o "$tmp/download" "$url"
  if ! printf '%s  %s\n' "$want" "$tmp/download" | sha256sum -c --status; then
    rm -rf "$tmp"
    printf 'provision_worker: checksum mismatch for %s\n' "$url" >&2
    exit 1
  fi
  mv -f "$tmp/download" "$dest"
  rm -rf "$tmp"
}

# --- packages --------------------------------------------------------------------------

# installed PACKAGE: dpkg has it installed.
installed() {
  [[ $(dpkg-query -W -f='${Status}' "$1" 2>/dev/null) == *' ok installed' ]]
}

# want PACKAGE...: adds those not installed to packages. apt-get install upgrades an
# installed package, and upgrading docker-ce, docker.io or containerd restarts the
# operator's rootful daemon, so only missing packages are ever installed.
want() {
  local package
  for package; do
    installed "$package" || packages+=("$package")
  done
}

# repo_files URL [EXCEPT]: the apt sources, one-line or deb822, other than EXCEPT that
# use URL in a line that is not a comment.
repo_files() {
  local url=$1 except=${2:-} file
  for file in "$ROOT/etc/apt/sources.list" "$ROOT"/etc/apt/sources.list.d/*.list \
    "$ROOT"/etc/apt/sources.list.d/*.sources; do
    [[ -f $file && $file != "$except" ]] || continue
    if grep -qE "^[^#]*${url//./\\.}(/| |\$)" "$file"; then
      printf '%s\n' "$file"
    fi
  done
}

# apt_repo NAME KEY_URL FINGERPRINT URL DEB_LINE: reuse a source that already names URL;
# else import the downloaded key into a scratch GNUPGHOME, require exactly one key
# whose primary fingerprint is the pinned one, and export only that key (binary) as
# the repository's signed-by keyring. A second source for one URL with another
# Signed-By would stop apt working at all ("Conflicting values set for option
# Signed-By"). Keyrings are never removed: one this script did not write may be
# another source's Signed-By, and an unreferenced one is trusted by nothing.
apt_repo() {
  local name=$1 key_url=$2 fpr=$3 url=$4 line=$5 tmp got others
  local keyring=/etc/apt/keyrings/$name.gpg list=$ROOT/etc/apt/sources.list.d/$name.list
  line=${line//@KEYRING@/$keyring}
  others=$(repo_files "$url" "$list")
  if [[ -n $others ]]; then
    ok "reusing the $url repository in $(tr '\n' ' ' <<<"$others")"
    # An earlier run's own entry, recognised by its exact content.
    if [[ -f $list ]] && [[ $(cat "$list") == "$line" ]]; then
      run rm -f "$list"
    fi
    return 0
  fi
  if ((dry)); then
    printf '  would trust %s (fingerprint %s), gpg --export only that key to %s and add: %s\n' \
      "$key_url" "$fpr" "$keyring" "$line"
    return 0
  fi
  install -d -m 0755 "$ROOT/etc/apt/keyrings"
  tmp=$(mktemp -d -p "$work")
  mkdir -m 0700 "$tmp/gnupg"
  curl -fsSL --proto '=https' --tlsv1.2 -o "$tmp/key" "$key_url"
  gpg --batch --homedir "$tmp/gnupg" --import "$tmp/key" >/dev/null 2>&1 || true
  got=$(gpg --batch --homedir "$tmp/gnupg" --list-keys --with-colons 2>/dev/null |
    awk -F: '$1 == "pub" { p = 1 } $1 == "fpr" && p { print $10; p = 0 }')
  if [[ $got != "$fpr" ]]; then
    rm -rf "$tmp"
    printf 'provision_worker: %s has primary fingerprint(s) %s, expected exactly %s\n' \
      "$key_url" "$(tr '\n' ' ' <<<"${got:-none}")" "$fpr" >&2
    exit 1
  fi
  gpg --batch --homedir "$tmp/gnupg" --export "$fpr" >"$tmp/keyring.gpg"
  install -m 0644 -o root -g root "$tmp/keyring.gpg" "$ROOT$keyring"
  rm -rf "$tmp"
  write_file "$list" 0644 "$line" || true
}

# --- root-owned install markers ----------------------------------------------------------

STATE_DIR=/usr/local/share/babysit-provision

# tree_digest PATH...: one digest over the names and contents of every file and link.
tree_digest() {
  find "$@" \( -type f -o -type l \) -print0 | sort -z | xargs -0 sha256sum | sha256sum |
    cut -d' ' -f1
}

# locked_down PATH...: true when everything is root-owned and nothing is group/world
# writable (symlinks only need root ownership).
locked_down() {
  [[ -z $(find "$@" \( -type l ! -user root \) -o \
    \( ! -type l \( ! -user root -o -perm /022 \) \) -print -quit) ]]
}

# install_ok NAME ARCHIVE_SHA PATH...: the marker written at install time still matches
# the pinned checksum and the installed files, which are still root's alone. Nothing
# installed is executed to find out.
install_ok() {
  local marker=$STATE_DIR/$1.sha256 want=$2
  shift 2
  local path
  for path in "$@"; do
    [[ -e $path ]] || return 1
  done
  [[ -f $marker && ! -L $marker ]] || return 1
  locked_down "$@" || return 1
  [[ $(cat "$marker") == "$want $(tree_digest "$@")" ]]
}

# record_install NAME ARCHIVE_SHA PATH...
record_install() {
  local name=$1 want=$2
  shift 2
  if ((dry)); then
    printf '  would record %s/%s.sha256 (verified checksum and install digest)\n' "$STATE_DIR" "$name"
    return 0
  fi
  install -d -m 0755 -o root -g root "$STATE_DIR"
  write_file "$STATE_DIR/$name.sha256" 0644 "$want $(tree_digest "$@")" || true
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
  if grep -q '@/tmp/\.X11-unix' <<<"$(ss -xl 2>/dev/null)"; then
    found="$found X11-abstract-socket"
  fi
  if [[ -n $found ]]; then
    warn "a desktop session is running ($found): its abstract sockets are reachable from the sandbox; run the worker headless"
  else
    ok "no desktop session"
  fi
}

# check_groups USER: membership that is (or leads to) root, or to raw devices.
check_groups() {
  local user=$1 group groups
  groups=$(id -nG "$user" 2>/dev/null | tr ' ' '\n') || groups=
  # Container and VM managers mount any host path; disk is raw block devices.
  for group in sudo admin wheel adm docker lxd incus-admin libvirt disk; do
    if grep -qx "$group" <<<"$groups"; then
      problem "$user is in the $group group, which is (or leads to) root on this machine"
    fi
  done
  if grep -qx kvm <<<"$groups"; then
    warn "$user is in the kvm group: /dev/kvm access, more than agents need"
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
  check_groups "$user_name"
  if grep -q 'may run the following commands' <<<"$(sudo -n -l -U "$user_name" 2>/dev/null)"; then
    problem "sudoers grants $user_name commands (sudo -l -U $user_name)"
  fi
  if [[ -S /var/run/docker.sock ]] &&
    sudo -u "$user_name" -H test -w /var/run/docker.sock >/dev/null 2>&1; then
    problem "$user_name can write the rootful docker socket /var/run/docker.sock (root on this machine)"
  else
    ok "$user_name cannot reach the rootful docker socket"
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
  # Read the whole dump first: grep -q stopping early would SIGPIPE sshd under pipefail.
  if grep -qx 'clientaliveinterval 15' <<<"$(sshd -T 2>/dev/null)"; then
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
      if [[ ${BWRAP_APPARMOR_I_UNDERSTAND:-0} != 1 ]]; then
        problem "BWRAP_APPARMOR=unconfined needs BWRAP_APPARMOR_I_UNDERSTAND=1: it leaves bwrap and its children unconfined"
        return 0
      fi
      say "Installing a bwrap AppArmor profile that leaves bwrap and its children unconfined"
      write_file /etc/apparmor.d/bwrap 0644 "$(
        cat <<'EOF'
# babysit-pr worker: lets /usr/bin/bwrap create user namespaces under
# kernel.apparmor_restrict_unprivileged_userns=1. Written by provision_worker.sh.
# WARNING: bwrap and everything it starts run unconfined, so any local user can get a
# fully capable user namespace through bwrap.
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
  # Docker's documented profile for rootlesskit on Ubuntu, for the packaged path.
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
    [[ ${ID:-} == ubuntu && ( ${VERSION_ID:-} == 24.04 || ${VERSION_ID:-} == 26.04 ) ]] ||
      warn "written for Ubuntu 24.04 and 26.04, found ${PRETTY_NAME:-unknown}"
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
  if [[ ${BWRAP_APPARMOR:-restrict} == unconfined && ${BWRAP_APPARMOR_I_UNDERSTAND:-0} != 1 ]]; then
    printf 'provision_worker: BWRAP_APPARMOR=unconfined leaves bwrap and its children unconfined (any local user gets a fully capable user namespace); set BWRAP_APPARMOR_I_UNDERSTAND=1 to accept\n' >&2
    exit 2
  fi
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
      printf '  would run: runuser -u %s -- sh -c <umask 077; refuse symlinked ~/.ssh or authorized_keys; append keys> (keys on stdin)\n' "$user_name"
    else
      home=$(getent passwd "$user_name" | cut -d: -f6)
      # As the user, so a symlink planted in its home cannot redirect root's writes.
      runuser -u "$user_name" -- sh -c '
        set -eu
        umask 077
        ssh=$1/.ssh
        keys=$ssh/authorized_keys
        for p in "$ssh" "$keys"; do
          if [ -L "$p" ]; then echo "provision_worker: $p is a symlink" >&2; exit 1; fi
        done
        [ -d "$ssh" ] || mkdir -m 0700 "$ssh"
        chmod 0700 "$ssh"
        touch "$keys"
        while IFS= read -r key; do
          [ -z "$key" ] || grep -qxF -- "$key" "$keys" || printf "%s\n" "$key" >>"$keys"
        done
        chmod 0600 "$keys"
      ' sh "$home" <<<"$BABYSIT_SSH_KEY"
    fi
  fi

  say "Base packages"
  run apt-get update -q
  local packages=()
  want ca-certificates curl gnupg git jq bubblewrap socat build-essential uidmap \
    dbus-user-session slirp4netns python3 python3-venv xz-utils apparmor apparmor-utils \
    apparmor-profiles iproute2
  if ((${#packages[@]})); then
    run apt-get install -y -q --no-install-recommends "${packages[@]}"
  else
    ok "base packages are installed"
  fi

  say "Signed apt repositories"
  # Keep a distro-packaged gh and docker.io rather than adding a conflicting repository.
  packages=()
  local docker_src=${DOCKER_SOURCE:-auto} setuptool=dockerd-rootless-setuptool.sh
  local docker_url=https://download.docker.com/linux/ubuntu gh_url=https://cli.github.com/packages
  if [[ $docker_src == auto ]]; then
    docker_src=docker-ce
    if ! installed docker-ce && [[ -z $(repo_files "$docker_url") ]] && installed docker.io; then
      docker_src=docker.io
    fi
  fi
  case $docker_src in docker-ce | docker.io) ;; *)
    printf 'provision_worker: DOCKER_SOURCE must be auto, docker-ce or docker.io\n' >&2
    exit 2
    ;;
  esac
  if [[ -n $(repo_files "$gh_url") ]] || ! installed gh; then
    apt_repo githubcli "$gh_url/githubcli-archive-keyring.gpg" "$GH_KEY_FPR" "$gh_url" \
      "deb [arch=$arch signed-by=@KEYRING@] $gh_url stable main"
    want gh
  else
    ok "gh is installed from the distro; not adding the GitHub CLI repository"
  fi
  if [[ $docker_src == docker-ce ]]; then
    apt_repo docker "$docker_url/gpg" "$DOCKER_KEY_FPR" "$docker_url" \
      "deb [arch=$arch signed-by=@KEYRING@] $docker_url $codename stable"
    if installed docker-ce; then
      # Rootless needs only the extras, at the running daemon's version: a newer one
      # would pull a docker-ce upgrade, which restarts the operator's daemon.
      installed docker-ce-rootless-extras ||
        packages+=("docker-ce-rootless-extras=$(dpkg-query -W -f='${Version}' docker-ce)")
    else
      want docker-ce docker-ce-cli containerd.io docker-ce-rootless-extras docker-buildx-plugin
    fi
  else
    ok "using the distro's docker.io; not adding Docker's repository"
    want docker.io rootlesskit
  fi
  if [[ ${INSTALL_CLAUDE:-1} == 1 ]]; then
    local channel=${CLAUDE_CHANNEL:-stable}
    apt_repo claude-code https://downloads.claude.ai/keys/claude-code.asc "$CLAUDE_KEY_FPR" \
      "https://downloads.claude.ai/claude-code/apt/$channel" \
      "deb [signed-by=@KEYRING@] https://downloads.claude.ai/claude-code/apt/$channel $channel main"
    want claude-code
  fi
  run apt-get update -q
  if ((${#packages[@]})); then
    run apt-get install -y -q --no-install-recommends "${packages[@]}"
  else
    ok "repository packages are installed"
  fi

  # Repair what an earlier version of this script left agent-owned (tar kept the
  # archive's uid), then trust nothing but the root-owned markers.
  if ((!dry)); then
    find /opt -maxdepth 1 -name 'node-*' -exec chown -hR root:root {} + -exec chmod -R go-w {} +
    for f in /usr/local/bin/uv /usr/local/bin/uvx /usr/local/bin/herdr; do
      [[ ! -e $f && ! -L $f ]] || chown -h root:root "$f"
      [[ ! -f $f || -L $f ]] || chmod go-w "$f"
    done
  fi

  say "Node.js $node_version (official tarball, pinned sha256)"
  local node_dir=/opt/node-$node_version-linux-$node_arch
  if ((!dry)) && install_ok "node-$node_version-$node_arch" "$node_sha" "$node_dir"; then
    ok "$node_dir matches its verified checksum marker"
  else
    fetch_verified "https://nodejs.org/dist/$node_version/node-$node_version-linux-$node_arch.tar.xz" \
      "$node_sha" "$work/node.tar.xz"
    run mkdir "$work/node"
    run tar -xJf "$work/node.tar.xz" -C "$work/node" --no-same-owner --no-same-permissions
    run chown -R root:root "$work/node"
    run chmod -R go-w "$work/node"
    run rm -rf "$node_dir"
    run mv "$work/node/node-$node_version-linux-$node_arch" "$node_dir"
    record_install "node-$node_version-$node_arch" "$node_sha" "$node_dir"
  fi
  for tool in node npm npx corepack; do
    run ln -sfn "$node_dir/bin/$tool" "/usr/local/bin/$tool"
  done

  say "uv $uv_version (pinned sha256)"
  if ((!dry)) && install_ok "uv-$uv_version-$tool_arch" "$uv_sha" /usr/local/bin/uv /usr/local/bin/uvx; then
    ok "uv $uv_version matches its verified checksum marker"
  else
    fetch_verified "https://github.com/astral-sh/uv/releases/download/$uv_version/uv-$tool_arch-unknown-linux-gnu.tar.gz" \
      "$uv_sha" "$work/uv.tar.gz"
    run mkdir "$work/uv"
    run tar -xzf "$work/uv.tar.gz" -C "$work/uv" --strip-components=1 --no-same-owner \
      --no-same-permissions "uv-$tool_arch-unknown-linux-gnu/uv" "uv-$tool_arch-unknown-linux-gnu/uvx"
    run install -o root -g root -m 0755 "$work/uv/uv" /usr/local/bin/uv
    run install -o root -g root -m 0755 "$work/uv/uvx" /usr/local/bin/uvx
    record_install "uv-$uv_version-$tool_arch" "$uv_sha" /usr/local/bin/uv /usr/local/bin/uvx
  fi

  say "herdr $herdr_version (pinned sha256)"
  if ((!dry)) && install_ok "herdr-$herdr_version-$tool_arch" "$herdr_sha" /usr/local/bin/herdr; then
    ok "herdr $herdr_version matches its verified checksum marker"
  else
    fetch_verified "https://github.com/herdrdev/herdr/releases/download/$herdr_version/herdr-linux-$tool_arch" \
      "$herdr_sha" "$work/herdr"
    run install -o root -g root -m 0755 "$work/herdr" /usr/local/bin/herdr
    record_install "herdr-$herdr_version-$tool_arch" "$herdr_sha" /usr/local/bin/herdr
  fi

  say "sshd keepalive"
  local dropin=/etc/ssh/sshd_config.d/60-babysit-worker.conf
  if write_file "$dropin" 0644 "$(
    printf '%s\n' '# babysit-pr worker: drop dead controller connections within a minute.' \
      'ClientAliveInterval 15' 'ClientAliveCountMax 4'
  )"; then
    if ((dry)); then
      run sshd -t
      run systemctl try-reload-or-restart ssh.service
    elif ! sshd -t; then
      rm -f "$dropin"
      problem "sshd -t rejected $dropin; removed it"
    else
      systemctl try-reload-or-restart ssh.service
    fi
  fi

  say "AppArmor and bubblewrap"
  ensure_bwrap_profile

  say "Rootless docker for $user_name"
  # The machine's rootful docker stays as it is (the operator may use it); babysit is kept
  # out of the docker group and uses only its own rootless daemon.
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
  if ! command -v "$setuptool" >/dev/null && [[ -x /usr/share/docker.io/contrib/$setuptool ]]; then
    # docker.io keeps the rootless scripts in contrib/ without the docker CLI, and the
    # tool runs $BIN/docker from the dockerd-rootless.sh directory; give it a
    # root-owned directory holding all three, which docker.service then keeps using.
    local rootless_bin=/usr/local/lib/babysit-rootless-docker
    run install -d -o root -g root -m 0755 "$rootless_bin"
    run ln -sfn "/usr/share/docker.io/contrib/$setuptool" "$rootless_bin/$setuptool"
    run ln -sfn /usr/share/docker.io/contrib/dockerd-rootless.sh "$rootless_bin/dockerd-rootless.sh"
    run ln -sfn /usr/bin/docker "$rootless_bin/docker"
    setuptool=$rootless_bin/$setuptool
  fi
  if ((!dry)) && [[ -f $(getent passwd "$user_name" | cut -d: -f6)/.config/systemd/user/docker.service ]]; then
    ok "rootless docker service exists"
  else
    # The tool looks dockerd-rootless.sh up on PATH and records that PATH in the
    # user's docker.service.
    run sudo -u "$user_name" -H env XDG_RUNTIME_DIR="/run/user/$uid" \
      DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" \
      PATH="$(dirname "$setuptool"):/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" \
      "$setuptool" install
  fi

  run_checks
  cat <<EOF

Next, as $user_name (see references/worker-setup.md):
  ssh $user_name@<box> 'bash -s -- --user-step --sha <SHA>' < scripts/provision_worker.sh
EOF
  finish
}

# --- user step -------------------------------------------------------------------------

# Commit signing through the worker's signing subkey (references/worker-setup.md), when
# it is set up: the agent is unlocked at login, signs directly, and signs a commit made
# inside babysit-bwrap --enable=gpg. Prints no key material and never the passphrase.
gpg_checks() {
  local key wrapper tmp result
  key=$(git config --global user.signingkey 2>/dev/null) || key=
  if [[ ! -f $HOME/.gnupg/signing-subkey.pass || -z $key ]]; then
    ok "commit signing not set up (no ~/.gnupg/signing-subkey.pass or user.signingkey)"
    return 0
  fi
  if systemctl --user is-enabled --quiet babysit-gpg-preset.service 2>/dev/null; then
    ok "babysit-gpg-preset.service is enabled"
  else
    problem "babysit-gpg-preset.service is not enabled; the agent stays locked after login"
  fi
  if gpg --batch --no-tty --pinentry-mode error --local-user "$key" --detach-sign \
    </dev/null >/dev/null 2>&1; then
    ok "gpg signs with user.signingkey"
  else
    problem "gpg cannot sign with user.signingkey without a prompt; is the preset unit's passphrase right?"
  fi
  wrapper=$HOME/$APP/scripts/babysit-bwrap
  if [[ ! -x $wrapper ]]; then
    problem "no babysit-bwrap at $wrapper; rerun the user step with a commit that has it"
    return
  fi
  tmp=$(mktemp -d)
  # A cd, since the sandbox starts in the caller's directory.
  if git init -q "$tmp" && (cd "$tmp" && "$wrapper" --enable=gpg --add-dirs="$tmp" -- \
    git -c commit.gpgsign=true commit -q --allow-empty -m "babysit signing check") \
    </dev/null >/dev/null 2>&1; then
    if git -C "$tmp" verify-commit HEAD >/dev/null 2>&1; then
      result=ok
    else
      result="signs, but git verify-commit rejects the signature"
    fi
  else
    result="cannot commit signed inside $wrapper --enable=gpg"
  fi
  rm -rf "$tmp"
  if [[ $result == ok ]]; then
    ok "a commit signed inside babysit-bwrap --enable=gpg verifies"
  else
    problem "$result"
  fi
}

user_checks() {
  local app=$HOME/$APP head found
  say "Checks"
  check_groups "$(id -un)"
  if [[ -d $app/.git ]] && head=$("${SAFE_GIT[@]}" -C "$app" rev-parse HEAD 2>/dev/null); then
    ok "babysit-pr deployed at $app ($head)"
    [[ -z $("${SAFE_GIT[@]}" -C "$app" status --porcelain 2>/dev/null) ]] ||
      problem "$app has local changes"
  else
    problem "no babysit-pr checkout at $app; run the user step"
  fi
  found=$(command -v babysit-bwrap 2>/dev/null) || found=
  if [[ $found != "$app/scripts/babysit-bwrap" ]]; then
    warn "babysit-bwrap on PATH is ${found:-missing}, not $app/scripts/babysit-bwrap (. ~/.bashrc?)"
  fi
  if grep -qF 'src/babysit-pr/scripts' "$HOME/.bashrc" 2>/dev/null; then
    problem "~/.bashrc still puts ~/src/babysit-pr/scripts on PATH; rerun the user step"
  fi
  if (cd / && bwrap --ro-bind / / true) >/dev/null 2>&1; then
    ok "bwrap works"
  else
    problem "bwrap --ro-bind / / true fails; rerun the system step"
  fi
  if grep -q rootless <<<"$(DOCKER_HOST="unix://${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/docker.sock" \
    docker info --format '{{.SecurityOptions}}' 2>/dev/null)"; then
    ok "docker is rootless"
  else
    problem "docker info does not report a rootless daemon"
  fi
  gpg_checks
}

user_step() {
  local dest=$HOME/$APP codex=${CODEX_VERSION:-$CODEX_DEFAULT}
  [[ -n $sha ]] || {
    printf 'provision_worker: --user-step needs --sha <babysit-pr commit>\n' >&2
    exit 2
  }
  [[ $sha =~ ^[0-9a-f]{40}$ ]] || {
    printf 'provision_worker: --sha must be a full 40-character hexadecimal commit id\n' >&2
    exit 2
  }
  if ((!dry)) && ((EUID == 0)); then
    printf 'provision_worker: run the user step as %s, not root\n' "$user_name" >&2
    exit 2
  fi
  [[ $(id -un) == "$user_name" ]] || ((dry)) || warn "running as $(id -un), not $user_name"

  say "babysit-pr at $sha in $dest"
  if ((!dry)) && [[ -d $dest/.git ]]; then
    [[ -z $("${SAFE_GIT[@]}" -C "$dest" status --porcelain) ]] || {
      problem "$dest has local changes; leaving it alone"
      finish
    }
  else
    run mkdir -p "$(dirname "$dest")"
    run "${SAFE_GIT[@]}" clone "$repo" "$dest"
  fi
  run "${SAFE_GIT[@]}" -C "$dest" fetch --quiet origin
  run "${SAFE_GIT[@]}" -C "$dest" checkout --quiet --detach "$sha"

  say "Directories and PATH"
  run mkdir -p "$HOME/.cache/babysit-sandbox" "$HOME/.npm-global" "$HOME/.local/bin"
  run chmod 700 "$HOME/.cache/babysit-sandbox"
  # Codex updates run `npm install -g`; a user prefix keeps them out of /opt.
  run npm config set prefix "$HOME/.npm-global"
  # First in ~/.bashrc, above Ubuntu's early return for non-interactive shells, so that
  # `ssh box codex ...` and herdr panes find the same tools as a login shell.
  local marker='# babysit-pr worker PATH'
  # shellcheck disable=SC2016
  local line="export PATH=\"\$HOME/.npm-global/bin:\$HOME/.local/bin:\$HOME/$APP/scripts:\$PATH\""
  if ((dry)); then
    printf '  would prepend the npm prefix, ~/.local/bin and %s/scripts to PATH in ~/.bashrc\n' "~/$APP"
  elif ! grep -qF "$marker" "$HOME/.bashrc" 2>/dev/null; then
    { printf '%s\n%s\n\n' "$marker" "$line" && cat "$HOME/.bashrc" 2>/dev/null; } >"$HOME/.bashrc.tmp.$$"
    mv -f "$HOME/.bashrc.tmp.$$" "$HOME/.bashrc"
  elif ! grep -qxF -- "$line" "$HOME/.bashrc"; then
    # Earlier runs put ~/src/babysit-pr/scripts, a clone agents may write, first.
    awk -v marker="$marker" -v line="$line" 'fix { print line; fix = 0; next } { print } $0 == marker { fix = 1 }' \
      "$HOME/.bashrc" >"$HOME/.bashrc.tmp.$$"
    mv -f "$HOME/.bashrc.tmp.$$" "$HOME/.bashrc"
  fi
  # Without DOCKER_HOST the CLI asks the rootful daemon's socket, which this user
  # cannot reach; agents and `ssh box docker ...` need the rootless one.
  local docker_marker='# babysit-pr worker rootless docker'
  # shellcheck disable=SC2016
  local docker_line='export DOCKER_HOST="unix://${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/docker.sock"'
  if ((dry)); then
    printf '  would point DOCKER_HOST at the rootless daemon in ~/.bashrc\n'
  elif ! grep -qF "$docker_marker" "$HOME/.bashrc" 2>/dev/null; then
    { printf '%s\n%s\n\n' "$docker_marker" "$docker_line" && cat "$HOME/.bashrc"; } >"$HOME/.bashrc.tmp.$$"
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

  ((dry)) || user_checks

  cat <<EOF

Next steps on this machine (interactive; nothing here pipes an installer into a shell):
  . ~/.bashrc
  git config --global user.name  "Your Name"          # if not set above
  git config --global user.email "you@example.com"
  git config --global commit.gpgsign false            # or a signing subkey (worker-setup.md)
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
    # Root inspects the host and other users; the worker user checks its own setup.
    if ((!dry)) && ((EUID != 0)); then
      user_checks
    else
      run_checks
    fi
    finish
    ;;
  *) system_step ;;
esac
