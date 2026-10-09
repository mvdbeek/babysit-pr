async function openComposer(source) {
  const id = crypto.randomUUID();
  await chrome.storage.session.set({ [id]: { source } });
  const tab = await chrome.tabs.create({ url: chrome.runtime.getURL(`compose.html#${id}`) });
  await chrome.storage.session.set({ [id]: { source, tabId: tab.id } });
}
chrome.runtime.onInstalled.addListener(async () => {
  await chrome.storage.local.setAccessLevel({ accessLevel: "TRUSTED_CONTEXTS" });
  await chrome.contextMenus.removeAll();
  chrome.contextMenus.create({
    id: "send-task",
    title: "Send to babysit-pr",
    contexts: ["page", "selection", "link"],
    documentUrlPatterns: ["http://*/*", "https://*/*"],
  });
});
chrome.contextMenus.onClicked.addListener((info, tab) => {
  if (info.menuItemId !== "send-task") return;
  void openComposer({
    url: info.linkUrl || info.pageUrl || tab?.url || "",
    title: tab?.title || "",
    selection: (info.selectionText || "").slice(0, 24000),
  });
});
chrome.action.onClicked.addListener(async (tab) => {
  let selection = "";
  if (/^https?:/.test(tab.url || "")) {
    try {
      const results = await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        func: () => window.getSelection()?.toString().slice(0, 24000) || "",
      });
      selection = results[0]?.result || "";
    } catch {
      // Some browser pages prohibit scripts. The user can still write a task.
    }
  }
  await openComposer(
    /^https?:/.test(tab.url || "") ? { url: tab.url, title: tab.title || "", selection } : {},
  );
});
chrome.tabs.onRemoved.addListener(async () => {
  // Drafts contain selected page text; retain them only while their composer tab exists.
  const tabs = await chrome.tabs.query({ url: chrome.runtime.getURL("compose.html*") });
  const keep = new Set(tabs.map((tab) => new URL(tab.url).hash.slice(1)));
  const drafts = await chrome.storage.session.get(null);
  await chrome.storage.session.remove(
    Object.keys(drafts).filter((id) => drafts[id].tabId && !keep.has(id)),
  );
});
