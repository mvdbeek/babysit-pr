"use strict";
// A newer Codex and a one-click npm update. Agents the babysitter starts skip Codex's
// own startup update prompt, so this notice is where updates surface instead.
(() => {
  const notice = document.getElementById("codex-update");
  const text = document.getElementById("codex-update-text");
  const button = document.getElementById("codex-update-button");
  let timer = null;
  // A finished update is reported only to a page that saw it run.
  let watched = false;
  function message(value) {
    const update = value.update ?? null;
    if (update?.running) return `Updating Codex to ${update.version}…`;
    if (watched && update?.ok)
      return `Codex updated to ${update.version}. New and resumed agents use it; running agents keep their version.`;
    const failed = update?.ok === false ? ` The last update failed: ${update.error}` : "";
    if (value.available)
      return (
        `Codex ${value.installed} → ${value.latest} is available.` +
        (value.updatable ? "" : " Update it the way it was installed.") +
        failed
      );
    return watched ? failed.trim() : "";
  }
  function show(value) {
    const running = Boolean(value.update?.running);
    watched ||= running;
    text.textContent = message(value);
    notice.hidden = !text.textContent;
    button.hidden = !value.updatable || !(running || value.available);
    button.disabled = running;
    clearTimeout(timer);
    timer = setTimeout(load, running ? 3000 : 600000);
  }
  async function load() {
    try {
      const response = await fetch("/api/codex-update", { cache: "no-store" });
      if (response.ok) {
        show(await response.json());
        return;
      }
    } catch {
      // Offline or restarting: the next poll tries again.
    }
    clearTimeout(timer);
    timer = setTimeout(load, 600000);
  }
  button.addEventListener("click", async () => {
    button.disabled = true;
    try {
      const response = await fetch("/api/codex-update", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Babysit-Action": "codex-update" },
        body: "{}",
      });
      const result = await response.json();
      if (!response.ok) throw Error(result.error || `HTTP ${response.status}`);
      watched = true;
      show(result);
    } catch (error) {
      text.textContent = `Could not update Codex: ${error.message}`;
      button.disabled = false;
    }
  });
  void load();
})();
