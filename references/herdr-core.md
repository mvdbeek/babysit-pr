# herdr-core terminals

babysit-pr can keep its agents' terminals in [herdr-core](https://github.com/herdrdev/herdr-core)
(`hc`) instead of herdr. Both are supported for now; herdr support goes once herdr-core
is confirmed working. One machine uses one of them at a time.

## Choosing the runtime

```sh
mkdir -p ~/.config/babysit-pr
echo hc > ~/.config/babysit-pr/terminals     # herdr, or no file, keeps herdr
```

Restart the dashboard and the watcher afterwards. `$BABYSIT_TERMINALS` overrides the file
for one process; a value that is neither `herdr` nor `hc` is an error, not a fallback. `wt` follows the same setting when `WT_MULTIPLEXER` is `auto`, and
`WT_MULTIPLEXER=hc` selects herdr-core for one launch.

The setting decides where new terminals start and which runtime the dashboard lists.
Agents still running in the other runtime are not listed or reached until you switch
back, and a watch bound to a pane there cannot launch its repairs; bind it again with
`bind-pane` once its agent runs in the current one.

Each worker needs `hc` too, added to the controller's core under the worker's
`machines.json` label:

```sh
hc machine add babysit@box --name <label>
hc machine ls            # state: connected
```

A worker needs no `terminals` file: its terminals are reached through the controller's
core, and a line the controller types into one of them sees `$HC_TERMINAL_ID` (on a
machine with a `worker.json`, that alone selects herdr-core).

## What a workspace is

herdr-core has terminals only, no workspaces, tabs or panes. babysit-pr maps its own ideas
onto them in `scripts/hc_panes.py`:

| babysit-pr      | herdr-core                                                                                                                                              |
| --------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------- |
| pane            | a terminal running the machine's shell, kept (`hc run --keep`), with no parent; its id is the address (`t7`), which is also `$HC_TERMINAL_ID` inside it |
| workspace       | the terminals labelled `babysit.checkout=<path>`, or started in that folder; its id is `hc-` plus a digest of the path                                  |
| workspace label | the `babysit.title` label, and the terminal's name (`hc attach <name>`)                                                                                 |
| agent           | a terminal whose program reports a status (OSC 7501), as Claude Code and Codex do                                                                       |
| agent status    | the agent's own report: idle, working, done, blocked; `error` shows as blocked                                                                          |
| session         | the `babysit.session` label a worker's sandbox broker sets; on the controller, Claude's per-process session record or the rollout a Codex has open      |

An agent is typed into the terminal's shell, so the terminal outlives it: a repair or a
resumed session is typed into the same shell later. Terminal addresses are never handed
out again, so a watch's binding cannot land on a different terminal.

To look at a terminal, `hc attach <address or name>` (Ctrl+\ detaches), the Herdr desktop
app, or the dashboard's Terminal tab. Collie shows herdr only, so herdr-core workspaces
have no Collie link.

## What differs from herdr

- **Status is the agent's word.** No screen is read to decide whether an agent is busy.
  A Claude or Codex that has not reported yet is listed by its program's name, with an
  unknown status; any other program that reports nothing is not an agent.
- **Messages are submitted by hc.** A message is pasted and submitted in one step. hc
  holds it while a person is typing in that terminal (until they send or clear their
  draft, at most a minute after their last key) and decides when the hold ends: it types
  only if the agent still reports the state it was in when asked (idle or working; one
  that reports `done` gets a ten second hold instead), so a message never
  lands in a dialog or in the shell an agent left behind. A message held past the
  dashboard's wait is taken back, not delivered later.
- **Exits.** Claude is asked with one submitted `/exit`; Codex still gets Ctrl-D. hc
  types the `/exit` only if a Claude seen idle is still idle; for one that reports
  `done`, hc has no such condition and the short hold below is the only guard. While a
  person is typing there the exit is held for about ten seconds, then given up. The
  composer is not read first: hc's screens are plain text, where a placeholder looks
  like typed input. So a draft nobody has touched for over a minute is not noticed:
  Claude would get it submitted with `/exit` behind it, and Codex would ignore the
  Ctrl-D.
- **Answers and the Terminal tab type whatever is there.** An answer to an agent's
  question, and a line typed in the Terminal tab, are meant for the dialog or program on
  screen, so hc is told to type them even while the agent reports a dialog, and with no
  condition on its state. If a person is typing in that terminal the line is held, for
  at most the dashboard's wait of 15 to 30 seconds; an agent that exits within that
  hold leaves the line to its shell.
- **Typing without submitting** works for single keys only (letters, digits, a few
  signs, arrows, Enter). The Terminal tab submits a typed line whole; a character hc
  has no key for (`"`, `$`, `~`, non-ASCII) cannot be typed one at a time. Free-text
  answers to an agent's question are submitted whole for the same reason.
- **The Terminal tab is monochrome**: hc reads screens as plain text.
- **No window focus.** The focus action is refused, and "copy command" gives
  `hc attach <terminal>` instead.
- **Sessions of hand-started agents on a worker** are unknown unless they run in
  babysit-bwrap, whose broker labels the terminal. On the controller they are read from
  the agent's own process.

## Sandbox

hc runs commands with your account's access, and anyone who reaches its socket can type
into unsandboxed terminals, exactly as with herdr's.

- **Linux (babysit-bwrap):** `~/.config/herdr-core`, `~/.local/state/herdr-core`,
  `~/.local/share/herdr-core` and `$XDG_RUNTIME_DIR/herdr-core` are never writable or
  grantable, and none is bound into the sandbox. The environment is cleared, so an agent
  there has no `$HC_TERMINAL_ID`; it registers watches through the broker as before.
- **macOS (Safehouse):** an agent that registers a watch or runs `wt` itself needs the
  same grant it has for herdr today: `hc` on `PATH`, `~/.local/state/herdr-core`
  (the socket and state) and `$HC_TERMINAL_ID` passed through. That grant lets it type
  into unsandboxed terminals, the residual risk the supervisor reference already
  describes for herdr.

## Registering a watch

Inside an hc terminal the pane is `$HC_TERMINAL_ID`:

```sh
python3 <skill-dir>/scripts/pr_supervisor.py register \
  --pr auto --cwd /absolute/worktree --session SESSION_UUID --pane "$HC_TERMINAL_ID" \
  --instructions '…'
```

`handoff WATCH_ID --pane "$HC_TERMINAL_ID"` and `bind-pane` take the same id. The agent
must have been started from the terminal's shell (as `wt` and the dashboard do): a
terminal started with a program of its own (`hc run claude`) ends when that program
exits, has no shell for a repair to be typed into, and is refused.
