"use strict";

(() => {
  const byId = (id) => document.getElementById(`credential-${id}`);
  const settings = byId("settings");
  const grant = byId("grant");
  const list = byId("profiles");
  const add = byId("add");
  const grantForm = byId("grant-form");
  const profile = byId("grant-profile");
  const duration = byId("grant-duration");
  const settingsStatus = byId("settings-status");
  const grantStatus = byId("grant-status");
  let target = null;
  let restarting = false;
  let directoryTarget = null;
  const directoryDialog = document.getElementById("directory-grant");
  const directoryForm = document.getElementById("directory-grant-form");
  const directoryStatus = document.getElementById("directory-grant-status");
  const directoryClose = document.getElementById("directory-grant-close");
  let directorySaving = false;
  directoryClose.onclick = () => directoryDialog.close();
  directoryDialog.addEventListener("cancel", (event) => {
    if (directorySaving) event.preventDefault();
  });
  directoryForm.onsubmit = async (event) => {
    event.preventDefault();
    directorySaving = true;
    directoryClose.disabled = directoryForm.querySelector('button[type="submit"]').disabled = true;
    directoryStatus.textContent = "Restarting the agent…";
    try {
      const value = await request("workspace-directory-grant", {
        ...directoryTarget,
        directory: document.getElementById("directory-grant-path").value.trim(),
      });
      directoryStatus.textContent =
        value.warning ||
        (value.changed ? "Directory access granted." : "The agent was not restarted.");
    } catch (error) {
      directoryStatus.textContent = error.message;
    } finally {
      directorySaving = false;
      directoryClose.disabled = directoryForm.querySelector('button[type="submit"]').disabled =
        false;
    }
  };

  async function request(action, body) {
    let response;
    try {
      response = await fetch(
        `/api/${action}`,
        body
          ? {
              method: "POST",
              headers: { "Content-Type": "application/json", "X-Babysit-Action": action },
              body: JSON.stringify(body),
            }
          : { cache: "no-store" },
      );
    } catch {
      throw Error(
        ["workspace-credential-grant", "workspace-directory-grant"].includes(action)
          ? "The connection dropped. Check the session before granting access again."
          : "Could not reach the dashboard.",
      );
    }
    const value = await response.json().catch(() => ({}));
    if (!response.ok) throw Error(value.error || `The dashboard returned HTTP ${response.status}.`);
    return value;
  }

  function renderProfiles(profiles) {
    list.replaceChildren();
    for (const [id, item] of Object.entries(profiles)) {
      const row = document.createElement("li");
      const remove = document.createElement("button");
      remove.type = "button";
      remove.textContent = "Remove";
      remove.setAttribute("aria-label", `Remove ${item.name}`);
      remove.onclick = () => void updateProfiles({ remove: id }, "Profile removed.");
      const contents = item.format === "entry" ? "entire entry" : "password line";
      row.append(`${item.name} · ${item.entry} · ${contents} `, remove);
      list.append(row);
    }
    list.hidden = !list.childElementCount;
    add.hidden = false;
  }

  let saving = false;
  async function updateProfiles(body, success) {
    if (saving) return;
    saving = true;
    byId("settings-close").disabled = true;
    settingsStatus.textContent = "Saving…";
    try {
      const value = await request("credential-profiles", body);
      renderProfiles(value.profiles);
      if (!body.remove) add.reset();
      settingsStatus.textContent = success;
    } catch (error) {
      settingsStatus.textContent = error.message;
    } finally {
      saving = false;
      byId("settings-close").disabled = false;
    }
  }

  let settingsLoad = 0;
  byId("settings-open").onclick = async () => {
    const load = ++settingsLoad;
    list.hidden = add.hidden = true;
    settingsStatus.textContent = "Loading profiles…";
    settings.showModal();
    try {
      const value = await request("credential-profiles");
      if (load !== settingsLoad || !settings.open) return;
      renderProfiles(value.profiles);
      settingsStatus.textContent = "";
    } catch (error) {
      if (load === settingsLoad) settingsStatus.textContent = error.message;
    }
  };
  byId("settings-close").onclick = () => settings.close();
  settings.addEventListener("cancel", (event) => {
    if (saving) event.preventDefault();
  });
  add.onsubmit = (event) => {
    event.preventDefault();
    void updateProfiles(
      {
        name: byId("name").value.trim(),
        entry: byId("entry").value.trim(),
        format: byId("format").value,
      },
      "Profile added.",
    );
  };

  window.dashboardCredentials = {
    grantDirectory(recipient, label) {
      directoryTarget = recipient;
      directoryForm.reset();
      directoryStatus.textContent = "";
      document.getElementById("directory-grant-target").textContent = `Grant access to ${label}.`;
      directoryDialog.showModal();
    },
    async grant(recipient, label, restarts = false) {
      target = recipient;
      restarting = restarts;
      grantForm.hidden = true;
      grantForm.reset();
      byId("grant-target").textContent = restarts
        ? `Grant access to ${label}. The idle agent restarts in its pane to receive it.`
        : `Grant access to ${label}.`;
      grantStatus.textContent = "Loading profiles…";
      grant.showModal();
      try {
        const value = await request("credential-profiles");
        if (target !== recipient || !grant.open) return;
        const profiles = Object.entries(value.profiles);
        if (!profiles.length) {
          grantStatus.textContent = "Add a profile under Settings → Credentials first.";
          return;
        }
        profile.replaceChildren(new Option("Choose a profile", ""));
        for (const [id, item] of profiles) profile.add(new Option(item.name, id));
        if (profiles.length === 1) profile.value = profiles[0][0];
        grantForm.hidden = false;
        grantStatus.textContent = "";
        (profiles.length === 1 ? duration : profile).focus();
      } catch (error) {
        if (target === recipient) grantStatus.textContent = error.message;
      }
    },
  };
  byId("grant-close").onclick = () => grant.close();
  const approve = grantForm.querySelector('button[type="submit"]');
  grant.addEventListener("cancel", (event) => {
    if (approve.disabled) event.preventDefault();
  });
  grantForm.onsubmit = async (event) => {
    event.preventDefault();
    if (approve.disabled) return;
    const minutes = duration.valueAsNumber;
    approve.disabled = byId("grant-close").disabled = profile.disabled = duration.disabled = true;
    grantStatus.textContent = restarting
      ? "Restarting the agent with credential access…"
      : "Granting credential access…";
    try {
      const value = await request("workspace-credential-grant", {
        ...target,
        profile: profile.value,
        minutes,
      });
      grantForm.hidden = true;
      grantStatus.textContent =
        `Access granted to ${value.pane} for ${minutes} ${minutes === 1 ? "minute" : "minutes"}.` +
        (value.warning ? ` ${value.warning}` : "");
    } catch (error) {
      grantStatus.textContent = error.message;
    } finally {
      approve.disabled =
        byId("grant-close").disabled =
        profile.disabled =
        duration.disabled =
          false;
    }
  };
})();
