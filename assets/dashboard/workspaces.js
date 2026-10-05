/* Optional experiment: owns its requests, rendering, selection and errors. */
(() => {
  const byId = (id) => document.getElementById(id);
  const STATES = {
    merged: ["Merged", "blue"],
    closed: ["Closed", "red"],
    open: ["Open", "green"],
    unknown: ["Unknown", "amber"],
  };
  const STATUSES = {
    ready: ["Ready to clean up", "green"],
    active: ["Open work", "blue"],
    unlinked: ["No linked item", "amber"],
    blocked: ["Needs an override", "amber"],
    protected: ["Protected", "red"],
    missing: ["Checkout missing", "amber"],
  };
  let snapshot = null;
  let busy = false;
  let pending = null;
  let ascending = false;
  const selected = new Set();
  const names = new Map();
  const opening = new Set();
  const openResults = new Map();

  function node(tag, text, className) {
    const element = document.createElement(tag);
    if (text !== undefined) element.textContent = text;
    if (className) element.className = className;
    return element;
  }
  function anchor(text, url) {
    const element = node("a", text);
    if (typeof url === "string" && url.startsWith("https://")) {
      element.href = url;
      element.target = "_blank";
      element.rel = "noopener noreferrer";
    }
    return element;
  }
  function badge(text, tone) {
    return node("span", text, `badge ${tone || ""}`.trim());
  }
  function date(value) {
    return value ? new Date(value * 1000).toLocaleString() : "never";
  }
  function rows() {
    return (snapshot && snapshot.workspaces) || [];
  }
  function matches(row) {
    const term = byId("ws-search").value.trim().toLowerCase();
    const repo = byId("ws-repo").value;
    const status = byId("ws-status").value;
    if (repo !== "all" && row.repo !== repo) return false;
    if (status === "agents" ? !row.agents.length : status !== "all" && row.status !== status)
      return false;
    if (!term) return true;
    return [row.name, row.repo, row.branch, row.path, ...row.links.map((l) => `#${l.number}`)]
      .filter(Boolean)
      .some((value) => String(value).toLowerCase().includes(term));
  }
  function visible() {
    const field = byId("ws-sort").value;
    return rows()
      .filter(matches)
      .sort((left, right) => {
        const a = left[field],
          b = right[field];
        if (a == null && b != null) return 1;
        if (b == null && a != null) return -1;
        return (
          (a != null && b != null ? (ascending ? a - b : b - a) : 0) ||
          (left.repo || "~").localeCompare(right.repo || "~") ||
          left.name.toLowerCase().localeCompare(right.name.toLowerCase()) ||
          left.key.localeCompare(right.key)
        );
      });
  }
  function selectable() {
    return visible().filter((row) => row.removable);
  }
  function linkCell(row) {
    const cell = node("td");
    if (!row.links.length) {
      cell.append(node("small", row.stale_links ? "Checking GitHub…" : "None found", "pr-meta"));
      return cell;
    }
    for (const link of row.links) {
      const line = node("div");
      const label = link.kind === "issue" ? "Issue" : "PR";
      line.append(anchor(`${label} #${link.number}`, link.url), " ");
      const [text, tone] = STATES[link.state] || STATES.unknown;
      line.append(badge(link.draft && link.state === "open" ? "Draft" : text, tone));
      if (link.title) line.append(node("small", link.title, "pr-meta"));
      cell.append(line);
    }
    return cell;
  }
  function localCell(row) {
    const cell = node("td", undefined, "ws-local");
    if (row.missing) {
      cell.append(node("span", "Checkout no longer exists"));
      return cell;
    }
    if (row.changes === null) {
      cell.append(node("small", "Not inspected", "pr-meta"));
      return cell;
    }
    const notes = [];
    if (row.changes) notes.push(`${row.changes} uncommitted`);
    if (row.unpushed)
      notes.push(`${row.unpushed >= 100 ? "100+" : row.unpushed} only on this branch`);
    cell.append(node("span", notes.length ? notes.join(" · ") : "Clean"));
    if (row.sha) cell.append(node("small", row.sha, "pr-meta"));
    return cell;
  }
  function row(entry) {
    const line = node("tr");
    const choose = node("td", undefined, "ws-select");
    const box = document.createElement("input");
    box.type = "checkbox";
    box.checked = selected.has(entry.key);
    box.disabled = !entry.removable;
    box.setAttribute("aria-label", `Select ${entry.name}`);
    box.onchange = () => {
      if (box.checked) selected.add(entry.key);
      else selected.delete(entry.key);
      render();
    };
    choose.append(box);
    line.append(choose);
    const name = node("td");
    name.append(
      entry.workspace_url ? anchor(entry.name, entry.workspace_url) : node("span", entry.name),
    );
    if (entry.path) name.append(node("small", entry.path, "pr-meta"));
    name.append(
      node("small", `Updated ${entry.updated_at ? date(entry.updated_at) : "unknown"}`, "pr-meta"),
      node("small", `Created ${entry.created_at ? date(entry.created_at) : "unknown"}`, "pr-meta"),
    );
    line.append(name);
    const where = node("td");
    where.append(node("span", entry.repo || "Unknown repository"));
    if (entry.branch) where.append(node("small", entry.branch, "pr-meta"));
    line.append(where, linkCell(entry));
    const agents = node("td");
    if (!entry.agents.length) agents.append(node("small", "No agent", "pr-meta"));
    for (const agent of entry.agents) {
      agents.append(
        node("div", `${agent.agent || "agent"} · ${agent.status || "unknown"}`, "ws-agent"),
      );
    }
    line.append(agents, localCell(entry));
    const cleanup = node("td");
    const [text, tone] = STATUSES[entry.status] || STATUSES.unlinked;
    cleanup.append(badge(text, tone));
    for (const item of entry.blockers) cleanup.append(node("small", item.text, "pr-meta"));
    line.append(cleanup);
    const actions = node("td", undefined, "pr-actions");
    const button = node(
      "button",
      opening.has(entry.key)
        ? "Opening…"
        : entry.workspace_ids.length || openResults.get(entry.key)?.url
          ? "Open workspace"
          : "Create workspace",
    );
    button.type = "button";
    button.disabled = opening.has(entry.key) || (!entry.workspace_ids.length && entry.missing);
    button.onclick = () => openWorkspace(entry);
    actions.append(button);
    // Only scanned checkouts have a diff or transcripts to read.
    if (entry.key === entry.path && !entry.missing) {
      actions.append(
        ...window.workspaceViewer.buttons({
          key: entry.key,
          workspace: entry.workspace_ids[0],
          name: entry.name,
          links: entry.links
            .filter((link) => link.number)
            .map((link) => ({
              label: `${link.kind === "issue" ? "Issue" : "PR"} #${link.number}`,
              url: link.url,
              title: link.title || undefined,
            })),
        }),
      );
    }
    const result = openResults.get(entry.key);
    if (result?.url) actions.append(anchor("Open in Collie", result.url));
    if (result?.error) {
      const error = node("small", result.error);
      error.setAttribute("role", "alert");
      actions.append(error);
    }
    line.append(actions);
    return line;
  }
  async function openWorkspace(entry) {
    const opened = window.open("about:blank", "_blank");
    if (opened) opened.opener = null;
    opening.add(entry.key);
    openResults.delete(entry.key);
    render();
    try {
      const response = await fetch("/api/workspace-open", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Babysit-Action": "workspace-open" },
        body: JSON.stringify({ key: entry.key }),
      });
      const value = await response.json();
      if (!response.ok || value.error) throw new Error(value.error || `HTTP ${response.status}`);
      openResults.set(entry.key, value);
      if (opened) opened.location.href = value.url;
    } catch (error) {
      if (opened) opened.close();
      openResults.set(entry.key, { error: error.message });
    } finally {
      opening.delete(entry.key);
      render();
    }
  }
  function repoOptions() {
    const select = byId("ws-repo");
    const names = [
      ...new Set(
        rows()
          .map((entry) => entry.repo)
          .filter(Boolean),
      ),
    ].sort();
    const current = [...select.options].map((option) => option.value);
    // Rebuilding on every refresh would disturb the shared searchable picker.
    if (current.join("\u0000") !== ["all", ...names].join("\u0000")) {
      const chosen = select.value;
      select.replaceChildren(node("option", "All repositories"));
      select.firstChild.value = "all";
      for (const name of names) {
        const option = node("option", name);
        option.value = name;
        select.append(option);
      }
      select.value = names.includes(chosen) ? chosen : "all";
    }
  }

  function render() {
    const data = snapshot;
    if (!data) return;
    byId("workspaces-tab").hidden = !data.enabled;
    const line = byId("ws-status-line");
    if (!data.enabled) {
      byId("ws-list").replaceChildren();
      line.textContent =
        data.error ||
        "Workspace overview is disabled. Enable it in experiments/workspaces/config.json.";
      return;
    }
    repoOptions();
    const shown = visible();
    for (const key of [...selected]) {
      if (!rows().some((entry) => entry.key === key && entry.removable)) selected.delete(key);
    }
    byId("ws-count").textContent = String(shown.length);
    byId("ws-list").replaceChildren(...shown.map(row));
    byId("ws-empty").hidden = shown.length > 0;
    byId("ws-empty").textContent = rows().length
      ? "No workspace matches these filters."
      : "No worktrees or herdr workspaces were found.";
    const ready = rows().filter((entry) => entry.status === "ready").length;
    line.textContent =
      `${data.loading ? "Scanning… " : ""}${data.stale ? "Stale inventory — " : ""}` +
      `${rows().length} workspaces, ${ready} ready to clean up. ` +
      `Observed ${date(data.synced_at)}. Local state is verified again before anything is removed.`;
    const notices = byId("ws-notices");
    notices.replaceChildren();
    for (const warning of [data.error, ...(data.warnings || [])].filter(Boolean).slice(0, 6)) {
      notices.append(node("p", warning, "alert"));
    }
    const all = byId("ws-all");
    const options = selectable();
    all.checked = options.length > 0 && options.every((entry) => selected.has(entry.key));
    all.disabled = !options.length;
    const hidden = selected.size - shown.filter((entry) => selected.has(entry.key)).length;
    byId("ws-selected").textContent =
      `${selected.size - hidden} selected` + (hidden ? ` (${hidden} hidden by filters)` : "");
    byId("ws-cleanup").disabled = selected.size === hidden || Boolean(running());
    renderDialog();
  }
  function running() {
    return snapshot && snapshot.cleanup && snapshot.cleanup.status === "running"
      ? snapshot.cleanup
      : null;
  }
  function results() {
    const job = snapshot && snapshot.cleanup;
    return job ? job.results : [];
  }
  function renderDialog() {
    const dialog = byId("ws-dialog");
    if (!dialog.open) return;
    const content = byId("ws-dialog-content");
    content.replaceChildren();
    const job = snapshot && snapshot.cleanup;
    if (pending) {
      byId("ws-dialog-title").textContent = `Clean up ${pending.length} workspace(s)`;
      byId("ws-dialog-intro").textContent =
        "Agents are asked to exit, the herdr workspace is closed, the worktree is removed and the local branch deleted. Anything not merged into a remote is kept unless you override it here.";
      for (const entry of pending) {
        const item = node("div", undefined, "ws-target");
        item.append(node("strong", entry.name));
        item.append(
          node("small", `${entry.repo || ""} · ${entry.branch || "no branch"}`, "pr-meta"),
        );
        if (!entry.remove_worktree) {
          item.append(node("small", "Only the herdr workspace is closed.", "pr-meta"));
        }
        if (entry.blockers.length) {
          const label = document.createElement("label");
          const box = document.createElement("input");
          box.type = "checkbox";
          box.checked = Boolean(entry.force);
          box.onchange = () => {
            entry.force = box.checked;
            renderDialog();
          };
          const listed = entry.blockers.map((item) => item.text).join("; ");
          label.append(box, ` Remove anyway: ${listed}`);
          item.append(label);
        }
        content.append(item);
      }
      byId("ws-confirm").hidden = false;
      byId("ws-confirm").disabled = false;
      byId("ws-confirm").textContent = `Clean up ${pending.length} workspace(s)`;
      byId("ws-cancel").textContent = "Cancel";
      return;
    }
    byId("ws-dialog-title").textContent =
      job && job.status === "running" ? "Cleaning up…" : "Cleanup results";
    byId("ws-dialog-intro").textContent =
      job && job.status === "running"
        ? "Each workspace is revalidated, then cleaned up one at a time."
        : "The inventory below is rescanned automatically.";
    for (const result of results()) {
      const item = node("div", undefined, "ws-target");
      item.append(node("strong", names.get(result.key) || result.key));
      item.append(
        badge(
          result.status,
          result.status === "failed" ? "red" : result.status === "done" ? "green" : "amber",
        ),
      );
      item.append(node("small", result.message, "pr-meta"));
      for (const step of result.steps || []) item.append(node("small", step, "pr-meta"));
      content.append(item);
    }
    byId("ws-confirm").hidden = true;
    byId("ws-cancel").textContent = "Close";
  }
  function openDialog(targets) {
    pending = targets;
    byId("ws-dialog-error").textContent = "";
    if (!byId("ws-dialog").open) byId("ws-dialog").showModal();
    renderDialog();
  }
  async function confirm() {
    if (!pending) return;
    byId("ws-confirm").disabled = true;
    byId("ws-dialog-error").textContent = "";
    const targets = pending.map((entry) => ({
      key: entry.key,
      approve: entry.force ? [...new Set(entry.blockers.map((item) => item.kind))] : [],
    }));
    for (const entry of pending) names.set(entry.key, entry.name);
    try {
      const response = await fetch("/api/workspace-cleanup", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Babysit-Action": "workspace-cleanup" },
        body: JSON.stringify({ targets }),
      });
      const value = await response.json();
      if (!response.ok) throw new Error(value.error || `HTTP ${response.status}`);
      if (value.accepted === false) {
        throw new Error("another cleanup is still running; wait for it to finish");
      }
      snapshot = { ...snapshot, cleanup: value.cleanup };
      selected.clear();
      pending = null;
      render();
      void refresh();
    } catch (error) {
      byId("ws-dialog-error").textContent = `Cleanup was not started: ${error.message}`;
      byId("ws-confirm").disabled = false;
    }
  }
  async function refresh(force) {
    if (busy) return;
    busy = true;
    try {
      const response = await fetch(`/api/workspace-overview${force ? "?refresh=1" : ""}`);
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      snapshot = await response.json();
      render();
    } catch (error) {
      byId("ws-status-line").textContent =
        `Workspace API unavailable: ${error.message}. Displayed workspaces may be stale.`;
    } finally {
      busy = false;
    }
  }
  byId("ws-search").oninput = render;
  byId("ws-repo").onchange = render;
  byId("ws-status").onchange = render;
  byId("ws-sort").onchange = () => {
    ascending = false;
    updateSortDirection();
  };
  function updateSortDirection() {
    const button = byId("ws-sort-direction");
    button.textContent = ascending ? "Oldest first" : "Newest first";
    button.setAttribute("aria-label", button.textContent);
    button.title = "Reverse sort order";
    render();
  }
  byId("ws-sort-direction").onclick = () => {
    ascending = !ascending;
    updateSortDirection();
  };
  byId("ws-refresh").onclick = () => refresh(true);
  byId("ws-all").onchange = (event) => {
    for (const entry of selectable()) {
      if (event.target.checked) selected.add(entry.key);
      else selected.delete(entry.key);
    }
    render();
  };
  byId("ws-cleanup").onclick = () => {
    // Only what the filters still show is submitted; a hidden row is never swept in.
    const targets = visible()
      .filter((entry) => selected.has(entry.key))
      .map((entry) => ({ ...entry, force: false }));
    if (targets.length) openDialog(targets);
  };
  byId("ws-confirm").onclick = confirm;
  byId("ws-cancel").onclick = () => {
    pending = null;
    byId("ws-dialog").close();
  };
  window.addEventListener("workspaces-visible", () => refresh());
  setInterval(() => {
    if (!document.hidden && !byId("workspaces-panel").hidden) void refresh();
  }, 5000);
  void refresh();
})();
