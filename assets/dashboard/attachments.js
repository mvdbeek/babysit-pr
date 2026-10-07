/* Files attached to what the dashboard sends an agent: a follow-up message or a task.
   Each file is uploaded as soon as it is chosen, pasted or dropped; the request then names the
   uploads, and the agent receives their paths after its text. */
(() => {
  const MAX_FILE = 25 * 1024 * 1024;
  const MAX_FILES = 10;
  function node(tag, text, className) {
    const element = document.createElement(tag);
    if (text !== undefined) element.textContent = text;
    if (className) element.className = className;
    return element;
  }
  function size(bytes) {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  }
  const carriesFiles = (event) => [...(event.dataTransfer?.types || [])].includes("Files");
  // A file dropped beside a drop target would open in the tab, leaving the dashboard.
  window.addEventListener("dragover", (event) => {
    if (!carriesFiles(event) || event.defaultPrevented) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = "none";
  });
  window.addEventListener("drop", (event) => {
    if (carriesFiles(event) && !event.defaultPrevented) event.preventDefault();
  });
  async function upload(file) {
    let response;
    try {
      response = await fetch("/api/attachment-upload", {
        method: "POST",
        headers: {
          "Content-Type": "application/octet-stream",
          "X-Babysit-Action": "attachment-upload",
          "X-Filename": encodeURIComponent(file.name || "pasted-file"),
        },
        body: file,
      });
    } catch {
      throw Error("The upload did not reach the dashboard");
    }
    const value = await response.json().catch(() => ({}));
    if (!response.ok) throw Error(value.error || `HTTP ${response.status}`);
    return value;
  }
  // A picker: a button choosing files, a list of them, paste into `pasteTarget` and
  // drop onto `dropTarget` (the paste target unless given).
  // `files` restores earlier uploads ({id, name, size}); `onchange` gets the list.
  function picker({
    files = [],
    pasteTarget = null,
    dropTarget = pasteTarget,
    onchange = () => {},
  } = {}) {
    const element = node("div", undefined, "attachments");
    const input = document.createElement("input");
    input.type = "file";
    input.multiple = true;
    input.hidden = true;
    const choose = node("button", "Attach files…", "attach-button");
    choose.type = "button";
    choose.onclick = () => input.click();
    const list = node("ul", undefined, "attachment-list");
    const status = node("small", undefined, "attachment-status");
    status.setAttribute("role", "status");
    const row = node("div", undefined, "attachment-row");
    row.append(choose, status);
    element.append(input, row, list);
    let items = files.filter((file) => file && typeof file.id === "string");
    let pending = 0;
    function render() {
      list.replaceChildren(
        ...items.map((file) => {
          const item = node("li");
          item.append(node("span", file.name, "attachment-name"), node("small", size(file.size)));
          const remove = node("button", "Remove");
          remove.type = "button";
          remove.setAttribute("aria-label", `Remove attachment ${file.name}`);
          remove.onclick = () => {
            items = items.filter((other) => other !== file);
            render();
            onchange(items);
          };
          item.append(remove);
          return item;
        }),
      );
      choose.disabled = items.length + pending >= MAX_FILES;
    }
    async function add(chosen) {
      const room = MAX_FILES - items.length - pending;
      const accepted = chosen.slice(0, Math.max(0, room));
      const problems = [];
      if (chosen.length > accepted.length) problems.push(`At most ${MAX_FILES} files.`);
      const sized = accepted.filter((file) => {
        if (file.size > 0 && file.size <= MAX_FILE) return true;
        problems.push(`${file.name || "A file"} is empty or over ${size(MAX_FILE)}.`);
        return false;
      });
      pending += sized.length;
      render();
      status.textContent = sized.length ? `Uploading ${sized.length}…` : problems.join(" ");
      await Promise.all(
        sized.map(async (file) => {
          try {
            const uploaded = await upload(file);
            items = [...items, uploaded]; // After the await: uploads finish in any order.
          } catch (error) {
            problems.push(`${file.name || "A file"}: ${error.message}`);
          } finally {
            pending--;
          }
        }),
      );
      render();
      status.textContent = pending ? `Uploading ${pending}…` : problems.join(" ");
      onchange(items);
    }
    input.onchange = () => {
      const chosen = [...input.files];
      input.value = "";
      void add(chosen);
    };
    // A pasted screenshot attaches; pasted text stays text.
    function paste(event) {
      const pasted = [...(event.clipboardData?.files || [])];
      if (!pasted.length) return;
      event.preventDefault();
      void add(pasted);
    }
    // Dropped files attach; dragged text still drops into the field as text.
    // The highlight lasts while dragover keeps arriving: dragleave goes missing when
    // the hovered child is redrawn mid-drag, and some browsers omit its relatedTarget.
    let fade = 0;
    function hover(on) {
      clearTimeout(fade);
      dropTarget.classList.toggle("attachment-drop", on);
      if (on) fade = setTimeout(() => hover(false), 200);
    }
    function dragover(event) {
      if (!carriesFiles(event)) return;
      event.preventDefault();
      event.dataTransfer.dropEffect = "copy";
      hover(true);
    }
    const dragenter = dragover;
    function dragleave(event) {
      if (carriesFiles(event) && event.relatedTarget && !dropTarget.contains(event.relatedTarget))
        hover(false);
    }
    function drop(event) {
      if (!carriesFiles(event)) return;
      hover(false);
      // An image dragged from a web page can name "Files" yet carry none; let it drop.
      if (!event.dataTransfer.files.length) return;
      event.preventDefault();
      const entries = [...event.dataTransfer.items].map((item) => item.webkitGetAsEntry?.());
      const dropped = [...event.dataTransfer.files].filter((_, i) => !entries[i]?.isDirectory);
      if (dropped.length) void add(dropped);
      else status.textContent = "Folders cannot be attached.";
    }
    const dragHandlers = { dragenter, dragover, dragleave, drop };
    pasteTarget?.addEventListener("paste", paste);
    for (const [type, handler] of Object.entries(dragHandlers))
      dropTarget?.addEventListener(type, handler);
    render();
    return {
      element,
      files: () => items,
      ids: () => items.map((file) => file.id),
      busy: () => pending > 0,
      clear() {
        items = [];
        status.textContent = "";
        render();
      },
      // Drop the given uploads, as once they were sent; any attached since stay.
      remove(ids) {
        const gone = new Set(ids);
        items = items.filter((file) => !gone.has(file.id));
        render();
      },
      // A picker replaced by another stops taking pastes and drops and reporting changes.
      destroy() {
        pasteTarget?.removeEventListener("paste", paste);
        for (const [type, handler] of Object.entries(dragHandlers))
          dropTarget?.removeEventListener(type, handler);
        if (dropTarget) hover(false);
        onchange = () => {};
      },
    };
  }
  window.dashboardAttachments = { picker };
})();
