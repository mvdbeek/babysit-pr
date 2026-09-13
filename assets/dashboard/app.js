"use strict";
const $ = (id) => document.getElementById(id);
const ended = new Set(["closed", "stopped"]);
const attention = new Set(["blocked", "awaiting_release", "handoff"]);
let data = null,
  selected = null,
  tab = "checks",
  logKind = "agent",
  busy = false,
  detailKey = null;
const cancelling = new Set(),
  cancelErrors = new Map(),
  approving = new Set(),
  feedbackErrors = new Map();
function needsAttention(job) {
  return (
    attention.has(job.status) ||
    job.cleanup_ready ||
    (!ended.has(job.status) && job.pending_reviews > 0)
  );
}
const names = {
  watching: "Watching",
  running: "Repairing",
  blocked: "Blocked",
  awaiting_release: "Awaiting handoff",
  handoff: "Handing off",
  paused: "Paused",
  stopped: "Stopped",
  closed: "Closed",
};
function el(tag, text, cls) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (cls) node.className = cls;
  return node;
}
// Keep native values and change events as the contract with filtering and forms.
const searchableSelects = new WeakMap();
let selectSequence = 0;
let dismissSelect = null;
function syncSelect(select) {
  searchableSelects.get(select)?.sync();
}
function searchableSelect(select) {
  if (searchableSelects.has(select)) return;
  const key = `select-${++selectSequence}`;
  const labels = [...select.labels];
  const name =
    select.getAttribute("aria-label") || labels.map((l) => l.textContent.trim()).join(" ");
  const wrapper = el("span", undefined, "searchable-select");
  const field = el("span", undefined, "select-field");
  const input = el("input");
  input.id = `${key}-input`;
  input.type = "text";
  input.autocomplete = "off";
  input.spellcheck = false;
  input.setAttribute("role", "combobox");
  input.setAttribute("aria-label", name);
  input.setAttribute("aria-autocomplete", "list");
  input.setAttribute("aria-expanded", "false");
  input.setAttribute("aria-controls", `${key}-options`);
  const clear = el("button", "×", "select-clear");
  clear.type = "button";
  clear.setAttribute("aria-label", `Clear search for ${name}`);
  const toggle = el("button", "▾", "select-toggle");
  toggle.type = "button";
  toggle.tabIndex = -1;
  toggle.setAttribute("aria-label", `Show options for ${name}`);
  const value = el("span", undefined, "select-value");
  const popup = el("span", undefined, "select-popup");
  popup.hidden = true;
  const list = el("span", undefined, "select-options");
  list.id = `${key}-options`;
  list.setAttribute("role", "listbox");
  list.setAttribute("aria-label", name);
  const status = el("span", undefined, "select-status");
  status.setAttribute("role", "status");
  popup.append(list, status);
  select.before(wrapper);
  // Unnest the original label so it labels only the editable input, not its buttons.
  if (select.parentElement.tagName === "LABEL") {
    const label = select.parentElement;
    label.after(wrapper);
    wrapper.append(label);
  }
  for (const label of labels) label.htmlFor = input.id;
  select.hidden = true;
  select.removeAttribute("aria-label");
  select.tabIndex = -1;
  field.append(input, clear, toggle);
  wrapper.append(select, field, value, popup);
  let opened = false,
    query = "",
    active = null,
    matches = [];
  function draw() {
    const selectedOption = select.selectedOptions[0];
    const text = selectedOption?.label || "";
    input.placeholder = opened ? "Type to narrow…" : "Choose an option";
    input.value = opened ? query : text;
    input.title = text;
    input.disabled = select.disabled;
    toggle.disabled = select.disabled;
    input.setAttribute("aria-required", String(select.required));
    value.textContent = text;
    value.hidden = text.length < 32;
    clear.hidden = !opened || !query;
    popup.hidden = !opened;
    input.setAttribute("aria-expanded", String(opened));
    input.removeAttribute("aria-activedescendant");
    if (!opened) return;
    matches = [...select.options].filter(
      (option) =>
        !option.disabled && option.label.toLocaleLowerCase().includes(query.toLocaleLowerCase()),
    );
    if (!matches.some((option) => option.value === active)) active = matches[0]?.value ?? null;
    list.replaceChildren(
      ...matches.map((option, index) => {
        const row = el("span", option.label, "select-option");
        row.id = `${key}-option-${index}`;
        row.setAttribute("role", "option");
        row.setAttribute("aria-selected", String(option.value === select.value));
        if (option.value === active) {
          row.classList.add("active");
          input.setAttribute("aria-activedescendant", row.id);
        }
        row.onmousedown = (event) => event.preventDefault();
        row.onclick = () => choose(option);
        return row;
      }),
    );
    status.textContent = matches.length
      ? `${matches.length} option${matches.length === 1 ? "" : "s"}`
      : "No matches";
  }
  function close() {
    if (opened) dismissSelect = null;
    opened = false;
    query = "";
    active = null;
    draw();
  }
  function open() {
    if (opened || select.disabled) return;
    dismissSelect?.();
    dismissSelect = (target) => {
      if (!wrapper.contains(target)) close();
    };
    opened = true;
    active = select.value;
    draw();
  }
  function choose(option) {
    const changed = select.value !== option.value;
    select.value = option.value;
    input.setCustomValidity("");
    input.focus();
    close();
    if (changed) {
      select.dispatchEvent(new Event("input", { bubbles: true }));
      select.dispatchEvent(new Event("change", { bubbles: true }));
    }
  }
  input.onfocus = open;
  input.onclick = open;
  input.oncompositionstart = open;
  input.oninput = () => {
    const typed = input.value;
    open();
    query = typed;
    active = null;
    draw();
  };
  input.onkeydown = (event) => {
    if (event.isComposing) return;
    if (
      !opened &&
      !event.ctrlKey &&
      !event.metaKey &&
      !event.altKey &&
      (event.key.length === 1 || ["Backspace", "Delete"].includes(event.key))
    )
      open();
    if (event.key === "Escape" && opened) {
      event.preventDefault();
      event.stopPropagation(); // Close the choices before a containing dialog.
      close();
    } else if (event.key === "Enter" && opened) {
      event.preventDefault();
      const option = matches.find((option) => option.value === active);
      if (option) choose(option);
    } else if (["ArrowDown", "ArrowUp"].includes(event.key)) {
      event.preventDefault();
      const wasOpen = opened;
      open();
      if (wasOpen) {
        const index = matches.findIndex((option) => option.value === active);
        active =
          matches[
            Math.max(0, Math.min(matches.length - 1, index + (event.key === "ArrowDown" ? 1 : -1)))
          ]?.value ?? null;
        draw();
      }
      list.querySelector(".active")?.scrollIntoView({ block: "nearest" });
    } else if (event.key === "Tab") close();
  };
  clear.onmousedown = toggle.onmousedown = (event) => event.preventDefault();
  clear.onclick = () => {
    input.focus();
    query = "";
    active = select.value;
    draw();
  };
  toggle.onclick = () => {
    const wasOpen = opened;
    input.focus();
    if (wasOpen) close();
    else open();
  };
  wrapper.addEventListener("focusout", (event) => {
    if (!wrapper.contains(event.relatedTarget)) close();
  });
  select.addEventListener("change", close);
  select.addEventListener("invalid", (event) => {
    event.preventDefault();
    input.setCustomValidity("Choose an option from the list.");
    input.focus();
    input.reportValidity();
  });
  searchableSelects.set(select, { sync: draw });
  draw();
}

function badge(status) {
  const color =
    {
      watching: "green",
      running: "blue",
      blocked: "red",
      awaiting_release: "amber",
      handoff: "amber",
      paused: "amber",
    }[status] || "";
  return el("span", names[status] || status, "badge " + color);
}
function ago(value) {
  if (!value) return "Not polled yet";
  const age = Math.max(0, Date.now() / 1000 - value);
  return age < 10
    ? "just now"
    : age < 60
      ? `${Math.floor(age)}s ago`
      : age < 3600
        ? `${Math.floor(age / 60)}m ago`
        : `${Math.floor(age / 3600)}h ago`;
}
function closeOnBackdropClick(dialog) {
  // A click on the backdrop targets the dialog element itself but lands outside its box.
  dialog.onclick = (event) => {
    if (event.target !== dialog) return;
    const bounds = dialog.getBoundingClientRect();
    if (
      event.clientX < bounds.left ||
      event.clientX > bounds.right ||
      event.clientY < bounds.top ||
      event.clientY > bounds.bottom
    )
      dialog.close();
  };
}
function link(label, url) {
  const node = el("a", label);
  try {
    const parsed = new URL(url);
    if (parsed.protocol !== "https:") throw Error();
    node.href = parsed.href;
    node.target = "_blank";
    node.rel = "noopener noreferrer";
  } catch {
    return el("span", label, "name");
  }
  return node;
}
function ciSummary(job) {
  const box = el("div", undefined, "ci");
  const c = job.checks;
  if (!c || !(c.passed_count + c.failed_count + c.pending_count)) {
    box.textContent = "No passing, failing, or pending checks";
    return box;
  }
  for (const [key, label, cls] of [
    ["passed_count", "passed", "passed"],
    ["failed_count", "failed", "failed"],
    ["pending_count", "pending", "pending"],
  ])
    if (c[key]) box.append(el("span", `${c[key]} ${label}`, cls));
  return box;
}
function visibleJobs() {
  const q = $("search").value.toLowerCase();
  const filter = $("filter").value;
  return (data?.jobs || []).filter(
    (j) =>
      (filter === "all" ||
        (filter === "active" && !ended.has(j.status)) ||
        (filter === "ended" && ended.has(j.status)) ||
        (filter === "attention" && needsAttention(j))) &&
      `${j.repo} ${j.ci_repo} ${j.branch} ${j.number || ""} ${j.summary}`.toLowerCase().includes(q),
  );
}
function render() {
  if (!data) return;
  syncSelect($("filter"));
  const jobs = data.jobs;
  $("watching").textContent = jobs.filter((j) => j.status === "watching").length;
  const running = jobs.filter((j) => j.status === "running").length;
  $("running").textContent = data.daemon.max_workers
    ? `${running} / ${data.daemon.max_workers}`
    : running;
  $("attention").textContent = jobs.filter((j) => needsAttention(j)).length;
  $("repairs").textContent = jobs.reduce((n, j) => n + j.attempts, 0);
  const endedCount = jobs.filter((j) => ended.has(j.status)).length;
  $("ended").textContent = endedCount;
  $("show-ended").setAttribute("aria-label", `Show ${endedCount} ended watches`);
  const health = data.daemon.health;
  $("health").textContent =
    health === "healthy"
      ? "Watcher online"
      : health === "stale"
        ? "Heartbeat stale"
        : health === "offline"
          ? "Watcher offline"
          : "State unavailable";
  $("health").className = "badge " + (health === "healthy" ? "green" : "amber");
  $("alert").hidden = !data.error && health !== "stale";
  $("alert").textContent =
    data.error ||
    "The watcher heartbeat is stale. These are saved results; monitoring may be stopped.";
  $("updated").textContent =
    `Refreshed ${new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}`;
  $("state-home").textContent = data.home;
  const visible = visibleJobs();
  $("count").textContent = visible.length;
  $("list").replaceChildren();
  if (selected && !visible.some((j) => j.id === selected)) {
    selected = null;
    detailKey = null;
  }
  for (const job of visible) {
    const row = el("button", undefined, "watch" + (selected === job.id ? " selected" : ""));
    row.type = "button";
    row.setAttribute("aria-pressed", selected === job.id);
    row.setAttribute(
      "aria-label",
      `${job.repo}, ${job.branch || "pending first poll"}, ${names[job.status] || job.status}`,
    );
    const top = el("div", undefined, "watch-top");
    top.append(
      el(
        "span",
        `${job.repo}${job.kind === "pr" && job.number ? ` #${job.number}` : ""}`,
        "watch-title",
      ),
      watchBadges(job),
    );
    const bottom = el("div", undefined, "watch-bottom");
    bottom.append(ciSummary(job), el("span", ago(job.last_poll)));
    row.append(
      top,
      el(
        "div",
        `${job.kind === "branch" ? "Branch" : "PR"} · ${job.branch || "Awaiting first poll"}`,
        "branch",
      ),
      el("p", job.summary || "Waiting for the first observation.", "watch-summary"),
      bottom,
    );
    if (job.cleanup_ready) row.append(el("span", "Ready for cleanup", "badge amber"));
    else if (job.pending_reviews && !ended.has(job.status))
      row.append(el("span", `${job.pending_reviews} feedback items`, "badge amber"));
    row.onclick = () => {
      selected = job.id;
      detailKey = null;
      render();
    };
    $("list").append(row);
  }
  $("empty").hidden = visible.length > 0;
  $("empty-title").textContent = data.error
    ? "Watcher state is unavailable"
    : jobs.length
      ? "No matching watches"
      : "Nothing to watch yet";
  $("empty-text").textContent = data.error
    ? "Run this dashboard from a shell that can read the watcher’s state directory."
    : jobs.length
      ? "Try another filter or search."
      : "Ask an agent to babysit a PR or your fork’s branch CI. Registered watches will appear here.";
  renderDetail();
}
function fact(label, value, wide = false) {
  const node = el("div", undefined, "fact" + (wide ? " wide" : ""));
  node.append(el("small", label), el("span", value || "—"));
  return node;
}
function watchBadges(job) {
  const badges = el("div", undefined, "pr-badges");
  if (job.kind === "pr") {
    const label =
      job.pr_outcome === "merged"
        ? "Merged"
        : job.pr_outcome === "closed"
          ? "Closed"
          : job.draft === true
            ? "Draft"
            : job.draft === false
              ? "Ready for review"
              : "PR status pending";
    badges.append(el("span", label, "badge " + (label === "Ready for review" ? "blue" : "")));
  }
  badges.append(badge(job.status));
  return badges;
}
function renderDetail() {
  const job = data.jobs.find((j) => j.id === selected);
  if (!job) {
    if (detailKey !== "empty") {
      $("detail").replaceChildren();
      const p = el("div", undefined, "detail-placeholder");
      p.append(
        el("span", "↖"),
        el("h3", "A closer look"),
        el("p", "Select a watch to see its checks, repair budget, and latest logs."),
      );
      $("detail").append(p);
      detailKey = "empty";
    }
    return;
  }
  // Keep log selection, scroll, and keyboard focus stable across quiet refreshes.
  const key = JSON.stringify([
    job,
    tab,
    cancelling.has(job.id),
    cancelErrors.get(job.id),
    approving.has(job.id),
    feedbackErrors.get(job.id),
    workspaceInfo({ id: `watch:${job.id}` }),
  ]);
  if (key === detailKey) {
    if (tab === "logs") loadLog(job);
    return;
  }
  detailKey = key;
  const head = el("div", undefined, "detail-head");
  const top = el("div", undefined, "detail-top");
  top.append(
    el("span", job.kind === "branch" ? "BRANCH CI" : "PULL REQUEST", "eyebrow"),
    watchBadges(job),
  );
  const title = el("h2");
  title.append(link(job.branch || job.repo, job.url));
  head.append(top, title, el("p", job.summary));
  head.append(workspaceControls({ ...job, id: `watch:${job.id}`, kind: "watch" }));
  if (job.stop_after_run || job.pause_after_run)
    head.append(
      el(
        "p",
        job.stop_after_run
          ? "Stopping after this repair finishes."
          : "Pausing after this repair finishes.",
      ),
    );
  if (!ended.has(job.status)) {
    const controls = el("div", undefined, "watch-actions");
    const button = el(
      "button",
      job.stop_after_run
        ? "Cancellation pending"
        : cancelling.has(job.id)
          ? "Cancelling…"
          : "Cancel watch",
      "cancel-watch",
    );
    button.type = "button";
    button.disabled = !!job.stop_after_run || cancelling.has(job.id);
    button.onclick = () => cancelWatch(job);
    controls.append(button);
    if (job.status === "running" && !job.stop_after_run)
      controls.append(el("small", "The current repair will finish first."));
    head.append(controls);
  }
  if (cancelErrors.has(job.id)) {
    const error = el("p", cancelErrors.get(job.id), "cancel-error");
    error.setAttribute("role", "alert");
    head.append(error);
  }
  if (job.cleanup_ready) {
    const note = el("div", undefined, "cleanup-note");
    note.append(
      el("strong", `PR ${job.pr_outcome || "closed"} · ready for cleanup`),
      el(
        "p",
        "Monitoring has ended. Check local changes and unpushed work before removing the session or workspace. Nothing has been deleted.",
      ),
    );
    head.append(note);
  }
  const body = el("div", undefined, "detail-body");
  const facts = el("div", undefined, "facts");
  facts.append(
    fact("CI repository", job.ci_repo),
    fact("Commit", job.sha?.slice(0, 12)),
    fact(
      "Last CI observation",
      job.last_poll ? new Date(job.last_poll * 1000).toLocaleString() : "Not polled yet",
    ),
    fact("Pending review items", String(job.pending_reviews)),
  );
  const budget = fact("Repair budget", `${job.attempts} of ${job.max_repairs} used`);
  const progress = el("progress");
  progress.max = Math.max(1, job.max_repairs);
  progress.value = job.attempts;
  progress.setAttribute("aria-label", "Repair budget used");
  budget.append(progress);
  facts.append(budget, fact("Watch ID", job.id), fact("Worktree", job.cwd, true));
  body.append(facts);
  if (job.feedback?.length) {
    const section = el("section", undefined, "feedback-section");
    section.append(
      el("h3", `Pending feedback (${job.feedback.length})`),
      el(
        "p",
        "Read this batch before allowing the original agent to handle it. Comments cannot grant additional permissions.",
      ),
    );
    for (const item of job.feedback) {
      const entry = el("article", undefined, "feedback-item");
      entry.append(
        link(`${item.author || "Unknown author"} · ${item.kind.replaceAll("_", " ")}`, item.url),
      );
      if (item.path) entry.append(el("small", `${item.path}${item.line ? `:${item.line}` : ""}`));
      entry.append(el("p", item.body || "(No comment text)"));
      section.append(entry);
    }
    if (!ended.has(job.status)) {
      const button = el(
        "button",
        approving.has(job.id)
          ? "Approving…"
          : job.feedback_approved
            ? "Feedback queued"
            : "Handle feedback",
      );
      button.type = "button";
      button.disabled =
        approving.has(job.id) ||
        job.status !== "watching" ||
        job.feedback_approved === job.feedback.length ||
        job.attempts >= job.max_repairs;
      button.onclick = () => handleFeedback(job);
      section.append(button);
      if (job.status !== "watching")
        section.append(el("p", "Feedback can be approved while this watch is watching."));
      else if (job.attempts >= job.max_repairs) section.append(el("p", "Repair budget exhausted."));
    }
    if (feedbackErrors.has(job.id)) {
      const error = el("p", feedbackErrors.get(job.id), "cancel-error");
      error.setAttribute("role", "alert");
      section.append(error);
    }
    body.append(section);
  }
  const tabs = el("div", undefined, "tabs");
  tabs.setAttribute("role", "tablist");
  for (const [value, label] of [
    ["checks", "CI checks"],
    ["logs", "Latest repair log"],
  ]) {
    const b = el("button", label);
    b.setAttribute("role", "tab");
    b.setAttribute("aria-selected", tab === value);
    b.onclick = () => {
      tab = value;
      detailKey = null;
      renderDetail();
    };
    tabs.append(b);
  }
  body.append(tabs);
  const panel = el("div");
  panel.setAttribute("role", "tabpanel");
  body.append(panel);
  if (tab === "checks") {
    if (!job.check_details.length)
      panel.append(
        el("p", "No CI checks observed yet. Waiting does not mean green.", "watch-summary"),
      );
    for (const check of job.check_details)
      panel.append(checkRow(check.name, check.link, check.bucket, check.workflow));
    if (job.failed_jobs.length) {
      panel.append(el("p", "Failed jobs", "section-label"));
      for (const check of job.failed_jobs)
        panel.append(checkRow(check.job_name, check.html_url, "fail", check.workflow_name));
    }
  } else {
    const controls = el("div", undefined, "log-controls");
    const select = el("select");
    select.setAttribute("aria-label", "Repair log source");
    for (const [value, label] of [
      ["agent", "Agent output"],
      ["guardian", "Repair runner"],
      ["result", "Repair result"],
    ]) {
      const o = el("option", label);
      o.value = value;
      select.append(o);
    }
    select.value = logKind;
    select.onchange = () => {
      logKind = select.value;
      loadLog(job);
    };
    controls.append(select, el("small", "Latest attempt · last 64 KB"));
    panel.append(controls);
    const pre = el("pre", "Loading…");
    pre.id = "repair-log";
    panel.append(pre);
  }
  $("detail").replaceChildren(head, body);
  if (tab === "logs") loadLog(job);
}
function checkRow(name, url, bucket, workflow) {
  const row = el("div", undefined, "check");
  const label = el("div");
  label.append(link(name || "Unnamed check", url));
  if (workflow && workflow !== name) label.append(el("small", workflow));
  const labels = {
    pass: "Passed",
    fail: "Failed",
    pending: "Pending",
    skipping: "Skipped",
    cancel: "Cancelled",
  };
  const color = { pass: "green", fail: "red", pending: "amber" };
  row.append(
    label,
    el("span", labels[bucket] || bucket || "Unknown", "badge " + (color[bucket] || "")),
  );
  return row;
}
async function cancelWatch(job) {
  if (cancelling.has(job.id)) return;
  cancelling.add(job.id);
  cancelErrors.delete(job.id);
  renderDetail();
  try {
    const response = await fetch("/api/cancel", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Babysit-Action": "cancel" },
      body: JSON.stringify({ id: job.id }),
    });
    const result = await response.json();
    if (!response.ok) throw Error(result.error || `HTTP ${response.status}`);
    data.jobs = data.jobs.map((j) => (j.id === job.id ? result.job : j));
  } catch (error) {
    cancelErrors.set(
      job.id,
      `Could not confirm cancellation: ${error.message}. Refresh to check the watch’s status.`,
    );
  } finally {
    cancelling.delete(job.id);
    detailKey = null;
    render();
  }
}
async function handleFeedback(job) {
  if (approving.has(job.id)) return;
  approving.add(job.id);
  feedbackErrors.delete(job.id);
  renderDetail();
  try {
    const response = await fetch("/api/feedback", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Babysit-Action": "feedback" },
      body: JSON.stringify({ id: job.id, token: job.feedback_token }),
    });
    const result = await response.json();
    if (!response.ok) throw Error(result.error || `HTTP ${response.status}`);
    data.jobs = data.jobs.map((j) => (j.id === job.id ? result.job : j));
  } catch (error) {
    feedbackErrors.set(
      job.id,
      `Could not confirm approval: ${error.message}. Refresh to check the watch’s status.`,
    );
  } finally {
    approving.delete(job.id);
    detailKey = null;
    render();
  }
}
async function get(url, signal) {
  const r = await fetch(url, { cache: "no-store", signal });
  const result = await r.json();
  if (!r.ok) throw Error(result.error || `HTTP ${r.status}`);
  return result;
}
async function loadLog(job) {
  const kind = logKind;
  try {
    const result = await get(`/api/log?job=${encodeURIComponent(job.id)}&kind=${kind}`);
    if (selected === job.id && tab === "logs" && kind === logKind && $("repair-log"))
      $("repair-log").textContent =
        (result.truncated ? "… showing the last 64 KB …\n" : "") + result.text;
  } catch (e) {
    if (selected === job.id && kind === logKind && $("repair-log"))
      $("repair-log").textContent = e.message;
  }
}
async function serviceLog() {
  try {
    $("service-log").textContent = (await get("/api/log?kind=supervisor")).text;
  } catch (e) {
    $("service-log").textContent = e.message;
  }
}

const roleNames = {
  author: "Author",
  reviewer: "Reviewer",
  assignee: "Assignee",
  mentioned: "Mentioned",
  participant: "Participant",
};
const ciStates = {
  SUCCESS: ["Passed", "green"],
  FAILURE: ["Failed", "red"],
  ERROR: ["Error", "red"],
  PENDING: ["Pending", "amber"],
  EXPECTED: ["Expected", "amber"],
  NONE: ["No checks", ""],
};

function dateDetail(value, label, cls) {
  const cell = el("div", undefined, `pr-time ${cls}`);
  const date = new Date(value || NaN);
  const valid = Number.isFinite(date.getTime());
  const stamp = el("time", valid ? date.toLocaleString() : "Unknown");
  if (valid) stamp.setAttribute("datetime", value);
  cell.append(
    el("small", `${label} ${valid ? ago(date.getTime() / 1000) : "at an unknown time"}`),
    stamp,
  );
  return cell;
}
function datesCell(item) {
  const cell = el("td", undefined, "pr-dates");
  const opened = dateDetail(item.opened_at, "Opened", "pr-opened");
  const updated = dateDetail(item.updated_at, "Updated", "pr-updated");
  const activity = item.latest_activity;
  const detail = el("div", undefined, "pr-activity");
  detail.title =
    "Latest available activity from recent GitHub events and description edits; may not explain the last-updated time.";
  detail.append(el("small", "Latest activity", "pr-meta"));
  if (activity) {
    const text = `${activity.actor || "Unknown actor"} ${activity.action}`;
    detail.append(activity.url ? link(text, activity.url) : el("span", text));
    const date = new Date(activity.at || NaN);
    if (Number.isFinite(date.getTime())) {
      const stamp = el("time", ago(date.getTime() / 1000));
      stamp.dateTime = activity.at;
      stamp.title = date.toLocaleString();
      detail.append(stamp);
    }
  } else detail.append(el("span", "Unavailable", "pr-meta"));
  updated.append(detail);
  cell.append(opened, updated);
  return { cell, updated };
}
function repoCell(repo) {
  const cell = el("td", undefined, "pr-repo");
  cell.append(link(repo, `https://github.com/${repo}`));
  return cell;
}
function authorCell(login) {
  const cell = el("td", undefined, "pr-author");
  cell.append(login ? link(login, `https://github.com/${login}`) : el("span", "Unknown"));
  return cell;
}
function rolesCell(values) {
  const cell = el("td");
  const tags = el("div", undefined, "pr-roles");
  for (const value of values || []) tags.append(el("span", roleNames[value] || value, "badge"));
  cell.append(tags);
  return cell;
}
function ciBadge(pr) {
  const [label, color] = ciStates[pr.ci] || ["Unknown", ""];
  const checksLink = link(label, `${pr.url}/checks`);
  checksLink.className = `badge ${color}`;
  checksLink.setAttribute("aria-label", `CI ${label} for ${pr.repo} #${pr.number}`);
  checksLink.onclick = (event) => {
    if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    openPRCI(pr);
  };
  return checksLink;
}
function labelBadge(label) {
  const node = el("span", label.name, "badge pr-label");
  if (/^[0-9a-f]{6}$/i.test(label.color || "")) {
    // CSSOM assignments are allowed by the style-src policy; inline attributes are not.
    const [r, g, b] = [0, 2, 4].map((i) => parseInt(label.color.slice(i, i + 2), 16));
    node.style.background = `#${label.color}`;
    node.style.color = r * 0.299 + g * 0.587 + b * 0.114 > 150 ? "#1e2a24" : "#fff";
  }
  return node;
}
function sortedNames(values) {
  return [...(values || [])].sort();
}
function prReviewBadges(pr) {
  const badges = pr.draft ? [["Draft", ""]] : [];
  if (pr.review_decision === "APPROVED") badges.push(["Approved", "green pr-approved"]);
  else if (!pr.draft) badges.push(["Ready for review", "blue"]);
  return badges;
}

// One sortable, filterable overview table with "since your last visit" highlighting.
// `spec` supplies the column list, per-item cells, filters, and wording; the factory
// owns sorting state, repository/label option lists, visit tracking, and rendering.
// Rows rendered per page; more append as the sentinel below the table scrolls into view.
const PAGE_SIZE = 50;

function itemTable(spec) {
  const id = (suffix) => $(`${spec.prefix}-${suffix}`);
  let renderedRows = new Map();
  const table = {
    data: null,
    busy: false,
    sort: spec.defaultSort,
    ascending: false,
    visits: new Map(),
    pins: new Map(),
    optionKeys: new Map(),
    limit: PAGE_SIZE,
  };
  function items() {
    return table.data?.[spec.key] || [];
  }
  const pinStatus = id("pins-status");
  function currentPins() {
    if (!table.data.login) return null;
    const key = `babysit-pr:pins-${spec.key}:v1:${table.data.login}`;
    if (!table.pins.has(key)) table.pins.set(key, { key, ids: new Set(), storage: true });
    const pins = table.pins.get(key);
    if (pins.storage) {
      try {
        const saved = JSON.parse(localStorage.getItem(key));
        pins.ids = new Set(
          Array.isArray(saved) ? saved.filter((value) => typeof value === "string" && value) : [],
        );
      } catch (error) {
        if (error instanceof SyntaxError) pins.ids = new Set();
        else pins.storage = false;
      }
    }
    return pins;
  }
  function pinButton(item, pins) {
    const pinned = Boolean(pins?.ids.has(item.id));
    const button = el("button", undefined, "item-pin");
    const icon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    icon.setAttribute("viewBox", "0 0 24 24");
    icon.setAttribute("aria-hidden", "true");
    icon.setAttribute("focusable", "false");
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", "M16 3H8l1 7-4 4v2h6v6h2v-6h6v-2l-4-4 1-7Z");
    icon.append(path);
    button.append(icon);
    button.type = "button";
    button.dataset.itemId = item.id;
    button.disabled = !pins;
    button.setAttribute("aria-pressed", String(pinned));
    button.setAttribute(
      "aria-label",
      `${pinned ? "Unpin" : "Pin"} ${spec.text.short} ${item.repo} #${item.number}`,
    );
    button.title = !pins
      ? "Pins are available once your GitHub account is known"
      : pinned
        ? "Unpin and restore normal sort position"
        : "Pin to the top of matching results";
    button.onclick = () => {
      // Read again so another tab's pins are preserved when changing this item.
      const latest = currentPins();
      if (!latest) return;
      if (pinned) latest.ids.delete(item.id);
      else latest.ids.add(item.id);
      if (latest.storage) {
        try {
          localStorage.setItem(latest.key, JSON.stringify([...latest.ids]));
        } catch {
          latest.storage = false;
        }
      }
      render();
      // Keep keyboard focus on the moved control, or
      // on pagination if unpinning moved the item outside the rendered window.
      const moved = [...id("list").querySelectorAll(".item-pin")].find(
        (node) => node.dataset.itemId === item.id,
      );
      (moved || id("more-button")).focus();
    };
    return button;
  }
  function sortValue(item) {
    const column = table.sort;
    if (spec.dates.includes(column)) {
      const value = Date.parse(item[column]);
      return Number.isFinite(value) ? value : null;
    }
    const value = spec.sortValue(item, column);
    return value === undefined ? item[column] || null : value;
  }
  function compare(a, b) {
    const left = sortValue(a),
      right = sortValue(b);
    // Keep missing metadata at the bottom in either direction.
    if (left === null || right === null) return left === right ? 0 : left === null ? 1 : -1;
    const order =
      typeof left === "number"
        ? left - right
        : left.localeCompare(right, undefined, { numeric: true, sensitivity: "base" });
    return (table.ascending ? order : -order) || a.id.localeCompare(b.id);
  }
  function sort(column, toggle = false) {
    table.ascending =
      column === table.sort && toggle ? !table.ascending : !spec.dates.includes(column);
    table.sort = column;
    restart();
  }
  function updateOptions(name, values, allLabel) {
    const select = id(name);
    const selected = select.value || "all";
    const options = [...new Set(values)].sort((a, b) => a.localeCompare(b));
    // Retain an active filter if its last item disappears between snapshots.
    if (selected !== "all" && !options.includes(selected)) options.push(selected);
    const key = JSON.stringify(options);
    if (key === table.optionKeys.get(name)) return;
    table.optionKeys.set(name, key);
    select.replaceChildren(
      ...[["all", allLabel], ...options.map((value) => [value, value])].map(([value, text]) => {
        const option = el("option", text);
        option.value = value;
        return option;
      }),
    );
    select.value = selected;
    syncSelect(select);
  }
  function changeValue(item, field) {
    const value = spec.changeValue?.(item, field);
    return value === undefined ? (item[field] ?? null) : value;
  }
  function visitSnapshot() {
    return {
      synced_at: table.data.synced_at,
      [spec.key]: Object.fromEntries(
        items().map((item) => [
          item.id,
          Object.fromEntries(
            [...Object.keys(spec.changeFields), "updated_at"].map((field) => [
              field,
              changeValue(item, field),
            ]),
          ),
        ]),
      ),
    };
  }
  function readVisit(key) {
    const saved = JSON.parse(localStorage.getItem(key));
    return saved &&
      Number.isFinite(saved.synced_at) &&
      saved[spec.key] &&
      typeof saved[spec.key] === "object" &&
      !Array.isArray(saved[spec.key])
      ? saved
      : null;
  }
  function currentVisit() {
    if (document.hidden || $(spec.panel).hidden || !table.data.login || !table.data.synced_at)
      return null;
    const key = `babysit-pr:seen-${spec.key}:v1:${table.data.login}`;
    if (!table.visits.has(key)) {
      let baseline = null;
      let storage = true;
      try {
        baseline = readVisit(key);
      } catch (error) {
        storage = error instanceof SyntaxError;
      }
      table.visits.set(key, {
        key,
        baseline: baseline || visitSnapshot(),
        first: !baseline,
        storage,
        saved: null,
      });
    }
    return table.visits.get(key);
  }
  function rememberVisit(visit) {
    if (!visit || !visit.storage || table.data.error || table.data.refreshing) return;
    const snapshot = visitSnapshot();
    if (snapshot.synced_at < visit.baseline.synced_at) return;
    const serialized = JSON.stringify(snapshot);
    if (serialized === visit.saved) return;
    try {
      let latest = null;
      try {
        latest = readVisit(visit.key);
      } catch (error) {
        if (!(error instanceof SyntaxError)) throw error;
      }
      // A slower tab must not overwrite a newer snapshot saved by another tab.
      if (!latest || latest.synced_at <= snapshot.synced_at)
        localStorage.setItem(visit.key, serialized);
      visit.saved = serialized;
    } catch {
      visit.storage = false;
    }
  }
  function changes(item, visit) {
    if (!visit || table.data.synced_at < visit.baseline.synced_at) return null;
    const seen = visit.baseline[spec.key];
    const old = Object.hasOwn(seen, item.id) ? seen[item.id] : null;
    if (!old || typeof old !== "object") return { isNew: true, fields: [] };
    const fields = Object.keys(spec.changeFields).filter(
      (field) => JSON.stringify(old[field] ?? null) !== JSON.stringify(changeValue(item, field)),
    );
    if (!fields.length && old.updated_at === (item.updated_at ?? null)) return null;
    return { isNew: false, fields };
  }
  function render() {
    if (!table.data) return;
    const all = items();
    const visit = currentVisit();
    const pins = currentPins();
    spec.updateFilters(all, updateOptions);
    const query = id("search").value.toLowerCase();
    const filters = Object.fromEntries(spec.filters.map((name) => [name, id(name).value]));
    const visible = all.filter(
      (item) => spec.visible(item, filters) && spec.searchText(item).toLowerCase().includes(query),
    );
    visible.sort(
      (a, b) =>
        Number(pins?.ids.has(b.id) || false) - Number(pins?.ids.has(a.id) || false) ||
        compare(a, b),
    );
    pinStatus.hidden = !pins || pins.storage;
    pinStatus.textContent = pinStatus.hidden
      ? ""
      : "Pins are kept in this tab only; browser storage is unavailable.";
    const direction = table.ascending ? "ascending" : "descending";
    for (const column of Object.keys(spec.columns)) {
      const active = column === table.sort;
      const button = id(`sort-${column}`);
      button.setAttribute("data-sort", active ? direction : "none");
      button.setAttribute("aria-pressed", String(active));
      if (!spec.dates.includes(column))
        id(`heading-${column}`).setAttribute("aria-sort", active ? direction : "none");
    }
    // Both chronological sort choices share one physical column.
    const dateSort = spec.dates.includes(table.sort);
    id("heading-dates").setAttribute("aria-sort", dateSort ? direction : "none");
    id("heading-dates").setAttribute(
      "aria-label",
      `Dates / activity${dateSort ? `, sorted by ${spec.columns[table.sort]}` : ""}`,
    );
    id("sort").value = table.sort;
    syncSelect(id("sort"));
    id("sort-direction").textContent = table.ascending ? "Ascending" : "Descending";
    id("count").textContent = `${visible.length} / ${all.length}`;
    const sync = table.data.synced_at
      ? new Date(table.data.synced_at * 1000).toLocaleString()
      : null;
    id("sync").textContent =
      `${table.data.login ? `@${table.data.login} · ` : ""}${spec.text.open} · Sorted by ${spec.columns[table.sort]} (${table.ascending ? "ascending" : "descending"}) · ${sync ? `Synced ${sync}` : "Not synced yet"}${table.data.refreshing ? " · Syncing…" : " · GitHub refreshes every 5 minutes"}`;
    const problems = [table.data.error, ...(table.data.warnings || [])].filter(Boolean);
    id("alert").hidden = !problems.length;
    id("alert").textContent =
      problems.join(" ") +
      (table.data.error && sync ? " Showing saved results; they may be out of date." : "");
    let newCount = 0,
      changedCount = 0;
    const rows = [];
    const nextRows = new Map();
    for (const [index, item] of visible.entries()) {
      const change = changes(item, visit);
      if (change) {
        if (change.isNew) newCount += 1;
        else changedCount += 1;
      }
      // Change counts cover the whole filtered list; only the current window gets rows.
      if (index >= table.limit) continue;
      const row = el("tr");
      const { cells, title, fields, updated } = spec.row(item);
      const pinCell = el("td", undefined, "item-pin-cell");
      pinCell.append(pinButton(item, pins));
      if (change) {
        row.className = change.isNew ? "pr-new" : "pr-changed";
        title.append(
          el(
            "span",
            change.isNew ? "New" : "Updated",
            `badge ${change.isNew ? "blue" : "amber"} pr-change-badge`,
          ),
        );
        title.append(
          el(
            "small",
            change.isNew
              ? "Since your last visit"
              : change.fields.length
                ? change.fields.map((field) => spec.changeFields[field]).join(" · ")
                : spec.text.activity,
            "pr-change-note",
          ),
        );
        for (const field of change.fields) fields[field].classList.add("pr-field-changed");
        if (!change.isNew && !change.fields.length) updated.classList.add("pr-field-changed");
      }
      row.append(pinCell, ...cells, workspaceCell(item));
      // Keep the original link between mouse-down and mouse-up during quiet polls.
      // Compare callback inputs too: identical markup can still carry new action data.
      const key = JSON.stringify([
        item,
        workspaceInfo(item),
        (item.linked_prs || []).map((pr) =>
          prTable.items().find((known) => known.repo === pr.repo && known.number === pr.number),
        ),
      ]);
      const previous = renderedRows.get(item.id);
      const stable = previous?.key === key && previous.row.isEqualNode(row) ? previous.row : row;
      nextRows.set(item.id, { key, row: stable });
      rows.push(stable);
    }
    const list = id("list");
    const retained = new Set(rows);
    for (const child of [...list.children]) if (!retained.has(child)) child.remove();
    for (const [index, row] of rows.entries()) {
      if (list.children[index] !== row) list.insertBefore(row, list.children[index] || null);
    }
    renderedRows = nextRows;
    const remaining = visible.length - rows.length;
    id("more").hidden = remaining <= 0;
    id("more-button").textContent =
      `Show ${Math.min(PAGE_SIZE, remaining)} more · ${rows.length} of ${visible.length} shown`;
    rememberVisit(visit);
    id("changes").hidden = !visit;
    id("changes").textContent = !visit
      ? ""
      : !visit.storage
        ? "Changes are tracked for this visit only; browser storage is unavailable."
        : table.data.synced_at < visit.baseline.synced_at
          ? `Waiting for a current ${spec.text.short} snapshot to compare with your last visit.`
          : newCount || changedCount
            ? `Since your last visit: ${newCount} new · ${changedCount} updated in this view.`
            : visit.first
              ? "Changes will be highlighted from this visit onward, in this browser."
              : "No changes since your last visit in this view.";
    id("empty").hidden = visible.length > 0;
    id("empty").textContent = all.length
      ? spec.text.noMatch
      : table.data.synced_at
        ? spec.text.none
        : table.data.error
          ? spec.text.unavailable
          : spec.text.loading;
  }
  async function refresh() {
    if (table.busy) return;
    table.busy = true;
    try {
      table.data = await get(spec.endpoint);
      render();
    } catch (error) {
      id("alert").hidden = false;
      id("alert").textContent =
        `${spec.text.refreshError}: ${error.message}. Saved results may be out of date.`;
    } finally {
      table.busy = false;
    }
  }
  function showMore() {
    if (id("more").hidden) return;
    table.limit += PAGE_SIZE;
    render();
  }
  function restart() {
    // A new search, filter or sort starts at the top of the first page; refreshes keep
    // the window and scroll position.
    table.limit = PAGE_SIZE;
    id("list").closest(".pr-table-wrap").scrollTop = 0;
    render();
  }
  for (const column of Object.keys(spec.columns)) {
    id(`sort-${column}`).onclick = () => sort(column, true);
  }
  id("sort").onchange = () => sort(id("sort").value);
  id("sort-direction").onclick = () => sort(table.sort, true);
  id("refresh").onclick = refresh;
  id("search").oninput = restart;
  for (const name of spec.filters) id(name).onchange = restart;
  id("more-button").onclick = showMore;
  // A hidden sentinel never intersects, so this only fires with more rows to show.
  if (typeof IntersectionObserver === "function") {
    new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.isIntersecting)) showMore();
    }).observe(id("more"));
  }
  return Object.assign(table, { items, render, refresh });
}

const prTable = itemTable({
  prefix: "pr",
  key: "prs",
  endpoint: "/api/prs",
  panel: "prs-panel",
  columns: {
    repo: "Repository",
    title: "Pull request",
    author: "Author",
    readiness: "Review status",
    roles: "Your role",
    ci: "CI",
    opened_at: "Opened",
    updated_at: "Last updated",
  },
  dates: ["opened_at", "updated_at"],
  defaultSort: "updated_at",
  filters: ["role", "ci", "repo", "review"],
  text: {
    short: "PR",
    open: "Open PRs",
    activity: "PR activity changed",
    noMatch: "No matching pull requests.",
    none: "No open pull requests for your roles.",
    unavailable: "PR data is unavailable. Check GitHub authentication and the sync error above.",
    loading: "Loading your open pull requests…",
    refreshError: "Cannot refresh PR overview",
  },
  changeFields: {
    repo: "Repository",
    title: "Title",
    author: "Author",
    ci: "CI",
    review_decision: "Review",
    draft: "Draft status",
    roles: "Your role",
    head_sha: "New commits",
  },
  changeValue: (pr, field) => (field === "roles" ? sortedNames(pr.roles) : undefined),
  sortValue(pr, column) {
    if (column === "readiness")
      return prReviewBadges(pr)
        .map(([label]) => label)
        .join(" · ");
    if (column === "roles")
      return sortedNames(pr.roles.map((role) => roleNames[role] || role)).join(", ");
    if (column === "ci") return (ciStates[pr.ci] || ["Unknown"])[0];
    return undefined;
  },
  updateFilters(prs, update) {
    update(
      "repo",
      prs.map((pr) => pr.repo),
      "All repositories",
    );
  },
  visible: (pr, f) =>
    (f.role === "all" || pr.roles.includes(f.role)) &&
    (f.ci === "all" || pr.ci === f.ci || (f.ci === "UNKNOWN" && !ciStates[pr.ci])) &&
    (f.repo === "all" || pr.repo === f.repo) &&
    (f.review === "all" || (f.review === "draft" ? pr.draft : !pr.draft)),
  searchText: (pr) => `${pr.repo} ${pr.title} ${pr.number} ${pr.author || ""}`,
  row(pr) {
    const title = el("td", undefined, "pr-description");
    const titleLink = link(pr.title, pr.url);
    titleLink.className = "pr-title";
    title.append(titleLink, el("span", `#${pr.number}`, "pr-meta"));
    const author = authorCell(pr.author);
    const readiness = el("td", undefined, "pr-readiness");
    for (const [label, color] of prReviewBadges(pr))
      readiness.append(el("span", label, `badge ${color}`));
    const roles = rolesCell(pr.roles);
    const checks = el("td");
    checks.append(ciBadge(pr));
    const { cell: dates, updated } = datesCell(pr);
    const repo = repoCell(pr.repo);
    return {
      cells: [repo, title, author, readiness, roles, checks, dates],
      title,
      updated,
      fields: {
        repo,
        title,
        author,
        ci: checks,
        review_decision: readiness,
        draft: readiness,
        roles,
        head_sha: updated,
      },
    };
  },
});

function linkedPRBadge(issue, pr) {
  const wrap = el("span", undefined, "pr-linked-pr");
  const label = pr.repo === issue.repo ? `#${pr.number}` : `${pr.repo}#${pr.number}`;
  const anchor = link(label, pr.url);
  anchor.className = `badge ${pr.draft ? "" : pr.state === "MERGED" ? "green" : "blue"}`;
  anchor.setAttribute("aria-label", `Pull request ${pr.repo} #${pr.number}`);
  if (pr.title) anchor.title = pr.title;
  wrap.append(anchor);
  if (pr.draft) wrap.append(el("small", "Draft"));
  // CI is only known for PRs already in the PR overview; nothing is fetched here.
  const known = prTable.items().find((p) => p.repo === pr.repo && p.number === pr.number);
  if (known) wrap.append(ciBadge(known));
  return wrap;
}

const issueTable = itemTable({
  prefix: "issue",
  key: "issues",
  endpoint: "/api/issues",
  panel: "issues-panel",
  columns: {
    repo: "Repository",
    title: "Issue",
    author: "Author",
    assignees: "Assignees",
    roles: "Your role",
    comments: "Comments",
    linked_prs: "Linked PRs",
    opened_at: "Opened",
    updated_at: "Last updated",
  },
  dates: ["opened_at", "updated_at"],
  defaultSort: "updated_at",
  filters: ["role", "repo", "label", "linked"],
  text: {
    short: "issue",
    open: "Open issues",
    activity: "Issue activity changed",
    noMatch: "No matching issues.",
    none: "No open issues for your roles.",
    unavailable: "Issue data is unavailable. Check GitHub authentication and the sync error above.",
    loading: "Loading your open issues…",
    refreshError: "Cannot refresh issue overview",
  },
  changeFields: {
    repo: "Repository",
    title: "Title",
    author: "Author",
    assignees: "Assignees",
    labels: "Labels",
    roles: "Your role",
    comments: "Comments",
    linked_prs: "Linked PRs",
  },
  changeValue(issue, field) {
    if (field === "roles" || field === "assignees") return sortedNames(issue[field]);
    if (field === "labels") return sortedNames((issue.labels || []).map((label) => label.name));
    if (field === "linked_prs")
      return sortedNames((issue.linked_prs || []).map((pr) => `${pr.repo}#${pr.number}`));
    return undefined;
  },
  sortValue(issue, column) {
    if (column === "roles")
      return sortedNames(issue.roles.map((role) => roleNames[role] || role)).join(", ");
    if (column === "assignees")
      return issue.assignees?.length ? sortedNames(issue.assignees).join(", ") : null;
    if (column === "comments") return Number(issue.comments) || 0;
    if (column === "linked_prs") return (issue.linked_prs || []).length;
    return undefined;
  },
  updateFilters(issues, update) {
    update(
      "repo",
      issues.map((issue) => issue.repo),
      "All repositories",
    );
    update(
      "label",
      issues.flatMap((issue) => (issue.labels || []).map((label) => label.name)),
      "All labels",
    );
  },
  visible: (issue, f) =>
    (f.role === "all" || issue.roles.includes(f.role)) &&
    (f.repo === "all" || issue.repo === f.repo) &&
    (f.label === "all" || (issue.labels || []).some((label) => label.name === f.label)) &&
    (f.linked === "all" || (f.linked === "linked") === Boolean(issue.linked_prs?.length)),
  searchText: (issue) =>
    `${issue.repo} ${issue.title} ${issue.number} ${issue.author || ""} ${(issue.assignees || []).join(" ")} ${(issue.labels || []).map((label) => label.name).join(" ")}`,
  row(issue) {
    const title = el("td", undefined, "pr-description");
    const titleLink = link(issue.title, issue.url);
    titleLink.className = "pr-title";
    title.append(titleLink, el("span", `#${issue.number}`, "pr-meta"));
    if (issue.labels?.length) {
      const labels = el("div", undefined, "pr-badges pr-labels");
      for (const label of issue.labels) labels.append(labelBadge(label));
      title.append(labels);
    }
    const author = authorCell(issue.author);
    const assignees = el("td", undefined, "pr-assignees");
    if (issue.assignees?.length) {
      const list = el("div", undefined, "pr-badges");
      for (const login of issue.assignees) list.append(link(login, `https://github.com/${login}`));
      assignees.append(list);
    } else assignees.append(el("span", "Unassigned", "pr-meta"));
    const roles = rolesCell(issue.roles);
    const comments = el("td", String(Number(issue.comments) || 0), "pr-comments");
    const linked = el("td", undefined, "pr-linked");
    if (issue.linked_prs?.length) {
      const list = el("div", undefined, "pr-badges");
      for (const pr of issue.linked_prs) list.append(linkedPRBadge(issue, pr));
      linked.append(list);
    } else linked.append(el("span", "None", "pr-meta"));
    const { cell: dates, updated } = datesCell(issue);
    const repo = repoCell(issue.repo);
    return {
      cells: [repo, title, author, assignees, roles, comments, linked, dates],
      title,
      updated,
      fields: {
        repo,
        title,
        author,
        assignees,
        labels: title,
        roles,
        comments,
        linked_prs: linked,
      },
    };
  },
});

async function refresh() {
  // Issues render linked-PR CI from the PR overview, so refresh PRs first.
  void prTable.refresh().then(() => issueTable.refresh());
  if (busy) return;
  busy = true;
  try {
    data = await get("/api/status");
    render();
    if ($("service-details").open) await serviceLog();
  } catch (e) {
    $("alert").hidden = false;
    $("alert").textContent =
      `Dashboard disconnected: ${e.message}. Saved results may be out of date.`;
    $("health").textContent = "Disconnected";
    $("health").className = "badge amber";
  } finally {
    busy = false;
  }
}
const pages = ["watcher", "prs", "issues", "workspaces", "upstream"];
function showPage(name) {
  for (const page of pages) {
    const active = page === name;
    $(`${page}-panel`).hidden = !active;
    $(`${page}-tab`).setAttribute("aria-selected", String(active));
    $(`${page}-tab`).tabIndex = active ? 0 : -1;
  }
  if (name === "prs") prTable.render();
  if (name === "issues") issueTable.render();
  if (name === "workspaces") window.dispatchEvent(new Event("workspaces-visible"));
  if (name === "upstream") window.dispatchEvent(new Event("upstream-visible"));
}
function overviewVisible() {
  return !$("prs-panel").hidden || !$("issues-panel").hidden;
}
function pageFromURL() {
  const name = window.location.hash.slice(1);
  showPage(pages.includes(name) ? name : "watcher");
}
for (const page of pages) {
  $(`${page}-tab`).onclick = () => {
    window.location.hash = page;
    showPage(page);
    void refreshWorkspaces();
  };
  $(`${page}-tab`).onkeydown = (event) => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const available = pages.filter((name) => !$(`${name}-tab`).hidden);
    const index = available.indexOf(page);
    const next =
      event.key === "Home"
        ? available[0]
        : event.key === "End"
          ? available[available.length - 1]
          : available[
              (index + (event.key === "ArrowRight" ? 1 : available.length - 1)) % available.length
            ];
    $(`${next}-tab`).click();
    $(`${next}-tab`).focus();
  };
}
window.addEventListener("hashchange", pageFromURL);
pageFromURL();
$("show-attention").onclick = () => {
  $("filter").value = "attention";
  $("search").value = "";
  render();
};
$("show-ended").onclick = () => {
  $("filter").value = "ended";
  $("search").value = "";
  render();
};
$("refresh").onclick = refresh;
$("search").oninput = render;
$("filter").onchange = render;
$("service-details").ontoggle = () => {
  if ($("service-details").open) serviceLog();
};
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) {
    refresh();
    void refreshWorkspaces();
  }
});
setInterval(() => {
  if (!document.hidden) refresh();
}, 5000);
refresh();

let workspaceData = { prs: {}, issues: {}, watches: {} },
  workspaceBusy = false;
// Overview ids are globally unique; watch ids carry a separate prefix.
function workspaceInfo(item) {
  return (
    workspaceData.prs?.[item.id] ??
    workspaceData.issues?.[item.id] ??
    workspaceData.watches?.[item.id]
  );
}
function setWorkspaceOperation(item, operation) {
  const info = workspaceInfo(item);
  if (info) info.operation = operation;
}
async function refreshWorkspaces() {
  if (workspaceBusy || document.hidden || (!overviewVisible() && $("watcher-panel").hidden)) return;
  workspaceBusy = true;
  try {
    workspaceData = await get("/api/workspaces");
    prTable.render();
    issueTable.render();
    renderDetail();
    updateWorkspaceOperation();
  } catch (error) {
    workspaceData.error = error.message;
  } finally {
    workspaceBusy = false;
  }
}
let workspaceDialogItem = null;
function workspaceButton(label, callback) {
  const button = el("button", label);
  button.type = "button";
  button.onclick = callback;
  return button;
}
function workspaceStatus(target) {
  return `${target.agent_status}${target.linked_pr ? ` · via PR #${target.linked_pr}` : ""} · ${target.path}`;
}
function workspaceCell(item) {
  const cell = el("td", undefined, "pr-actions");
  cell.append(workspaceControls(item));
  return cell;
}
function workspaceControls(item) {
  const cell = el("div", undefined, "workspace-actions");
  const info = workspaceInfo(item);
  const matches = info?.matches || [];
  const label = matches.some((m) => m.workspace_id)
    ? "Open workspace"
    : matches.length
      ? "Reopen workspace"
      : item.kind === "watch"
        ? "Workspace unavailable"
        : info?.clones.length
          ? "Create workspace"
          : "Clone and create";
  const button = workspaceButton(info ? label : "Workspace actions", () => {
    if (matches.length === 1 && matches[0].workspace_id) {
      void chooseWorkspace(item, matches[0], "open");
    } else void workspaceDialog(item);
  });
  cell.append(button);
  button.disabled = item.kind === "watch" && !!info && !matches.length;
  if (button.disabled) button.title = "The watch’s registered checkout is no longer available.";
  if (matches.length === 1) {
    const menu = el("details");
    menu.append(el("summary", "More actions"));
    if (matches[0].workspace_id)
      menu.append(
        workspaceButton("Focus in herdr", () => chooseWorkspace(item, matches[0], "focus")),
      );
    menu.append(workspaceButton("Copy command", () => chooseWorkspace(item, matches[0], "copy")));
    cell.append(menu);
    if (matches[0].linked_pr) cell.append(el("small", `Via PR #${matches[0].linked_pr}`));
  }
  if (info?.operation) cell.append(el("small", info.operation.message));
  return cell;
}
async function workspaceRequest(item, action, params = {}) {
  const response = await fetch("/api/workspace-action", {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Babysit-Action": "workspace-action" },
    body: JSON.stringify({ id: item.id, action, ...params }),
  });
  const value = await response.json();
  if (!response.ok) throw Error(value.error || `HTTP ${response.status}`);
  return value;
}
function workspaceError(error) {
  $("workspace-error").textContent = error.message;
}
async function chooseWorkspace(item, target, action) {
  const opened = action === "open" ? window.open("about:blank", "_blank") : null;
  if (opened) opened.opener = null;
  $("workspace-error").textContent = "";
  const params = { path: target.path };
  if (target.workspace_id) params.workspace_id = target.workspace_id;
  try {
    const value = await workspaceRequest(item, action, params);
    if (action === "copy") {
      await navigator.clipboard.writeText(value.command);
      $("workspace-error").textContent = "Command copied";
    } else if (value.result && action === "open") {
      if (opened) opened.location.href = value.result.url;
      else {
        await workspaceDialog(item);
        $("workspace-error").textContent = "Use Open in Collie to continue.";
      }
      $("workspace-result").replaceChildren(link("Open in Collie", value.result.url));
    } else if (value.operation) {
      setWorkspaceOperation(item, value.operation);
      updateWorkspaceOperation();
    }
  } catch (error) {
    if (opened) opened.close();
    if (!$("workspace-dialog").open) await workspaceDialog(item);
    workspaceError(error);
  }
}
async function workspaceDialog(item) {
  workspaceDialogItem = item;
  $("workspace-title").textContent =
    item.kind === "watch"
      ? `${item.repo} · ${item.branch || "Watched PR"}`
      : `${item.repo} #${item.number}`;
  $("workspace-error").textContent = "";
  $("workspace-content").replaceChildren(el("p", "Discovering local workspaces…"));
  $("workspace-result").replaceChildren();
  $("workspace-progress").textContent = "";
  $("workspace-log").textContent = "";
  if (!$("workspace-dialog").open) $("workspace-dialog").showModal();
  try {
    // Discovery is shared and cached; every action revalidates the selected target.
    workspaceData = await get("/api/workspaces");
    if (workspaceDialogItem !== item) return;
    const info = workspaceInfo(item);
    if (workspaceData.error) throw Error(workspaceData.error);
    if (workspaceData.synced_at === null)
      throw Error("Local discovery is still running. Try again in a moment.");
    if (!info) throw Error("Workspace discovery is unavailable. Try again after refreshing.");
    const body = $("workspace-content");
    body.replaceChildren();
    if (info.matches.length) {
      body.append(el("p", "Choose a checkout. Opening a workspace preserves its current agent."));
      for (const target of info.matches) {
        const row = el("section", undefined, "workspace-choice");
        row.append(el("strong", target.name), el("small", workspaceStatus(target)));
        row.append(
          workspaceButton(target.workspace_id ? "Open workspace" : "Reopen workspace", () =>
            chooseWorkspace(item, target, target.workspace_id ? "open" : "reopen"),
          ),
        );
        const menu = el("details");
        menu.append(el("summary", "More actions"));
        if (target.workspace_id)
          menu.append(
            workspaceButton("Focus in herdr", () => chooseWorkspace(item, target, "focus")),
          );
        menu.append(workspaceButton("Copy command", () => chooseWorkspace(item, target, "copy")));
        row.append(menu);
        body.append(row);
      }
    } else if (item.kind === "watch") {
      body.append(el("p", "The watch’s registered checkout is no longer available."));
    } else {
      const form = el("form");
      const clone = el("select");
      clone.id = "workspace-clone";
      for (const path of info.clones) {
        const option = el("option", path);
        option.value = path;
        clone.append(option);
      }
      if (info.preferred_clone) clone.value = info.preferred_clone;
      if (info.clones.length > 1 && !info.preferred_clone) {
        const option = el("option", "Choose a local clone");
        option.value = "";
        clone.prepend(option);
        clone.value = "";
      }
      clone.required = true;
      const agent = el("select");
      agent.id = "workspace-agent";
      for (const value of ["codex", "claude"]) {
        const option = el("option", value === "codex" ? "Codex" : "Claude");
        option.value = value;
        agent.append(option);
      }
      const model = el("select");
      model.id = "workspace-model";
      const effort = el("select");
      effort.id = "workspace-effort";
      const settingsNote = el("small", "Default keeps the agent’s configured setting.");
      function options(select, values) {
        select.replaceChildren();
        for (const value of ["", ...values]) {
          const option = el("option", value || "Default");
          option.value = value;
          select.append(option);
        }
      }
      function updateEfforts() {
        const choices = workspaceData.agent_choices?.[agent.value];
        const selected = choices?.models.find((choice) => choice.id === model.value);
        const previous = effort.value;
        options(effort, selected ? selected.efforts : (choices?.efforts ?? []));
        if ([...effort.options].some((option) => option.value === previous))
          effort.value = previous;
        syncSelect(effort);
        settingsNote.textContent =
          !model.value && effort.value
            ? "Effort support depends on the agent’s configured model."
            : "Default keeps the agent’s configured setting.";
      }
      function updateModels() {
        const choices = workspaceData.agent_choices?.[agent.value];
        options(model, choices?.models.map((choice) => choice.id) ?? []);
        effort.value = "";
        updateEfforts();
        syncSelect(model);
        if (agent.value === "codex" && !choices?.models.length)
          settingsNote.textContent =
            "Default keeps your settings. Codex model choices need its local model cache.";
      }
      agent.onchange = updateModels;
      model.onchange = updateEfforts;
      effort.onchange = updateEfforts;
      updateModels();
      const task = el("textarea");
      task.id = "workspace-task";
      task.required = true;
      task.maxLength = 32000;
      task.rows = 6;
      function field(text, input) {
        const label = el("label", text);
        label.htmlFor = input.id;
        form.append(label, input);
      }
      if (info.clones.length) field("Local clone", clone);
      else
        form.append(
          el(
            "p",
            info.destination
              ? `Clone ${item.repo} into ${info.destination}`
              : "Both clone destinations already exist. Move conflicting content before trying again.",
          ),
        );
      field("Agent", agent);
      field("Model (optional)", model);
      field("Reasoning effort (optional)", effort);
      form.append(settingsNote);
      field("Task", task);
      const submit = el("button", info.clones.length ? "Create workspace" : "Clone and create");
      submit.type = "submit";
      submit.disabled = !info.clones.length && !info.destination;
      form.append(submit);
      form.onsubmit = async (event) => {
        event.preventDefault();
        if (!task.value.trim()) {
          task.setCustomValidity("Enter a task");
          task.reportValidity();
          return;
        }
        submit.disabled = true;
        $("workspace-error").textContent = "";
        try {
          const value = await workspaceRequest(
            item,
            info.clones.length ? "create" : "clone-and-create",
            {
              agent: agent.value,
              ...(model.value ? { model: model.value } : {}),
              ...(effort.value ? { effort: effort.value } : {}),
              task: task.value,
              ...(info.clones.length ? { clone: clone.value } : { destination: info.destination }),
              retry: workspaceInfo(item)?.operation?.status === "failed",
            },
          );
          setWorkspaceOperation(item, value.operation);
          updateWorkspaceOperation();
        } catch (error) {
          workspaceError(error);
          submit.disabled = false;
        }
      };
      task.oninput = () => task.setCustomValidity("");
      body.append(form);
      if (info.clones.length) searchableSelect(clone);
      searchableSelect(model);
      searchableSelect(effort);
      if (info.suggestions.length)
        body.append(el("p", `Unverified branch-name suggestions: ${info.suggestions.join(", ")}`));
    }
    updateWorkspaceOperation();
  } catch (error) {
    workspaceError(error);
  }
}
function updateWorkspaceOperation() {
  if (!workspaceDialogItem || !$("workspace-dialog").open) return;
  const op = workspaceInfo(workspaceDialogItem)?.operation;
  if (!op) return;
  $("workspace-progress").textContent = `${op.status}: ${op.message}`;
  $("workspace-log").textContent = op.log || "";
  if (op.result?.url) $("workspace-result").replaceChildren(link("Open in Collie", op.result.url));
  const submit = $("workspace-content").querySelector("button[type=submit]");
  if (submit) submit.disabled = op.status !== "failed";
  syncWorkspacePolling();
}
let workspacePoll = null;
function syncWorkspacePolling() {
  // Poll every two seconds while the open dialog shows a queued or running operation;
  // otherwise the shared 15-second workspace refresh is enough.
  clearTimeout(workspacePoll);
  const op =
    workspaceDialogItem && $("workspace-dialog").open
      ? workspaceInfo(workspaceDialogItem)?.operation
      : null;
  if (!op || !["queued", "running"].includes(op.status)) return;
  workspacePoll = setTimeout(async () => {
    await refreshWorkspaces();
    syncWorkspacePolling();
  }, 2000);
}
$("workspace-close").onclick = () => $("workspace-dialog").close();
$("workspace-dialog").onclose = syncWorkspacePolling;
closeOnBackdropClick($("workspace-dialog"));
setInterval(refreshWorkspaces, 15000);
void refreshWorkspaces();

let ciGeneration = 0;
const ciTimers = new Set();
const ciCleanups = new Set();
function stopCILoads() {
  ciGeneration++;
  for (const timer of ciTimers) clearTimeout(timer);
  ciTimers.clear();
  for (const cleanup of ciCleanups) cleanup();
  ciCleanups.clear();
}
async function loadCI(url, generation, render, fail, attempt = 0) {
  if (generation !== ciGeneration || !$("ci-dialog").open) return;
  try {
    if (!document.hidden && overviewVisible()) {
      const result = await get(url);
      if (generation !== ciGeneration || !$("ci-dialog").open) return;
      render(result);
      if (!result.refreshing) return;
    }
    if (attempt >= 100)
      throw Error("CI is taking longer than expected. Close and reopen to check again.");
    const timer = setTimeout(() => {
      ciTimers.delete(timer);
      void loadCI(url, generation, render, fail, attempt + 1);
    }, 2000);
    ciTimers.add(timer);
  } catch (error) {
    if (generation === ciGeneration) fail(error.message);
  }
}
function ciReportedFailure(pr, check) {
  const detail = el("details", undefined, "ci-reported");
  detail.append(el("summary", "Reported failure details"));
  const body = el("div");
  detail.append(body);
  let loaded = false;
  detail.ontoggle = () => {
    if (!detail.open || loaded) return;
    loaded = true;
    body.replaceChildren(el("p", "Loading reported failures…"));
    const url = `/api/pr-ci?id=${encodeURIComponent(pr.id)}&check=${encodeURIComponent(check.id)}`;
    void loadCI(
      url,
      ciGeneration,
      (result) => {
        if (!result.value && result.refreshing) return;
        body.replaceChildren();
        if (result.error) body.append(el("p", result.error, "ci-error"));
        if (!result.value) return;
        const value = result.value;
        if (value.title) body.append(el("strong", value.title));
        for (const text of [value.summary, value.text].filter(Boolean))
          body.append(el("pre", text));
        for (const annotation of value.annotations) {
          const entry = el("article", undefined, "ci-annotation");
          entry.append(el("strong", annotation.title || annotation.level));
          if (annotation.path) entry.append(el("small", `${annotation.path}:${annotation.line}`));
          entry.append(el("pre", annotation.message));
          body.append(entry);
        }
        if (!value.summary && !value.text && !value.annotations.length)
          body.append(
            el(
              "p",
              "This check did not publish failure messages. Open the job on GitHub for its test output.",
            ),
          );
        if (value.truncated)
          body.append(
            el(
              "p",
              "Showing a limited excerpt and the first 20 annotations. Open the job for the full output.",
            ),
          );
      },
      (message) => body.replaceChildren(el("p", message, "ci-error")),
    );
  };
  return detail;
}
function openPRCI(pr) {
  stopCILoads();
  $("ci-title").textContent = `${pr.repo} #${pr.number} · CI`;
  $("ci-meta").textContent = "Loading checks…";
  $("ci-error").textContent = "";
  $("ci-links").replaceChildren(link("All checks on GitHub", `${pr.url}/checks`));
  $("ci-content").replaceChildren();
  if (!$("ci-dialog").open) $("ci-dialog").showModal();
  let rendered = null;
  void loadCI(
    `/api/pr-ci?id=${encodeURIComponent(pr.id)}`,
    ciGeneration,
    (result) => {
      $("ci-error").textContent = result.error || "";
      $("ci-meta").textContent = result.refreshing
        ? result.busy
          ? "Other CI details are loading; waiting for a slot…"
          : "Loading checks…"
        : "Details are fetched on demand and cached for 5 minutes.";
      if (!result.value) return;
      $("ci-meta").textContent =
        `Commit ${result.value.sha.slice(0, 12)} · Fetched ${new Date(result.synced_at * 1000).toLocaleString()}${result.stale || result.error ? " · Saved results may be out of date" : " · Cached for 5 minutes"}${result.refreshing ? " · Updating…" : ""}`;
      const key = JSON.stringify(result.value);
      if (key === rendered) return;
      rendered = key;
      const content = $("ci-content");
      content.replaceChildren();
      const checks = result.value.checks;
      if (!checks.length) content.append(el("p", "No checks reported for this commit."));
      for (const [bucket, label] of [
        ["fail", "Failing checks"],
        ["pending", "Pending checks"],
        ["cancel", "Cancelled checks"],
        ["pass", "Passed checks"],
        ["skipping", "Skipped / neutral checks"],
      ]) {
        const group = checks.filter((check) => check.bucket === bucket);
        if (!group.length) continue;
        const section = el("section", undefined, "ci-group");
        section.append(el("h3", `${label} (${group.length})`));
        for (const check of group) {
          const item = el("article", undefined, "ci-item");
          item.append(checkRow(check.name, check.url, check.bucket, check.workflow));
          item.append(el("small", check.state.replaceAll("_", " ")));
          if (check.description) item.append(el("p", check.description));
          if (check.has_details) {
            item.append(ciReportedFailure(pr, check));
            item.append(ciDownloadedLog(pr, check));
          }
          section.append(item);
        }
        content.append(section);
      }
      if (result.value.truncated)
        content.prepend(
          el(
            "p",
            "Showing the first 300 checks. The list is incomplete; open All checks on GitHub to see the rest.",
            "ci-error",
          ),
        );
    },
    (message) => {
      $("ci-error").textContent = message;
      $("ci-meta").textContent = "Could not load CI details.";
    },
  );
}
$("ci-close").onclick = () => $("ci-dialog").close();
closeOnBackdropClick($("ci-dialog"));
$("ci-dialog").onclose = stopCILoads;

function ciDownloadedLog(pr, check) {
  const detail = el("details", undefined, "ci-reported");
  detail.append(el("summary", "Downloaded test log"));
  const body = el("div");
  detail.append(body);
  let loaded = false;
  detail.ontoggle = () => {
    if (!detail.open || loaded) return;
    loaded = true;
    body.replaceChildren(el("p", "Checking the background download…"));
    const url = `/api/pr-ci-log?id=${encodeURIComponent(pr.id)}&check=${encodeURIComponent(check.id)}`;
    void loadCI(
      url,
      ciGeneration,
      (result) => {
        body.replaceChildren(el("p", result.message));
        if (result.error) body.append(el("p", result.error, "ci-error"));
        if (!result.value) {
          if (["queued", "downloading", "error"].includes(result.state))
            body.append(el("small", "Downloads continue with the browser closed."));
          return;
        }
        const value = result.value;
        if (value.failed_steps.length)
          body.append(el("p", `Failed steps: ${value.failed_steps.join(", ")}`));
        if (value.tests.length) {
          body.append(el("h4", "Detected test failures"));
          const list = el("ul");
          for (const test of value.tests) list.append(el("li", test));
          body.append(list);
        } else
          body.append(el("p", "No individual test names were detected. The job output is below."));
        if (value.truncated)
          body.append(
            el("p", "Download stopped at the 8 MiB limit; this log is incomplete.", "ci-error"),
          );
        body.append(ciLogStream(url, detail));
      },
      (message) => body.replaceChildren(el("p", message, "ci-error")),
    );
  };
  return detail;
}

function ciLogStream(url, detail) {
  const viewer = el("div", undefined, "ci-log-viewer");
  const output = el("pre", "");
  output.tabIndex = 0;
  output.setAttribute("aria-label", "Cached job log output");
  const status = el("p", "Loading log…", "pr-sync");
  status.setAttribute("role", "status");
  const error = el("p", "", "ci-error");
  error.setAttribute("role", "alert");
  error.hidden = true;
  viewer.append(output, status, error);
  const generation = ciGeneration;
  let page = 0,
    pages = null,
    loading = false,
    disposed = false;
  let failures = 0,
    retryTimer = null,
    frame = null,
    controller = null;
  function active() {
    return (
      !disposed &&
      generation === ciGeneration &&
      viewer.isConnected &&
      detail.open &&
      $("ci-dialog").open &&
      !document.hidden &&
      overviewVisible() &&
      output.getClientRects().length
    );
  }
  function maybeLoad() {
    if (
      !active() ||
      loading ||
      retryTimer !== null ||
      failures >= 3 ||
      (pages !== null && page >= pages)
    )
      return;
    if (output.scrollHeight - output.scrollTop - output.clientHeight <= 160) void loadMore();
  }
  function schedule() {
    if (frame !== null) cancelAnimationFrame(frame);
    frame = requestAnimationFrame(() => {
      frame = null;
      maybeLoad();
    });
  }
  async function loadMore() {
    loading = true;
    controller = new AbortController();
    output.setAttribute("aria-busy", "true");
    status.textContent = page ? "Loading more…" : "Loading log…";
    error.hidden = true;
    try {
      const result = await get(`${url}&page=${page + 1}`, controller.signal);
      if (disposed || generation !== ciGeneration || !viewer.isConnected) return;
      if (
        result.page !== page + 1 ||
        !Number.isInteger(result.pages) ||
        result.pages < result.page ||
        typeof result.text !== "string"
      )
        throw Error("Could not read the cached log response");
      const scrollTop = output.scrollTop;
      output.append(document.createTextNode(result.text));
      output.scrollTop = scrollTop;
      page = result.page;
      pages = result.pages;
      failures = 0;
      if (page === pages) {
        if (!output.textContent) output.textContent = "The cached job log is empty.";
        status.textContent = result.truncated
          ? "End of cached log · This log is incomplete."
          : "End of cached log";
      } else status.textContent = "Scroll for more";
      schedule();
    } catch (failure) {
      if (disposed || failure.name === "AbortError") return;
      failures += 1;
      error.textContent = failure.message;
      error.hidden = false;
      status.textContent =
        failures < 3
          ? "Could not load more. Retrying…"
          : "Could not load more. Collapse and reopen this log to retry.";
      if (failures < 3)
        retryTimer = setTimeout(() => {
          retryTimer = null;
          maybeLoad();
        }, failures * 2000);
    } finally {
      loading = false;
      controller = null;
      output.setAttribute("aria-busy", "false");
    }
  }
  function reopen() {
    if (detail.open) {
      failures = 0;
      maybeLoad();
    }
  }
  const observer = new ResizeObserver(maybeLoad);
  observer.observe(output);
  output.addEventListener("scroll", maybeLoad);
  detail.addEventListener("toggle", reopen);
  document.addEventListener("visibilitychange", maybeLoad);
  const cleanup = () => {
    disposed = true;
    controller?.abort();
    if (retryTimer !== null) clearTimeout(retryTimer);
    if (frame !== null) cancelAnimationFrame(frame);
    observer.disconnect();
    detail.removeEventListener("toggle", reopen);
    document.removeEventListener("visibilitychange", maybeLoad);
    output.removeEventListener("scroll", maybeLoad);
    ciCleanups.delete(cleanup);
  };
  ciCleanups.add(cleanup);
  schedule();
  return viewer;
}

document.addEventListener("pointerdown", (event) => dismissSelect?.(event.target));
for (const select of document.querySelectorAll("select")) searchableSelect(select);
