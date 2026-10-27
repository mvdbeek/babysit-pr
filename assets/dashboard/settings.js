"use strict";
// What the page prefills and who reviews: the task behind each Handle button, and the
// agent a review starts with. Both used to be fixed in this file, so changing a word
// meant editing the checkout; they are kept with the watcher instead, where a phone on
// the tailnet can change them and every device sees the same ones. With workers
// registered, also how many agents this machine takes before Auto places elsewhere.
(() => {
  const byId = (id) => document.getElementById(`settings-${id}`);
  const dialog = byId("dialog");
  // Where a prompt names its pull request or issue, as the batch dialog writes it too.
  const URL_TOKEN = "{url}";
  const NAMES = { codex: "Codex", claude: "Claude" };
  // The wording the page starts from. A saved prompt replaces one of these; the task
  // keys are what the server stores them under.
  const TASKS = [
    {
      id: "prReview",
      label: "Review pull request",
      prompt: `Review ${URL_TOKEN}. Read its description, linked issues, and the full diff against its base branch, then check out the branch and read the code the change touches. Look for correctness bugs, regressions, missing tests, and unclear or unnecessarily complex code. Do not push, comment, or submit a review on GitHub. Report each finding ranked by severity with file and line references, and say what you checked.`,
    },
    {
      id: "issue",
      label: "Handle issue",
      prompt: `Investigate and resolve ${URL_TOKEN}. Read the issue and relevant code, implement the fix, and run the appropriate tests. Summarize the changes and validation.`,
    },
    {
      id: "ci",
      label: "Handle failing tests",
      prompt: `Investigate and fix the failing tests and CI checks for ${URL_TOKEN}. Inspect the latest check results and failure logs, reproduce the failures where possible, fix their root causes, and run the relevant tests. Summarize the changes, validation, and any remaining failures.`,
    },
    {
      id: "review",
      label: "Handle review comments",
      prompt: `Address the review feedback on ${URL_TOKEN}. Read the submitted reviews and every unresolved review thread, including outdated ones, make the requested changes where they are sound, and run the relevant tests. Do not reply to or resolve threads on GitHub. Summarize each comment with what you changed, or why you did not.`,
    },
  ];
  const DEFAULTS = Object.fromEntries(TASKS.map((task) => [task.id, task.prompt]));
  // Until the saved settings arrive, and when they cannot be read, the page's own.
  let saved = { task_prompts: {}, reviewer: "codex", local_max_agents: 20 };
  let chosen = TASKS[0].id;
  // What was last written into the box. While it still holds that, a refresh may
  // replace it; once something else is in it, the person is typing and it stands.
  let shown = null;
  function prompt(id) {
    return saved.task_prompts[id] ?? DEFAULTS[id];
  }
  function edited(id) {
    return id in saved.task_prompts;
  }
  function status(text) {
    byId("status").textContent = text;
  }
  async function load() {
    const response = await fetch("/api/settings", { cache: "no-store" });
    const value = await response.json();
    if (!response.ok) throw Error(value.error || `HTTP ${response.status}`);
    saved = value;
    window.dispatchEvent(new CustomEvent("settings-updated"));
    return value;
  }
  async function send(body) {
    const response = await fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Babysit-Action": "settings" },
      body: JSON.stringify(body),
    });
    const value = await response.json();
    if (!response.ok) throw Error(value.error || `HTTP ${response.status}`);
    saved = value;
    window.dispatchEvent(new CustomEvent("settings-updated"));
    return value;
  }
  function renderReviewer() {
    const select = byId("reviewer");
    select.replaceChildren();
    for (const [agent, name] of Object.entries(NAMES)) {
      const option = document.createElement("option");
      option.value = agent;
      option.textContent = name;
      select.append(option);
    }
    select.value = saved.reviewer;
    // Every select on the page is the searchable picker; it reads its options again.
    window.dashboardAgents?.sync(select);
    byId("reviewer-note").textContent =
      `Review pull request starts ${NAMES[saved.reviewer]}. A card that needs you also ` +
      `offers the same review from ${NAMES[other()]}.`;
  }
  function showPrompt() {
    byId("prompt").value = prompt(chosen);
    shown = byId("prompt").value;
  }
  function renderTasks() {
    const select = byId("task");
    select.replaceChildren();
    for (const task of TASKS) {
      const option = document.createElement("option");
      option.value = task.id;
      option.textContent = edited(task.id) ? `${task.label} (edited)` : task.label;
      select.append(option);
    }
    select.value = chosen;
    window.dashboardAgents?.sync(select);
    if (shown === null || byId("prompt").value === shown) showPrompt();
    byId("reset").disabled = !edited(chosen);
  }
  function renderCapacity() {
    const input = byId("capacity");
    // A number being typed stands until it is saved or refused.
    if (document.activeElement !== input) input.value = String(saved.local_max_agents);
  }
  // Only placement reads it, so it is offered once another machine can take launches.
  async function showCapacity() {
    let machines = [];
    try {
      const response = await fetch("/api/machines", { cache: "no-store" });
      if (response.ok) machines = (await response.json()).machines ?? [];
    } catch {
      // Without the list the field stays hidden, as on a single machine.
    }
    byId("capacity-field").hidden = machines.length < 2;
    byId("capacity-note").hidden = machines.length < 2;
  }
  function render() {
    renderReviewer();
    renderCapacity();
    renderTasks();
  }
  function dirty() {
    return byId("prompt").value !== prompt(chosen);
  }
  async function savePrompt(text) {
    const save = byId("save");
    save.disabled = true;
    status("Saving…");
    try {
      await send({ task: chosen, prompt: text });
      showPrompt();
      renderTasks();
      status(text ? "Saved." : "Back to the default.");
    } catch (error) {
      status(`Could not save: ${error.message}`);
    } finally {
      save.disabled = false;
    }
  }
  function other(agent) {
    return (agent ?? saved.reviewer) === "codex" ? "claude" : "codex";
  }
  byId("task").onchange = (event) => {
    if (dirty() && !confirm("Discard the unsaved prompt?")) {
      // Back to the task still in the box, keeping what was typed there.
      event.target.value = chosen;
      window.dashboardAgents?.sync(event.target);
      return;
    }
    chosen = event.target.value;
    status("");
    showPrompt();
    renderTasks();
  };
  byId("save").onclick = () => void savePrompt(byId("prompt").value);
  byId("reset").onclick = () => void savePrompt("");
  byId("reviewer").onchange = async (event) => {
    const select = event.target;
    select.disabled = true;
    status("Saving…");
    try {
      await send({ reviewer: select.value });
      renderReviewer();
      status("Saved.");
    } catch (error) {
      renderReviewer();
      status(`Could not save: ${error.message}`);
    } finally {
      select.disabled = false;
    }
  };
  byId("capacity").onchange = async (event) => {
    const input = event.target;
    const value = Number(input.value);
    if (!Number.isInteger(value) || value < 1 || value > 200) {
      status("Choose a whole number of agents from 1 to 200");
      input.value = String(saved.local_max_agents);
      return;
    }
    input.disabled = true;
    status("Saving…");
    try {
      await send({ local_max_agents: value });
      status("Saved.");
    } catch (error) {
      status(`Could not save: ${error.message}`);
      input.value = String(saved.local_max_agents);
    } finally {
      renderCapacity();
      input.disabled = false;
    }
  };
  document.getElementById("settings-toggle").onclick = async () => {
    status("");
    render();
    void showCapacity();
    dialog.showModal();
    // Another device may have changed them; a failure leaves what this page knows.
    try {
      await load();
      render();
    } catch (error) {
      status(`Showing this page’s defaults: ${error.message}`);
    }
  };
  byId("close").onclick = () => dialog.close();
  dialog.addEventListener("close", () => {
    // An unsaved prompt is a draft, not a setting: the next opening shows what is saved.
    shown = null;
    status("");
  });
  // Close only when both press and release land outside the box, as for the services.
  const onBackdrop = (event) => {
    if (event.target !== dialog) return false;
    const rect = dialog.getBoundingClientRect();
    return (
      event.clientX < rect.left ||
      event.clientX > rect.right ||
      event.clientY < rect.top ||
      event.clientY > rect.bottom
    );
  };
  let pressedOnBackdrop = false;
  dialog.addEventListener("pointerdown", (event) => {
    pressedOnBackdrop = onBackdrop(event);
  });
  dialog.addEventListener("click", (event) => {
    if (pressedOnBackdrop && onBackdrop(event)) dialog.close();
    pressedOnBackdrop = false;
  });
  // The tasks the page prefills, and who reviews, for the rest of the dashboard.
  window.dashboardSettings = {
    label: (id) => TASKS.find((task) => task.id === id)?.label ?? id,
    // The task text for an item's link, or the `{url}` template for a batch.
    prompt: (id, url = URL_TOKEN) => prompt(id).replaceAll(URL_TOKEN, url),
    reviewer: () => saved.reviewer,
    other,
    name: (agent) => NAMES[agent] ?? agent,
  };
  void load().catch(() => {
    // The page's own wording stands in until the dialog asks again.
  });
})();
