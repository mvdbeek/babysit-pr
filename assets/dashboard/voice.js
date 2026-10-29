/* Voice assistant: ask or tell the dashboard anything in your own words ("walk me through
   what needs me"). Claude answers from the supervisor, aloud, and acts on the page through
   the commands below; anything that changes something waits for the user's own "confirm".
   The field takes the same requests typed, which is also the way in where the browser has
   no speech recognition (or a phone keyboard's own microphone dictates them). */
(() => {
  const byId = (id) => document.getElementById(id);
  const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  const PREFS_KEY = "babysit-pr:voice:v1";
  const bar = byId("voice-bar");
  const input = byId("voice-input");
  // Spoken names of each tab, longest first so "pull requests" wins over "pull".
  const PAGES = {
    attention: ["needs you", "attention", "needs me", "inbox", "home"],
    watcher: ["watcher", "watches", "watch list"],
    prs: ["pull requests", "pull request", "prs", "pr list", "pulls"],
    issues: ["issues", "issue list"],
    scheduled: ["scheduled tasks", "scheduled", "schedule"],
    cron: ["cron jobs", "cron job", "cron", "crons", "jobs"],
    workspaces: ["workspaces", "workspace list", "work spaces"],
    upstream: ["upstream tests", "upstream", "experiments"],
    sentry: ["sentry issues", "sentry", "errors"],
  };
  // Header buttons that open a dialog, by what one would call it.
  const DIALOGS = [
    [/^(notifications?|updates|bell|recent updates)$/, "notifications-toggle"],
    [/^(settings|preferences|options)$/, "settings-toggle"],
    [/^(usage|quota|subscription usage|limits)$/, "usage-toggle"],
    [/^(services|service status|health|daemon)$/, "health"],
  ];
  // Presses that only change what the page shows go straight through; every other button
  // can start, stop, send or remove something, and waits for "confirm" first.
  const SAFE =
    /^(refresh|reload|close|×|back|next|previous|prev|more|fewer|less|details|filters|help|diff|transcript|full screen|done|ascending|descending|github|open in collie|more actions|(show|hide|load|sort by|select|deselect|clear|pin|unpin|expand|collapse|minimi[sz]e|restore|close|copy|previous|next|earlier|later|show in) .*)$/;
  // Names of safe-sounding buttons whose title says otherwise.
  const DESTRUCTIVE = /\b(remove|delete|clean ?up|stop|quit|cancel|discard|publish|approve)\b/;
  // Said to the bar itself, never to the assistant: only the user confirms.
  const CONFIRM = /^(yes|yes please|confirm|confirmed|do it|go ahead|ok|okay|send it)$/;
  const CANCEL = /^(no|no thanks|cancel|cancel that|never ?mind|forget it|dont|stop)$/;
  const STOP = /^(stop|stop listening|stop voice|mic off|microphone off|go to sleep|be quiet)$/;
  // Said while a reply is read, at the end of what the microphone heard: stops the reading.
  const HUSH = /(?:^|\s)(stop( reading| talking)?|quiet|be quiet|enough|thats enough|shut up)$/;
  const RESTART = /^(start over|new conversation|forget (all )?that|reset)$/;
  const HELP = /^(help|what can i say)$/;
  const NUMBERS = {
    zero: 0,
    oh: 0,
    one: 1,
    won: 1,
    two: 2,
    to: 2,
    too: 2,
    three: 3,
    four: 4,
    for: 4,
    five: 5,
    six: 6,
    seven: 7,
    eight: 8,
    ate: 8,
    nine: 9,
    ten: 10,
    eleven: 11,
    twelve: 12,
    thirteen: 13,
    fourteen: 14,
    fifteen: 15,
    sixteen: 16,
    seventeen: 17,
    eighteen: 18,
    nineteen: 19,
    twenty: 20,
    thirty: 30,
    forty: 40,
    fifty: 50,
    sixty: 60,
    seventy: 70,
    eighty: 80,
    ninety: 90,
  };
  const EXAMPLES = [
    [
      "Ask",
      [
        "Walk me through what needs me",
        "Why is CI failing on galaxy 1234?",
        "What did the agent say last?",
        "Any new reviews on my pull requests?",
      ],
    ],
    [
      "Act",
      [
        "Tell the agent to rebase on main",
        "Set this one aside",
        "Answer option 2",
        "Start a task to fix the flaky upload test",
      ],
    ],
    ["Get around", ["Next", "Show my pull requests with failing CI", "Open settings", "Close"]],
    ["Handled here", ["confirm", "cancel", "start over", "stop listening"]],
  ];
  const ACTIONABLE = [
    "button",
    "a[href]",
    "summary",
    "[role=tab]",
    "[role=button]",
    "[role=option]",
    "[role=menuitem]",
    "input[type=checkbox]",
    "input[type=radio]",
    "select",
  ].join(", ");
  // Turns of the conversation sent with each request, and how long it lasts unused.
  const HISTORY = 40;
  const FORGET_AFTER = 30 * 60 * 1000;
  const POLL_MS = 700;
  // Below the server's limit on the page's description (MAX_CONTEXT), with room to spare.
  const CONTEXT_LIMIT = 7500;
  // Longer than the supervisor may take: a minute to start, three to answer.
  const ANSWER_WITHIN = 250 * 1000;
  // A late “confirm” is told it came too late rather than sent to the assistant.
  const LATE = 2 * 60 * 1000;
  // How long a confirmation waits, counted from when its question has been read out.
  const CONFIRM_WITHIN = 45 * 1000;
  // A spoken request goes once the speaker has been quiet this long (the Pause choice):
  // the recognizer's own end comes at the first breath. Longer after a word that leaves
  // the sentence unfinished; short for the bar's own one-word answers.
  const PAUSES = { short: 1000, normal: 1600, long: 2600 };
  const RUN_ON_MS = 1500;
  const QUICK_MS = 700;
  const RUNS_ON =
    /\b(and|or|but|so|then|to|the|a|an|of|for|with|that|which|because|if|about|on|in|at|is|are|was|my|its|their|this|um|uh|er|erm|like)$/;

  let prefs = { continuous: false, speak: true, pause: "normal" };
  try {
    prefs = { ...prefs, ...JSON.parse(localStorage.getItem(PREFS_KEY) || "{}") };
  } catch {
    // Private windows and blocked storage keep the defaults.
  }
  function savePrefs() {
    try {
      localStorage.setItem(PREFS_KEY, JSON.stringify(prefs));
    } catch {
      // Not remembered; the choice still holds for this page.
    }
  }

  let recognition = null;
  let listening = false;
  // Listening was asked for and continues past the end of one recognition session.
  let wanted = false;
  // Counts presses of stop: a reply that finishes speaking after one does not listen again.
  let stops = 0;
  let quickEnds = 0;
  let startedAt = 0;
  let pending = null;
  let expiredAt = 0;
  let mutedUntil = 0;
  let speaking = null;
  // The last request was spoken, so a reply's reading listens for "stop".
  let voiced = false;
  let hushedAt = 0;
  // The reply last read aloud, whose echo the microphone may still be hearing.
  let lastReading = "";
  // Asked to listen while a session runs (a reading's): the next session takes over.
  let keepOn = false;
  // The session runs only to hear "stop" during a reading.
  let readingOnly = false;
  // Send, or drop, the request being spoken before its pause is over.
  let flush = null;
  let drop = null;
  // The conversation with the assistant: user and assistant turns, actions' outcomes.
  let conversation = [];
  // Names the conversation in the transcript, where the supervisor's part joins it.
  let conversationId = newConversation();
  let lastTurnAt = 0;
  // Counts requests: an answer to an older one, overtaken by a newer, is dropped.
  let generation = 0;
  let thinking = false;
  // The request the supervisor is answering, cancelled when a newer one overtakes it.
  let inFlight = null;

  // ---- Text --------------------------------------------------------------------------

  function normalize(text) {
    return (
      text
        .toLowerCase()
        // Recognizers group long numbers: "PR 19,876".
        .replace(/(\d),(?=\d{3}\b)/g, "$1")
        .replace(/[’']/g, "")
        .replace(/[^\p{L}\p{N}#/._-]+/gu, " ")
        .replace(/(^|\s)[._-]+|[._-]+(?=\s|$)/g, " ")
        .replace(/\s+/g, " ")
        .trim()
    );
  }
  // A number said as digits or words up to 999, or null.
  function numberFrom(text) {
    const said = normalize(text)
      .replace(/^(number |no |#)/, "")
      .split(" ");
    if (said.length === 1 && /^\d+$/.test(said[0])) return Number(said[0]);
    // "three hundred twenty one" adds up; "two three" is not one number.
    let total = 0;
    let last = null;
    for (const word of said) {
      if (word === "and" && last === "hundred") continue;
      if (word === "hundred") {
        if (!total || total >= 100) return null;
        total *= 100;
        last = "hundred";
        continue;
      }
      const value = NUMBERS[word];
      if (value === undefined) return null;
      const kind = value >= 10 ? "tens" : "unit";
      if (last && last !== "hundred" && !(last === "tens" && kind === "unit" && total % 10 === 0))
        return null;
      if (last === "tens" && total % 100 < 20 && total % 100 >= 10) return null;
      total += value;
      last = kind;
    }
    return last ? total : null;
  }
  const FILLER = new Set(["the", "a", "an", "button", "link", "tab", "on", "please"]);
  function words(text) {
    return normalize(text)
      .split(" ")
      .filter((word) => word && !FILLER.has(word));
  }
  // How well `query` names an element called `name`: 1 for every word, less for some.
  function score(query, name) {
    const asked = words(query);
    const named = words(name);
    if (!asked.length || !named.length) return 0;
    if (asked.join(" ") === named.join(" ")) return 1;
    // The same word, or forms of it: "fail", "failing" and "failed" all name "Failed".
    const close = (a, b) => {
      if (a === b) return true;
      let common = 0;
      while (common < a.length && a[common] === b[common]) common += 1;
      return common >= 4 && common >= Math.min(a.length, b.length) - 3;
    };
    const hits = asked.filter((word) => named.some((other) => close(word, other))).length;
    if (!hits) return 0;
    const recall = hits / asked.length;
    const precision = hits / named.length;
    return Math.min(0.99, 0.7 * recall + 0.3 * precision);
  }
  // A command that cannot do what it was asked: the assistant hears why next turn.
  function fail(text) {
    throw Error(text);
  }

  // ---- Page ---------------------------------------------------------------------------

  // The open modal dialog on top, whose contents are all that can be pressed meanwhile.
  function topDialog() {
    const open = [...document.querySelectorAll("dialog[open]")].filter((dialog) =>
      dialog.matches(":modal"),
    );
    return open[open.length - 1] || null;
  }
  function shown(element) {
    if (element.closest("[hidden], [inert], #voice-bar")) return false;
    if (!element.getClientRects().length) return false;
    return getComputedStyle(element).visibility !== "hidden";
  }
  function onScreen(element) {
    const box = element.getBoundingClientRect();
    return box.bottom > 0 && box.right > 0 && box.top < innerHeight && box.left < innerWidth;
  }
  function textOf(element) {
    return (element?.textContent || "").replace(/\s+/g, " ").trim();
  }
  function nameOf(element) {
    const label = element.getAttribute("aria-label");
    if (label) return label;
    const by = element.getAttribute("aria-labelledby");
    if (by)
      return by
        .split(" ")
        .map((id) => textOf(byId(id)))
        .join(" ");
    if (element.labels?.length) return [...element.labels].map(textOf).join(" ");
    return textOf(element) || element.value || element.title || "";
  }
  function actionable(root = topDialog() || document) {
    return [...root.querySelectorAll(ACTIONABLE)].filter(
      (element) => shown(element) && !element.disabled,
    );
  }
  // The best-named elements for `query`: one when it is clear, several when they tie.
  function named(query, elements = actionable()) {
    const scored = elements
      .map((element) => {
        const names = [nameOf(element), element.title].filter(Boolean);
        return { element, score: Math.max(...names.map((name) => score(query, name)), 0) };
      })
      .filter((entry) => entry.score >= 0.7)
      .sort((a, b) => b.score - a.score);
    if (!scored.length) return [];
    const best = scored[0].score;
    // Prefer what is in view; the same button scrolled away is rarely the one meant.
    const ties = scored.filter((entry) => best - entry.score < 0.05).map((entry) => entry.element);
    const visible = ties.filter(onScreen);
    return visible.length ? visible : ties;
  }
  function press(element) {
    if (element.tagName === "SELECT") {
      element.focus();
      return `${nameOf(element) || "That list"} is focused; choose with “filter … to …”.`;
    }
    element.scrollIntoView({ block: "nearest", inline: "nearest" });
    element.click();
    if (element.matches("input[type=checkbox]"))
      return `${element.checked ? "Checked" : "Unchecked"} ${nameOf(element)}.`;
    return `Pressed ${nameOf(element) || "it"}.`;
  }
  // Tabs, links, disclosures and form choices only change the page; buttons by their name.
  function harmless(element) {
    // An agent's question or dialog takes any of its answers at once.
    if (element.closest(".ws-answer, .ws-choices")) return false;
    const name = normalize(nameOf(element));
    // Settings save for every device as they change: only finding one's way there is.
    if (element.closest("#settings-dialog"))
      return (
        element.matches("[role=tab], summary, a[href], select, [aria-expanded]") ||
        /^(close|×|cancel|done)$/.test(name)
      );
    // Opening a dialog, a disclosure or a list's options; sorting by a column.
    if (element.matches("[role=tab], a[href], summary, select, [data-sort]")) return true;
    if (element.matches("[aria-haspopup=dialog], [aria-expanded]")) return true;
    // A list's option picks a filter or a form's choice, which the form's own button sends.
    if (element.matches("[role=option], input[type=radio]")) return true;
    // Checkboxes can grant an agent Docker or mark something reviewed: only a row's
    // selection is harmless.
    if (element.matches("input")) return /^select /.test(name);
    // Cancel only when it dismisses a dialog (a scheduled task's Cancel cancels its launch).
    if (name === "cancel") return Boolean(element.closest("dialog"));
    return SAFE.test(name) && !DESTRUCTIVE.test(normalize(element.title));
  }
  // The item a row's button acts on, for a confirmation to name: "test/repo #12".
  function rowName(element) {
    const row = element.closest("tr, li, article, .card, [role=row]");
    const text = row?.innerText || "";
    return text.match(/[\w.-]+\/[\w.-]+ ?#\d+/)?.[0] || text.match(/#\d+/)?.[0] || "";
  }
  // What shows for `element`: a searchable list's picker stands in for its hidden select.
  function face(element) {
    return element.closest(".searchable-select") || element;
  }
  function usable(element) {
    return element.isConnected && shown(face(element)) && !element.disabled;
  }
  function clip(text, limit) {
    const plain = text.replace(/\s+/g, " ").trim();
    return plain.length > limit ? `${plain.slice(0, limit - 1)}…` : plain;
  }
  // Waits for the user's “confirm” before `run`, outlining `element` meanwhile. `undo`
  // puts back what was prepared for it when it is not confirmed.
  // `aloud`, when given, is a shorter `what` to read out; the bar shows all of it.
  function ask(what, element, run, undo = () => {}, aloud = null) {
    dropPending("replaced by another action");
    expiredAt = 0;
    face(element).scrollIntoView({ block: "nearest", inline: "nearest" });
    face(element).classList.add("voice-pending");
    pending = {
      what,
      aloud: aloud && `Say confirm to ${aloud}, or cancel.`,
      element,
      // Counted again from the end of its reading, which can take a while.
      until: Date.now() + CONFIRM_WITHIN,
      // The page refreshes underneath: the control may have gone or changed meanwhile.
      run: () => (usable(element) ? run() : fail(`Cannot ${what}: it is no longer available.`)),
      undo,
      // Tells the conversation what became of it.
      settle: () => {},
    };
    return `Say “confirm” to ${what}, or “cancel”.`;
  }
  // The text a form's button would send, for its confirmation to read out.
  function sending(element) {
    const area = (element.form || element.closest("dialog"))?.querySelector("textarea");
    return area?.value.trim() ? ` with “${clip(area.value, 160)}”` : "";
  }
  // Press, unless it does more than change the view: that waits for “confirm”.
  function pressChecked(element) {
    if (harmless(element)) return press(element);
    const subject = rowName(element);
    return ask(
      `press ${nameOf(element)}${subject ? ` for ${subject}` : ""}${sending(element)}`,
      element,
      () => press(element),
    );
  }
  function clearPending() {
    if (pending) face(pending.element).classList.remove("voice-pending");
    pending = null;
  }
  // Ends a confirmation that will not come: its preparation undone, the assistant told.
  function dropPending(outcome) {
    if (!pending) return;
    record("confirmation", { what: pending.what, outcome });
    pending.settle(outcome);
    pending.undo();
    clearPending();
  }
  // A checkbox already as "check" or "uncheck" asked is left alone.
  function pressOnce(element, checked) {
    if (checked !== null && element.matches("input[type=checkbox]") && element.checked === checked)
      return `${nameOf(element)} is already ${checked ? "checked" : "unchecked"}.`;
    return pressChecked(element);
  }
  function choices(found, describe) {
    const names = [...new Set(found.map(describe).filter(Boolean))];
    return names.length > 1 ? `: ${names.slice(0, 6).join("; ")}` : "";
  }
  // `checked`: true or false when the words were "check" or "uncheck".
  function click(query, { exact = false, checked = null } = {}) {
    const found = named(query).filter((element) => !exact || score(query, nameOf(element)) === 1);
    if (!found.length) return null;
    if (found.length === 1) return () => pressOnce(found[0], checked);
    return () =>
      fail(
        `${found.length} controls match “${query}”${choices(found, (element) => [nameOf(element), rowName(element)].filter(Boolean).join(" for "))}. Name the row.`,
      );
  }
  // An action inside the row that mentions `target`: "transcript for 1234".
  // `checked` as for click().
  function rowAction(action, target, checked = null) {
    // "1234", "galaxy 1234" or "twelve": a number, and words its row must also show.
    const tail = target.match(/^(.*?)\s*#?(\d+)$/);
    const number = tail ? Number(tail[2]) : numberFrom(target);
    const rest = tail ? words(tail[1]) : number === null ? words(target) : [];
    const matches = (text) => {
      const plain = normalize(text);
      if (number !== null && !new RegExp(`#${number}\\b`).test(plain)) return false;
      return rest.every((word) => plain.includes(word));
    };
    const candidates = actionable().filter((element) => {
      if (score(action, nameOf(element)) < 0.7) return false;
      // Only a real row: anything wider would name every item on the page.
      const row = element.closest("tr, li, article, .card, [role=row]");
      // innerText, which spaces table cells apart: "#8" then "CI", not "#8CI".
      return row && matches(row.innerText);
    });
    if (!candidates.length) return null;
    if (candidates.length === 1) return () => pressOnce(candidates[0], checked);
    return () =>
      fail(`${candidates.length} rows match “${target}”${choices(candidates, rowName)}.`);
  }

  // ---- Fields --------------------------------------------------------------------------

  function messageBox() {
    const form = byId("ws-message");
    return byId("ws-viewer").open && !form.hidden && shown(byId("ws-message-text"))
      ? byId("ws-message-text")
      : null;
  }
  function fire(field) {
    field.dispatchEvent(new Event("input", { bubbles: true }));
  }
  function setField(field, text) {
    field.value = text;
    field.setSelectionRange?.(text.length, text.length);
    fire(field);
  }
  // The search box of whatever is showing.
  function searchField() {
    const root = topDialog() || document;
    return (
      [...root.querySelectorAll('input[type="search"], input[id$="search"]')].find(shown) || null
    );
  }
  // Filter and sort lists of whatever is showing, the searchable ones included.
  function lists() {
    const root = topDialog() || document;
    return [...root.querySelectorAll("select")].filter((select) =>
      shown(select.closest(".searchable-select") || select),
    );
  }
  function listName(select) {
    const wrapper = select.closest(".searchable-select");
    const field = wrapper?.querySelector("input[role=combobox]");
    return (
      select.getAttribute("aria-label") ||
      field?.getAttribute("aria-label") ||
      [...select.labels].map(textOf).join(" ") ||
      select.id.replace(/-/g, " ")
    );
  }
  function choose(select, option) {
    select.value = option.value;
    select.dispatchEvent(new Event("change", { bubbles: true }));
    select.dispatchEvent(new Event("input", { bubbles: true }));
    window.dashboardAgents?.sync(select);
    return `${listName(select)}: ${option.label}.`;
  }
  // "filter CI to failing", or just "filter failing" when only one list has that option.
  function filter(listQuery, optionQuery) {
    const candidates = lists()
      .map((select) => {
        const fit = listQuery ? score(listQuery, listName(select)) : 1;
        const options = [...select.options].filter((option) => !option.disabled && !option.hidden);
        const best = options
          .map((option) => ({ option, score: score(optionQuery, option.label || option.text) }))
          .sort((a, b) => b.score - a.score)[0];
        return best && best.score >= 0.7 && fit >= 0.5
          ? { select, option: best.option, score: fit + best.score }
          : null;
      })
      .filter(Boolean)
      .sort((a, b) => b.score - a.score);
    if (!candidates.length) return null;
    const { select, option } = candidates[0];
    // Settings save as soon as they change, for every device; the message box's picks
    // whom a message goes to.
    if (select.closest("#settings-dialog, #ws-message") && select.id !== "settings-task")
      return () =>
        ask(`set ${listName(select)} to ${option.label}`, select, () => choose(select, option));
    return () => choose(select, option);
  }

  // ---- Reading back --------------------------------------------------------------------

  // Speaks `text`, then calls `then` (also when speech is unavailable or fails).
  function speak(text, then = () => {}) {
    if (!window.speechSynthesis || !text) {
      then();
      return;
    }
    speechSynthesis.cancel();
    const utterance = new SpeechSynthesisUtterance(text);
    utterance.lang = document.documentElement.lang || navigator.language;
    utterance.rate = 1.1;
    const began = Date.now();
    // What the microphone hears of it, until a moment after, is not a request. Capped by
    // its length, as a dropped utterance may never report its end.
    const cap = text.length * 80 + 3000;
    mutedUntil = Date.now() + cap;
    let done = false;
    const finish = () => {
      if (done) return;
      done = true;
      mutedUntil = Math.min(mutedUntil, Date.now() + 1000);
      if (speaking === utterance) speaking = null;
      record("spoken", { text, ms: Date.now() - began, cut_short: hushedAt >= began });
      then();
      // Listening only to hear "stop" during the reading ends with it, unless asked on.
      if (readingOnly && recognition) recognition.stop();
    };
    utterance.onend = utterance.onerror = finish;
    setTimeout(finish, cap);
    // Held, or Chrome may collect it before it ends.
    speaking = utterance;
    lastReading = text;
    speechSynthesis.speak(utterance);
    if (voiced && Recognition && !bar.hidden) listen(true);
  }
  // "stop" during a reading, unless the reading itself said it and the microphone heard it.
  function interrupts(event) {
    const reading = ` ${normalize(speaking.text)} `;
    for (let index = event.resultIndex; index < event.results.length; index += 1) {
      for (const alternative of event.results[index]) {
        const match = normalize(alternative.transcript).match(HUSH);
        if (match && !reading.includes(` ${match[1]} `)) return true;
      }
    }
    return false;
  }
  function hush() {
    hushedAt = Date.now();
    // Its end runs whatever follows the reply, as if it had finished.
    speechSynthesis.cancel();
    report("Stopped reading.", false, false);
  }
  // The words of a reading, to tell its echo from the user talking over it.
  function readingWords(text) {
    return new Set(normalize(text).split(" "));
  }
  // Words the reading never said, two of them or all of a short phrase: the user talking.
  function talksOver(event, said) {
    for (let index = event.resultIndex; index < event.results.length; index += 1) {
      const words = normalize(event.results[index][0].transcript).split(" ").filter(Boolean);
      const fresh = words.filter((word) => !said.has(word));
      if (fresh.length >= 2 || (fresh.length && fresh.length === words.length)) return true;
    }
    return false;
  }
  // What the user said after the echo of the reading that was heard with it.
  function unecho(transcript, said) {
    const words = transcript.trim().split(/\s+/);
    let start = 0;
    while (start < words.length && said.has(normalize(words[start]))) start += 1;
    return words.slice(start).join(" ");
  }
  function notificationsText() {
    const history = Object.values(window.dashboardNotifications?.history() || {})
      .filter((entry) => entry.seenAt < entry.at)
      .sort((a, b) => b.at - a.at);
    if (!history.length) return "No unseen updates.";
    const titles = history.slice(0, 3).map((entry) => entry.title);
    const more = history.length > 3 ? ` And ${history.length - 3} more.` : "";
    return `${history.length} unseen. ${titles.join(". ")}.${more}`;
  }

  // ---- Commands ------------------------------------------------------------------------

  function showPage(name) {
    const tab = byId(`${name}-tab`);
    // The tab's own words, without its counts.
    const label = [...tab.childNodes]
      .filter((child) => child.nodeType === Node.TEXT_NODE)
      .map((child) => child.textContent.trim())
      .join(" ")
      .trim();
    if (tab.hidden) fail(`${label} is not set up on this dashboard.`);
    closeDialogs();
    tab.click();
    return `Showing ${label}.`;
  }
  function pageFrom(text) {
    const plain = text.replace(/^(the )/, "").replace(/ (tab|page|view|panel)$/, "");
    for (const [page, names] of Object.entries(PAGES)) if (names.includes(plain)) return page;
    return null;
  }
  function closeTop() {
    const dialog = topDialog();
    if (!dialog) fail("Nothing is open to close.");
    // The dialog's own Close button runs whatever it does on closing.
    const close = [...dialog.querySelectorAll("button")].find(
      (button) => shown(button) && /^(close|×)/i.test(nameOf(button)),
    );
    if (close) close.click();
    else dialog.close();
    return "Closed.";
  }
  function scroll(direction) {
    const dialog = topDialog();
    const box = dialog || document.scrollingElement;
    const page = (dialog?.clientHeight || innerHeight) * 0.8;
    const behavior = matchMedia("(prefers-reduced-motion: reduce)").matches ? "instant" : "smooth";
    if (direction === "top") box.scrollTo({ top: 0, behavior });
    else if (direction === "bottom") box.scrollTo({ top: box.scrollHeight, behavior });
    else box.scrollBy({ top: direction === "up" ? -page : page, behavior });
    return "";
  }
  function showItem(url, page) {
    closeDialogs();
    const target = new URL(location.href);
    target.search = new URLSearchParams({ item: url }).toString();
    target.hash = page;
    window.dashboardNavigation.open(target.href);
  }
  async function openItem(kind, number, where) {
    const overview = window.dashboardOverview;
    const near = (repo) => !where || words(where).every((word) => normalize(repo).includes(word));
    let page = kind === "issue" ? "issues" : "prs";
    const paths = { issue: "issues", pr: "pull", watch: "(pull|issues)" }[kind];
    let found =
      kind === "watch"
        ? []
        : (await overview.items(page)).filter((item) => item.number === number && near(item.repo));
    if (!found.length) {
      page = "watcher";
      found = overview
        .watches()
        .filter((job) => new RegExp(`/${paths}/${number}$`).test(job.url || "") && near(job.repo));
    }
    const noun = kind === "issue" ? "Issue" : kind === "watch" ? "Watch" : "PR";
    if (!found.length) fail(`${noun} ${number} is not in the dashboard.`);
    if (found.length > 1)
      fail(`${noun} ${number} is in ${found.map((item) => item.repo).join(" and ")}.`);
    showItem(found[0].url, page);
    return `Showing ${found[0].repo} #${number}.`;
  }
  // Every open modal, top first: stacked ones would keep the page behind them inert.
  function closeDialogs() {
    for (let dialog = topDialog(); dialog; dialog = topDialog()) dialog.close();
  }
  // The open agent's question: its newest answer form and the choices in it.
  function question() {
    const forms = [...byId("ws-viewer").querySelectorAll(".ws-answer")].filter(shown);
    const box = forms[forms.length - 1];
    if (!box) return null;
    const options = [
      ...box.querySelectorAll(
        ".ws-choices button, label:not(.ws-answer-other) > input[type=radio], input[type=checkbox]",
      ),
    ].filter((element) => !element.disabled);
    return { box, options };
  }
  function pickOption(number) {
    // A question in the transcript, or a live dialog on the agent's screen.
    const asked = byId("ws-viewer").open && question();
    if (!asked) fail("The open agent is not asking anything.");
    const choice = asked.options[number - 1];
    if (!choice) fail(`There are ${asked.options.length} options.`);
    // A dialog's buttons answer the agent at once, so they wait for “confirm”.
    if (choice.tagName === "BUTTON") return pressChecked(choice);
    choice.click();
    return `Picked ${textOf(choice.closest("label"))}; “click answer” sends it.`;
  }

  // Each command: a pattern on the normalized words, and what it returns to do, if it
  // applies. A command that does not apply returns null and the next is tried.
  const COMMANDS = [
    [
      /^(?:choose |pick |select |answer )?option (.+)$/,
      (m) => {
        const number = numberFrom(m[1]);
        return number === null ? null : () => pickOption(number);
      },
    ],
    [
      /^(?:new task|start a task|create a task|start a new task)(?:\s+(?:to|for|about|that))?(?:\s+(.+))?$/,
      (m, raw) => () => {
        const task = m[1]
          ? raw.replace(
              /^\s*(start )?(a )?(new task|create a task|start a task)[\s,:]+((to|for|about|that)\s+)?/i,
              "",
            )
          : "";
        closeDialogs();
        void window.dashboardTasks.create({ task });
        return task ? "New task, filled in. Pick the repository, then start it." : "New task.";
      },
    ],
    [
      /^(?:open |show |find |go to )?(pr|pull request|pull|issue|watch) (?:number )?#?(.+?)(?: (?:in|on|from|of) (.+))?$/,
      (m) => {
        const number = numberFrom(m[2]);
        if (number === null) return null;
        const kind = m[1] === "issue" ? "issue" : m[1] === "watch" ? "watch" : "pr";
        return () => openItem(kind, number, m[3]);
      },
    ],
    [
      /^(?:go to |go |show me |show |open |switch to |view )?(.+)$/,
      (m) => {
        const page = pageFrom(m[1]);
        if (page) return () => showPage(page);
        const dialog = DIALOGS.find(([pattern]) => pattern.test(m[1]));
        return dialog ? () => press(byId(dialog[1])) : null;
      },
    ],
    [
      /^(close|dismiss|escape|exit)( (this|that|the )?(dialog|viewer|window|popup|panel|workspace))?$/,
      () => () => closeTop(),
    ],
    [
      /^(go )?back$/,
      () => () => {
        history.back();
        return "";
      },
    ],
    [
      /^(go )?forward$/,
      () => () => {
        history.forward();
        return "";
      },
    ],
    [
      /^(?:scroll|page|go)(?: to the| to)? (up|down|top|bottom)$|^(top|bottom)$/,
      (m) => () => scroll(m[1] || m[2]),
    ],
    [
      /^(next|previous|prev)( workspace| one| agent)?$/,
      (m) => {
        if (!byId("ws-viewer").open) return null;
        const button = byId(m[1] === "next" ? "ws-viewer-next" : "ws-viewer-prev");
        return () =>
          shown(button) ? press(button) : fail("There is no other workspace to go to.");
      },
    ],
    [
      /^(?:search|search for|find|look for|look up) (.+)$/,
      (m, raw) => {
        const field = searchField();
        if (!field) return null;
        const text = raw.replace(/^\s*(search( for)?|find|look (for|up))[\s,:]+/i, "");
        return () => {
          setField(field, text);
          return `Searching for ${text}.`;
        };
      },
    ],
    [
      /^clear (the )?(search|filters?|everything)$/,
      (m) => () => {
        const field = searchField();
        if (field) setField(field, "");
        if (m[2].startsWith("filter") || m[2] === "everything")
          // Not settings, nor whom a message goes to: those confirm one at a time.
          for (const select of lists().filter((l) => !l.closest("#settings-dialog, #ws-message"))) {
            const all = [...select.options].find((option) => option.value === "all");
            if (all && select.value !== "all") choose(select, all);
          }
        return "Cleared.";
      },
    ],
    [
      /^(?:sort|order)(?: by)? (.+)$/,
      (m) => {
        // Only sort controls: any other name of three words or more would match as well.
        const sorts = actionable().filter(
          (element) => element.dataset.sort !== undefined || /^sort by /i.test(nameOf(element)),
        );
        const buttons = named(`sort by ${m[1]}`, sorts);
        if (buttons.length === 1) return () => pressChecked(buttons[0]);
        return filter("sort", m[1]);
      },
    ],
    [
      /^(ascending|descending|reverse|reverse order|flip (the )?(sort|order)|newest first|oldest first)$/,
      () => {
        // Phones have a direction button; wide tables flip on their sorted column's header.
        const button =
          actionable().find((element) => element.id?.endsWith("sort-direction")) ||
          actionable().find((element) => /^(a|de)scending$/.test(element.dataset.sort || ""));
        return button ? () => press(button) : null;
      },
    ],
    [
      /^(?:filter|set|show only|only show|change) (.+?) (?:to|by|as|is) (.+)$/,
      (m) => filter(m[1], m[2]),
    ],
    [/^(?:filter by|filter|show only|only show) (.+)$/, (m) => filter(null, m[1])],
    [
      /^(refresh|reload|update)( (this|the )?(page|list|view|lists))?$/,
      () => click("refresh") || (() => fail("Nothing here refreshes.")),
    ],
    [
      /^(click|press|tap|choose|pick|select|open|hit|toggle|check|uncheck) (.+?) (?:for|on|of|in) (.+)$/,
      (m) => rowAction(m[2], m[3], m[1] === "check" ? true : m[1] === "uncheck" ? false : null),
    ],
    [
      /^(click|press|tap|choose|pick|select|open|hit|toggle|check|uncheck) (.+)$/,
      (m) => {
        const checked = m[1] === "check" ? true : m[1] === "uncheck" ? false : null;
        return click(m[2], { checked }) || (() => fail(`Nothing on screen is called “${m[2]}”.`));
      },
    ],
    // Without a verb: a row's action, then a control by its exact name.
    [/^(.+?) (?:for|on|of) (.+)$/, (m) => rowAction(m[1], m[2])],
    [/^(.+)$/, (m) => click(m[1], { exact: true })],
  ];

  // What `raw` asks for, ready to run, or null when it names nothing.
  function resolve(raw) {
    const text = normalize(raw);
    if (!text) return null;
    for (const [pattern, make] of COMMANDS) {
      const match = text.match(pattern);
      if (!match) continue;
      const action = make(match, raw.trim());
      if (action) return action;
    }
    return null;
  }

  // ---- The assistant -------------------------------------------------------------------

  function newConversation() {
    return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
  }
  // The dashboard keeps a transcript of each exchange, to look back on where talking to
  // the assistant went wrong. A lost event never holds the conversation up.
  function record(event, details = {}) {
    void call("/api/voice-log", { event, conversation: conversationId, ...details }).catch(
      () => {},
    );
  }
  async function call(url, body) {
    const action = url.split("?")[0].replace("/api/", "");
    const response = await fetch(
      url,
      body
        ? {
            method: "POST",
            headers: { "Content-Type": "application/json", "X-Babysit-Action": action },
            body: JSON.stringify(body),
          }
        : { cache: "no-store" },
    );
    let value = {};
    try {
      value = await response.json();
    } catch {
      // The status says enough.
    }
    if (!response.ok || value.error) throw Error(value.error || `HTTP ${response.status}`);
    return value;
  }
  const sleep = (ms) => new Promise((done) => setTimeout(done, ms));
  async function until(check, ms) {
    for (const end = Date.now() + ms; Date.now() < end; await sleep(100)) {
      const value = check();
      if (value) return value;
    }
    return check();
  }
  // What the assistant needs to know of the page, beyond what its tools read.
  function context() {
    const agent = window.workspaceViewer?.current() || null;
    const asked = agent && question();
    const dialog = topDialog();
    return {
      page: location.hash.slice(1) || "attention",
      item: new URLSearchParams(location.search).get("item"),
      dialog:
        dialog && dialog.id !== "ws-viewer"
          ? dialog.getAttribute("aria-label") || textOf(dialog.querySelector("h1, h2, h3"))
          : null,
      agent,
      agent_question: asked
        ? {
            text: textOf(asked.box.querySelector("legend, p, h3")).slice(0, 500),
            options: asked.options.map((option) =>
              textOf(option.closest("label") || option).slice(0, 200),
            ),
          }
        : null,
      unseen_updates: notificationsText(),
      controls: controls(),
    };
  }
  // What can be pressed or chosen where the user is, by the names commands match, so the
  // assistant names real controls instead of guessing them. Tabs are the pages already;
  // links and a row's own checkbox name one item each.
  function controls() {
    const ROW = "tr, li, article, .card, [role=row]";
    const names = (elements) => [...new Set(elements.map(nameOf).filter(Boolean))];
    const pressable = actionable().filter(
      (element) => !element.matches("[role=tab], a[href], select"),
    );
    const rows = pressable.filter((element) => element.closest(ROW));
    return {
      buttons: names(pressable.filter((element) => !element.closest(ROW)))
        .slice(0, 40)
        .map((name) => clip(name, 80)),
      row_buttons: names(rows.filter((element) => !element.matches("input")))
        .slice(0, 20)
        .map((name) => clip(name, 60)),
      lists: lists()
        .slice(0, 12)
        .map((select) => ({
          name: clip(listName(select), 80),
          value: clip(select.selectedOptions[0]?.label || "", 80),
          options: [...select.options]
            .filter((option) => !option.disabled && !option.hidden)
            .slice(0, 20)
            .map((option) => clip(option.label || option.text, 60)),
        })),
    };
  }
  // Leaves out the least useful of the page's description until the server takes it.
  function within(described, limit) {
    const cuts = [
      () => described.controls.lists.forEach((list) => list.options.splice(6)),
      () => (described.controls.row_buttons = []),
      () => described.controls.lists.forEach((list) => (list.options = [])),
      () => (described.controls = null),
    ];
    for (const cut of cuts) {
      if (JSON.stringify(described).length <= limit) break;
      cut();
    }
    return described;
  }
  async function openAgent(action) {
    const viewer = window.workspaceViewer || fail("Agent conversations are not available here.");
    const target = action.workspace ? { workspace: action.workspace } : { watch: action.watch };
    const name = agentName(target);
    const now = viewer.current();
    if (!now || now.workspace !== target.workspace || now.watch !== target.watch) {
      closeDialogs();
      const links = action.url ? [{ label: "GitHub", url: action.url }] : [];
      await viewer.open({ ...target, name, links }, "transcript");
    }
    return `Showing ${name}.`;
  }
  // What the dashboard itself calls the item an agent works on, never the assistant's
  // label: the workspace or watch id when the dashboard lists neither.
  function agentName(target) {
    const label = (item) =>
      [item.repo, item.number ? `#${item.number}` : ""].filter(Boolean).join(" ") || item.title;
    const items = window.dashboardAttention?.feed()?.items || [];
    const item = items.find((entry) =>
      target.workspace ? entry.workspace_id === target.workspace : entry.watch?.id === target.watch,
    );
    if (item && label(item)) return label(item);
    const job =
      target.watch &&
      (window.dashboardOverview?.watches() || []).find((j) => j.id === target.watch);
    const number = job?.url?.match(/\/(\d+)$/)?.[1];
    if (job?.repo) return `${job.repo}${number ? ` #${number}` : ""}`;
    return target.workspace ? `workspace ${target.workspace}` : `watch ${target.watch}`;
  }
  // Whom the viewer's message box sends to: the agent shown, and the pane picked in it.
  function recipient() {
    const agent = window.workspaceViewer?.current();
    const pane = byId("ws-message-agent").querySelector("select")?.value ?? "";
    return JSON.stringify([agent?.workspace, agent?.watch, agent?.key, pane]);
  }
  // Why the viewer will not send, in its own words: no agent here, or a question first.
  function unsendable() {
    if (question())
      return "The agent is waiting on its question; answer it first with “option” and a number.";
    const notes = [
      ...document.querySelectorAll("#ws-message-agent .pr-meta, #ws-agent-interaction .pr-meta"),
    ]
      .filter(shown)
      .map(textOf)
      // "To <agent>" names the recipient; "Looking for agents…" is not an answer yet.
      .filter((text) => text && !text.startsWith("To ") && !text.endsWith("…"));
    return notes.length ? `The message cannot be sent: ${notes.join(" ")}` : null;
  }
  async function messageAgent(action, live) {
    await openAgent(action);
    const field =
      (await until(messageBox, 5000)) || fail("This agent cannot be messaged from here.");
    const send = byId("ws-message-send");
    if (!live()) fail("Stopped: the user moved on.");
    const draft = field.value;
    setField(field, action.text);
    // The viewer enables Send once it has read the agents, which can take a while on
    // another machine; until then, or instead, it says why not.
    const ready = () => shown(send) && !send.disabled;
    await until(() => ready() || unsendable(), 10000);
    if (!ready()) fail(unsendable() || "The message cannot be sent from here.");
    if (!live()) fail("Stopped: the user moved on.");
    // What the user confirms is read from the page, not taken from the assistant.
    const to = recipient();
    const current = window.workspaceViewer.current();
    const name = agentName(current?.workspace ? current : { watch: current?.watch });
    const long =
      action.text.length > 200 ? `a ${action.text.length}-character message starting ` : "";
    // A long message is shown whole but read out by its start: hearing all of it again
    // takes longer than reading it.
    const words = action.text.trim().split(/\s+/);
    const aloud =
      words.length > 20
        ? `send your ${words.length}-word message, starting “${words.slice(0, 12).join(" ")}…”, to ${name}`
        : null;
    return ask(
      `send ${long}“${clip(action.text, 200)}” to ${name}`,
      send,
      () => {
        if (recipient() !== to || field.value !== action.text)
          fail("The message or whom it goes to changed; nothing was sent.");
        // The viewer reports what happened; a watch's session asks first.
        byId("ws-message").requestSubmit(send);
        return "Sent.";
      },
      () => {
        // The user's own draft comes back, unless they have written over ours meanwhile.
        if (field.isConnected && field.value === action.text && recipient() === to)
          setField(field, draft);
      },
      aloud,
    );
  }
  async function perform(action, live = () => true) {
    if (action.type === "message_agent") return messageAgent(action, live);
    if (action.type === "command") {
      const run = resolve(action.text) || fail(`There is no dashboard command “${action.text}”.`);
      return (await run()) || "";
    }
    if (action.workspace || action.watch) return openAgent(action);
    if (action.url) {
      const page = action.page || (/\/issues\/\d+$/.test(action.url) ? "issues" : "prs");
      showItem(action.url, page);
      return "";
    }
    return showPage(action.page);
  }
  function describe(action) {
    const target = action.name || action.url || action.workspace || action.watch || action.page;
    if (action.type === "command") return `command “${clip(action.text, 200)}”`;
    if (action.type === "message_agent") return `message ${target}: “${clip(action.text, 300)}”`;
    return `open ${target}`;
  }
  // Runs the assistant's actions in order until one fails or waits for “confirm”.
  async function act(actions, entry, live) {
    const problems = [];
    for (const [index, action] of actions.entries()) {
      const before = pending;
      let result;
      try {
        result = await perform(action, live);
      } catch (error) {
        problems.push(clip(error.message, 500));
        entry.outcomes.push(`${describe(action)} → failed: ${clip(error.message, 500)}`);
        break;
      }
      // Overtaken meanwhile (a newer request, start over, the bar closed): stop here.
      if (!live()) {
        if (pending && pending !== before) dropPending("dropped: the user moved on");
        entry.outcomes.push(`${describe(action)} → stopped: the user moved on`);
        return { problems: [], asking: null, stopped: true };
      }
      if (pending && pending !== before) {
        const asked = describe(action);
        entry.outcomes.push(`${asked} → waiting for the user to confirm`);
        const at = entry.outcomes.length - 1;
        pending.settle = (outcome) => {
          entry.outcomes[at] = `${asked} → ${outcome}`;
        };
        if (index < actions.length - 1) entry.outcomes.push("later actions → not run");
        return { problems, asking: result };
      }
      entry.outcomes.push(`${describe(action)} → done${result ? `: ${clip(result, 500)}` : ""}`);
    }
    return { problems, asking: null };
  }
  // Stops waiting for the request being answered, and the supervisor answering it.
  function abandon() {
    generation += 1;
    thinking = false;
    if (inFlight) void call("/api/voice-turn", { cancel: inFlight }).catch(() => {});
    inFlight = null;
    render();
  }
  function forget() {
    abandon();
    conversation = [];
    conversationId = newConversation();
    dropPending("cancelled: the user started over");
  }
  async function converse(text, spoken) {
    lastTurnAt = Date.now();
    if (inFlight) abandon();
    // A “yes” after this answers the assistant, not a confirmation that ran out.
    expiredAt = 0;
    const turn = ++generation;
    const live = () => turn === generation;
    const quiet = stops;
    const history = conversation.slice(-HISTORY);
    conversation.push({ role: "user", text, outcomes: [] });
    thinking = true;
    render();
    // Progress shows, but only the answer is spoken.
    report("Thinking…", false, false);
    let reply;
    let id = null;
    const asked = Date.now();
    try {
      ({ id } = await call("/api/voice-turn", {
        text,
        history,
        context: within(context(), CONTEXT_LIMIT),
        conversation: conversationId,
      }));
      // Overtaken before the supervisor even had it: it need not answer.
      if (live()) inFlight = id;
      else void call("/api/voice-turn", { cancel: id }).catch(() => {});
      for (const end = Date.now() + ANSWER_WITHIN; ;) {
        await sleep(POLL_MS);
        if (turn !== generation) return;
        if (Date.now() > end) fail("No answer in time.");
        const state = await call(`/api/voice-turn?id=${encodeURIComponent(id)}`);
        if (turn !== generation) return;
        if (state.status === "done") {
          reply = state.reply;
          break;
        }
        if (state.status === "error") fail(state.error);
        if (state.progress?.length) report(state.progress[state.progress.length - 1], false, false);
      }
    } catch (error) {
      if (turn !== generation) return;
      inFlight = null;
      thinking = false;
      render();
      conversation.pop();
      record("failed", { turn: id, text, error: error.message });
      report(`The assistant could not answer: ${error.message}`, true, true);
      return;
    }
    inFlight = null;
    thinking = false;
    render();
    record("reply", {
      turn: id,
      say: reply.say,
      actions: reply.actions,
      seconds: Math.round((Date.now() - asked) / 100) / 10,
    });
    const entry = { role: "assistant", text: reply.say || "…", outcomes: [] };
    conversation.push(entry);
    const { problems, asking, stopped } = await act(reply.actions || [], entry, live);
    record("outcomes", { turn: id, outcomes: entry.outcomes });
    if (stopped || !live()) return;
    const join = (...parts) => parts.filter(Boolean).join(" ");
    const failed = problems.length ? `That did not work: ${problems[0]}` : "";
    // What needs confirming is always the bar's own words, from the page.
    const said = join(reply.say, failed, asking);
    report(said || "Done.", problems.length > 0, false);
    const waiting = asking ? pending : null;
    // A spoken conversation goes on: the bar listens for the reply once it has spoken,
    // unless the user stopped it meanwhile. The time to confirm starts then too.
    const resume = () => {
      if (waiting && pending === waiting) waiting.until = Date.now() + CONFIRM_WITHIN;
      if (spoken && live() && stops === quiet && !bar.hidden) listen();
    };
    if (prefs.speak) speak(join(reply.say, failed, waiting?.aloud || asking), resume);
    else resume();
  }

  // What the user said or typed: the bar's own words first, the rest to the assistant.
  // `heardAs`: how a spoken request ended and how long it took, for the transcript.
  async function run(raw, alternatives = [], spoken = false, heardAs = {}) {
    if (pending && pending.until <= Date.now()) {
      dropPending("not confirmed in time");
      expiredAt = Date.now();
    }
    const said = [raw, ...alternatives].map(normalize);
    const is = (pattern) => said.some((text) => pattern.test(text));
    const heard = raw.trim();
    showHeard(heard);
    if (!heard) return;
    voiced = spoken;
    // Long unused, the conversation starts afresh, in the transcript too.
    if (conversation.length && Date.now() - lastTurnAt > FORGET_AFTER) {
      conversation = [];
      conversationId = newConversation();
    }
    record("heard", { text: heard, alternatives, how: spoken ? "pause" : "typed", ...heardAs });
    const handled = (what) => record("local", { text: what });
    // Cancel wins on any of the recognizer's guesses; confirm needs its best one.
    if (pending && is(CANCEL)) {
      handled("cancel");
      dropPending("cancelled by the user");
      report("Cancelled.");
      return;
    }
    if (!pending && CONFIRM.test(said[0]) && Date.now() - expiredAt < LATE) {
      handled("confirm, too late");
      expiredAt = 0;
      report("That waited too long, so nothing was done. Ask again.", true);
      return;
    }
    if (pending && CONFIRM.test(said[0])) {
      const waiting = pending;
      clearPending();
      try {
        const result = waiting.run();
        record("confirmation", { what: waiting.what, outcome: `confirmed: ${result || "done"}` });
        waiting.settle(`confirmed: ${result || "done"}`);
        report(result || "Done.");
      } catch (error) {
        record("confirmation", { what: waiting.what, outcome: `failed: ${error.message}` });
        waiting.settle(`confirmed, but failed: ${error.message}`);
        report(error.message, true, true);
      }
      return;
    }
    if (is(STOP)) {
      handled("stop listening");
      window.speechSynthesis?.cancel();
      stop();
      report("Stopped listening.", false, false);
      return;
    }
    if (is(RESTART)) {
      handled("start over");
      forget();
      report("Starting over.");
      return;
    }
    if (is(HELP)) {
      handled("help");
      toggleHelp(true);
      report("Ask in your own words; here are some examples.", false, false);
      return;
    }
    dropPending("not confirmed; the user went on to something else");
    await converse(heard, spoken);
  }

  // ---- Listening -----------------------------------------------------------------------

  // How long a quiet spell ends what has been said so far.
  function pauseFor(text) {
    const plain = normalize(text);
    if ([CONFIRM, CANCEL, STOP, RESTART, HELP].some((pattern) => pattern.test(plain)))
      return QUICK_MS;
    const base = PAUSES[prefs.pause] || PAUSES.normal;
    return RUNS_ON.test(plain) ? base + RUN_ON_MS : base;
  }
  // `forReading`: only to hear "stop" while a reply is read, ending with the reading.
  function listen(forReading = false) {
    if (!Recognition) {
      openBar();
      input.focus();
      report(
        "This browser has no speech recognition. Type instead, or use your keyboard's microphone.",
      );
      return;
    }
    wanted = true;
    if (recognition) {
      if (!forReading) {
        keepOn = true;
        readingOnly = false;
      }
      return;
    }
    readingOnly = forReading;
    // A session that never starts still counts as a quick end below.
    startedAt = Date.now();
    const session = new Recognition();
    recognition = session;
    session.lang = document.documentElement.lang || navigator.language || "en-US";
    session.interimResults = true;
    session.maxAlternatives = 3;
    // The bar judges when a request is over: the recognizer would end it at a breath.
    session.continuous = true;
    // The request being spoken: final results by their index, and the words still forming.
    const finals = new Map();
    let interim = "";
    // Results heard during a reading are its echo; those before a sent request are spent.
    const muted = new Set();
    // Results that talked over a reading, heard with its echo in front.
    const over = new Set();
    let overWords = new Set();
    const heard = (index, text) => (over.has(index) ? unecho(text, overWords) : text);
    let spent = 0;
    let seen = 0;
    let began = 0;
    let timer = null;
    let wait = 0;
    const order = () => [...finals.keys()].sort((a, b) => a - b);
    const sofar = () =>
      [...order().map((index) => finals.get(index)[0]), interim]
        .join(" ")
        .replace(/\s+/g, " ")
        .trim();
    const settled = () => {
      clearTimeout(timer);
      timer = null;
      bar.classList.remove("voice-settling");
      finals.clear();
      interim = "";
      began = 0;
      spent = seen;
      if (flush === sendNow) flush = null;
      if (drop === dropNow) drop = null;
      render();
    };
    // Sends what was said; false when there was nothing to send.
    const send = (how) => {
      const text = sofar();
      const indices = order();
      const last = !interim && indices.length ? finals.get(indices[indices.length - 1]) : null;
      const head = (last ? indices.slice(0, -1) : indices).map((index) => finals.get(index)[0]);
      // The recognizer's other guesses at the last part, after what came before it.
      const alternatives = last ? last.slice(1).map((guess) => [...head, guess].join(" ")) : [];
      const speakingMs = began ? Date.now() - began : 0;
      settled();
      if (!text) return false;
      // The "stop" that cut a reading short, finished late: not "stop listening".
      if (Date.now() - hushedAt < 3000 && HUSH.test(normalize(text))) return false;
      // Unless it keeps listening, the microphone rests while the answer comes.
      if (!prefs.continuous && recognition === session) session.stop();
      void run(text, alternatives, true, { how, speaking_ms: speakingMs, waited_ms: wait });
      return true;
    };
    const sendNow = () => send("tap");
    const dropNow = () => settled();
    session.onstart = () => {
      listening = true;
      render();
    };
    session.onresult = (event) => {
      // Our own spoken replies are not requests: "stop" during one cuts it short, and
      // anything else the user says over it stops it and is heard as the next request.
      if (window.speechSynthesis?.speaking || Date.now() < mutedUntil) {
        const said = readingWords(speaking?.text ?? lastReading);
        if ((speaking && interrupts(event)) || !talksOver(event, said)) {
          for (let index = event.resultIndex; index < event.results.length; index += 1)
            muted.add(index);
          if (speaking && interrupts(event)) hush();
          return;
        }
        // A result still forming keeps its index: heard as echo, it now holds the user too.
        for (let index = event.resultIndex; index < event.results.length; index += 1) {
          muted.delete(index);
          over.add(index);
        }
        overWords = said;
        hushedAt = Date.now();
        // This session now hears a request: it outlives the reading, unmuted.
        readingOnly = false;
        keepOn = true;
        mutedUntil = 0;
        window.speechSynthesis?.cancel();
      }
      seen = event.results.length;
      interim = "";
      for (let index = Math.max(event.resultIndex, spent); index < seen; index += 1) {
        if (muted.has(index)) continue;
        const result = event.results[index];
        // Safari can report a final result again: by its index, it counts once.
        if (result.isFinal)
          finals.set(
            index,
            [...result].map((guess) => heard(index, guess.transcript).trim()),
          );
        else interim += ` ${heard(index, result[0].transcript)}`;
      }
      const text = sofar();
      if (!text) return;
      began ||= Date.now();
      showHeard(text, true);
      // Each word heard starts the quiet spell again; the line under the words runs it out.
      clearTimeout(timer);
      wait = pauseFor(text);
      bar.style.setProperty("--voice-pause", `${wait}ms`);
      bar.classList.remove("voice-settling");
      void bar.offsetWidth;
      bar.classList.add("voice-settling");
      timer = setTimeout(() => send("pause"), wait);
      flush = sendNow;
      drop = dropNow;
      render();
    };
    session.onerror = (event) => {
      if (event.error === "no-speech" || event.error === "aborted") return;
      wanted = false;
      const reasons = {
        "not-allowed": "Microphone access is blocked. Allow it for this site, or type instead.",
        "service-not-allowed":
          "Speech recognition is turned off (on an iPhone: Settings → Siri, and Keyboard → Enable Dictation). You can still type.",
        "audio-capture": "No microphone was found. You can still type.",
      };
      report(
        reasons[event.error] || `Speech recognition stopped (${event.error}). You can still type.`,
        true,
      );
    };
    session.onend = () => {
      if (recognition === session) recognition = null;
      listening = false;
      // Safari ends a session at a pause of its own: what was said so far goes then.
      if (finals.size || interim) send("session end");
      // Sessions that end at once, again and again, would spin: give up after a few.
      quickEnds = Date.now() - startedAt < 1000 ? quickEnds + 1 : 0;
      // A reading keeps listening for "stop" until it ends.
      const again = wanted && (prefs.continuous || speaking || keepOn) && quickEnds < 3;
      const reading = readingOnly && !keepOn;
      keepOn = false;
      if (again) setTimeout(() => wanted && !recognition && listen(reading), 150);
      else wanted = false;
      render();
    };
    try {
      session.start();
    } catch (error) {
      recognition = null;
      wanted = false;
      report(`Cannot start listening: ${error.message}`, true);
    }
    openBar();
    render();
  }
  function stop() {
    wanted = false;
    voiced = false;
    keepOn = false;
    readingOnly = false;
    stops += 1;
    // What was being said is dropped, not sent as the microphone goes off.
    drop?.();
    recognition?.stop();
    render();
  }

  // ---- Bar -----------------------------------------------------------------------------

  function showHeard(text, interim = false) {
    byId("voice-heard").textContent = text ? `“${text.trim()}”` : "";
    byId("voice-heard").classList.toggle("voice-interim", interim);
  }
  function report(text, problem = false, aloud = prefs.speak) {
    byId("voice-status").textContent = text;
    byId("voice-status").classList.toggle("voice-problem", problem);
    if (text && aloud) speak(text);
  }
  function render() {
    const mic = byId("voice-mic");
    mic.setAttribute("aria-pressed", String(listening));
    // While a request waits out its pause, a tap sends it at once.
    mic.setAttribute(
      "aria-label",
      flush ? "Send now" : listening ? "Stop listening" : "Start listening",
    );
    byId("voice-toggle").classList.toggle("voice-on", listening);
    bar.classList.toggle("voice-listening", listening);
    bar.classList.toggle("voice-thinking", thinking);
    input.placeholder = listening
      ? "Listening…"
      : Recognition
        ? "Ask or say what to do"
        : "Type what you want to know or do";
  }
  // Within the top modal dialog while one is open, since everything else is inert then.
  function dock() {
    const home = topDialog() || document.body;
    if (bar.parentElement !== home) home.append(bar);
  }
  function openBar() {
    bar.hidden = false;
    byId("voice-toggle").setAttribute("aria-expanded", "true");
    document.body.classList.add("voice-open");
    dock();
  }
  function closeBar() {
    stop();
    window.speechSynthesis?.cancel();
    abandon();
    dropPending("cancelled: the user closed the voice bar");
    bar.hidden = true;
    toggleHelp(false);
    byId("voice-toggle").setAttribute("aria-expanded", "false");
    document.body.classList.remove("voice-open");
  }
  function toggleHelp(open = byId("voice-help").hidden) {
    byId("voice-help").hidden = !open;
    byId("voice-help-toggle").setAttribute("aria-expanded", String(open));
  }
  for (const [title, samples] of EXAMPLES) {
    const term = document.createElement("dt");
    term.textContent = title;
    const detail = document.createElement("dd");
    for (const sample of samples) {
      const example = document.createElement("button");
      example.type = "button";
      example.className = "voice-example";
      example.textContent = sample;
      // Fills the field to try or adapt; Enter sends it.
      example.onclick = () => {
        input.value = sample;
        input.focus();
      };
      detail.append(example);
    }
    byId("voice-examples").append(term, detail);
  }
  // Floating controls keep clear of the bar, however tall its replies make it.
  new ResizeObserver(() => {
    document.body.style.setProperty("--voice-bar-height", `${bar.offsetHeight}px`);
  }).observe(bar);
  new MutationObserver(() => {
    if (!bar.hidden) dock();
  }).observe(document.body, {
    subtree: true,
    attributes: true,
    attributeFilter: ["open"],
  });

  // iOS only speaks once a page has spoken inside a tap.
  let primed = false;
  function prime() {
    if (primed || !window.speechSynthesis) return;
    primed = true;
    speechSynthesis.speak(new SpeechSynthesisUtterance(""));
  }
  byId("voice-toggle").addEventListener("click", prime);
  byId("voice-mic").addEventListener("click", prime);
  byId("voice-toggle").onclick = () => {
    if (bar.hidden) listen();
    else closeBar();
  };
  byId("voice-mic").onclick = () => {
    // Tapping the mic while it speaks interrupts it, to answer sooner.
    window.speechSynthesis?.cancel();
    if (flush?.()) return;
    if (listening || wanted) stop();
    else listen();
  };
  byId("voice-close").onclick = closeBar;
  byId("voice-help-toggle").onclick = () => toggleHelp();
  byId("voice-form").onsubmit = (event) => {
    event.preventDefault();
    const text = input.value;
    input.value = "";
    if (text.trim()) void run(text);
  };
  input.addEventListener("keydown", (event) => {
    // Escape here leaves the field, not the dialog it sits in.
    if (event.key !== "Escape") return;
    event.preventDefault();
    event.stopPropagation();
    if (pending) {
      dropPending("cancelled by the user");
      report("Cancelled.", false, false);
    } else closeBar();
  });
  for (const key of ["continuous", "speak"]) {
    const box = byId(`voice-${key}`);
    box.checked = prefs[key];
    box.onchange = () => {
      prefs[key] = box.checked;
      savePrefs();
      if (key === "continuous" && recognition) recognition.stop();
      if (key === "speak" && !box.checked) window.speechSynthesis?.cancel();
    };
  }
  const pause = byId("voice-pause");
  pause.value = PAUSES[prefs.pause] ? prefs.pause : "normal";
  pause.onchange = () => {
    prefs.pause = pause.value;
    savePrefs();
  };
  // Alt+V (Option+V) listens from anywhere, dialogs included, except while typing
  // elsewhere: Option+V types a character on a Mac.
  document.addEventListener("keydown", (event) => {
    if (event.code !== "KeyV" || !event.altKey || event.ctrlKey || event.metaKey) return;
    if (event.target !== input && event.target.matches?.("input, textarea, [contenteditable]"))
      return;
    event.preventDefault();
    window.speechSynthesis?.cancel();
    if (flush?.()) return;
    if (listening || wanted) stop();
    else listen();
  });
  render();

  window.dashboardVoice = {
    run,
    resolve,
    perform,
    listen,
    stop,
    forget,
    numberFrom,
    normalize,
    within,
  };
})();
