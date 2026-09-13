/* No offline page cache: watcher actions always use the current dashboard. */
function storage(mode, update) {
  return new Promise((resolve, reject) => {
    const opening = indexedDB.open("babysitter-push", 1);
    opening.onupgradeneeded = () => opening.result.createObjectStore("state");
    opening.onerror = () => reject(opening.error);
    opening.onsuccess = () => {
      const db = opening.result;
      const tx = db.transaction("state", mode);
      const store = tx.objectStore("state");
      const request = store.get("inbox");
      let result;
      request.onsuccess = () => {
        result = request.result;
        if (update) {
          result = update(result);
          store.put(result, "inbox");
        }
      };
      tx.oncomplete = () => {
        db.close();
        resolve(result);
      };
      tx.onerror = () => {
        db.close();
        reject(tx.error);
      };
    };
  });
}

async function badge(count) {
  if ("setAppBadge" in self.navigator) {
    try {
      await self.navigator.setAppBadge(count);
    } catch {
      /* Badges may be disabled in Settings. */
    }
  }
}

function notificationURL(raw) {
  const fallback = new URL("/?updates=1", self.location.origin).href;
  try {
    const url = new URL(raw || fallback, self.location.origin);
    return url.origin === self.location.origin && url.pathname === "/" ? url.href : fallback;
  } catch {
    return fallback;
  }
}

self.addEventListener("install", (event) => event.waitUntil(self.skipWaiting()));
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));
self.addEventListener("message", (event) => {
  if (event.data?.type !== "inbox") return;
  event.waitUntil(
    storage("readwrite", (old) =>
      !old || event.data.revision >= old.revision ? event.data : old,
    ).then((value) => badge(value.count)),
  );
});
self.addEventListener("push", (event) => {
  event.waitUntil(
    (async () => {
      let payload = {};
      try {
        payload = event.data?.json() || {};
      } catch {
        /* Still display a visible notification. */
      }
      let state;
      try {
        state = await storage("readonly");
        if (state?.token) {
          try {
            const response = await fetch("/api/push-read", {
              method: "POST",
              headers: { "Content-Type": "application/json", "X-Babysit-Action": "push-read" },
              body: JSON.stringify({ token: state.token, login: state.login }),
              signal: AbortSignal.timeout(5000),
            });
            if (response.ok) {
              const latest = await response.json();
              payload.count = latest.count;
              payload.revision = latest.revision;
            }
          } catch {
            /* The encrypted payload works even when the tailnet is disconnected. */
          }
        }
        state = await storage("readwrite", (old) => {
          if (old && old.revision > (payload.revision || 0)) return old;
          return {
            ...old,
            count: Number.isSafeInteger(payload.count) && payload.count >= 0 ? payload.count : 0,
            revision: payload.revision || Date.now(),
          };
        });
      } catch {
        state = {
          count: Number.isSafeInteger(payload.count) && payload.count >= 0 ? payload.count : 0,
        };
      }
      await Promise.all([
        badge(state.count),
        self.registration.showNotification("Babysitter", {
          body: state.count
            ? `${state.count} unseen ${state.count === 1 ? "item" : "items"}. Open Babysitter for the latest updates.`
            : "Your dashboard is up to date.",
          icon: "/icon-192.png",
          tag: "babysitter-updates",
          data: { url: notificationURL(payload.url) },
        }),
      ]);
      for (const client of await self.clients.matchAll({
        type: "window",
        includeUncontrolled: true,
      }))
        client.postMessage({ type: "push-update" });
    })(),
  );
});
self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  event.waitUntil(
    (async () => {
      const url = notificationURL(event.notification.data?.url);
      const clients = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
      for (const client of clients) {
        if (new URL(client.url).origin === self.location.origin) {
          await client.focus();
          return client.navigate(url);
        }
      }
      return self.clients.openWindow(url);
    })(),
  );
});
