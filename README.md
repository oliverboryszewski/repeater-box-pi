# Repeater Box — Pi dashboard

Software for a Pi 4, two NA6D AIOCs, and two Baofeng radios. Version 0.14.0.

Source: [GitHub repository](https://github.com/oliverboryszewski/repeater-box-pi).

The box creates its own password-protected **Repeater-Box** Wi-Fi network. Connect any phone, laptop, or iPad, then visit **http://10.42.0.1**. No dedicated tablet or installed client app is needed. The dashboard runs locally and its assets work without internet.

This package is ready for installation on a fresh Pi OS card. Automated checks verify the audio pipeline, recording, archive playback, and admin controls. Installation, hotspot operation, AIOC audio, and radio behavior require verification on the target Pi. This is an installable software bundle; writing the card and the initial SSH setup below are required for a new installation.

## What this version does

| Dashboard tab | Available features |
|---|---|
| **Overview** | Pi temperature, uptime, storage, voltage flags, network/service status, and USB audio/serial inventory |
| **Live audio** | Start/stop listening over Wi-Fi, with local listening volume |
| **Data** | Admin-only SSTV Robot 36 live preview and PNG gallery with downloads and confirmed deletion, APRS packet decoding and log downloads |
| **Audio archives** | Browse, play, and download saved clips; admin-controlled recording and confirmed deletion |
| **Settings / Admin** | Password sign-in, station/audio toggle switches, archive limits, Wi-Fi broadcast visibility, optional DTMF status/recording control, on-demand radio programming, and software updates |

Phones, laptops, and iPads use the same responsive page. Overview, live listening, and archive playback are available to anyone connected to the trusted local network. Changing settings or recording requires the dashboard admin password. The Wi-Fi and admin passwords are separate.

Settings / Admin shows the sign-in card first; administration options appear only after sign-in and hide again when the session ends.

The initial installation starts a **hardware-free SvxLink bench engine**. It does not enable radio reception, PTT, repeating, station identification, DTMF commands, or timed messages. Live listening and recording become useful after mapping the RX AIOC and commissioning the separate receive-only monitor described below. USB detection alone does not confirm receiver audio. Unavailable Pi measurements remain unavailable, and power-bank charge percentage is not measured.

Radio frequencies and tones come from a private channel-pair catalog configured on the Pi. The public package contains no default pair or deployment-location list. The dashboard displays configured values, rather than continuous readings from the radios. The optional programming workflow applies the selected local plan after inspection and confirmation. TX power is High; the RX radio's transmit function is inhibited on its box channel.

## 1. Before the card arrives

1. Install [Raspberry Pi Imager](https://www.raspberrypi.com/software/) on the Mac if needed.
2. Have the Pi 4, its wall power supply, and an Ethernet cable ready. Ethernet from the Pi to your home router is the simplest initial path and keeps access working when Wi-Fi switches to hotspot mode.
3. Choose a Pi account password, a Wi-Fi hotspot password, and a separate dashboard admin password of at least 6 characters. Enter them locally in Imager / on the Pi; none belongs in this bundle or chat.

If Ethernet is unavailable, configure home Wi-Fi in Imager for the initial download/install. Use a keyboard and monitor attached to the Pi when switching that single Wi-Fi interface to hotspot mode. The hotspot script refuses a recognized SSH connection over wlan0. A second Wi-Fi adapter is not required for normal offline use.

## 2. Write the new SD card

In Imager select:

- **Device:** Raspberry Pi 4.
- **OS:** Raspberry Pi OS Lite (64-bit), the current Debian Trixie version under the other/Lite OS choices. Do not choose the PC/Mac Raspberry Pi Desktop image.
- **Storage:** the new microSD card, carefully identified. Writing the image erases the selected card.
- **Hostname:** `repeater-box`.
- **Username:** choose an account name; replace `your_username` in the SSH example below.
- **Locale:** select your local time zone and actual wireless country.
- **Remote access:** enable SSH; use your SSH public key if you have one, otherwise a strong account password.
- **Wi-Fi:** optional home network for first setup, if Ethernet will not provide the connection.

The current official Lite 64-bit release supports the Pi 4; [OS choices](https://www.raspberrypi.com/software/operating-systems/) and [Imager setup instructions](https://www.raspberrypi.com/documentation/computers/getting-started.html) are the primary references. Imager menus can change, so use the named settings rather than an old gear-icon tutorial.

Write and verify the image, eject the card, insert it into the unpowered Pi, connect Ethernet, then power the Pi using the wall supply. Leave the radios disconnected for this stage. Allow a few minutes for first boot.

## 3. Download and install this package

Run this on your **computer**, in a terminal, replacing `your_username`:

```bash
ssh your_username@repeater-box.local
```

If `.local` cannot be resolved, find the Pi's Ethernet address in your router's connected-device list and substitute it. SSH may ask you to verify a new host key on the first connection.

Now run these on the **Pi**, in that SSH session:

```bash
mkdir -p ~/Repeater_Box_Pi
curl --fail --location --proto '=https' https://github.com/oliverboryszewski/repeater-box-pi/archive/refs/heads/main.tar.gz -o /tmp/repeater-box-pi.tar.gz
tar -xzf /tmp/repeater-box-pi.tar.gz --strip-components=1 -C ~/Repeater_Box_Pi
cd ~/Repeater_Box_Pi
sudo bash scripts/install-pi.sh
```

Internet is required for this installation. The script installs Debian packages, a Python standard-library dashboard, the bench engine, a separate receive-only monitor service, and a pinned CHIRP library with pyserial for optional on-demand programming. The monitor is installed disabled. It masks the vendor `svxlink.service` before installing the radio package so its unrelated sample configuration cannot start. Our separate engine is fixed to a hardware-free bench configuration. The installer leaves any already-running vendor SvxLink installation alone and stops with an explanation instead.

Open **http://repeater-box.local** on a device on your home network. The Pi's numeric Ethernet address also works. Check that Pi readings appear and that the repeater engine shows Active. There should be no live radio reception, recordings, or PTT activity yet.

Set the dashboard admin password locally on the Pi:

```bash
sudo python3 /opt/repeater-box/dashboard/admin_password.py
```

The prompt asks twice without displaying the password. In **Settings / Admin**, sign in with that password to unlock recording and station settings. A dashboard-service restart signs browsers out. If the password is lost, rerun the same local command to replace it; it also invalidates existing sessions.

The Pi dashboard uses standard HTTP port **80**, so its URL needs no port suffix. It runs as the unprivileged `repeaterdash` account, with only `CAP_NET_BIND_SERVICE` supplied by systemd to bind that port. Settings persist under `/var/lib/repeater-dashboard`; the salted admin password hash is stored in `/etc/repeater-box/admin.json` with restricted permissions. The archive directory `/var/lib/svxlink/recordings` is shared through the `repeater-recordings` group with mode `2770`; the dashboard service is allowed to write there through its systemd `ReadWritePaths` setting.

## 4. Create the portable Wi-Fi network

Keep the Pi connected by **Ethernet**, and stay in the Ethernet SSH session. Alternatively use a local keyboard/monitor. Then run on the Pi:

```bash
cd ~/Repeater_Box_Pi
sudo --preserve-env=SSH_CONNECTION bash scripts/configure-hotspot.sh
```

Enter the new hotspot password twice when prompted. It is stored by NetworkManager on the Pi; the script does not print it. The profile uses WPA2/CCMP, 2.4 GHz channel 6, automatic startup, and address 10.42.0.1. It uses NetworkManager's shared mode for client addresses. Review the subnet first if your home network also uses 10.42.0.0/24. The script detects that subnet on a Pi interface but cannot detect every upstream routed network.

On your phone, laptop or iPad:

1. Join **Repeater-Box** using the password you just chose.
2. Choose **Stay connected / Use without internet** if prompted.
3. Type **http://10.42.0.1** into the browser address bar, including `http://`.

The dashboard is a normal browser page, not a captive-portal login page. You may need to switch back to your usual Wi-Fi for internet access. To test portable operation, disconnect the Pi's Ethernet cable while leaving it powered; the dashboard should continue working over its Wi-Fi. It may take a refresh if your device briefly switches networks.

The Pi's built-in Wi-Fi is used for the access point. This setup does not assume it can also connect to home Wi-Fi simultaneously. Use Ethernet for later package updates; a second Wi-Fi adapter is an optional future addition. Raspberry Pi documents this [hotspot approach](https://www.raspberrypi.com/tutorials/host-a-hotel-wifi-hotspot/) and [current networking configuration](https://www.raspberrypi.com/documentation/computers/configuration.html); property definitions are in [NetworkManager's manual](https://www.networkmanager.dev/docs/api/latest/nm-settings-nmcli.html).

The dashboard trusts the protected local network. Anyone given access to it can view status, listen to the enabled receiver feed, and play/download recordings. Admin sign-in is required for recording and settings changes. The dashboard also listens on Ethernet for initial setup; keep it on your trusted local network and do not forward its port to the public internet.

## 5. Confirm a clean boot

On the Pi:

```bash
systemctl --no-pager status repeater-dashboard.service repeater-engine.service
sudo reboot
```

Reconnect to Repeater-Box and reopen the dashboard. Confirm that the hotspot, dashboard, and bench engine returned. The receive-only monitor should remain inactive until deliberately commissioned. An offline Pi 4 has no battery-backed clock in this build; recording timestamps may be wrong after extended power loss until time synchronization is restored over Ethernet. The dashboard shows clock synchronization state. A future RTC or browser time-setting feature can address this if needed.

Use a clean shutdown before disconnecting Pi power:

```bash
sudo shutdown -h now
```

Wait for shutdown to complete before removing power. Use the wall adapter for the first setup; test the power-bank arrangement afterward and check the Pi voltage flags.

## When the AIOCs arrive

Connect and label **one interface at a time**, initially with its radio unplugged. The dashboard lists USB capture/playback devices and `/dev/serial/by-id/` paths. It does not open those serial ports. Run the read-only inventory if needed:

```bash
bash ~/Repeater_Box_Pi/scripts/diagnostics.sh
```

Follow [the commissioning notes](config/COMMISSIONING.md) to map the real devices and establish stable RX/TX identities. The default bench service rejects any active logic that loses `RX=NONE`, `TX=NONE`, or `ONLINE=0`.

For UV-5RX radios, follow **[On-demand radio programming](config/RADIO_PROGRAMMING.md)** before commissioning audio. This uses the CHIRP **Baofeng / 5RX** profile, a private local RX/TX device mapping, an initial read-only inspection, and a fresh backup before each confirmed write. Programming never runs at boot. Original UV-5R images and the different UV-5RX3 profile must not be substituted for the box radios.

On a fresh installation, configure the private channel catalog from the Pi terminal before programming either box radio:

```bash
sudo python3 /opt/repeater-box/scripts/configure-channel-pairs.py
```

The interactive prompts accept one to nine locally chosen pairs and their tones. The catalog stays in `/etc/repeater-box/channel-pairs.json`; keep its contents, device identities and radio images out of the public checkout. The first pair is the default. Radio labels are assigned by catalog order: `BOX-RX1` / `BOX-TX1`, `BOX-RX2` / `BOX-TX2`, and so on. Establish local channel suitability before entering values. This command saves configuration only; it does not program radios or start transmission. If the catalog is absent, the dashboard requests local configuration and disables plan-based programming and pair presets.

The separate **[Radio editor](config/RADIO_EDITOR.md)** is available directly at **http://10.42.0.1/radio-editor** after installation. It has no link in the normal dashboard and requires admin sign-in for radio data and changes. It edits channel memories and selected operating settings through the same backup, confirmation and verification workflow. Failed verification still allows a fresh editor read and reviewed recovery. A locally configured external USB programming cable adds a separate External programmer option for other UV-5RX/5RX handhelds or original UV-5Rs with the reviewed `HN5RV011` revision. Select the exact external model, then read each newly connected radio before editing. Changing models clears the loaded snapshot, and an unfinished upload locks its saved model until recovery succeeds; RX/TX assignments and their 5RX profile stay independent. Private channel-pair presets stage matching RX, TX or handheld memories from the Pi's catalog for review. See the editor guide for configuration and recovery details.

Inspect, Program and Verify show the current step, measured block progress and a bounded activity log. While a task is active the page polls a lightweight status endpoint every second; other dashboard measurements retain their normal refresh interval. Progress percentages describe the current step rather than a promised completion time.

Writes use a separate upload connection with a new identification exchange, matching the pinned CHIRP driver's upload-session setup. The logical field allowlist is unchanged. Channel and global-setting transfers cover 32 bytes; name transfers cover a contiguous 256-byte group. Each transfer uses consecutive 16-byte frames and preserves all companion bytes from the same radio’s candidate. These transfer sizes do not establish the firmware’s internal erase geometry. The worker leaves five quiet seconds before reusing the same radio after a clone session, including the later fresh read used for verification. Quiet time can overlap work on its partner radio. Leave the radio powered during routine reads, writes and verification; a manual power cycle is only a recovery option if it becomes unresponsive or cannot reconnect. Turn a radio off before attaching or removing its K1 plug. The optional **[paired-change workflow](config/RADIO_PAIR_SWITCHING.md)** adds an admin dropdown, a confirmation and a fresh password check, then backs up both radios and programs/verifies them sequentially. It keeps both radios on working memory 1 and preserves other memories. It is disabled until both radios have passed local reload checks; a name change or stored configuration readback does not prove their active RF frequency.

For the next useful stage, follow **[Live RX audio over Wi-Fi](config/LIVE_AUDIO.md)**. It walks through assigning the receiving AIOC, replacing the RX placeholder in the monitor configuration, stopping the bench engine, and starting the installed-but-disabled `repeater-rx-monitor.service`. Keep the TX radio disconnected during this receive-only phase. The monitor uses a dummy transmitter and does not open a PTT port.

Then sign in under **Settings / Admin**, select **Enable receiver audio feed**, and save. That browser setting makes the dashboard accept the already configured local receiver feed; it does not start SvxLink, select an AIOC, change radio programming, or enable transmission. In **Live audio**, tap **Start listening** to allow browser playback. The volume slider changes playback on that device only. A short delay is normal; locking or backgrounding a phone may suspend its browser audio.

The feed contains **incoming RX speaker audio**, sent locally by SvxLink as mono **48 kHz float32 little-endian PCM**. It is tapped before SvxLink software squelch, gain processing, and DTMF muting, so hardware squelch/CTCSS and speaker level matter. Control tones or idle noise passed by the radio may be audible. Outgoing IDs and transmit messages are not included. [The live-audio guide](config/LIVE_AUDIO.md#primary-references) links the official SvxLink source and configuration references behind this arrangement.

Optional **[radio DTMF control](config/DTMF_CONTROL.md)** provides option **0** to read the configured pair and all three control states, **1** for status announcements, **2** for recording and **3** for automatic identification. It is off by default and uses a separate six-character password, configured in Admin or privately on the Pi. Its alphabet is `0–9` and uppercase `A–D`; `#` submits a step and `*` cancels. SvxLink decodes the received tones using its existing audio device; the dashboard does not open another AIOC input. The receive-only monitor keeps `TX=NONE` and `NO_REPEAT=1`, so commands do not produce an on-air voice response at this stage.

After separate receive-audio and transmit key/release/audio tests pass, follow **[Live repeater commissioning](config/LIVE_REPEATER.md)**. The separate `repeater-live.service` uses private station configuration, synthesized CW identification, the existing receiver feed and restricted DTMF menu. Installation leaves live RF stopped; activation is a deliberate local terminal step. Radio programming pauses this service and leaves it paused if verification or connection release fails. Remote pair changes can resume the previously active live service only after both radios verify against the authorized private catalog.

Optional **[Live data decoding](config/DATA_AUDIO.md)** adds receive-only SSTV Robot 36 pictures and APRS / AX.25 AFSK1200 packets to the separate **Data** tab. Both share the existing raw receiver feed, work offline after their one-time native decoder installations, and remain independent of listening and audio recording. Admins can view pictures as scanlines arrive, browse and download complete or clearly marked partial PNGs, and export bounded packet logs. Decoding does not retune the radio, transmit data or reconstruct arbitrary files.

**Automatic station identification** has its own confirmed Admin switch, on by default. Turning it off saves a manual-identification preference across restarts and leaves spoken DTMF prompts enabled. An ID already playing may finish. Automatic scheduling groups nearby speaker turns into a conversation, waits for a safe idle gap, and suppresses Morse during the radio menu. The local default interval is nine minutes; an explicit 30-minute testing profile is available. The operator remains responsible for identification on the transmitting channel, including when automatic identification is off or testing mode is selected.

## Recording and station settings

Admin groups controls into **Daily operation**, **Audio and recording**, **Network**, **Maintenance**, and **Radio tools**. Normal controls stay accessible; completed programming tools and longer setup explanations collapse. Pending work, errors and recovery automatically reveal the relevant tools and progress. Unavailable controls leave concise setup guidance instead of inactive buttons. All admin options remain hidden until sign-in.

In **Audio archives**, an admin can turn **Record traffic** on or off. **Armed** means recording is enabled but waiting for receiver audio above the activity threshold; **Recording** means a clip is currently being saved. Stopping recording finalizes the current clip. Finalized files appear for playback/download; the current `.wav.part` file is not offered as an archive.

Listening and recording are independent. Recording can continue with no browser open, and listening works while recording is off. This dashboard uses its own audio-level-triggered recorder, sharing the one RX feed instead of opening the AIOC a second time. Clips are **48 kHz mono, 16-bit PCM WAV**, with a **500 ms pre-roll**, approximately **3 seconds of quiet hang time**, and a **15-minute maximum clip length**. At low disk space the recorder rotates the oldest closed RX clips, and pauses if it cannot recover the **256 MiB** free-space floor.

Admin settings apply to this Pi and persist across restarts:

The receiver-feed and automatic-recording controls are toggle switches. Select **Save settings** to apply them. The Wi-Fi broadcast switch has its own confirmation dialog and applies separately.

| Setting | Default and behavior |
|---|---|
| Station name | `Repeater Box`; 1–48 printable characters |
| Enable receiver audio feed | Off; accepts the configured localhost audio source when enabled |
| Record automatically after a restart | Off; selects recording state when the dashboard service next starts |
| Keep recordings indefinitely | Off; when enabled, skips age expiry while retaining storage limits |
| Keep recordings for | 7 days; adjustable from 1–90 days |
| Archive storage limit | 4096 MiB; adjustable from 64–16384 MiB (shown as MB in the dashboard) |
| Recording threshold | −45 dBFS; slider from −70 to −10 dBFS with sensitivity descriptions |

The Archives switch changes the recording state **now**; it does not change the startup preference. Lower recording thresholds catch quieter speech but can also record noise. Higher thresholds require louder audio and may miss quiet speech. The slider shows the selected dBFS value and a sensitivity description; select **Save settings** to apply it.

Admins can delete an individual saved clip from **Audio archives**, or a saved picture from **Data**. Each Delete button opens a confirmation identifying the selected item. The server requires an authenticated session and CSRF protection, rechecks the reviewed file's identity, and refuses active recordings or pictures still being received. Picture deletion also removes its private gallery metadata. Deletion is permanent and does not stop the live repeater or change recording preferences. Public listeners cannot delete files. If another operation removes or changes an item before confirmation, refresh and review the current list.

The separate **Radio DTMF control** card has an **Enable DTMF control** switch with confirmation and a **Change DTMF password** form requiring the current admin password. The RF menu requires login, an option, then off/on as separate submitted transmissions. It permits recording control and the **Spoken status after DTMF actions** preference; it does not change channel pairs, program radios, enable transmission or run arbitrary commands. Recording commands affect the current recorder state and leave **Record automatically after a restart** unchanged. A DTMF password is sent as audible radio tones and can be heard or recorded; choose a different password from the Wi-Fi and web admin passwords.

Enable **Keep recordings indefinitely** to turn off deletion by age. The day-count field is disabled while this is enabled, and its previous value is retained. The archive storage limit still applies. If the Pi falls below the 256 MiB free-space floor, the recorder removes the oldest closed dashboard RX clips first to recover space, aiming for an additional 16 MiB of headroom. It leaves the active clip, unrelated files, and symbolic links alone. If eligible clips cannot recover enough space, recording pauses while live listening continues. Disabling indefinite retention resumes the selected age limit and may remove older clips immediately. Limits are checked periodically and when clips close; the archive cap is not a disk partition.

The archive directory also supports playback of existing WAV, OGG, FLAC, or MP3 files. The commented SvxLink native QSO-recorder configuration remains an optional example: it is not enabled. Do not enable it alongside the dashboard recorder unless duplicate archives are intentional. Native QSO recordings and the dashboard's RX clips use different audio paths and controls.

The station/audio settings form does not program the radios. The separate **Radio programming** card applies the first configured private channel pair only after inspection and confirmation. The Wi-Fi visibility control changes only whether the hotspot advertises its network name; it preserves its name and password.

## Wi-Fi network visibility

In **Settings / Admin**, sign in, then use the separate **Wi-Fi visibility** card:

- **Broadcast Wi-Fi network name On:** the hotspot appears in nearby Wi-Fi network lists.
- **Off:** the hotspot continues running with a hidden name. A new device must join manually using the displayed network name, WPA2 security, and the existing Wi-Fi password. Some devices also need their saved network marked as hidden.

Tap the switch to open a confirmation dialog, then choose **Stop broadcasting** or **Broadcast name**. **Cancel** or Escape leaves the setting unchanged. The switch reflects the saved state until you confirm. Applying an active hotspot setting may briefly disconnect its clients while the Pi restarts that connection. Rejoin the same network and return to **http://10.42.0.1**. The card reports pending, applied, or failed status using the Pi's actual profile and worker result. The setting persists in NetworkManager across reboots. An inactive hotspot profile is updated without starting it or disconnecting the Pi's home Wi-Fi connection.

This is name visibility, not a Wi-Fi power switch or an additional security layer. [NetworkManager documents the hidden setting for AP mode](https://www.networkmanager.dev/docs/api/latest/settings-802-11-wireless.html). The existing Wi-Fi password remains required.

The browser cannot run general network-management commands. A separate systemd worker accepts only this one flag for the fixed `repeater-ap` profile. The installer supplies that worker and its path unit. Until the hotspot and worker exist, the control reports unavailable. On the Mac preview you can open the confirmation dialog, but its confirmation button is disabled and it never changes the Mac's Wi-Fi.

If a device cannot rejoin the hidden network, use Ethernet SSH or a Pi keyboard/monitor to restore visibility:

```bash
sudo nmcli connection modify repeater-ap 802-11-wireless.hidden no
sudo nmcli connection up repeater-ap ifname wlan0
```

The visibility worker log is available with `journalctl -u repeater-wifi-visibility.service -n 30 --no-pager`.

Setup guidance appears only while the related setup is incomplete. Password instructions disappear once an admin password exists, including while signed out. Wi-Fi instructions and checklist step 2 disappear once the hotspot profile exists, including when it is inactive. Checklist step 1 disappears when the installed software is running on the Pi. AIOC assignment and radio commissioning remain on the checklist; USB detection alone does not complete either step. The first-audio guidance disappears after the receive monitor is running and the dashboard has received audio during this service run. Fresh installations show the relevant instructions again.

## Updating an installed box

Version 0.14.0 adds admin-only confirmed deletion of individual audio recordings and SSTV gallery pictures. It is an app-only update compatible with update API 17; no SSH command, new native decoder or radio-service restart is required.

Version 0.13.1 removes completed setup wording from commissioned live dashboards and distinguishes stopped or failed radio services from an active bench engine. Fresh installations retain their setup guidance. It also fixes the optional SSTV installer by stripping unneeded symbols from the final Debian-built decoder before its size check. It remains compatible with update API 17.

Version 0.13.0 moves decoding to a separate Data tab and adds SSTV Robot 36 live scanline previews and a private PNG gallery. It remains compatible with update API 17. Its new optional SSTV engine needs one native installation using the reviewed helper in [Live data decoding](config/DATA_AUDIO.md); the helper leaves radio services running. The default SSTV switch is off, and existing APRS and recording preferences are preserved. Reload the page after the dashboard update to load its new tab and assets.

Version 0.12.0 adds admin-only live APRS decoding and bounded packet-log downloads. It remains compatible with update API 17, so the dashboard can update from Admin while live operation continues. The optional native decoder needs a separate one-time installation described in [Live data decoding](config/DATA_AUDIO.md); running that helper does not restart radio services.

Version 0.11.3 applies the same progress animation to update checks, radio programming, the radio editor and audio-message jobs. Measured progress keeps its actual fill and completed percentage bars stay static. It remains compatible with update API 17.

Version 0.11.2 adds an indeterminate progress bar and lightweight status polling while checking for updates. It uses API 17 and can be installed from Admin without stopping the live repeater.

Version 0.11.1 uses the existing update API 17 and can be installed through the Admin updater after 0.11.0. It clears update results tied to a previous installation and compares version numbers numerically, so an older cached release is never offered as an upgrade. Live radio operation and private settings are preserved.

Version 0.11.0 requires one manual installer upgrade (update API 17). It adds the monitored transmit-audio path, private audio-message library and dispatcher, native activity indicators, and prompt-aware DTMF idle timing. The installer migrates a valid private live profile while preserving station identity, audio gain, channel catalog and prior authorization. It raises the Pi transmit timeout to 40 seconds for uploaded messages up to 30 seconds; keep the radio’s own timeout longer than that. Live operation remains stopped after installation until deliberately restarted. Uploading or previewing a message never transmits. See [Live audio and radio messages](config/LIVE_AUDIO.md).

Version 0.10.4 requires one manual installer upgrade (update API 16). Live startup waits up to 30 seconds for the authorized RX/TX serial and ALSA identities, checking authorization and pending radio work throughout the wait. Missing devices fail closed at the deadline; visible identity mismatches and invalid configuration fail immediately. After commissioning, disable the bench and receive-monitor boot units and enable `repeater-live.service` as described in [Live repeater commissioning](config/LIVE_REPEATER.md). A later successful manual upgrade stops live operation while replacing files and preserves its existing enabled-at-boot preference; an unfinished radio upload still disables radio startup. Compatible dashboard updates preserve the running service.

Version 0.10.3 requires rerunning the Pi installer once (update API 15). Required source files must be nonempty regular files, installed software and service files must match the bundle, and filesystem writes must be flushed before services start. The dashboard health endpoint rejects missing or empty page assets. The radio service wrapper forwards one stop signal to SvxLink and reports an expected termination as a clean stop. Existing station configuration is preserved; later dashboard updates compatible with API 15 can use the browser updater.

Version 0.10.2 requires rerunning the Pi installer once (update API 14). It installs the matching DTMF protocol, new control prompts and grouped Admin page. Download the current bundle using section 3 and rerun the installer; existing passwords, hotspot, private channel pairs, radio bindings, backups, recordings and settings are preserved. Older browser updaters refuse this release before replacing the dashboard. Earlier installers stopped and disabled an existing live service during a manual installation. An already prepared 0.10.1 live configuration keeps its authorization, audio gain and interval; deliberately start it after reviewing the result. Older live configurations without the interval field need preparation and authorization again. Re-enable boot startup only after commissioning. Do not rerun password or hotspot setup when those are already working.

During an upgrade, the installer migrates an older installed channel table into the private Pi catalog before replacing that helper. An existing valid private catalog is retained. Migration preserves the existing frequencies, tones and pair order, while assigning generic pair labels and the numbered `BOX-RXn` / `BOX-TXn` radio names. Device qualification and protected backups remain intact. Saving or migrating a catalog does not rename physical radio memories: review and program the default pair afterward to apply the new names. A fresh installation supplies no implicit frequencies; run the local configuration command above.

**Update software** stays disabled until **Check for updates** completes successfully in the current page and admin session. A saved check may still be displayed after reopening the page, but it does not enable installation. Changing the installed version, available commit or admin session requires another check.

The writer keeps the same logical edit permissions and strict whole-image readback checks. A paired retry compares both fresh radio reads with their protected saved candidates, including a radio that already verified individually. Unrelated drift blocks the retry before either radio is uploaded. See the [paired-change guide](config/RADIO_PAIR_SWITCHING.md) for qualification and recovery; installation alone does not enable remote channel changes.

The dashboard identifies which connection has an unfinished upload, including the external programming cable. **Repair saved upload** restores the reviewed candidate's affected operating pages from its protected local backup. It refuses changed device bindings, unsupported profiles, stale pending identifiers, unrelated changes outside those pages, or an unfinished paired transaction. This is separate from adopting the radio's current values. Read [the repair procedure](config/RADIO_PROGRAMMING.md#repairing-an-unfinished-upload) before using it.

After that, connect the Pi to the internet over Ethernet, open **Settings / Admin**, sign in, and select **Check for updates**. If a newer commit is available, select **Update software**, review the confirmation, and confirm. The updater follows the public repository's `main` branch and displays the checked version. The first check after a manual installation may offer the same version to establish the exact installed GitHub commit.

The worker rechecks the selected commit, downloads that exact archive over HTTPS, validates the manifest and fixed dashboard file list, and compiles Python before replacing the application. It restarts only the dashboard and checks its HTTP status/version. A failed activation restores the previous dashboard directory. Network/download/validation failures before activation leave the running dashboard alone. The retained backup is `/opt/repeater-box/dashboard.previous`.

The dashboard restart briefly stops live browser audio, closes any current recording, and signs browsers out. The recording state after restart follows **Record automatically after a restart**. Keep power connected during installation; after the page reconnects, sign in again and resume listening if desired. The updater result remains visible after restarting.

Browser software updates cover the dashboard Python/audio code and web assets. They preserve `/etc/repeater-box`, including the private channel catalog, NetworkManager Wi-Fi settings, archives, station settings, admin password, and SvxLink services/configuration. Changes to privileged helpers, the CHIRP library, OS packages, or systemd units require downloading the new bundle and rerunning the installer from SSH. The separate programming worker accepts only the locally configured RX/TX plans and reviewed editor-profile fields. A shared root-owned maintenance lock prevents programming and software installation from running together. The browser cannot supply a repository, URL, branch, path, or shell command; the fixed repository/branch are root-owned in `/etc/repeater-box/updates.json`.

Update logs are available with `journalctl -u repeater-software-update.service -n 50 --no-pager`. The Mac preview shows this card but cannot update its host. The public repository includes software and example configuration only; passwords, recordings, local settings, and runtime state stay on the Pi.

On a fresh installation the installer starts the guarded bench engine. If the receive-only monitor was already active or enabled, it preserves that mode and leaves the bench engine stopped. Recheck the interface mapping and audio after updating before relying on it.

## Troubleshooting and rollback

View logs:

```bash
journalctl -u repeater-dashboard.service -n 50 --no-pager
journalctl -u repeater-engine.service -n 50 --no-pager
journalctl -u repeater-rx-monitor.service -n 50 --no-pager
```

If the dashboard works on Ethernet but no hotspot appears, check the country setting in `sudo raspi-config`, `nmcli device status`, and `journalctl -u NetworkManager -n 50 --no-pager`. Reboot after correcting country/radio settings. An existing `repeater-ap` profile is left intact on script reruns; activate it with `sudo nmcli connection up repeater-ap`.

To return the Pi to a saved home Wi-Fi profile, do this from Ethernet or a local keyboard; it disconnects hotspot clients:

```bash
sudo nmcli connection modify repeater-ap connection.autoconnect no
sudo nmcli connection down repeater-ap
nmcli connection show
sudo nmcli connection up "YOUR_SAVED_HOME_WIFI_PROFILE"
```

To stop this project's services:

```bash
sudo systemctl disable --now repeater-dashboard.service repeater-engine.service repeater-rx-monitor.service repeater-live.service repeater-wifi-visibility.path repeater-software-update.path repeater-radio-programming.path
```

Keep the vendor SvxLink service masked until its configuration is reviewed separately. Installation itself does not program a radio.

## Development preview and verification

For a local preview without installing anything on the Pi, from this bundle directory:

```bash
python3 dashboard/server.py --host 127.0.0.1 --port 8766 --recordings ./preview-recordings --state-dir ./preview-state --admin-file ./preview-state/admin.json
```

Open http://127.0.0.1:8766. This explicit port is for the Mac development preview; the installed Pi uses port 80. The Mac identifies itself as development and leaves unavailable Pi measurements unavailable. Without an explicitly labeled test source, it has no real receiver feed, recordings, or radio activity. The normal preview does not manufacture them. Stop with Ctrl-C.

Run the checks (Node.js is only needed for development checks, not the Pi runtime):

```bash
python3 -m unittest discover -s tests -v
node tests/check-dashboard-ui.cjs
node tests/check-radio-editor.cjs
bash -n scripts/install-pi.sh scripts/configure-hotspot.sh scripts/diagnostics.sh
```

For the programming fixture/protocol checks, set `CHIRP_TEST_SOURCE` to an extracted copy of the pinned upstream CHIRP commit that includes `tests/images/Baofeng_5RX.img`, then run the Python suite. Those checks skip when that external source is absent; no radio is needed. The installer keeps only the runtime library and does not install upstream test images.

The local checks cover archive playback byte ranges, denied traversal/symlinks, static assets, status data, authenticated recording/settings/update/programming requests, session/CSRF handling, real loopback UDP input through the HTTP live stream, recorder behavior, and the bench/receive-only guards. Programming checks cover serial line ordering, separate download/upload sessions, automated settling, protected image bytes, local backups/rotation, and unfinished-upload recovery. Update tests check fixed HTTPS sources, archive validation, compilation before service stop, failure rollback, and metadata recovery. Frontend checks cover toggle state, setup visibility, confirmation cancellation, and stale-session/commit protection. Browser appearance/playback and Pi-specific systemd, NetworkManager, ALSA, firmware, wireless reliability, actual radio audio, and RF behavior require a check on the hardware.
