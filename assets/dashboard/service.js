"use strict";
// The two services behind this page: the watcher daemon that polls GitHub and starts
// repairs, and the dashboard that serves the page. Restarting either from the health
// badge is what a terminal was needed for after a wedged watcher, or after a pull.
(() => {
  const byId = (id) => document.getElementById(`service-${id}`);
  const dialog = byId("dialog");
  const names = {
    healthy: "Online",
    stale: "Heartbeat stale",
    offline: "Offline",
    unknown: "State unavailable",
  };
  // The dashboard answered, so it is online; only the watcher can look otherwise.
  const levels = { healthy: "green" };
  let state = null;
  let busy = null;
  let timer = null;
  function node(tag, text, cls) {
    const element = document.createElement(tag);
    if (text !== undefined) element.textContent = text;
    if (cls) element.className = cls;
    return element;
  }
  function since(seconds) {
    const minutes = Math.round((Date.now() / 1000 - seconds) / 60);
    if (minutes < 1) return "just now";
    if (minutes < 60) return `${minutes}m ago`;
    const hours = Math.floor(minutes / 60);
    return hours < 48 ? `${hours}h ago` : `${Math.round(hours / 24)}d ago`;
  }
  function details(parts) {
    return parts.filter(Boolean).join(" · ");
  }
  function rows() {
    const daemon = state.daemon ?? {};
    const dashboard = state.dashboard ?? {};
    return [
      {
        id: "daemon",
        name: "Watcher daemon",
        state: names[daemon.health] ?? names.unknown,
        level: levels[daemon.health] ?? "amber",
        detail: details([
          daemon.pid ? `pid ${daemon.pid}` : "no pid recorded",
          daemon.started_at && `started ${since(daemon.started_at)}`,
          daemon.max_workers && `${daemon.max_workers} repairs at a time`,
        ]),
        note: "Repairs already running keep going; the new watcher picks up their results.",
        restartable: true,
      },
      {
        id: "dashboard",
        name: "Dashboard",
        state: "Online",
        level: "green",
        detail: details([
          dashboard.pid ? `pid ${dashboard.pid}` : "",
          dashboard.started_at && `started ${since(dashboard.started_at)}`,
          dashboard.service ? `${dashboard.service} starts it again` : "started by hand",
        ]),
        note: dashboard.service
          ? "The page reloads once the new dashboard answers."
          : "Nothing would start this dashboard again, so it cannot be restarted from here.",
        restartable: Boolean(dashboard.service),
      },
    ];
  }
  function render() {
    const list = byId("list");
    list.replaceChildren();
    if (!state) return;
    for (const spec of rows()) {
      const item = node("li", undefined, "service-item");
      const head = node("div", undefined, "service-head");
      head.append(node("strong", spec.name), node("span", spec.state, `badge ${spec.level}`));
      const button = node("button", busy === spec.id ? "Restarting…" : "Restart");
      button.type = "button";
      button.disabled = Boolean(busy) || !spec.restartable;
      button.setAttribute("aria-label", `Restart the ${spec.name.toLowerCase()}`);
      button.onclick = () => void restart(spec.id);
      const foot = node("div", undefined, "service-foot");
      foot.append(node("small", spec.detail), button);
      item.append(head, foot, node("small", spec.note, "service-note"));
      list.append(item);
    }
  }
  async function load() {
    const response = await fetch("/api/service", { cache: "no-store" });
    const value = await response.json();
    if (!response.ok) throw Error(value.error || `HTTP ${response.status}`);
    state = value;
    render();
    return value;
  }
  async function poll() {
    try {
      await load();
    } catch {
      // The dashboard may be restarting; the next poll finds it.
    }
  }
  // Wait for a dashboard that is not the process this page asked to stop, then start
  // the page again on the code that one serves.
  async function awaitDashboard(before) {
    const deadline = Date.now() + 90000;
    while (Date.now() < deadline) {
      await new Promise((resolve) => setTimeout(resolve, 1000));
      try {
        if ((await load()).dashboard.pid !== before.pid) return;
      } catch {
        // Down between the two processes.
      }
    }
    throw Error("it did not answer in 90 seconds; check it where it runs");
  }
  async function restart(service) {
    if (busy) return;
    busy = service;
    byId("status").textContent =
      service === "daemon" ? "Restarting the watcher…" : "Restarting the dashboard…";
    render();
    try {
      const response = await fetch("/api/service-restart", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Babysit-Action": "service-restart" },
        body: JSON.stringify({ service }),
      });
      const result = await response.json();
      if (!response.ok) throw Error(result.error || `HTTP ${response.status}`);
      if (service === "dashboard") {
        await awaitDashboard(result.dashboard);
        location.reload();
        return;
      }
      state = { ...state, ...result };
      byId("status").textContent = "The watcher restarted. Repairs already running kept going.";
    } catch (error) {
      byId("status").textContent = `Could not restart: ${error.message}`;
    } finally {
      busy = null;
      render();
    }
  }
  document.getElementById("health").onclick = () => {
    render();
    dialog.showModal();
    void poll();
    clearInterval(timer);
    timer = setInterval(() => {
      if (!document.hidden && dialog.open) void poll();
    }, 5000);
  };
  byId("close").onclick = () => dialog.close();
  dialog.addEventListener("close", () => {
    clearInterval(timer);
    byId("status").textContent = "";
  });
  // Close only when both press and release land outside the box, as for usage.
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
})();
