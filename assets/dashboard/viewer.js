/* Diff and agent transcript dialog for any checkout Collie can open, with diff comments
   and follow-up messages sent to the workspace's agent. */
(() => {
  const byId = (id) => document.getElementById(id);
  // The dialog shows one workspace at a time, re-fetched on demand. Every change starts
  // a request; only the newest one may render.
  let viewer = null;
  let viewerRequest = 0;
  let viewerInflight = 0;
  // Run once no load is in flight any more (a reload that one could have overtaken).
  let afterLoad = null;
  let opener = null;
  // The open workspace in Collie, once its agents are read; text that sends the
  // reader to Collie links there.
  let collieUrl = null;

  function node(tag, text, className) {
    const element = document.createElement(tag);
    if (text !== undefined) element.textContent = text;
    if (className) element.className = className;
    return element;
  }
  function tell(element, text) {
    element.replaceChildren(window.collieText(text, collieUrl));
  }
  function badge(text, tone) {
    return node("span", text, `badge ${tone || ""}`.trim());
  }
  function date(value) {
    return value ? new Date(value * 1000).toLocaleString() : "never";
  }
  // When a transcript entry was recorded: the time of day, with the date unless it is
  // today; the full date and time on hover.
  function stamp(value) {
    const when = value ? new Date(value) : null;
    if (!when || Number.isNaN(when.getTime())) return null;
    const time = when.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
    const now = new Date();
    let text = time;
    if (when.toDateString() !== now.toDateString()) {
      const day = { month: "short", day: "numeric" };
      if (when.getFullYear() !== now.getFullYear()) day.year = "numeric";
      text = `${when.toLocaleDateString([], day)}, ${time}`;
    }
    const element = node("time", text, "ws-time");
    element.dateTime = when.toISOString();
    element.title = when.toLocaleString();
    return element;
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
    byId("ws-live").hidden = true;
    for (const [id, mode] of [
      ["ws-view-diff", "diff"],
      ["ws-view-transcript", "transcript"],
    ]) {
      byId(id).setAttribute("aria-selected", String(viewer?.mode === mode));
    }
  }
  // A view names a Workspaces-tab row or a herdr workspace (anything Collie can open).
  function where(target) {
    return target.key ? { key: target.key } : { workspace: target.workspace };
  }
  // Whom a message goes to: the herdr workspace, or a Workspaces-tab row listed while
  // none was open, whose recorded sessions can still be resumed.
  function recipient(target) {
    return target.workspace ? { workspace: target.workspace } : { key: target.key };
  }
  function messageable(target) {
    return Boolean(target.workspace || target.key);
  }
  // The issue, pull request or Sentry issue the checkout serves, as {label, url, title}.
  function renderLinks(links) {
    byId("ws-viewer-links").replaceChildren(
      ...[...(links || []), ...(collieUrl ? [{ label: "Open in Collie", url: collieUrl }] : [])]
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
    collieUrl = null;
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
      if (!viewerInflight && afterLoad) {
        const next = afterLoad;
        afterLoad = null;
        next();
      }
    }
  }
  function refreshButton() {
    const button = node("button", "Refresh");
    button.type = "button";
    button.onclick = () => {
      change(() => {});
      if (viewer && messageable(viewer.entry)) void fetchAgents(viewer.entry);
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
    const account = session.claude_account ? ` (${session.claude_account})` : "";
    return `${date(session.updated)} · ${agent}${account} · ${session.title || session.id}`;
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
    renderLive();
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
          if (entry.questions) {
            const card = transcriptEntry(entry, agentName(data));
            viewer.nodes[index].replaceWith(card);
            viewer.nodes[index] = card;
          } else updateTool(viewer.nodes[index], entry);
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
    // Async questions can stay open while subsequent tool calls fill the transcript.
    if (current.entries.some(answerable)) void fetchAgents(viewer.entry);
    renderLive();
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
    if (entry.error) summary.insertBefore(badge("Error", "red"), summary.querySelector(".ws-time"));
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
  // Questions an agent asked ------------------------------------------------------
  // Answers go through the matching session's question dialog, for both agents.
  function openQuestion(entry) {
    if (entry.role !== "tool" || !entry.questions || entry.error || entry.answeredHere)
      return false;
    if (entry.output == null) return true;
    if (entry.name === "request_user_input_async") {
      try {
        const value = JSON.parse(entry.output);
        return value.accepted === true && Object.keys(value).length === 1;
      } catch {
        return false;
      }
    }
    return false;
  }
  function answerable(entry) {
    return (
      openQuestion(entry) && /^(AskUserQuestion|request_user_input(_async)?)$/.test(entry.name)
    );
  }
  // Claude joins a multiple-choice answer's labels with ", ", which labels may contain.
  function chosenLabels(question, answer) {
    if (answer === undefined) return [];
    const labels = question.options.map((option) => option.label);
    if (labels.includes(answer)) return [answer];
    if (!question.multi) return [];
    // Read the answer left to right, trying the longest labels first.
    const longest = [...labels].sort((a, b) => b.length - a.length);
    const found = [];
    let rest = answer;
    while (rest) {
      const label = longest.find((l) => rest === l || rest.startsWith(`${l}, `));
      if (!label) return found;
      found.push(label);
      rest = rest.slice(label.length + 2);
    }
    return found;
  }
  // What decides how an open question is drawn: whether, and to whom, it can be answered.
  function questionAgent(entry) {
    const session = viewer?.data?.session?.id;
    const kind = entry.name === "AskUserQuestion" ? "claude" : "codex";
    return agents.find((a) => a.agent === kind && a.session && a.session === session);
  }
  function answerState(entry) {
    const agent = questionAgent(entry);
    return `${Boolean(draft)}|${agent?.pane}|${agent?.status}`;
  }
  function questionCard(entry) {
    const card = node("div", undefined, "ws-question");
    card.dataset.tool = entry.id || "";
    card.dataset.state = answerState(entry);
    const head = node("div", undefined, "ws-question-head");
    head.append(node("strong", entry.questions.length === 1 ? "Question" : "Questions"));
    const open = openQuestion(entry);
    if (entry.answers || entry.answeredHere) head.append(badge("Answered", "green"));
    else if (entry.error) head.append(badge("Not answered", "red"));
    else if (open && questionAgent(entry)?.status === "blocked")
      head.append(badge("Waiting for an answer", "amber"));
    const when = stamp(entry.time);
    if (when) head.append(when);
    card.append(head);
    // A dialog that can be answered here shows its questions in the form instead.
    const controls = answerable(entry) ? answerControls(entry) : null;
    const answering = controls?.tagName === "FORM";
    entry.questions.forEach((question) => {
      if (answering) return;
      const section = node("section", undefined, "ws-question-item");
      const title = node("p", undefined, "ws-question-text");
      if (question.header) title.append(badge(question.header), " ");
      title.append(linkify(question.question));
      section.append(title);
      const answer = entry.answers?.[question.question];
      const chosen = chosenLabels(question, answer);
      const list = node("ul", undefined, "ws-question-options");
      for (const option of question.options) {
        const item = node("li");
        if (chosen.includes(option.label)) {
          item.className = "ws-chosen";
          item.append(node("span", "✓ ", "ws-chosen-mark"));
        }
        item.append(node("strong", option.label));
        if (option.description) item.append(" ", node("span", option.description, "pr-meta"));
        list.append(item);
      }
      if (question.options.length) section.append(list);
      if (question.multi) section.append(node("small", "Any number can be chosen.", "pr-meta"));
      if (answer !== undefined && !chosen.length) {
        const typed = node("p", undefined, "ws-question-typed");
        typed.append(node("strong", "Answered: "), linkify(answer));
        section.append(typed);
      }
      card.append(section);
    });
    if (entry.error && entry.output) card.append(linked("p", entry.output, "pr-meta"));
    if (controls) card.append(controls);
    return card;
  }
  function answerControls(entry, target = null) {
    const session = target?.session || viewer?.data?.session?.id;
    const agent = target || questionAgent(entry);
    if (!draft) return node("p", "Answer it in the agent's terminal.", "pr-meta");
    if (!agent)
      return node("p", "No running agent in this workspace is on this session.", "pr-meta");
    if (agent.status !== "blocked")
      return node("p", "The agent is not waiting on this question right now.", "pr-meta");
    const form = node("form", undefined, "ws-answer");
    const fields = entry.questions.map((question, index) => {
      const set = node("fieldset");
      const legend = node("legend");
      if (question.header) legend.append(badge(question.header), " ");
      legend.append(node("strong", question.question));
      if (question.multi) legend.append(" ", node("small", "any number", "pr-meta"));
      set.append(legend);
      const name = `ws-answer-${entry.id}-${index}`;
      const inputs = question.options.map((option, choice) => {
        const label = node("label");
        const input = document.createElement("input");
        input.type = question.multi ? "checkbox" : "radio";
        input.name = name;
        input.value = String(choice);
        const text = node("span");
        text.append(option.label);
        if (option.description) text.append(" ", node("small", option.description, "pr-meta"));
        label.append(input, text);
        set.append(label);
        return input;
      });
      let other = null;
      let text = null;
      if (!question.multi) {
        const label = node("label", undefined, "ws-answer-other");
        other = document.createElement("input");
        other.type = "radio";
        other.name = name;
        text = document.createElement("input");
        text.type = "text";
        text.maxLength = 2000;
        text.placeholder = "Something else";
        text.setAttribute("aria-label", `Other answer to: ${question.question}`);
        text.oninput = () => (other.checked = Boolean(text.value.trim()) || other.checked);
        label.append(other, text);
        set.append(label);
      }
      form.append(set);
      return { question, inputs, other, text };
    });
    const status = node("p", undefined, "ws-answer-status");
    status.setAttribute("role", "status");
    const submit = node("button", `Answer in ${agent.pane}`);
    submit.type = "submit";
    form.append(submit, status);
    form.oninput = () => (status.textContent = "");
    form.onsubmit = async (event) => {
      event.preventDefault();
      const answers = [];
      for (const { question, inputs, other, text } of fields) {
        if (other?.checked) {
          if (!text.value.trim()) {
            status.textContent = `Write your answer to “${question.question}”.`;
            return;
          }
          answers.push({ text: text.value.trim() });
          continue;
        }
        const chosen = inputs.filter((input) => input.checked).map((input) => Number(input.value));
        if (!chosen.length) {
          status.textContent = `Answer “${question.question}” first.`;
          return;
        }
        answers.push({ options: chosen });
      }
      submit.disabled = true;
      status.textContent = "Answering…";
      const current = viewer.entry;
      const value = await answerRequest({
        workspace: current.workspace,
        pane: agent.pane,
        session,
        tool: entry.id,
        answers,
      });
      if (viewer?.entry !== current) return;
      if (value.error) {
        tell(status, value.error);
        submit.disabled = false;
        return;
      }
      entry.answeredHere = true;
      form
        .closest(".ws-question")
        ?.querySelector(".ws-question-head .badge")
        ?.replaceWith(badge("Answered", "green"));
      status.textContent = `Answered in ${value.pane}.`;
      // Refresh synchronous results; async transcripts can retain just an acknowledgment.
      setTimeout(() => {
        if (viewer?.entry === current) void fetchAgents(current);
        if (viewer?.entry === current && viewer.mode === "transcript")
          void loadViewer({ after: Math.max(viewer.data.start, viewer.data.total - 20) });
      }, 2500);
    };
    return form;
  }
  async function answerRequest(body) {
    try {
      const response = await fetch("/api/workspace-answer", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Babysit-Action": "workspace-answer" },
        body: JSON.stringify(body),
      });
      const value = await response.json();
      return response.ok ? value : { error: value.error || `HTTP ${response.status}` };
    } catch {
      return { error: "The connection dropped; check the question in Collie." };
    }
  }
  // Agents arrive after the transcript: open questions are drawn again with them.
  function refreshQuestions() {
    if (viewer?.mode !== "transcript" || !viewer.data?.entries) return;
    viewer.data.entries.forEach((entry, index) => {
      const shown = viewer.nodes[index];
      if (!answerable(entry) || !shown || shown.dataset.state === answerState(entry)) return;
      // A form being filled in is left alone.
      if (
        shown.contains(document.activeElement) ||
        shown.querySelector("form :checked, form input[type=text]:not(:placeholder-shown)")
      )
        return;
      const card = questionCard(entry);
      shown.replaceWith(card);
      viewer.nodes[index] = card;
    });
  }
  function transcriptEntry(entry, agent) {
    if (entry.role === "tool" && entry.questions) return questionCard(entry);
    if (entry.role === "tool") {
      const item = node("details", undefined, "ws-tool");
      const summary = node("summary");
      summary.append(node("strong", entry.name));
      if (entry.about) summary.append(node("span", entry.about, "ws-tool-about"));
      if (entry.brief) summary.append(node("code", entry.brief, "ws-tool-brief"));
      if (entry.error) summary.append(badge("Error", "red"));
      const when = stamp(entry.time);
      if (when) summary.append(when);
      item.append(summary);
      if (entry.input) item.append(linked("pre", entry.input, "ws-tool-input"));
      toolOutput(item, entry);
      return item;
    }
    const kind = entry.role === "user" ? entry.kind || "prompt" : null;
    if (kind === "notification") {
      const item = node("div", undefined, "ws-note");
      const when = stamp(entry.time);
      if (when) item.append(when);
      item.append(node("strong", "Notification"), " ", linkify(entry.text));
      return item;
    }
    const item = node("div", undefined, `ws-msg ws-${entry.role} ws-kind-${kind || "reply"}`);
    const head = node("div", undefined, "ws-msg-head");
    head.append(node("strong", kind ? USER_KINDS[kind] || "Prompt" : agent, "ws-msg-who"));
    const when = stamp(entry.time);
    if (when) head.append(when);
    item.append(head);
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
  let changingDocker = false;
  let sending = false; // A send in flight keeps the button disabled through refreshes.
  let files = null; // The composer's attachments, kept with the draft.
  function draftKey(entry) {
    return `ws-viewer-draft:${entry.workspace || entry.key}`;
  }
  function loadDraft(entry) {
    try {
      const saved = JSON.parse(localStorage.getItem(draftKey(entry)) || "null");
      if (saved && Array.isArray(saved.comments) && typeof saved.message === "string")
        return { ...saved, files: Array.isArray(saved.files) ? saved.files : [] };
    } catch {
      // Storage may be unavailable or hold something else; start empty.
    }
    return { comments: [], message: "", path: null, files: [] };
  }
  function storeDraft(entry, value) {
    try {
      if (value.comments.length || value.message || value.files?.length) {
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
    byId("ws-agent-interaction").replaceChildren();
    delete byId("ws-agent-interaction").dataset.state;
    byId("ws-message-status").textContent = "";
    byId("ws-live-status").textContent = "";
    agents = [];
    sessions = [];
    if (!messageable(entry)) {
      // A checkout without a herdr workspace or a row has no agent to talk to.
      draft = null;
      files?.destroy();
      files = null;
      form.hidden = true;
      return;
    }
    form.hidden = false;
    draft = loadDraft(entry);
    byId("ws-message-text").value = draft.message;
    files?.destroy();
    files = window.dashboardAttachments.picker({
      files: draft.files,
      pasteTarget: byId("ws-message-text"),
      dropTarget: form,
      onchange: (list) => {
        if (!draft || viewer?.entry !== entry) return;
        draft.files = list;
        saveDraft();
      },
    });
    byId("ws-message-files").replaceChildren(files.element);
    renderComments();
    renderAgents("Looking for agents…");
    void fetchAgents(entry);
  }
  // Agents are read by polls, refreshes and actions alike; only the newest answer counts.
  let agentsRequest = 0;
  async function fetchAgents(entry) {
    const token = ++agentsRequest;
    try {
      const value = await getJSON("/api/workspace-agents", recipient(entry));
      if (viewer?.entry !== entry || token !== agentsRequest) return;
      if (draft.path && draft.path !== value.path) {
        // The workspace ID now names another checkout: its old draft does not apply.
        draft = { comments: [], message: "", path: value.path, files: [] };
        files?.clear();
        storeDraft(entry, draft);
        byId("ws-message-text").value = "";
        byId("ws-viewer-content")
          .querySelectorAll(".ws-comment-note")
          .forEach((note) => note.remove());
        renderComments();
        byId("ws-message-status").textContent = "A saved draft for another checkout was discarded.";
      }
      draft.path = value.path;
      if (value.workspace && value.workspace !== entry.workspace) {
        // Reopened under a new ID: follow it, and move the draft along.
        storeDraft(entry, { comments: [], message: "", files: [] });
        entry.workspace = value.workspace;
        saveDraft();
      }
      if ((value.url || null) !== collieUrl) {
        collieUrl = value.url || null;
        renderLinks(entry.links);
      }
      agents = value.agents;
      sessions = value.sessions;
      renderAgents();
      refreshQuestions();
      renderLive();
    } catch (error) {
      if (viewer?.entry === entry && token === agentsRequest) renderAgents(error.message);
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
    // Polls redraw only on a change, so an open agent choice is never swapped under
    // the reader's finger.
    const key = JSON.stringify([message, agents, sessions, sending, Boolean(changingDocker)]);
    if (holder.dataset.state === key) return;
    holder.dataset.state = key;
    // A refresh keeps the agent or session the reader picked, while it is still listed.
    const picked = holder.querySelector("select")?.value;
    const keep = (values) => (values.includes(picked) ? picked : values[0]);
    holder.replaceChildren();
    const send = byId("ws-message-send");
    const interaction = byId("ws-agent-interaction");
    send.textContent = "Send to agent";
    send.disabled = true;
    byId("ws-docker").hidden = true;
    if (message) {
      interaction.replaceChildren();
      delete interaction.dataset.state;
      const note = node("small", undefined, "pr-meta");
      tell(note, message);
      holder.append(note);
      return;
    }
    if (agents.length) {
      send.disabled = sending || changingDocker;
      holder.append(
        agents.length === 1
          ? node("small", `To ${agentLabel(agents[0])}`, "pr-meta")
          : choice(
              "Agent",
              agents.map((agent) => [agent.pane, agentLabel(agent)]),
              keep(agents.map((agent) => agent.pane)),
              () => {
                renderDocker();
                renderInteraction();
              },
            ),
      );
      renderDocker();
      renderInteraction();
      return;
    }
    interaction.replaceChildren();
    delete interaction.dataset.state;
    if (!sessions.length) {
      holder.append(
        node("small", "No agent is running and no session was recorded here.", "pr-meta"),
      );
      return;
    }
    send.disabled = sending || changingDocker;
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
  function renderInteraction() {
    const agent = selectedAgent();
    // herdr can call an agent blocked while its screen shows an empty prompt box.
    const blocked = agent?.status === "blocked" && !agent.interaction?.idle;
    byId("ws-message-send").disabled = sending || changingDocker || blocked;
    const holder = byId("ws-agent-interaction");
    const state = JSON.stringify([agent?.pane, agent?.session, blocked, agent?.interaction]);
    if (holder.dataset.state === state) return;
    holder.dataset.state = state;
    holder.replaceChildren();
    if (!blocked) return;
    holder.append(
      node("p", "The agent is waiting for an answer. Your message is saved.", "pr-meta"),
    );
    const question = agent.interaction?.question;
    const choices = agent.interaction?.choices;
    if (question) {
      const card = node("div", undefined, "ws-question");
      card.append(node("strong", "Live question from the agent"), answerControls(question, agent));
      holder.append(card);
    } else if (choices) {
      const card = node("div", undefined, "ws-question");
      card.append(node("strong", "Live dialog from the agent"), choiceControls(choices, agent));
      holder.append(card);
    } else {
      if (agent.interaction?.screen) {
        const screen = node("pre", agent.interaction.screen, "ws-agent-screen");
        holder.append(screen);
        // A dialog sits at the bottom of the screen.
        screen.scrollTop = screen.scrollHeight;
      }
      const note = node("p", undefined, "pr-meta");
      tell(note, "Answer this dialog in Collie, then refresh to send your message.");
      holder.append(note);
    }
  }
  // A dialog's options, each picked with its number key once the server sees the same
  // dialog on the agent's screen.
  function choiceControls(choices, agent) {
    const box = node("div", undefined, "ws-answer");
    if (choices.text) box.append(node("pre", choices.text, "ws-agent-screen"));
    const status = node("p", undefined, "ws-answer-status");
    status.setAttribute("role", "status");
    const buttons = choices.options.map((option) => {
      const button = node("button", option.label);
      button.type = "button";
      button.onclick = async () => {
        buttons.forEach((b) => (b.disabled = true));
        status.textContent = "Choosing…";
        const current = viewer.entry;
        const value = await choiceRequest({
          workspace: current.workspace,
          pane: agent.pane,
          session: agent.session,
          dialog: choices.id,
          option: option.key,
        });
        if (viewer?.entry !== current) return;
        if (value.error) {
          status.textContent = value.error;
          // The key may have gone through: a retry could answer the next, identical
          // dialog unseen, so only a clean refusal allows one.
          if (!value.uncertain) buttons.forEach((b) => (b.disabled = false));
          return;
        }
        status.textContent = `Chose “${option.label}” in ${value.pane}.`;
        setTimeout(() => {
          if (viewer?.entry === current) void fetchAgents(current);
        }, 1500);
      };
      return button;
    });
    const list = node("div", undefined, "ws-choices");
    list.append(...buttons);
    box.append(list, status);
    return box;
  }
  async function choiceRequest(body) {
    try {
      const response = await fetch("/api/workspace-choose", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Babysit-Action": "workspace-choose" },
        body: JSON.stringify(body),
      });
      const value = await response.json();
      if (response.ok) return value;
      // 400 is a refusal before any key; anything else may have pressed it.
      const uncertain = response.status !== 400;
      const error = value.error || `HTTP ${response.status}`;
      return {
        error: uncertain ? `${error}. It may have gone through; refresh.` : error,
        uncertain,
      };
    } catch {
      return {
        error: "The connection dropped; it may have gone through. Check the dialog in Collie.",
        uncertain: true,
      };
    }
  }
  function selectedAgent() {
    const picked = byId("ws-message-agent").querySelector("select")?.value;
    return agents.find((agent) => agent.pane === picked) || agents[0];
  }
  // Running agents ------------------------------------------------------------------
  // Above a transcript: each agent running in the workspace and what it is doing, a
  // way to its session's transcript, and Stop, which interrupts its turn as Esc would.
  const ACTIVITY = {
    working: ["Working", "amber"],
    blocked: ["Waiting for an answer", "amber"],
    idle: ["Idle", "green"],
    done: ["Done", "green"],
  };
  let stopping = null; // The pane a stop is on its way to.
  function agentTitle(agent) {
    return { claude: "Claude", codex: "Codex" }[agent.agent] || agent.agent || "Agent";
  }
  function activity(agent) {
    if (agent.status === "blocked" && agent.interaction?.idle) return ACTIVITY.idle;
    return ACTIVITY[agent.status] || [agent.status || "Unknown", ""];
  }
  function renderLive() {
    const holder = byId("ws-live");
    const data = viewer?.mode === "transcript" ? viewer.data : null;
    const shown = data?.session?.id;
    const live = (id) => agents.find((agent) => agent.session && agent.session === id);
    // A session with a running agent says so in the session choice.
    const select = byId("ws-viewer-controls").querySelector('select[aria-label="Session"]');
    for (const option of select?.options || []) {
      const session = data?.sessions.find((s) => s.id === option.value);
      const agent = live(option.value);
      if (session)
        option.textContent = (agent ? `● ${activity(agent)[0]} · ` : "") + sessionLabel(session);
    }
    holder.hidden = !data || !(agents.length || byId("ws-live-status").textContent);
    if (holder.hidden) return;
    // Polls redraw only on a change, so a focused button stays focused.
    const list = byId("ws-live-agents");
    const key = JSON.stringify([
      shown,
      stopping,
      data.sessions.map((session) => session.id),
      agents.map((agent) => [agent.pane, agent.agent, agent.status, agent.session]),
    ]);
    if (list.dataset.state === key) return;
    list.dataset.state = key;
    list.replaceChildren(
      ...agents.map((agent) => {
        const [text, tone] = activity(agent);
        const row = node("div", undefined, "ws-live-agent");
        row.dataset.status = agent.status || "";
        row.append(
          badge(text, tone),
          node("strong", agentTitle(agent)),
          node("span", `in ${agent.pane}`, "pr-meta"),
        );
        if (agent.session && agent.session === shown) {
          row.append(node("small", "this transcript", "pr-meta"));
        } else if (data.sessions.some((session) => session.id === agent.session)) {
          const show = node("button", "Show its transcript");
          show.type = "button";
          show.onclick = () => change((state) => (state.session = agent.session));
          row.append(show);
        }
        if (agent.status === "working") {
          const stop = node("button", stopping === agent.pane ? "Stopping…" : "Stop", "ws-stop");
          stop.type = "button";
          stop.disabled = Boolean(stopping);
          stop.setAttribute("aria-label", `Stop ${agentTitle(agent)} in ${agent.pane}`);
          stop.onclick = () => void stopAgent(agent);
          row.append(stop);
        }
        return row;
      }),
    );
  }
  async function stopAgent(agent) {
    if (!viewer || stopping) return;
    const entry = viewer.entry;
    const status = byId("ws-live-status");
    stopping = agent.pane;
    status.textContent = "";
    renderLive();
    let message;
    try {
      const response = await fetch("/api/workspace-interrupt", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Babysit-Action": "workspace-interrupt" },
        body: JSON.stringify({
          workspace: entry.workspace,
          pane: agent.pane,
          session: agent.session ?? null,
        }),
      });
      const value = await response.json();
      message = value.error || value.warning || `Stopped ${agentTitle(agent)} in ${value.pane}.`;
    } catch {
      message = "The stop could not be confirmed; refresh or check Collie.";
    } finally {
      stopping = null;
    }
    if (viewer?.entry !== entry) {
      renderLive(); // Another viewer's Stop buttons were waiting on this one.
      return;
    }
    tell(status, message);
    await fetchAgents(entry);
    // The transcript records where the turn was cut off.
    const data = viewer?.entry === entry && viewer.mode === "transcript" ? viewer.data : null;
    if (data?.session && !viewerInflight)
      void loadViewer({ after: Math.max(data.start, data.total - 20) });
  }
  function renderDocker() {
    const agent = selectedAgent();
    byId("ws-docker").hidden = !agent;
    if (!agent) return;
    const checkbox = byId("ws-docker-enabled");
    const known = typeof agent.docker === "boolean";
    checkbox.checked =
      changingDocker && changingDocker.pane === agent.pane
        ? changingDocker.enabled
        : agent.docker === true;
    checkbox.indeterminate = !known;
    checkbox.disabled = changingDocker || sending || !known || !agent.session;
    tell(
      byId("ws-docker-status"),
      changingDocker
        ? "Applying Docker access…"
        : !known
          ? agent.docker_error || "Docker access is unknown."
          : "Changes require an idle agent and restart its session.",
    );
  }
  byId("ws-docker-enabled").onchange = async (event) => {
    const agent = selectedAgent();
    if (!viewer || !agent || changingDocker || sending) return;
    const entry = viewer.entry;
    const enabled = event.target.checked;
    changingDocker = { pane: agent.pane, enabled };
    renderAgents();
    const status = byId("ws-message-status");
    try {
      const response = await fetch("/api/workspace-docker", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Babysit-Action": "workspace-docker" },
        body: JSON.stringify({
          workspace: entry.workspace,
          pane: agent.pane,
          session: agent.session,
          enabled,
        }),
      });
      const value = await response.json();
      if (viewer?.entry !== entry) return;
      if (value.error) tell(status, value.error);
      else {
        agent.docker = value.docker;
        tell(status, value.warning || `Docker access ${value.docker ? "enabled" : "disabled"}.`);
      }
    } catch {
      if (viewer?.entry === entry)
        tell(status, "Docker access change could not be confirmed; refresh or check Collie.");
    } finally {
      changingDocker = false;
      if (viewer && messageable(viewer.entry)) await fetchAgents(viewer.entry);
    }
  };
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
    if (!viewer || !draft || sending || changingDocker) return;
    const entry = viewer.entry;
    const comments = [...draft.comments];
    const message = byId("ws-message-text").value;
    const text = compose(comments, message);
    const status = byId("ws-message-status");
    const attachments = files?.ids() || [];
    if (files?.busy()) {
      status.textContent = "Wait for the attachments to upload.";
      return;
    }
    if (!text && !attachments.length) {
      status.textContent = "Write a message, add a comment or attach a file first.";
      return;
    }
    if (text.length > MAX_MESSAGE) {
      status.textContent = `The message is ${text.length.toLocaleString()} characters; the limit is ${MAX_MESSAGE.toLocaleString()}.`;
      return;
    }
    const picked = byId("ws-message-agent").querySelector("select")?.value;
    const body = {
      ...recipient(entry),
      ...(agents.length
        ? { pane: picked || agents[0].pane }
        : { resume: picked || sessions[0]?.id }),
      text,
      ...(attachments.length ? { attachments } : {}),
    };
    sending = true;
    renderAgents();
    status.textContent = body.resume ? "Resuming the session…" : "Sending…";
    try {
      const value = await deliver(body);
      if (value.error) {
        if (viewer?.entry === entry) tell(status, value.error);
        return;
      }
      clearSent(entry, comments, message, attachments);
      if (viewer?.entry !== entry) return;
      tell(
        status,
        value.warning ||
          (value.resumed
            ? `Resumed the session in ${value.pane} and sent the message.`
            : `Sent to the agent in ${value.pane}.`),
      );
      // The transcript shows the new turn soon after; load just its tail, like the poll, so
      // expanded tool calls and the scroll position stay as they are. A load still running
      // then may predate the turn: look once more when it settles.
      if (viewer.mode === "transcript")
        setTimeout(() => {
          const reload = () => {
            if (viewer?.entry !== entry || viewer.mode !== "transcript") return;
            const data = viewer.data;
            void loadViewer(data?.session ? { after: Math.max(data.start, data.total - 20) } : {});
          };
          if (viewerInflight) afterLoad = reload;
          else reload();
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
  function clearSent(entry, comments, message, attachments = []) {
    const sent = new Set(comments.map((comment) => comment.id));
    const attachedSent = new Set(attachments);
    const prune = (value) => {
      value.comments = value.comments.filter((comment) => !sent.has(comment.id));
      if (value.message === message) value.message = "";
      value.files = (value.files || []).filter((file) => !attachedSent.has(file.id));
      return value;
    };
    storeDraft(entry, prune(loadDraft(entry)));
    if (!draft || !viewer || draftKey(viewer.entry) !== draftKey(entry)) return;
    prune(draft);
    saveDraft();
    if (attachments.length) files?.remove(attachments);
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
    if (viewer && messageable(viewer.entry) && byId("ws-viewer").open)
      void fetchAgents(viewer.entry);
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
  // A live transcript keeps up while it is open and its newest entries are shown, and
  // so do its running agents.
  let pollingAgents = false;
  setInterval(() => {
    if (viewer?.mode === "transcript" && messageable(viewer.entry) && !document.hidden) {
      if (!pollingAgents && !stopping) {
        pollingAgents = true;
        void fetchAgents(viewer.entry).finally(() => (pollingAgents = false));
      }
    }
    const data = viewer?.data;
    if (viewer?.mode !== "transcript" || !data || viewerInflight || document.hidden) return;
    // An agent waiting on a startup prompt (trusting the folder, allowing imports) has
    // no transcript yet: look again until its first turn records one.
    if (!data.session) void loadViewer();
    else if (data.start + data.entries.length >= data.total)
      void loadViewer({ after: Math.max(data.start, data.total - 20) });
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
