/* Optional experiment: owns its requests, rendering, and errors. */
(() => {
  const byId = (id) => document.getElementById(`upstream-${id}`);
  let snapshot = null;
  let busy = false;
  let limit = 50;
  const classifications = {
    likely_flaky: "Likely flaky",
    likely_broken: "Likely broken",
    mixed: "Mixed results",
    insufficient: "Insufficient history",
  };
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
        `${value.summary || ""}${value.retried ? " after retry" : ""} · ${value.current ? "Latest run" : "Historical observation"} · ${value.artifact || ""} · ${value.report || ""} · Commit ${(value.sha || "").slice(0, 12) || "unavailable"} · Updated ${date(value.updated_at)}`,
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
    byId("history").textContent = "";
    if (!data.enabled) {
      status.textContent =
        data.error ||
        "Upstream test experiment is disabled. Enable it in experiments/upstream-tests/config.json.";
      return;
    }
    status.textContent = `${data.loading ? "Loading / refreshing… " : ""}${data.stale ? "Stale saved results — " : ""}${data.synced_at ? `Observed ${date(data.synced_at * 1000)}. ` : "No observation yet. "}${data.incomplete ? "Incomplete coverage. " : ""}Refresh checks the cache; GitHub collection runs at most every 15 minutes.`;
    if (data.repo) {
      byId("scope").textContent =
        `${data.repo} · Tracking: ${(data.branches || []).join(", ")}. ${data.selection}. Latest run per workflow and branch, with up to ${data.history?.runs_per_workflow || 5} recent runs from the past ${data.window_days} days (push, schedule, manual). Historical flake evidence is labeled separately from current failures.`;
      if (data.policy_url) byId("scope").append(" ", anchor("Support policy", data.policy_url));
    }
    if (data.history) {
      byId("history").textContent =
        `History: ${data.history.sampled_runs} of ${data.history.selected_runs} selected runs have individual test outcomes.`;
      const explanation = node("details");
      explanation.append(
        node("summary", "How findings are classified"),
        node(
          "p",
          "Likely flaky: passed after a retry, or both passed and failed on the same commit. Likely broken: currently failing in at least two sampled runs with no observed pass. Different commits can reflect fixes or regressions. These are clues, not proof; missing tests and green workflows never count as individual passes.",
        ),
      );
      byId("history").append(explanation);
    }
    for (const warning of [data.error, ...(data.warnings || [])].filter(Boolean)) {
      byId("notices").append(node("p", warning, "alert"));
    }
    if (data.history?.gaps?.length) {
      const gaps = node("details");
      gaps.append(
        node("summary", `${data.history.gaps.length} runs with incomplete report coverage`),
      );
      const list = node("ul");
      for (const gap of data.history.gaps) {
        const item = node("li");
        item.append(anchor(`${gap.branch} · ${gap.workflow} · run ${gap.run_id}`, gap.url));
        item.append(node("p", gap.notes.join(" · ")));
        list.append(item);
      }
      gaps.append(list);
      byId("notices").append(gaps);
    }
    const filter = byId("classification").value;
    const groups = (data.groups || []).filter(
      (group) => filter === "all" || group.assessments?.some((a) => a.classification === filter),
    );
    if (byId("group").value === "test") {
      if (!groups.length && data.synced_at) {
        byId("results").append(
          node(
            "p",
            filter !== "all"
              ? "No findings match this filter."
              : data.incomplete
                ? "No failing or likely flaky tests in the available reports. Inspect Branch / workflow for missing reports and infrastructure failures."
                : "No failing or likely flaky tests in the selected runs.",
            "empty",
          ),
        );
      }
      for (const group of groups.slice(0, limit)) {
        const card = node("article", undefined, "upstream-card");
        card.append(node("h3", group.test));
        const badges = node("div", undefined, "pr-badges");
        for (const classification of new Set(
          (group.assessments || []).map((a) => a.classification),
        )) {
          badges.append(
            node(
              "span",
              classifications[classification] || classification,
              `badge ${classification === "likely_broken" ? "red" : "amber"}`,
            ),
          );
        }
        card.append(badges);
        card.append(
          node(
            "p",
            `${new Set(group.occurrences.map((entry) => entry.branch)).size} branches · ${group.occurrences.length} report observations`,
          ),
        );
        for (const assessment of group.assessments || []) {
          card.append(
            node(
              "p",
              `${assessment.branch} · ${assessment.workflow} · ${assessment.artifact} · ${assessment.report}: ${classifications[assessment.classification]} — ${assessment.reason} ${assessment.failures} failed / ${assessment.passes} passed observations (${assessment.retry_passes} passed after retry). ${assessment.currently_failing ? "Failing in latest run." : "Historical signal; no confirmed failure in the latest run."}`,
            ),
          );
        }
        const list = node("ul");
        for (const value of group.occurrences) list.append(occurrence(value));
        const evidence = node("details");
        evidence.append(node("summary", "Pass / fail evidence"), list);
        card.append(evidence);
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
      if (filter !== "all")
        byId("results").append(
          node(
            "p",
            "The finding filter applies to the Test grouping. All latest workflow runs are shown below.",
          ),
        );
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
  byId("classification").onchange = () => {
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
