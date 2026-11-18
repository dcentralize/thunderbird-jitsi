const form = document.getElementById("settings-form");
const serverInput = document.getElementById("server-url");
const includeTitleInput = document.getElementById("include-event-title");
const prefixInput = document.getElementById("internal-room-prefix");
const domainsInput = document.getElementById("internal-email-domains");
const status = document.getElementById("status");

browser.storage.local.get("settings").then(({ settings }) => {
  if (settings) {
    serverInput.value = settings.serverUrl;
    includeTitleInput.checked = settings.includeEventTitle;
    prefixInput.value = settings.internalRoomPrefix;
    domainsInput.value = settings.internalEmailDomains.join("\n");
  }
});

for (const input of [serverInput, domainsInput]) {
  input.addEventListener("input", () => input.setCustomValidity(""));
}

form.addEventListener("submit", async event => {
  event.preventDefault();
  status.textContent = "";
  const serverValue = serverInput.value.trim();
  const server = new URL(serverValue);
  if (
    server.protocol !== "https:" ||
    server.username ||
    server.password ||
    serverValue.includes("?") ||
    serverValue.includes("#")
  ) {
    serverInput.setCustomValidity(
      "Enter an HTTPS server URL without credentials, a query or a fragment."
    );
    serverInput.reportValidity();
    return;
  }
  server.pathname = server.pathname.replace(/\/+$/, "") + "/";

  const internalEmailDomains = domainsInput.value
    .split(/\r?\n/)
    .map(domain => domain.trim().toLowerCase())
    .filter(Boolean);
  if (
    internalEmailDomains.some(
      domain =>
        !/^(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)*[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$/.test(domain)
    )
  ) {
    domainsInput.setCustomValidity(
      "Enter email domains only, one per line, for example example.org."
    );
    domainsInput.reportValidity();
    return;
  }

  await browser.storage.local.set({
    settings: {
      serverUrl: server.href,
      includeEventTitle: includeTitleInput.checked,
      internalRoomPrefix: prefixInput.value.trim(),
      internalEmailDomains,
    },
  });
  status.textContent = "Settings saved.";
});
