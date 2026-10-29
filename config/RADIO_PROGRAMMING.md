# On-demand radio programming

This workflow is for radios physically labeled **UV-5RX**, using CHIRP's **Baofeng / 5RX** profile. The [driver maintainer tested the UV-5RX and renamed that profile to 5RX](https://chirpmyradio.com/issues/11046#note-6). UV-5RX3 and original UV-5R are different profiles. The selected profile is an explicit configuration choice; the serial handshake does not automatically identify an exact radio model.

The optional external USB programming connection separately supports the **original UV-5R / UV5R** profile in the hidden editor. Its initial write support is bounded to the reviewed image identifier and `HN5RV011` firmware family, rather than every UV-5R revision. Select that exact profile and read the connected handheld before editing. This does not change the box RX/TX profile or make the images interchangeable. An unfinished upload keeps its saved model until verification or reviewed recovery succeeds. See [external programming](RADIO_EDITOR.md#optional-external-usb-programming-port) for the model selector and cable setup.

The worker uses pinned upstream CHIRP commit `0b4076d9b19a2a133d11f31546cff8153b39eb89`. It initially permits writes only for the tested image layout/identifier and firmware family. Inspection can back up and report a different revision, but programming remains disabled until that revision is reviewed. No browser override or fallback driver is supplied.

## Install and bind the interfaces

Connect the Pi to the internet over Ethernet. Download the current repository and rerun `sudo bash scripts/install-pi.sh` as described in the README. Version 0.10.1 adds live-service programming interlocks and requires update API 13. This needs a one-time terminal installation; an older browser updater refuses the release before replacing the dashboard. Existing passwords, hotspot, private channel pairs, bindings, qualification, backups, recordings and settings are preserved.

Keep the assigned AIOCs in their labeled USB ports. Confirm their stable names with:

```bash
arecord -l
aplay -l
ls -l /dev/serial/by-id/
```

Use the serial numbers discovered for the physical RX and TX interfaces in this command, replacing the placeholders:

```bash
sudo python3 /opt/repeater-box/scripts/configure-radios.py \
  --model 5RX --rx-serial RX_SERIAL_HERE --tx-serial TX_SERIAL_HERE
```

This checks both devices and writes a private root-owned mapping in `/etc/repeater-box/radio-programming.json`. It does not program either radio. Device serials, original images and local settings are not added to the public repository. Swapping an AIOC or changing the mapping requires inspection again.

## Configure the private channel pairs

There is no built-in frequency plan. On a fresh installation, run this on the Pi before using Program RX/TX or channel-pair presets:

```bash
sudo python3 /opt/repeater-box/scripts/configure-channel-pairs.py
```

Enter one to nine locally chosen pairs and their CTCSS tone, or no tone, at the interactive prompts. The first pair is the default. The root-owned catalog is `/etc/repeater-box/channel-pairs.json`; its frequencies, tones and labels stay on the Pi. Do not add that file or private radio images to the public checkout. Resolve unfinished uploads before changing the catalog. Read-only inspection and manual editor work remain available when no catalog exists.

On upgrade, the installer migrates an older installed channel table before replacing the helper, preserving its frequencies, tones and order while assigning generic pair labels and numbered names. An existing valid private catalog is retained. Configuration and migration do not change physical radio memories; review and program the default pair afterward to apply the numbered names. Catalog changes invalidate stale programming reviews rather than silently using different values.

## Inspect, then program one radio

1. Turn the selected radio off, firmly connect its K1 plug/extension to the correctly labeled AIOC, attach its antenna or suitable RF load, then turn it on. Use a charged battery and stable Pi power. Do not press PTT during programming.
2. Open **http://10.42.0.1 → Settings / Admin**, sign in, and find **Radio setup**. Select **Inspect RX** or **Inspect TX** and confirm. Inspection only reads the radio and saves a local backup. It pauses the project's radio services to free the interface.
3. Check the displayed profile, firmware and compatibility result. If incompatible or unrecognized, stop and retain the backup for review; do not substitute another driver or upload an old image.
4. Leave the radio powered on. Select the matching **Program RX** or **Program TX** button and review the displayed plan before confirming. The worker reads that radio again and saves another original backup before changing channel 1 and the allowlisted operating settings.
5. Wait for the result. The worker automatically closes the download connection, waits five quiet seconds, uses a new upload identification, checks the firmware/auxiliary data, and sends the reviewed changes in contiguous transfers: 32 bytes for channel/global data and 256 bytes for names. Consecutive 16-byte frames preserve every unchanged companion byte from the same radio's candidate. The logical edit permissions are unchanged; these transfer sizes do not establish internal erase geometry. After writing it closes the upload connection, automatically waits another five quiet seconds, and reconnects for verification. A routine inspection, upload or verification requires no manual power cycle. If a separate readback is needed, choose **Verify RX** or **Verify TX**; it is read-only.
6. After success, check channel 1 on the physical radio and select memory 1 if needed. Repeat the sequence for the other radio. Proceed to receive-only audio commissioning after both radios are verified. If a radio becomes unresponsive or cannot reconnect, wait for the operation to finish before power-cycling it as a recovery step and retrying verification.

| Role | Memory 1 from the first private pair | Tone | Mode / power |
|---|---|---|---|
| RX | `BOX-RX1`, receives the configured input; transmission inhibited | Configured receive tone, or off | NFM; High configured; no transmission on this channel |
| TX | `BOX-TX1`, receives/transmits on the configured output | Configured transmit tone, or off | NFM; High |

Additional pairs use `BOX-RX2` / `BOX-TX2` and so on, following catalog order. The remote paired-change workflow copies a selected pair into working memory 1 on both radios. The editor can stage other matching memories separately, including `BOX-CHn` handheld memories.

The allowlisted settings disable VOX, battery saving, dual watch, beeps, voice prompts, roger tone and PTT ID; set a 60-second timeout; enable Auto Lock, and select channel mode. Select memory 1 manually on each radio after programming. The worker permits only reviewed field changes. Its larger physical transfers preserve the other bytes from the same candidate; they do not authorize additional logical edits. Auxiliary and calibration regions are excluded from physical writes. High is the radio's power setting, not a measured wattage or a guarantee of continuous-transmit duty cycle.

## Repairing an unfinished upload

The worker sends contiguous 256-byte name groups and 32-byte channel/global transfers. Every companion byte comes from the protected candidate prepared from that radio; the logical field allowlist and whole-image comparison remain strict. These transfer boundaries do not establish an internal erase-page size. Qualify each radio's live frequency reload separately before enabling unattended paired changes; a routine read or write requires no manual power cycle.

1. Keep the same physical radio on the same configured cable and powered on. Restart it only if it is unresponsive or a previous connection failed, after that operation has finished.
2. In Admin or the radio editor, choose **Repair saved upload** for the unfinished connection and review the confirmation. This restores the saved candidate's affected pages, including companion settings cleared by the earlier write. It does not adopt the cleared settings as a new baseline.
3. Keep the radio and Pi powered until writing and automatic verification finish. An additional power-cycle and **Verify** can check persistence during initial bench qualification or investigation of a particular radio revision; it is not required after every repair or normal upload.

The root worker validates the saved original's manifest hash, private file ownership, source identities, model and original change boundaries. Legacy candidates have no previously recorded digest; those are validated against their protected original and allowed operation, and the repair records source digests for retries. A fresh backup is saved before writing. Unexpected changes outside the implicated pages, a changed binding or an unfinished paired transaction stop repair before upload. Original and candidate images are never replaced by an image from another radio. A failed repair retains its source and leaves services paused; a confirmed retry stays bound to that same source.

Recovery must retain strict verification and must not adopt unexplained protected-data changes. Establish the private catalog's local channel suitability and complete RF/PTT/identification commissioning before putting the repeater on air. Programming itself does not enable repeating, start a transmitter, or provide station identification. Overview values remain configured labels rather than continuous radio telemetry.

For an admin dropdown that changes both working memories with a fresh password confirmation, see [paired changes](RADIO_PAIR_SWITCHING.md). It requires local qualification of both radios' frequency reload behavior; ordinary stored-setting verification alone does not enable it.

## Backups and failure recovery

Backups stay on the Pi under `/var/lib/repeater-radio-backups/`, including the original image and a manifest. The worker retains **five backup sets per radio** and removes the oldest eligible sets after an operation. The current programming/verification set, active editor base and saved repair source are protected and count toward the five; the remaining slots hold the newest other sets. These small radio images normally use well under 1 MiB for both radios combined. There is no separate age limit. Unrelated files and symbolic links are not removed.

Backups are not public downloads or GitHub files. Copy a set to a trusted private location before it rotates out if long-term recovery matters. The worker does not automatically restore an image after a failed write.

The two working-channel selectors can be reported differently by radio firmware after power cycling. They are not uploaded by this version, and their seven channel-selection bits are excluded from readback equality. The Boolean bit for the current keypad-lock state is also excluded because automatic or manual locking changes it. The separate Auto Lock setting, reserved bits, stored channel frequencies/tones/power, other operating settings and calibration remain checked. This also allows a pending upload from version 0.4.0 to be verified against its saved candidate without requiring those selectors to stay at 1. Verification does not apply the new template settings; program again after successful verification to apply High power and Auto Lock.

Programming and software maintenance share a root-owned lock. If another task is running, wait for it to finish and retry. Previously active radio services resume only after a safe completion; an uncertain write or failed verification leaves them paused. A reboot alone does not constitute verification.

For a failed operation, leave the mapping and cables unchanged and check:

```bash
journalctl -u repeater-radio-programming.service -n 60 --no-pager
```

Use **Verify** for a read-only retry. If the radio is unresponsive or cannot reconnect, a power cycle is an optional recovery step after the current task ends. If verification still fails, open the [radio editor](RADIO_EDITOR.md) for a fresh read and explicit recovery review. The existing candidate remains protected until a verified replacement is accepted. A loose K1 extension is a common physical cause of clone failures. Turn the radio off before reseating its plug. [NA6D's guide](https://na6d.com/pages/aioc-getting-started) explains AIOC programming connections; [upstream AIOC documentation](https://github.com/skuep/AIOC#notes-on-chirp) covers its serial interface.

Automated checks use mocked devices and the pinned upstream CHIRP fixture. Actual USB line behavior, radio firmware compatibility, clone timing, and RF operation still require validation on the physical hardware.
