#!/bin/zsh
# Match this user's verified ~/.zshrc Claude wrapper without loading prompt/UI setup.
set -eu
source "$HOME/.config/safehouse/safe.env"
extra=(--env-pass=BABYSIT_PR_REPAIR)
if [[ -n "${CMUX_SURFACE_ID:-}" ]]; then
  names=$(printenv | sed -n 's/^\(CMUX_[A-Za-z0-9_]*\)=.*/\1/p' | paste -sd, -)
  [[ -z "$names" ]] || extra+=(--env-pass="$names")
fi
# Unattended repairs: git and gh inside the sandbox fail fast instead of prompting.
quiet=$(printenv | sed -En 's/^(GIT_TERMINAL_PROMPT|GH_PROMPT_DISABLED|GH_NO_UPDATE_NOTIFIER|GCM_INTERACTIVE)=.*/\1/p' | paste -sd, -)
[[ -z "$quiet" ]] || extra+=(--env-pass="$quiet")
exec "$HOME/.local/bin/safehouse" --enable=playwright-chrome "${extra[@]}" \
  --append-profile "$HOME/.config/safehouse/python-ipc.sb" \
  claude --dangerously-skip-permissions "$@"
