# /// script
# requires-python = ">=3.11"
# dependencies = ["marionette-driver==3.7.1"]
# ///

import argparse
import json
import os
import socket
import subprocess
import tempfile
import uuid
from pathlib import Path
from urllib.parse import unquote, urlsplit
from zipfile import ZipFile

from marionette_driver.addons import Addons
from marionette_driver.keys import Keys
from marionette_driver.marionette import Marionette
from marionette_driver.wait import Wait


def editor(driver, script, script_args=()):
    return driver.execute_script(
        'const editor = document.getElementById("calendar-item-panel-iframe").contentDocument; '
        + script,
        script_args=script_args,
    )


def wait_editor(driver, title):
    Wait(driver, timeout=15).until(lambda _: len(driver.chrome_window_handles) == 2)
    driver.switch_to_window(driver.chrome_window_handles[-1])
    Wait(driver, timeout=15).until(
        lambda _: driver.execute_script(
            'const editor = document.getElementById("calendar-item-panel-iframe")?.contentDocument; '
            'return editor?.readyState == "complete" '
            f'&& editor.getElementById("item-title")?.value == {json.dumps(title)};',
        )
    )


def save(driver):
    editor(
        driver, 'editor.getElementById("notify-attendees-checkbox").checked = false;'
    )
    driver.find_element("id", "button-saveandclose").click()
    Wait(driver, timeout=15).until(lambda _: len(driver.chrome_window_handles) == 1)
    driver.switch_to_window(driver.chrome_window_handles[0])


def stored_events(driver):
    return driver.execute_async_script(
        """
        const done = arguments[arguments.length - 1];
        (async () => {
          const items = await cal.iterate.streamToArray(window.testCalendar.getItems(
            Ci.calICalendar.ITEM_FILTER_TYPE_EVENT, 0, null, null
          ));
          done(items.map(item => ({
            id: item.id,
            title: item.title,
            location: item.getProperty('LOCATION'),
            start: item.startDate.icalString,
            end: item.endDate.icalString,
            description: item.getProperty('DESCRIPTION'),
            attendees: item.getAttendees().map(attendee => attendee.id.toLowerCase()).sort()
          })));
        })();
        """
    )


def option(driver, label, role, *, value=None, click=False):
    return driver.execute_script(
        """
        const browser = document.getElementById('tabmail').currentTabInfo.browser;
        const [label, role, value, click] = arguments;
        function search(accessible) {
          if (!accessible) return null;
          if (accessible.name == label && window.testA11y.getStringRole(accessible.role) == role) {
            return accessible;
          }
          for (let child = accessible.firstChild; child; child = child.nextSibling) {
            const found = search(child);
            if (found) return found;
          }
          return null;
        }
        const control = search(window.testA11y.getAccessibleFor(browser));
        if (!control) return null;
        if (value !== null) {
          control.QueryInterface(Ci.nsIAccessibleEditableText).setTextContents(value);
        }
        if (click) control.doAction(0);
        const state = {};
        control.getState(state, {});
        return {
          name:control.name,
          value:control.value,
          checked:Boolean(state.value & Ci.nsIAccessibleStates.STATE_CHECKED)
        };
        """,
        script_args=[label, role, value, click],
    )


def open_options(driver, addon_id):
    driver.execute_async_script(
        """
        const done = arguments[arguments.length - 1];
        const { AddonManager } = ChromeUtils.importESModule(
          'resource://gre/modules/AddonManager.sys.mjs'
        );
        AddonManager.getAddonByID(arguments[0]).then(addon => {
          openContentTab(addon.optionsURL);
          done();
        });
        """,
        script_args=[addon_id],
    )
    Wait(driver, timeout=15).until(
        lambda _: option(driver, "Jitsi server URL", "entry") is not None
    )


def configure_page(driver, prefix="", domains="", *, include_event_title=None):
    Wait(driver, timeout=15).until(
        lambda _: option(driver, "Jitsi server URL", "entry") is not None
    )
    for label, value in (
        ("Jitsi server URL", "https://meet.example.test/team"),
        ("Internal room prefix (optional)", prefix),
        ("Internal email domains (optional)", domains),
    ):
        option(driver, label, "entry", value=value)
    if include_event_title is not None:
        checkbox = option(driver, "Include event title in meeting link", "checkbutton")
        if checkbox["checked"] != include_event_title:
            option(
                driver,
                "Include event title in meeting link",
                "checkbutton",
                click=True,
            )
    option(driver, "Save settings", "pushbutton", click=True)
    Wait(driver, timeout=15).until(
        lambda _: option(driver, "Settings saved.", "text leaf") is not None
    )
    driver.execute_script(
        "const tabs=document.getElementById('tabmail'); tabs.closeTab(tabs.currentTabInfo,true);"
    )


def new_event(driver, title, location=""):
    driver.execute_script(
        """
        const { CalEvent } = ChromeUtils.importESModule('resource:///modules/CalEvent.sys.mjs');
        const item = new CalEvent();
        item.title = arguments[0];
        item.startDate = cal.createDateTime('20261201T090000Z');
        item.endDate = cal.createDateTime('20261201T100000Z');
        item.setProperty('DESCRIPTION', 'Keep the agenda');
        item.setProperty('LOCATION', arguments[1]);
        item.calendar = window.testCalendar;
        createEventWithDialog(window.testCalendar, null, null, null, item);
        """,
        script_args=[title, location],
    )
    wait_editor(driver, title)


def invite(driver, *addresses):
    driver.execute_script(
        'window.setTimeout(() => document.getElementById("button-attendees").click(), 0);'
    )
    Wait(driver, timeout=15).until(lambda _: len(driver.chrome_window_handles) == 3)
    driver.switch_to_window(driver.chrome_window_handles[-1])
    Wait(driver, timeout=15).until(
        lambda _: driver.execute_script(
            'return !!document.querySelector("#attendee-list input");'
        )
    )
    for address in addresses:
        field = driver.execute_script(
            'return [...document.querySelectorAll("#attendee-list input")].find(input => !input.value);'
        )
        field.send_keys(address, Keys.TAB)
    driver.execute_script(
        'return document.querySelector("dialog").getButton("accept");'
    ).click()
    Wait(driver, timeout=15).until(lambda _: len(driver.chrome_window_handles) == 2)
    driver.switch_to_window(driver.chrome_window_handles[-1])


def rename(driver, title):
    editor(
        driver,
        """
        const accessible = Cc['@mozilla.org/accessibilityService;1']
          .getService(Ci.nsIAccessibilityService).getAccessibleFor(editor.getElementById('item-title'));
        accessible.QueryInterface(Ci.nsIAccessibleEditableText).setTextContents(arguments[0]);
        """,
        [title],
    )


def choose_meeting(driver, label, profile):
    # Native chrome prompts block Marionette requests; X11 drives the visible controls.
    point_file = profile / "prompt.json"
    point_file.unlink(missing_ok=True)
    driver.execute_script(
        """
        const [label, path] = arguments;
        const observer = subject => subject.addEventListener('load', () => {
          if (subject.document.documentURI != 'chrome://global/content/commonDialog.xhtml') return;
          Services.obs.removeObserver(observer, 'domwindowopened');
          subject.setTimeout(() => {
            const dialog = subject.document.querySelector('dialog');
            const button = ['accept','cancel','extra1','extra2'].map(name => dialog.getButton(name))
              .find(button => button.label == label);
            if (!button) {
              IOUtils.writeUTF8(path, JSON.stringify({error:'Missing prompt action: ' + label}));
              return;
            }
            const rect = button.getBoundingClientRect();
            IOUtils.writeUTF8(path, JSON.stringify({
              x:Math.round((subject.mozInnerScreenX + rect.x + rect.width / 2) * subject.devicePixelRatio),
              y:Math.round((subject.mozInnerScreenY + rect.y + rect.height / 2) * subject.devicePixelRatio)
            }));
          }, 0);
        }, {once:true});
        Services.obs.addObserver(observer, 'domwindowopened');
        const editor = document.getElementById('calendar-item-panel-iframe').contentDocument;
        editor.defaultView.setTimeout(() => [...editor.querySelectorAll('button')]
          .find(button => button.textContent.trim() == 'Add Jitsi meeting').click(), 0);
        """,
        script_args=[label or "Internal meeting (recommended)", str(point_file)],
    )
    Wait(driver, timeout=15).until(
        lambda _: point_file.exists() and point_file.stat().st_size > 0
    )
    point = json.loads(point_file.read_text())
    if "error" in point:
        subprocess.run(["xdotool", "key", "Escape"], check=True)
        raise AssertionError(point["error"])
    if label is None:
        subprocess.run(["xdotool", "key", "Escape"], check=True)
    else:
        subprocess.run(
            [
                "xdotool",
                "mousemove",
                "--sync",
                str(point["x"]),
                str(point["y"]),
                "click",
                "1",
            ],
            check=True,
        )
    Wait(driver, timeout=15).until(lambda _: len(driver.chrome_window_handles) == 2)
    driver.switch_to_window(driver.chrome_window_handles[-1])


def meeting_path(link, title, prefix=""):
    parsed = urlsplit(link)
    assert (parsed.scheme, parsed.netloc, parsed.query) == (
        "https",
        "meet.example.test",
        "",
    )
    identifier = uuid.UUID(parsed.path.removeprefix(f"/team/{prefix}"))
    assert identifier.version == 4
    assert parsed.path == f"/team/{prefix}{identifier}"
    if title is None:
        assert parsed.fragment == ""
    else:
        parameters = dict(part.split("=", 1) for part in parsed.fragment.split("&"))
        subject = unquote(parameters["config.localSubject"])
        subject = subject.replace(chr(92) * 2 + "&", "&", 1).translate(
            {0x2018: "'", 0x2019: "'", 0x201C: '"', 0x201D: '"'}
        )
        assert json.loads(subject) == title
    return parsed.path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--addon", type=Path, required=True)
    args = parser.parse_args()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    preferences = {
        "marionette.port": port,
        "mail.provider.suppress_dialog_on_startup": True,
        "mail.shell.checkDefaultClient": False,
        "mailnews.start_page.enabled": False,
        "app.update.enabled": False,
        "datareporting.policy.dataSubmissionPolicyBypassNotification": True,
        "mail.accountmanager.accounts": "account1",
        "mail.accountmanager.defaultaccount": "account1",
        "mail.accountmanager.localfoldersserver": "server1",
        "mail.account.account1.server": "server1",
        "mail.server.server1.type": "none",
        "mail.server.server1.hostname": "Local Folders",
        "mail.server.server1.userName": "nobody",
        "mail.server.server1.name": "Local Folders",
        "mail.server.server1.directory-rel": "[ProfD]Mail/Local Folders",
        "calendar.item.editInTab": False,
        "calendar.alarms.show": False,
        "calendar.alarms.onforevents": 0,
        "calendar.alarms.onfortodos": 0,
    }
    with tempfile.TemporaryDirectory(prefix="thunderbird-jitsi-test-") as directory:
        profile = Path(directory)
        (profile / "user.js").write_text(
            "".join(
                f"user_pref({json.dumps(key)}, {json.dumps(value)});\n"
                for key, value in preferences.items()
            )
        )
        with (profile / "thunderbird.log").open("w") as log:
            process = subprocess.Popen(
                [
                    str(args.binary.resolve()),
                    "--no-remote",
                    "--profile",
                    directory,
                    "--marionette",
                ],
                stdout=log,
                stderr=subprocess.STDOUT,
                env={
                    **os.environ,
                    "MOZ_DISABLE_CONTENT_SANDBOX": "1",
                    "MOZ_REMOTE_ALLOW_SYSTEM_ACCESS": "1",
                },
            )
            try:
                driver = Marionette(port=port, socket_timeout=30)
                driver.start_session(timeout=40)
                driver.set_context("chrome")
                addon_id = Addons(driver).install(str(args.addon.resolve()))
                driver.execute_script(
                    """
                    window.testA11y = Cc['@mozilla.org/accessibilityService;1']
                      .getService(Ci.nsIAccessibilityService);
                    const { MailServices } = ChromeUtils.importESModule(
                      'resource:///modules/MailServices.sys.mjs'
                    );
                    Services.io.offline = true;
                    const account = MailServices.accounts.createAccount();
                    account.incomingServer = MailServices.accounts.createIncomingServer(
                      'test', 'mail.invalid', 'imap'
                    );
                    MailServices.accounts.defaultAccount = account;
                    const other = MailServices.accounts.createIdentity();
                    other.email = 'default@outside.test';
                    account.addIdentity(other);
                    account.defaultIdentity = other;
                    const selected = MailServices.accounts.createIdentity();
                    selected.email = 'selected@example.test';
                    account.addIdentity(selected);
                    window.testCalendar = cal.manager.createCalendar(
                      'storage', Services.io.newURI('moz-storage-calendar://')
                    );
                    window.testCalendar.id = Services.uuid.generateUUID().toString().slice(1, -1);
                    window.testCalendar.name = 'Integration calendar';
                    window.testCalendar.setProperty('imip.identity.key', selected.key);
                    cal.manager.registerCalendar(window.testCalendar);
                    """
                )
                expected = {}
                paths = []
                click = (
                    '[...editor.querySelectorAll("button")].find('
                    'button => button.textContent.trim() == "Add Jitsi meeting").click();'
                )
                for title in ("Customer discussion", "Planning call"):
                    new_event(driver, title)
                    assert (
                        editor(
                            driver,
                            'return editor.getElementById("item-location").value;',
                        )
                        == ""
                    )
                    if not expected:
                        editor(driver, click)
                        driver.switch_to_window(driver.chrome_window_handles[0])
                        Wait(driver, timeout=15).until(
                            lambda _: driver.execute_script(
                                "return document.getElementById('tabmail').tabInfo.some("
                                "tab => tab.browser?.currentURI.spec.endsWith('/options/options.html'));"
                            )
                        )
                        Wait(driver, timeout=15).until(
                            lambda _: (
                                option(driver, "Jitsi server URL", "entry") is not None
                            )
                        )
                        for label in (
                            "Jitsi server URL",
                            "Internal room prefix (optional)",
                            "Internal email domains (optional)",
                        ):
                            assert option(driver, label, "entry")["value"] == ""
                        checkbox = option(
                            driver, "Include event title in meeting link", "checkbutton"
                        )
                        assert checkbox is not None, (
                            "The title preference must be visible"
                        )
                        assert checkbox["checked"] is True
                        configure_page(driver)
                        driver.switch_to_window(driver.chrome_window_handles[-1])
                        assert (
                            editor(
                                driver,
                                'return editor.getElementById("item-location").value;',
                            )
                            == ""
                        )
                    editor(driver, click)
                    link = editor(
                        driver, 'return editor.getElementById("item-location").value;'
                    )
                    paths.append(meeting_path(link, title))
                    editor(driver, click)
                    assert (
                        editor(
                            driver,
                            'return editor.getElementById("item-location").value;',
                        )
                        == link
                    )
                    save(driver)
                    expected[title] = {
                        "title": title,
                        "location": link,
                        "start": "20261201T090000Z",
                        "end": "20261201T100000Z",
                        "description": "Keep the agenda",
                        "attendees": [],
                    }
                assert paths[0] != paths[1], "Separate events must use different rooms"
                open_options(driver, addon_id)
                configure_page(driver, "dc-", "EXAMPLE.TEST")
                for title, addresses, choice, prefix in (
                    (
                        "Internal planning",
                        ["alice@example.test"],
                        "Internal meeting (recommended)",
                        "dc-",
                    ),
                    ("External choice", ["alice@example.test"], "External meeting", ""),
                    (
                        "Guest discussion",
                        ["alice@example.test", "guest@sub.example.test"],
                        None,
                        "",
                    ),
                ):
                    new_event(driver, title)
                    invite(driver, *addresses)
                    if prefix:
                        choose_meeting(driver, "Cancel", profile)
                        assert (
                            editor(
                                driver,
                                'return editor.getElementById("item-location").value;',
                            )
                            == ""
                        )
                        choose_meeting(driver, None, profile)
                        assert (
                            editor(
                                driver,
                                'return editor.getElementById("item-location").value;',
                            )
                            == ""
                        ), "Escape must cancel without generating a room"
                    if choice:
                        choose_meeting(driver, choice, profile)
                    else:
                        editor(driver, click)
                    link = editor(
                        driver, 'return editor.getElementById("item-location").value;'
                    )
                    path = meeting_path(link, title, prefix)
                    if prefix:
                        title = "Café ‘team’ “plan” & C:\\notes \\& details"
                        rename(driver, title)
                        Wait(driver, timeout=15).until(
                            lambda _, previous=link: (
                                editor(
                                    driver,
                                    'return editor.getElementById("item-location").value;',
                                )
                                != previous
                            )
                        )
                        link = editor(
                            driver,
                            'return editor.getElementById("item-location").value;',
                        )
                        assert meeting_path(link, title, prefix) == path
                        invite(driver, "later@outside.test")
                        addresses.append("later@outside.test")
                        assert (
                            editor(
                                driver,
                                'return editor.getElementById("item-location").value;',
                            )
                            == link
                        )
                        editor(driver, click)
                        assert (
                            editor(
                                driver,
                                'return editor.getElementById("item-location").value;',
                            )
                            == link
                        )
                    save(driver)
                    expected[title] = {
                        "title": title,
                        "location": link,
                        "start": "20261201T090000Z",
                        "end": "20261201T100000Z",
                        "description": "Keep the agenda",
                        "attendees": sorted(
                            "mailto:" + address
                            for address in ["selected@example.test", *addresses]
                        ),
                    }
                manual = "https://other.example.test/manual-room"
                new_event(driver, "Manual meeting", manual)
                rename(driver, "Manual meeting renamed")
                assert (
                    editor(
                        driver, 'return editor.getElementById("item-location").value;'
                    )
                    == manual
                )
                save(driver)
                expected["Manual meeting renamed"] = {
                    "title": "Manual meeting renamed",
                    "location": manual,
                    "start": "20261201T090000Z",
                    "end": "20261201T100000Z",
                    "description": "Keep the agenda",
                    "attendees": [],
                }
                open_options(driver, addon_id)
                configure_page(driver, "dc-", "example.test", include_event_title=False)
                open_options(driver, addon_id)
                assert (
                    option(
                        driver, "Include event title in meeting link", "checkbutton"
                    )["checked"]
                    is False
                ), "Disabling titles must survive reopening settings"
                driver.execute_script(
                    "const tabs=document.getElementById('tabmail'); tabs.closeTab(tabs.currentTabInfo,true);"
                )
                for title, addresses, prefix in (
                    ("Private customer discussion", [], ""),
                    ("Private internal planning", ["alice@example.test"], "dc-"),
                ):
                    new_event(driver, title)
                    if addresses:
                        invite(driver, *addresses)
                        choose_meeting(
                            driver, "Internal meeting (recommended)", profile
                        )
                    else:
                        editor(driver, click)
                    link = editor(
                        driver, 'return editor.getElementById("item-location").value;'
                    )
                    meeting_path(link, None, prefix)
                    title += " renamed"
                    rename(driver, title)
                    assert (
                        editor(
                            driver,
                            'return editor.getElementById("item-location").value;',
                        )
                        == link
                    ), "Editing a title must keep the untitled room link"
                    save(driver)
                    expected[title] = {
                        **expected["Customer discussion"],
                        "title": title,
                        "location": link,
                        "attendees": sorted(
                            "mailto:" + address
                            for address in (
                                ["selected@example.test", *addresses]
                                if addresses
                                else []
                            )
                        ),
                    }
                existing_path = "/team/00112233-4455-4677-8899-aabbccddeeff"
                other_parameters = [
                    "config.startWithAudioMuted=true",
                    "userInfo.displayName=%22Guest%20%26%20one%22",
                ]
                untitled = f"https://meet.example.test{existing_path}#{'&'.join(other_parameters)}"
                titled = untitled.replace(
                    "&", "&config.localSubject=%22Original%20title%22&", 1
                )
                title = "Existing meeting"
                new_event(driver, title, titled)
                editor(driver, click)
                assert (
                    editor(
                        driver, 'return editor.getElementById("item-location").value;'
                    )
                    == untitled
                ), "Only the event-title parameter must be removed"
                title = "Existing meeting renamed"
                rename(driver, title)
                assert (
                    editor(
                        driver, 'return editor.getElementById("item-location").value;'
                    )
                    == untitled
                )
                driver.switch_to_window(driver.chrome_window_handles[0])
                open_options(driver, addon_id)
                configure_page(driver, "dc-", "example.test", include_event_title=True)
                driver.switch_to_window(driver.chrome_window_handles[-1])
                editor(driver, click)
                restored = editor(
                    driver, 'return editor.getElementById("item-location").value;'
                )
                assert meeting_path(restored, title) == existing_path
                assert [
                    part
                    for part in urlsplit(restored).fragment.split("&")
                    if part.split("=", 1)[0] != "config.localSubject"
                ] == other_parameters
                save(driver)
                expected[title] = {
                    **expected["Customer discussion"],
                    "title": title,
                    "location": restored,
                    "attendees": [],
                }
                saved = stored_events(driver)
                assert {
                    item["title"]: {k: v for k, v in item.items() if k != "id"}
                    for item in saved
                } == expected
                first = next(
                    item for item in saved if item["title"] == "Customer discussion"
                )
                driver.execute_async_script(
                    """
                    const done = arguments[arguments.length - 1];
                    window.testCalendar.getItem(arguments[0]).then(item => {
                      modifyEventWithDialog(item, false);
                      done();
                    });
                    """,
                    script_args=[first["id"]],
                )
                wait_editor(driver, "Customer discussion")
                link = expected["Customer discussion"]["location"]
                assert (
                    editor(
                        driver, 'return editor.getElementById("item-location").value;'
                    )
                    == link
                )
                editor(driver, click)
                assert (
                    editor(
                        driver, 'return editor.getElementById("item-location").value;'
                    )
                    == link
                )
                driver.execute_async_script(
                    """
                    const done = arguments[arguments.length - 1];
                    const { AddonManager } = ChromeUtils.importESModule(
                      'resource://gre/modules/AddonManager.sys.mjs'
                    );
                    AddonManager.getAddonByID(arguments[0]).then(addon => addon.disable()).then(done);
                    """,
                    script_args=[addon_id],
                )
                assert (
                    editor(
                        driver,
                        'return [...editor.querySelectorAll("button")].some(button => button.textContent.trim() == "Add Jitsi meeting");',
                    )
                    is False
                )
                assert (
                    editor(
                        driver, 'return editor.getElementById("item-location").value;'
                    )
                    == link
                )
                save(driver)
                assert {item["id"]: item for item in stored_events(driver)} == {
                    item["id"]: item for item in saved
                }
                Addons(driver).uninstall(addon_id)
                legacy = profile / "legacy-settings.xpi"
                with ZipFile(legacy, "w") as archive:
                    archive.writestr(
                        "manifest.json",
                        json.dumps(
                            {
                                "manifest_version": 2,
                                "name": "Legacy settings fixture",
                                "version": "0.2.0",
                                "browser_specific_settings": {
                                    "gecko": {
                                        "id": addon_id,
                                        "strict_min_version": "159.0",
                                    }
                                },
                                "permissions": ["storage"],
                                "background": {"scripts": ["seed.js"]},
                                "options_ui": {
                                    "page": "ready.html",
                                    "open_in_tab": True,
                                },
                            }
                        ),
                    )
                    archive.writestr(
                        "seed.js",
                        "browser.storage.local.set({settings:{"
                        "serverUrl:'https://meet.example.test/team/',"
                        "internalRoomPrefix:'dc-',internalEmailDomains:['example.test']"
                        "}}).then(() => browser.runtime.openOptionsPage());",
                    )
                    archive.writestr(
                        "ready.html", "<!doctype html><p>Legacy settings saved</p>"
                    )
                Addons(driver).install(str(legacy))
                Wait(driver, timeout=15).until(
                    lambda _: (
                        option(driver, "Legacy settings saved", "text leaf") is not None
                    )
                )
                driver.execute_script(
                    "const tabs=document.getElementById('tabmail'); tabs.closeTab(tabs.currentTabInfo,true);"
                )
                Addons(driver).install(str(args.addon.resolve()))
                open_options(driver, addon_id)
                assert (
                    option(
                        driver, "Include event title in meeting link", "checkbutton"
                    )["checked"]
                    is True
                )
                driver.execute_script(
                    "const tabs=document.getElementById('tabmail'); tabs.closeTab(tabs.currentTabInfo,true);"
                )
                new_event(driver, "Upgraded meeting")
                editor(driver, click)
                meeting_path(
                    editor(
                        driver, 'return editor.getElementById("item-location").value;'
                    ),
                    "Upgraded meeting",
                )
                save(driver)
                Addons(driver).uninstall(addon_id)
                driver.delete_session()
                print(
                    "PASS packaged options, title preference persistence and legacy settings upgrade, custom server, internal suggestions, cancel/Escape, UUID rooms, guest titles, native attendee edits, save/reopen and disable cleanup"
                )
            finally:
                process.terminate()
                process.wait(timeout=10)


if __name__ == "__main__":
    main()
