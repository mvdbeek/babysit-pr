/* Needs you: one list of everything waiting on the user, merged across every source.
   Owns its polling, rendering and the attention badges on the other tabs. */
(() => {
  const byId = (id) => document.getElementById(id);
  // Tabs whose badge counts the items listed here; Scheduled and Cron keep their own.
  const BADGED = ["watcher", "prs", "issues", "workspaces"];
  const TONES = { answer: "red", review: "green", unblock: "amber", tidy: "", start: "blue" };
  const KINDS = { pr: "PR", issue: "Issue", branch: "Branch", cron: "Cron job" };
  const WATCH = {
    watching: "Watching",
    running: "Repairing",
    blocked: "Blocked",
    awaiting_release: "Awaiting handoff",
    handoff: "Handing off",
    paused: "Paused",
  };
  let feed = null;
  let busy = false;
  let renderedKey = null;
  let fetched = 0;
  // Parked items and the ones waiting on others start folded.
  const collapsed = new Set(["parked", "waiting"]);
  const triaging = new Set();
  // A poll answered after a triage click must not bring back the state before it.
  let generation = 0;

  function node(tag, text, className) {
    const element = document.createElement(tag);
    if (text !== undefined) element.textContent = text;
    if (className) element.className = className;
    return element;
  }
  // Item URLs come from GitHub and herdr; anything that is not a web address stays text.
  function anchor(text, url, className) {
    if (!/^(https?:\/\/|[?#])/.test(url)) return node("span", text, className);
    const element = node("a", text, className);
    element.href = url;
    if (/^https?:/.test(url)) {
      element.target = "_blank";
      element.rel = "noopener noreferrer";
    }
    return element;
  }
  function visible() {
    return !byId("attention-panel").hidden;
  }
  function relative(seconds) {
    if (!seconds) return "";
    const age = Math.max(0, Date.now() / 1000 - seconds);
    if (age < 60) return "just now";
    if (age < 3600) return `${Math.floor(age / 60)}m ago`;
    if (age < 86400) return `${Math.floor(age / 3600)}h ago`;
    return `${Math.floor(age / 86400)}d ago`;
  }
  // Where the item's row with all its actions lives: the overview tab that lists it.
  function dashboardLink(item) {
    const page = item.pages[0];
    if (!["prs", "issues", "watcher"].includes(page) || !item.url) return `#${page}`;
    return `?item=${encodeURIComponent(item.url)}#${page}`;
  }
  function pageName(page) {
    return (
      {
        prs: "Pull requests",
        issues: "Issues",
        watcher: "Watcher",
        workspaces: "Workspaces",
        scheduled: "Scheduled",
        cron: "Cron jobs",
      }[page] || page
    );
  }
  function card(item) {
    const row = node("li", undefined, `attention-item attention-${item.group}`);
    row.dataset.key = item.key;
    const head = node("div", undefined, "attention-head");
    const subject = node("div", undefined, "attention-subject");
    const label = [KINDS[item.kind] || "", item.repo, item.number ? `#${item.number}` : ""]
      .filter(Boolean)
      .join(" ");
    if (label) subject.append(node("span", label, "attention-where"));
    subject.append(
      item.url
        ? anchor(item.title || item.url, item.url, "attention-title")
        : node("span", item.title || item.key, "attention-title"),
    );
    head.append(subject);
    if (item.since) {
      const when = node("time", relative(item.since), "attention-since");
      when.dataset.since = item.since;
      when.dateTime = new Date(item.since * 1000).toISOString();
      when.title = new Date(item.since * 1000).toLocaleString();
      head.append(when);
    }
    row.append(head);
    const reasons = node("ul", undefined, "attention-reasons");
    for (const reason of item.reasons)
      reasons.append(node("li", reason.text, `badge ${TONES[reason.group] || ""}`.trim()));
    row.append(reasons);
    const meta = [];
    if (item.watch?.status && WATCH[item.watch.status])
      meta.push(`Watch: ${WATCH[item.watch.status]}`);
    if (item.agent?.status) meta.push(`Agent: ${item.agent.status}`);
    if (meta.length) row.append(node("p", meta.join(" · "), "attention-meta"));
    const actions = node("div", undefined, "attention-actions");
    const page = item.pages[0];
    const open = anchor(`Show in ${pageName(page)}`, dashboardLink(item), "attention-show");
    open.onclick = (event) => {
      if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
      event.preventDefault();
      window.dashboardNavigation?.open(open.getAttribute("href"));
    };
    actions.append(open);
    if (item.workspace_url) actions.append(anchor("Open in Collie", item.workspace_url));
    if (item.url) actions.append(anchor("GitHub", item.url));
    const waiting = "waiting_since" in item;
    const triage = node("button", waiting ? "Resume" : "Wait for activity", "attention-triage");
    triage.type = "button";
    triage.title = waiting
      ? "List this item again now"
      : "Set this item aside until something happens on it";
    triage.disabled = triaging.has(item.key);
    triage.onclick = () => void setAside(item, waiting ? "clear" : "wait");
    actions.append(triage);
    if (item.cron?.run) {
      // The same acknowledgement as on the Cron jobs tab; an agent left open stays open.
      const dismiss = node("button", "Dismiss", "attention-dismiss");
      dismiss.type = "button";
      dismiss.title = "Mark the job's latest run as dealt with";
      dismiss.disabled = triaging.has(item.key);
      dismiss.onclick = () => void dismissRun(item);
      actions.append(dismiss);
    }
    row.append(actions);
    return row;
  }
  async function setAside(item, action) {
    if (triaging.has(item.key)) return;
    triaging.add(item.key);
    byId("attention-error").textContent = "";
    render();
    try {
      const response = await fetch("/api/attention-triage", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Babysit-Action": "attention-triage" },
        body: JSON.stringify({ login: feed?.login ?? "", key: item.key, action }),
      });
      const result = await response.json();
      if (!response.ok) throw Error(result.error || `HTTP ${response.status}`);
      generation += 1;
      feed = result.feed;
    } catch (error) {
      byId("attention-error").textContent = `Could not change the item: ${error.message}`;
    } finally {
      triaging.delete(item.key);
      render();
    }
  }
  async function dismissRun(item) {
    if (triaging.has(item.key)) return;
    triaging.add(item.key);
    byId("attention-error").textContent = "";
    render();
    try {
      const response = await fetch("/api/cron-action", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Babysit-Action": "cron-action" },
        body: JSON.stringify({ action: "dismiss", id: item.cron.id, run: item.cron.run }),
      });
      const result = await response.json();
      if (!response.ok || result.error) throw Error(result.error || `HTTP ${response.status}`);
      // A poll already under way may predate the dismissal; this fresh feed replaces it.
      generation += 1;
      const fresh = await fetch("/api/attention?refresh=1", { cache: "no-store" });
      if (fresh.ok) feed = await fresh.json();
    } catch (error) {
      byId("attention-error").textContent = `Could not dismiss the run: ${error.message}`;
    } finally {
      triaging.delete(item.key);
      render();
    }
  }
  function group(entry, items) {
    const section = node("section", undefined, "attention-group");
    section.dataset.group = entry.key;
    const heading = node("h3");
    const toggle = node("button", undefined, "attention-toggle");
    toggle.type = "button";
    toggle.append(`${entry.label} `, node("span", String(items.length), "count"));
    toggle.setAttribute("aria-expanded", String(!collapsed.has(entry.key)));
    toggle.setAttribute("aria-controls", `attention-list-${entry.key}`);
    const list = node("ol", undefined, "attention-list");
    list.id = `attention-list-${entry.key}`;
    list.hidden = collapsed.has(entry.key);
    list.append(...items.map(card));
    // Folded in place: the button keeps focus and nothing else is rebuilt.
    toggle.onclick = () => {
      if (collapsed.has(entry.key)) collapsed.delete(entry.key);
      else collapsed.add(entry.key);
      list.hidden = collapsed.has(entry.key);
      toggle.setAttribute("aria-expanded", String(!list.hidden));
    };
    heading.append(toggle);
    section.append(heading, list);
    return section;
  }
  function badges() {
    const pages = feed?.pages || {};
    for (const page of BADGED) {
      const badge = byId(`${page}-tab-count`);
      const count = pages[page] || 0;
      badge.hidden = !count;
      badge.textContent = count;
      // The badge is outside the tab's name (which stays "Pull requests"); the hidden
      // span the tab is described by carries the count to screen readers.
      const description = !count
        ? ""
        : count === 1
          ? "1 item needs you"
          : `${count} items need you`;
      badge.title = description;
      byId(`${page}-tab-needs`).textContent = description;
    }
    // Parked items are listed, folded, but not counted.
    const total = feed ? feed.items.filter((item) => !item.parked).length : 0;
    byId("attention-tab-count").hidden = !total;
    byId("attention-tab-count").textContent = total;
    byId("attention-tab-needs").textContent = total ? `${total} item${total === 1 ? "" : "s"}` : "";
    byId("attention-count").textContent = total;
    const waiting = feed?.agents_waiting || 0;
    const chip = byId("agents-waiting");
    chip.hidden = !waiting;
    byId("agents-waiting-count").textContent = waiting;
    chip.setAttribute(
      "aria-label",
      waiting
        ? `${waiting} agent${waiting === 1 ? "" : "s"} waiting for you`
        : "No agents waiting for you",
    );
  }
  function status() {
    if (!feed) return "Loading what needs you…";
    const sources = feed.sources || {};
    const failed = Object.entries(sources)
      .filter(([, info]) => info?.error)
      .map(([name, info]) => info.error || name);
    const base =
      "Merged from the watcher, your pull requests and issues, workspaces, scheduled tasks and cron jobs.";
    return failed.length ? `${base} Some sources are unavailable: ${failed.join("; ")}` : base;
  }
  function render() {
    badges();
    byId("attention-status").textContent = status();
    const items = feed?.items || [];
    const waiting = feed?.waiting || [];
    const key = JSON.stringify([items, waiting, [...triaging]]);
    if (key === renderedKey) {
      for (const when of byId("attention-groups").querySelectorAll("time[data-since]"))
        when.textContent = relative(Number(when.dataset.since));
      return;
    }
    renderedKey = key;
    // Give focus back to the same control of the same card or group after the rebuild.
    const active = document.activeElement;
    const owner = active?.closest("#attention-groups [data-key], #attention-groups [data-group]");
    const controls = (root) => [...root.querySelectorAll("a, button")];
    const position = owner ? controls(owner).indexOf(active) : -1;
    const groups = (feed?.groups || []).filter((entry) => entry.count);
    byId("attention-groups").replaceChildren(
      ...groups.map((entry) =>
        group(
          entry,
          items.filter((item) => item.group === entry.key),
        ),
      ),
      ...(waiting.length ? [group({ key: "waiting", label: "Waiting on others" }, waiting)] : []),
    );
    if (owner) {
      const same = owner.dataset.key
        ? [...byId("attention-groups").querySelectorAll("[data-key]")].find(
            (card) => card.dataset.key === owner.dataset.key,
          )
        : byId("attention-groups").querySelector(`[data-group="${owner.dataset.group}"]`);
      if (same) controls(same)[position]?.focus({ preventScroll: true });
    }
    byId("attention-empty").hidden = !feed || items.length > 0;
  }
  async function refresh(force = false) {
    // Every 5 seconds while shown (the tab badges need it everywhere, so 30 otherwise).
    if (busy || document.hidden) return;
    if (!force && Date.now() - fetched < (visible() ? 4000 : 29000)) return;
    busy = true;
    fetched = Date.now();
    const started = generation;
    try {
      const response = await fetch(force ? "/api/attention?refresh=1" : "/api/attention", {
        cache: "no-store",
      });
      const result = await response.json();
      if (!response.ok) throw Error(result.error || `HTTP ${response.status}`);
      if (started === generation) feed = result;
      byId("attention-error").textContent = "";
    } catch (error) {
      byId("attention-error").textContent = `Cannot load what needs you: ${error.message}`;
    } finally {
      busy = false;
    }
    render();
  }
  byId("attention-refresh").onclick = () => void refresh(true);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) void refresh(true);
  });
  setInterval(() => void refresh(), 5000);
  window.dashboardAttention = { refresh, feed: () => feed };
  void refresh(true);
})();
