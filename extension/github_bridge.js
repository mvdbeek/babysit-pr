import { api, dashboardURL, GITHUB_ORIGINS } from "./shared.js";

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

const ITEM = /^https:\/\/github\.com\/[^/]+\/[^/]+\/(pull|issues)\/\d+$/;
async function githubPages(urls) {
  const wanted = (Array.isArray(urls) ? urls : [])
    .filter((url) => typeof url === "string" && ITEM.test(url))
    .slice(0, 100);
  if (!wanted.length) return { pages: {} };
  const { config } = await chrome.storage.local.get("config");
  if (!config?.url) throw Error("Set the dashboard URL in the extension's Settings.");
  const origin = dashboardURL(config.url);
  const { pages } = await api({ action: "pages", urls: wanted });
  // The dashboard answers its own links as paths; only those become links to it.
  const absolute = (path) => (path?.startsWith("/?") ? origin + path : null);
  for (const page of Object.values(pages || {})) {
    page.dashboard = absolute(page.dashboard);
    for (const workspace of page.workspaces || [])
      workspace.dashboard = absolute(workspace.dashboard);
  }
  return { pages: pages || {} };
}
// Only this extension's own script on a github.com page may ask; it never sees the token.
chrome.runtime.onMessage.addListener((message, sender, reply) => {
  if (
    message?.type !== "github-pages" ||
    sender.id !== chrome.runtime.id ||
    !sender.url?.startsWith("https://github.com/")
  )
    return false;
  githubPages(message.urls).then(reply, (error) => reply({ error: error.message }));
  return true;
});
