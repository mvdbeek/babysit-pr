import { api, dashboardURL, githubTarget, safeLink, taskText } from "./shared.js";
const $ = (id) => document.getElementById(id);
let draftId = location.hash.slice(1);
if (!/^[a-f0-9-]{36}$/.test(draftId)) {
  draftId = crypto.randomUUID();
  history.replaceState(null, "", `#${draftId}`);
}
let draft = (await chrome.storage.session.get(draftId))[draftId] || {};
let context = null;
let target = null;
let agents = [];
let polling = 0;
const fields = [
  "mode",
  "repo",
  "name",
  "base",
  "agent",
  "model",
  "effort",
  "account",
  "recipient",
  "task",
  "babysit",
  "source-url",
  "source-title",
  "selection",
];
function values() {
  return Object.fromEntries(
    fields.map((id) => [id, id === "babysit" ? $(id).checked : $(id).value]),
  );
}
async function save() {
  draft = { ...draft, fields: values() };
  await chrome.storage.session.set({ [draftId]: draft });
}
function options(id, choices, empty) {
  const previous = $(id).value;
  $(id).replaceChildren();
  for (const choice of [...(empty ? [{ value: "", label: empty }] : []), ...choices]) {
    const option = document.createElement("option");
    option.value = choice.value;
    option.textContent = choice.label;
    $(id).append(option);
  }
  if ([...$(id).options].some((o) => o.value === previous)) $(id).value = previous;
}
function efforts() {
  const choices = context?.agent_choices?.[$("agent").value];
  const model = choices?.models.find((m) => m.id === $("model").value);
  options(
    "effort",
    (model?.efforts || choices?.efforts || []).map((e) => ({ value: e, label: e })),
    "Default",
  );
}
function models() {
  const choices = context?.agent_choices?.[$("agent").value];
  options(
    "model",
    (choices?.models || []).map((m) => ({ value: m.id, label: m.id })),
    "Default",
  );
  options(
    "account",
    (choices?.accounts || [])
      .filter((a) => a.id !== "default")
      .map((a) => ({ value: a.id, label: a.label })),
    "Default",
  );
  $("account").disabled = $("agent").value !== "claude";
  efforts();
}
function source() {
  const result = {};
  if ($("source-url").value) {
    const url = new URL($("source-url").value);
    if (!["http:", "https:"].includes(url.protocol)) throw Error("Use an HTTP or HTTPS page URL.");
    result.url = url.href;
  }
  if ($("source-title").value) result.title = $("source-title").value;
  if ($("selection").value) result.selection = $("selection").value;
  return result;
}
function detectTarget() {
  const github = githubTarget($("source-url").value);
  target =
    (github &&
      context?.targets.find(
        (t) => githubTarget(t.url)?.url.toLowerCase() === github.url.toLowerCase(),
      )) ||
    null;
  $("mode").querySelector('[value="target"]').disabled = !target;
  if (!target && $("mode").value === "target") $("mode").value = "new";
  $("target-note").textContent = target
    ? `GitHub target: ${target.repo} · ${target.title || target.url}`
    : github
      ? "This PR or issue is not in the dashboard overview yet. Reload choices after it appears, or start a new task with its URL as context."
      : "";
}
async function modeChanged() {
  const message = $("mode").value === "message";
  $("launch-fields").hidden = message;
  $("message-fields").hidden = !message;
  $("branch-fields").hidden = $("mode").value !== "new";
  $("name").required = false; // Handoff can leave the branch choice to the dashboard.
  $("handoff").disabled = message;
  $("send").textContent = message ? "Send follow-up" : "Start task";
  if (message) {
    try {
      agents = (await api({ action: "agents" })).agents;
      options(
        "recipient",
        agents.map((a, i) => ({ value: String(i), label: `${a.agent} · ${a.label}` })),
        "Choose an agent",
      );
    } catch (error) {
      $("status").textContent = error.message;
    }
  }
}
async function load() {
  $("reload").disabled = true;
  try {
    const { config } = await chrome.storage.local.get("config");
    if (config?.url) safeLink($("dashboard"), dashboardURL(config.url));
    context = await api({ action: "context" });
    const saved = draft.fields || {};
    options(
      "repo",
      context.repos.map((r) => ({
        value: JSON.stringify([r.repo, r.clone]),
        label: `${r.repo} · ${r.clone}`,
      })),
      "Choose a repository",
    );
    const github = githubTarget($("source-url").value);
    const match = context.repos.find((r) => r.repo.toLowerCase() === github?.repo.toLowerCase());
    if (saved.repo && [...$("repo").options].some((o) => o.value === saved.repo))
      $("repo").value = saved.repo;
    else if (match) $("repo").value = JSON.stringify([match.repo, match.clone]);
    models();
    if (saved.model && [...$("model").options].some((o) => o.value === saved.model))
      $("model").value = saved.model;
    efforts();
    for (const id of ["effort", "account"])
      if (saved[id] && [...$(id).options].some((o) => o.value === saved[id]))
        $(id).value = saved[id];
    detectTarget();
    if (!saved.mode && target) $("mode").value = "target";
    await modeChanged();
    if (!draft.sent) $("status").textContent = "Ready.";
  } catch (error) {
    $("status").textContent =
      `${error.message} You can still use Open in dashboard after saving its URL in Settings.`;
  } finally {
    $("reload").disabled = false;
  }
}
async function showOperation(operation) {
  clearTimeout(polling);
  $("status").textContent = `${operation.status}: ${operation.message || "Task accepted"}`;
  safeLink($("workspace"), operation.result?.url);
  if (["queued", "running"].includes(operation.status)) {
    polling = setTimeout(async () => {
      try {
        const result = await api({ action: "status", id: operation.id });
        await showOperation(result.operation);
      } catch (error) {
        $("status").textContent =
          `Task accepted. ${error.message} Check the dashboard for progress.`;
      }
    }, 2000);
  }
}
$("source-url").value = draft.source?.url || "";
$("source-title").value = draft.source?.title || "";
$("selection").value = draft.source?.selection || "";
for (const [id, value] of Object.entries(draft.fields || {})) {
  if (!fields.includes(id)) continue;
  if (id === "babysit") $(id).checked = value;
  else $(id).value = value;
}
models();
$("agent").onchange = models;
$("model").onchange = efforts;
$("mode").onchange = () => void modeChanged();
$("source-url").onchange = () => {
  detectTarget();
  void modeChanged();
};
$("task-form").addEventListener("input", () => void save());
$("task-form").addEventListener("change", () => void save());
$("reload").onclick = () => void load();
$("handoff").onclick = async () => {
  try {
    const { config } = await chrome.storage.local.get("config");
    if (!config?.url) throw Error("Save the dashboard URL in Settings first.");
    const task = taskText($("task").value, source(), $("babysit").checked);
    const repo = $("repo").value
      ? JSON.parse($("repo").value)[0]
      : githubTarget($("source-url").value)?.repo;
    const handoff = { task, repo, name: $("name").value, base: $("base").value };
    await chrome.tabs.create({
      url: `${dashboardURL(config.url)}/#task=${encodeURIComponent(JSON.stringify(handoff))}`,
    });
    $("status").textContent = "Task opened in the dashboard. Review it there and press Start task.";
  } catch (error) {
    $("status").textContent = error.message;
  }
};
$("task-form").onsubmit = async (event) => {
  event.preventDefault();
  $("send").disabled = true;
  try {
    const reference = source();
    taskText($("task").value, reference, $("babysit").checked);
    const mode = $("mode").value;
    let payload;
    if (mode === "message") {
      const recipient = agents[Number($("recipient").value)];
      if (!$("recipient").value || !recipient) throw Error("Choose an existing agent.");
      payload = {
        workspace: recipient.workspace,
        pane: recipient.pane,
        session: recipient.session,
        text: $("task").value,
      };
    } else {
      if (!context) throw Error("Connect and reload choices first.");
      if (!$("repo").value) throw Error("Choose a repository and clone.");
      const [repo, clone] = JSON.parse($("repo").value);
      payload = {
        clone,
        agent: $("agent").value,
        model: $("model").value,
        effort: $("effort").value,
        task: $("task").value,
      };
      if ($("agent").value === "claude" && $("account").value)
        payload.claude_account = $("account").value;
      if (mode === "target") {
        if (!target) throw Error("Reload the GitHub target first.");
        if (repo.toLowerCase() !== target.repo.toLowerCase())
          throw Error("Choose a clone of the GitHub target's repository.");
        Object.assign(payload, { id: target.id, action: "handle" });
      } else {
        if (!$("name").value.trim()) throw Error("Name the new branch.");
        Object.assign(payload, { repo, name: $("name").value, base: $("base").value });
      }
    }
    const request = {
      action: "submit",
      mode,
      payload,
      source: reference,
      babysit: $("babysit").checked,
    };
    const fingerprint = JSON.stringify(request);
    if (draft.fingerprint !== fingerprint) draft.requestId = crypto.randomUUID();
    draft.fingerprint = fingerprint;
    await save(); // Save the ID before network delivery, including across composer reloads.
    $("status").textContent = "Sending…";
    const result = await api({ ...request, request_id: draft.requestId });
    draft.sent = result;
    await save();
    $("handoff").disabled = true;
    if (result.operation) await showOperation(result.operation);
    else $("status").textContent = `Follow-up sent. ${result.message?.warning || ""}`;
  } catch (error) {
    $("status").textContent =
      `${error.message}\nIf delivery is uncertain, check the dashboard before changing the task. Retrying the unchanged task will reuse its request ID.`;
    $("send").disabled = false;
  }
};
await load();
if (draft.sent) {
  $("send").disabled = true;
  $("handoff").disabled = true;
  if (draft.sent.operation) await showOperation(draft.sent.operation);
  else $("status").textContent = `Follow-up already sent. ${draft.sent.message?.warning || ""}`;
}
