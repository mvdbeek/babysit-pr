// Registered on github.com only after you turn it on in Settings. It finds the pull
// requests a page shows, asks the extension's background (which holds the pairing token)
// what Babysitter knows about them, and shows that as text. Nothing is sent to GitHub.
(() => {
  if (window.babysitGitHub) return;
  window.babysitGitHub = true;
  const PULL = /^\/([^/]+)\/([^/]+)\/pull\/(\d+)(?:\/.*)?$/;
  const LIST = /^(?:\/[^/]+\/[^/]+)?\/pulls\/?$/;
  // A list's title links: the classic list, and the React one.
  const TITLES = 'a[id^="issue_"][id$="_link"], a[data-testid="issue-pr-title-link"]';
  const FRESH = 60 * 1000;
  const known = new Map();
  const asking = new Set();
  let error = null;

  function pullURL(href) {
    try {
      const url = new URL(href, location.href);
      const match = url.origin === "https://github.com" && PULL.exec(url.pathname);
      return match ? `https://github.com/${match[1]}/${match[2]}/pull/${match[3]}` : null;
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
    const parts = ["P1", "P2", "P3"]
      .filter((priority) => review.counts[priority])
      .map((priority) => `${review.counts[priority]} ${priority}`);
    return parts.length ? parts.join(" · ") : "no findings";
  }
  function tone(review) {
    return review.counts.P1 ? "danger" : review.counts.P2 ? "attention" : "success";
  }
  function roundLabel(review) {
    return `r${review.round}${review.trim ? ` trim ${review.trim}` : ""}`;
  }
  function description(review) {
    return [
      `pr-review ${roundLabel(review)}, ${review.date}: ${counts(review)}`,
      ...review.findings.map((finding) => `${finding.priority}: ${finding.title}`),
      review.risk || "",
    ]
      .filter(Boolean)
      .join("\n");
  }
  function openButton(page, className) {
    const anchor = link(page.workspace ? "Open workspace" : "Open in Babysitter", page.dashboard);
    anchor.className = className;
    anchor.title = page.workspace
      ? "Show this pull request and its agent's conversation in Babysitter"
      : "Show this pull request in Babysitter";
    return anchor;
  }

  // One line beside a list row's title.
  function decorate(anchor, url, page) {
    const next = anchor.nextElementSibling;
    if (next?.classList.contains("babysit-chips")) {
      if (next.dataset.url === url && next.dataset.state === JSON.stringify(page)) return;
      next.remove();
    }
    if (!page.review && !page.dashboard) return;
    const chips = node("span", undefined, "babysit-chips");
    chips.dataset.url = url;
    chips.dataset.state = JSON.stringify(page);
    if (page.review) {
      const review = page.review;
      const chip = review.links?.verified
        ? link(`Review ${roundLabel(review)}: ${counts(review)}`, review.links.verified)
        : node("span", `Review ${roundLabel(review)}: ${counts(review)}`);
      chip.className = `babysit-chip babysit-${tone(review)}`;
      chip.title = description(review);
      chips.append(chip);
    }
    if (page.dashboard) chips.append(openButton(page, "babysit-chip babysit-open"));
    anchor.after(chips);
  }

  // The pull request's own page: a card in its sidebar, or in a corner without one.
  function card(url, page) {
    const sidebar = document.querySelector("#partial-discussion-sidebar");
    let box = document.querySelector(".babysit-card");
    const state = JSON.stringify([url, page, error, Boolean(sidebar)]);
    if (box?.dataset.state === state) return;
    box?.remove();
    if (!page?.review && !page?.dashboard && !error) return;
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
      line.append(node("span", counts(review), `babysit-chip babysit-${tone(review)}`));
      line.append(
        " ",
        node(
          "span",
          `pr-review ${roundLabel(review)}, ${review.date}` +
            (review.commit ? ` at ${review.commit.slice(0, 9)}` : ""),
          "babysit-muted",
        ),
      );
      box.append(line);
      if (review.findings.length) {
        const list = node("ul", undefined, "babysit-findings");
        for (const finding of review.findings) {
          const item = node("li");
          item.append(
            node("span", finding.priority, `babysit-priority babysit-${finding.priority}`),
            " ",
            finding.title,
          );
          list.append(item);
        }
        box.append(list);
      }
      if (review.risk) box.append(node("p", review.risk, "babysit-muted"));
      const links = node("p", undefined, "babysit-links");
      if (review.links?.verified) links.append(link("Findings", review.links.verified));
      if (review.links?.reply) links.append(link("Draft reply", review.links.reply));
      if (links.childElementCount) box.append(links);
    } else if (page) box.append(node("p", "No pr-review round found.", "babysit-muted"));
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
    const own = PULL.exec(location.pathname) ? pullURL(location.href) : null;
    if (own) {
      ask([own]);
      if (known.has(own)) card(own, known.get(own).page);
      return;
    }
    document.querySelector(".babysit-card")?.remove();
    if (!LIST.test(location.pathname)) return;
    const anchors = [...document.querySelectorAll(TITLES)]
      .map((anchor) => [anchor, pullURL(anchor.href)])
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
