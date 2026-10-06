#!/bin/zsh
# Match this user's verified ~/.zshrc Claude wrapper without loading prompt/UI setup.
set -eu
account_config="${CLAUDE_CONFIG_DIR:-}"
source "$HOME/.config/safehouse/safe.env"
if [[ -n "$account_config" ]]; then
  export CLAUDE_CONFIG_DIR="$account_config"
fi
extra=(--env-pass=BABYSIT_PR_REPAIR)
if [[ -n "${CLAUDE_CONFIG_DIR:-}" ]]; then
  extra+=(--env-pass=CLAUDE_CONFIG_DIR --add-dirs="$CLAUDE_CONFIG_DIR")
fi
if [[ -n "${CMUX_SURFACE_ID:-}" ]]; then
  names=$(printenv | sed -n 's/^\(CMUX_[A-Za-z0-9_]*\)=.*/\1/p' | paste -sd, -)
  [[ -z "$names" ]] || extra+=(--env-pass="$names")
fi
# Unattended repairs: git and gh inside the sandbox fail fast instead of prompting.
quiet=$(printenv | sed -En 's/^(GIT_TERMINAL_PROMPT|GH_PROMPT_DISABLED|GH_NO_UPDATE_NOTIFIER|GCM_INTERACTIVE)=.*/\1/p' | paste -sd, -)
[[ -z "$quiet" ]] || extra+=(--env-pass="$quiet")
[[ -z "${SAFE_ENABLE:-}" ]] || extra+=(--enable="$SAFE_ENABLE")
exec "$HOME/.local/bin/safehouse" --enable=playwright-chrome "${extra[@]}" \
  --append-profile "$HOME/.config/safehouse/python-ipc.sb" \
  claude --dangerously-skip-permissions "$@"
