// Registered on github.com only after you turn it on in Settings. It finds the pull
// requests and issues a page shows, asks the extension's background (which holds the
// pairing token) what Babysitter knows about them, and shows that as text. Nothing is sent
// to GitHub.
(() => {
  if (window.babysitGitHub) return;
  window.babysitGitHub = true;
  const ITEM = /^\/([^/]+)\/([^/]+)\/(pull|issues)\/(\d+)(?:\/.*)?$/;
  const LIST = /^(?:\/[^/]+\/[^/]+)?\/(pulls|issues)\/?$/;
  // A list's title links: the classic list, and the React one.
  const TITLES = 'a[id^="issue_"][id$="_link"], a[data-testid="issue-pr-title-link"]';
  const FRESH = 60 * 1000;
  const known = new Map();
  const asking = new Set();
  let error = null;

  function itemURL(href) {
    try {
      const url = new URL(href, location.href);
      const match = url.origin === "https://github.com" && ITEM.exec(url.pathname);
      return match ? `https://github.com/${match[1]}/${match[2]}/${match[3]}/${match[4]}` : null;
    } catch {
      return null;
    }
  }
  function node(tag, text, className) {
    const element = document.createElement(tag);
    if (text !== undefined) element.textContent = text;
    if (className) element.className = className;
    return element;
  }
  // Review text comes from agents' files: it is only ever text, and only web links link.
  function link(text, href, className) {
    const element = node("a", text, className);
    try {
      const url = new URL(href);
      if (["http:", "https:"].includes(url.protocol)) {
        element.href = url.href;
        element.target = "_blank";
        element.rel = "noopener noreferrer";
      }
    } catch {
      // Not a link; left as text.
    }
    return element;
  }
  function counts(review) {
    const parts = review.counts.map((c) => `${c.count} ${c.severity}`);
    return parts.length ? parts.join(" · ") : review.verdict ? "" : "no findings";
  }
  function summary(review) {
    return [review.verdict, counts(review)].filter(Boolean).join(" · ");
  }
  function tone(review) {
    if (/change|block|reject/i.test(review.verdict || "")) return "danger";
    const tones = review.counts.map((c) => c.tone);
    return tones.includes("danger")
      ? "danger"
      : tones.includes("attention")
        ? "attention"
        : "success";
  }
  function kind(review) {
    return review.source === "sweep" ? "Sweep review" : "pr-review";
  }
  // The head moved on since the review: matched by prefix, as reviews keep short shas.
  function stale(page) {
    const reviewed = page.review?.head;
    return Boolean(reviewed && page.head && !page.head.startsWith(reviewed));
  }
  function description(page) {
    const review = page.review;
    return [
      `${kind(review)} ${review.label || ""}, ${review.date || ""}: ${summary(review)}`,
      stale(page) ? "The pull request has new commits since this review." : "",
      ...review.findings.map((finding) => `${finding.severity}: ${finding.title}`),
      review.note || "",
    ]
      .filter(Boolean)
      .join("\n");
  }
  function openButton(page, className) {
    const anchor = link(page.workspace ? "Open workspace" : "Open in Babysitter", page.dashboard);
    anchor.className = className;
    anchor.title = page.workspace
      ? "Show this pull request and its agent's conversation in Babysitter"
      : "Show this item in Babysitter";
    return anchor;
  }
  // What a node was drawn from, short: the page's own scripts can read the DOM.
  function digest(value) {
    let hash = 5381;
    for (const ch of JSON.stringify(value)) hash = ((hash * 33) ^ ch.charCodeAt(0)) >>> 0;
    return hash.toString(36);
  }
  function shown(page) {
    return page && (page.review || page.dashboard || page.workspaces?.length);
  }

  // One line beside a list row's title.
  function decorate(anchor, url, page) {
    const next = anchor.nextElementSibling;
    if (next?.classList.contains("babysit-chips")) {
      if (next.dataset.url === url && next.dataset.state === digest(page)) return;
      next.remove();
    }
    if (!shown(page)) return;
    const chips = node("span", undefined, "babysit-chips");
    chips.dataset.url = url;
    chips.dataset.state = digest(page);
    if (page.review) {
      const review = page.review;
      const text = `Review${stale(page) ? " (old head)" : ""}: ${summary(review)}`;
      const chip = review.links?.report ? link(text, review.links.report) : node("span", text);
      chip.className = `babysit-chip babysit-${stale(page) ? "muted" : tone(review)}`;
      chip.title = description(page);
      chips.append(chip);
    }
    if (page.workspaces?.length) {
      const count = node(
        "span",
        `${page.workspaces.length} workspace${page.workspaces.length === 1 ? "" : "s"}`,
        "babysit-chip",
      );
      count.title = page.workspaces
        .map((w) => [w.name, w.machine, w.status].filter(Boolean).join(" · "))
        .join("\n");
      chips.append(count);
    }
    if (page.dashboard) chips.append(openButton(page, "babysit-chip babysit-open"));
    anchor.after(chips);
  }

  function workspaceList(page) {
    const list = node("ul", undefined, "babysit-workspaces");
    for (const workspace of page.workspaces) {
      const item = node("li");
      const name = workspace.dashboard
        ? link(workspace.name || workspace.branch || "Checkout", workspace.dashboard)
        : node("span", workspace.name || workspace.branch || "Checkout");
      name.title = workspace.branch ? `Branch ${workspace.branch}` : "";
      item.append(name);
      const meta = [workspace.machine, workspace.status].filter(Boolean).join(" · ");
      if (meta) item.append(" ", node("span", meta, "babysit-muted"));
      if (workspace.collie) item.append(" ", link("Collie", workspace.collie, "babysit-small"));
      list.append(item);
    }
    return list;
  }

  // The item's own page: a card in its sidebar, or in a corner without one. It always
  // shows, so a page Babysitter knows nothing about says so.
  function card(url, page) {
    const sidebar = document.querySelector("#partial-discussion-sidebar");
    let box = document.querySelector(".babysit-card");
    const state = digest([url, page, error, Boolean(sidebar)]);
    if (box?.dataset.state === state) return;
    box?.remove();
    box = node("section", undefined, `babysit-card${sidebar ? " babysit-in-sidebar" : ""}`);
    box.dataset.state = state;
    box.setAttribute("aria-label", "Babysitter");
    const heading = node("div", undefined, "babysit-heading");
    heading.append(node("strong", "Babysitter"));
    if (!sidebar) {
      const close = node("button", "×", "babysit-close");
      close.type = "button";
      close.title = "Hide until the next page";
      close.setAttribute("aria-label", "Hide Babysitter");
      close.onclick = () => box.remove();
      heading.append(close);
    }
    box.append(heading);
    if (error) box.append(node("p", error, "babysit-muted"));
    const review = page?.review;
    if (review) {
      const line = node("p");
      line.append(node("span", summary(review), `babysit-chip babysit-${tone(review)}`));
      line.append(
        " ",
        node(
          "span",
          `${kind(review)} ${review.label || ""}, ${review.date || ""}` +
            (review.head ? ` at ${review.head.slice(0, 9)}` : ""),
          "babysit-muted",
        ),
      );
      box.append(line);
      if (stale(page))
        box.append(
          node("p", "The pull request has new commits since this review.", "babysit-warning"),
        );
      if (review.findings.length) {
        const list = node("ul", undefined, "babysit-findings");
        for (const finding of review.findings.slice(0, 8)) {
          const item = node("li");
          item.append(
            node("span", finding.severity, `babysit-priority babysit-${finding.tone}`),
            " ",
            finding.title,
          );
          list.append(item);
        }
        if (review.findings.length > 8)
          list.append(node("li", `and ${review.findings.length - 8} more`, "babysit-muted"));
        box.append(list);
      }
      if (review.note) box.append(node("p", review.note, "babysit-muted"));
      const links = node("p", undefined, "babysit-links");
      if (review.links?.report) links.append(link("Report", review.links.report));
      if (review.links?.reply) links.append(link("Draft reply", review.links.reply));
      if (links.childElementCount) box.append(links);
    } else if (page?.kind !== "issue" && !error) {
      box.append(node("p", "No agent review found.", "babysit-muted"));
    }
    if (page?.workspaces?.length) {
      box.append(node("p", "Workspaces", "babysit-subheading"));
      box.append(workspaceList(page));
    } else if (page && !error) {
      box.append(node("p", "No linked workspace.", "babysit-muted"));
    }
    if (page && !page.dashboard && !error)
      box.append(node("p", "Not in your dashboard's overview.", "babysit-muted"));
    if (page?.dashboard) box.append(openButton(page, "babysit-button"));
    if (sidebar) sidebar.prepend(box);
    else document.body.append(box);
  }

  function ask(urls) {
    const now = Date.now();
    const wanted = urls.filter(
      (url) => !asking.has(url) && !(now - (known.get(url)?.at || 0) < FRESH),
    );
    if (!wanted.length) return;
    for (const url of wanted) asking.add(url);
    chrome.runtime.sendMessage({ type: "github-pages", urls: wanted }, (answer) => {
      for (const url of wanted) asking.delete(url);
      if (chrome.runtime.lastError || !answer || answer.error) {
        error = `Babysitter: ${answer?.error || chrome.runtime.lastError?.message || "no answer"}`;
        // Asked again after FRESH, not on every page change.
        for (const url of wanted) known.set(url, { at: Date.now(), page: null });
      } else {
        error = null;
        for (const url of wanted)
          known.set(url, { at: Date.now(), page: answer.pages[url] || null });
      }
      render();
    });
  }

  function render() {
    const own = ITEM.exec(location.pathname) ? itemURL(location.href) : null;
    if (own) {
      ask([own]);
      if (known.has(own)) card(own, known.get(own).page);
      return;
    }
    document.querySelector(".babysit-card")?.remove();
    if (!LIST.test(location.pathname)) return;
    const anchors = [...document.querySelectorAll(TITLES)]
      .map((anchor) => [anchor, itemURL(anchor.href)])
      .filter(([, url]) => url);
    ask([...new Set(anchors.map(([, url]) => url))].slice(0, 100));
    for (const [anchor, url] of anchors) {
      const page = known.get(url)?.page;
      if (page) decorate(anchor, url, page);
    }
  }

  // GitHub swaps pages in place; one look per burst of changes.
  let timer = null;
  new MutationObserver(() => {
    clearTimeout(timer);
    timer = setTimeout(render, 300);
  }).observe(document.documentElement, { childList: true, subtree: true });
  render();
})();
