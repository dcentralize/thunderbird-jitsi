var jitsiMeeting = class extends ChromeUtils.importESModule(
  "resource://gre/modules/ExtensionCommon.sys.mjs"
).ExtensionCommon.ExtensionAPI {
  onStartup() {
    this.editors = new Map();
    this.observer = document => this.addButton(document);
    Services.obs.addObserver(this.observer, "chrome-document-loaded");

    for (const window of Services.wm.getEnumerator(null)) {
      this.addButton(window.document);
      for (const iframe of window.document.querySelectorAll("iframe")) {
        if (iframe.contentDocument?.readyState === "complete") {
          this.addButton(iframe.contentDocument);
        }
      }
    }
  }

  getAPI(context) {
    const { ExtensionCommon } = ChromeUtils.importESModule(
      "resource://gre/modules/ExtensionCommon.sys.mjs"
    );
    return {
      jitsiMeeting: {
        configure: async settings => {
          this.settings = settings;
        },
        onOptionsRequested: new ExtensionCommon.EventManager({
          context,
          name: "jitsiMeeting.onOptionsRequested",
          register: fire => {
            this.requestOptions = () => fire.async();
            return () => {
              delete this.requestOptions;
            };
          },
        }).api(),
      },
    };
  }

  addButton(document) {
    if (
      document.documentURI !==
        "chrome://calendar/content/calendar-item-iframe.xhtml" ||
      this.editors.has(document)
    ) {
      return;
    }

    const window = document.defaultView;
    if (!window.arguments[0].calendarEvent.isEvent()) {
      return;
    }

    const location = document.getElementById("item-location");
    const title = document.getElementById("item-title");
    const button = document.createElementNS(
      "http://www.w3.org/1999/xhtml",
      "button"
    );
    button.id = "jitsi-meeting-link-button";
    button.type = "button";
    button.textContent = "Add Jitsi meeting";
    button.style.marginInlineStart = "0.5em";
    button.setAttribute("disable-on-readonly", "true");
    button.disabled = location.disabled || location.readOnly;

    let managedLocation;
    const setMeetingLocation = value => {
      const hashIndex = value.indexOf("#");
      const fragment = hashIndex === -1 ? "" : value.slice(hashIndex + 1);
      const parameters = fragment
        ? fragment.split("&").filter(part => part.split("=")[0] !== "config.localSubject")
        : [];
      if (this.settings.includeEventTitle) {
        // Jitsi normalizes punctuation before parsing JSON; transmit it as JSON escapes.
        const encodedTitle = encodeURIComponent(
          JSON.stringify(title.value).replace(/[&‘’“”]/g, character =>
            "\\u" + character.charCodeAt(0).toString(16).padStart(4, "0")
          )
        );
        parameters.push(`config.localSubject=${encodedTitle}`);
      }
      managedLocation =
        (hashIndex === -1 ? value : value.slice(0, hashIndex)) +
        (parameters.length ? "#" + parameters.join("&") : "");
      location.value = managedLocation;
      location.dispatchEvent(new window.Event("input", { bubbles: true }));
    };
    const onTitleInput = () => {
      if (location.value === managedLocation) {
        setMeetingLocation(location.value);
      }
    };

    const onClick = () => {
      if (!this.settings?.serverUrl) {
        this.requestOptions();
        return;
      }

      const { serverUrl, internalRoomPrefix, internalEmailDomains } =
        this.settings;
      if (window.URL.canParse(location.value)) {
        const current = new window.URL(location.value);
        const server = new window.URL(serverUrl);
        if (
          current.origin === server.origin &&
          current.pathname.startsWith(server.pathname) &&
          current.pathname.length > server.pathname.length
        ) {
          setMeetingLocation(location.value);
          return;
        }
      }

      let prefix = "";
      if (
        internalRoomPrefix &&
        internalEmailDomains.length > 0 &&
        window.attendees.length > 0 &&
        window.attendees.every(attendee => {
          const match = /^mailto:[^@]+@([^@]+)$/i.exec(attendee.id);
          return match && internalEmailDomains.includes(match[1].toLowerCase());
        })
      ) {
        const choice = Services.prompt.confirmEx(
          window,
          "Add Jitsi meeting",
          "All attendees match your internal email domains. Which meeting would you like?",
          Services.prompt.BUTTON_POS_0 * Services.prompt.BUTTON_TITLE_IS_STRING +
            Services.prompt.BUTTON_POS_1 * Services.prompt.BUTTON_TITLE_CANCEL +
            Services.prompt.BUTTON_POS_2 * Services.prompt.BUTTON_TITLE_IS_STRING,
          "Internal meeting (recommended)",
          null,
          "External meeting",
          null,
          {}
        );
        if (choice === 1) {
          return;
        }
        if (choice === 0) {
          prefix = internalRoomPrefix;
        }
      }

      setMeetingLocation(
        serverUrl + encodeURIComponent(prefix + window.crypto.randomUUID())
      );
    };
    const onUnload = () => this.editors.delete(document);
    button.addEventListener("click", onClick);
    title.addEventListener("input", onTitleInput);
    window.addEventListener("unload", onUnload, { once: true });
    location.after(button);
    this.editors.set(document, { button, onClick, title, onTitleInput, onUnload });
  }

  onShutdown(isAppShutdown) {
    Services.obs.removeObserver(this.observer, "chrome-document-loaded");
    for (const [
      document,
      { button, onClick, title, onTitleInput, onUnload },
    ] of this.editors) {
      button.removeEventListener("click", onClick);
      title.removeEventListener("input", onTitleInput);
      document.defaultView.removeEventListener("unload", onUnload);
      button.remove();
    }
    this.editors.clear();

    if (!isAppShutdown) {
      Services.obs.notifyObservers(null, "startupcache-invalidate");
    }
  }
};
