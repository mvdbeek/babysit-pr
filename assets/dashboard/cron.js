/* Cron jobs: recurring agent tasks and shell commands, their schedules and run history. */
(() => {
  const byId = (id) => document.getElementById(`cron-${id}`);
  const STATUS = {
    starting: ["Starting", "blue"],
    running: ["Running", "blue"],
    attention: ["Needs attention", "amber"],
    succeeded: ["Succeeded", "green"],
    failed: ["Failed", "red"],
    timed_out: ["Timed out", "red"],
    error: ["Could not run", "red"],
    stopped: ["Stopped", "amber"],
    interrupted: ["Interrupted", "amber"],
    missed: ["Missed", "amber"],
    skipped: ["Skipped", ""],
  };
  const FAILING = new Set(["failed", "timed_out", "error", "attention"]);
  const AGENTS = { codex: "Codex", claude: "Claude" };
  const TRIGGERS = { schedule: "Scheduled", manual: "Run now" };
  let snapshot = null;
  let busy = false;
  let selectedJob = null;
  let selectedRun = null;
  // The run whose output is shown, and whether its log is being fetched.
  let output = { run: null, text: "", fetching: false };
  let editing = null;
  const acting = new Set();
  const jobErrors = new Map();

  function node(tag, text, className) {
    const element = document.createElement(tag);
    if (text !== undefined) element.textContent = text;
    if (className) element.className = className;
    return element;
  }
  function button(text, callback, { pending = false, disabled = false, key = text } = {}) {
    const element = node("button", text);
    element.type = "button";
    // A pending action keeps the button focusable, so focus survives the refresh after it.
    element.onclick = () => {
      if (!pending) callback();
    };
    element.disabled = disabled;
    if (pending) element.setAttribute("aria-disabled", "true");
    // Lets a rebuilt view give focus back to the same control, even when its label changes.
    element.dataset.key = key;
    return element;
  }
  // Views are rebuilt on every refresh; keep keyboard focus where it was.
  function rebuild(container, ...children) {
    const active = container.contains(document.activeElement)
      ? document.activeElement.dataset.key
      : null;
    container.replaceChildren(...children);
    if (active)
      [...container.querySelectorAll("[data-key]")]
        .find((element) => element.dataset.key === active)
        ?.focus({ preventScroll: true });
  }
  function badge(status) {
    const [label, tone] = STATUS[status] || [status, ""];
    return node("span", label, `badge ${tone}`.trim());
  }
  function when(seconds) {
    return new Date(seconds * 1000).toLocaleString(undefined, {
      weekday: "short",
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  }
  function relative(seconds) {
    const minutes = Math.round((seconds - Date.now() / 1000) / 60);
    const size = Math.abs(minutes);
    const text =
      size < 1
        ? null
        : size < 60
          ? `${size} min`
          : size < 48 * 60
            ? `${Math.round(size / 60)} h`
            : `${Math.round(size / 1440)} days`;
    if (!text) return seconds <= Date.now() / 1000 ? "just now" : "now";
    return minutes > 0 ? `in ${text}` : `${text} ago`;
  }
  function duration(run) {
    const end = run.finished_at ?? Date.now() / 1000;
    const total = Math.max(0, Math.round(end - run.started_at));
    if (total < 60) return `${total} s`;
    if (total < 3600) return `${Math.floor(total / 60)} min ${total % 60} s`;
    return `${Math.floor(total / 3600)} h ${Math.floor((total % 3600) / 60)} min`;
  }
  function unitOf(seconds) {
    if (seconds % 86400 === 0) return [seconds / 86400, 86400];
    if (seconds % 3600 === 0) return [seconds / 3600, 3600];
    return [seconds / 60, 60];
  }
  function frequency(schedule) {
    if (schedule.cron) return `Cron: ${schedule.cron}`;
    const [count, unit] = unitOf(schedule.every);
    const name = { 60: "minute", 3600: "hour", 86400: "day" }[unit];
    return count === 1 ? `Every ${name}` : `Every ${count} ${name}s`;
  }
  function agentSummary(job) {
    return [
      AGENTS[job.agent] || job.agent,
      job.model,
      job.effort && `${job.effort} effort`,
      job.claude_account && `Claude account: ${job.claude_account}`,
      job.docker && "Docker",
    ]
      .filter(Boolean)
      .join(" · ");
  }
  function link(text, url) {
    const element = node("a", text);
    if (typeof url === "string" && /^https?:\/\//.test(url)) {
      element.href = url;
      element.target = "_blank";
      element.rel = "noopener noreferrer";
    }
    return element;
  }
  function jobs() {
    return snapshot?.jobs ?? [];
  }
  function findJob(id) {
    return jobs().find((job) => job.id === id) || null;
  }
  function latest(job) {
    return job.runs?.[0] ?? null;
  }

  async function post(body) {
    const response = await fetch("/api/cron-action", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Babysit-Action": "cron-action" },
      body: JSON.stringify(body),
    });
    const value = await response.json();
    if (!response.ok || value.error) throw new Error(value.error || `HTTP ${response.status}`);
    return value;
  }
  async function act(job, body, after) {
    acting.add(job.id);
    jobErrors.delete(job.id);
    render();
    try {
      const value = await post({ ...body, id: job.id });
      if (after) after(value);
    } catch (error) {
      jobErrors.set(job.id, error.message);
    } finally {
      acting.delete(job.id);
      await refresh(true);
    }
  }

  function card(job) {
    const item = node("li");
    const select = node("button", undefined, "cron-job");
    select.type = "button";
    select.dataset.key = `job:${job.id}`;
    select.setAttribute("aria-pressed", String(job.id === selectedJob));
    if (job.id === selectedJob) select.classList.add("selected");
    select.onclick = () => {
      selectedJob = job.id;
      selectedRun = null;
      render();
      if (matchMedia("(max-width: 900px)").matches)
        byId("detail").scrollIntoView({
          block: "start",
          behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "instant" : "smooth",
        });
    };
    const top = node("div", undefined, "cron-job-top");
    top.append(node("strong", job.name, "cron-job-name"));
    const last = latest(job);
    if (job.running) top.append(badge("running"));
    else if (last) top.append(badge(last.status));
    const bottom = node("div", undefined, "cron-job-bottom");
    bottom.append(node("span", frequency(job.schedule)));
    bottom.append(
      node(
        "span",
        !job.enabled
          ? "Paused"
          : job.upcoming?.length
            ? `Next ${relative(job.upcoming[0])}`
            : "Not scheduled",
      ),
    );
    if (last && !job.running) bottom.append(node("span", `Last ${relative(last.started_at)}`));
    select.append(top, bottom);
    item.append(select);
    return item;
  }

  function runRow(job, run) {
    const row = node("tr");
    if (run.id === selectedRun) row.classList.add("selected");
    const started = node("td");
    const open = button(when(run.started_at), () => {
      selectedRun = run.id;
      render();
    });
    open.className = "cron-run-open";
    open.dataset.key = `run:${run.id}`;
    open.setAttribute("aria-label", `Show output of the run started ${when(run.started_at)}`);
    started.append(open);
    const result = node("td");
    result.append(badge(run.status));
    if (run.message && (run.status !== "running" || run.kind === "agent"))
      result.append(node("small", run.message));
    row.append(
      started,
      node("td", TRIGGERS[run.trigger] || run.trigger),
      node("td", ["skipped", "missed"].includes(run.status) ? "—" : duration(run)),
      result,
    );
    return row;
  }

  // The detail view keeps its sections, and the output itself, across refreshes so
  // a reader's scroll position, selected text and focus survive polling.
  const view = {
    placeholder: node("p", undefined, "pr-sync"),
    head: node("div", undefined, "cron-detail-head"),
    history: node("section", undefined, "cron-history"),
    output: node("section", undefined, "cron-output"),
    outputHead: node("div"),
    log: node("pre", undefined, "cron-log"),
    truncated: node("small", undefined, "cron-truncated"),
  };
  view.log.tabIndex = 0;
  view.log.setAttribute("aria-label", "Command output");
  view.output.append(view.outputHead, view.log, view.truncated);
  byId("detail").replaceChildren(view.placeholder, view.head, view.history, view.output);

  function setLog(text) {
    const pre = view.log;
    if (pre.textContent === text) return;
    const following = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 8;
    pre.textContent = text;
    if (following) pre.scrollTop = pre.scrollHeight;
  }

  function renderOutput(job) {
    const run = job.runs.find((entry) => entry.id === selectedRun);
    view.output.hidden = !run;
    if (!run) return;
    const quiet = ["skipped", "missed"].includes(run.status);
    const agent = run.kind === "agent";
    const head = node("div", undefined, "cron-output-head");
    head.append(node("h4", `Output of the run started ${when(run.started_at)}`), badge(run.status));
    const facts = quiet
      ? [run.message]
      : [
          run.trigger === "schedule" && run.due_at && Math.abs(run.started_at - run.due_at) > 90
            ? `Due ${when(run.due_at)}`
            : null,
          run.exit_code !== null && run.exit_code !== undefined
            ? `Exit status ${run.exit_code}`
            : null,
          ["running", "starting"].includes(run.status)
            ? `Running for ${duration(run)}`
            : `Took ${duration(run)}`,
          run.cwd ? `In ${run.cwd}` : null,
          agent && run.status !== "starting" ? run.message : null,
        ].filter(Boolean);
    const parts = [head, node("p", facts.join(" · "), "pr-sync")];
    if (agent && run.agent_run) {
      // The session lives on in its workspace: open it there, or read it here.
      const open = node("div", undefined, "cron-open");
      open.append(link("Open in Collie", run.agent_run.url));
      if (window.workspaceViewer)
        open.append(
          ...window.workspaceViewer.buttons({
            workspace: run.agent_run.workspace_id,
            name: job.name,
          }),
        );
      parts.push(open);
    }
    const then = agent ? run.prompt : run.command;
    if (!quiet && then !== undefined && then !== (agent ? job.prompt : job.command)) {
      const previous = view.outputHead.querySelector(".cron-command-then");
      const details = node("details", undefined, "cron-command-then");
      details.dataset.run = run.id;
      details.open = Boolean(previous?.open && previous.dataset.run === run.id);
      details.append(
        node("summary", `This run used an earlier ${agent ? "prompt" : "command"}`),
        node("pre", then),
      );
      parts.push(details);
    }
    rebuild(view.outputHead, ...parts);
    view.log.hidden = quiet;
    if (!quiet) setLog(output.run === run.id ? output.text || "(no output)" : "Loading output…");
    view.truncated.hidden = quiet || !run.truncated;
    view.truncated.textContent = run.truncated
      ? `Only the first ${Math.round(snapshot.log_limit / 1024)} KiB of ${Math.round(
          run.output_bytes / 1024,
        )} KiB were kept.`
      : "";
  }

  function detail(job) {
    view.placeholder.hidden = Boolean(job);
    view.head.hidden = view.history.hidden = !job;
    if (!job) {
      view.output.hidden = true;
      view.placeholder.textContent = jobs().length
        ? "Select a job to see its runs."
        : "Jobs and their runs appear here.";
      return;
    }
    const title = node("div", undefined, "cron-title");
    title.append(node("h3", job.name));
    if (!job.enabled) title.append(node("span", "Paused", "badge amber"));
    const agent = job.kind === "agent";
    const meta = [
      frequency(job.schedule),
      agent
        ? `${job.repo} · branch ${job.branch}`
        : job.cwd
          ? `In ${job.cwd}`
          : "In your home directory",
      agent ? agentSummary(job) : null,
      `Time limit ${Math.round(job.timeout / 60)} min`,
    ].filter(Boolean);
    const actions = node("div", undefined, "cron-actions");
    const pending = acting.has(job.id);
    actions.append(
      job.running && !agent
        ? button("Stop", () => act(job, { action: "stop" }), { pending, key: "primary" })
        : button(
            "Run now",
            () =>
              act(job, { action: "run" }, (value) => {
                selectedRun = value.run.id;
              }),
            // An agent run finishes in Collie; the job is free again once it does.
            { pending, key: "primary", disabled: job.running },
          ),
      button(
        job.enabled ? "Pause" : "Resume",
        () => act(job, { action: "enable", enabled: !job.enabled }),
        { pending, key: "enable" },
      ),
      button("Edit", () => openEditor(job), { pending }),
      button(
        "Delete",
        () => {
          if (confirm(`Delete “${job.name}” and its run history?`))
            void act(job, { action: "delete" }, () => {
              selectedJob = null;
              selectedRun = null;
            });
        },
        { pending, disabled: job.running },
      ),
    );
    const head = [title, node("p", meta.join(" · "), "pr-sync")];
    head.push(node("pre", agent ? job.prompt : job.command, "cron-command"), actions);
    if (jobErrors.has(job.id)) head.push(node("p", jobErrors.get(job.id), "cron-job-error"));
    if (job.enabled && job.upcoming?.length) {
      head.push(
        node(
          "p",
          `Next: ${job.upcoming.map((time) => when(time)).join(", ")}${
            snapshot.timezone ? ` (${snapshot.timezone})` : ""
          }`,
          "cron-upcoming",
        ),
      );
    }
    rebuild(view.head, ...head);

    const history = [node("h4", "Runs")];
    if (!job.runs.length) {
      history.push(node("p", "This job has not run yet.", "pr-sync"));
    } else {
      const wrap = node("div", undefined, "cron-table-wrap");
      const table = node("table", undefined, "cron-runs");
      const header = node("tr");
      for (const label of ["Started", "Trigger", "Duration", "Result"])
        header.append(node("th", label));
      const thead = node("thead");
      thead.append(header);
      const tbody = node("tbody");
      tbody.append(...job.runs.map((run) => runRow(job, run)));
      table.append(thead, tbody);
      wrap.append(table);
      history.push(
        wrap,
        node("small", `The latest ${job.runs.length} runs are listed; up to 50 are kept.`),
      );
    }
    rebuild(view.history, ...history);
    renderOutput(job);
  }

  function render() {
    byId("tab").hidden = !snapshot?.enabled;
    if (!snapshot) return;
    if (!snapshot.enabled) {
      byId("status").textContent = "Cron jobs are not enabled on this dashboard.";
      return;
    }
    const all = jobs();
    if (selectedJob && !findJob(selectedJob)) selectedJob = null;
    if (!selectedJob && all.length && !matchMedia("(max-width: 900px)").matches)
      selectedJob = all[0].id;
    const job = findJob(selectedJob);
    if (job && !job.runs.some((run) => run.id === selectedRun))
      selectedRun = job.runs[0]?.id ?? null;
    const failing = all.filter((entry) => FAILING.has(latest(entry)?.status)).length;
    byId("tab-count").hidden = !failing;
    byId("tab-count").textContent = failing;
    byId("tab-count").title = `${failing} job${failing === 1 ? "" : "s"} failed on the last run`;
    byId("count").textContent = all.length;
    byId("status").textContent = snapshot.active
      ? `Jobs run on the dashboard host while the dashboard is running: ${
          snapshot.agents ? "agent tasks in their own workspace in Collie, " : ""
        }commands with ${snapshot.shell}. ` +
        "A run due while it was stopped starts once when it returns, up to a day late."
      : "Another dashboard process over this state directory runs these jobs; this one only shows them.";
    rebuild(byId("list"), ...all.map(card));
    byId("list").hidden = !all.length;
    byId("empty").hidden = all.length > 0;
    detail(job);
    void loadOutput(job);
  }

  async function loadOutput(job) {
    const run = job?.runs.find((entry) => entry.id === selectedRun);
    if (!run || ["skipped", "missed"].includes(run.status) || output.fetching) return;
    // Finished output never changes; a running job's output is fetched on each refresh.
    if (output.run === run.id && output.status === run.status && run.status !== "running") return;
    output.fetching = true;
    try {
      const response = await fetch(`/api/cron-log?run=${encodeURIComponent(run.id)}`);
      const value = await response.json();
      if (!response.ok || value.error) throw new Error(value.error || `HTTP ${response.status}`);
      output = { run: run.id, status: value.run.status, text: value.text, fetching: false };
    } catch (error) {
      output = {
        run: run.id,
        status: null,
        text: `Cannot load the output: ${error.message}`,
        fetching: false,
      };
    }
    // Another run may have been selected while this one loaded.
    if (selectedRun !== run.id) void loadOutput(findJob(selectedJob));
    else if (!view.log.hidden) setLog(output.text || "(no output)");
  }

  let requested = 0,
    applied = 0;
  async function refresh(force) {
    if ((busy && !force) || document.hidden) return;
    busy = true;
    const sequence = ++requested;
    try {
      const response = await fetch("/api/cron");
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const value = await response.json();
      // A forced refresh can overtake a polling one; never go back to an older snapshot.
      if (sequence < applied) return;
      applied = sequence;
      snapshot = value;
      byId("error").textContent = "";
      if (!snapshot.enabled && !byId("panel").hidden) location.hash = "watcher";
      render();
    } catch (error) {
      byId("error").textContent =
        `Cannot load cron jobs: ${error.message}. Shown results may be stale.`;
    } finally {
      busy = false;
    }
  }

  function syncKind() {
    const cron = byId("kind").value === "cron";
    byId("every-fields").hidden = cron;
    byId("expression-fields").hidden = !cron;
  }
  // The agent settings of the open editor, rebuilt each time it opens.
  let agentForm = null;
  let editorSequence = 0;
  function jobKind() {
    return byId("kinds").querySelector("input:checked")?.value ?? "shell";
  }
  function syncJobKind() {
    const agent = jobKind() === "agent";
    byId("agent-fields").hidden = !agent;
    byId("shell-fields").hidden = agent;
  }
  async function buildAgentFields(job, sequence) {
    const tools = window.dashboardAgents;
    const container = byId("agent-fields");
    agentForm = null;
    if (!snapshot?.agents || !tools) {
      container.replaceChildren(
        node("p", "Agent tasks need the dashboard’s workspace actions.", "pr-sync"),
      );
      return;
    }
    container.replaceChildren(node("p", "Loading agents and local clones…", "pr-sync"));
    let fields, listed;
    try {
      [fields, listed] = await Promise.all([tools.fields(), tools.repositories()]);
    } catch (error) {
      if (sequence === editorSequence)
        container.replaceChildren(
          node("p", `Cannot load agents: ${error.message}`, "cron-job-error"),
        );
      return;
    }
    if (sequence !== editorSequence) return;
    const repos = [...listed.repos];
    const repo = node("select");
    repo.id = "cron-repo";
    if (job?.repo && !repos.some((r) => r.repo === job.repo && r.clone === job.clone))
      repos.push({ repo: job.repo, clone: job.clone, missing: true });
    repos.forEach((choice, index) => {
      const option = node(
        "option",
        `${choice.repo} · ${choice.clone}${choice.missing ? " (not found)" : ""}`,
      );
      option.value = String(index);
      repo.append(option);
    });
    if (job?.repo)
      repo.value = String(repos.findIndex((r) => r.repo === job.repo && r.clone === job.clone));
    const text = (id, value, placeholder) => {
      const input = node("input");
      input.id = id;
      input.type = "text";
      input.value = value ?? "";
      input.placeholder = placeholder;
      input.autocapitalize = "off";
      input.autocomplete = "off";
      input.spellcheck = false;
      return input;
    };
    const branch = text("cron-branch", job?.branch, "nightly-triage");
    branch.maxLength = 100;
    const base = text("cron-base", job?.base, "The clone’s current commit");
    const { agent, model, effort, account, settingsNote, docker, dockerLabel } = fields;
    // agentFields names its controls for the New task dialog; this one has its own.
    agent.id = "cron-agent";
    model.id = "cron-model";
    effort.id = "cron-effort";
    account.id = "cron-claude-account";
    docker.id = "cron-docker";
    // A saved setting the current choices lack (an uncached model, a removed account)
    // stays selected, so editing something else does not quietly drop it.
    const keep = (select, value) => {
      if (value && ![...select.options].some((option) => option.value === value)) {
        const option = node("option", `${value} (not listed)`);
        option.value = value;
        select.append(option);
      }
      select.value = value || "";
    };
    if (job?.agent) {
      agent.value = job.agent;
      agent.onchange();
      keep(model, job.model);
      model.onchange();
      keep(effort, job.effort);
      keep(account, job.claude_account);
      docker.checked = Boolean(job.docker);
    }
    const prompt = node("textarea");
    prompt.id = "cron-prompt";
    prompt.rows = 6;
    prompt.maxLength = 32000;
    prompt.value = job?.prompt ?? "";
    const parts = [];
    const field = (label, input) => {
      const element = node("label", label);
      element.htmlFor = input.id;
      parts.push(element, input);
    };
    field("Repository and local clone", repo);
    field("Branch", branch);
    field("Base branch (optional)", base);
    parts.push(
      node(
        "small",
        "Every run works in this branch’s worktree, made from the base on the first run. Changes carry over between runs.",
      ),
    );
    field("Agent", agent);
    field("Model (optional)", model);
    field("Reasoning effort (optional)", effort);
    field("Claude account", account);
    parts.push(settingsNote, dockerLabel);
    field("Prompt", prompt);
    parts.push(
      node(
        "small",
        "Each run starts a fresh session in its own pane. The agent is exited once it confirms the task is done; one that asks a question stays open in Collie.",
      ),
    );
    container.replaceChildren(...parts);
    for (const select of [repo, model, effort]) tools.searchable(select);
    agentForm = { repos, repo, branch, base, agent, model, effort, account, docker, prompt };
  }
  function openEditor(job) {
    const sequence = ++editorSequence;
    editing = job ? job.id : null;
    byId("dialog-title").textContent = job ? `Edit ${job.name}` : "New job";
    byId("name").value = job?.name ?? "";
    const kind = job ? job.kind || "shell" : snapshot?.agents ? "agent" : "shell";
    for (const input of byId("kinds").querySelectorAll("input")) {
      input.checked = input.value === kind;
      input.disabled = input.value === "agent" && !snapshot?.agents && kind !== "agent";
    }
    syncJobKind();
    byId("command").value = job?.command ?? "";
    byId("cwd").value = job?.cwd ?? "";
    byId("shell").textContent = snapshot
      ? `Runs with ${snapshot.shell}. Output and exit status are kept for each run.`
      : "";
    const schedule = job?.schedule ?? { every: 3600 };
    byId("kind").value = schedule.cron ? "cron" : "every";
    byId("expression").value = schedule.cron ?? "";
    const [count, unit] = unitOf(schedule.every ?? 3600);
    byId("every").value = count;
    byId("unit").value = String(unit);
    byId("timeout").value = Math.round((job?.timeout ?? 3600) / 60);
    // Enhanced pickers redraw on change, which also shows the matching fields.
    for (const select of [byId("kind"), byId("unit")])
      select.dispatchEvent(new Event("change", { bubbles: true }));
    byId("enabled-input").checked = job?.enabled ?? true;
    byId("form-error").textContent = "";
    byId("save").disabled = false;
    byId("dialog").showModal();
    byId("name").focus();
    void buildAgentFields(job?.kind === "agent" ? job : null, sequence);
  }
  function task() {
    if (jobKind() === "shell")
      return { kind: "shell", command: byId("command").value, cwd: byId("cwd").value };
    if (!agentForm) throw new Error("The agent settings are still loading");
    const form = agentForm;
    const choice = form.repos[Number(form.repo.value)] ?? {};
    return {
      kind: "agent",
      repo: choice.repo,
      clone: choice.clone,
      branch: form.branch.value.trim(),
      base: form.base.value.trim(),
      agent: form.agent.value,
      model: form.model.value,
      effort: form.effort.value,
      claude_account: form.agent.value === "claude" ? form.account.value : "",
      docker: form.docker.checked,
      prompt: form.prompt.value,
    };
  }
  async function save(event) {
    event.preventDefault();
    const schedule =
      byId("kind").value === "cron"
        ? { cron: byId("expression").value }
        : { every: Number(byId("every").value) * Number(byId("unit").value) };
    byId("save").disabled = true;
    byId("form-error").textContent = "";
    try {
      const value = await post({
        action: "save",
        id: editing,
        name: byId("name").value,
        ...task(),
        schedule,
        timeout: Number(byId("timeout").value) * 60,
        enabled: byId("enabled-input").checked,
      });
      selectedJob = value.job.id;
      byId("dialog").close();
      await refresh(true);
    } catch (error) {
      byId("form-error").textContent = error.message;
    } finally {
      byId("save").disabled = false;
    }
  }

  byId("new").onclick = () => openEditor(null);
  byId("kind").onchange = syncKind;
  for (const input of byId("kinds").querySelectorAll("input")) input.onchange = syncJobKind;
  byId("form").onsubmit = save;
  byId("dialog-close").onclick = () => byId("dialog").close();
  window.addEventListener("cron-visible", () => refresh());
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) void refresh();
  });
  // Every 3 seconds while a job runs, so its output follows along; otherwise every 15.
  // Other tabs refresh every 30 seconds for the failure count on this tab.
  let ticks = 0;
  setInterval(() => {
    ticks += 1;
    const running = jobs().some((job) => job.running);
    const every = byId("panel").hidden ? 10 : running ? 1 : 5;
    if (ticks % every === 0) void refresh();
  }, 3000);
  void refresh();
})();
