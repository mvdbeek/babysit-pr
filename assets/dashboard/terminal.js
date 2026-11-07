/* The workspace viewer's Terminal tab: one pane of the workspace, mirrored from herdr's
   view of its screen every couple of seconds while shown. Keys, typed text and shell
   commands go to that pane as they would in Collie; a command runs in a new split, never
   in a pane someone types in. The controls are built once per pane, so a poll only
   replaces the screen. */
(() => {
  const byId = (id) => document.getElementById(id);
  function node(tag, text, className) {
    const element = document.createElement(tag);
    if (text !== undefined) element.textContent = text;
    if (className) element.className = className;
    return element;
  }
  const PALETTE = [
    "#1e1e1e",
    "#cd3131",
    "#0dbc79",
    "#e5e510",
    "#2472c8",
    "#bc3fbc",
    "#11a8cd",
    "#e5e5e5",
    "#666666",
    "#f14c4c",
    "#23d18b",
    "#f5f543",
    "#3b8eea",
    "#d670d6",
    "#29b8db",
    "#ffffff",
  ];
  function colour256(index) {
    if (index < 16) return PALETTE[index];
    if (index >= 232) {
      const level = 8 + (index - 232) * 10;
      return `rgb(${level},${level},${level})`;
    }
    const cube = index - 16;
    const step = (value) => (value ? 55 + value * 40 : 0);
    return `rgb(${step(Math.floor(cube / 36))},${step(Math.floor(cube / 6) % 6)},${step(cube % 6)})`;
  }
  // Colour and weight from SGR codes; any other escape sequence is dropped.
  function sgr(style, codes) {
    for (let i = 0; i < codes.length; i += 1) {
      const code = codes[i];
      if (code === 0) Object.keys(style).forEach((key) => delete style[key]);
      else if (code === 1) style.bold = true;
      else if (code === 2) style.dim = true;
      else if (code === 3) style.italic = true;
      else if (code === 4) style.underline = true;
      else if (code === 7) style.inverse = true;
      else if (code === 22) {
        delete style.bold;
        delete style.dim;
      } else if (code === 23) delete style.italic;
      else if (code === 24) delete style.underline;
      else if (code === 27) delete style.inverse;
      else if (code >= 30 && code <= 37) style.fg = PALETTE[code - 30];
      else if (code >= 90 && code <= 97) style.fg = PALETTE[code - 82];
      else if (code >= 40 && code <= 47) style.bg = PALETTE[code - 40];
      else if (code >= 100 && code <= 107) style.bg = PALETTE[code - 92];
      else if (code === 39) delete style.fg;
      else if (code === 49) delete style.bg;
      else if (code === 38 || code === 48) {
        const key = code === 38 ? "fg" : "bg";
        if (codes[i + 1] === 5 && codes[i + 2] !== undefined) {
          style[key] = colour256(codes[i + 2]);
          i += 2;
        } else if (codes[i + 1] === 2 && codes[i + 4] !== undefined) {
          style[key] = `rgb(${codes[i + 2]},${codes[i + 3]},${codes[i + 4]})`;
          i += 4;
        }
      }
    }
  }
  function styled(text, style) {
    if (!Object.keys(style).length) return document.createTextNode(text);
    const span = node("span", text);
    const fg = style.inverse ? style.bg || "var(--term-bg)" : style.fg;
    const bg = style.inverse ? style.fg || "var(--term-fg)" : style.bg;
    if (fg) span.style.color = fg;
    if (bg) span.style.backgroundColor = bg;
    if (style.bold) span.style.fontWeight = "bold";
    if (style.dim) span.style.opacity = "0.6";
    if (style.italic) span.style.fontStyle = "italic";
    if (style.underline) span.style.textDecoration = "underline";
    return span;
  }
  // eslint-disable-next-line no-control-regex
  const ESCAPE = /\x1b(?:\[([0-9;?]*)([A-Za-z])|\][^\x07\x1b]*(?:\x07|\x1b\\)|[()][A-Za-z0-9]|.)/g;
  function ansi(text) {
    const fragment = document.createDocumentFragment();
    const style = {};
    let at = 0;
    for (const match of text.matchAll(ESCAPE)) {
      if (match.index > at) fragment.append(styled(text.slice(at, match.index), { ...style }));
      if (match[2] === "m")
        sgr(
          style,
          (match[1] || "0").split(";").map((part) => Number(part) || 0),
        );
      at = match.index + match[0].length;
    }
    if (at < text.length) fragment.append(styled(text.slice(at), { ...style }));
    return fragment;
  }
  // Named keys herdr sends, in the order the pad shows them.
  const PAD = [
    ["Esc", "esc"],
    ["Enter", "enter"],
    ["Tab", "tab"],
    ["⇧Tab", "shift+tab"],
    ["↑", "up"],
    ["↓", "down"],
    ["←", "left"],
    ["→", "right"],
    ["Space", "space"],
    ["⌫", "backspace"],
    ["Ctrl+C", "ctrl+c"],
    ["Ctrl+D", "ctrl+d"],
    ["1", "1"],
    ["2", "2"],
    ["3", "3"],
  ];
  const PAD_NAMES = {
    "⇧Tab": "Shift+Tab",
    "↑": "Up",
    "↓": "Down",
    "←": "Left",
    "→": "Right",
    "⌫": "Backspace",
  };
  // The viewer is the host: ``current()`` is its state while the terminal shows (else
  // null), ``showPane`` reopens it on another pane, and ``busy()`` says a load of its own
  // is under way. ``choice``, ``getJSON`` and ``paneLabel`` are its controls and requests.
  function create(host) {
    let shown = null;
    let polling = false;
    // Keys and text typed while capturing, sent in order, one request at a time.
    const typedQueue = [];
    let typing = false;
    async function postTerminal(path, body) {
      const response = await fetch(`/api/${path}`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Babysit-Action": path },
        body: JSON.stringify(body),
      });
      let value;
      try {
        value = await response.json();
      } catch {
        throw new Error(`HTTP ${response.status}: the dashboard did not answer`);
      }
      if (!response.ok || value.error) throw new Error(value.error || `HTTP ${response.status}`);
      return value;
    }
    function terminalNote(text) {
      const note = byId("ws-terminal-note");
      if (note) note.textContent = text;
    }
    // A screen read soon after typing, so the reader sees what the keys did.
    function refreshSoon() {
      const current = host.current();
      setTimeout(() => {
        if (host.current() === current) void poll();
      }, 300);
    }
    async function sendKeys(body) {
      const current = host.current();
      if (!current) return;
      try {
        await postTerminal("workspace-keys", {
          workspace: current.entry.workspace,
          pane: current.pane,
          ...body,
        });
        if (host.current() === current) terminalNote("");
      } catch (error) {
        if (host.current() === current) terminalNote(error.message);
        throw error;
      }
      refreshSoon();
    }
    // Captured keystrokes: runs of text become one text request, runs of keys one keys
    // request, in the order typed.
    function queueTyped(item) {
      const last = typedQueue.at(-1);
      if (item.text !== undefined && last?.text !== undefined) last.text += item.text;
      else if (item.keys && last?.keys && last.keys.length < 32) last.keys.push(...item.keys);
      else typedQueue.push(item);
      if (!typing) void drainTyped();
    }
    async function drainTyped() {
      typing = true;
      try {
        while (typedQueue.length) {
          const item = typedQueue.shift();
          try {
            await sendKeys(item.keys ? { keys: item.keys } : { text: item.text, enter: false });
          } catch {
            typedQueue.length = 0; // The note says why; what followed would land out of order.
          }
        }
      } finally {
        typing = false;
      }
    }
    const KEY_NAMES = {
      Escape: "esc",
      Enter: "enter",
      Tab: "tab",
      Backspace: "backspace",
      Delete: "delete",
      ArrowUp: "up",
      ArrowDown: "down",
      ArrowLeft: "left",
      ArrowRight: "right",
      Home: "home",
      End: "end",
      PageUp: "pageup",
      PageDown: "pagedown",
    };
    function captured(event) {
      if (event.metaKey || event.isComposing) return null;
      let name =
        KEY_NAMES[event.key] || (/^F([1-9]|1[0-2])$/.test(event.key) && event.key.toLowerCase());
      if (!name && (event.ctrlKey || event.altKey) && /^[a-z0-9]$/i.test(event.key))
        name = event.key.toLowerCase();
      if (name) {
        const modifiers = [
          event.ctrlKey && "ctrl",
          event.altKey && "alt",
          event.shiftKey && !/^[a-z0-9]$/.test(name) && "shift",
        ].filter(Boolean);
        return { keys: [[...modifiers, name].join("+")] };
      }
      if (event.key.length === 1 && !event.ctrlKey && !event.altKey) return { text: event.key };
      return null;
    }
    function screenElement() {
      const screen = node("pre", undefined, "ws-terminal");
      screen.tabIndex = 0;
      screen.setAttribute("role", "log");
      screen.setAttribute(
        "aria-label",
        "Terminal screen; while focused with typing on, keys go to the pane",
      );
      screen.addEventListener("keydown", (event) => {
        const capture = byId("ws-terminal-capture");
        if (!capture?.checked) return;
        if (event.ctrlKey && event.key === "]") {
          // The way out, as telnet's: every other key, Tab included, goes to the pane.
          event.preventDefault();
          capture.checked = false;
          screen.classList.remove("capturing");
          capture.focus();
          return;
        }
        const item = captured(event);
        if (!item) return;
        event.preventDefault();
        queueTyped(item);
      });
      screen.addEventListener("paste", (event) => {
        if (!byId("ws-terminal-capture")?.checked) return;
        event.preventDefault();
        // Line breaks are typed as Enter, never as raw control characters.
        const lines = event.clipboardData.getData("text").replace(/\r\n?/g, "\n").split("\n");
        lines.forEach((line, index) => {
          if (index) queueTyped({ keys: ["enter"] });
          // eslint-disable-next-line no-control-regex
          if (line) queueTyped({ text: line.replace(/[\x00-\x1f\x7f-\x9f]/g, "") });
        });
      });
      return screen;
    }
    function terminalControls(data) {
      const holder = node("div", undefined, "ws-terminal-controls");
      if (data.panes.length > 1)
        holder.append(
          host.choice(
            "Pane",
            data.panes.map((pane) => [pane.pane, host.paneLabel(pane)]),
            data.pane,
            (value) => host.showPane(value),
          ),
        );
      const capture = node("label", undefined, "ws-terminal-capture");
      const box = node("input");
      box.type = "checkbox";
      box.id = "ws-terminal-capture";
      capture.title =
        "While the screen is focused, every key you press goes to the pane, Esc and Tab included; Ctrl+] stops";
      capture.append(box, " Type into the screen");
      box.onchange = () => {
        byId("ws-viewer").querySelector(".ws-terminal")?.classList.toggle("capturing", box.checked);
        if (box.checked) byId("ws-viewer").querySelector(".ws-terminal")?.focus();
      };
      holder.append(capture);
      return holder;
    }
    function keyPad() {
      const pad = node("div", undefined, "ws-terminal-pad");
      pad.setAttribute("role", "group");
      pad.setAttribute("aria-label", "Send a key to the pane");
      for (const [label, key] of PAD) {
        const button = node("button", label);
        button.type = "button";
        button.setAttribute("aria-label", `Send ${PAD_NAMES[label] || label}`);
        button.onclick = () => void sendKeys({ keys: [key] }).catch(() => {});
        pad.append(button);
      }
      return pad;
    }
    function typeForm() {
      const form = node("form", undefined, "ws-terminal-form");
      const field = node("input");
      field.type = "text";
      field.id = "ws-terminal-text";
      field.placeholder = "Text to type into the pane";
      field.setAttribute("aria-label", "Text to type into the pane");
      field.autocomplete = "off";
      field.setAttribute("autocapitalize", "off");
      field.spellcheck = false;
      const enter = node("label");
      const box = node("input");
      box.type = "checkbox";
      box.id = "ws-terminal-enter";
      box.checked = true;
      enter.append(box, " then Enter");
      const send = node("button", "Type");
      send.type = "submit";
      form.append(field, enter, send);
      form.onsubmit = (event) => {
        event.preventDefault();
        if (!field.value && !box.checked) return;
        send.disabled = true;
        void sendKeys({ text: field.value, enter: box.checked })
          .then(() => (field.value = ""))
          .catch(() => {})
          .finally(() => (send.disabled = false));
      };
      return form;
    }
    function commandForm() {
      const form = node("form", undefined, "ws-terminal-form ws-terminal-command");
      const field = node("input");
      field.type = "text";
      field.id = "ws-terminal-command";
      field.placeholder = "Shell command, e.g. git status";
      field.setAttribute("aria-label", "Shell command to run in a new pane");
      field.autocomplete = "off";
      field.setAttribute("autocapitalize", "off");
      field.spellcheck = false;
      const run = node("button", "Run in new pane");
      run.type = "submit";
      run.title = "Opens a shell split in this workspace's checkout and runs the command there";
      form.append(field, run);
      form.onsubmit = async (event) => {
        event.preventDefault();
        const current = host.current();
        const command = field.value.trim();
        if (!current || !command) return;
        run.disabled = true;
        try {
          const value = await postTerminal("workspace-shell", {
            workspace: current.entry.workspace,
            command,
          });
          if (host.current() !== current) return;
          field.value = "";
          host.showPane(value.pane);
        } catch (error) {
          if (host.current() === current) terminalNote(error.message);
        } finally {
          run.disabled = false;
        }
      };
      return form;
    }
    function render(data) {
      byId("ws-viewer-meta").textContent = "";
      const content = byId("ws-viewer-content");
      const key = JSON.stringify([host.current().id, data.pane, data.panes]);
      if (shown !== key || !content.querySelector(".ws-terminal")) {
        // A new pane or pane list: the controls are rebuilt, keeping what was typed.
        const typed = byId("ws-terminal-text")?.value || "";
        const command = byId("ws-terminal-command")?.value || "";
        const capturing = byId("ws-terminal-capture")?.checked || false;
        const screen = screenElement();
        byId("ws-viewer-controls").replaceChildren(terminalControls(data));
        content.replaceChildren(
          screen,
          keyPad(),
          typeForm(),
          commandForm(),
          node("p", "", "ws-terminal-note"),
        );
        content.querySelector(".ws-terminal-note").id = "ws-terminal-note";
        content.querySelector(".ws-terminal-note").setAttribute("role", "status");
        byId("ws-terminal-text").value = typed;
        byId("ws-terminal-command").value = command;
        byId("ws-terminal-capture").checked = capturing;
        screen.classList.toggle("capturing", capturing);
        shown = key;
      }
      const screen = content.querySelector(".ws-terminal");
      if (screen.dataset.text !== data.screen) {
        screen.dataset.text = data.screen;
        screen.replaceChildren(ansi(data.screen.replace(/\s+$/, "")));
      }
    }
    async function poll() {
      const current = host.current();
      if (polling || !current?.data || host.busy()) return;
      polling = true;
      try {
        const pane = current.pane;
        const data = await host.getJSON("/api/workspace-terminal", {
          workspace: current.entry.workspace,
          pane,
        });
        // Another pane chosen meanwhile is the same view: its own load shows it.
        if (host.current() !== current || current.pane !== pane) return;
        current.data = data;
        render(data);
        byId("ws-viewer-error").textContent = "";
      } catch (error) {
        if (host.current() === current) byId("ws-viewer-error").textContent = error.message;
      } finally {
        polling = false;
      }
    }
    setInterval(() => {
      if (!document.hidden && byId("ws-viewer").open) void poll();
    }, 2000);
    return {
      render,
      // A new view starts its controls afresh.
      reset: () => (shown = null),
    };
  }
  window.workspaceTerminal = create;
})();
