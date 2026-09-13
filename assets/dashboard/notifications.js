/* A browser-local inbox shared by the watcher, PR, and issue snapshots. */
(() => {
  const byId = (id) => document.getElementById(`notifications-${id}`);
  const dialog = byId("dialog");
  const accounts = new Map();
  let account = null;
  let watcherSnapshot = null;
  let renderedList = null;
  const object = (value) => value !== null && typeof value === "object" && !Array.isArray(value);
  const blank = () => ({ baselines: {}, entries: {} });

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
    render();
  }

  function recent() {
    return Object.entries(
      window.dashboardPush?.entries(account?.login) || account?.state.entries || {},
    ).sort((a, b) => b[1].at - a[1].at || a[0].localeCompare(b[0]));
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
      anchor.href = key;
      anchor.target = "_blank";
      anchor.rel = "noopener noreferrer";
      anchor.append(node("small", `${item.kind} · ${item.label}`), node("strong", entry.title));
      const notes = Object.values(entry.notes).sort((a, b) => b.at - a.at);
      anchor.append(node("span", [...new Set(notes.map((note) => note.text))].join(" · ")));
      const date = new Date(entry.at);
      const time = node(
        "time",
        date.toLocaleString([], { dateStyle: "short", timeStyle: "short" }),
      );
      time.dateTime = date.toISOString();
      anchor.append(time);
      anchor.onclick = () => {
        window.dashboardPush?.seen([key]);
        sync();
        if (account.state.entries[key])
          account.state.entries[key].seenAt = account.state.entries[key].at;
        save();
        render();
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
      const fields = Object.keys(values).filter((field) => old?.values[field] !== values[field]);
      baseline[item.key] = { at: sample.at, values };
      const changed = initialized && (!old || fields.length > 0);
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
        values: {
          status: job.status,
          summary: job.summary,
          sha: job.sha,
          outcome: job.pr_outcome,
          attempts: job.attempts,
          approved: job.feedback_approved,
          feedback: (job.feedback || []).map(({ kind, id, body }) => ({ kind, id, body })),
          checks: (job.check_details || [])
            .map(({ name, bucket, state }) => ({ name, bucket, state }))
            .sort((a, b) => JSON.stringify(a).localeCompare(JSON.stringify(b))),
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
  if (new URLSearchParams(location.search).get("updates") === "1") dialog.showModal();
})();
