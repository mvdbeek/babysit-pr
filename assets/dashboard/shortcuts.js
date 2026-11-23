"use strict";
// Single keys for the things done most from a keyboard, and the list that shows and
// changes them. A keyboard belongs to a device rather than to the watcher, so unlike the
// rest of Settings the keys are kept in this browser. A shortcut does nothing while a
// field has the keys or a dialog is open, so typing and dialogs keep every key they had.
(() => {
  const byId = (id) => document.getElementById(id);
  const dialog = byId("shortcuts-dialog");
  const STORAGE = "babysit-shortcuts";
  // The order the list shows them in. `key` is the default; `target` is the header
  // button whose tooltip names the key.
  const ACTIONS = [
    { id: "help", label: "Show keyboard shortcuts", key: "?" },
    { id: "search", label: "Search this view", key: "/" },
    { id: "newTask", label: "New task", key: "n", target: "new-task" },
    { id: "nextView", label: "Next view", key: "]" },
    { id: "previousView", label: "Previous view", key: "[" },
    { id: "notifications", label: "Recent updates", key: "b", target: "notifications-toggle" },
    { id: "settings", label: "Settings", key: ",", target: "settings-toggle" },
  ];
  // Kept by their own modules; listed so the keys are not given away twice.
  const FIXED = [{ label: "Voice assistant", key: "Alt+v" }];
  // Keys a page needs for moving through it and pressing buttons.
  const RESERVED = new Set(["Tab", "Shift+Tab", "Enter", "Space", "Escape"]);
  const MODIFIERS = new Set([
    "Shift",
    "Control",
    "Alt",
    "AltGraph",
    "Meta",
    "CapsLock",
    "Dead",
    "Process",
    "Unidentified",
  ]);
  const mac = /Mac|iP(hone|ad|od)/.test(navigator.platform || navigator.userAgent);
  let overrides = read();
  let recording = null;

  function read() {
    try {
      const value = JSON.parse(localStorage.getItem(STORAGE) || "{}");
      return value && typeof value === "object" && !Array.isArray(value) ? value : {};
    } catch {
      return {};
    }
  }
  function write() {
    try {
      if (Object.keys(overrides).length) localStorage.setItem(STORAGE, JSON.stringify(overrides));
      else localStorage.removeItem(STORAGE);
      return true;
    } catch {
      return false;
    }
  }
  function binding(id) {
    const value = overrides[id];
    return typeof value === "string" ? value : ACTIONS.find((action) => action.id === id).key;
  }
  // The same name for a key however it was pressed: modifiers in a fixed order, a
  // letter typed with Alt or Ctrl by its key rather than the character a Mac makes of
  // it, and Shift only where the character does not already say so ("?" is not "/").
  function combo(event) {
    if (MODIFIERS.has(event.key)) return null;
    const held = event.ctrlKey || event.altKey || event.metaKey;
    let name = event.key;
    let fromCode = false;
    const physical = /^(?:Key|Digit)(.)$/.exec(event.code || "");
    if (held && physical) {
      name = physical[1].toLowerCase();
      fromCode = true;
    }
    if (name === " ") name = "Space";
    const parts = [];
    if (event.ctrlKey) parts.push("Ctrl");
    if (event.altKey) parts.push("Alt");
    if (event.metaKey) parts.push("Meta");
    if (event.shiftKey && (name.length > 1 || fromCode)) parts.push("Shift");
    parts.push(name);
    return parts.join("+");
  }
  function label(key) {
    if (!key) return "Off";
    const name = key === "+" || key.endsWith("++") ? "+" : key.split("+").pop();
    const parts = key
      .slice(0, key.length - name.length)
      .split("+")
      .filter(Boolean);
    let shown = name.length === 1 ? name.toUpperCase() : name;
    if (name.length === 1 && name !== name.toLowerCase()) parts.push("Shift");
    if (name === "ArrowUp") shown = "↑";
    else if (name === "ArrowDown") shown = "↓";
    else if (name === "ArrowLeft") shown = "←";
    else if (name === "ArrowRight") shown = "→";
    const words = parts.map((part) => (part === "Meta" ? (mac ? "Cmd" : "Meta") : part));
    return [...words, shown].join("+");
  }
  function owner(key) {
    const action = ACTIONS.find((entry) => binding(entry.id) === key);
    if (action) return action.label;
    return FIXED.find((entry) => entry.key === key)?.label ?? null;
  }
  // Where keys are being typed, a shortcut would eat a character.
  function typing(target) {
    if (!(target instanceof Element)) return false;
    if (target.isContentEditable || target.closest(".ws-terminal.capturing")) return true;
    return target.matches(
      "textarea, select, input:not([type=checkbox], [type=radio], [type=button], " +
        "[type=submit], [type=reset], [type=range], [type=color], [type=file])",
    );
  }
  function tabs() {
    return [...document.querySelectorAll(".page-tabs [role=tab]")].filter((tab) => !tab.hidden);
  }
  function step(by) {
    const available = tabs();
    if (!available.length) return;
    const index = available.findIndex((tab) => tab.getAttribute("aria-selected") === "true");
    available[(index + by + available.length) % available.length].click();
  }
  function search() {
    const tab = tabs().find((entry) => entry.getAttribute("aria-selected") === "true");
    const panel = tab && byId(tab.getAttribute("aria-controls"));
    const input = [...(panel?.querySelectorAll("input[type=search]") ?? [])].find(
      (entry) => entry.offsetParent !== null && !entry.disabled,
    );
    if (!input) return false;
    input.focus();
    input.select();
    return true;
  }
  const RUN = {
    help: () => open(),
    search,
    newTask: () => byId("new-task").click(),
    nextView: () => step(1),
    previousView: () => step(-1),
    notifications: () => byId("notifications-toggle").click(),
    settings: () => byId("settings-toggle").click(),
  };
  document.addEventListener("keydown", (event) => {
    // Handlers nearer the key, such as a terminal taking keys or a menu's Escape, go first.
    if (recording || event.defaultPrevented || event.repeat || event.isComposing) return;
    if (typing(event.target) || document.querySelector("dialog[open]")) return;
    const key = combo(event);
    if (!key) return;
    const action = ACTIONS.find((entry) => binding(entry.id) === key);
    if (!action) return;
    if (RUN[action.id]() === false) return;
    event.preventDefault();
  });

  // Each header button's tooltip names its key, the way the voice button names Alt+V.
  function hints() {
    for (const action of ACTIONS) {
      const button = action.target && byId(action.target);
      if (!button) continue;
      const base = button.dataset.shortcutTitle ?? button.getAttribute("title") ?? action.label;
      button.dataset.shortcutTitle = base;
      const key = binding(action.id);
      button.title = key ? `${base} (${label(key)})` : base;
      if (key) button.setAttribute("aria-keyshortcuts", key);
      else button.removeAttribute("aria-keyshortcuts");
    }
    const help = binding("help");
    byId("settings-shortcuts-note").textContent = help
      ? `Press ${label(help)} anywhere to see them.`
      : "Single keys for common actions, kept in this browser.";
  }
  function status(text) {
    byId("shortcuts-status").textContent = text;
  }
  function render() {
    const list = byId("shortcuts-list");
    const rows = ACTIONS.map((action) => {
      const key = binding(action.id);
      const row = document.createElement("tr");
      row.dataset.action = action.id;
      const name = document.createElement("th");
      name.scope = "row";
      name.textContent = action.label;
      const keyCell = document.createElement("td");
      const kbd = document.createElement("kbd");
      kbd.textContent = recording === action.id ? "Press a key…" : label(key);
      kbd.classList.toggle("shortcut-off", !key && recording !== action.id);
      keyCell.append(kbd);
      const actions = document.createElement("td");
      actions.className = "shortcut-actions";
      const change = document.createElement("button");
      change.type = "button";
      change.textContent = recording === action.id ? "Cancel" : "Change";
      change.setAttribute("aria-label", `${change.textContent} ${action.label}`);
      change.onclick = () => {
        recording = recording === action.id ? null : action.id;
        status(recording ? `Press the new key for ${action.label}. Escape cancels.` : "");
        render();
        list.querySelector(`tr[data-action="${action.id}"] button`)?.focus();
      };
      const off = document.createElement("button");
      off.type = "button";
      off.textContent = "Turn off";
      off.setAttribute("aria-label", `Turn off ${action.label}`);
      off.disabled = !key;
      off.onclick = () => save(action, "");
      actions.append(change, off);
      row.append(name, keyCell, actions);
      return row;
    });
    for (const entry of FIXED) {
      const row = document.createElement("tr");
      const name = document.createElement("th");
      name.scope = "row";
      name.textContent = entry.label;
      const keyCell = document.createElement("td");
      const kbd = document.createElement("kbd");
      kbd.textContent = label(entry.key);
      keyCell.append(kbd);
      const note = document.createElement("td");
      note.className = "shortcut-fixed";
      note.textContent = "Fixed";
      row.append(name, keyCell, note);
      rows.push(row);
    }
    list.replaceChildren(...rows);
    byId("shortcuts-reset").disabled = !Object.keys(overrides).length;
  }
  function save(action, key) {
    if (key === action.key) delete overrides[action.id];
    else overrides[action.id] = key;
    recording = null;
    const kept = write();
    render();
    hints();
    const what = key ? `${action.label}: ${label(key)}` : `${action.label} is off`;
    status(kept ? `${what}.` : `${what}, until this page closes: the browser will not keep it.`);
  }
  // While a key is being chosen, every key is the answer, so this listens first.
  document.addEventListener(
    "keydown",
    (event) => {
      if (!recording) return;
      const key = combo(event);
      if (!key) return;
      event.preventDefault();
      event.stopPropagation();
      const action = ACTIONS.find((entry) => entry.id === recording);
      if (key === "Escape") {
        recording = null;
        status("");
        render();
        dialog.querySelector(`tr[data-action="${action.id}"] button`)?.focus();
        return;
      }
      if (key === "Backspace" || key === "Delete") return save(action, "");
      if (RESERVED.has(key)) {
        status(`${label(key)} stays with the page. Choose another key, or Escape to cancel.`);
        return;
      }
      const taken = owner(key);
      if (taken && taken !== action.label) {
        status(`${label(key)} is already ${taken}. Choose another key, or Escape to cancel.`);
        return;
      }
      save(action, key);
      dialog.querySelector(`tr[data-action="${action.id}"] button`)?.focus();
    },
    true,
  );
  function open() {
    recording = null;
    status("");
    render();
    if (!dialog.open) dialog.showModal();
  }
  byId("shortcuts-reset").onclick = () => {
    overrides = {};
    recording = null;
    write();
    render();
    hints();
    status("Back to the default keys.");
  };
  byId("shortcuts-close").onclick = () => dialog.close();
  byId("settings-shortcuts").onclick = open;
  dialog.addEventListener("cancel", (event) => {
    // Escape while choosing a key cancels the choice, not the list.
    if (recording) event.preventDefault();
  });
  dialog.addEventListener("close", () => {
    recording = null;
  });
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) {
      const rect = dialog.getBoundingClientRect();
      const outside =
        event.clientX < rect.left ||
        event.clientX > rect.right ||
        event.clientY < rect.top ||
        event.clientY > rect.bottom;
      if (outside) dialog.close();
    }
  });
  // Another tab changed them.
  window.addEventListener("storage", (event) => {
    if (event.key !== STORAGE && event.key !== null) return;
    overrides = read();
    hints();
    if (dialog.open) render();
  });
  hints();
  window.dashboardShortcuts = { open, key: (id) => binding(id), label };
})();
