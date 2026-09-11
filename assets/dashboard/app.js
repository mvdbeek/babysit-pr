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
let prData = null,
  prBusy = false;
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
      badge(job.status),
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
    badge(job.status),
  );
  const title = el("h2");
  title.append(link(job.branch || job.repo, job.url));
  head.append(top, title, el("p", job.summary));
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
async function get(url) {
  const r = await fetch(url, { cache: "no-store" });
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

function renderPRs() {
  if (!prData) return;
  const prs = prData.prs || [];
  const query = $("pr-search").value.toLowerCase();
  const role = $("pr-role").value;
  const ci = $("pr-ci").value;
  const visible = prs.filter(
    (pr) =>
      (role === "all" || pr.roles.includes(role)) &&
      (ci === "all" ||
        pr.ci === ci ||
        (ci === "FAILURE" && pr.ci === "ERROR") ||
        (ci === "PENDING" && pr.ci === "EXPECTED")) &&
      `${pr.repo} ${pr.title} ${pr.number} ${pr.author || ""}`.toLowerCase().includes(query),
  );
  $("pr-count").textContent = `${visible.length} / ${prs.length}`;
  const sync = prData.synced_at ? new Date(prData.synced_at * 1000).toLocaleString() : null;
  $("pr-sync").textContent =
    `${prData.login ? `@${prData.login} · ` : ""}Open PRs · most recently updated first · ${sync ? `Synced ${sync}` : "Not synced yet"}${prData.refreshing ? " · Syncing…" : " · GitHub refreshes every 2 minutes"}`;
  const issues = [prData.error, ...(prData.warnings || [])].filter(Boolean);
  $("pr-alert").hidden = !issues.length;
  $("pr-alert").textContent =
    issues.join(" ") +
    (prData.error && sync ? " Showing saved results; they may be out of date." : "");
  $("pr-list").replaceChildren();
  const roleNames = { author: "Author", reviewer: "Reviewer", assignee: "Assignee" };
  const ciStates = {
    SUCCESS: ["Passed", "green"],
    FAILURE: ["Failed", "red"],
    ERROR: ["Error", "red"],
    PENDING: ["Pending", "amber"],
    EXPECTED: ["Expected", "amber"],
    NONE: ["No checks", ""],
  };
  for (const pr of visible) {
    const row = el("tr");
    const title = el("td");
    const titleLink = link(pr.title, pr.url);
    titleLink.className = "pr-title";
    title.append(
      titleLink,
      el(
        "span",
        `${pr.repo} #${pr.number}${pr.author ? ` · ${pr.author}` : ""}${pr.draft ? " · Draft" : ""}`,
        "pr-meta",
      ),
    );
    const roles = el("td");
    const tags = el("div", undefined, "pr-roles");
    for (const value of pr.roles) tags.append(el("span", roleNames[value] || value, "badge"));
    roles.append(tags);
    const checks = el("td");
    const [label, color] = ciStates[pr.ci] || ["Unknown", ""];
    const checksLink = link(label, `${pr.url}/checks`);
    checksLink.className = `badge ${color}`;
    checksLink.setAttribute("aria-label", `CI ${label} for ${pr.repo} #${pr.number}`);
    checks.append(checksLink);
    const updated = el("td", undefined, "pr-time");
    const date = new Date(pr.updated_at);
    const valid = Number.isFinite(date.getTime());
    const stamp = el("time", valid ? date.toLocaleString() : "Unknown");
    if (valid) stamp.setAttribute("datetime", pr.updated_at);
    updated.append(
      el("small", `Updated ${valid ? ago(date.getTime() / 1000) : "at an unknown time"}`),
      stamp,
    );
    row.append(title, roles, checks, updated);
    $("pr-list").append(row);
  }
  $("pr-empty").hidden = visible.length > 0;
  $("pr-empty").textContent = prs.length
    ? "No matching pull requests."
    : prData.synced_at
      ? "No open pull requests for your roles."
      : prData.error
        ? "PR data is unavailable. Check GitHub authentication and the sync error above."
        : "Loading your open pull requests…";
}
async function refreshPRs() {
  if (prBusy) return;
  prBusy = true;
  try {
    prData = await get("/api/prs");
    renderPRs();
  } catch (error) {
    $("pr-alert").hidden = false;
    $("pr-alert").textContent =
      `Cannot refresh PR overview: ${error.message}. Saved results may be out of date.`;
  } finally {
    prBusy = false;
  }
}

async function refresh() {
  void refreshPRs();
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
$("pr-search").oninput = renderPRs;
$("pr-role").onchange = renderPRs;
$("pr-ci").onchange = renderPRs;
$("search").oninput = render;
$("filter").onchange = render;
$("service-details").ontoggle = () => {
  if ($("service-details").open) serviceLog();
};
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) refresh();
});
setInterval(() => {
  if (!document.hidden) refresh();
}, 5000);
refresh();
