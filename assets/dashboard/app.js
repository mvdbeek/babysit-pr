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

const roleNames = { author: "Author", reviewer: "Reviewer", assignee: "Assignee" };
const ciStates = {
  SUCCESS: ["Passed", "green"],
  FAILURE: ["Failed", "red"],
  ERROR: ["Error", "red"],
  PENDING: ["Pending", "amber"],
  EXPECTED: ["Expected", "amber"],
  NONE: ["No checks", ""],
};

const prColumns = {
  repo: "Repository",
  title: "Pull request",
  author: "Author",
  readiness: "Review status",
  roles: "Your role",
  ci: "CI",
  opened_at: "Opened",
  updated_at: "Last updated",
};
let prSort = "updated_at",
  prAscending = false;
function prReviewBadges(pr) {
  const badges = pr.draft ? [["Draft", ""]] : [];
  if (pr.review_decision === "APPROVED") badges.push(["Approved", "green pr-approved"]);
  else if (!pr.draft) badges.push(["Ready for review", "blue"]);
  return badges;
}
function prSortValue(pr) {
  if (prSort === "readiness")
    return prReviewBadges(pr)
      .map(([label]) => label)
      .join(" · ");
  if (prSort === "roles")
    return pr.roles
      .map((role) => roleNames[role] || role)
      .sort()
      .join(", ");
  if (prSort === "ci") return (ciStates[pr.ci] || ["Unknown"])[0];
  if (prSort === "opened_at" || prSort === "updated_at") {
    const value = Date.parse(pr[prSort]);
    return Number.isFinite(value) ? value : null;
  }
  return pr[prSort] || null;
}
function comparePRs(a, b) {
  const left = prSortValue(a),
    right = prSortValue(b);
  // Keep missing metadata at the bottom in either direction.
  if (left === null || right === null) return left === right ? 0 : left === null ? 1 : -1;
  const order =
    typeof left === "number"
      ? left - right
      : left.localeCompare(right, undefined, { numeric: true, sensitivity: "base" });
  return (prAscending ? order : -order) || a.id.localeCompare(b.id);
}
function sortPRs(column, toggle = false) {
  prAscending =
    column === prSort && toggle ? !prAscending : !["opened_at", "updated_at"].includes(column);
  prSort = column;
  renderPRs();
}
function prDate(value, label, cls) {
  const cell = el("td", undefined, `pr-time ${cls}`);
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
let prRepositoriesKey = null;
function updatePRRepositories(prs) {
  const select = $("pr-repo");
  const selected = select.value || "all";
  const repositories = [...new Set(prs.map((pr) => pr.repo))].sort((a, b) => a.localeCompare(b));
  // Retain an active filter if its last PR closes between snapshots.
  if (selected !== "all" && !repositories.includes(selected)) repositories.push(selected);
  const key = JSON.stringify(repositories);
  if (key === prRepositoriesKey) return;
  prRepositoriesKey = key;
  const options = [["all", "All repositories"], ...repositories.map((repo) => [repo, repo])].map(
    ([value, text]) => {
      const option = el("option", text);
      option.value = value;
      return option;
    },
  );
  select.replaceChildren(...options);
  select.value = selected;
}
const prVisits = new Map();
const prChangeFields = {
  repo: "Repository",
  title: "Title",
  author: "Author",
  ci: "CI",
  review_decision: "Review",
  draft: "Draft status",
  roles: "Your role",
  head_sha: "New commits",
};
function prVisitSnapshot() {
  return {
    synced_at: prData.synced_at,
    prs: Object.fromEntries(
      prData.prs.map((pr) => [
        pr.id,
        Object.fromEntries(
          [...Object.keys(prChangeFields), "updated_at"].map((field) => [
            field,
            field === "roles" ? [...(pr.roles || [])].sort() : (pr[field] ?? null),
          ]),
        ),
      ]),
    ),
  };
}
function readPRVisit(key) {
  const saved = JSON.parse(localStorage.getItem(key));
  return saved &&
    Number.isFinite(saved.synced_at) &&
    saved.prs &&
    typeof saved.prs === "object" &&
    !Array.isArray(saved.prs)
    ? saved
    : null;
}
function currentPRVisit() {
  if (document.hidden || $("prs-panel").hidden || !prData.login || !prData.synced_at) return null;
  const key = `babysit-pr:seen-prs:v1:${prData.login}`;
  if (!prVisits.has(key)) {
    let baseline = null;
    let storage = true;
    try {
      baseline = readPRVisit(key);
    } catch (error) {
      storage = error instanceof SyntaxError;
    }
    prVisits.set(key, {
      key,
      baseline: baseline || prVisitSnapshot(),
      first: !baseline,
      storage,
      saved: null,
    });
  }
  return prVisits.get(key);
}
function rememberPRVisit(visit) {
  if (!visit || !visit.storage || prData.error || prData.refreshing) return;
  const snapshot = prVisitSnapshot();
  if (snapshot.synced_at < visit.baseline.synced_at) return;
  const serialized = JSON.stringify(snapshot);
  if (serialized === visit.saved) return;
  try {
    let latest = null;
    try {
      latest = readPRVisit(visit.key);
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
function prChanges(pr, visit) {
  if (!visit || prData.synced_at < visit.baseline.synced_at) return null;
  const old = Object.hasOwn(visit.baseline.prs, pr.id) ? visit.baseline.prs[pr.id] : null;
  if (!old || typeof old !== "object") return { isNew: true, fields: [] };
  const fields = Object.keys(prChangeFields).filter((field) => {
    const value = field === "roles" ? [...(pr.roles || [])].sort() : (pr[field] ?? null);
    return JSON.stringify(old[field] ?? null) !== JSON.stringify(value);
  });
  if (!fields.length && old.updated_at === (pr.updated_at ?? null)) return null;
  return { isNew: false, fields };
}
function renderPRs() {
  if (!prData) return;
  const prs = prData.prs || [];
  const visit = currentPRVisit();
  updatePRRepositories(prs);
  const query = $("pr-search").value.toLowerCase();
  const role = $("pr-role").value;
  const ci = $("pr-ci").value;
  const repo = $("pr-repo").value;
  const review = $("pr-review").value;
  const visible = prs.filter(
    (pr) =>
      (role === "all" || pr.roles.includes(role)) &&
      (ci === "all" || pr.ci === ci || (ci === "UNKNOWN" && !ciStates[pr.ci])) &&
      (repo === "all" || pr.repo === repo) &&
      (review === "all" || (review === "draft" ? pr.draft : !pr.draft)) &&
      `${pr.repo} ${pr.title} ${pr.number} ${pr.author || ""}`.toLowerCase().includes(query),
  );
  visible.sort(comparePRs);
  for (const column of Object.keys(prColumns)) {
    $(`pr-heading-${column}`).setAttribute(
      "aria-sort",
      column === prSort ? (prAscending ? "ascending" : "descending") : "none",
    );
  }
  $("pr-sort").value = prSort;
  $("pr-sort-direction").textContent = prAscending ? "Ascending" : "Descending";
  $("pr-count").textContent = `${visible.length} / ${prs.length}`;
  const sync = prData.synced_at ? new Date(prData.synced_at * 1000).toLocaleString() : null;
  $("pr-sync").textContent =
    `${prData.login ? `@${prData.login} · ` : ""}Open PRs · Sorted by ${prColumns[prSort]} (${prAscending ? "ascending" : "descending"}) · ${sync ? `Synced ${sync}` : "Not synced yet"}${prData.refreshing ? " · Syncing…" : " · GitHub refreshes every 5 minutes"}`;
  const issues = [prData.error, ...(prData.warnings || [])].filter(Boolean);
  $("pr-alert").hidden = !issues.length;
  $("pr-alert").textContent =
    issues.join(" ") +
    (prData.error && sync ? " Showing saved results; they may be out of date." : "");
  $("pr-list").replaceChildren();
  let newCount = 0,
    changedCount = 0;
  for (const pr of visible) {
    const row = el("tr");
    const title = el("td");
    const titleLink = link(pr.title, pr.url);
    titleLink.className = "pr-title";
    title.append(titleLink, el("span", `#${pr.number}`, "pr-meta"));
    const author = el("td", undefined, "pr-author");
    author.append(
      pr.author ? link(pr.author, `https://github.com/${pr.author}`) : el("span", "Unknown"),
    );
    const readiness = el("td", undefined, "pr-readiness");
    for (const [label, color] of prReviewBadges(pr))
      readiness.append(el("span", label, `badge ${color}`));
    const roles = el("td");
    const tags = el("div", undefined, "pr-roles");
    for (const value of pr.roles) tags.append(el("span", roleNames[value] || value, "badge"));
    roles.append(tags);
    const checks = el("td");
    const [label, color] = ciStates[pr.ci] || ["Unknown", ""];
    const checksLink = link(label, `${pr.url}/checks`);
    checksLink.className = `badge ${color}`;
    checksLink.setAttribute("aria-label", `CI ${label} for ${pr.repo} #${pr.number}`);
    checksLink.onclick = (event) => {
      if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
      event.preventDefault();
      openPRCI(pr);
    };
    checks.append(checksLink);
    const opened = prDate(pr.opened_at, "Opened", "pr-opened");
    const updated = prDate(pr.updated_at, "Updated", "pr-updated");
    const repo = el("td", undefined, "pr-repo");
    repo.append(link(pr.repo, `https://github.com/${pr.repo}`));
    title.className = "pr-description";
    const changes = prChanges(pr, visit);
    if (changes) {
      row.className = changes.isNew ? "pr-new" : "pr-changed";
      if (changes.isNew) newCount += 1;
      else changedCount += 1;
      title.append(
        el(
          "span",
          changes.isNew ? "New" : "Updated",
          `badge ${changes.isNew ? "blue" : "amber"} pr-change-badge`,
        ),
      );
      title.append(
        el(
          "small",
          changes.isNew
            ? "Since your last visit"
            : changes.fields.length
              ? changes.fields.map((field) => prChangeFields[field]).join(" · ")
              : "PR activity changed",
          "pr-change-note",
        ),
      );
      const cells = {
        repo,
        title,
        author,
        ci: checks,
        review_decision: readiness,
        draft: readiness,
        roles,
        head_sha: updated,
      };
      for (const field of changes.fields) cells[field].classList.add("pr-field-changed");
      if (!changes.isNew && !changes.fields.length) updated.classList.add("pr-field-changed");
    }
    row.append(repo, title, author, readiness, roles, checks, opened, updated, workspaceCell(pr));
    $("pr-list").append(row);
  }
  rememberPRVisit(visit);
  $("pr-changes").hidden = !visit;
  $("pr-changes").textContent = !visit
    ? ""
    : !visit.storage
      ? "Changes are tracked for this visit only; browser storage is unavailable."
      : prData.synced_at < visit.baseline.synced_at
        ? "Waiting for a current PR snapshot to compare with your last visit."
        : newCount || changedCount
          ? `Since your last visit: ${newCount} new · ${changedCount} updated in this view.`
          : visit.first
            ? "Changes will be highlighted from this visit onward, in this browser."
            : "No changes since your last visit in this view.";
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
function showPage(name) {
  for (const page of ["watcher", "prs"]) {
    const active = page === name;
    $(`${page}-panel`).hidden = !active;
    $(`${page}-tab`).setAttribute("aria-selected", String(active));
    $(`${page}-tab`).tabIndex = active ? 0 : -1;
  }
  if (name === "prs") renderPRs();
}
function pageFromURL() {
  showPage(window.location.hash === "#prs" ? "prs" : "watcher");
}
for (const page of ["watcher", "prs"]) {
  $(`${page}-tab`).onclick = () => {
    window.location.hash = page;
    showPage(page);
    void refreshWorkspaces();
  };
  $(`${page}-tab`).onkeydown = (event) => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const next =
      event.key === "Home"
        ? "watcher"
        : event.key === "End"
          ? "prs"
          : page === "watcher"
            ? "prs"
            : "watcher";
    $(`${next}-tab`).click();
    $(`${next}-tab`).focus();
  };
}
window.addEventListener("hashchange", pageFromURL);
pageFromURL();
for (const column of Object.keys(prColumns)) {
  $(`pr-sort-${column}`).onclick = () => sortPRs(column, true);
}
$("pr-sort").onchange = () => sortPRs($("pr-sort").value);
$("pr-sort-direction").onclick = () => sortPRs(prSort, true);
$("pr-refresh").onclick = refreshPRs;
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
$("pr-repo").onchange = renderPRs;
$("pr-review").onchange = renderPRs;
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

let workspaceData = { prs: {} },
  workspaceBusy = false;
async function refreshWorkspaces() {
  if (workspaceBusy || document.hidden || $("prs-panel").hidden) return;
  workspaceBusy = true;
  try {
    workspaceData = await get("/api/workspaces");
    renderPRs();
    updateWorkspaceOperation();
  } catch (error) {
    workspaceData.error = error.message;
  } finally {
    workspaceBusy = false;
  }
}
let workspaceDialogPR = null;
function workspaceButton(label, callback) {
  const button = el("button", label);
  button.type = "button";
  button.onclick = callback;
  return button;
}
function workspaceCell(pr) {
  const cell = el("td", undefined, "pr-actions");
  const info = workspaceData.prs[pr.id];
  const matches = info?.matches || [];
  const label = matches.some((m) => m.workspace_id)
    ? "Open workspace"
    : matches.length
      ? "Reopen workspace"
      : info?.clones.length
        ? "Create workspace"
        : "Clone and create";
  const button = workspaceButton(info ? label : "Workspace actions", () => {
    if (matches.length === 1 && matches[0].workspace_id) {
      void chooseWorkspace(pr, matches[0], "open");
    } else void workspaceDialog(pr);
  });
  cell.append(button);
  if (matches.length === 1) {
    const menu = el("details");
    menu.append(el("summary", "More actions"));
    if (matches[0].workspace_id)
      menu.append(
        workspaceButton("Focus in herdr", () => chooseWorkspace(pr, matches[0], "focus")),
      );
    menu.append(workspaceButton("Copy command", () => chooseWorkspace(pr, matches[0], "copy")));
    cell.append(menu);
  }
  if (info?.operation) cell.append(el("small", info.operation.message));
  return cell;
}
async function workspaceRequest(pr, action, params = {}) {
  const response = await fetch("/api/workspace-action", {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Babysit-Action": "workspace-action" },
    body: JSON.stringify({ id: pr.id, action, ...params }),
  });
  const value = await response.json();
  if (!response.ok) throw Error(value.error || `HTTP ${response.status}`);
  return value;
}
function workspaceError(error) {
  $("workspace-error").textContent = error.message;
}
async function chooseWorkspace(pr, target, action) {
  const opened = action === "open" ? window.open("about:blank", "_blank") : null;
  if (opened) opened.opener = null;
  $("workspace-error").textContent = "";
  const params = { path: target.path };
  if (target.workspace_id) params.workspace_id = target.workspace_id;
  try {
    const value = await workspaceRequest(pr, action, params);
    if (action === "copy") {
      await navigator.clipboard.writeText(value.command);
      $("workspace-error").textContent = "Command copied";
    } else if (value.result && action === "open") {
      if (opened) opened.location.href = value.result.url;
      else {
        await workspaceDialog(pr);
        $("workspace-error").textContent = "Use Open in Collie to continue.";
      }
      $("workspace-result").replaceChildren(link("Open in Collie", value.result.url));
    } else if (value.operation) {
      workspaceData.prs[pr.id].operation = value.operation;
      updateWorkspaceOperation();
    }
  } catch (error) {
    if (opened) opened.close();
    if (!$("workspace-dialog").open) await workspaceDialog(pr);
    workspaceError(error);
  }
}
async function workspaceDialog(pr) {
  workspaceDialogPR = pr.id;
  $("workspace-title").textContent = `${pr.repo} #${pr.number}`;
  $("workspace-error").textContent = "";
  $("workspace-content").replaceChildren(el("p", "Discovering local workspaces…"));
  $("workspace-result").replaceChildren();
  $("workspace-progress").textContent = "";
  $("workspace-log").textContent = "";
  if (!$("workspace-dialog").open) $("workspace-dialog").showModal();
  try {
    // Discovery is shared and cached; every action revalidates the selected target.
    workspaceData = await get("/api/workspaces");
    if (workspaceDialogPR !== pr.id) return;
    const info = workspaceData.prs[pr.id];
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
        row.append(
          el("strong", target.name),
          el("small", `${target.agent_status} · ${target.path}`),
        );
        row.append(
          workspaceButton(target.workspace_id ? "Open workspace" : "Reopen workspace", () =>
            chooseWorkspace(pr, target, target.workspace_id ? "open" : "reopen"),
          ),
        );
        const menu = el("details");
        menu.append(el("summary", "More actions"));
        if (target.workspace_id)
          menu.append(
            workspaceButton("Focus in herdr", () => chooseWorkspace(pr, target, "focus")),
          );
        menu.append(workspaceButton("Copy command", () => chooseWorkspace(pr, target, "copy")));
        row.append(menu);
        body.append(row);
      }
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
              ? `Clone ${pr.repo} into ${info.destination}`
              : "Both clone destinations already exist. Move conflicting content before trying again.",
          ),
        );
      field("Agent", agent);
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
            pr,
            info.clones.length ? "create" : "clone-and-create",
            {
              agent: agent.value,
              task: task.value,
              ...(info.clones.length ? { clone: clone.value } : { destination: info.destination }),
              retry: workspaceData.prs[pr.id]?.operation?.status === "failed",
            },
          );
          workspaceData.prs[pr.id].operation = value.operation;
          updateWorkspaceOperation();
        } catch (error) {
          workspaceError(error);
          submit.disabled = false;
        }
      };
      task.oninput = () => task.setCustomValidity("");
      body.append(form);
      if (info.suggestions.length)
        body.append(el("p", `Unverified branch-name suggestions: ${info.suggestions.join(", ")}`));
    }
    updateWorkspaceOperation();
  } catch (error) {
    workspaceError(error);
  }
}
function updateWorkspaceOperation() {
  if (!workspaceDialogPR || !$("workspace-dialog").open) return;
  const op = workspaceData.prs[workspaceDialogPR]?.operation;
  if (!op) return;
  $("workspace-progress").textContent = `${op.status}: ${op.message}`;
  $("workspace-log").textContent = op.log || "";
  if (op.result?.url) $("workspace-result").replaceChildren(link("Open in Collie", op.result.url));
  const submit = $("workspace-content").querySelector("button[type=submit]");
  if (submit) submit.disabled = op.status !== "failed";
}
$("workspace-close").onclick = () => $("workspace-dialog").close();
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
    if (!document.hidden && !$("prs-panel").hidden) {
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
$("ci-dialog").onclick = (event) => {
  const dialog = $("ci-dialog");
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
      !$("prs-panel").hidden &&
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
