async function configure() {
  const { settings } = await browser.storage.local.get("settings");
  await browser.jitsiMeeting.configure(settings);
}

browser.jitsiMeeting.onOptionsRequested.addListener(() =>
  browser.runtime.openOptionsPage()
);
browser.storage.onChanged.addListener((changes, area) => {
  if (area === "local" && "settings" in changes) {
    configure();
  }
});
configure();
