async function openComposer(source) {
  const id = crypto.randomUUID();
  await chrome.storage.session.set({ [id]: { source } });
  await chrome.tabs.create({ url: chrome.runtime.getURL(`compose.html#${id}`) });
}

async function capture(tab) {
  if (!/^https?:/.test(tab?.url || "")) return {};
  try {
    const results = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: () => {
        const selection = window.getSelection()?.toString().slice(0, 24000) || "";
        const root = document.querySelector("main, article, [role=main]");
        const content = (root?.innerText || "")
          .slice(0, Math.max(0, 18000 - selection.length))
          .slice(0, 12000);
        const links = [
          ...new Set(
            [...(root || document).querySelectorAll("a[href]")]
              .map((a) => a.href)
              .filter((url) => url.startsWith("https://github.com/")),
          ),
        ]
          .slice(0, 12)
          .join("\n")
          .slice(0, 4000);
        return { selection, content, links };
      },
    });
    return results[0]?.result || {};
  } catch {
    return {};
  }
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
chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  if (info.menuItemId !== "send-task") return;
  const captured = await capture(tab);
  const linked = info.linkUrl && info.linkUrl !== (info.pageUrl || tab?.url);
  await openComposer({
    ...captured,
    url: info.linkUrl || info.pageUrl || tab?.url || "",
    title: linked ? "" : tab?.title || "",
    selection: (info.selectionText || "").slice(0, 24000),
    // The containing page is not the linked issue's body.
    ...(linked ? { content: "", links: "" } : {}),
  });
});
chrome.action.onClicked.addListener(async (tab) => {
  const captured = await capture(tab);
  await openComposer(
    /^https?:/.test(tab.url || "") ? { url: tab.url, title: tab.title || "", ...captured } : {},
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
