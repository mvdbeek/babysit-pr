# Send to Babysitter browser extension

The Chromium Manifest V3 extension in `extension/` captures a page URL, its title,
and selected text when you click its toolbar button or choose **Send to babysit-pr**
from a page, link, or selection's context menu. It opens a task composer in a new
tab so drafts and delivery results remain available while you browse.

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
5. Return to the composer and click **Reload choices**. You should see your local
   clones and the dashboard's current agent/model/effort choices.

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
  babysit-pr**. Choose a repository, name a new branch, and write a small task such
  as “Read the README and summarize the development commands; do not edit files.”
  Press **Start task**. The status should progress to completion of the launch and
  offer **Open in Collie**. This means the agent was launched, not that it finished
  its work. Closing the composer does not cancel an accepted task.
- **GitHub task:** open an open PR or issue already in the dashboard overview,
  including a PR's Files changed page. The composer selects the target and its
  repository. Supply instructions and start it; this reuses **Handle** and creates
  a separate workspace. Targets outside the overview are identified as unavailable;
  reload after discovery, or use their URL as reference for a new task. Direct
  launches require an existing local clone; use the dashboard's full workspace
  controls for cloning or scheduling.
- **Follow-up:** choose **Existing agent**, select the intended conversation, and
  send a message. The server rechecks its session before delivery. Agents waiting
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
