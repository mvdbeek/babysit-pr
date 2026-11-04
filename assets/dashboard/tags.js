/* Shared focus across repositories and across the PR, issue and workspace tabs. */
(() => {
  const byId = (id) => document.getElementById(id);
  const select = byId("tag-focus");
  const status = byId("tag-status");
  const dialog = byId("tag-dialog");
  const input = byId("tag-name");
  const error = byId("tag-error");
  const storageKey = "babysit-pr:focus-tag:v1";
  let data = { assignments: {}, links: {}, names: [] };
  let signature = "";
  let indexedNames = new Map();
  let revision = 0;
  let loading = null;
  let generation = 0;
  let editing = null;
  let saving = false;
  let ready = false;
  const params = new URLSearchParams(location.search);
  let focus = params.get("tag") || "";
  if (!params.has("tag")) {
    try {
      focus = localStorage.getItem(storageKey) || "";
    } catch {
      // The URL still preserves focus when storage is unavailable.
    }
  }

  function node(tag, text, className) {
    const element = document.createElement(tag);
    if (text !== undefined) element.textContent = text;
    if (className) element.className = className;
    return element;
  }
  function target(item, kind) {
    return kind === "workspace"
      ? `workspace:${item.key}`
      : `${kind}:${item.repo.toLowerCase()}#${item.number}`;
  }
  function indexNames() {
    const index = new Map(
      Object.entries(data.assignments).map(([key, names]) => [key, new Set(names)]),
    );
    function add(key, names) {
      if (!index.has(key)) index.set(key, new Set());
      for (const name of names) index.get(key).add(name);
    }
    for (const [workspace, items] of Object.entries(data.links)) {
      for (const item of items) {
        add(workspace, data.assignments[item] || []);
        add(item, data.assignments[workspace] || []);
      }
    }
    indexedNames = new Map([...index].map(([key, names]) => [key, [...names].sort()]));
  }
  function names(key) {
    return indexedNames.get(key) || [];
  }
  function announce() {
    revision += 1;
    window.dispatchEvent(new Event("tags-changed"));
  }
  function options() {
    const all = [...new Set([...data.names, ...(focus ? [focus] : [])])].sort();
    const option = (value, label = value) => {
      const item = node("option", label);
      item.value = value;
      return item;
    };
    select.replaceChildren(option("", "All tags"), ...all.map((name) => option(name)));
    select.value = focus;
    window.dashboardAgents?.sync(select);
    byId("tag-suggestions").replaceChildren(...data.names.map((name) => option(name)));
    byId("tag-clear").hidden = !focus;
  }
  function setFocus(name) {
    focus = name;
    try {
      localStorage.setItem(storageKey, name);
    } catch {
      // Focus remains available for this tab and in its URL.
    }
    const url = new URL(location.href);
    if (name) url.searchParams.set("tag", name);
    else url.searchParams.delete("tag");
    history.replaceState(null, "", url);
    options();
    announce();
  }
  function accept(value) {
    ready = true;
    status.textContent = value.warning || "";
    const next = JSON.stringify(value);
    if (next === signature) return;
    signature = next;
    data = value;
    indexNames();
    options();
    if (dialog.open) renderEditor();
    announce();
  }
  async function response(url, init) {
    const result = await fetch(url, { ...init, signal: AbortSignal.timeout(15000) });
    const value = await result.json();
    if (!result.ok) throw Error(value.error || "Tag request failed");
    return value;
  }
  function refresh() {
    loading ??= (async () => {
      const requestGeneration = generation;
      try {
        const value = await response("/api/tags");
        if (generation === requestGeneration) accept(value);
      } catch (err) {
        status.textContent = `Tags unavailable: ${err.message}`;
      } finally {
        loading = null;
      }
    })();
    return loading;
  }
  async function change(field, name) {
    if (saving) return;
    saving = true;
    generation += 1;
    error.textContent = "";
    renderEditor();
    try {
      accept(
        await response("/api/tags", {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-Babysit-Action": "tags" },
          body: JSON.stringify({ target: editing, [field]: [name] }),
        }),
      );
      if (field === "add") input.value = "";
    } catch (err) {
      error.textContent = err.message;
    } finally {
      saving = false;
      renderEditor();
    }
  }
  function renderEditor() {
    const direct = data.assignments[editing] || [];
    const list = byId("tag-assigned");
    list.replaceChildren();
    for (const name of direct) {
      const line = node("li");
      const remove = node("button", "Remove");
      remove.type = "button";
      remove.setAttribute("aria-label", `Remove tag ${name}`);
      remove.disabled = saving;
      remove.onclick = () => void change("remove", name);
      line.append(node("span", name), remove);
      list.append(line);
    }
    if (!direct.length) list.append(node("li", "No tags assigned directly."));
    const inherited = names(editing).filter((name) => !direct.includes(name));
    byId("tag-inherited").textContent = inherited.length
      ? `From linked work: ${inherited.join(", ")}. Remove these tags on the linked workspace or item.`
      : "";
    byId("tag-add").disabled = saving;
    input.disabled = saving;
  }
  function controls(item, kind) {
    const key = target(item, kind);
    const box = node("div", undefined, "work-tags");
    box.addEventListener("click", (event) => event.stopPropagation());
    for (const name of names(key)) {
      const chip = node("button", name, "badge blue tag-chip");
      chip.type = "button";
      chip.title = `Focus on tag ${name}`;
      chip.onclick = () => setFocus(name);
      box.append(chip);
    }
    const edit = node("button", "Tags", "tag-edit");
    edit.type = "button";
    const label = kind === "workspace" ? item.name : `${item.repo} #${item.number}`;
    edit.setAttribute("aria-label", `Edit tags for ${label}`);
    edit.onclick = () => {
      editing = key;
      byId("tag-item").textContent = label;
      error.textContent = "";
      input.value = "";
      renderEditor();
      dialog.showModal();
      input.focus();
      void refresh();
    };
    box.append(edit);
    return box;
  }
  window.dashboardTags = {
    controls,
    matches: (item, kind) => !focus || (ready && names(target(item, kind)).includes(focus)),
    get revision() {
      return revision;
    },
    refresh,
  };
  select.onchange = () => setFocus(select.value);
  byId("tag-clear").onclick = () => setFocus("");
  byId("tag-close").onclick = () => dialog.close();
  byId("tag-form").onsubmit = (event) => {
    event.preventDefault();
    if (input.value.trim()) void change("add", input.value);
  };
  window.addEventListener("storage", (event) => {
    if (event.key === storageKey) setFocus(event.newValue || "");
  });
  options();
  void refresh();
  setInterval(() => {
    if (!document.hidden) void refresh();
  }, 5000);
})();
