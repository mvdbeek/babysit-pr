/* Optional experiment: owns its requests, rendering, publishing and errors. */
(() => {
  const byId = (id) => document.getElementById(`sentry-${id}`);
  const BUCKETS = {
    critical: ["Critical", "red"],
    high: ["High", "amber"],
    medium: ["Medium", "blue"],
    low: ["Low", ""],
  };
  const TRIAGE = {
    queued: "Triage queued",
    running: "Triage running…",
    failed: "Triage failed",
    skipped: "Triage skipped",
  };
  const PUBLISHING = {
    sanitizing: "The sanitizer is reviewing the draft…",
    ready: "Review the sanitized text below before creating the issue.",
    blocked: "Publishing is blocked: the sanitizer found content that is not safe to publish.",
    creating: "Creating the GitHub issue…",
    created: "The GitHub issue was created.",
    failed: "Publishing failed.",
  };
  const WRITEBACK = {
    pending: "Linking the issue back in Sentry…",
    ok: "Linked back in Sentry.",
    failed: "The link could not be written back to Sentry.",
    disabled: "Writing the link back to Sentry is disabled.",
  };
  let snapshot = null;
  let busy = false;
  let limit = 50;
  const rowErrors = new Map();
  const acting = new Set();
  // Rows are rebuilt on every poll; keep expanded triage notes open across rebuilds.
  const openNotes = new Set();
  // The open publish dialog: which group, and which sanitized text was last prefilled.
  let publishKey = null;
  let prefilled = null;
  let submitting = false;
  let publishPoll = null;

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
  function button(text, callback) {
    const element = node("button", text);
    element.type = "button";
    element.onclick = callback;
    return element;
  }
  function number(value) {
    return Number.isFinite(value) ? value.toLocaleString() : "—";
  }
  function date(value) {
    return value ? new Date(value * 1000).toLocaleString() : "never";
  }
  function ago(value) {
    const time = Date.parse(value);
    if (!time) return "unknown";
    const age = Math.max(0, (Date.now() - time) / 1000);
    return age < 60
      ? "just now"
      : age < 3600
        ? `${Math.floor(age / 60)}m ago`
        : age < 86400
          ? `${Math.floor(age / 3600)}h ago`
          : `${Math.floor(age / 86400)}d ago`;
  }
  function groups() {
    return (snapshot && snapshot.groups) || [];
  }
  function group(key) {
    return groups().find((entry) => entry.key === key) || null;
  }
  function matches(entry) {
    const term = byId("search").value.trim().toLowerCase();
    const scope = byId("repo").value;
    const bucket = byId("severity").value;
    const state = byId("state").value;
    const projects = entry.projects || [];
    if (scope.startsWith("repo:") && entry.repo !== scope.slice(5)) return false;
    if (scope.startsWith("project:") && !projects.some((p) => p.slug === scope.slice(8)))
      return false;
    if (bucket !== "all" && entry.bucket !== bucket) return false;
    if (state !== "all" && entry.substatus !== state) return false;
    if (!term) return true;
    return [
      entry.title,
      entry.culprit,
      entry.short_id,
      entry.repo,
      ...projects.flatMap((p) => [p.slug, p.short_id]),
    ]
      .filter(Boolean)
      .some((value) => String(value).toLowerCase().includes(term));
  }
  function visible() {
    return groups()
      .filter(matches)
      .sort(
        (a, b) =>
          (b.score || 0) - (a.score || 0) ||
          (Date.parse(b.last_seen) || 0) - (Date.parse(a.last_seen) || 0),
      );
  }
  // Extra servers point the agent at the same crash under their own short ids.
  function sentryTask(entry) {
    const others = (entry.projects || []).filter(
      (p) => p.short_id && p.short_id !== entry.short_id,
    );
    const link =
      typeof entry.permalink === "string" && entry.permalink.startsWith("https://")
        ? ` (${entry.permalink})`
        : "";
    const also = others.length
      ? `, also seen as ${others.map((p) => `${p.short_id} on ${p.slug}`).join(", ")}`
      : "";
    return (
      `Investigate and fix Sentry issue ${entry.short_id}${link}${also}. ` +
      "Use the Sentry MCP tools to read the stack trace, breadcrumbs and recent events, find the root cause, implement the fix and add a regression test. " +
      `Put \`Fixes ${entry.short_id}\` in the commit message. ` +
      "Do not resolve or change the issue in Sentry. Summarize the root cause, the fix and the validation."
    );
  }
  function handle(entry) {
    const item = {
      id: `sentry:${entry.key}`,
      kind: "sentry",
      repo: entry.repo,
      short_id: entry.short_id,
      title: entry.title,
      url: entry.permalink,
    };
    void window.workspaceDialog(item, sentryTask(entry));
  }
  function severityCell(entry) {
    const cell = node("td", undefined, "sentry-severity");
    const [text, tone] = BUCKETS[entry.bucket] || BUCKETS.low;
    const head = node("div", undefined, "sentry-score");
    head.append(badge(text, tone), node("span", `Score ${entry.score ?? "—"}`));
    cell.append(head);
    if (entry.reasons?.length) {
      const chips = node("div", undefined, "sentry-chips");
      for (const reason of entry.reasons) chips.append(node("span", reason, "sentry-chip"));
      cell.append(chips);
    }
    const triage = entry.triage;
    if (triage?.status === "done") {
      const llm = badge(
        `LLM: ${triage.severity || "unknown"}`,
        `sentry-llm-badge${triage.disagrees ? " sentry-disagrees" : ""}`,
      );
      llm.title = triage.disagrees
        ? `The LLM rates this ${triage.severity}, two or more steps from the score (confidence ${triage.confidence || "unknown"}).`
        : `LLM triage, confidence ${triage.confidence || "unknown"}`;
      cell.append(llm);
    } else if (triage && TRIAGE[triage.status]) {
      const note = node("small", TRIAGE[triage.status], "pr-meta");
      if (triage.error) note.title = triage.error;
      cell.append(note);
    }
    return cell;
  }
  function triageNotes(key, triage) {
    const details = node("details", undefined, "sentry-notes");
    details.open = openNotes.has(key);
    details.ontoggle = () => {
      if (details.open) openNotes.add(key);
      else openNotes.delete(key);
    };
    details.append(node("summary", "Triage notes"));
    if (triage.summary) details.append(node("p", triage.summary));
    for (const [label, value] of [
      ["Likely cause", triage.likely_cause],
      ["User impact", triage.user_impact],
      ["Suggested area", triage.suggested_area],
      ["Confidence", triage.confidence],
    ]) {
      if (!value) continue;
      const line = node("p");
      line.append(node("strong", `${label}: `), value);
      details.append(line);
    }
    if (triage.reasons?.length) {
      const list = node("ul");
      for (const reason of triage.reasons) list.append(node("li", reason));
      details.append(list);
    }
    return details;
  }
  function issueCell(entry) {
    const cell = node("td", undefined, "sentry-issue");
    cell.append(anchor(entry.short_id || "Sentry issue", entry.permalink));
    cell.append(node("span", entry.title || "Untitled issue", "pr-title sentry-title"));
    if (entry.culprit) cell.append(node("small", entry.culprit, "pr-meta"));
    const facts = [
      entry.level,
      entry.substatus || entry.status,
      entry.unhandled ? "unhandled" : null,
    ].filter(Boolean);
    if (facts.length) cell.append(node("small", facts.join(" · "), "pr-meta"));
    if (entry.triage?.status === "done") cell.append(triageNotes(entry.key, entry.triage));
    return cell;
  }
  function serversCell(entry) {
    const cell = node("td", undefined, "sentry-servers");
    for (const project of entry.projects || []) {
      const line = node("div");
      const link = anchor(project.slug, project.permalink);
      if (project.short_id) link.title = project.short_id;
      line.append(link);
      if (project.release) line.append(node("small", project.release, "pr-meta"));
      cell.append(line);
    }
    return cell;
  }
  function impactCell(entry) {
    const cell = node("td", undefined, "sentry-impact");
    cell.append(node("strong", `${number(entry.events_24h)}/24h`));
    cell.append(node("small", `${number(entry.count)} events`, "pr-meta"));
    cell.append(node("small", `${number(entry.users)} users`, "pr-meta"));
    return cell;
  }
  function seenCell(entry) {
    const cell = node("td", undefined, "sentry-seen");
    for (const [label, value] of [
      ["Last", entry.last_seen],
      ["First", entry.first_seen],
    ]) {
      const line = node("small", `${label} ${ago(value)}`, "pr-meta");
      if (Date.parse(value)) line.title = new Date(value).toLocaleString();
      cell.append(line);
    }
    return cell;
  }
  function actionsCell(entry) {
    const td = node("td", undefined, "pr-actions sentry-actions");
    const cell = node("div", undefined, "sentry-action-list");
    td.append(cell);
    cell.append(button("Handle", () => handle(entry)));
    const issue = entry.publish?.url || entry.publish?.existing?.url;
    if (issue) cell.append(anchor("Open GitHub issue", issue));
    else cell.append(button("Publish to GitHub", () => openPublish(entry)));
    const llm = snapshot.llm || {};
    const retriage = button(acting.has(entry.key) ? "Queuing…" : "Re-triage", () =>
      retriageGroup(entry),
    );
    const pending = ["queued", "running"].includes(entry.triage?.status);
    retriage.disabled = !llm.triage_enabled || pending || acting.has(entry.key);
    if (!llm.triage_enabled) retriage.title = "LLM triage is disabled in the experiment config.";
    else if (pending) retriage.title = "A triage run is already queued.";
    cell.append(retriage);
    if (rowErrors.has(entry.key)) {
      const error = node("small", rowErrors.get(entry.key));
      error.setAttribute("role", "alert");
      cell.append(error);
    }
    return td;
  }
  function row(entry) {
    const line = node("tr");
    line.dataset.key = entry.key;
    line.append(
      severityCell(entry),
      issueCell(entry),
      serversCell(entry),
      impactCell(entry),
      seenCell(entry),
      actionsCell(entry),
    );
    return line;
  }
  function scopeOptions() {
    const select = byId("repo");
    const repos = [...new Set(groups().map((entry) => entry.repo))].filter(Boolean).sort();
    const slugs = [
      ...new Set(groups().flatMap((entry) => (entry.projects || []).map((p) => p.slug))),
    ]
      .filter(Boolean)
      .sort();
    const values = ["all", ...repos.map((r) => `repo:${r}`), ...slugs.map((s) => `project:${s}`)];
    // Rebuilding on every refresh would disturb the shared searchable picker.
    if ([...select.options].map((option) => option.value).join("\u0000") === values.join("\u0000"))
      return;
    const chosen = select.value;
    select.replaceChildren();
    for (const value of values) {
      const option = node(
        "option",
        value === "all"
          ? "All repositories"
          : value.startsWith("repo:")
            ? value.slice(5)
            : `Server ${value.slice(8)}`,
      );
      option.value = value;
      select.append(option);
    }
    select.value = values.includes(chosen) ? chosen : "all";
  }
  function llmLine(llm) {
    const line = byId("llm");
    line.hidden = !llm;
    if (!llm) return;
    const parts = [
      llm.worker?.alive ? "LLM worker running" : "LLM worker not running (start the supervisor)",
    ];
    if (llm.gate)
      parts.push(
        `Gate ${llm.gate.state || "unknown"}${llm.gate.reason ? `: ${llm.gate.reason}` : ""}`,
      );
    if (llm.budget)
      parts.push(`Budget ${number(llm.budget.used_today)}/${number(llm.budget.per_day)} today`);
    if (!llm.triage_enabled) parts.push("Triage disabled");
    if (!llm.sanitizer_enabled) parts.push("Sanitizer disabled");
    line.textContent = parts.join(" · ");
    line.classList.toggle("sentry-warn", !llm.worker?.alive || llm.gate?.state !== "open");
  }
  function render() {
    const data = snapshot;
    if (!data) return;
    byId("tab").hidden = !data.enabled;
    const status = byId("status");
    const notices = byId("notices");
    notices.replaceChildren();
    if (!data.enabled) {
      byId("list").replaceChildren();
      byId("more").hidden = true;
      byId("empty").hidden = true;
      byId("llm").hidden = true;
      byId("count").textContent = "0";
      status.textContent =
        data.error || "Sentry overview is disabled. Enable it in experiments/sentry/config.json.";
      return;
    }
    scopeOptions();
    const shown = visible();
    byId("count").textContent = String(shown.length);
    byId("list").replaceChildren(...shown.slice(0, limit).map(row));
    byId("more").hidden = shown.length <= limit;
    byId("more-button").textContent = `Show 50 more (${shown.length - limit} remaining)`;
    byId("empty").hidden = shown.length > 0;
    byId("empty").textContent = groups().length
      ? "No Sentry issue matches these filters."
      : data.synced_at
        ? "No unresolved Sentry issues in the configured projects."
        : "No Sentry snapshot yet.";
    const projects = data.projects || [];
    status.textContent =
      `${data.loading ? "Loading / refreshing… " : ""}${data.stale ? "Stale snapshot — " : ""}` +
      `${groups().length} issue groups from ${projects.length} Sentry projects` +
      `${data.organization ? ` in ${data.organization}` : ""}. ` +
      (data.synced_at ? `Observed ${date(data.synced_at)}.` : "No observation yet.");
    llmLine(data.llm);
    for (const warning of [data.error, ...(data.warnings || [])].filter(Boolean).slice(0, 6)) {
      notices.append(node("p", warning, "alert"));
    }
    renderPublish();
  }
  function merge(updated) {
    if (!snapshot || !updated?.key) return;
    snapshot = {
      ...snapshot,
      groups: groups().map((entry) => (entry.key === updated.key ? updated : entry)),
    };
  }
  async function act(body) {
    const response = await fetch("/api/sentry-action", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Babysit-Action": "sentry-action" },
      body: JSON.stringify(body),
    });
    const value = await response.json();
    if (!response.ok || value.error) throw new Error(value.error || `HTTP ${response.status}`);
    merge(value.group);
    return value;
  }
  async function retriageGroup(entry) {
    acting.add(entry.key);
    rowErrors.delete(entry.key);
    render();
    try {
      await act({ action: "retriage", key: entry.key });
    } catch (error) {
      rowErrors.set(entry.key, error.message);
    } finally {
      acting.delete(entry.key);
      render();
    }
  }

  function list(heading, items, format) {
    const box = node("div", undefined, "sentry-review-list");
    if (!items?.length) return box;
    box.append(node("h3", heading));
    const entries = node("ul");
    for (const item of items) entries.append(format(item));
    box.append(entries);
    return box;
  }
  function publishable(publish) {
    return (
      publish?.status === "ready" &&
      publish.sanitized?.safe_to_publish === true &&
      !(publish.findings || []).length &&
      !publish.existing
    );
  }
  function renderPublish() {
    const dialog = byId("publish-dialog");
    if (!dialog.open || !publishKey) return;
    const entry = group(publishKey);
    if (!entry) {
      byId("publish-status").textContent = "This Sentry issue is no longer in the snapshot.";
      byId("publish-create").disabled = true;
      return;
    }
    const publish = entry.publish;
    byId("publish-meta").textContent = `${entry.repo} · ${entry.short_id} · ${entry.title}`;
    byId("publish-status").textContent = publish
      ? PUBLISHING[publish.status] || publish.status
      : "Preparing the draft…";
    byId("publish-public").hidden = !publish?.repo_public;
    const existing = byId("publish-existing");
    existing.hidden = !publish?.existing;
    existing.replaceChildren();
    if (publish?.existing) {
      existing.append(
        "An issue for this Sentry group already exists: ",
        anchor(
          publish.existing.number ? `#${publish.existing.number}` : "Open issue",
          publish.existing.url,
        ),
        ". No new issue will be created.",
      );
    }
    const findings = publish?.findings || [];
    byId("publish-findings").hidden = !findings.length;
    byId("publish-findings").textContent = findings.length
      ? `The pattern check still finds sensitive content, so publishing is blocked: ${findings.map((f) => `${f.kind} (${f.excerpt})`).join("; ")}`
      : "";
    byId("publish-draft").hidden = !publish?.draft;
    byId("publish-draft-text").textContent = publish?.draft
      ? `${publish.draft.title || ""}\n\n${publish.draft.body || ""}`
      : "";
    const sanitized = publish?.sanitized;
    const done = publish?.status === "created";
    byId("publish-form").hidden = !sanitized || !!publish.existing || done;
    if (sanitized) {
      // Prefill once per sanitizer result so a poll never overwrites the user's edits.
      const version = `${publishKey}\u0000${sanitized.title}\u0000${sanitized.body}`;
      if (prefilled !== version) {
        prefilled = version;
        byId("publish-title").value = sanitized.title || "";
        byId("publish-body").value = sanitized.body || "";
        byId("publish-reviewed").checked = false;
      }
      byId("publish-redactions").replaceChildren(
        list("Redactions", sanitized.redactions, (item) =>
          node("li", `${item.kind}: ${item.original_excerpt} → ${item.replacement}`),
        ),
      );
      const concerns = [...(sanitized.concerns || [])];
      if (sanitized.safe_to_publish === false)
        concerns.unshift("The sanitizer marked this text as not safe to publish.");
      byId("publish-concerns").replaceChildren(
        list("Concerns", concerns, (item) => node("li", item)),
      );
    }
    const create = byId("publish-create");
    create.hidden = !!publish?.existing || done;
    create.disabled = submitting || !publishable(publish) || !byId("publish-reviewed").checked;
    create.textContent = submitting ? "Creating…" : "Create GitHub issue";
    byId("publish-redraft").hidden = !["blocked", "failed", "uncertain"].includes(publish?.status);
    if (publish?.error && !byId("publish-error").textContent)
      byId("publish-error").textContent = publish.error;
    const result = byId("publish-result");
    result.replaceChildren();
    if (done) {
      const created = node("p", "Created ");
      created.append(anchor(publish.number ? `#${publish.number}` : "the issue", publish.url));
      result.append(created, node("p", WRITEBACK[publish.writeback] || publish.writeback || ""));
      if (publish.writeback === "failed")
        result.append(button("Retry Sentry link", () => publishAction("writeback-retry")));
    }
    schedulePublishPoll(publish);
  }
  // Poll faster while the server is still working on this dialog's group.
  function schedulePublishPoll(publish) {
    clearTimeout(publishPoll);
    const working =
      ["sanitizing", "creating"].includes(publish?.status) ||
      (publish?.status === "created" && publish.writeback === "pending");
    if (!working || !byId("publish-dialog").open) return;
    publishPoll = setTimeout(() => void refresh(), 1500);
  }
  async function publishAction(action, extra = {}) {
    const key = publishKey;
    byId("publish-error").textContent = "";
    try {
      await act({ action, key, ...extra });
    } catch (error) {
      if (publishKey === key) byId("publish-error").textContent = error.message;
    }
    render();
  }
  function openPublish(entry) {
    publishKey = entry.key;
    prefilled = null;
    submitting = false;
    byId("publish-title").value = "";
    byId("publish-body").value = "";
    byId("publish-reviewed").checked = false;
    byId("publish-error").textContent = "";
    const dialog = byId("publish-dialog");
    if (!dialog.open) dialog.showModal();
    renderPublish();
    if (!entry.publish || ["failed", "uncertain"].includes(entry.publish.status))
      void publishAction("publish-draft");
  }
  async function createIssue() {
    const entry = group(publishKey);
    if (!entry || !publishable(entry.publish) || !byId("publish-reviewed").checked) return;
    submitting = true;
    renderPublish();
    try {
      await publishAction("publish-create", {
        title: byId("publish-title").value,
        body: byId("publish-body").value,
      });
    } finally {
      submitting = false;
      renderPublish();
    }
  }

  async function refresh(force) {
    if (busy) return;
    busy = true;
    try {
      const response = await fetch(`/api/sentry${force ? "?refresh=1" : ""}`);
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      snapshot = await response.json();
      render();
    } catch (error) {
      byId("status").textContent =
        `Sentry API unavailable: ${error.message}. Displayed issues may be stale.`;
    } finally {
      busy = false;
    }
  }
  function refilter() {
    limit = 50;
    render();
  }
  byId("search").oninput = refilter;
  byId("repo").onchange = refilter;
  byId("severity").onchange = refilter;
  byId("state").onchange = refilter;
  byId("refresh").onclick = () => refresh(true);
  byId("more-button").onclick = () => {
    limit += 50;
    render();
  };
  byId("publish-reviewed").onchange = renderPublish;
  byId("publish-create").onclick = createIssue;
  byId("publish-redraft").onclick = () => {
    prefilled = null;
    byId("publish-reviewed").checked = false;
    void publishAction("publish-draft");
  };
  byId("publish-close").onclick = () => byId("publish-dialog").close();
  byId("publish-dialog").onclose = () => {
    clearTimeout(publishPoll);
    publishKey = null;
  };
  window.addEventListener("sentry-visible", () => refresh());
  setInterval(() => {
    if (!document.hidden && !byId("panel").hidden) void refresh();
  }, 5000);
  void refresh();
})();
