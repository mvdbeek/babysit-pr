/* Opt-in background delivery. The server owns the subscribed device's inbox. */
(() => {
  const button = document.getElementById("notifications-push-enable");
  const status = document.getElementById("notifications-push-status");
  const supported =
    window.isSecureContext &&
    "serviceWorker" in navigator &&
    "PushManager" in window &&
    "Notification" in window;
  let login = null;
  let token = null;
  let remote = null;
  let config = null;
  let registration = null;
  let pending = {};
  let syncing = null;
  let resync = false;
  let busy = false;
  let message = "";
  const key = () => `babysit-pr:push:v1:${login}`;

  async function api(action, data, identity = { login, token }) {
    const response = await fetch(`/api/push-${action}`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Babysit-Action": `push-${action}` },
      body: JSON.stringify({ ...identity, ...data }),
      signal: AbortSignal.timeout(15000),
    });
    const result = await response.json();
    if (!response.ok) {
      const error = new Error(result.error || "Cannot sync notifications");
      error.status = response.status;
      throw error;
    }
    return result;
  }

  function save() {
    localStorage.setItem(key(), JSON.stringify({ token, pending }));
  }

  function controls() {
    button.hidden = !supported || !config?.available;
    button.disabled = busy || !login || !registration;
    button.textContent = busy
      ? "Updating…"
      : token && remote?.active !== false
        ? "Disable notifications"
        : "Enable notifications";
    status.textContent =
      message ||
      (token
        ? remote?.error || "Notifications and the Home Screen badge are enabled on this device."
        : supported
          ? config?.error ||
            "Enable alerts and a Home Screen badge, including while Babysitter is closed."
          : "On iPhone, add Babysitter to your Home Screen and open it there to enable notifications.");
  }

  function repaint() {
    if (remote) {
      for (const [url, at] of Object.entries(pending)) {
        if (remote.entries[url])
          remote.entries[url].seenAt = Math.max(remote.entries[url].seenAt, at);
      }
      const unseen = Object.values(remote.entries).filter(
        (entry) => entry.at > entry.seenAt,
      ).length;
      // With focus on, the badge counts what needs the user rather than unseen updates.
      const count = remote.focus ? (remote.badge ?? unseen) : unseen;
      if ("setAppBadge" in navigator) navigator.setAppBadge(count).catch(() => {});
      registration?.active?.postMessage({
        type: "inbox",
        login,
        token,
        count,
        revision: Object.keys(pending).length ? Date.now() : remote.revision || Date.now(),
      });
    }
    window.dashboardNotifications?.render();
    controls();
  }

  function sync() {
    if (!token) return Promise.resolve();
    // One request at a time: calls made meanwhile share a single follow-up request, so a
    // slow or unreachable server never builds a queue that bursts when it recovers.
    if (syncing) {
      resync = true;
      return syncing;
    }
    syncing = (async () => {
      do {
        resync = false;
        if (!token) break;
        const identity = { login, token };
        const sent = { ...pending };
        try {
          const result = await api(
            Object.keys(sent).length ? "seen" : "read",
            { seen: sent },
            identity,
          );
          if (identity.login !== login || identity.token !== token) continue;
          remote = result;
          for (const [url, at] of Object.entries(sent)) {
            if (pending[url] === at) delete pending[url];
          }
          save();
          message = "";
        } catch (error) {
          if (identity.login !== login || identity.token !== token) continue;
          if (error.status === 400)
            remote = { login, entries: remote?.entries || {}, active: false, error: error.message };
          message = `${error.message}. Will retry when connected.`;
        }
        repaint();
      } while (resync);
    })().finally(() => {
      syncing = null;
    });
    return syncing;
  }

  function account(value) {
    if (login === value) return;
    if (login && token)
      registration?.active?.postMessage({
        type: "inbox",
        token: null,
        count: 0,
        revision: Date.now(),
      });
    login = value;
    token = null;
    remote = null;
    pending = {};
    message = "";
    try {
      const stored = JSON.parse(localStorage.getItem(key()));
      if (typeof stored?.token === "string") {
        token = stored.token;
        pending = stored.pending && typeof stored.pending === "object" ? stored.pending : {};
      }
    } catch {
      /* Storage is checked again before subscribing. */
    }
    sync();
    controls();
  }

  function seen(urls, source = null, at = Infinity) {
    if (!remote || !token) return;
    for (const url of urls) {
      if (source && remote.observed?.[source]?.[url] > at) continue;
      const entry = remote.entries[url];
      if (entry && entry.seenAt < entry.at) pending[url] = entry.at;
    }
    if (!Object.keys(pending).length) return;
    try {
      save();
    } catch {
      message = "Seen updates are kept in this tab until the connection recovers.";
    }
    repaint();
    sync();
  }

  button.onclick = async () => {
    busy = true;
    message = "";
    controls();
    try {
      if (token && remote?.active !== false) {
        await api("unsubscribe", {});
        token = null;
        remote = null;
        pending = {};
        save();
        registration.active?.postMessage({
          type: "inbox",
          token: null,
          count: 0,
          revision: Date.now(),
        });
        if ("clearAppBadge" in navigator) await navigator.clearAppBadge();
        await (await registration.pushManager.getSubscription())?.unsubscribe();
      } else {
        // Request permission directly from this tap, before any network await.
        const permission = await Notification.requestPermission();
        if (permission !== "granted")
          throw new Error("Allow notifications in your device settings to enable the badge");
        save(); // Require persistent storage before creating a server subscription.
        const publicKey = Uint8Array.from(
          atob(config.publicKey.replace(/-/g, "+").replace(/_/g, "/")),
          (char) => char.charCodeAt(0),
        );
        let existing = await registration.pushManager.getSubscription();
        if (existing && remote?.active === false) {
          await existing.unsubscribe();
          existing = null;
        }
        const subscription =
          existing ||
          (await registration.pushManager.subscribe({
            userVisibleOnly: true,
            applicationServerKey: publicKey,
          }));
        const unseen = window.dashboardNotifications.unseen();
        const identity = { login, token };
        const result = await api(
          "subscribe",
          {
            subscription: subscription.toJSON(),
            unseen,
            history: window.dashboardNotifications.history(),
          },
          identity,
        );
        if (identity.login !== login)
          throw new Error("The GitHub account changed; enable notifications again");
        token = result.token;
        remote = result;
        pending = {};
        save();
      }
    } catch (error) {
      message = error.message || "Could not change notification settings";
    } finally {
      busy = false;
      repaint();
    }
  };

  if (supported) {
    Promise.all([
      fetch("/api/push-config").then((response) => response.json()),
      navigator.serviceWorker.register("/sw.js").then(() => navigator.serviceWorker.ready),
    ])
      .then(([value, worker]) => {
        config = value;
        registration = worker;
        controls();
        sync();
      })
      .catch(() => {
        message = "Notification setup is unavailable. Refresh to retry.";
        controls();
      });
    navigator.serviceWorker.addEventListener("message", () => sync());
    window.addEventListener("online", () => sync());
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) sync();
    });
    setInterval(() => {
      if (!document.hidden) sync();
    }, 15000);
  }
  window.dashboardPush = {
    account,
    seen,
    entries: (value) => (value === login ? remote?.entries : undefined),
  };
  window.addEventListener("storage", (event) => {
    if (!login || (event.key !== key() && event.key !== null)) return;
    const previous = login;
    login = null;
    account(previous);
    repaint();
  });
  controls();
})();
