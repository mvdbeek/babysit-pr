import { api, dashboardURL, safeLink, taskText } from "./shared.js";
import { infer, suggestedTask, branchName, pageKey } from "./inference.js";
const $ = (id) => document.getElementById(id);
let draftId = location.hash.slice(1);
if (!/^[a-f0-9-]{36}$/.test(draftId)) {
  draftId = crypto.randomUUID();
  history.replaceState(null, "", `#${draftId}`);
}
let draft = (await chrome.storage.session.get(draftId))[draftId] || {};
const tab = await chrome.tabs.getCurrent();
if (tab) draft.tabId = tab.id;
const manual = new Set(draft.manual || (draft.fields ? Object.keys(draft.fields) : []));
let context = null,
  target = null,
  agents = [],
  preferences = {},
  preferenceKey = "",
  polling = 0;
let loading = false,
  sending = false,
  recommendation = null;
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
  "content",
  "links",
];
function values() {
  return Object.fromEntries(
    fields.map((id) => [id, id === "babysit" ? $(id).checked : $(id).value]),
  );
}
async function save() {
  draft = { ...draft, fields: values(), manual: [...manual] };
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
  select(id, previous);
}
function select(id, value) {
  if ([...$(id).options].some((o) => o.value === value)) $(id).value = value;
}
function repoChoice() {
  return context?.repos.find((r) => JSON.stringify([r.repo, r.clone]) === $("repo").value);
}
function efforts(wanted = $("effort").value) {
  const choices = context?.agent_choices?.[$("agent").value];
  const model = choices?.models.find((m) => m.id === $("model").value);
  const levels = model?.efforts || choices?.efforts || [];
  const defaults = context?.effort_defaults || {};
  const level = [defaults.repos?.[repoChoice()?.repo.toLowerCase()], defaults.effort].find((v) =>
    levels.includes(v),
  );
  options(
    "effort",
    levels.map((e) => ({ value: e, label: e })),
    level ? `Dashboard default (${level})` : "Agent default",
  );
  select("effort", wanted);
}
function settings() {
  const repo = repoChoice()?.repo.toLowerCase();
  const remembered = preferences.repos?.[repo] || preferences.last || {};
  const saved = draft.fields || {};
  const want = (field) =>
    manual.has(field) ? (saved[field] ?? $(field).value) : (remembered[field] ?? "");
  select("agent", want("agent") || "codex");
  const choices = context?.agent_choices?.[$("agent").value];
  options(
    "model",
    (choices?.models || []).map((m) => ({ value: m.id, label: m.id })),
    "Agent default",
  );
  select("model", want("model"));
  options(
    "account",
    (choices?.accounts || [])
      .filter((a) => a.id !== "default")
      .map((a) => ({ value: a.id, label: a.label })),
    "Default",
  );
  select("account", want("account"));
  $("account").disabled = $("agent").value !== "claude";
  efforts(want("effort"));
}
function source() {
  const result = {};
  if ($("source-url").value) {
    const url = new URL($("source-url").value);
    if (!["http:", "https:"].includes(url.protocol)) throw Error("Use an HTTP or HTTPS page URL.");
    result.url = url.href;
  }
  for (const [key, id] of [
    ["title", "source-title"],
    ["selection", "selection"],
    ["content", "content"],
    ["links", "links"],
  ])
    if ($(id).value) result[key] = $(id).value;
  return result;
}
function recipientKey(a) {
  return `${a.workspace}:${a.pane}:${a.session}`;
}
function chosenAgent() {
  return agents.find((a) => recipientKey(a) === $("recipient").value);
}
function fillAgents() {
  const exact = new Set((recommendation?.matching || []).map(recipientKey));
  agents = [...(context?.agents || [])]
    .filter((a) => a.session)
    .sort((a, b) => Number(exact.has(recipientKey(b))) - Number(exact.has(recipientKey(a))));
  options(
    "recipient",
    agents.map((a) => ({
      value: recipientKey(a),
      label: `${exact.has(recipientKey(a)) ? "This page · " : ""}${a.label} · ${a.agent}${a.status ? ` · ${a.status}` : ""}`,
    })),
    "Choose a conversation",
  );
}
function render() {
  $("composer-fields").disabled = sending || Boolean(draft.sent);
  $("name").placeholder = branchName($("task").value, $("source-title").value, draftId.slice(0, 8));
  const mode = $("mode").value,
    message = mode === "message";
  $("launch-fields").hidden = message;
  $("message-fields").hidden = !message;
  $("branch-fields").hidden = mode !== "new";
  $("handoff").disabled = message || Boolean(draft.sent) || sending;
  $("send").disabled = !context || loading || sending || Boolean(draft.sent);
  $("send").textContent = message ? "Send follow-up" : "Start task";
  const recipient = chosenAgent(),
    repo = repoChoice();
  $("destination-summary").textContent = message
    ? recipient
      ? `Continue ${recipient.label} · ${recipient.agent}`
      : "Choose the conversation to continue"
    : `${mode === "target" && target ? `Work on ${target.title || target.url}` : "New task"}${repo ? ` in ${repo.repo}` : " · choose a repository"} · ${$("agent").value}${$("model").value ? ` / ${$("model").value}` : ""}`;
  $("target-note").textContent = message
    ? "Your instructions and page context will go to this existing conversation."
    : recommendation?.requestedRepo && !repo
      ? `No local clone of ${recommendation.requestedRepo} was found. Choose its clone or open the dashboard.`
      : mode === "new" && repo
        ? `${recommendation?.reason || "Selected repository"}.`
        : target?.url || "";
  $("suggested-agents").replaceChildren();
  if (!message)
    for (const a of (recommendation?.matching.length
      ? recommendation.matching
      : recommendation?.suggestedAgents || []
    ).slice(0, 3)) {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = `Continue ${a.label}`;
      button.onclick = () => {
        $("mode").value = "message";
        select("recipient", recipientKey(a));
        manual.add("mode");
        manual.add("recipient");
        draft.pending = null;
        render();
        void save();
      };
      $("suggested-agents").append(button);
    }
}
function suggest() {
  if (!context) return;
  recommendation = infer(source(), context, preferences, $("task").value);
  target = recommendation.target;
  $("mode").querySelector('[value="target"]').disabled = !target;
  if (!manual.has("repo"))
    $("repo").value = recommendation.repository
      ? JSON.stringify([recommendation.repository.repo, recommendation.repository.clone])
      : "";
  if (!manual.has("mode"))
    $("mode").value = recommendation.matching.length ? "message" : target ? "target" : "new";
  if (!target && $("mode").value === "target") $("mode").value = "new";
  fillAgents();
  if (manual.has("recipient")) select("recipient", draft.fields?.recipient);
  else if (recommendation.matching.length === 1)
    select("recipient", recipientKey(recommendation.matching[0]));
  if (!manual.has("task")) $("task").value = suggestedTask(source(), target);
  settings();
  if (
    ($("mode").value === "message" && !chosenAgent()) ||
    ($("mode").value !== "message" && !repoChoice())
  )
    $("choices").open = true;
  render();
}
function quickActions() {
  const actions = [
    [
      "Fix the problem",
      "Investigate the problem described on this page and implement a fix with appropriate tests.",
    ],
    [
      "Fix CI",
      "Diagnose and fix the failing CI checks. Run the relevant tests, commit and push the fix.",
    ],
    [
      "Handle feedback",
      "Address the valid findings in the selected review feedback. Test the changes, commit and push. Do not reply to or resolve review threads.",
    ],
    [
      "Explain",
      "Explain the selected text or the relevant behavior on this page in the context of this repository.",
    ],
  ];
  for (const [label, text] of actions) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = label;
    button.onclick = () => {
      $("task").value = text;
      manual.add("task");
      draft.pending = null;
      render();
      void save();
      $("task").focus();
    };
    $("quick-actions").append(button);
  }
}
async function load() {
  if (loading || sending) return;
  loading = true;
  $("reload").disabled = true;
  render();
  try {
    const { config } = await chrome.storage.local.get("config");
    if (config?.url) {
      safeLink($("dashboard"), dashboardURL(config.url));
      preferenceKey = `preferences:${dashboardURL(config.url)}`;
      preferences = (await chrome.storage.local.get(preferenceKey))[preferenceKey] || {};
    }
    context = await api({ action: "context" });
    options(
      "repo",
      context.repos.map((r) => ({
        value: JSON.stringify([r.repo, r.clone]),
        label: `${r.repo} · ${r.clone}`,
      })),
      "Choose a repository",
    );
    select("repo", draft.fields?.repo);
    try {
      suggest();
    } catch {
      /* A half-typed source URL keeps the loaded context; Send reports it. */
    }
    if (!draft.sent) $("status").textContent = context.agents_error || "Ready.";
  } catch (error) {
    context = null;
    $("status").textContent =
      `${error.message} You can still use Open in dashboard after saving its URL in Settings.`;
  } finally {
    loading = false;
    $("reload").disabled = false;
    render();
  }
}
async function showOperation(operation) {
  clearTimeout(polling);
  $("status").textContent = `${operation.status}: ${operation.message || "Task accepted"}`;
  safeLink($("workspace"), operation.result?.url);
  if (["queued", "running"].includes(operation.status))
    polling = setTimeout(async () => {
      try {
        await showOperation((await api({ action: "status", id: operation.id })).operation);
      } catch (error) {
        $("status").textContent =
          `Task accepted. ${error.message} Check the dashboard for progress.`;
      }
    }, 2000);
}
for (const [key, id] of [
  ["url", "source-url"],
  ["title", "source-title"],
  ["selection", "selection"],
  ["content", "content"],
  ["links", "links"],
])
  $(id).value = draft.source?.[key] || "";
for (const [id, value] of Object.entries(draft.fields || {})) {
  if (!fields.includes(id)) continue;
  if (id === "babysit") $(id).checked = value;
  else $(id).value = value;
}
$("task-form").addEventListener("input", (event) => {
  const id = event.target.id;
  if (!fields.includes(id)) return;
  manual.add(id);
  draft.pending = null;
  draft.fields = values();
  if (["task", "source-url", "links", "selection"].includes(id)) {
    try {
      suggest();
    } catch {
      /* Allow incomplete URLs while typing. */
    }
  } else if (["agent", "repo"].includes(id)) settings();
  else if (id === "model") efforts("");
  render();
  void save();
});
$("task-form").addEventListener("change", () => {
  render();
  void save();
});
$("reload").onclick = () => void load();
window.addEventListener("focus", () => {
  if (!draft.sent) void load();
});
chrome.storage.onChanged.addListener((changes, area) => {
  if (area === "local" && changes.config && !draft.sent) void load();
});
$("handoff").onclick = async () => {
  try {
    const { config } = await chrome.storage.local.get("config");
    if (!config?.url) throw Error("Save the dashboard URL in Settings first.");
    const task = taskText($("task").value, source(), $("babysit").checked);
    const repo = repoChoice()?.repo || recommendation?.requestedRepo;
    const handoff = {
      task,
      repo,
      name:
        $("name").value ||
        branchName($("task").value, $("source-title").value, draftId.slice(0, 8)),
      base: $("base").value,
    };
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
  if (sending || draft.sent) return;
  sending = true;
  render();
  try {
    const request =
      draft.pending ||
      (() => {
        const reference = source();
        taskText($("task").value, reference, $("babysit").checked);
        const mode = $("mode").value;
        let payload;
        if (mode === "message") {
          const recipient = chosenAgent();
          if (!recipient) {
            $("choices").open = true;
            throw Error("Choose the conversation to continue.");
          }
          payload = {
            workspace: recipient.workspace,
            pane: recipient.pane,
            session: recipient.session,
            text: $("task").value,
          };
        } else {
          const choice = repoChoice();
          if (!choice) {
            $("choices").open = true;
            throw Error("Choose a repository and clone.");
          }
          payload = {
            clone: choice.clone,
            agent: $("agent").value,
            model: $("model").value,
            effort: $("effort").value,
            task: $("task").value,
          };
          if ($("agent").value === "claude" && $("account").value)
            payload.claude_account = $("account").value;
          if (mode === "target") {
            if (!target) throw Error("Reload the GitHub target first.");
            if (choice.repo.toLowerCase() !== target.repo.toLowerCase())
              throw Error("Choose a clone of the GitHub target's repository.");
            Object.assign(payload, { id: target.id, action: "handle" });
          } else
            Object.assign(payload, {
              repo: choice.repo,
              name:
                $("name").value ||
                branchName($("task").value, $("source-title").value, draftId.slice(0, 8)),
              base: $("base").value,
            });
        }
        return {
          action: "submit",
          mode,
          payload,
          source: reference,
          babysit: $("babysit").checked,
        };
      })();
    const mode = request.mode,
      reference = request.source;
    // Storage may reorder object keys. Keep the ID of the frozen request until a
    // person edits a field, rather than comparing its serialized representation.
    if (!draft.pending || !draft.requestId) draft.requestId = crypto.randomUUID();
    draft.pending = request;
    for (const field of fields) manual.add(field);
    await save();
    $("status").textContent = "Sending…";
    const result = await api({ ...request, request_id: draft.requestId });
    draft.sent = result;
    await save();
    if (mode !== "message" && preferenceKey) {
      const chosen = {
        ...repoChoice(),
        agent: $("agent").value,
        model: $("model").value,
        effort: $("effort").value,
        account: $("account").value,
      };
      preferences = (await chrome.storage.local.get(preferenceKey))[preferenceKey] || {};
      preferences.last = chosen;
      preferences.repos = { ...preferences.repos, [chosen.repo.toLowerCase()]: chosen };
      preferences.sites = { ...preferences.sites, [pageKey(reference)]: chosen };
      await chrome.storage.local.set({ [preferenceKey]: preferences });
    }
    if (result.operation) await showOperation(result.operation);
    else {
      $("status").textContent = `Follow-up sent. ${result.message?.warning || ""}`;
      safeLink($("workspace"), chosenAgent()?.url);
    }
  } catch (error) {
    $("status").textContent =
      `${error.message}\nIf delivery is uncertain, check the dashboard before changing the task. Retrying the unchanged task will reuse its request ID.`;
  } finally {
    sending = false;
    render();
  }
};
quickActions();
await load();
await save();
if (draft.sent) {
  if (draft.sent.operation) await showOperation(draft.sent.operation);
  else $("status").textContent = `Follow-up already sent. ${draft.sent.message?.warning || ""}`;
}
