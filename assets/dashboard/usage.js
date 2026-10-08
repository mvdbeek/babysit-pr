"use strict";
// Subscription quota left for Codex and each Claude login. The watcher daemon takes the
// readings (it can read the login Keychain); this page asks for them and shows them.
(() => {
  const byId = (id) => document.getElementById(`usage-${id}`);
  const dialog = byId("dialog");
  const windowNames = {
    five_hour: "5-hour",
    seven_day: "Weekly",
    seven_day_opus: "Weekly Opus",
    seven_day_sonnet: "Weekly Sonnet",
  };
  let snapshot = null;
  let loading = null;
  function node(tag, text, cls) {
    const element = document.createElement(tag);
    if (text !== undefined) element.textContent = text;
    if (cls) element.className = cls;
    return element;
  }
  const percent = (value) => `${Math.round(value)}%`;
  const level = (left) => (left < 10 ? "red" : left < 30 ? "amber" : "green");
  function duration(seconds) {
    const minutes = Math.max(1, Math.round(seconds / 60));
    if (minutes < 60) return `${minutes}m`;
    const hours = Math.floor(minutes / 60);
    if (hours < 48) return minutes % 60 ? `${hours}h ${minutes % 60}m` : `${hours}h`;
    return `${Math.round(hours / 24)}d`;
  }
  function account(id) {
    return snapshot?.accounts.find((entry) => entry.id === id) ?? null;
  }
  // The same rule as the server's: most quota left, ties keep catalog order.
  function best(agent) {
    let found = null;
    for (const entry of snapshot?.accounts ?? []) {
      if (entry.left_percent == null || (agent && entry.agent !== agent)) continue;
      if (!found || entry.left_percent > found.left_percent) found = entry;
    }
    return found;
  }
  function windowRow(item) {
    const row = node("div", undefined, "usage-window");
    const used = Math.min(100, Math.max(0, item.used_percent));
    const left = 100 - used;
    const resets = Date.parse(item.resets_at);
    const head = node("div", undefined, "usage-window-head");
    head.append(
      node("span", windowNames[item.name] ?? String(item.name ?? "Quota").replaceAll("_", " ")),
      node(
        "span",
        `${percent(left)} left` +
          (resets > Date.now() ? ` · resets in ${duration((resets - Date.now()) / 1000)}` : ""),
      ),
    );
    const bar = node("div", undefined, `usage-bar ${level(left)}`);
    bar.setAttribute("aria-hidden", "true");
    const fill = node("span");
    fill.style.width = `${used}%`;
    bar.append(fill);
    row.append(head, bar);
    return row;
  }
  function renderList() {
    const list = byId("list");
    const status = byId("status");
    list.replaceChildren();
    if (!snapshot) {
      status.textContent = "Loading usage…";
      return;
    }
    const checked = snapshot.attempted_at
      ? `Checked ${duration(Date.now() / 1000 - snapshot.attempted_at)} ago.`
      : "";
    status.textContent =
      snapshot.reader === "offline"
        ? `The watcher daemon reads usage and is not running. ${checked}`.trim()
        : snapshot.reader === "outdated"
          ? `Restart the watcher daemon to read usage. ${checked}`.trim()
          : !snapshot.accounts.length
            ? "Reading usage…"
            : checked;
    const top = best();
    for (const entry of snapshot.accounts) {
      const item = node("li", undefined, "usage-account");
      const head = node("div", undefined, "usage-account-head");
      head.append(node("strong", entry.label));
      if (top && entry.id === top.id) head.append(node("span", "Most left", "badge green"));
      item.append(head);
      for (const window of entry.windows ?? []) item.append(windowRow(window));
      if (entry.error)
        item.append(
          node(
            "small",
            entry.windows?.length
              ? `Last reading ${entry.checked_at ? `${duration(Date.now() / 1000 - entry.checked_at)} old` : "unavailable"}: ${entry.error}`
              : `Unavailable: ${entry.error}`,
            "usage-error",
          ),
        );
      list.append(item);
    }
  }
  function render() {
    const top = best();
    const toggle = byId("toggle");
    byId("value").textContent = top ? percent(top.left_percent) : "–";
    toggle.dataset.level = top ? level(top.left_percent) : "";
    toggle.setAttribute(
      "aria-label",
      top ? `Usage: ${top.label} has the most quota left, ${percent(top.left_percent)}` : "Usage",
    );
    if (dialog.open) renderList();
  }
  async function load(refresh = false) {
    if (loading && !refresh) return loading;
    const request = (async () => {
      try {
        const response = await fetch(`/api/llm-usage${refresh ? "?refresh=1" : ""}`, {
          cache: "no-store",
        });
        if (!response.ok) throw Error(`HTTP ${response.status}`);
        const value = await response.json();
        if (!Array.isArray(value?.accounts)) throw Error("Unexpected usage reply");
        snapshot = value;
        window.dispatchEvent(new Event("usage-updated"));
        render();
      } catch {
        // Keep the last snapshot; the next poll retries.
      } finally {
        if (loading === request) loading = null;
      }
      return snapshot;
    })();
    loading = request;
    return request;
  }
  byId("toggle").onclick = () => {
    renderList();
    dialog.showModal();
    void load();
  };
  byId("close").onclick = () => dialog.close();
  byId("refresh").onclick = () => {
    byId("status").textContent = "Refresh requested…";
    void load(true);
    // The daemon checks for requests every few seconds, then reads each login.
    for (const delay of [8000, 20000]) setTimeout(() => void load(), delay);
  };
  // Close only when both press and release land outside the box, as for notifications.
  const onBackdrop = (event) => {
    if (event.target !== dialog) return false;
    const rect = dialog.getBoundingClientRect();
    return (
      event.clientX < rect.left ||
      event.clientX > rect.right ||
      event.clientY < rect.top ||
      event.clientY > rect.bottom
    );
  };
  let pressedOnBackdrop = false;
  dialog.addEventListener("pointerdown", (event) => {
    pressedOnBackdrop = onBackdrop(event);
  });
  dialog.addEventListener("click", (event) => {
    if (pressedOnBackdrop && onBackdrop(event)) dialog.close();
    pressedOnBackdrop = false;
  });
  window.dashboardUsage = { account, best, load };
  setInterval(() => {
    if (!document.hidden) void load();
  }, 60000);
  void load();
})();
