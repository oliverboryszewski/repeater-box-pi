# Paired changes from the dashboard

Settings / Admin includes a channel-pair dropdown populated from the Pi's private catalog. Select a pair, choose **Change channel pair**, review both frequencies and the tone, and enter the admin password again. The password is checked for that request and is never written to the programming queue, backups or activity log.

Both box radios stay on **memory 1, display A, channel mode**, with dual watch off. The worker copies the chosen pair into that working memory rather than relying on firmware-managed current-channel cursor values. It preserves the other memories and global settings. Names follow catalog order: **BOX-RX1 / BOX-TX1**, **BOX-RX2 / BOX-TX2**, through **BOX-RX9 / BOX-TX9**. The first pair is the default.

The dashboard reports the **configured pair**, based on the most recent complete programming transaction. This is not live frequency telemetry. A separate RX/TX write or failed verification invalidates that paired record. The Pi cannot detect someone selecting another channel with the radio's buttons; keep both radios on memory 1 and enable keypad lock.

## Configure a private channel catalog

The public package contains no default radio frequencies, location list or regional channel table. After installing the current bundle, run this in a local Pi terminal or SSH session:

```bash
sudo python3 /opt/repeater-box/scripts/configure-channel-pairs.py
```

Follow the interactive prompts to enter one to nine locally chosen pairs and their CTCSS tone, or no tone. The command saves `/etc/repeater-box/channel-pairs.json` with root ownership and restricted permissions. Values, labels and catalog order stay on the Pi. Review local suitability before entering a pair; a saved configuration does not establish that a channel is clear or authorized for repeater use.

This command does not program radios or enable transmission. Resolve unfinished uploads before replacing a catalog. A missing or invalid catalog disables plan-based programming, paired changes and the editor's **Channel pairs** presets. Read-only inspection and manual editor work remain available. Keep private catalogs, radio images and device identities out of the public checkout and shared screenshots.

## Install the matching helper

Version **0.9.1 / update API 11** requires downloading the current bundle and rerunning `sudo bash scripts/install-pi.sh` from its directory on the Pi. See the [README installation steps](../README.md#3-download-and-install-this-package). This installs the per-radio settling helper; older browser updaters refuse the release before replacing the dashboard.

The installer preserves passwords, hotspot configuration, device bindings, qualification, backups, archives and saved settings. It migrates an older installed channel table into the private catalog before replacing the helper, using generic pair labels and the numbered radio names. An existing valid private catalog is kept. A fresh installation has no implicit pairs and requires the terminal command above. Saving or migrating a catalog does not alter radio memories; review and program the first configured pair when ready to apply its new names. Later dashboard updates compatible with API 11 can use the browser updater.

## Check live reload behavior locally

Ordinary writes close the download connection, automatically wait **five quiet seconds**, and use a new upload identification and auxiliary-data check. The worker sends contiguous 256-byte name groups and 32-byte channel/global groups, preserving every companion byte from the candidate prepared from that radio. These are transfer boundaries, not a claim about internal erase geometry. It then closes the upload connection, waits another **five quiet seconds**, and opens a fresh connection for readback verification.

For paired changes, each initial read connection closes as soon as that radio's original backup and checks finish. Reading TX therefore counts toward RX's quiet interval, and RX work counts toward TX's. Before reconnecting to either radio, the worker waits only for any remaining part of its five-second interval. Switching to a radio that has already been quiet long enough adds no extra pause. The same-radio upload and post-upload verification waits still apply.

Leave both radios powered throughout routine reads, writes and verification. Restart only as recovery if a radio is unresponsive or cannot reconnect after its task finishes. The activity log shows connection changes and settling periods. A failed operation retains its verification candidate and keeps services paused; it does not automatically upload again.

Before enabling unattended paired changes, test both actual radios locally. Each must return to normal operation, load a changed frequency in the already selected working memory, and accept another read and write without a power cycle or front-panel channel change. Confirm reception and transmission on the intended frequencies with suitable test equipment or another correctly programmed radio. A changed name or successful stored-image readback alone is insufficient. Keep repeating stopped during qualification.

After both radios pass, put them on memory 1 / display A, confirm the intended operating settings and keypad lock, then run:

```bash
sudo python3 /opt/repeater-box/scripts/configure-pair-switching.py \
  --enable --confirm-memory-one --confirm-live-reload
```

This requires both supported radios to be verified with no unfinished upload. It saves a local root-owned qualification flag without reading or writing radio data. Requalify if the radios, firmware or bindings change. To disable the feature:

```bash
sudo python3 /opt/repeater-box/scripts/configure-pair-switching.py --disable
```

## During a paired change

The worker pauses radio services, reads both radios and saves both original images and candidates before writing either. It then programs and verifies RX, followed by TX. The reviewed request is bound to the exact private catalog; changing that catalog invalidates a stale browser review. Radio services and their autostart remain held until both radios verify and the programming connections close. Only previously active/enabled project services resume; this does not commission or enable a new transmitter.

A partial failure keeps a durable interlock across reboot. After investigating the failure, select the desired pair and confirm a fresh-password retry. Both radios are read again before replacement candidates are prepared. Each fresh image must match its protected saved candidate outside working memory 1 and known runtime bits, including a radio already verified individually. Unrelated memories, unused-name padding, global settings and calibration remain protected. An unexplained difference stops the retry before either upload. Individual Verify, a reboot, or a catalog edit cannot clear the paired interlock.

Backups are limited to five sets per configured connection, with current candidates and required recovery sources protected. Each paired operation has separate RX and TX sets. Once replacements are durable, older eligible sets can rotate out. There is no automatic rollback upload.

The external handheld cable is independent. Read each newly connected handheld with its exact supported model, then use the editor's **Channel pairs** control to stage a matching memory. The handheld receives on the box output and transmits on its input; its label is **BOX-CHn** for the selected pair. Review, write and verify handheld memories separately.
