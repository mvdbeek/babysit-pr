/* Read-only diff and agent transcript dialog for any checkout Collie can open. */
(() => {
  const byId = (id) => document.getElementById(id);
  // The dialog shows one workspace at a time, re-fetched on demand. Every change starts
  // a request; only the newest one may render.
  let viewer = null;
  let viewerRequest = 0;
  let viewerInflight = 0;
  let opener = null;

  function node(tag, text, className) {
    const element = document.createElement(tag);
    if (text !== undefined) element.textContent = text;
    if (className) element.className = className;
    return element;
  }
  function badge(text, tone) {
    return node("span", text, `badge ${tone || ""}`.trim());
  }
  function date(value) {
    return value ? new Date(value * 1000).toLocaleString() : "never";
  }
  function query(params) {
    return Object.entries(params)
      .filter(([, value]) => value !== null && value !== undefined && value !== "")
      .map(([name, value]) => `${encodeURIComponent(name)}=${encodeURIComponent(value)}`)
      .join("&");
  }
  async function getJSON(path, params) {
    const response = await fetch(`${path}?${query(params)}`);
    let value;
    try {
      value = await response.json();
    } catch {
      // A proxy error page is not JSON; say what happened instead of a parse error.
      throw new Error(`HTTP ${response.status}: the dashboard did not answer`);
    }
    if (!response.ok || value.error) throw new Error(value.error || `HTTP ${response.status}`);
    return value;
  }
  function resetViewer(message) {
    byId("ws-viewer-controls").replaceChildren();
    byId("ws-viewer-content").replaceChildren();
    byId("ws-viewer-error").textContent = "";
    byId("ws-viewer-meta").textContent = message;
    for (const [id, mode] of [
      ["ws-view-diff", "diff"],
      ["ws-view-transcript", "transcript"],
    ]) {
      byId(id).setAttribute("aria-selected", String(viewer?.mode === mode));
    }
  }
  // A view names a herdr workspace (anything Collie can open) or a Workspaces-tab row.
  function where(target) {
    return target.workspace ? { workspace: target.workspace } : { key: target.key };
  }
  function openViewer(entry, mode) {
    viewer = { entry, mode, scope: "branch", base: null, session: null, data: null, nodes: [] };
    byId("ws-viewer-title").textContent = entry.name || "Workspace";
    resetViewer("Loading…");
    const dialog = byId("ws-viewer");
    if (!dialog.open) dialog.showModal();
    void loadViewer();
  }
  function change(update) {
    update(viewer);
    viewer.data = null;
    viewer.nodes = [];
    resetViewer("Loading…");
    void loadViewer();
  }
  function setMode(mode) {
    if (viewer && viewer.mode !== mode) change((state) => (state.mode = mode));
  }
  async function loadViewer(options = {}) {
    const current = viewer;
    if (!current) return;
    const token = ++viewerRequest;
    viewerInflight += 1;
    try {
      if (current.mode === "diff") {
        const data = await getJSON("/api/workspace-diff", {
          ...where(current.entry),
          scope: current.scope,
          base: current.scope === "branch" ? current.base : null,
        });
        if (token !== viewerRequest || viewer !== current) return;
        current.data = data;
        current.base = data.base;
        renderDiff(data);
      } else {
        const data = await getJSON("/api/workspace-transcript", {
          ...where(current.entry),
          session: current.session,
          before: options.before,
          after: options.after,
        });
        if (token !== viewerRequest || viewer !== current) return;
        current.session = data.session ? data.session.id : null;
        if (options.after !== undefined && current.data && data.start === options.after) {
          mergeTranscript(data);
        } else if (options.before !== undefined && current.data) {
          prependTranscript(data);
        } else {
          current.data = data;
          renderTranscript(data, true);
        }
      }
      byId("ws-viewer-error").textContent = "";
    } catch (error) {
      if (token === viewerRequest && viewer === current) {
        if (!options.after) byId("ws-viewer-meta").textContent = "";
        byId("ws-viewer-error").textContent = error.message;
        // A failed diff still offers its other choices, such as uncommitted changes only.
        if (current.mode === "diff") diffControls(null);
      }
    } finally {
      viewerInflight -= 1;
    }
  }
  function refreshButton() {
    const button = node("button", "Refresh");
    button.type = "button";
    button.onclick = () => change(() => {});
    return button;
  }
  function choice(label, options, value, onchange) {
    const wrap = node("label", undefined, "ws-viewer-choice");
    const select = document.createElement("select");
    for (const [optionValue, text] of options) {
      const option = node("option", text);
      option.value = optionValue;
      select.append(option);
    }
    select.value = value;
    select.onchange = () => onchange(select.value);
    wrap.append(node("span", label), select);
    return wrap;
  }
  const FILE_STATES = {
    added: ["Added", "green"],
    deleted: ["Deleted", "red"],
    renamed: ["Renamed", "blue"],
  };
  function diffControls(data) {
    const controls = byId("ws-viewer-controls");
    controls.replaceChildren(
      choice(
        "Changes",
        [
          ["branch", "Branch, with uncommitted"],
          ["uncommitted", "Uncommitted only"],
        ],
        data ? data.scope : viewer.scope,
        (value) =>
          change((state) => {
            state.scope = value;
            state.base = null;
          }),
      ),
    );
    if (data?.scope === "branch" && data.bases.length) {
      controls.append(
        choice(
          "Base",
          data.bases.map((base) => [base.ref, `${base.ref} (${base.ahead} ahead)`]),
          data.base,
          (value) => change((state) => (state.base = value)),
        ),
      );
    }
    controls.append(refreshButton());
  }
  function renderDiff(data) {
    viewer.scope = data.scope;
    diffControls(data);
    const files = data.files.length;
    byId("ws-viewer-meta").textContent =
      (data.note ? `${data.note} ` : "") +
      `${files} file${files === 1 ? "" : "s"} changed, +${data.added} −${data.removed}` +
      (data.scope === "branch" ? ` since ${data.base}` : " since the last commit") +
      (data.truncated ? ". The diff was cut short at its size limit." : "");
    const content = byId("ws-viewer-content");
    content.replaceChildren();
    if (data.commits.length) {
      const commits = node("details", undefined, "ws-commits");
      commits.append(
        node("summary", `${data.commits.length} commit${data.commits.length === 1 ? "" : "s"}`),
      );
      for (const commit of data.commits) {
        const line = node("div", undefined, "ws-commit");
        line.append(node("code", commit.sha.slice(0, 9)), " ", node("span", commit.subject));
        line.append(node("small", `${commit.author} · ${date(commit.time)}`, "pr-meta"));
        commits.append(line);
      }
      content.append(commits);
    }
    if (!files && !data.untracked.length) {
      content.append(node("p", "No changes.", "pr-meta"));
    }
    for (const file of data.files) {
      const item = node("details", undefined, "ws-file");
      item.open = files <= 20;
      const summary = node("summary");
      const [text, tone] = FILE_STATES[file.status] || ["Modified", ""];
      summary.append(badge(text, tone), " ", node("code", file.path));
      if (file.binary) summary.append(" ", badge("Binary", "amber"));
      if (file.old_path) summary.append(node("small", ` from ${file.old_path}`, "pr-meta"));
      summary.append(
        node("span", `+${file.added}`, "ws-added"),
        node("span", `−${file.removed}`, "ws-removed"),
      );
      item.append(summary);
      const pre = node("pre", undefined, "ws-diff");
      const lines = document.createDocumentFragment();
      for (const line of file.lines) {
        const kind = line.startsWith("@@")
          ? "ws-hunk"
          : line.startsWith("+")
            ? "ws-add"
            : line.startsWith("-")
              ? "ws-del"
              : "";
        lines.append(node("span", `${line}\n`, kind || undefined));
      }
      pre.append(lines);
      if (!file.lines.length) pre.append(node("span", "No text changes to show.\n", "ws-hunk"));
      if (file.truncated) pre.append(node("span", "… cut short\n", "ws-hunk"));
      item.append(pre);
      content.append(item);
    }
    if (data.untracked.length) {
      const untracked = node("details", undefined, "ws-file");
      untracked.open = true;
      untracked.append(
        node(
          "summary",
          `${data.untracked.length + data.untracked_more} untracked file(s), not in the diff`,
        ),
      );
      const list = node("ul", undefined, "ws-untracked");
      for (const path of data.untracked) list.append(node("li", path));
      if (data.untracked_more) list.append(node("li", `… and ${data.untracked_more} more`));
      untracked.append(list);
      content.append(untracked);
    }
  }
  function sessionLabel(session) {
    const agent = session.agent === "claude" ? "Claude" : "Codex";
    return `${date(session.updated)} · ${agent} · ${session.title || session.id}`;
  }
  function agentName(data) {
    return data.session.agent === "claude" ? "Claude" : "Codex";
  }
  function transcriptMeta(data) {
    byId("ws-viewer-meta").textContent =
      `${agentName(data)} session ${data.session.id}, ${data.total} entries, ` +
      `updated ${date(data.session.updated)}.`;
  }
  function earlierButton(data) {
    const earlier = node("button", `Show earlier entries (${data.start})`, "ws-earlier");
    earlier.type = "button";
    earlier.onclick = () => {
      earlier.disabled = true;
      void loadViewer({ before: viewer.data.start });
    };
    return earlier;
  }
  function renderTranscript(data, follow) {
    const controls = byId("ws-viewer-controls");
    controls.replaceChildren();
    if (data.sessions.length) {
      controls.append(
        choice(
          "Session",
          data.sessions.map((session) => [session.id, sessionLabel(session)]),
          data.session.id,
          (value) => change((state) => (state.session = value)),
        ),
      );
    }
    controls.append(refreshButton());
    const content = byId("ws-viewer-content");
    if (!data.session) {
      byId("ws-viewer-meta").textContent = "";
      content.replaceChildren(
        node("p", "No Claude or Codex session was recorded in this checkout.", "pr-meta"),
      );
      return;
    }
    transcriptMeta(data);
    content.replaceChildren();
    if (data.start > 0) content.append(earlierButton(data));
    const list = node("div", undefined, "ws-transcript");
    viewer.nodes = data.entries.map((entry) => transcriptEntry(entry, agentName(data)));
    list.append(...viewer.nodes);
    content.append(list);
    if (follow) byId("ws-viewer").scrollTop = byId("ws-viewer").scrollHeight;
  }
  function nearBottom() {
    const scroller = byId("ws-viewer");
    return scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 80;
  }
  // A poll returns the last few known entries and anything newer: known entries are
  // updated in place (a tool's output arrives later), so opened tool calls stay open.
  function mergeTranscript(data) {
    const current = viewer.data;
    const follow = nearBottom();
    const list = byId("ws-viewer-content").querySelector(".ws-transcript");
    data.entries.forEach((entry, offset) => {
      const index = data.start + offset - current.start;
      if (index < current.entries.length) {
        const known = current.entries[index];
        if (
          entry.role === "tool" &&
          (known.output !== entry.output || known.error !== entry.error)
        ) {
          current.entries[index] = entry;
          updateTool(viewer.nodes[index], entry);
        }
        return;
      }
      current.entries.push(entry);
      const item = transcriptEntry(entry, agentName(data));
      viewer.nodes.push(item);
      list.append(item);
    });
    current.total = data.total;
    current.session = data.session;
    current.sessions = data.sessions;
    transcriptMeta(current);
    if (follow) byId("ws-viewer").scrollTop = byId("ws-viewer").scrollHeight;
  }
  function prependTranscript(data) {
    const current = viewer.data;
    const scroller = byId("ws-viewer");
    const before = scroller.scrollHeight;
    const added = data.entries.slice(0, current.start - data.start);
    const items = added.map((entry) => transcriptEntry(entry, agentName(data)));
    current.entries = [...added, ...current.entries];
    viewer.nodes = [...items, ...viewer.nodes];
    current.start = data.start;
    const content = byId("ws-viewer-content");
    content.querySelector(".ws-earlier")?.remove();
    content.querySelector(".ws-transcript").prepend(...items);
    if (current.start > 0) content.prepend(earlierButton(current));
    // Keep the entry the reader was looking at in place.
    scroller.scrollTop += scroller.scrollHeight - before;
  }
  function toolOutput(item, entry) {
    item.querySelector(".ws-tool-output, .ws-tool-error")?.remove();
    if (entry.output !== null && entry.output !== undefined) {
      item.append(node("pre", entry.output, entry.error ? "ws-tool-error" : "ws-tool-output"));
    }
  }
  function updateTool(item, entry) {
    const summary = item.querySelector("summary");
    summary.querySelector(".badge")?.remove();
    if (entry.error) summary.append(badge("Error", "red"));
    toolOutput(item, entry);
  }
  function transcriptEntry(entry, agent) {
    if (entry.role === "tool") {
      const item = node("details", undefined, "ws-tool");
      const firstLine = String(entry.input || "")
        .split("\n")[0]
        .slice(0, 120);
      const summary = node("summary");
      summary.append(node("strong", entry.name), " ", node("code", firstLine));
      if (entry.error) summary.append(badge("Error", "red"));
      item.append(summary, node("pre", entry.input || ""));
      toolOutput(item, entry);
      return item;
    }
    const item = node("div", undefined, `ws-msg ws-${entry.role}`);
    item.append(node("strong", entry.role === "user" ? "Prompt" : agent, "ws-msg-who"));
    item.append(node("div", entry.text, "ws-msg-text"));
    return item;
  }
  byId("ws-view-diff").onclick = () => setMode("diff");
  byId("ws-view-transcript").onclick = () => setMode("transcript");
  byId("ws-viewer-close").onclick = () => byId("ws-viewer").close();
  byId("ws-viewer").addEventListener("close", () => {
    viewer = null;
    viewerRequest += 1; // Responses still on their way are dropped.
    // A refreshed list may have replaced the button that opened the viewer.
    const back = opener?.isConnected ? opener : document.querySelector("dialog[open]");
    opener = null;
    back?.focus();
  });
  // A live transcript keeps up while it is open and its newest entries are shown.
  setInterval(() => {
    const data = viewer?.data;
    if (
      viewer?.mode === "transcript" &&
      data?.session &&
      !viewerInflight &&
      !document.hidden &&
      data.start + data.entries.length >= data.total
    ) {
      void loadViewer({ after: Math.max(data.start, data.total - 20) });
    }
  }, 10000);
  // Diff and Transcript buttons for a target: {workspace} (a herdr workspace ID) or
  // {key} (a Workspaces-tab row), plus a name for the dialog title.
  // The pair is one element, so a narrow cell wraps it together.
  function buttons(target) {
    const pair = node("span", undefined, "ws-view-buttons");
    for (const [label, mode] of [
      ["Diff", "diff"],
      ["Transcript", "transcript"],
    ]) {
      const button = node("button", label);
      button.type = "button";
      button.onclick = () => {
        opener = button;
        openViewer(target, mode);
      };
      pair.append(button);
    }
    return [pair];
  }
  window.workspaceViewer = { open: openViewer, buttons };
})();
