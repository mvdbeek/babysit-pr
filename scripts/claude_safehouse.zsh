#!/bin/zsh
# Match this user's verified ~/.zshrc Claude wrapper without loading prompt/UI setup.
set -eu
source "$HOME/.config/safehouse/safe.env"
extra=(--env-pass=BABYSIT_PR_REPAIR)
if [[ -n "${CMUX_SURFACE_ID:-}" ]]; then
  names=$(printenv | sed -n 's/^\(CMUX_[A-Za-z0-9_]*\)=.*/\1/p' | paste -sd, -)
  [[ -z "$names" ]] || extra+=(--env-pass="$names")
fi
exec "$HOME/.local/bin/safehouse" --enable=playwright-chrome "${extra[@]}" \
  --append-profile "$HOME/.config/safehouse/python-ipc.sb" \
  claude --dangerously-skip-permissions "$@"
