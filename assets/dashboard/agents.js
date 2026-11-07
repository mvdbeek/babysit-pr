/* Agents: every agent herdr runs, on this machine and the workers, and what each is doing.
   Owns its polling, filters and the tab's badge. Choosing an agent opens the workspace
   viewer on it: docked beside the list on a wide screen, as a dialog on a narrow one. */
(() => {
  const byId = (id) => document.getElementById(id);
  // Polled while the tab shows; otherwise only for the badge, much less often.
  const VISIBLE_POLL = 3000;
  const HIDDEN_POLL = 30000;
  const STATES = {
    blocked: ["Waiting for you", "red", 0],
    working: ["Working", "amber", 1],
    idle: ["Idle", "green", 2],
    done: ["Done", "green", 3],
    unknown: ["Unknown", "", 4],
  };
  const ORIGINS = { watch: ["Watch", "blue"], cron: ["Cron", "blue"] };
  const KINDS = { claude: "Claude", codex: "Codex" };
  const WIDE = matchMedia("(min-width: 1000px)");
  let snapshot = null;
  let busy = false;
  let fetchedAt = 0;
  let selected = null;
  let renderedKey = null;

  function node(tag, text, className) {
    const element = document.createElement(tag);
    if (text !== undefined) element.textContent = text;
    if (className) element.className = className;
    return element;
  }
  function badge(text, tone) {
    return node("span", text, `badge ${tone || ""}`.trim());
  }
  function visible() {
    return !byId("agents-panel").hidden && !document.hidden;
  }
  // "4m", "2h", "3d": how long an agent has been in its state, as far as the dashboard saw.
  function age(since, now) {
    if (!since) return "";
    const seconds = Math.max(0, now - since);
    if (seconds < 60) return `${Math.floor(seconds)}s`;
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)}h`;
    return `${Math.floor(seconds / 86400)}d`;
  }
  function machineName(agent) {
    return agent.machine_label || "This machine";
  }
  function workers() {
    return (snapshot?.machines || []).filter((machine) => !machine.local);
  }
  function filtered() {
    const agents = snapshot?.agents || [];
    const search = byId("agents-search").value.trim().toLowerCase();
    const machine = byId("agents-machine").value;
    const state = byId("agents-state").value;
    const origin = byId("agents-origin").value;
    return agents
      .filter(
        (agent) =>
          (!machine || (agent.machine || "local") === machine) &&
          (!state ||
            (state === "settled"
              ? ["idle", "done"].includes(agent.status)
              : agent.status === state)) &&
          (!origin || agent.origin === origin) &&
          (!search ||
            [
              agent.name,
              agent.repo,
              agent.branch,
              agent.title,
              agent.workspace_label,
              agent.checkout,
            ]
              .filter(Boolean)
              .some((text) => text.toLowerCase().includes(search))),
      )
      .sort(
        (a, b) =>
          (STATES[a.status] || STATES.unknown)[2] - (STATES[b.status] || STATES.unknown)[2] ||
          // Waiting longest first; otherwise the latest change first.
          (a.status === "blocked" ? 1 : -1) * ((a.since || 0) - (b.since || 0)) ||
          a.id.localeCompare(b.id),
      );
  }
  // What the viewer opens: the agent's workspace, with its session and pane to start on.
  function target(agent) {
    return {
      id: `agent:${agent.id}`,
      session: agent.session || null,
      pane: agent.pane,
      workspace: agent.workspace,
      name: agent.name || agent.workspace,
      links: [
        ...(agent.links || []),
        ...(agent.origin_url ? [{ label: agent.origin_label, url: agent.origin_url }] : []),
      ],
      machineLabel: agent.machine_label || undefined,
    };
  }
  function open(agent) {
    const viewer = window.workspaceViewer;
    if (!viewer) return;
    selected = agent.id;
    const current = viewer.current();
    const mode = current?.mode || "transcript";
    if (WIDE.matches) viewer.dock(byId("agents-detail"));
    else viewer.undock();
    byId("agents-placeholder").hidden = WIDE.matches;
    void viewer.open(target(agent), mode, () => filtered().map(target));
    render(true);
  }
  function row(agent, now) {
    const [label, tone] = STATES[agent.status] || [agent.status, ""];
    const item = node("li", undefined, "agents-row");
    item.dataset.status = agent.status;
    item.dataset.agent = agent.id;
    const button = node("button", undefined, "agents-open");
    button.type = "button";
    button.setAttribute("aria-current", String(agent.id === selected));
    const head = node("span", undefined, "agents-head");
    head.append(badge(label, tone));
    const since = age(agent.since, now);
    if (since) {
      const time = node("span", since, "agents-age");
      time.title = `In this state since ${new Date(agent.since * 1000).toLocaleString()}`;
      head.append(time);
    }
    head.append(node("span", KINDS[agent.agent] || agent.agent || "Agent", "agents-kind"));
    if (workers().length) head.append(node("span", machineName(agent), "agents-machine"));
    if (ORIGINS[agent.origin]) {
      const origin = badge(...ORIGINS[agent.origin]);
      if (agent.origin_label) origin.title = agent.origin_label;
      head.append(origin);
    }
    const name = [agent.repo, agent.name].filter(Boolean);
    const title = node(
      "strong",
      name.length === 2 && name[0] !== name[1]
        ? `${name[0]} · ${name[1]}`
        : name[0] || agent.workspace,
      "agents-name",
    );
    button.append(head, title);
    if (agent.title) button.append(node("span", agent.title, "agents-title"));
    const where = [agent.branch, agent.pane].filter(Boolean).join(" · ");
    button.append(node("span", where, "agents-where pr-meta"));
    button.onclick = () => open(agent);
    item.append(button);
    for (const link of agent.links || []) {
      if (!/^https:\/\//.test(link.url || "")) continue;
      const anchor = node("a", link.label, "agents-link");
      anchor.href = link.url;
      anchor.target = "_blank";
      anchor.rel = "noopener noreferrer";
      if (link.title) anchor.title = link.title;
      item.append(anchor);
    }
    return item;
  }
  function machineOptions() {
    const select = byId("agents-machine");
    const machines = snapshot?.machines || [];
    const wanted = [
      ["", "All machines"],
      ...machines.map((machine) => [machine.local ? "local" : machine.id, machine.label]),
    ];
    const current = select.value;
    if (
      JSON.stringify([...select.options].map((o) => [o.value, o.text])) !== JSON.stringify(wanted)
    )
      select.replaceChildren(
        ...wanted.map(([value, text]) => {
          const option = node("option", text);
          option.value = value;
          return option;
        }),
      );
    select.value = wanted.some(([value]) => value === current) ? current : "";
    // app.js makes every select searchable, wrapping it with its label.
    window.dashboardAgents?.sync(select);
    const field = select.closest(".searchable-select") || select.closest("label");
    if (field) field.hidden = machines.length < 2;
  }
  function statusLine() {
    const parts = [];
    for (const machine of snapshot.machines) {
      if (machine.error)
        parts.push(
          `${machine.label}: ${machine.error}` +
            (machine.synced_at
              ? `, showing agents from ${new Date(machine.synced_at * 1000).toLocaleTimeString()}`
              : ""),
        );
      else if (machine.pending) parts.push(`Checking ${machine.label}…`);
    }
    if (!parts.length) parts.push(`Updated ${new Date(snapshot.now * 1000).toLocaleTimeString()}`);
    return parts.join(" · ");
  }
  function badgeTab() {
    const agents = snapshot?.agents || [];
    const waiting = agents.filter((agent) => agent.status === "blocked").length;
    const count = byId("agents-tab-count");
    count.hidden = !agents.length;
    count.textContent = String(agents.length);
    count.classList.toggle("attention-count", waiting > 0);
    // Hidden, it still describes the tab to screen readers.
    const description = waiting ? `${waiting} waiting for you` : "";
    byId("agents-tab-needs").textContent = description;
    count.title = description;
  }
  function render(force = false) {
    if (!snapshot) return;
    badgeTab();
    machineOptions();
    const now = snapshot.now;
    const shown = filtered();
    byId("agents-count").textContent = String(snapshot.agents.length);
    byId("agents-status").textContent = statusLine();
    // Rebuilt only on a change, so focus and scroll stay; ages tick with each poll.
    const key = JSON.stringify([shown, selected, now - (now % 60), workers().length]);
    if (!force && key === renderedKey) return;
    renderedKey = key;
    const list = byId("agents-list");
    const focused = document.activeElement?.closest?.(".agents-row")?.dataset.agent;
    if (!shown.length) {
      list.replaceChildren(
        node(
          "li",
          snapshot.agents.length ? "No agent matches these filters." : "No agents are running.",
          "agents-empty pr-meta",
        ),
      );
    } else list.replaceChildren(...shown.map((agent) => row(agent, now)));
    if (focused)
      list.querySelector(`.agents-row[data-agent="${CSS.escape(focused)}"] .agents-open`)?.focus();
    window.workspaceViewer?.listChanged();
  }
  async function refresh() {
    if (busy) return;
    busy = true;
    try {
      const response = await fetch("/api/agents");
      const value = await response.json();
      if (!response.ok || value.error) throw new Error(value.error || `HTTP ${response.status}`);
      snapshot = value;
      fetchedAt = Date.now();
      render();
    } catch (error) {
      byId("agents-status").textContent = `Could not list agents: ${error.message}`;
    } finally {
      busy = false;
    }
  }
  // The viewer docks only while this tab shows on a wide screen.
  function placeViewer() {
    const viewer = window.workspaceViewer;
    if (!viewer) return;
    const docked = byId("agents-detail").querySelector("#ws-viewer");
    if (docked && (byId("agents-panel").hidden || !WIDE.matches)) {
      viewer.undock();
      selected = null;
      byId("agents-placeholder").hidden = false;
      render(true);
    }
  }
  for (const id of ["agents-search", "agents-machine", "agents-state", "agents-origin"])
    byId(id).addEventListener("input", () => render(true));
  window.addEventListener("agents-visible", () => void refresh());
  new MutationObserver(placeViewer).observe(byId("agents-panel"), {
    attributes: true,
    attributeFilter: ["hidden"],
  });
  WIDE.addEventListener("change", placeViewer);
  // The viewer stepped to another agent, or closed: the list follows.
  window.addEventListener("workspace-viewer-open", (event) => {
    const id = event.detail.id;
    const next = id?.startsWith("agent:") ? id.slice(6) : null;
    if (next !== selected && byId("agents-detail").querySelector("#ws-viewer")) {
      selected = next;
      render(true);
    }
  });
  byId("ws-viewer").addEventListener("close", () => {
    if (!selected) return;
    selected = null;
    byId("agents-placeholder").hidden = false;
    render(true);
  });
  setInterval(() => {
    const due = visible() ? VISIBLE_POLL : HIDDEN_POLL;
    if (!document.hidden && Date.now() - fetchedAt >= due - 500) void refresh();
  }, 1000);
  window.agentsTab = {
    // From a machine badge on the Workspaces tab: that machine's agents.
    showMachine(machine) {
      const select = byId("agents-machine");
      if (![...select.options].some((option) => option.value === machine)) {
        const option = node("option", machine);
        option.value = machine;
        select.append(option);
      }
      select.value = machine;
      window.dashboardAgents?.sync(select);
      render(true);
    },
  };
  void refresh();
})();
