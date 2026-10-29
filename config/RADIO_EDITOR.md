# Hidden radio editor

Open **http://10.42.0.1/radio-editor** after installing the current bundle. There is no link in the normal dashboard. Sign in using the existing dashboard admin password; radio snapshots require an authenticated session, and changes also require its CSRF token.

This browser editor uses pinned CHIRP drivers. The box RX and TX stay on the Baofeng / 5RX profile for UV-5RX radios. The optional external programming connection also supports the original Baofeng / UV-5R profile, selected explicitly before reading. It supports the 128 channel memories, names, receive/transmit frequencies, transmit inhibition, FM/NFM, High/Low power, scan inclusion, independent CTCSS/DCS tones, and the displayed operating settings. It does not expose service/calibration settings or provide continuous radio control.

## Read, edit and review

1. Keep both AIOCs in their assigned ports. With the selected radio off, attach its K1 cable to the matching interface, then turn it on. Leave it powered for the following read, write and verification steps; routine operations do not require manual power cycles.
2. Select **RX** or **TX**, then **Read radio** and confirm. This pauses the radio services, reads the actual memories/settings, and saves a private local backup. The editor is populated from this read, not from example data.
3. Edit the desired channels or operating settings. Use the search/filter controls to find a memory. The RX/TX selector identifies the physical radio; it does not lock every channel on that radio to one frequency.
4. Review the changes. The review lists only changed memories and settings. No radio data changes while editing or reviewing in the browser. Explicitly confirm before uploading.
5. The worker reads the radio again, rejects a stale base if the contents changed, and saves a fresh backup. It closes that download connection, automatically leaves five quiet seconds, and starts a separately identified upload session with firmware/auxiliary-data checks. It sends permitted field changes using contiguous 256-byte name transfers and 32-byte channel/global transfers, preserving all companion bytes from that radio's candidate, then closes the upload connection, automatically waits another five quiet seconds, and reconnects for verification. Keep the radio connected and powered until the operation finishes.
6. Check the result and the intended working memory on the radio. If a separate verification is needed, select **Verify**; no routine manual restart is required. If the radio stops responding or the worker cannot reconnect, let the task finish, power the radio off and on as a recovery step, then retry the read-only verification. For a settings mismatch, review the recovery options below rather than repeating an upload without investigation.

Qualify each pair of physical radios using the live-frequency checks in the [paired-change guide](RADIO_PAIR_SWITCHING.md). A successful stored-image readback and the displayed configured pair do not provide live radio telemetry.

**Add channel** lets you choose an empty memory slot. Enter its receive frequency and optional name, then select the desired tone and transmit settings. A new channel starts with High power, NFM, and transmission disabled until you configure it. All 128 slots retain the radio's numbering, 0–127.

Each occupied row has **Delete**. Confirm **Delete from draft** to mark that memory for clearing, including its name. The deleted row stays visible with **Undo**, which restores its draft values and any edits made before deleting it. If all slots are occupied, delete a channel in the draft to free a slot for a new one. **Review changes** shows Added, Deleted and Updated counts and identifies each affected memory before you confirm the radio write. Adding, deleting and undoing in the browser do not send programming commands.

The browser fetches a completed read once, and a delayed snapshot response cannot overwrite edits made while that response was in flight. It rechecks the reviewed draft and session after the final snapshot check before queuing a write. The review labels the full Before and After values separately so a channel name cannot be mistaken for a scan setting. Version 0.10.1 requires rerunning the Pi installer for update API 13. Older browser updaters refuse this helper release. A fresh installation also needs a private channel catalog for plan-based presets; manual editor work remains available without one. Automatic settling, backups and strict verification remain in place. The larger name transfer does not expand the editor's logical field permissions or establish firmware erase geometry.

For an unfinished upload that cleared companion settings, use **Repair saved upload** after reviewing the [saved-candidate repair procedure](RADIO_PROGRAMMING.md#repairing-an-unfinished-upload). It restores the affected pages from the protected candidate. An ordinary editor draft still cannot change protected fields, and adopting current values remains a separate explicit choice.

Frequency entries use MHz with 10 Hz resolution. Receive frequencies must fit the selected driver's supported bands. This project's editor permits new or changed transmit frequencies in its configured 144–148, 222–225 and 420–450 MHz ranges, or transmission can be disabled. Existing factory transmit frequencies are preserved when an unrelated field is edited. These are software profile constraints, not confirmation that a frequency is suitable or authorized for operation.

Auto Lock is available in operating settings. The default box-programming buttons set High power and Auto Lock on for both radios. Keypad locking helps protect the front-panel settings; it does not provide the Pi's transmit interlock. Channel selection is performed manually because the working-channel cursor values can change independently of the stored memories.

## Live progress and results

The editor and normal dashboard show the current operation and step: connection check, reading, backup, validation, writing and verification. Where a block count is available, a progress bar shows the count and percentage **for that step**. Other steps use an indeterminate indicator. A bounded activity log retains the latest 20 safe operation messages rather than raw serial traffic or an indefinite log file. Active tasks refresh through a lightweight status endpoint every second while the page is visible.

If Wi-Fi disconnects, keep the radio connected and let the task finish. Reconnect and inspect the existing result rather than resubmitting an upload. Detailed verification errors identify differing settings or byte positions, while image data remains private on the Pi.

Backups retain **five sets per configured connection**: RX, TX, and the optional Programming port. When several handhelds share the external cable, those five external backup sets cover their combined recent jobs. The current verification candidate, active editor base and saved repair source are protected within that count. Passwords, device bindings and original images are not sent to GitHub. See [the programming guide](RADIO_PROGRAMMING.md) for backup locations, service recovery and diagnostic logs.

The normal dashboard's programming card displays the **first configured private pair**. A successful custom edit is labeled separately; choosing Program RX/TX there reapplies that pair to memory 1 and the reviewed operating settings after confirmation. Radio names are BOX-RX1 / BOX-TX1 for the first pair, then BOX-RX2 / BOX-TX2 and so on. Overview values remain configured labels, not live radio telemetry.

Later compatible dashboard-only updates can use the existing admin updater. The actual radio revision and USB behavior still require hardware commissioning; tests use upstream fixtures and a simulated clone protocol.

## Private channel-pair presets

**Channel pairs** appears beside Add channel. It reads the Pi's private catalog; there is no built-in frequency table or region selector. Configure the catalog from the Pi terminal as described in [paired changes](RADIO_PAIR_SWITCHING.md#configure-a-private-channel-catalog). A missing or invalid catalog disables these presets while manual channel editing remains available.

To stage a matching memory:

1. Read the **RX radio**, open **Channel pairs**, choose a configured pair and an empty memory. Its draft receives the box input, uses the pair's receive tone when configured, and has transmission disabled. Its name is **BOX-RXn** for that pair's position.
2. Review, write and wait for automatic verification. Repeat for **TX** using the same pair. Its draft receives and transmits on the box output, with the configured transmit tone and receive tone off. Its name is **BOX-TXn**.
3. For a handheld on the **External programmer**, select its exact model and read it first. Choose the same pair and an empty memory. The handheld receives on the box output and transmits on the input, with the configured transmit tone and receive tone off. Its name is **BOX-CHn**. Review, write and verify each handheld separately.
4. With repeating stopped, manually select the intended matching memories before resuming operation. The dashboard cannot detect front-panel channel selection. Use the separately qualified [paired-change workflow](RADIO_PAIR_SWITCHING.md) to replace working memory 1 remotely on the two box radios.

Pair presets use **High power and NFM**, retain global settings, and stage only the chosen empty memory. An occupied slot is never overwritten. Memory 1 is reserved from this editor preset workflow. The normal dashboard's Program RX/TX buttons instead apply the first configured pair to working memory 1.

Selecting a preset changes the browser draft only. The review remains bound to the current private catalog, selected model, read snapshot and admin session. It does not detect interference, select an alternate automatically, or establish local channel suitability. CTCSS controls audio access; it does not prevent RF interference. Monitor the intended frequencies and obtain local coordination guidance before operating.

## Recovering after failed verification

A failed verification does not lock out reading or drafting. Select the affected connection and **Read radio**. The editor keeps the unfinished candidate and original backups, shows the expected and current values, and leaves radio services paused.

You can change the desired settings and review a **repair write**. If the current values are already what you want, review **Adopt current values** instead. The confirmation identifies the exact unfinished candidate being replaced. A fresh backup and verified replacement are required before that role is cleared. An adoption with no edits makes no radio writes; it verifies the fresh current image as the explicitly chosen replacement. It does not mean the old requested settings were applied.

Recovery is refused if calibration, reserved bits, or other protected data differ. Other pending connections keep the radio services paused until all unfinished uploads are resolved. Reading another connection never clears the first one's verification state. A failed later re-verification also marks the radio unverified and pauses its services.

## Automatic and current keypad lock

Automatic keypad lock is a persistent operating setting and remains strictly checked. Current keypad-lock state is separate: its changing Boolean bit, along with the known working-channel cursor bits, is excluded from readback equality and fresh-base checks. Reserved bits remain checked.

If Auto Lock returns to OFF after cloning, use **Repair saved upload** if an unfinished upload has a protected candidate with Auto Lock enabled. Alternatively, set **MENU → 24 AUTOLK → ON**, confirm with MENU, exit, and verify again. This is a manual workaround; the helper does not silently treat OFF as a successful ON setting. A power cycle can be used as an additional persistence check, rather than a requirement for routine verification. After Auto Lock engages, the current keypad-lock flag can change without invalidating the programmed settings.

## Optional external USB programming port

A data-capable waterproof USB-A panel extension can bring a spare Pi USB host port outside the case. Plug a supported radio programming cable into that extension. The cable is a separate programming connection and supplies no audio feed. The external connection supports two reviewed profiles: **UV-5RX / 5RX** and **original UV-5R / UV5R**. The initial original UV-5R profile is bounded to the reviewed `aa307604000520dd` image identifier and `HN5RV011` firmware family; selecting UV5R does not enable arbitrary original UV-5R revisions. A different revision needs a profile review before writing. The two box radios remain on 5RX. UV-5RX3 and other similarly named radios are not substitutes; other models need a separately reviewed driver and suitable cable.

With the radio end disconnected, identify the USB cable and its stable path:

```bash
lsusb
lsusb -t
ls -l /dev/serial/by-id/
ls -l /dev/serial/by-path/
```

For cables without a unique serial number, use the by-path alias for the physical USB port being extended. Save that complete path locally, replacing the placeholder:

```bash
sudo python3 /opt/repeater-box/scripts/configure-radios.py \
  --programmer-port /dev/serial/by-path/YOUR_PROGRAMMING_PORT
```

This adds the external connection to the existing local configuration without replacing the RX/TX AIOC assignments. It checks the serial device without opening it or programming a radio. A binding with an unfinished upload cannot be changed until that connection is resolved. An unplugged optional cable does not prevent using the RX/TX connections.

Refresh the editor and select **External programmer**, then select the exact **External radio model** printed on the handheld: **UV-5RX / 5RX** or **UV-5R / UV5R**. Model selection is manual; a serial identification exchange does not reliably distinguish similarly named radios. Read every newly connected radio before editing, even if another radio with the same model used the cable earlier. No boot programming is performed.

Changing the selected external model clears the loaded snapshot and requires a new read. If the draft has edits, a confirmation lets you keep them or discard them before switching. Delayed responses and reviewed changes remain bound to the selected model, snapshot and admin session. A write uses the model stored with that fresh private read; the browser cannot override its driver during a write.

An unfinished external upload locks the model selector to that upload's saved profile. Keep the same physical radio connected, then use **Verify upload**, **Repair saved upload**, or a fresh read and reviewed recovery. Selecting another model or reading RX/TX does not clear it. The same five external backup sets cover both supported models and all handhelds sharing that cable.

The returned settings match the selected profile. The original UV-5R uses an integer squelch setting and omits the 5RX PTT-ID control; service/calibration and band-limit settings remain excluded. Ordinary writes preserve the original UV-5R's protected data and use that profile's reviewed operating layout. Do not load a 5RX image into an original UV-5R, or the reverse.

Use the same read, review, write and automatic-verification procedure. Turn a radio off before attaching or removing its K1 plug, then leave it powered during the operation. To remove the optional connection after its work is resolved:

```bash
sudo python3 /opt/repeater-box/scripts/configure-radios.py --remove-programmer
```
