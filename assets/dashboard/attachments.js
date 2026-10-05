/* Files attached to what the dashboard sends an agent: a follow-up message or a task.
   Each file is uploaded as soon as it is chosen or pasted; the request then names the
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
  // A picker: a button choosing files, a list of them, and paste into `pasteTarget`.
  // `files` restores earlier uploads ({id, name, size}); `onchange` gets the list.
  function picker({ files = [], pasteTarget = null, onchange = () => {} } = {}) {
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
    pasteTarget?.addEventListener("paste", paste);
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
      // A picker replaced by another stops taking pastes and reporting changes.
      destroy() {
        pasteTarget?.removeEventListener("paste", paste);
        onchange = () => {};
      },
    };
  }
  window.dashboardAttachments = { picker };
})();
