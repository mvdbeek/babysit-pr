const form = document.getElementById("pair-form");
const status = document.getElementById("pair-status");
const token = document.getElementById("token");
const id = document.getElementById("extension-id");
const supplied = new URLSearchParams(location.hash.slice(1)).get("id");
if (/^[a-p]{32}$/.test(supplied || "")) id.value = supplied;
history.replaceState(null, "", location.pathname);
// The extension this dashboard ships: its version, and the fixed ID a download loads as.
fetch("/api/extension-package")
  .then((response) => (response.ok ? response.json() : Promise.reject()))
  .then((shipped) => {
    document.getElementById("shipped-version").textContent = `(version ${shipped.version})`;
    document.getElementById("shipped-id").textContent = shipped.id;
    if (!id.value) id.value = shipped.id;
  })
  .catch(() => {
    document.getElementById("shipped-version").textContent = "";
  });
async function pair(revoke) {
  if (!form.reportValidity()) return;
  document.getElementById("token-box").hidden = true;
  token.value = "";
  status.textContent = "Saving…";
  try {
    const response = await fetch("/api/extension-pair", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Babysit-Action": "extension-pair" },
      body: JSON.stringify({ id: id.value, revoke }),
    });
    const value = await response.json();
    if (!response.ok) throw Error(value.error);
    status.textContent = revoke ? "Access revoked." : "Paired. Save this token in the extension.";
    token.value = value.token || "";
    document.getElementById("token-box").hidden = revoke;
  } catch (error) {
    status.textContent = error.message;
  }
}
form.onsubmit = (event) => {
  event.preventDefault();
  void pair(false);
};
document.getElementById("revoke").onclick = () => void pair(true);
document.getElementById("copy-token").onclick = async () => {
  try {
    await navigator.clipboard.writeText(token.value);
    status.textContent = "Token copied.";
  } catch {
    token.select();
    status.textContent = "Select and copy the token above.";
  }
};
