# Experimental dufs integration: a workspace's artefacts

This opt-in internal module adds an **Artefacts** tab to the workspace viewer, beside Diff, Transcript and Terminal. It lists the reports, screenshots, patches and other files the workspace's agents left on a [dufs](https://github.com/sigoden/dufs) file share, each with its size, when it last changed, a link that opens it on the share, and the transcript entry that named it. It reads transcripts and the share; it writes nothing, to either. Its own API route, module and script keep its failures inside this experiment.

## Activation

Create `experiments/artefacts/config.json` under the dashboard's `--home` directory, then restart the dashboard:

```json
{
  "enabled": true,
  "url": "https://share.example.ts.net/outbox",
  "api": "http://127.0.0.1:8642/outbox",
  "root": "~/src/agent-artefacts/outbox"
}
```

- `url` is the shared directory as a browser reaches it: links lead there, and it is how agents write a link in a transcript. It may be the whole share or a directory below it; only what is below it is ever listed.
- `api` (optional, `url` by default) is where the dashboard itself reads the same directory, for a share that is fronted by a proxy the dashboard's host need not go through.
- `root` (optional) is that same directory on this machine's disk, absolute or under `~/`. With it, a path an agent on this machine wrote or copied to counts as well as a link. A `~/` path is recognised as typed, expanded, and resolved through links.

Both addresses must be `http(s)://host[:port][/path]` without credentials, a query or a fragment. A share that needs a login is not supported, and the dashboard asks the share directly, ignoring any proxy set in its environment. Configuration is read at dashboard startup; the module does no work when disabled, and a configuration it refuses shows its reason in the tab.

## What is listed

A workspace has no folder of its own on the share, so its artefacts are found by what its agents **named**. The tab searches the transcripts of every Claude and Codex session recorded in the checkout (the sessions the Transcript tab lists, through the same full-text index) for the share's address and, on this machine, for `root`. A name counts when it is in:

- an agent's reply, such as the link it hands over when it is done, or
- the input of a tool call, such as the path of a file it writes or a command that copies one.

A prompt you sent never counts, nor does a tool's output, nor the input of a tool that only reads (`Read`, `Grep`, `Glob`, `LS`, `WebFetch`, `WebSearch`): a file someone asked the agent to look at is not its work. This is evidence, not proof. An agent that writes about another task's report, runs `cat` on it, or names it in the brief of a sub-agent (a tool input) names it too.

Each name is then looked up on the share:

- A **file** is listed in its folder, which also says how many other files that folder holds and links to it.
- A **folder** is listed with all the files in it, since an agent that names only the folder (`cp shots/*.png …/task/`) made what is inside. A path ends at whatever a shell would expand, so a glob or a variable names the folder before it. Folders inside it are shown as folders, not opened, and a named folder that holds nothing is left out.
- A name the share does not hold is counted under **not on the share**: a file deleted since, one a worker has not pushed yet, or a path that was never a file.

Folders are ordered by their newest file, files newest first. Images up to 8 MiB show a thumbnail, loaded by the browser straight from the share; for that the page's content security policy allows images, and only images, from the origin of `url`. Markdown links open in dufs's viewer (`?view`), as dufs otherwise serves it as a download. **Named by …** opens the session's transcript around the entry that named the file, as a search hit does. **Refresh** reads the transcripts and the share again; the tab does not poll.

A path with a space in it is recognised in a link, where it is percent-encoded, but not as a bare path, which ends at the space.

## Worker machines

A worker's checkout is searched through its probe, as its transcript search is. Only links count there: a worker's paths are its own, and its sandboxes each see a different directory behind `~/src/agent-artefacts/outbox`. A worker's agents are found once they give the link their `artefact-url` prints in a reply (the helper's own output is a tool's output, which does not count), and once the files have reached the share.

## Limits and failure states

One answer reads at most the newest 1,000 transcript entries that mention the share (prompts and tool output mention it too, and are passed over) and keeps 200 distinct names, newest sessions first. It asks the share for at most 24 directory listings (`<directory>/?json`), each within 2 MiB and 10,000 entries and its body read within 5 seconds (a share that trickles its headers is only held to 5 seconds per read), for at most 12 seconds in all between requests, and shows 100 files per folder. One listing serves every name in a folder. When a limit cuts the answer short, the tab says which: a name that may lie past the end of a cut listing is reported as not looked up, never as missing. Sessions not read to their end within the search's 20 seconds are searched as far as they were read, and counted; refreshing reads on.

A path from a transcript only selects a directory below `url`: `..` segments, control characters and over-long names are dropped before anything is asked, names are percent-encoded into the request, and a redirect is refused rather than followed. Listing entries are type-checked: names holding a slash or a backslash are dropped, and a size or date that is not a plausible number is shown as unknown. In the page, names are text and only `http(s)` addresses become links or pictures.

Disabled, empty, partly read, and unreachable are separate messages. A folder the share will not list (an error status, a redirect, an answer that is too large or not JSON) costs only the names in it, and the tab names the folder. A share that does not answer at all, answers too slowly or breaks off is not asked again for that answer; what was read until then is shown with a note, and when nothing was, the tab shows the error. Nothing else in the viewer is affected.

## Disable or remove

Set `"enabled": false` (or remove its config file) and restart the dashboard. This hides the tab and takes the share's origin out of the content security policy.

To remove the code, delete `scripts/workspace_artefacts.py`, `assets/dashboard/artefacts.js`, the `.ws-artefact*` rules in `assets/dashboard/viewer.css`, `tests/test_workspace_artefacts.py`, `tests/test_artefacts_browser.py`, `tests/dufs_share.py`, the worker's artefacts test in `tests/test_remote_viewer.py`, the `artefacts` stub of the stand-in server in `tests/test_dashboard_http.py`, and this document. Remove the hooks in `scripts/dashboard.py` (construction, the `img-src` policy, one route, one asset), `assets/dashboard/index.html` (script, tab button) and `assets/dashboard/viewer.js` (the `artefacts` host, its mode in `resetViewer`, `openViewer`, `loadViewer` with its `artefacts.failed()` on an error, and `minimize`). `workspace_viewer.matching` and `remote_matching` stay: `search` and `remote_search` are built on them.

Validation uses recorded sessions in temporary agent stores, a stand-in share on a loopback port and, where the `dufs` binary is on `PATH`, dufs itself, so the stand-in is shown to answer as dufs does. Screenshots cover the desktop, dark and phone listing and the transcript entry a file was named in. Run all checks with `uv run --locked tox`.
