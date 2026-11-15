/* The workspace viewer's Artefacts tab: the files the workspace's agents named in their
   transcripts that a dufs share holds, by folder, each with a link that opens it on the
   share and the transcript entry that named it. The tab is offered only where the
   dashboard has a share configured, which is asked once, when a viewer first opens. */
(() => {
  const byId = (id) => document.getElementById(id);
  function node(tag, text, className) {
    const element = document.createElement(tag);
    if (text !== undefined) element.textContent = text;
    if (className) element.className = className;
    return element;
  }
  const web = (url) => /^https?:\/\//.test(url || "");
  function plural(count, one, many) {
    return `${count} ${count === 1 ? one : many}`;
  }
  function size(bytes) {
    if (typeof bytes !== "number") return null;
    const units = ["B", "KiB", "MiB", "GiB"];
    let value = bytes;
    let unit = 0;
    while (value >= 1024 && unit < units.length - 1) {
      value /= 1024;
      unit += 1;
    }
    return `${unit && value < 10 ? value.toFixed(1) : Math.round(value)} ${units[unit]}`;
  }
  // What stands in for a file's picture: its extension, as agents name files by it.
  function kind(row) {
    if (row.kind === "folder") return "DIR";
    const dot = row.name.lastIndexOf(".");
    const extension = dot > 0 ? row.name.slice(dot + 1) : "";
    return /^[A-Za-z0-9]{1,5}$/.test(extension) ? extension.toUpperCase() : "FILE";
  }
  // The viewer is the host: ``showEntry`` opens a session's transcript around an entry,
  // and ``external``, ``stamp`` and ``refreshButton`` are its link, time and reload
  // controls.
  function create(host) {
    let offered = false;
    let asking = null;
    // Whether the dashboard has a share: asked until it answers, then kept for the page.
    function offer() {
      if (asking) return asking;
      asking = fetch("/api/workspace-artefacts")
        .then((response) => {
          if (!response.ok) throw new Error(`HTTP ${response.status}`);
          return response.json();
        })
        .then((value) => {
          offered = value?.enabled === true;
          byId("ws-view-artefacts").hidden = !offered;
        })
        .catch(() => {
          asking = null; // Asked again when the next viewer opens.
        });
      return asking;
    }
    window.addEventListener("workspace-viewer-open", () => void offer());
    function source(mention) {
      const agent = mention.agent === "claude" ? "Claude" : "Codex";
      const button = node("button", undefined, "ws-artefact-source");
      button.type = "button";
      button.append(`Named by ${agent} in ${mention.tool || "a reply"}`);
      const when = host.stamp(mention.time);
      if (when) button.append(" · ", when);
      button.title = "Show where the transcript names it";
      button.onclick = () => host.showEntry(mention.session, mention.index);
      return button;
    }
    function tile(row) {
      const holder = node("span", undefined, "ws-artefact-tile");
      const label = node("span", kind(row), "ws-artefact-kind");
      if (!web(row.image)) {
        holder.append(label);
        return holder;
      }
      const image = node("img");
      image.alt = "";
      image.loading = "lazy";
      image.decoding = "async";
      image.referrerPolicy = "no-referrer";
      // A share this browser cannot reach still lists the file, by its kind.
      image.onerror = () => holder.replaceChildren(label);
      image.src = row.image;
      holder.append(image);
      return holder;
    }
    function fileRow(row) {
      const item = node("li", undefined, "ws-artefact");
      const main = node("div", undefined, "ws-artefact-main");
      const name = web(row.url)
        ? host.external(row.name + (row.kind === "folder" ? "/" : ""), row.url)
        : node("span", row.name);
      name.classList.add("ws-artefact-name");
      const facts = node("span", undefined, "ws-artefact-facts");
      const measured =
        row.kind === "folder" ? plural(row.size ?? 0, "entry", "entries") : size(row.size);
      if (measured) facts.append(measured);
      const when = row.modified ? host.stamp(row.modified * 1000) : null;
      if (when) facts.append(measured ? " · " : "", "changed ", when);
      main.append(name, facts);
      if (row.named) main.append(source(row.named));
      item.append(tile(row), main);
      return item;
    }
    function folderSection(folder) {
      const section = node("section", undefined, "ws-artefact-folder");
      const head = node("h3");
      const label = `${folder.path || "Top of the share"}${folder.path ? "/" : ""}`;
      head.append(web(folder.url) ? host.external(label, folder.url) : label);
      const inner = folder.files.filter((row) => row.kind === "folder").length;
      const notes = [plural(folder.files.length - inner, "file", "files")];
      if (inner) notes.push(plural(inner, "folder", "folders"));
      if (folder.more) notes.push(`${folder.more} more on the share`);
      if (folder.others)
        notes.push(`${plural(folder.others, "other file", "other files")} in this folder`);
      head.append(node("span", notes.join(" · "), "ws-artefact-count"));
      section.append(head);
      if (folder.named) {
        const whole = node(
          "p",
          "The folder itself was named, so all its files are listed. ",
          "pr-meta",
        );
        whole.append(source(folder.named));
        section.append(whole);
      }
      const list = node("ul", undefined, "ws-artefact-list");
      list.append(...folder.files.map(fileRow));
      section.append(list);
      return section;
    }
    function missingSection(missing) {
      const details = node("details", undefined, "ws-artefact-missing");
      details.append(
        node(
          "summary",
          `${plural(missing.length, "named path is", "named paths are")} not on the share`,
        ),
      );
      const list = node("ul");
      for (const entry of missing) {
        const item = node("li");
        item.append(node("code", entry.path), " ", source(entry.named));
        list.append(item);
      }
      details.append(list);
      return details;
    }
    function render(data) {
      if (!Array.isArray(data.folders)) {
        // The dashboard was restarted without its share since this page loaded.
        byId("ws-viewer-controls").replaceChildren(host.refreshButton());
        byId("ws-viewer-meta").textContent =
          "This dashboard no longer has an artefact share configured; reload the page.";
        byId("ws-viewer-content").replaceChildren();
        return;
      }
      const controls = [host.refreshButton()];
      if (web(data.share)) {
        const share = host.external("Open the share", data.share);
        share.className = "ws-artefact-share";
        controls.push(share);
      }
      byId("ws-viewer-controls").replaceChildren(...controls);
      const files = data.folders.reduce(
        (count, folder) => count + folder.files.filter((row) => row.kind === "file").length,
        0,
      );
      const sessions = plural(data.searched, "session", "sessions");
      // Folders listed without a file hold only folders; names that could not be looked
      // up (the notes say why) are not the same as no names.
      const summary = files
        ? `${plural(files, "artefact", "artefacts")} in ${plural(data.folders.length, "folder", "folders")}, ` +
          `named in the transcripts of ${sessions}.`
        : data.folders.length
          ? `No files, only folders, in what the transcripts of ${sessions} name.`
          : (data.notes || []).length
            ? `No artefacts could be listed from the transcripts of ${sessions}.`
            : `No artefacts: nothing on the share is named in the transcripts of ${sessions}.`;
      byId("ws-viewer-meta").textContent =
        summary +
        (data.behind
          ? ` ${plural(data.behind, "session was", "sessions were")} not read to the end in time;` +
            " refresh to read on."
          : "");
      const content = node("div", undefined, "ws-artefacts");
      for (const note of data.notes || []) content.append(node("p", note, "ws-artefact-note"));
      content.append(...data.folders.map(folderSection));
      if (!data.folders.length)
        content.append(
          node(
            "p",
            "Links an agent gives to the share, and files it writes or copies there, are listed here.",
            "pr-meta",
          ),
        );
      if (data.missing.length) content.append(missingSection(data.missing));
      byId("ws-viewer-content").replaceChildren(content);
    }
    // An answer that failed leaves the way to ask again.
    function failed() {
      byId("ws-viewer-controls").replaceChildren(host.refreshButton());
    }
    return { render, failed, offer, offered: () => offered };
  }
  window.workspaceArtefacts = create;
})();
