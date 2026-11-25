import { api, availableUpdate, dashboardURL, GITHUB_ORIGINS } from "./shared.js";

// GitHub pages: the content script is registered only while Settings turns it on and
// github.com access is granted, and checked again each time this worker starts.
async function syncGitHub() {
  const { github } = await chrome.storage.local.get("github");
  const allowed =
    Boolean(github) && (await chrome.permissions.contains({ origins: GITHUB_ORIGINS }));
  const registered = await chrome.scripting.getRegisteredContentScripts({ ids: ["github"] });
  if (allowed && !registered.length)
    await chrome.scripting.registerContentScripts([
      { id: "github", matches: GITHUB_ORIGINS, js: ["github.js"], css: ["github.css"] },
    ]);
  if (!allowed && registered.length)
    await chrome.scripting.unregisterContentScripts({ ids: ["github"] });
}
chrome.storage.onChanged.addListener((changes, area) => {
  if (area === "local" && changes.github) void syncGitHub();
});
chrome.permissions.onAdded.addListener(() => void syncGitHub());
chrome.permissions.onRemoved.addListener(() => void syncGitHub());
void syncGitHub();

const ITEM = /^https:\/\/github\.com\/([^/]+\/[^/]+)\/(pull|issues)\/\d+$/;
async function connection() {
  const { config } = await chrome.storage.local.get("config");
  if (!config?.url) throw Error("Set the dashboard URL in the extension's Settings.");
  return dashboardURL(config.url);
}
async function githubPages(urls) {
  const wanted = (Array.isArray(urls) ? urls : [])
    .filter((url) => typeof url === "string" && ITEM.test(url))
    .slice(0, 100);
  if (!wanted.length) return { pages: {} };
  const origin = await connection();
  const { pages } = await api({ action: "pages", urls: wanted });
  // The dashboard answers its own links as paths; only those become links to it.
  const absolute = (path) => (path?.startsWith("/?") ? origin + path : null);
  for (const page of Object.values(pages || {})) {
    page.dashboard = absolute(page.dashboard);
    for (const workspace of page.workspaces || [])
      workspace.dashboard = absolute(workspace.dashboard);
  }
  return { pages: pages || {}, update: await availableUpdate() };
}
function item(url, kind) {
  const match = typeof url === "string" && ITEM.exec(url);
  if (!match || match[2] !== kind) throw Error("Not a GitHub item page");
  return match[1].toLowerCase();
}
function text(value, limit) {
  return typeof value === "string" ? value.slice(0, limit) : "";
}
// Handle issue starts with the clone and agent settings the composer last used for this
// repository, unless the page picks another agent. Review pull request keeps only the
// clone unless the page picks the remembered agent: the dashboard's main reviewer
// otherwise.
async function githubLaunch(message, kind) {
  const repo = item(message.url, kind);
  const key = `preferences:${await connection()}`;
  const preferences = (await chrome.storage.local.get(key))[key] || {};
  const remembered = preferences.repos?.[repo] || {};
  const picked = ["codex", "claude"].includes(message.agent) ? message.agent : null;
  const agent = picked || (kind === "issues" ? remembered.agent : null);
  const same = agent && agent === remembered.agent;
  const request = {
    action: kind === "issues" ? "handle-issue" : "review-pr",
    request_id: crypto.randomUUID(),
    url: message.url,
    instructions: text(message.instructions, 8000),
  };
  if (remembered.clone && remembered.repo?.toLowerCase() === repo) request.clone = remembered.clone;
  if (agent) request.agent = agent;
  if (same && remembered.model) request.model = remembered.model;
  if (same && remembered.effort) request.effort = remembered.effort;
  if (agent === "claude" && same && remembered.account) request.claude_account = remembered.account;
  return api(request);
}
function questionId(value) {
  if (typeof value !== "string" || !/^[0-9a-f]{32}$/.test(value)) throw Error("Unknown question");
  return value;
}
// Each message builds its own request: a page never passes fields through to the dashboard.
const HANDLERS = {
  "github-pages": (message) => githubPages(message.urls),
  "github-handle": (message) => githubLaunch(message, "issues"),
  "github-review": (message) => githubLaunch(message, "pull"),
  "github-status": (message) =>
    api({ action: "status", id: text(message.id, 100) }).then(({ operation }) => ({ operation })),
  "github-ask": (message) => {
    item(message.url, "pull");
    const history = (Array.isArray(message.history) ? message.history : [])
      .slice(-6)
      .map((turn) => ({ question: text(turn?.question, 6000), answer: text(turn?.answer, 6000) }));
    return api({
      action: "ask",
      url: message.url,
      question: text(message.question, 4000),
      history,
    });
  },
  "github-answer": (message) => api({ action: "answer", id: questionId(message.id) }),
  "github-ask-cancel": (message) => api({ action: "ask-cancel", id: questionId(message.id) }),
};
// Only this extension's own script on a github.com page may ask; it never sees the token.
chrome.runtime.onMessage.addListener((message, sender, reply) => {
  const handler = Object.hasOwn(HANDLERS, message?.type) && HANDLERS[message.type];
  if (!handler || sender.id !== chrome.runtime.id || !sender.url?.startsWith("https://github.com/"))
    return false;
  Promise.resolve()
    .then(() => handler(message))
    .then(reply, (error) => reply({ error: error.message }));
  return true;
});
