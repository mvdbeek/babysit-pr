/* A browser-local inbox shared by the watcher, PR, and issue snapshots. */
(() => {
  const byId = (id) => document.getElementById(`notifications-${id}`);
  const dialog = byId("dialog");
  const accounts = new Map();
  let account = null;
  let watcherSnapshot = null;
  let renderedList = null;
  const object = (value) => value !== null && typeof value === "object" && !Array.isArray(value);
  const blank = () => ({ baselines: {}, entries: {}, outcomes: {} });
  const ownActivityFields = {
    IssueComment: ["comments", "updated_at"],
    ContentEdited: ["updated_at"],
    RenamedTitleEvent: ["title", "updated_at"],
    AssignedEvent: ["assignees", "roles", "updated_at"],
    UnassignedEvent: ["assignees", "roles", "updated_at"],
    LabeledEvent: ["labels", "updated_at"],
    UnlabeledEvent: ["labels", "updated_at"],
    ReadyForReviewEvent: ["draft", "updated_at"],
    ConvertToDraftEvent: ["draft", "updated_at"],
    ReviewRequestedEvent: ["roles", "updated_at"],
    ReviewRequestRemovedEvent: ["roles", "updated_at"],
    PullRequestReview: ["review_decision", "updated_at"],
    ReviewDismissedEvent: ["review_decision", "updated_at"],
    HeadRefForcePushedEvent: ["head_sha", "updated_at"],
  };

  function notificationFields(source, sample, old, values) {
    if (source === "watcher" && old) {
      const checks = JSON.parse(old.values.checks || "null");
      if (Array.isArray(checks)) old.values.checks = JSON.stringify(checkResult(checks));
    }
    const fields = new Set(
      // A field added after the baseline was stored reads as null, not as a change.
      Object.keys(values).filter(
        (field) => !old || (old.values[field] ?? "null") !== values[field],
      ),
    );
    if (source === "prs" && !["SUCCESS", "FAILURE", "ERROR"].includes(sample.values.ci))
      fields.delete("ci");
    if (source === "watcher") {
      if (sample.values.status === "running") {
        fields.delete("attempts");
        fields.delete("status");
      }
      if (!["SUCCESS", "FAILURE", "ERROR"].includes(sample.values.checks)) fields.delete("checks");
      fields.delete("summary");
      if (!old) return [...fields];
      for (const field of fields) {
        const actionField =
          { approved: "feedback_approved", outcome: "pr_outcome" }[field] || field;
        let expected = old.values[field];
        for (const action of sample.actions || []) {
          if (
            action.at * 1000 >= old.at &&
            Object.hasOwn(action.before, actionField) &&
            expected === JSON.stringify(action.before[actionField])
          )
            expected = JSON.stringify(action.after[actionField]);
        }
        if (expected === values[field]) fields.delete(field);
      }
      const key = (item) => `${item.kind}:${item.id}`;
      const previous = new Map(
        JSON.parse(old.values.feedback || "[]").map((item) => [key(item), JSON.stringify(item)]),
      );
      const current = new Map(
        (sample.values.feedback || []).map((item) => [key(item), JSON.stringify(item)]),
      );
      const authors = new Map(
        (sample.feedbackAuthors || []).map((item) => [key(item), item.author?.toLowerCase()]),
      );
      const added = [...current.keys()].filter((id) => !previous.has(id));
      if (
        added.length &&
        [...previous].every(([id, body]) => current.get(id) === body) &&
        added.every((id) => authors.get(id) === account.login.toLowerCase())
      )
        fields.delete("feedback");
      return [...fields];
    }
    const history = sample.activity;
    const cursor = old && JSON.parse(old.values.updated_at || "null");
    const updated = sample.values.updated_at;
    if (!old || !history?.since || !cursor || !updated || history.since > cursor)
      return [...fields];
    const events = history.events.filter((event) => event.at > cursor);
    if (
      !events.length ||
      !events.some((event) => event.at >= updated) ||
      events.some(
        (event) =>
          event.actor?.toLowerCase() !== account.login.toLowerCase() ||
          !ownActivityFields[event.type],
      )
    )
      return [...fields];
    for (const event of events)
      for (const field of ownActivityFields[event.type]) fields.delete(field);
    return [...fields];
  }

  function subject(raw) {
    try {
      const url = new URL(raw);
      if (url.protocol !== "https:" || url.hostname !== "github.com") return null;
      const match = url.pathname.match(/^\/([^/]+)\/([^/]+)\/(pull|issues|tree)\/(.+?)\/?$/);
      if (!match || (match[3] !== "tree" && !/^\d+$/.test(match[4]))) return null;
      const repo = `${match[1]}/${match[2]}`;
      const kind = match[3] === "pull" ? "PR" : match[3] === "issues" ? "Issue" : "Branch";
      return {
        key: `https://github.com/${repo.toLowerCase()}/${match[3]}/${match[4]}`,
        label: `${repo} ${kind === "Branch" ? match[4] : `#${match[4]}`}`,
        kind,
      };
    } catch {
      return null;
    }
  }

  function read(key) {
    const value = JSON.parse(localStorage.getItem(key));
    if (!object(value) || !object(value.baselines) || !object(value.entries)) return blank();
    const result = blank();
    if (object(value.outcomes)) result.outcomes = value.outcomes;
    for (const source of ["prs", "issues", "watcher"]) {
      if (!object(value.baselines[source])) continue;
      result.baselines[source] = {};
      for (const [url, sample] of Object.entries(value.baselines[source])) {
        if (
          subject(url) &&
          object(sample) &&
          Number.isFinite(sample.at) &&
          object(sample.values) &&
          Object.values(sample.values).every((field) => typeof field === "string")
        )
          result.baselines[source][url] = sample;
      }
    }
    for (const [url, entry] of Object.entries(value.entries)) {
      if (
        !subject(url) ||
        !object(entry) ||
        typeof entry.title !== "string" ||
        !Number.isFinite(entry.at) ||
        !Number.isFinite(entry.seenAt) ||
        !object(entry.notes)
      )
        continue;
      entry.notes = Object.fromEntries(
        Object.entries(entry.notes).filter(
          ([, note]) => object(note) && Number.isFinite(note.at) && typeof note.text === "string",
        ),
      );
      result.entries[url] = entry;
    }
    return result;
  }

  function sync() {
    if (!account?.storage) return;
    try {
      account.state = read(account.key);
      account.serialized = JSON.stringify(account.state);
    } catch (error) {
      if (error instanceof SyntaxError) account.state = blank();
      else account.storage = false;
    }
  }

  function activate(login) {
    if (account?.login === login) return;
    if (!accounts.has(login))
      accounts.set(login, {
        login,
        key: `babysit-pr:notifications:v1:${login}`,
        state: blank(),
        storage: true,
      });
    account = accounts.get(login);
    window.dashboardPush?.account(login);
    sync();
    try {
      account.silenced = JSON.parse(localStorage.getItem(`${account.key}:silenced`)) || [];
    } catch {
      account.silenced = [];
    }
    refreshPreferences();
    render();
  }

  function isSilenced(url) {
    return account?.silenced?.includes(subject(url)?.key) || false;
  }

  function applyPreferences(target, value) {
    if (value.login !== target.login || !Array.isArray(value.silenced)) return;
    const changed = JSON.stringify(target.silenced) !== JSON.stringify(value.silenced);
    target.silenced = value.silenced;
    try {
      localStorage.setItem(`${target.key}:silenced`, JSON.stringify(value.silenced));
    } catch {
      /* Server preferences still persist. */
    }
    if (account !== target) return;
    sync();
    for (const url of target.silenced) {
      const entry = target.state.entries[url];
      if (entry) entry.seenAt = entry.at;
    }
    save();
    render();
    if (changed) window.dispatchEvent(new Event("notification-preferences"));
  }

  async function refreshPreferences(target = account) {
    if (!target || target.loadingPreferences) return;
    target.loadingPreferences = true;
    try {
      const response = await fetch("/api/notification-preferences");
      if (response.ok) applyPreferences(target, await response.json());
    } catch {
      /* Keep the last saved preferences while offline. */
    } finally {
      target.loadingPreferences = false;
    }
  }

  function silenceButton(url) {
    const button = node("button", undefined, "notification-silence");
    button.type = "button";
    button.dataset.url = url;
    const muted = isSilenced(url);
    const label = `${muted ? "Unsilence" : "Silence"} notifications for ${subject(url)?.label || "PR"}`;
    button.title = label;
    button.setAttribute("aria-label", label);
    button.setAttribute("aria-pressed", String(muted));
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("aria-hidden", "true");
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute(
      "d",
      "M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9M10 21h4" + (muted ? "M2 2l20 20" : ""),
    );
    svg.append(path);
    button.append(svg);
    button.onclick = async () => {
      const target = account;
      button.disabled = true;
      try {
        const response = await fetch("/api/notification-silence", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-Babysit-Action": "notification-silence",
          },
          body: JSON.stringify({ login: target.login, url, silenced: !muted }),
        });
        const value = await response.json();
        if (!response.ok)
          throw new Error(value.error || "Could not change notification preferences");
        applyPreferences(target, value);
        if (account === target) {
          seen([url]);
          [...document.querySelectorAll(".notification-silence")]
            .find((item) => item.dataset.url === url)
            ?.focus({ preventScroll: true });
        }
      } catch (error) {
        window.alert(error.message);
      } finally {
        button.disabled = false;
      }
    };
    return button;
  }

  function recent() {
    return Object.entries(
      window.dashboardPush?.entries(account?.login) || account?.state.entries || {},
    )
      .map(([url, entry]) => [url, isSilenced(url) ? { ...entry, seenAt: entry.at } : entry])
      .sort((a, b) => b[1].at - a[1].at || a[0].localeCompare(b[0]));
  }

  function save() {
    const ordered = Object.entries(account.state.entries).sort((a, b) => b[1].at - a[1].at);
    // Keep every unseen item for the count, plus the ten most recent seen items.
    for (const [key, entry] of ordered.slice(10)) {
      if (entry.seenAt >= entry.at) delete account.state.entries[key];
    }
    if (account.storage) {
      try {
        const serialized = JSON.stringify(account.state);
        if (serialized !== account.serialized) localStorage.setItem(account.key, serialized);
        account.serialized = serialized;
      } catch {
        account.storage = false;
      }
    }
  }

  function markAllSeen() {
    if (!account) return;
    window.dashboardPush?.seen(recent().map(([url]) => url));
    sync();
    for (const entry of Object.values(account.state.entries)) entry.seenAt = entry.at;
    save();
    render();
  }

  function seen(urls, login = account?.login, source = null, at = Infinity) {
    if (!account || login !== account.login) return;
    sync();
    window.dashboardPush?.seen(urls.map((url) => subject(url)?.key).filter(Boolean), source, at);
    for (const url of urls) {
      const key = subject(url)?.key;
      if (source && account.state.baselines[source]?.[key]?.at > at) continue;
      const entry = account.state.entries[key];
      if (entry) entry.seenAt = entry.at;
    }
    save();
    render();
  }

  function node(tag, text, className) {
    const result = document.createElement(tag);
    if (text !== undefined) result.textContent = text;
    if (className) result.className = className;
    return result;
  }

  function destination(key, entry) {
    const source = Object.entries(entry.notes).sort(
      (a, b) => b[1].at - a[1].at || Number(b[0] === "watcher") - Number(a[0] === "watcher"),
    )[0]?.[0];
    const page = ["prs", "issues", "watcher"].includes(source)
      ? source
      : subject(key)?.kind === "Issue"
        ? "issues"
        : subject(key)?.kind === "Branch"
          ? "watcher"
          : "prs";
    return `/?item=${encodeURIComponent(key)}#${page}`;
  }

  function render() {
    const ordered = recent();
    const unseen = ordered.filter(([, entry]) => entry.seenAt < entry.at).length;
    byId("count").textContent = String(unseen);
    byId("count").hidden = unseen === 0;
    byId("toggle").setAttribute(
      "aria-label",
      unseen
        ? `Notifications, ${unseen} unseen ${unseen === 1 ? "item" : "items"}`
        : "Notifications, no unseen updates",
    );
    byId("seen").disabled = unseen === 0;
    byId("empty").hidden = ordered.length > 0;
    byId("status").textContent =
      account && !account.storage
        ? "Browser storage is unavailable; updates are kept for this tab only."
        : "";
    const signature = JSON.stringify([account?.login, ordered.slice(0, 10)]);
    if (signature === renderedList) return;
    renderedList = signature;
    const rows = ordered.slice(0, 10).map(([key, entry]) => {
      const item = subject(key);
      const row = node("li", undefined, entry.seenAt < entry.at ? "notification-unseen" : "");
      const anchor = node("a", undefined, "notification-link");
      anchor.href = destination(key, entry);
      anchor.append(
        node("small", `${anchor.hash === "#watcher" ? "Watcher" : item.kind} · ${item.label}`),
        node("strong", entry.title),
      );
      const notes = Object.values(entry.notes).sort((a, b) => b.at - a.at);
      anchor.append(node("span", [...new Set(notes.map((note) => note.text))].join(" · ")));
      const date = new Date(entry.at);
      const time = node(
        "time",
        date.toLocaleString([], { dateStyle: "short", timeStyle: "short" }),
      );
      time.dateTime = date.toISOString();
      anchor.append(time);
      anchor.onclick = (event) => {
        if (event.button || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey)
          return;
        event.preventDefault();
        const href = anchor.href;
        window.dashboardPush?.seen([key]);
        sync();
        if (account.state.entries[key])
          account.state.entries[key].seenAt = account.state.entries[key].at;
        save();
        render();
        dialog.close();
        window.dashboardNavigation.open(href);
      };
      row.append(anchor);
      return row;
    });
    byId("list").replaceChildren(...rows);
  }

  function observe(source, samples, labels) {
    if (!account) return;
    sync();
    const initialized = Object.hasOwn(account.state.baselines, source);
    const baseline = (account.state.baselines[source] ||= {});
    for (const sample of samples) {
      const item = subject(sample.url);
      if (!item || !Number.isFinite(sample.at)) continue;
      const old = baseline[item.key];
      if (old && sample.at < old.at) continue;
      const values = Object.fromEntries(
        Object.entries(sample.values).map(([field, value]) => [
          field,
          JSON.stringify(value ?? null),
        ]),
      );
      const fields = notificationFields(source, sample, old, values);
      baseline[item.key] = { at: sample.at, values };
      const ciField = source === "prs" ? "ci" : source === "watcher" ? "checks" : null;
      const result = sample.values[ciField];
      if (["SUCCESS", "FAILURE", "ERROR"].includes(result) && (!old || fields.includes(ciField))) {
        const outcome = JSON.stringify({
          sha: sample.values[source === "prs" ? "head_sha" : "sha"] ?? null,
          result,
        });
        if (account.state.outcomes[item.key] === outcome) {
          const index = fields.indexOf(ciField);
          if (index !== -1) fields.splice(index, 1);
        }
        account.state.outcomes[item.key] = outcome;
      }
      if (isSilenced(item.key)) continue;
      const changed = initialized && fields.length > 0;
      let entry = account.state.entries[item.key];
      if (!entry && !changed && initialized) continue;
      if (!entry)
        entry = account.state.entries[item.key] = {
          title: sample.title || item.label,
          at: sample.seedAt || sample.at,
          seenAt: sample.seedAt || sample.at,
          notes: {},
        };
      // A watcher uses the branch as its title; prefer the PR/issue title when known.
      if (source !== "watcher" || !entry.notes.prs) entry.title = sample.title || item.label;
      if (changed) {
        entry.at = Math.max(Date.now(), entry.at + 1, entry.seenAt + 1);
        const names = fields.filter((field) => labels[field]).map((field) => labels[field]);
        entry.notes[source] = {
          at: entry.at,
          text: !old
            ? `New ${source === "watcher" ? "watch" : item.kind}`
            : names.length
              ? names.join(" · ")
              : sample.summary,
        };
      } else if (!entry.notes[source] && !old) {
        entry.notes[source] = { at: sample.seedAt || sample.at, text: sample.summary };
      }
    }
    save();
    render();
  }

  function checkResult(checks) {
    if (!checks.length) return "NONE";
    if (
      checks.some(
        (check) =>
          check.bucket === "pending" ||
          [
            "PENDING",
            "QUEUED",
            "IN_PROGRESS",
            "EXPECTED",
            "WAITING",
            "REQUESTED",
            "PENDING_APPROVAL",
          ].includes(check.state),
      )
    )
      return "PENDING";
    return checks.some(
      (check) =>
        ["fail", "cancel"].includes(check.bucket) ||
        ["FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED"].includes(check.state),
    )
      ? "FAILURE"
      : "SUCCESS";
  }

  function watcher(payload) {
    if (payload.error) return;
    watcherSnapshot = payload;
    if (!account) return;
    observe(
      "watcher",
      (payload.jobs || []).map((job) => ({
        url: job.url,
        title: job.branch,
        at: (job.updated_at || 0) * 1000,
        seedAt: (job.updated_at || 0) * 1000,
        actions: job.notification_actions,
        feedbackAuthors: job.feedback,
        values: {
          status: job.status,
          summary: job.summary,
          sha: job.sha,
          outcome: job.pr_outcome,
          attempts: job.attempts,
          approved: job.feedback_approved,
          feedback: (job.feedback || []).map(({ kind, id, body }) => ({ kind, id, body })),
          checks: checkResult(job.check_details || []),
        },
        summary: job.summary || `Watcher: ${job.status}`,
      })),
      {
        status: "Watch status changed",
        sha: "New commits",
        outcome: "PR closed or merged",
        attempts: "Repair activity",
        approved: "Feedback approval changed",
        feedback: "Feedback updated",
        checks: "CI updated",
      },
    );
  }

  function overview(kind, payload, labels, value) {
    if (!payload.login || !payload.synced_at || payload.error || payload.refreshing) return;
    if (kind === "issues" && account && account.login !== payload.login) return;
    activate(payload.login);
    sync();
    if (!Object.hasOwn(account.state.baselines, kind)) {
      try {
        const visit = JSON.parse(
          localStorage.getItem(`babysit-pr:seen-${kind}:v1:${payload.login}`),
        );
        if (object(visit) && Number.isFinite(visit.synced_at) && object(visit[kind])) {
          const baseline = (account.state.baselines[kind] = {});
          for (const item of payload[kind] || []) {
            const old = visit[kind][item.id];
            const key = subject(item.url)?.key;
            if (key && object(old))
              baseline[key] = {
                at: visit.synced_at * 1000,
                values: Object.fromEntries(
                  [...Object.keys(labels), "updated_at"].map((field) => [
                    field,
                    JSON.stringify(old[field] ?? null),
                  ]),
                ),
              };
          }
          save();
        }
      } catch {
        /* Missing or unavailable visit history starts with a quiet baseline. */
      }
    }
    observe(
      kind,
      (payload[kind] || []).map((item) => ({
        url: item.url,
        title: item.title,
        activity: item.notification_activity,
        at: payload.synced_at * 1000,
        seedAt: Date.parse(item.updated_at) || payload.synced_at * 1000,
        values: Object.fromEntries(
          [...Object.keys(labels), "updated_at"].map((field) => [field, value(item, field)]),
        ),
        summary: item.latest_activity
          ? `${item.latest_activity.actor || "Someone"} ${item.latest_activity.action}`
          : `${kind === "prs" ? "PR" : "Issue"} activity updated`,
      })),
      Object.fromEntries(
        Object.entries(labels).map(([field, label]) => [
          field,
          field === "head_sha" ? label : `${label} changed`,
        ]),
      ),
    );
    if (watcherSnapshot) watcher(watcherSnapshot);
  }

  byId("toggle").onclick = () => {
    sync();
    render();
    dialog.showModal();
  };
  byId("close").onclick = () => dialog.close();
  byId("seen").onclick = markAllSeen;
  dialog.addEventListener("click", (event) => {
    if (event.target !== dialog) return;
    const rect = dialog.getBoundingClientRect();
    if (
      event.clientX < rect.left ||
      event.clientX > rect.right ||
      event.clientY < rect.top ||
      event.clientY > rect.bottom
    )
      dialog.close();
  });
  window.addEventListener("storage", (event) => {
    if (account && (event.key === account.key || event.key === null)) {
      sync();
      render();
    }
  });
  window.dashboardNotifications = {
    overview,
    watcher,
    silenceButton,
    seen,
    render,
    unseen: () =>
      recent()
        .filter(([, entry]) => entry.seenAt < entry.at)
        .map(([url]) => url),
    history: () =>
      Object.fromEntries(
        recent().map(([url, entry]) => [
          url,
          {
            title: entry.title,
            at: entry.at,
            seenAt: entry.seenAt,
          },
        ]),
      ),
  };
  setInterval(() => {
    if (!document.hidden) refreshPreferences();
  }, 15000);
  if (new URLSearchParams(location.search).get("updates") === "1") dialog.showModal();
})();
