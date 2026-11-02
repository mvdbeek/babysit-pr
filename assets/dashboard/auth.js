"use strict";
// Dashboard sign-in: the header's session button, a banner when the API answers 401,
// and the break-glass page, which trades its link's token (kept in the URL fragment, so
// no server or preview ever logs it) for a session.
(() => {
  const here = () => location.pathname + location.search + location.hash;

  async function breakGlass() {
    const status = document.getElementById("break-glass-status");
    const token = new URLSearchParams(location.hash.slice(1)).get("t");
    // The token is single use; it never stays in the address bar or history.
    history.replaceState(null, "", location.pathname);
    if (!token) {
      status.textContent =
        "This link has no sign-in token. Print a new one with pr_supervisor.py auth login-link.";
      return;
    }
    try {
      const response = await fetch("/auth/break-glass", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Babysit-Action": "break-glass" },
        body: JSON.stringify({ token }),
      });
      if (response.ok) {
        status.textContent = "Signed in. Opening the dashboard…";
        location.replace("/");
        return;
      }
      const body = await response.json().catch(() => ({}));
      status.textContent = body.error || `Sign-in failed (${response.status})`;
    } catch (error) {
      status.textContent = `Sign-in failed: ${error.message}`;
    }
  }

  if (location.pathname === "/auth/break-glass") {
    breakGlass();
    return;
  }

  let session = null;
  const chip = document.createElement("button");
  chip.id = "auth-chip";
  chip.type = "button";
  chip.hidden = true;
  const banner = document.createElement("div");
  banner.id = "auth-banner";
  banner.setAttribute("role", "alert");
  banner.hidden = true;
  const tools = document.querySelector(".header-tools");
  if (tools) tools.insertBefore(chip, document.getElementById("new-task"));
  document.body.prepend(banner);

  function signIn() {
    if (session?.login) location.assign(`${session.login}?next=${encodeURIComponent(here())}`);
  }

  async function signOut() {
    chip.disabled = true;
    try {
      const response = await fetch("/auth/logout", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Babysit-Action": "logout" },
        body: "{}",
      });
      const body = await response.json().catch(() => ({}));
      location.assign(response.ok && body.redirect ? body.redirect : "/");
    } finally {
      chip.disabled = false;
    }
  }

  function render() {
    if (!session?.enabled) {
      chip.hidden = true;
      return;
    }
    chip.hidden = false;
    const signedIn = session.authenticated && session.credential === "session";
    chip.textContent = signedIn ? session.user || "Signed in" : "Sign in";
    const notes = [
      signedIn
        ? `Signed in as ${session.user} (${session.via}). Click to sign out.`
        : session.login
          ? "Sign in with your identity provider."
          : "OIDC is not configured: sign in with pr_supervisor.py auth login-link.",
    ];
    if (session.mode === "report" && session.would_deny > 0) {
      notes.push(
        `${session.would_deny} requests would have been refused under enforce mode ` +
          "(pr_supervisor.py auth report).",
      );
    }
    chip.title = notes.join(" ");
    chip.dataset.count = session.mode === "report" && session.would_deny ? session.would_deny : "";
    chip.classList.toggle("signed-out", !signedIn);
    if (signedIn) banner.hidden = true;
  }

  async function load() {
    try {
      const response = await original("/api/session", { cache: "no-store" });
      if (response.ok) session = await response.json();
    } catch {
      /* Offline: keep what was shown. */
    }
    render();
  }

  function expired() {
    banner.replaceChildren();
    const text = document.createElement("span");
    text.textContent = "You are signed out, so the dashboard cannot load its data.";
    banner.append(text);
    if (session?.login) {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = "Sign in";
      button.addEventListener("click", signIn);
      banner.append(button);
    } else {
      const hint = document.createElement("code");
      hint.textContent = "pr_supervisor.py auth login-link";
      banner.append(hint);
    }
    banner.hidden = false;
  }

  chip.addEventListener("click", () => {
    if (session?.authenticated && session.credential === "session") signOut();
    else signIn();
  });

  // Every script's fetches pass through here: a 401 from the API means signed out.
  const original = window.fetch.bind(window);
  window.fetch = async (...args) => {
    const response = await original(...args);
    if (response.status === 401) {
      const path = new URL(response.url, location.href).pathname;
      if (path.startsWith("/api/") && banner.hidden) {
        await load();
        expired();
      }
    }
    return response;
  };

  load();
  setInterval(load, 5 * 60 * 1000);
})();
