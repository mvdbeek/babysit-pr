import { api, availableUpdate, dashboardURL, GITHUB_ORIGINS } from "./shared.js";
const $ = (id) => document.getElementById(id);
$("extension-id").textContent = chrome.runtime.id;
$("version").textContent = chrome.runtime.getManifest().version;
$("reload-extension").onclick = () => chrome.runtime.reload();
async function showUpdate() {
  const update = await availableUpdate();
  $("update").hidden = !update;
  if (!update) return;
  $("update-version").textContent = update.version;
  $("update-link").href = update.download;
}
// Asking the dashboard records the version it ships; a page left open notices later replies.
chrome.storage.onChanged.addListener((changes, area) => {
  if (area === "local" && (changes.shipped || changes.config)) void showUpdate();
});
void showUpdate();
void api({ action: "version" }).catch(() => {});
const { config } = await chrome.storage.local.get("config");
$("url").value = config?.url || "http://127.0.0.1:8765";
$("token").value = config?.token || "";
$("url").oninput = () => {
  // Never send an existing server's credential to a newly configured server.
  $("token").value = "";
};
$("pair").onclick = async () => {
  try {
    const url = dashboardURL($("url").value);
    await chrome.tabs.create({ url: `${url}/extension.html#id=${chrome.runtime.id}` });
  } catch (error) {
    $("status").textContent = error.message;
  }
};
$("settings").onsubmit = async (event) => {
  event.preventDefault();
  try {
    const url = dashboardURL($("url").value);
    const host = new URL(url);
    const granted = await chrome.permissions.request({
      origins: [`${host.protocol}//${host.hostname}/*`],
    });
    if (!granted) throw Error("Dashboard access was not granted.");
    await chrome.storage.local.setAccessLevel({ accessLevel: "TRUSTED_CONTEXTS" });
    await chrome.storage.local.set({ config: { url, token: $("token").value.trim() } });
    $("status").textContent = "Saved. Return to your task; its choices will refresh automatically.";
  } catch (error) {
    $("status").textContent = error.message;
  }
};
const { github } = await chrome.storage.local.get("github");
$("github").checked =
  Boolean(github) && (await chrome.permissions.contains({ origins: GITHUB_ORIGINS }));
$("github").onchange = async () => {
  const on = $("github").checked;
  try {
    // Asked first, while the click still counts as a user gesture.
    if (on && !(await chrome.permissions.request({ origins: GITHUB_ORIGINS })))
      throw Error("GitHub access was not granted.");
    await chrome.storage.local.set({ github: on });
    $("github-status").textContent = on
      ? "On. Reload open GitHub tabs to see it there."
      : "Off. GitHub pages no longer ask the dashboard.";
  } catch (error) {
    $("github").checked = false;
    $("github-status").textContent = error.message;
  }
};
