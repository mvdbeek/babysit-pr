# Send to Babysitter browser extension

The Chromium Manifest V3 extension in `extension/` captures a page URL, its title,
and selected text when you click its toolbar button or choose **Send to babysit-pr**
from a page, link, or selection's context menu. It opens a task composer in a new
tab so drafts and delivery results remain available while you browse.

## Quick task flow

Version 0.2 opens on a suggested task and destination. The usual flow is to review
or edit the instructions and press **Start task** or **Send follow-up**. Open
**Change destination and settings** only to override the suggestion.

- Repository inference works on GitHub repository, file, PR, issue, and Actions
  pages. On other pages, it can use a unique known repository named in your task,
  selection, page title, or captured GitHub links. Otherwise it reuses your previous
  choice for that site, your last repository, or the only available repository.
  A GitHub page for an unavailable repository does not fall back to an unrelated
  remembered repository.
- The composer remembers the clone and agent/model/effort/account choices from
  successful launches, per repository and dashboard connection. Blank effort keeps
  the dashboard's repository/global default; the picker shows that default.
- New branches get a readable name from the task plus a unique draft suffix. You
  can still enter a branch or base explicitly under **Change**.
- PRs with failing checks suggest a CI repair; issues suggest implementation;
  outstanding review feedback suggests handling it; file pages suggest inspection;
  selected text suggests investigation. **Fix the problem**, **Fix CI**, **Handle
  feedback**, and **Explain** are editable prompt shortcuts. These suggestions use
  page type and current dashboard metadata, not an LLM call.
- A single running conversation associated with this exact PR/issue or Collie
  workspace is selected as a follow-up. With several matches, the conversation
  picker opens for a choice. Other agents working in the repository appear as
  **Continue …** suggestions; a repository match alone does not redirect a task.
- The extension includes a bounded excerpt of the page's main/article content and
  GitHub links alongside the selection. Inspect or remove it in **Page context**.
  Right-clicking a link does not label the containing page as the linked page's
  content. Collection happens only when you invoke the extension, and delivery
  still requires pressing Send/Start.

Choices refresh when you return to the composer or save the connection settings.
Typed instructions and explicit choices survive refreshes. Uncertain delivery
freezes the submitted request for retries even if the available defaults change.

To update an unpacked installation, update the dashboard backend, replace the
extension files in the **same directory**, click **Reload** on its
`chrome://extensions` card, and open a fresh composer. Keeping the directory
preserves its extension ID, connection and pairing. No new browser permissions
are required. If you load a different directory, pair the new ID.

## Install and connect

1. Run the dashboard from this revision. For an isolated manual smoke test, start
   a second dashboard with its own state and port:

   ```sh
   uv run --locked python scripts/pr_supervisor.py \
     --home "$HOME/.local/state/babysit-pr-extension-test" dashboard --port 8766
   ```

   This does not start a watcher. Workspace discovery still uses local clones;
   pressing **Start task** launches a real agent. Leave the monitoring checkbox
   off for this isolated smoke test. The existing watcher and its state are not
   changed. For normal use, run the updated dashboard with your usual shared
   state directory. The extension never needs its own watcher process.

2. Open `chrome://extensions`, enable **Developer mode**, choose **Load unpacked**,
   and select this checkout's `extension/` directory. Pin **Send to Babysitter**
   if you want its toolbar button. The same files target Chromium browsers; the
   automated browser tests run in Chromium. Firefox and Safari packaging is not
   included.
3. Open the extension's **Options** (or **Settings** in its task composer). Set
   the dashboard URL, e.g. `http://127.0.0.1:8766` for the smoke test, or your HTTPS
   Tailscale dashboard origin. URL prefixes, query strings, and credentials in the
   URL are not supported. On another laptop, use the Tailscale URL, not localhost.
   The dashboard still needs its normal `--allow-host` configuration for that URL.
4. Click **Open dashboard pairing**, then **Create pairing token**. Copy the token
   back into the extension settings and click **Save connection**. Accept the
   browser's permission request for that dashboard host.
5. Return to the composer. The inferred destination should appear automatically;
   **Reload choices** is also available.

No GitHub token goes into the extension. GitHub access and agent execution use
the dashboard host's existing configuration. Pairing tokens are saved in browser
local storage, restricted to extension contexts, never Chrome Sync. On the server,
only their hashes are saved in the private `browser-extension.sqlite` file under
the chosen state directory. Pairing again rotates the token; **Revoke access** on
the pairing page disables that extension immediately. Different browser profiles
using the same unpacked extension ID share one pairing slot; re-pairing invalidates
the other profile's token.

## Try it

- **New task:** select some text on a web page, right-click, and choose **Send to
  babysit-pr**. Check the inferred repository and write a small task such
  as “Read the README and summarize the development commands; do not edit files.”
  Press **Start task**. The status should progress to completion of the launch and
  offer **Open in Collie**. This means the agent was launched, not that it finished
  its work. Closing the composer does not cancel an accepted task.
- **GitHub task:** open an open PR or issue already in the dashboard overview,
  including a PR's Files changed page. The composer selects the target and its
  repository, or the matching existing conversation. Use **Change** to choose a
  separate **GitHub PR or issue** task; this reuses **Handle** and creates a separate
  workspace. Targets outside the overview use their URL as reference for a new
  task until discovery includes them. Direct
  launches require an existing local clone; use the dashboard's full workspace
  controls for cloning or scheduling.
- **Follow-up:** when the page matches a conversation, review the suggested
  destination and send. You can also use **Change → Existing agent** to pick a
  conversation. The server rechecks its session before delivery. Agents waiting
  on a question or approval must be answered in the dashboard/Collie first.
- **Monitoring:** in normal use with the shared watcher setup, check **Babysit the
  PR after completing this task**. This adds explicit monitoring instructions for
  the launched agent to follow via the babysit-pr skill; it does not itself register
  a watch or guarantee the agent completed the handoff. Verify the watch in the
  dashboard. Feedback still needs dashboard approval; this checkbox does not grant
  permission to merge, post reviews, or create a PR when none exists.
- **Dashboard handoff:** leave the token empty in settings, write a task, and choose
  **Open in dashboard**. This opens the existing **New task** dialog with instructions,
  repository suggestion, branch and base prefilled. Review repository and agent
  settings, then submit there. It does not launch automatically; GitHub references
  remain task context in this mode. Follow-ups use direct submission only.
- **Revocation:** revoke the token on the pairing page, then click **Reload choices**
  in the composer. Direct access should be refused. Same-origin dashboard controls
  continue to work.

The composer includes at most 24,000 characters of selected text; edit or remove
the page reference before sending if needed. Instructions plus context must fit
within the existing 32,000-character task limit. Page content is labeled untrusted.
Captured drafts stay in session storage and are removed when their composer tabs
close or the browser session ends. The handoff puts its prefill in a URL fragment
that the dashboard consumes immediately; it is not sent in the HTTP request.

If submission times out, retry the **unchanged** task. Its saved request ID prevents
duplicate delivery, including across server restarts and composer reloads. An
interrupted server-side delivery is marked uncertain and is never automatically
repeated. Check the dashboard/Collie before editing and submitting a replacement.
After successful submission, open another composer to send another task.

## Development checks

```sh
uv run --locked tox -e unit,browser -- tests/test_browser_extension.py
uv run --locked tox -e frontend
uv run --locked tox
```

Browser tests load an actual unpacked extension in a temporary Chromium profile
and pair it against a temporary dashboard with fake launchers. Only that test copy
pregrants loopback host access; the shipped extension requests optional access in
Settings. Tests exercise launch, GitHub targeting, follow-up, reload deduplication,
revocation, and token-free dashboard handoff. No test uses the live watcher.

After editing the extension, click **Reload** on its `chrome://extensions` card and
open a fresh composer. Restart the test dashboard after Python changes. Extension
pages use bundled scripts only, with no remotely hosted code or content scripts
running persistently on websites.
