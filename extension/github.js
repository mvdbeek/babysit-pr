// Registered on github.com only after you turn it on in Settings. It finds the pull
// requests and issues a page shows, asks the extension's background (which holds the
// pairing token) what Babysitter knows about them, and shows that as text. An issue's card
// can start an agent on it, and a pull request's can start a review of it or ask
// Babysitter about it. Nothing is sent to GitHub.
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
  // Each item page's forms and conversation, kept while GitHub redraws the page around them.
  const tools = new Map();
  let error = null;
  // A newer extension on the dashboard: { version, download }.
  let update = null;
  // The item whose corner card was closed, until another page.
  let hidden = null;

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
  function send(message) {
    return new Promise((resolve, reject) =>
      chrome.runtime.sendMessage(message, (answer) => {
        if (chrome.runtime.lastError) reject(Error(chrome.runtime.lastError.message));
        else if (!answer || answer.error) reject(Error(answer?.error || "No answer"));
        else resolve(answer);
      }),
    );
  }
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  // The card's buttons take their clicks on the window, in the capture phase, before any
  // listener of the page's: a page that stops clicks on their way down would otherwise
  // leave an onclick deaf.
  const presses = new WeakMap();
  window.addEventListener(
    "click",
    (event) => {
      const target = event.target instanceof Element ? event.target.closest("button") : null;
      const handler = target && presses.get(target);
      if (handler && !target.disabled) handler(event);
    },
    true,
  );
  function onPress(element, handler) {
    presses.set(element, handler);
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

  // What Babysitter knows about the item, redrawn whenever that changes.
  function details(page) {
    const parts = [];
    if (error) parts.push(node("p", error, "babysit-muted"));
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
      parts.push(line);
      if (stale(page))
        parts.push(
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
        parts.push(list);
      }
      if (review.note) parts.push(node("p", review.note, "babysit-muted"));
      const links = node("p", undefined, "babysit-links");
      if (review.links?.report) links.append(link("Report", review.links.report));
      if (review.links?.reply) links.append(link("Draft reply", review.links.reply));
      if (links.childElementCount) parts.push(links);
    } else if (page?.kind !== "issue" && !error) {
      parts.push(node("p", "No agent review found.", "babysit-muted"));
    }
    if (page?.workspaces?.length) {
      parts.push(node("p", "Workspaces", "babysit-subheading"), workspaceList(page));
    } else if (page && !error) {
      parts.push(node("p", "No linked workspace.", "babysit-muted"));
    }
    if (page && !page.dashboard && !error)
      parts.push(node("p", "Not in your dashboard's overview.", "babysit-muted"));
    if (page?.dashboard) parts.push(openButton(page, "babysit-button"));
    if (update) {
      const notice = node(
        "p",
        `Extension ${update.version} is on your dashboard: `,
        "babysit-small",
      );
      notice.append(
        link("download it", update.download),
        ", unzip it over this one and reload it.",
      );
      parts.push(notice);
    }
    return parts;
  }

  // The item's own page: a card in its sidebar, or in a corner without one. It always
  // shows, so a page Babysitter knows nothing about says so.
  function card(url, page) {
    if (hidden === url) return;
    const sidebar = document.querySelector("#partial-discussion-sidebar");
    let box = document.querySelector(".babysit-card");
    if (box && (box.dataset.url !== url || box.parentElement !== (sidebar || document.body))) {
      box.remove();
      box = null;
    }
    if (!box) {
      box = node("section", undefined, `babysit-card${sidebar ? " babysit-in-sidebar" : ""}`);
      box.dataset.url = url;
      box.setAttribute("aria-label", "Babysitter");
      const heading = node("div", undefined, "babysit-heading");
      heading.append(node("strong", "Babysitter"));
      if (!sidebar) {
        const close = node("button", "×", "babysit-close");
        close.type = "button";
        close.title = "Hide until the next page";
        close.setAttribute("aria-label", "Hide Babysitter");
        onPress(close, () => {
          hidden = url;
          box.remove();
        });
        heading.append(close);
      }
      box.append(heading, node("div", undefined, "babysit-details"));
      if (sidebar) sidebar.prepend(box);
      else document.body.append(box);
    }
    const shown = box.querySelector(".babysit-details");
    const state = digest([page, error, update]);
    if (shown.dataset.state !== state) {
      shown.dataset.state = state;
      shown.replaceChildren(...details(page));
    }
    if (!tools.has(url))
      tools.set(url, url.includes("/pull/") ? pullTools(url) : launchTools(url, "issue"));
    const own = tools.get(url);
    if (own.parentElement !== box) box.append(own);
  }

  function textarea(placeholder, rows) {
    const box = node("textarea", undefined, "babysit-input");
    box.rows = rows;
    box.placeholder = placeholder;
    box.setAttribute("aria-label", placeholder);
    return box;
  }
  function button(text, className = "babysit-button") {
    const element = node("button", text, className);
    element.type = "button";
    return element;
  }

  // What a card's start button launches: an issue's task, or a pull request's review.
  const LAUNCHES = {
    issue: {
      type: "github-handle",
      heading: "Handle this issue",
      button: "Handle issue",
      usual: "Usual agent",
      rows: 3,
    },
    pull: {
      type: "github-review",
      heading: "Review this pull request",
      button: "Review pull request",
      usual: "Main reviewer",
      rows: 2,
    },
  };

  // Start an agent on the item with the dashboard's task for it (an issue's Handle issue,
  // a pull request's Review pull request), plus whatever the box adds.
  function launchTools(url, kind) {
    const launch = LAUNCHES[kind];
    const form = node("form", undefined, "babysit-tools");
    const instructions = textarea("Extra instructions (optional)", launch.rows);
    const agent = node("select", undefined, "babysit-select");
    agent.setAttribute("aria-label", "Agent");
    for (const [value, label] of [
      ["", launch.usual],
      ["codex", "Codex"],
      ["claude", "Claude"],
    ]) {
      const option = node("option", label);
      option.value = value;
      agent.append(option);
    }
    const start = node("button", launch.button, "babysit-button babysit-primary");
    start.type = "submit";
    const status = node("p", undefined, "babysit-muted babysit-status");
    status.setAttribute("role", "status");
    const row = node("div", undefined, "babysit-row");
    row.append(agent, start);
    form.append(node("p", launch.heading, "babysit-subheading"), instructions, row, status);
    instructions.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) form.requestSubmit();
    });

    async function follow(operation) {
      for (;;) {
        status.replaceChildren(
          operation.status === "complete"
            ? operation.message || "Started"
            : `${operation.status}: ${operation.message || "accepted"}`,
        );
        if (operation.result?.url)
          status.append(" ", link("Open in Collie", operation.result.url, "babysit-small"));
        // A launch waiting for a machine with room may wait for hours: the dashboard shows it.
        if (!["queued", "running"].includes(operation.status)) return operation;
        await sleep(2000);
        operation = (await send({ type: "github-status", id: operation.id })).operation;
      }
    }
    form.onsubmit = async (event) => {
      event.preventDefault();
      start.disabled = true;
      status.textContent = "Starting…";
      try {
        const answer = await send({
          type: launch.type,
          url,
          instructions: instructions.value,
          agent: agent.value,
        });
        if (!answer.operation) status.textContent = "Accepted; the dashboard shows its progress.";
        else if ((await follow(answer.operation)).status !== "failed") instructions.value = "";
      } catch (failure) {
        status.textContent = failure.message;
      } finally {
        start.disabled = false;
      }
    };
    return form;
  }

  // An answer's markdown, as text nodes only: paragraphs, headings, lists, code.
  function inline(parent, text) {
    for (const part of text.split(/(`[^`]+`|\*\*[^*]+\*\*)/)) {
      if (/^`[^`]+`$/.test(part)) parent.append(node("code", part.slice(1, -1)));
      else if (/^\*\*[^*]+\*\*$/.test(part)) parent.append(node("strong", part.slice(2, -2)));
      else if (part) parent.append(part);
    }
  }
  function markdown(text) {
    const root = node("div", undefined, "babysit-answer");
    let list = null;
    let paragraph = null;
    let code = null;
    for (const line of text.split("\n")) {
      if (code) {
        if (/^\s*```/.test(line)) code = null;
        else code.textContent += `${line}\n`;
        continue;
      }
      const item = /^\s*([-*]|\d+\.)\s+(.*)$/.exec(line);
      const heading = /^#{1,6}\s+(.*)$/.exec(line);
      if (/^\s*```/.test(line)) {
        const pre = node("pre");
        code = node("code");
        pre.append(code);
        root.append(pre);
      } else if (heading) {
        const element = node("p", undefined, "babysit-answer-heading");
        inline(element, heading[1]);
        root.append(element);
      } else if (item) {
        if (!list) root.append((list = node(/\d/.test(item[1]) ? "ol" : "ul")));
        const element = node("li");
        inline(element, item[2]);
        list.append(element);
        paragraph = null;
        continue;
      } else if (line.trim()) {
        if (paragraph) paragraph.append(document.createElement("br"));
        else root.append((paragraph = node("p")));
        inline(paragraph, line);
        list = null;
        continue;
      }
      list = paragraph = null;
    }
    return root;
  }

  // A pull request's card: questions about it, each answered by an agent the dashboard
  // starts, streamed in as it writes. Earlier answers go along as the conversation.
  function askTools(url) {
    const section = node("div", undefined, "babysit-tools");
    const conversation = node("div", undefined, "babysit-conversation");
    const form = node("form");
    const question = textarea("Ask about this pull request", 2);
    const ask = node("button", "Ask", "babysit-button babysit-primary");
    ask.type = "submit";
    const stop = button("Stop");
    const clear = button("Clear");
    stop.hidden = clear.hidden = true;
    const row = node("div", undefined, "babysit-row");
    row.append(clear, stop, ask);
    form.append(question, row);
    section.append(node("p", "Ask about this pull request", "babysit-subheading"));
    section.append(conversation, form);
    const turns = [];
    let current = null;

    function draw(turn) {
      const parts = [node("p", turn.question, "babysit-question")];
      if (turn.answer) parts.push(markdown(turn.answer));
      if (turn.failure) parts.push(node("p", turn.failure, "babysit-warning"));
      else if (["pending", "running"].includes(turn.status))
        parts.push(
          node(
            "p",
            turn.activity?.at(-1) || (turn.status === "pending" ? "Waiting…" : "Thinking…"),
            "babysit-muted babysit-activity",
          ),
        );
      turn.element.replaceChildren(...parts);
    }
    function idle() {
      current = null;
      ask.disabled = false;
      stop.hidden = true;
      clear.hidden = !turns.length;
    }
    question.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
        event.preventDefault();
        form.requestSubmit();
      }
    });
    onPress(clear, () => {
      turns.length = 0;
      conversation.replaceChildren();
      idle();
    });
    onPress(stop, () => {
      if (current?.id) void send({ type: "github-ask-cancel", id: current.id }).catch(() => {});
    });
    form.onsubmit = async (event) => {
      event.preventDefault();
      const text = question.value.trim();
      if (!text || current) return;
      const history = turns
        .filter((turn) => turn.status === "done")
        .map((turn) => ({ question: turn.question, answer: turn.answer }));
      const turn = { question: text, answer: "", status: "pending", element: node("div") };
      turn.element.className = "babysit-turn";
      turns.push(turn);
      conversation.append(turn.element);
      current = turn;
      ask.disabled = true;
      clear.hidden = true;
      question.value = "";
      draw(turn);
      try {
        turn.id = (await send({ type: "github-ask", url, question: text, history })).id;
        stop.hidden = false;
        while (["pending", "running"].includes(turn.status)) {
          await sleep(700);
          Object.assign(turn, await send({ type: "github-answer", id: turn.id }));
          draw(turn);
          turn.element.scrollIntoView({ block: "nearest" });
        }
      } catch (failure) {
        Object.assign(turn, { status: "error", failure: failure.message });
        if (!turn.id) question.value = text;
        draw(turn);
      } finally {
        idle();
      }
    };
    return section;
  }

  // A pull request's card: start a review of it, and ask about it.
  function pullTools(url) {
    const box = node("div");
    box.append(launchTools(url, "pull"), askTools(url));
    return box;
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
        update = answer.update || null;
        for (const url of wanted)
          known.set(url, { at: Date.now(), page: answer.pages[url] || null });
      }
      render();
    });
  }

  function render() {
    const own = ITEM.exec(location.pathname) ? itemURL(location.href) : null;
    if (hidden !== own) hidden = null;
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
