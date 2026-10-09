import { dashboardURL } from "./shared.js";
const $ = (id) => document.getElementById(id);
$("extension-id").textContent = chrome.runtime.id;
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
    $("status").textContent = "Saved. Return to your task and click Reload choices.";
  } catch (error) {
    $("status").textContent = error.message;
  }
};
