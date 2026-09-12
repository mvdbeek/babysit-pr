/* Optional experiment: owns its requests, rendering, and errors. */
(() => {
  const byId = (id) => document.getElementById(`upstream-${id}`);
  let snapshot = null;
  let busy = false;
  let limit = 50;
  function node(tag, text, className) {
    const element = document.createElement(tag);
    if (text !== undefined) element.textContent = text;
    if (className) element.className = className;
    return element;
  }
  function anchor(text, url) {
    const element = node("a", text);
    if (typeof url === "string" && url.startsWith("https://github.com/")) {
      element.href = url;
      element.target = "_blank";
      element.rel = "noopener noreferrer";
    }
    return element;
  }
  function date(value) {
    return value ? new Date(value).toLocaleString() : "unavailable";
  }
  function occurrence(value) {
    const item = node("li");
    item.append(
      anchor(
        `${value.branch} · ${value.workflow} · run ${value.run_id} / attempt ${value.attempt}`,
        value.url,
      ),
    );
    item.append(
      node(
        "p",
        `${value.summary || ""} · ${value.artifact || ""} · Updated ${date(value.updated_at)}`,
      ),
    );
    const jobs = node("details");
    jobs.append(node("summary", "Run jobs (artifact-to-job mapping unavailable)"));
    const list = node("ul");
    for (const job of value.jobs || []) {
      const entry = node("li");
      entry.append(anchor(`${job.name}: ${job.state}`, job.url));
      list.append(entry);
    }
    jobs.append(list);
    item.append(jobs);
    return item;
  }
  function render() {
    const data = snapshot;
    if (!data) return;
    byId("tab").hidden = !data.enabled;
    const status = byId("status");
    byId("results").replaceChildren();
    byId("notices").replaceChildren();
    byId("scope").textContent = "";
    if (!data.enabled) {
      status.textContent =
        data.error ||
        "Upstream test experiment is disabled. Enable it in experiments/upstream-tests/config.json.";
      return;
    }
    status.textContent = `${data.loading ? "Loading / refreshing… " : ""}${data.stale ? "Stale saved results — " : ""}${data.synced_at ? `Observed ${date(data.synced_at * 1000)}. ` : "No observation yet. "}${data.incomplete ? "Incomplete coverage. " : ""}Refresh checks the cache; GitHub collection runs at most every 15 minutes.`;
    if (data.repo) {
      byId("scope").textContent =
        `${data.repo} · Tracking: ${(data.branches || []).join(", ")}. ${data.selection}. Newest run per workflow and branch from the past ${data.window_days} days (push, schedule, manual); pending and passing replacements clear older failures. This is an observation, not branch-wide test coverage.`;
      if (data.policy_url) byId("scope").append(" ", anchor("Support policy", data.policy_url));
    }
    for (const warning of [data.error, ...(data.warnings || [])].filter(Boolean)) {
      byId("notices").append(node("p", warning, "alert"));
    }
    const groups = data.groups || [];
    if (byId("group").value === "test") {
      if (!groups.length && data.synced_at) {
        byId("results").append(
          node(
            "p",
            data.incomplete
              ? "No confirmed failing tests in the available reports. Inspect Branch / workflow for missing reports and infrastructure failures."
              : "No confirmed failing tests in the selected runs.",
            "empty",
          ),
        );
      }
      for (const group of groups.slice(0, limit)) {
        const card = node("article", undefined, "upstream-card");
        card.append(node("h3", group.test));
        card.append(
          node(
            "p",
            `${new Set(group.occurrences.map((entry) => entry.branch)).size} branches · ${group.occurrences.length} report occurrences`,
          ),
        );
        const list = node("ul");
        for (const value of group.occurrences) list.append(occurrence(value));
        card.append(list);
        byId("results").append(card);
      }
      if (groups.length > limit) {
        const more = node("button", `Show more (${groups.length - limit} remaining)`);
        more.onclick = () => {
          limit += 50;
          render();
        };
        byId("results").append(more);
      }
    } else {
      for (const branch of data.branches || []) {
        byId("results").append(node("h3", branch));
        for (const run of (data.runs || []).filter((entry) => entry.branch === branch)) {
          const card = node("article", undefined, "upstream-card");
          card.append(
            anchor(
              `${run.workflow} · ${run.state} · run ${run.run_id} / attempt ${run.attempt}`,
              run.url,
            ),
          );
          card.append(
            node(
              "p",
              `Updated ${date(run.updated_at)} · ${run.failures.length} confirmed test failures · ${(run.sha || "").slice(0, 12)}`,
            ),
          );
          for (const note of run.notes) card.append(node("p", note, "upstream-note"));
          const details = node("details");
          details.append(node("summary", "Jobs and test reports"));
          const list = node("ul");
          for (const job of run.jobs) {
            const item = node("li");
            item.append(anchor(`${job.name}: ${job.state}`, job.url));
            list.append(item);
          }
          for (const failure of run.failures)
            list.append(node("li", `${failure.test} · ${failure.summary} · ${failure.artifact}`));
          details.append(list);
          card.append(details);
          byId("results").append(card);
        }
      }
    }
  }
  async function refresh() {
    if (busy) return;
    busy = true;
    try {
      const response = await fetch("/api/upstream-tests");
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      snapshot = await response.json();
      render();
    } catch (error) {
      byId("status").textContent =
        `Upstream test API unavailable: ${error.message}. Displayed results may be stale.`;
    } finally {
      busy = false;
    }
  }
  byId("group").onchange = () => {
    limit = 50;
    render();
  };
  byId("refresh").onclick = refresh;
  window.addEventListener("upstream-visible", refresh);
  setInterval(() => {
    if (!document.hidden && !byId("panel").hidden) void refresh();
  }, 5000);
  void refresh();
})();
