# --- worktree helpers ---

# Optional config file. Sourced once at load; may set WT_MULTIPLEXER to one of
# herdr | tmux | cmux | none | auto  (see _wt_mux). Lets the same helpers drive
# tmux on one machine and cmux or herdr on another without touching this script.
[ -r "${XDG_CONFIG_HOME:-$HOME/.config}/worktree/config" ] && \
  source "${XDG_CONFIG_HOME:-$HOME/.config}/worktree/config"

# True when a herdr server is reachable from this shell. herdr exports HERDR_ENV=1
# into the panes it manages, but not every launcher passes it on (Claude Code's
# Bash tool does not), so also ask the server directly. The probe is a local
# socket round-trip of about 10 ms and fails fast when nothing is listening.
_wt_herdr_available() {
  command -v herdr >/dev/null 2>&1 || return 1
  [[ "$HERDR_ENV" == 1 ]] && return 0
  herdr status server --json 2>/dev/null | grep -q '"running":true'
}

# Resolve which multiplexer to drive. $WT_MULTIPLEXER wins (env or config file);
# "auto" (the default when unset) prefers herdr when a herdr server is running on
# this machine (see _wt_herdr_available), then cmux when running inside cmux,
# then tmux, then a plain `cd`.
_wt_mux() {
  local m="${WT_MULTIPLEXER:-auto}"
  if [[ "$m" == "auto" ]]; then
    if _wt_herdr_available; then
      m=herdr
    elif [[ -n "$CMUX_SURFACE_ID" ]] && command -v cmux >/dev/null 2>&1; then
      m=cmux
    elif command -v tmux >/dev/null 2>&1; then
      m=tmux
    else
      m=none
    fi
  fi
  printf '%s\n' "$m"
}

# JSON-encode a string, including its surrounding quotes, for embedding in a cmux
# layout. Needed because an arbitrary prompt may contain quotes, backslashes or
# newlines that would otherwise produce invalid JSON.
_wt_json_str() {
  if command -v jq >/dev/null 2>&1; then
    jq -Rn --arg s "$1" '$s'
  elif command -v python3 >/dev/null 2>&1; then
    python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$1"
  else
    return 1
  fi
}

# Read an initial prompt from a file, so a long brief (a diagnosis, a spec) can be
# handed to the agent without pasting it. Errors go to stderr; the prompt to stdout.
_wt_read_prompt_file() {
  local f="$1"
  if [[ -z "$f" ]]; then
    print -u2 "wt: --prompt-file needs a path"
    return 1
  fi
  if [[ ! -r "$f" ]]; then
    print -u2 "wt: cannot read prompt file: $f"
    return 1
  fi
  print -r -- "$(<"$f")"
}

# Build the shell command that starts the agent, optionally with an initial prompt
# (claude and codex both accept one as a positional argument).
#
# Every multiplexer delivers this by *typing* it into a shell (tmux send-keys; cmux
# "send text+Enter"; herdr pane run), which puts a hard ceiling on the command: canonical-mode tty
# input silently drops a line over MAX_CANON (1024 bytes on macOS). Measured: a
# 629-char line runs, a 2130-char one never executes at all -- so inlining the prompt
# breaks on exactly the long briefs -F exists for, and fails *silently*, leaving an
# idle shell that looks fine.
#
# So stage the prompt in a file and let the shell read it back at execution time. The
# typed line stays ~60 chars whatever the prompt's size, and command-substitution
# output is not re-scanned, so the content needs no escaping. ${(qqqq)} still quotes
# the *path* ($'...' form: exact round-trip, single line, no expansion).
#
# The staged file is left behind deliberately -- the agent reads it after we return.
# It lives in $TMPDIR, which the OS reaps.
_wt_agent_cmd() {
  local agent="$1" prompt="$2"
  if [[ -z "$prompt" ]]; then
    printf '%s' "$agent"
    return
  fi
  local pfile
  if ! pfile=$(mktemp "${TMPDIR:-/tmp}/wt-prompt.XXXXXX"); then
    print -u2 "wt: could not stage the prompt in \${TMPDIR:-/tmp}"
    return 1
  fi
  print -r -- "$prompt" > "$pfile" || return 1
  printf '%s "$(cat %s)"' "$agent" "${(qqqq)pfile}"
}

# open (or attach to) a session for a worktree:
#   left pane runs the selected agent, right pane is a spare terminal
# $4 is an optional initial prompt for the agent; it only applies to a session
# being created, since an existing one already has an agent running in it.
# $5 is the repository name: the cmux workspace-group name, and for herdr the
# parent checkout (~/src/<repo>) that worktree workspaces are grouped under.
_wt_session() {
  local dir="$1" name="$2" agent="${3:-claude}" prompt="$4" repo="$5"
  case "$(_wt_mux)" in
    herdr) _wt_session_herdr "$dir" "$name" "$agent" "$prompt" "$repo" ;;
    cmux) _wt_session_cmux "$dir" "$name" "$agent" "$prompt" "$repo" ;;
    tmux) _wt_session_tmux "$dir" "$name" "$agent" "$prompt" ;;
    *)
      [[ -n "$prompt" ]] && print -u2 "wt: no multiplexer (WT_MULTIPLEXER=none); ignoring the prompt"
      cd "$dir" ;;
  esac
}

_wt_session_tmux() {
  local dir="$1" name="$2" agent="${3:-claude}" prompt="$4"
  if ! command -v tmux >/dev/null 2>&1; then
    cd "$dir"
    return
  fi
  local sess="${name//[^a-zA-Z0-9_-]/-}"   # tmux-safe session name
  if ! tmux has-session -t "=$sess" 2>/dev/null; then
    local cmd
    cmd=$(_wt_agent_cmd "$agent" "$prompt") || return 1
    tmux new-session -d -s "$sess" -c "$dir"
    tmux split-window -h -t "$sess" -c "$dir"
    tmux select-pane -t "$sess" -L
    tmux send-keys -t "$sess" "$cmd" C-m
  elif [[ -n "$prompt" ]]; then
    print -u2 "wt: tmux session '$sess' already running; ignoring the prompt"
  fi
  if [[ -n "$TMUX" ]]; then
    tmux switch-client -t "$sess"
  elif [[ -t 0 ]]; then
    tmux attach-session -t "$sess"
  else
    # No terminal to attach from (e.g. called from an agent's shell tool). The
    # session is running detached; say where instead of failing the whole call.
    print -u2 "wt: tmux session '$sess' started detached; attach with: tmux attach-session -t $sess"
  fi
}

_wt_session_herdr() {
  local dir="$1" name="$2" agent="${3:-claude}" prompt="$4" repo="$5"
  if ! command -v herdr >/dev/null 2>&1; then
    cd "$dir"
    return
  fi
  if ! command -v jq >/dev/null 2>&1; then
    print -u2 "wt: the herdr backend needs jq to read herdr's JSON replies"
    return 1
  fi
  local title="${name//[^a-zA-Z0-9_-]/-}"
  local cmd
  cmd=$(_wt_agent_cmd "$agent" "$prompt") || return 1
  # herdr models a worktree as a workspace with checkout provenance and groups it
  # under a workspace for the parent checkout, which it creates on first use. The
  # open/create actions must be issued *from* that parent (--cwd), not from the
  # worktree itself. The worktree already exists on disk (wt created it), so open
  # rather than create; open is idempotent and reports already_open.
  local out
  local parent="${WT_REPO_PATH:-$HOME/src/$repo}"
  local -a focus_args
  focus_args=(--focus)
  [[ "$WT_NO_FOCUS" == 1 ]] && focus_args=(--no-focus)
  if ! out=$(herdr worktree open --cwd "$parent" --path "$dir" --label "$title" "${focus_args[@]}" 2>&1); then
    print -u2 "wt: herdr worktree open failed: $(jq -r '.error.message // .' 2>/dev/null <<<"$out" || print -r -- "$out")"
    return 1
  fi
  local already root
  already=$(jq -r '.result.already_open // false' <<<"$out")
  root=$(jq -r '.result.root_pane.pane_id // empty' <<<"$out")
  if [[ "$already" == "true" ]]; then
    # Existing workspaces retain their agents; focus follows the explicit option.
    [[ -n "$prompt" ]] && print -u2 "wt: a herdr workspace for $dir already exists; ignoring the prompt"
    return
  fi
  if [[ -z "$root" ]]; then
    print -u2 "wt: herdr did not return a root pane for $dir"
    return 1
  fi
  # new workspace: left pane (the root) runs the agent, right pane is a spare terminal.
  herdr pane split "$root" --direction right --cwd "$dir" --no-focus >/dev/null || return 1
  # pane run submits text + Enter; the pty buffers it until the new shell reads it.
  herdr pane run "$root" "$cmd" >/dev/null
}

_wt_session_cmux() {
  local dir="$1" name="$2" agent="${3:-claude}" prompt="$4" repo="$5"
  if ! command -v cmux >/dev/null 2>&1; then
    cd "$dir"
    return
  fi
  local title="${name//[^a-zA-Z0-9_-]/-}"
  # reuse the workspace already rooted at this worktree, if one exists (needs jq)
  if command -v jq >/dev/null 2>&1; then
    local ref
    ref=$(CMUX_QUIET=1 cmux list-workspaces --json 2>/dev/null \
      | jq -r --arg d "$dir" 'first(.workspaces[] | select(.current_directory==$d) | .ref) // empty')
    if [[ -n "$ref" ]]; then
      [[ -n "$prompt" ]] && print -u2 "wt: a cmux workspace for $dir already exists; ignoring the prompt"
      CMUX_QUIET=1 cmux select-workspace --workspace "$ref" >/dev/null 2>&1
      return
    fi
  fi
  # new workspace: left pane runs the agent, right pane is a spare terminal.
  # A surface's command is typed into the pane, so it is shell-parsed: shell-quote it
  # via _wt_agent_cmd, then JSON-encode it via _wt_json_str (a prompt is arbitrary text
  # and would otherwise break the layout JSON).
  local cmd cmd_json layout group_ref group_json
  cmd=$(_wt_agent_cmd "$agent" "$prompt") || return 1
  if ! cmd_json=$(_wt_json_str "$cmd"); then
    print -u2 "wt: need jq or python3 to build the cmux layout"
    return 1
  fi
  layout='{"direction":"horizontal","split":0.5,"children":[{"pane":{"surfaces":[{"type":"terminal","command":'"$cmd_json"'}]}},{"pane":{"surfaces":[{"type":"terminal"}]}}]}'

  # Keep all workspaces made for a repository together. A cmux group always has
  # an anchor workspace, so create an empty group the first time and put this
  # worktree workspace (and all later ones) beneath it.
  if [[ -n "$repo" ]] && command -v jq >/dev/null 2>&1; then
    group_ref=$(CMUX_QUIET=1 cmux workspace-group list --json 2>/dev/null \
      | jq -r --arg name "$repo" 'first(.groups[] | select(.name==$name) | .ref) // empty')
    if [[ -z "$group_ref" ]]; then
      # Pass an explicit empty --from value: older cmux releases otherwise add
      # the caller workspace to the new group along with its anchor.
      group_json=$(CMUX_QUIET=1 cmux workspace-group create --name "$repo" --cwd "$dir" --from "" --json 2>/dev/null) || return 1
      group_ref=$(print -r -- "$group_json" | jq -r '.group.ref // empty')
      if [[ -z "$group_ref" ]]; then
        print -u2 "wt: cmux did not return a group reference for '$repo'"
        return 1
      fi
    fi
  fi

  local -a group_args
  [[ -n "$group_ref" ]] && group_args=(--group "$group_ref" --group-placement end)
  CMUX_QUIET=1 cmux new-workspace --name "$title" --cwd "$dir" --focus true \
    --layout "$layout" "${group_args[@]}" >/dev/null 2>&1
}

_wt_help() {
  cat <<'EOF'
usage:
  wt [opts] <branch|pr-number> [dirname]
  wt [opts] issue <issue-number|issue-url> [branch-name]

Open or create a Git worktree, then attach a multiplexer session.
Numeric arguments are treated as PR numbers.

The multiplexer is chosen by $WT_MULTIPLEXER (herdr|tmux|cmux|none|auto), set
in the env or ~/.config/worktree/config; "auto" (default) uses herdr inside
herdr, cmux inside cmux, else tmux.

options:
  --codex       start Codex in the left pane
  --claude      start Claude in the left pane (default)
  -r repo       use ~/src/<repo> instead of ~/src/galaxy
  -p, --prompt <text>       start the agent with this initial prompt
  -F, --prompt-file <path>  start the agent with this file's contents as the prompt
  -h, --help    show this help

The prompt only applies to a session being created; if one is already running for
the worktree it is reattached and the prompt is ignored (with a warning).

examples:
  # fire off a fix with a brief, no pasting
  wt -F /tmp/diagnosis.md release_26.1 workflow-copy-drops-readme
  wt -p 'Fix the failing tests in lib/galaxy/tools' dev my-branch
  wt --codex -p 'Review this PR for security issues' 22886
EOF
}

_wti_help() {
  cat <<'EOF'
usage:
  wti [--codex|--claude] [-r repo] [--name name] [--no-focus] <issue-number|issue-url> [branch-name]
  wri [--codex|--claude] [-r repo] <issue-number|issue-url> [branch-name]
  wtissue [--codex|--claude] [-r repo] <issue-number|issue-url> [branch-name]
  wt [--codex|--claude] [-r repo] issue <issue-number|issue-url> [branch-name]

Create or open a worktree for a GitHub issue. If branch-name is omitted,
the branch is generated from the issue number and title (issue-<number>-<slug>).

options:
  --codex       start Codex in the left pane
  --claude      start Claude in the left pane (default)
  -r repo       use ~/src/<repo> instead of the repo from the issue URL or ~/src/galaxy
  -p, --prompt <text>       start the agent with this initial prompt
  -F, --prompt-file <path>  start the agent with this file's contents as the prompt
  --name <name>           reserve a new worktree and branch with this explicit name
  --no-focus              leave native herdr focus unchanged
  --repo-path <path>      use this main clone (overrides -r location)
  --worktree-root <path>  parent directory for the new checkout
  -h, --help    show this help
EOF
}

_wtpr_help() {
  cat <<'EOF'
usage:
  wtpr [--codex|--claude] [-r repo] [--name name] [--no-focus] <pr-number|pr-url>
  wt [--codex|--claude] [-r repo] <pr-number>

Create or open a worktree for a GitHub pull request. PRs from forks are
handled by fetching the PR branch from the contributor repository.

options:
  --codex       start Codex in the left pane
  --claude      start Claude in the left pane (default)
  -r repo       use ~/src/<repo> instead of the repo from the PR URL or ~/src/galaxy
  -p, --prompt <text>       start the agent with this initial prompt
  -F, --prompt-file <path>  start the agent with this file's contents as the prompt
  --name <name>           reserve a new worktree and branch with this explicit name
  --no-focus              leave native herdr focus unchanged
  --repo-path <path>      use this main clone (overrides -r location)
  --worktree-root <path>  parent directory for the new checkout
  -h, --help    show this help
EOF
}

# usage: wt [--codex|--claude] [-r repo] <branch|pr-number> [dirname]
#        wt [--codex|--claude] [-r repo] issue <issue-number|issue-url> [branch-name]
#        numeric arg is treated as a PR number
wt() {
  local repo=galaxy agent=claude prompt=
  while [[ "$1" == -* ]]; do
    case "$1" in
      --codex) agent=codex; shift ;;
      --claude) agent=claude; shift ;;
      -r) repo="$2"; shift 2 ;;
      -p|--prompt) prompt="$2"; shift 2 ;;
      -F|--prompt-file) prompt="$(_wt_read_prompt_file "$2")" || return 1; shift 2 ;;
      -h|--help) _wt_help; return 0 ;;
      *) print -u2 "wt: unknown option: $1"; _wt_help >&2; return 1 ;;
    esac
  done
  local -a fwd
  [[ "$agent" == "codex" ]] && fwd+=(--codex)
  [[ -n "$prompt" ]] && fwd+=(-p "$prompt")
  if [[ "$1" == "issue" || "$1" == "i" ]]; then
    shift
    wti -r "$repo" "${fwd[@]}" "$@"
    return
  fi
  if [[ "$1" =~ ^[0-9]+$ ]]; then
    wtpr -r "$repo" "${fwd[@]}" "$1"
    return
  fi
  local base="$1"
  local branch="${2:-$base}"
  local dir="$HOME/src/worktrees/$repo/$branch"
  if [[ -d "$dir" ]]; then _wt_session "$dir" "$branch" "$agent" "$prompt" "$repo"; return; fi
  mkdir -p "$HOME/src/worktrees/$repo" &&
  git -C "$HOME/src/$repo" fetch origin "$base" &&
  if git -C "$HOME/src/$repo" show-ref --verify --quiet "refs/heads/$branch"; then
    git -C "$HOME/src/$repo" worktree add "$dir" "$branch"
  else
    git -C "$HOME/src/$repo" worktree add -b "$branch" "$dir" "origin/$base"
  fi &&
  _wt_session "$dir" "$branch" "$agent" "$prompt" "$repo"
}

# usage: wti [--codex|--claude] [-r repo] [--name name] [--no-focus] <issue-number|issue-url> [branch-name]
#        wt [--codex|--claude] issue <issue-number|issue-url> [branch-name] also works
wti() {
  local repo=galaxy repo_override= slug= agent=claude prompt= name= repo_path= worktree_root=
  local WT_NO_FOCUS=0 WT_REPO_PATH=
  while [[ "$1" == -* ]]; do
    case "$1" in
      --codex) agent=codex; shift ;;
      --claude) agent=claude; shift ;;
      --no-focus) WT_NO_FOCUS=1; shift ;;
      --name|--repo-path|--worktree-root|-r|-p|--prompt|-F|--prompt-file)
        [[ $# -ge 2 && -n "$2" ]] || { print -u2 "wti: $1 needs a value"; return 1; }
        case "$1" in
          --name) name="$2" ;;
          --repo-path) repo_path="$2" ;;
          --worktree-root) worktree_root="$2" ;;
          -r) repo="$2"; repo_override=1 ;;
          -p|--prompt) prompt="$2" ;;
          -F|--prompt-file) prompt="$(_wt_read_prompt_file "$2")" || return 1 ;;
        esac
        shift 2 ;;
      -h|--help) _wti_help; return 0 ;;
      *) print -u2 "wti: unknown option: $1"; _wti_help >&2; return 1 ;;
    esac
  done
  local issue="$1"
  # A positional branch name may reuse an existing checkout; --name reserves a new one.
  local requested="${2:-$name}"
  # accept a full issue URL, e.g. https://github.com/owner/repo/issues/12345
  if [[ "$issue" == *github.com/*/issues/* ]]; then
    local rest="${issue##*github.com/}"   # owner/repo/issues/12345/...
    local url_owner="${rest%%/*}"; rest="${rest#*/}"
    local url_repo="${rest%%/*}"          # repo name from the URL
    issue="${issue##*/issues/}"   # strip everything up to and including /issues/
    issue="${issue%%[!0-9]*}"     # keep only the leading digits (drop /comments, #comment, etc.)
    slug="$url_owner/$url_repo"                     # look the issue up in the URL's repo
    [[ -n "$repo_override" ]] || repo="$url_repo"   # unless -r was given, target ~/src/<url_repo>
  fi
  if [[ ! "$issue" =~ ^[0-9]+$ ]]; then
    print -u2 "wti: expected an issue number or GitHub issue URL"
    return 1
  fi
  repo_path="${repo_path:-$HOME/src/$repo}"
  WT_REPO_PATH="$repo_path"
  worktree_root="${worktree_root:-$HOME/src/worktrees/$repo}"
  if [[ ! -d "$repo_path/.git" ]]; then
    print -u2 "wti: no main clone at $repo_path (needed to attach a worktree)"
    return 1
  fi
  if [[ -n "$name" ]] && { [[ ! "$name" =~ ^[a-zA-Z0-9][a-zA-Z0-9._-]*$ ]] || [[ "$name" == *..* ]]; }; then
    print -u2 "wti: --name must be a simple worktree name"; return 1
  fi
  if [[ -z "$slug" ]]; then
    slug=$(git -C "$repo_path" config --get remote.upstream.url 2>/dev/null \
        || git -C "$repo_path" config --get remote.origin.url) || return 1
    slug="${slug#https://github.com/}"
    slug="${slug#git@github.com:}"
    slug="${slug%.git}"
  fi
  local info
  info=$(gh -R "$slug" issue view "$issue" --json number,title \
    -q '[.number, .title] | @tsv') || return
  local number title title_slug branch dir base
  IFS=$'\t' read -r number title <<< "$info"
  if [[ -n "$requested" ]]; then
    branch="$requested"
  else
    title_slug=$(print -r -- "$title" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9]+/-/g; s/^-//; s/-$//; s/^(.{60}).*/\1/; s/-$//')
    branch="issue-$number${title_slug:+-$title_slug}"
  fi
  dir="$worktree_root/$branch"
  if [[ -e "$dir" || -L "$dir" ]]; then
    # Explicit dashboard names reserve new resources and never reuse a directory.
    if [[ -n "$name" ]]; then
      print -u2 "wti: explicit worktree destination already exists: $dir"; return 1
    fi
    _wt_session "$dir" "$branch" "$agent" "$prompt" "$repo"; return
  fi
  if [[ -n "$name" ]] && git -C "$repo_path" show-ref --verify --quiet "refs/heads/$branch"; then
    print -u2 "wti: explicit branch already exists: $branch"; return 1
  fi
  base=$(git -C "$repo_path" symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null)
  base="${base#origin/}"
  [[ -n "$base" ]] || base=main
  mkdir -p "$worktree_root" &&
  git -C "$repo_path" fetch origin "$base" &&
  if git -C "$repo_path" show-ref --verify --quiet "refs/heads/$branch"; then
    git -C "$repo_path" worktree add "$dir" "$branch"
  else
    git -C "$repo_path" worktree add -b "$branch" "$dir" "origin/$base"
  fi &&
  _wt_session "$dir" "$branch" "$agent" "$prompt" "$repo"
}
wri() { wti "$@"; }
wtissue() { wti "$@"; }

# usage: wtpr [--codex|--claude] [-r repo] <pr-number|pr-url>  (requires gh; handles PRs from forks)
wtpr() {
  local repo=galaxy repo_override= slug= agent=claude prompt= name= repo_path= worktree_root=
  local WT_NO_FOCUS=0 WT_REPO_PATH=
  while [[ "$1" == -* ]]; do
    case "$1" in
      --codex) agent=codex; shift ;;
      --claude) agent=claude; shift ;;
      --no-focus) WT_NO_FOCUS=1; shift ;;
      --name|--repo-path|--worktree-root|-r|-p|--prompt|-F|--prompt-file)
        [[ $# -ge 2 && -n "$2" ]] || { print -u2 "wtpr: $1 needs a value"; return 1; }
        case "$1" in
          --name) name="$2" ;;
          --repo-path) repo_path="$2" ;;
          --worktree-root) worktree_root="$2" ;;
          -r) repo="$2"; repo_override=1 ;;
          -p|--prompt) prompt="$2" ;;
          -F|--prompt-file) prompt="$(_wt_read_prompt_file "$2")" || return 1 ;;
        esac
        shift 2 ;;
      -h|--help) _wtpr_help; return 0 ;;
      *) print -u2 "wtpr: unknown option: $1"; _wtpr_help >&2; return 1 ;;
    esac
  done
  local pr="$1"
  if [[ "$pr" == https://github.com/*/pull/* ]]; then
    local rest="${pr#https://github.com/}"
    local url_owner="${rest%%/*}"; rest="${rest#*/}"
    local url_repo="${rest%%/*}"
    pr="${pr##*/pull/}"
    pr="${pr%%[!0-9]*}"
    slug="$url_owner/$url_repo"
    [[ -n "$repo_override" ]] || repo="$url_repo"
  fi
  if [[ ! "$pr" =~ ^[0-9]+$ ]]; then
    print -u2 "wtpr: expected a PR number or GitHub PR URL"; return 1
  fi
  repo_path="${repo_path:-$HOME/src/$repo}"
  WT_REPO_PATH="$repo_path"
  worktree_root="${worktree_root:-$HOME/src/worktrees/$repo}"
  if [[ ! -d "$repo_path/.git" ]]; then
    print -u2 "wtpr: no main clone at $repo_path"; return 1
  fi
  if [[ -n "$name" ]] && { [[ ! "$name" =~ ^[a-zA-Z0-9][a-zA-Z0-9._-]*$ ]] || [[ "$name" == *..* ]]; }; then
    print -u2 "wtpr: --name must be a simple worktree name"; return 1
  fi
  if [[ -z "$slug" ]]; then
    slug=$(git -C "$repo_path" config --get remote.upstream.url 2>/dev/null \
        || git -C "$repo_path" config --get remote.origin.url) || return 1
    slug="${slug#https://github.com/}"
    slug="${slug#git@github.com:}"
    slug="${slug%.git}"
  fi
  local info
  info=$(gh -R "$slug" pr view "$pr" --json headRefName,headRepository,headRepositoryOwner \
    -q '[.headRepositoryOwner.login, .headRepository.name, .headRefName] | @tsv') || return
  local owner rname branch local_branch dir remote existing_url
  IFS=$'\t' read -r owner rname branch <<< "$info"
  if [[ ! "$owner/$rname" =~ ^[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+$ ]] || \
     ! git check-ref-format "refs/heads/$branch"; then
    print -u2 "wtpr: invalid PR head repository or branch"; return 1
  fi
  local_branch="${name:-$branch}"
  dir="$worktree_root/$local_branch"
  if [[ -e "$dir" || -L "$dir" ]]; then
    # Explicit dashboard names reserve new resources. Terminal reuse still verifies
    # Git provenance before opening and never submits a task to an existing agent.
    if [[ -n "$name" ]]; then
      print -u2 "wtpr: explicit worktree destination already exists: $dir"; return 1
    fi
    local common expected current
    common=$(git -C "$dir" rev-parse --path-format=absolute --git-common-dir) || return 1
    expected=$(git -C "$repo_path" rev-parse --path-format=absolute --git-common-dir) || return 1
    current=$(git -C "$dir" symbolic-ref --short HEAD) || return 1
    if [[ "$common" != "$expected" || "$current" != "$branch" ]]; then
      print -u2 "wtpr: existing directory is not the requested checkout"; return 1
    fi
    _wt_session "$dir" "$local_branch" "$agent" "$prompt" "$repo"; return
  fi
  if git -C "$repo_path" show-ref --verify --quiet "refs/heads/$local_branch"; then
    if [[ -n "$name" ]]; then
      print -u2 "wtpr: explicit branch already exists: $local_branch"; return 1
    fi
    local_branch="pr-${slug%%/*}-$pr"
    local n=1 base_branch="$local_branch"
    while git -C "$repo_path" show-ref --verify --quiet "refs/heads/$local_branch" || \
          [[ -e "$worktree_root/$local_branch" || -L "$worktree_root/$local_branch" ]]; do
      (( n++ )); local_branch="$base_branch-$n"
    done
    dir="$worktree_root/$local_branch"
  fi
  remote="wtpr-$owner-$rname"
  existing_url=$(git -C "$repo_path" config --get "remote.$remote.url")
  if [[ -n "$existing_url" && "$existing_url" != "https://github.com/$owner/$rname.git" ]]; then
    print -u2 "wtpr: PR remote name belongs to another repository"; return 1
  fi
  if [[ -z "$existing_url" ]]; then
    git -C "$repo_path" remote add "$remote" "https://github.com/$owner/$rname.git" || return 1
  fi
  mkdir -p "$worktree_root" &&
  git -C "$repo_path" fetch "$remote" "refs/heads/${branch}:refs/heads/$local_branch" &&
  git -C "$repo_path" worktree add "$dir" "$local_branch" &&
  git -C "$dir" config "branch.$local_branch.remote" "$remote" &&
  git -C "$dir" config "branch.$local_branch.merge" "refs/heads/$branch" &&
  _wt_session "$dir" "$local_branch" "$agent" "$prompt" "$repo"
}
