/* Diff and agent transcript dialog for any checkout Collie can open, with diff comments
   and follow-up messages sent to the workspace's agent. */
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
  function external(label, url) {
    const anchor = node("a", label);
    anchor.href = url;
    anchor.target = "_blank";
    anchor.rel = "noopener noreferrer";
    return anchor;
  }
  // Web links in agent text become anchors; everything else stays plain text.
  const LINK = /https?:\/\/[^\s<>"'`]+/g;
  function linkify(text) {
    const fragment = document.createDocumentFragment();
    let last = 0;
    for (const match of String(text).matchAll(LINK)) {
      // Sentence punctuation and an unmatched closing bracket end the link.
      let url = match[0].replace(/[.,;:!?]+$/, "");
      for (const [open, close] of ["()", "[]"]) {
        while (url.endsWith(close) && url.split(open).length < url.split(close).length)
          url = url.slice(0, -1);
      }
      fragment.append(String(text).slice(last, match.index), external(url, url));
      last = match.index + url.length;
    }
    fragment.append(String(text).slice(last));
    return fragment;
  }
  // Agents answer in Markdown. It is rendered as DOM nodes, never as HTML, and links
  // only to web addresses; anything it does not recognize stays plain text.
  // Each span ends on its own line, which keeps matching linear in the line's length.
  // No lookbehind: older iOS Safari would reject the whole script.
  const INLINE =
    /(`+)([^`\n]|[^`\n][^\n]*?[^`\n])\1(?!`)|\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)|\*\*(?!\s)((?:[^*\n]|\*(?!\*))+?)\*\*|__(?!\s)([^_\n]+?)__|(^|[^\w*])\*(?![\s*])([^*\n]+?)\*(?![\w*])|(^|[^\w_])_(?![\s_])([^_\n]+?)_(?![\w_])/g;
  function inline(text) {
    const fragment = document.createDocumentFragment();
    let last = 0;
    for (const match of text.matchAll(INLINE)) {
      fragment.append(linkify(text.slice(last, match.index)));
      const [, ticks, code, label, url, bold, bolder, before, italic, beside, slanted] = match;
      // An italic span's match starts with the character before it.
      fragment.append(before ?? beside ?? "");
      if (ticks) fragment.append(node("code", code.replace(/^ ([\s\S]*\S[\s\S]*) $/, "$1")));
      else if (label) fragment.append(external(label, url));
      else {
        const styled = node(bold || bolder ? "strong" : "em");
        styled.append(inline(bold || bolder || italic || slanted));
        fragment.append(styled);
      }
      last = match.index + match[0].length;
    }
    fragment.append(linkify(text.slice(last)));
    return fragment;
  }
  // Single line breaks are kept, as agents write them to be read.
  function inlineLines(lines, tag) {
    const element = node(tag);
    lines.forEach((line, index) => {
      if (index) element.append(node("br"));
      element.append(inline(line));
    });
    return element;
  }
  // A backtick fence's info string has no backticks, so ```x``` is inline code.
  const FENCE = /^\s*(`{3,}(?=[^`]*$)|~{3,})/;
  const HEADING = /^\s{0,3}(#{1,6})(?:\s+(.*))?$/;
  const RULE = /^\s{0,3}([-*_])(\s*\1){2,}\s*$/;
  const QUOTE = /^\s{0,3}>\s?/;
  const ITEM = /^(\s*)([-*+]|\d{1,9}[.)])\s+(.*)$/;
  const TABLE_RULE = /^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$/;
  function startsBlock(lines, index) {
    const line = lines[index];
    return (
      FENCE.test(line) ||
      HEADING.test(line) ||
      RULE.test(line) ||
      QUOTE.test(line) ||
      ITEM.test(line) ||
      tableStart(lines, index)
    );
  }
  function tableStart(lines, index) {
    const rule = lines[index + 1] ?? "";
    return lines[index].includes("|") && rule.includes("|") && TABLE_RULE.test(rule);
  }
  function cells(line) {
    // Split on pipes, except escaped ones, dropping the optional outer pipes.
    const found = [""];
    const text = line.trim().replace(/^\|/, "");
    for (let index = 0; index < text.length; index++) {
      if (text[index] === "\\" && text[index + 1] === "|") {
        found[found.length - 1] += "|";
        index++;
      } else if (text[index] === "|") found.push("");
      else found[found.length - 1] += text[index];
    }
    if (found.length > 1 && !found[found.length - 1].trim()) found.pop();
    return found.map((cell) => cell.trim());
  }
  function list(lines, start) {
    const first = ITEM.exec(lines[start]);
    const indent = first[1].length;
    const ordered = /\d/.test(first[2]);
    const element = node(ordered ? "ol" : "ul");
    if (ordered && parseInt(first[2], 10) !== 1) element.start = parseInt(first[2], 10);
    let index = start;
    while (index < lines.length) {
      const match = ITEM.exec(lines[index]);
      if (!match) {
        // A blank line between items keeps the list going.
        const next = lines.slice(index).findIndex((line) => line.trim());
        const after = next < 0 ? null : ITEM.exec(lines[index + next]);
        if (!lines[index].trim() && after && after[1].length >= indent) {
          index += next;
          continue;
        }
        break;
      }
      const depth = match[1].length;
      if (depth > indent) {
        const [nested, end] = list(lines, index);
        element.lastElementChild.append(nested);
        index = end;
        continue;
      }
      // A shallower item, or one of the other list type, ends this list.
      if (depth < indent || /\d/.test(match[2]) !== ordered) break;
      const text = [match[3]];
      index++;
      while (index < lines.length && lines[index].trim() && !startsBlock(lines, index))
        text.push(lines[index++].trim());
      element.append(inlineLines(text, "li"));
    }
    return [element, index];
  }
  function markdown(text, className) {
    const root = node("div", undefined, className);
    const lines = String(text).replace(/\r\n?/g, "\n").split("\n");
    let index = 0;
    while (index < lines.length) {
      const line = lines[index];
      const fence = FENCE.exec(line);
      if (fence) {
        const body = [];
        index++;
        // Only a bare run of at least as many of the same character closes it.
        const closes = (text) =>
          text.length >= fence[1].length && [...text].every((c) => c === fence[1][0]);
        while (index < lines.length && !closes(lines[index].trim())) body.push(lines[index++]);
        index++;
        const pre = node("pre");
        pre.append(node("code", body.join("\n")));
        root.append(pre);
        continue;
      }
      if (!line.trim()) {
        index++;
        continue;
      }
      const heading = HEADING.exec(line);
      if (heading) {
        const level = Math.min(6, heading[1].length + 2);
        const element = node(`h${level}`);
        element.append(inline((heading[2] || "").replace(/\s+#+\s*$/, "").trim()));
        root.append(element);
        index++;
      } else if (RULE.test(line)) {
        root.append(node("hr"));
        index++;
      } else if (tableStart(lines, index)) {
        const wrap = node("div", undefined, "md-table");
        const table = node("table");
        const head = node("tr");
        for (const cell of cells(line)) head.append(inlineLines([cell], "th"));
        const body = node("tbody");
        index += 2;
        while (index < lines.length && lines[index].includes("|") && lines[index].trim()) {
          const row = node("tr");
          for (const cell of cells(lines[index++])) row.append(inlineLines([cell], "td"));
          body.append(row);
        }
        const thead = node("thead");
        thead.append(head);
        table.append(thead, body);
        wrap.append(table);
        root.append(wrap);
      } else if (QUOTE.test(line)) {
        const quoted = [];
        while (index < lines.length && QUOTE.test(lines[index]))
          quoted.push(lines[index++].replace(QUOTE, ""));
        const quote = node("blockquote");
        quote.append(...markdown(quoted.join("\n")).childNodes);
        root.append(quote);
      } else if (ITEM.test(line)) {
        const [element, end] = list(lines, index);
        root.append(element);
        index = end;
      } else {
        const paragraph = [line.trim()];
        index++;
        while (index < lines.length && lines[index].trim() && !startsBlock(lines, index))
          paragraph.push(lines[index++].trim());
        root.append(inlineLines(paragraph, "p"));
      }
    }
    return root;
  }
  function linked(tag, text, className) {
    const element = node(tag, undefined, className);
    element.append(linkify(text));
    return element;
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
  // A view names a Workspaces-tab row or a herdr workspace (anything Collie can open);
  // only a herdr workspace has agents to message.
  function where(target) {
    return target.key ? { key: target.key } : { workspace: target.workspace };
  }
  // The issue, pull request or Sentry issue the checkout serves, as {label, url, title}.
  function renderLinks(links) {
    byId("ws-viewer-links").replaceChildren(
      ...(links || [])
        .filter((link) => /^https?:\/\//.test(link?.url || ""))
        .map((link) => {
          const anchor = external(link.label, link.url);
          if (link.title) anchor.title = link.title;
          return anchor;
        }),
    );
  }
  function openViewer(entry, mode) {
    viewer = { entry, mode, scope: "branch", base: null, session: null, data: null, nodes: [] };
    byId("ws-viewer-title").textContent = entry.name || "Workspace";
    renderLinks(entry.links);
    resetViewer("Loading…");
    openComposer(entry);
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
    button.onclick = () => {
      change(() => {});
      if (viewer?.entry.workspace) void fetchAgents(viewer.entry);
    };
    return button;
  }
  function choice(label, options, value, onchange) {
    const wrap = node("label", undefined, "ws-viewer-choice");
    const select = document.createElement("select");
    select.setAttribute("aria-label", label);
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
      "." +
      (data.truncated ? " The diff was cut short at its size limit." : "") +
      (files && draft ? " Click a line to comment on it." : "");
    const content = byId("ws-viewer-content");
    content.replaceChildren();
    if (data.commits.length) {
      const commits = node("details", undefined, "ws-commits");
      commits.append(
        node("summary", `${data.commits.length} commit${data.commits.length === 1 ? "" : "s"}`),
      );
      for (const commit of data.commits) {
        const line = node(commit.body ? "details" : "div", undefined, "ws-commit");
        const heading = commit.body ? node("summary") : line;
        heading.append(node("code", commit.sha.slice(0, 9)), " ", linked("span", commit.subject));
        heading.append(node("small", `${commit.author} · ${date(commit.time)}`, "pr-meta"));
        if (commit.body) {
          line.append(heading, linked("div", commit.body, "ws-commit-body"));
        }
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
      // Line numbers follow the hunk headers: removed lines count on the old side,
      // added lines on the new side, context lines on both.
      let oldLine = 0;
      let newLine = 0;
      for (const line of file.lines) {
        const hunk = line.match(/^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/);
        const kind = line.startsWith("@@")
          ? "ws-hunk"
          : line.startsWith("+")
            ? "ws-add"
            : line.startsWith("-")
              ? "ws-del"
              : "";
        const span = node("span", `${line}\n`, kind || undefined);
        if (hunk) {
          oldLine = Number(hunk[1]);
          newLine = Number(hunk[2]);
        } else if (line.startsWith("+")) {
          Object.assign(span.dataset, { side: "new", line: newLine++ });
        } else if (line.startsWith("-")) {
          Object.assign(span.dataset, { side: "old", line: oldLine++ });
        } else if (line.startsWith(" ") || line === "") {
          Object.assign(span.dataset, { side: "new", line: newLine++ });
          oldLine++;
        }
        lines.append(span);
        for (const comment of draft?.comments || []) {
          if (
            comment.path === file.path &&
            comment.side === span.dataset.side &&
            String(comment.line) === span.dataset.line
          )
            lines.append(commentNote(comment));
        }
      }
      pre.append(lines);
      if (draft) {
        pre.onclick = (event) => commentOn(file, event);
        pre.tabIndex = 0;
        pre.setAttribute(
          "aria-label",
          `Diff of ${file.path}. Use the arrow keys to choose a line and Enter to comment on it.`,
        );
        pre.onkeydown = (event) => diffKeys(file, event);
        for (const line of pre.querySelectorAll("span[data-line]")) line.tabIndex = -1;
      }
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
      item.append(linked("pre", entry.output, entry.error ? "ws-tool-error" : "ws-tool-output"));
    }
  }
  function updateTool(item, entry) {
    const summary = item.querySelector("summary");
    summary.querySelector(".badge")?.remove();
    if (entry.error) summary.append(badge("Error", "red"));
    toolOutput(item, entry);
  }
  // What each kind of user turn is called; Claude records commands, their output and
  // background-task reports as user turns too.
  const USER_KINDS = {
    prompt: "Prompt",
    command: "Command",
    output: "Command output",
    notification: "Notification",
  };
  function transcriptEntry(entry, agent) {
    if (entry.role === "tool") {
      const item = node("details", undefined, "ws-tool");
      const summary = node("summary");
      summary.append(node("strong", entry.name));
      if (entry.about) summary.append(node("span", entry.about, "ws-tool-about"));
      if (entry.brief) summary.append(node("code", entry.brief, "ws-tool-brief"));
      if (entry.error) summary.append(badge("Error", "red"));
      item.append(summary);
      if (entry.input) item.append(linked("pre", entry.input, "ws-tool-input"));
      toolOutput(item, entry);
      return item;
    }
    const kind = entry.role === "user" ? entry.kind || "prompt" : null;
    if (kind === "notification") {
      const item = node("div", undefined, "ws-note");
      item.append(node("strong", "Notification"), " ", linkify(entry.text));
      return item;
    }
    const item = node("div", undefined, `ws-msg ws-${entry.role} ws-kind-${kind || "reply"}`);
    item.append(node("strong", kind ? USER_KINDS[kind] || "Prompt" : agent, "ws-msg-who"));
    if (kind === "command" || kind === "output")
      item.append(linked("pre", entry.text, "ws-msg-code"));
    else if (kind) item.append(linked("div", entry.text, "ws-msg-text"));
    else item.append(markdown(entry.text, "ws-msg-text ws-md"));
    return item;
  }

  // Comments and follow-up messages ---------------------------------------------
  // A draft (comments on diff lines plus a message) belongs to a herdr workspace's
  // checkout and is kept in this browser until it is sent, so closing loses nothing.
  // With no agent running, a message resumes one of the checkout's recorded sessions.
  const MAX_MESSAGE = 32000;
  let draft = null;
  let agents = [];
  let sessions = [];
  let sending = false; // A send in flight keeps the button disabled through refreshes.
  function draftKey(entry) {
    return `ws-viewer-draft:${entry.workspace}`;
  }
  function loadDraft(entry) {
    try {
      const saved = JSON.parse(localStorage.getItem(draftKey(entry)) || "null");
      if (saved && Array.isArray(saved.comments) && typeof saved.message === "string") return saved;
    } catch {
      // Storage may be unavailable or hold something else; start empty.
    }
    return { comments: [], message: "", path: null };
  }
  function storeDraft(entry, value) {
    try {
      if (value.comments.length || value.message) {
        localStorage.setItem(draftKey(entry), JSON.stringify(value));
      } else localStorage.removeItem(draftKey(entry));
    } catch {
      // Without storage the draft lasts as long as the page.
    }
  }
  function saveDraft() {
    if (draft && viewer) storeDraft(viewer.entry, draft);
  }
  function openComposer(entry) {
    const form = byId("ws-message");
    byId("ws-message-status").textContent = "";
    agents = [];
    sessions = [];
    if (!entry.workspace) {
      // A checkout without a herdr workspace has no agent to talk to.
      draft = null;
      form.hidden = true;
      return;
    }
    form.hidden = false;
    draft = loadDraft(entry);
    byId("ws-message-text").value = draft.message;
    renderComments();
    renderAgents("Looking for agents…");
    void fetchAgents(entry);
  }
  async function fetchAgents(entry) {
    try {
      const value = await getJSON("/api/workspace-agents", { workspace: entry.workspace });
      if (viewer?.entry !== entry) return;
      if (draft.path && draft.path !== value.path) {
        // The workspace ID now names another checkout: its old draft does not apply.
        draft = { comments: [], message: "", path: value.path };
        storeDraft(entry, draft);
        byId("ws-message-text").value = "";
        byId("ws-viewer-content")
          .querySelectorAll(".ws-comment-note")
          .forEach((note) => note.remove());
        renderComments();
        byId("ws-message-status").textContent = "A saved draft for another checkout was discarded.";
      }
      draft.path = value.path;
      agents = value.agents;
      sessions = value.sessions;
      renderAgents();
    } catch (error) {
      if (viewer?.entry === entry) renderAgents(error.message);
    }
  }
  function agentLabel(agent) {
    return (
      `${agent.agent || "agent"} · ${agent.status || "unknown"}` +
      (agent.title ? ` · ${agent.title}` : "")
    );
  }
  function renderAgents(message) {
    const holder = byId("ws-message-agent");
    // A refresh keeps the agent or session the reader picked, while it is still listed.
    const picked = holder.querySelector("select")?.value;
    const keep = (values) => (values.includes(picked) ? picked : values[0]);
    holder.replaceChildren();
    const send = byId("ws-message-send");
    send.textContent = "Send to agent";
    send.disabled = true;
    if (message) {
      holder.append(node("small", message, "pr-meta"));
      return;
    }
    if (agents.length) {
      send.disabled = sending;
      holder.append(
        agents.length === 1
          ? node("small", `To ${agentLabel(agents[0])}`, "pr-meta")
          : choice(
              "Agent",
              agents.map((agent) => [agent.pane, agentLabel(agent)]),
              keep(agents.map((agent) => agent.pane)),
              () => {},
            ),
      );
      return;
    }
    if (!sessions.length) {
      holder.append(
        node("small", "No agent is running and no session was recorded here.", "pr-meta"),
      );
      return;
    }
    send.disabled = sending;
    send.textContent = "Resume and send";
    holder.append(
      node("small", "No agent is running; the message resumes this session:", "pr-meta"),
      choice(
        "Resume",
        sessions.map((session) => [
          session.id,
          // The babysit watcher resumes a watched session itself; resuming it is refused.
          sessionLabel(session) + (session.watched ? " (babysit watch)" : ""),
        ]),
        keep(sessions.map((session) => session.id)),
        () => {},
      ),
    );
  }
  function removeComment(id) {
    draft.comments = draft.comments.filter((comment) => comment.id !== id);
    saveDraft();
    byId("ws-viewer-content")
      .querySelectorAll(".ws-comment-note")
      .forEach((note) => {
        if (note.dataset.comment === id) note.remove();
      });
    renderComments();
  }
  function commentNote(comment) {
    const note = node("div", undefined, "ws-comment-note");
    note.dataset.comment = comment.id;
    note.append(node("strong", "Comment"), " ", node("span", comment.text));
    const remove = node("button", "Remove");
    remove.type = "button";
    remove.onclick = (event) => {
      event.stopPropagation();
      removeComment(comment.id);
    };
    note.append(remove);
    return note;
  }
  function openCommentBox(file, span) {
    if (!draft || span.nextElementSibling?.classList.contains("ws-comment-box")) return;
    const box = node("div", undefined, "ws-comment-box");
    const field = document.createElement("textarea");
    field.rows = 3;
    field.setAttribute("aria-label", `Comment on ${file.path} line ${span.dataset.line}`);
    const add = node("button", "Add comment");
    add.type = "button";
    const cancel = node("button", "Cancel");
    cancel.type = "button";
    cancel.onclick = () => {
      box.remove();
      span.focus();
    };
    add.onclick = () => {
      const text = field.value.trim();
      if (!text) return;
      const comment = {
        id: `${Date.now()}-${Math.random().toString(36).slice(2)}`,
        path: file.path,
        side: span.dataset.side,
        line: Number(span.dataset.line),
        code: span.textContent.replace(/\n$/, ""),
        text,
      };
      draft.comments.push(comment);
      saveDraft();
      box.replaceWith(commentNote(comment));
      renderComments();
      span.focus();
    };
    box.addEventListener("click", (inner) => inner.stopPropagation());
    box.addEventListener("keydown", (inner) => inner.stopPropagation());
    box.append(field, add, cancel);
    span.after(box);
    field.focus();
    // The composer sits over the bottom of the dialog; keep the new box in view.
    box.scrollIntoView({ block: "center" });
  }
  // A single click on a line comments on it; a double or triple click selects text.
  let pendingComment = null;
  function commentOn(file, event) {
    clearTimeout(pendingComment);
    const span = event.target.closest("span[data-line]");
    if (!span || event.detail > 1) return;
    pendingComment = setTimeout(() => {
      if (!String(window.getSelection())) openCommentBox(file, span);
    }, 250);
  }
  // Keyboard: the diff is one tab stop; arrows move between lines, Enter comments.
  function diffKeys(file, event) {
    const lines = [...event.currentTarget.querySelectorAll("span[data-line]")];
    const at = lines.indexOf(document.activeElement);
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      const next = at < 0 ? 0 : at + (event.key === "ArrowDown" ? 1 : -1);
      lines[Math.max(0, Math.min(lines.length - 1, next))]?.focus();
    } else if ((event.key === "Enter" || event.key === " ") && at >= 0) {
      event.preventDefault();
      openCommentBox(file, lines[at]);
    }
  }
  function renderComments() {
    const list = byId("ws-message-comments");
    list.replaceChildren();
    if (!draft?.comments.length) return;
    const head = node("div", undefined, "ws-comment-head");
    head.append(
      node(
        "strong",
        `${draft.comments.length} diff comment${draft.comments.length === 1 ? "" : "s"} will be sent`,
      ),
    );
    const clear = node("button", "Clear comments");
    clear.type = "button";
    clear.onclick = () => {
      for (const comment of [...draft.comments]) removeComment(comment.id);
    };
    head.append(clear);
    list.append(head);
    for (const comment of draft.comments) {
      const item = node("div", undefined, "ws-comment-chip");
      const where = `${comment.path}:${comment.line}${comment.side === "old" ? " (removed)" : ""}`;
      const remove = node("button", "Remove");
      remove.type = "button";
      remove.setAttribute("aria-label", `Remove comment on ${where}`);
      remove.onclick = () => removeComment(comment.id);
      item.append(node("code", where), " ", node("span", comment.text), remove);
      list.append(item);
    }
  }
  // The message the agent receives: each comment with its place and code, then the text.
  function compose(comments, message) {
    const parts = [];
    if (comments.length) {
      parts.push("Review comments on your changes:");
      for (const comment of comments) {
        const side = comment.side === "old" ? "removed line" : "line";
        parts.push(`${comment.path}, ${side} ${comment.line}:\n> ${comment.code}\n${comment.text}`);
      }
    }
    if (message.trim()) parts.push(message.trim());
    return parts.join("\n\n");
  }
  async function sendMessage(event) {
    event.preventDefault();
    if (!viewer || !draft || sending) return;
    const entry = viewer.entry;
    const comments = [...draft.comments];
    const message = byId("ws-message-text").value;
    const text = compose(comments, message);
    const status = byId("ws-message-status");
    if (!text) {
      status.textContent = "Write a message or add a comment first.";
      return;
    }
    if (text.length > MAX_MESSAGE) {
      status.textContent = `The message is ${text.length.toLocaleString()} characters; the limit is ${MAX_MESSAGE.toLocaleString()}.`;
      return;
    }
    const picked = byId("ws-message-agent").querySelector("select")?.value;
    const body = agents.length
      ? { workspace: entry.workspace, pane: picked || agents[0].pane, text }
      : { workspace: entry.workspace, resume: picked || sessions[0]?.id, text };
    sending = true;
    byId("ws-message-send").disabled = true;
    status.textContent = body.resume ? "Resuming the session…" : "Sending…";
    try {
      const value = await deliver(body);
      if (value.error) {
        if (viewer?.entry === entry) status.textContent = value.error;
        return;
      }
      clearSent(entry, comments, message);
      if (viewer?.entry !== entry) return;
      status.textContent =
        value.warning ||
        (value.resumed
          ? `Resumed the session in ${value.pane} and sent the message.`
          : `Sent to the agent in ${value.pane}.`);
      // The transcript shows the new turn soon after.
      if (viewer.mode === "transcript")
        setTimeout(() => {
          if (viewer?.entry === entry && viewer.mode === "transcript") change(() => {});
        }, 3000);
    } finally {
      sending = false;
      if (viewer?.entry === entry) void fetchAgents(entry);
    }
  }
  // POST the message; an error result says whether anything may have been typed.
  async function deliver(body) {
    let response;
    try {
      response = await fetch("/api/workspace-message", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Babysit-Action": "workspace-message" },
        body: JSON.stringify(body),
      });
    } catch {
      // The request may have arrived before the connection dropped.
      return {
        error: "Delivery unknown: the connection dropped; check Collie before resending.",
      };
    }
    let value;
    try {
      value = await response.json();
    } catch {
      return { error: `Delivery unknown: HTTP ${response.status}; check Collie before resending.` };
    }
    if (!response.ok || value.error)
      return { error: `Not sent: ${value.error || response.status}` };
    return value;
  }
  // Clear exactly what was sent, from storage and from whichever draft is now open (the
  // dialog may have been reopened meanwhile); later edits stay.
  function clearSent(entry, comments, message) {
    const sent = new Set(comments.map((comment) => comment.id));
    const prune = (value) => {
      value.comments = value.comments.filter((comment) => !sent.has(comment.id));
      if (value.message === message) value.message = "";
      return value;
    };
    storeDraft(entry, prune(loadDraft(entry)));
    if (!draft || viewer?.entry.workspace !== entry.workspace) return;
    prune(draft);
    saveDraft();
    if (byId("ws-message-text").value === message) byId("ws-message-text").value = "";
    byId("ws-viewer-content")
      .querySelectorAll(".ws-comment-note")
      .forEach((note) => {
        if (sent.has(note.dataset.comment)) note.remove();
      });
    renderComments();
  }
  byId("ws-message").addEventListener("submit", sendMessage);
  window.addEventListener("focus", () => {
    if (viewer?.entry.workspace && byId("ws-viewer").open) void fetchAgents(viewer.entry);
  });
  byId("ws-message-text").addEventListener("input", () => {
    if (!draft) return;
    draft.message = byId("ws-message-text").value;
    saveDraft();
  });
  byId("ws-view-diff").onclick = () => setMode("diff");
  byId("ws-view-transcript").onclick = () => setMode("transcript");
  byId("ws-viewer-close").onclick = () => byId("ws-viewer").close();
  // Full screen is a per-browser preference, kept across viewers and reloads.
  const FULL_KEY = "ws-viewer-full";
  function setFull(full) {
    byId("ws-viewer").classList.toggle("ws-viewer-full", full);
    byId("ws-viewer-full").setAttribute("aria-pressed", String(full));
  }
  try {
    setFull(localStorage.getItem(FULL_KEY) === "1");
  } catch {
    setFull(false);
  }
  byId("ws-viewer-full").onclick = () => {
    const full = !byId("ws-viewer").classList.contains("ws-viewer-full");
    setFull(full);
    try {
      if (full) localStorage.setItem(FULL_KEY, "1");
      else localStorage.removeItem(FULL_KEY);
    } catch {
      // Without storage the choice lasts until the page reloads.
    }
  };
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
  // Diff and Transcript buttons for a target: {workspace} (a herdr workspace ID) and/or
  // {key} (a Workspaces-tab row), plus a name for the dialog title and the links it shows.
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
