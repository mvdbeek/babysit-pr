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

## Updates from the dashboard

The dashboard serves the extension it ships: **Download the extension** on its pairing
page (`/extension.html`) downloads `/extension.zip`, built from the checkout's
`extension/` folder, under a `babysitter-extension/` folder. Every reply to the
extension names that version (`X-Babysit-Extension-Version`), so when the dashboard
ships a newer one, the extension's Settings, its composer and its GitHub card say so
and link to the download. Chrome does not install extensions from other sites, and
Manifest V3 runs no code fetched from a server, so an update is still yours to load:

- **Downloaded:** unzip the new download over the old `babysitter-extension` folder
  (`unzip -o ~/Downloads/babysitter-extension-*.zip -d ~`, if that is where it lives)
  and click **Reload extension** in its Settings, or **Reload** on its
  `chrome://extensions` card.
- **Loaded from the dashboard's checkout** (the browser and the dashboard on one
  machine): after the checkout updates, click **Reload extension**.

The manifest's `key` fixes the extension's ID (`ljkolpgamhbplbfdeominlmhjandgfie`),
wherever its folder is, so updates and a moved folder keep its pairing. The pairing
page fills that ID in. Version 0.6 is the first with the key: an older install had an
ID from its folder's path and needs pairing once more. No new browser permissions are
required unless you turn on GitHub pages (below).

## On GitHub pull requests and issues

Version 0.4 shows what Babysitter knows on GitHub's pull request and issue lists and
pages; version 0.5 adds **Handle issue** to issue pages and **Ask about this pull request**
to pull request pages. Turn on **Show Babysitter on GitHub** in the extension's Settings and accept the request for github.com access, then
reload open GitHub tabs. It needs a paired dashboard.

- **List pages** (`/<owner>/<repo>/pulls`, `/issues`, and `/pulls`): beside a title,
  a chip with the pull request's newest agent review, its verdict and findings by
  severity (`Review: changes needed · 1 Major · 4 Minor`), linking to the report; how
  many checkouts are linked to the item; and **Open workspace** or **Open in
  Babysitter**. A review of a head the pull request has moved on from reads
  `Review (old head)` in grey. Hover the chips for the finding titles and the checkouts.
- **An item's page**: a Babysitter section at the top of the sidebar (a card in the
  corner on tabs without one, such as Files changed). For a pull request: the review's
  kind (sweep or pr-review), run or round, date and reviewed commit, a warning when the
  pull request has commits since, up to eight finding titles, the verdict sentence or
  risk verdict, and links to the report and draft reply. For every item: its linked
  checkouts, on this machine and on workers, each with its machine and agent status, a
  link that opens its conversation in the dashboard and one to Collie. The card always
  shows, and says when nothing is known: no review, no linked workspace, not in the
  dashboard's overview.
- **Open workspace** appears when the pull request has a checkout with a herdr
  workspace. It opens the dashboard's PR list at that row and then the workspace's
  conversation. **Open in Babysitter** opens the row alone, with its workspace actions.
  A checkout's own link opens the Workspaces tab with that checkout's conversation.

- **Handle issue** (an issue's card): a box for extra instructions, an agent choice and
  a **Handle issue** button. It starts the dashboard's issue task (Settings → task prompts,
  **Handle issue**) for this issue, with the box's text appended under "Additional
  instructions". An issue in the dashboard's overview gets its Handle action, as the
  dashboard's own button does: in its preferred clone, or cloned first when there is none.
  Any other issue starts a new task on branch `issue-<number>` in a clone of its
  repository on the dashboard's machine; with none, it says so. **Usual agent** uses the
  agent, model, effort, account and clone the composer last used for this repository
  (Codex if it never did); picking the other agent keeps only the clone. The card follows
  the launch and links to the agent in Collie. Cmd/Ctrl+Enter in the box submits.
- **Review pull request** (a pull request's card, version 0.7): the same box, agent
  choice and button, starting the dashboard's **Review pull request** task (Settings →
  task prompts) for this pull request, as the dashboard's own Review pull request button
  does. A pull request in the overview gets its Handle action; any other starts a new
  task on branch `review-pr-<number>` in a clone of its repository on the dashboard's
  machine. **Main reviewer** is the agent Settings names as the main reviewer; picking an
  agent overrides it, with the composer's model, effort and account for this repository
  when it is the agent the composer last used. The clone is the composer's for this
  repository either way.
- **Ask about this pull request** (a pull request's card): type a question and press
  Enter (Shift+Enter for a new line). The answer streams into the card as Claude writes
  it, with what it is reading; **Stop** cancels it. Follow-up questions carry the
  conversation so far (up to six exchanges); **Clear** starts over. The conversation lives
  in the tab: reloading the page drops it.

Questions are answered by the watcher's supervisor, as the voice assistant's are, since
it can use the Claude login the launchd dashboard cannot: the dashboard queues each
question as a private file under `pr-questions/` in its state directory, and the
extension polls it. Without a running supervisor, asking says so. Claude (`opus`) gets the
pull request's metadata, description, comments, reviews, inline review comments and diff
(up to 150,000 characters), read with the dashboard's GitHub login, labelled untrusted.
When the dashboard's machine has a clone of the repository, the pull request's head
commit is unpacked from it (`git archive`, after fetching `refs/pull/<n>/head` into the
clone's object store if it is missing; no ref, branch, worktree or `FETCH_HEAD` changes)
into `pr-questions/checkouts/<sha>`, reused by follow-ups and removed two hours after its
last use. Claude runs there with `--restricted` and only the Read, Grep and Glob tools,
which cannot reach outside that folder; without a clone it has no tools and answers from
the diff. It cannot run code, change files or act on GitHub. At most three questions are
answered at once; one takes at most ten minutes. Only the extension that asked can read
an answer, and answers are deleted 30 minutes after they finish. The answer is rendered as
text (paragraphs, lists, code), never markup, and like the rest of the card GitHub's own
scripts can read it.

Reviews come from the dashboard's store of agent reviews (`GET /api/agent-reviews`):
the [review sweep](review-sweep.md)'s, which it records as each review finishes, and
pr-review rounds, imported from the folders the pr-review skill writes,
`pr-review-<owner>-<repo>-<number>`. The dashboard reads its own machine's folders from
disk: `~/src/agent-artefacts/outbox`, each sandbox's
`~/.cache/babysit-sandbox/<key>/artifacts` and `~/.local/state/pr-review`. Workers'
folders are found on the artefacts share, when the [artefacts experiment](artefacts.md)
is configured and dufs runs with `--allow-search` (or `--allow-all`): one search for
`pr-review-` per minute finds them all. Report links need the share; a report outside the
shared folder shows its summary without a link. Linked checkouts are the Workspaces tab's
rows; without the workspace overview, this machine's checkouts verified for the pull
request.

The page asks the extension's background for the items it shows, and the background asks
the dashboard with the pairing token; the page never sees the token. Only this
extension's script on a github.com page is answered. The summary is shown as text, never
markup. It is added to GitHub's page, so GitHub's own scripts can read it like the rest
of the page. Turning the setting off unregisters the script; the github.com permission
stays granted until you remove it on the extension's details page.

### Open in the installed app

The dashboard's web app manifest focuses an already open Babysitter window
(`launch_handler` `focus-existing`) and routes the link there without a reload. For
Chrome to open dashboard links in the installed app at all, install the dashboard as an
app and turn on **Open supported links** in its settings (`chrome://app-settings`, or
the app window's menu → **App info** → **Settings**). Otherwise the link opens in a
browser tab.

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

2. Download the extension from the dashboard's pairing page and unzip it (see
   [Updates from the dashboard](#updates-from-the-dashboard)), or use this checkout's
   `extension/` directory. Open `chrome://extensions`, enable **Developer mode**,
   choose **Load unpacked**, and select that folder. Pin **Send to Babysitter**
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
the pairing page disables that extension immediately. The extension's ID is fixed,
so every browser profile that loads it shares one pairing slot on a dashboard:
pairing one profile invalidates another's token.

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
close or the browser session ends; a draft whose composer never finished loading is
removed on a later tab close once it is 10 minutes old. The handoff puts its prefill
in a URL fragment that the dashboard consumes immediately; it is not sent in the HTTP
request. Any website can link to such a fragment, so the dialog says the task was
prefilled from a link and starts nothing until you submit it.

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
pages use bundled scripts only, with no remotely hosted code. The only content
script runs on github.com, and only while GitHub pages are turned on in Settings.
