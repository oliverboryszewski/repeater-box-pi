# Receive data and SSTV pictures

The separate **Data** tab receives **SSTV Robot 36 pictures** and **APRS / AX.25 UI packets using AFSK1200** from the box receiver. It works without internet after installation. Admin sign-in is required for decoding controls, previews, galleries and downloads. Both switches default off; an existing APRS preference survives an upgrade.

These are specific supported formats, not a universal audio-data decoder. Digital voice, other SSTV modes and arbitrary file-transfer protocols need their own decoder. JSON and text downloads contain received packets, rather than recovered attachments. SSTV produces a picture reconstructed from audio; it is not a checksum-verified transfer of the sender's original image file.

## One-time installation

First install the dashboard normally and commission its receiver feed. On the Pi, from a current trusted source checkout, run:

```bash
cd ~/Repeater_Box_Pi
sudo bash scripts/install-data-decoder.sh
sudo bash scripts/install-sstv-decoder.sh
```

Run only the helper for the decoder you need. An existing working APRS installation does not need reinstalling to add SSTV. A fresh full installation copies both helpers and their supporting sources into `/opt/repeater-box/scripts`; they can be run there instead. App-only dashboard updates do not replace native prerequisites. These optional helpers are not run by the main installer.

Internet is needed only for this installation. The helper verifies pinned multimon-ng source and an output-only patch, builds a stdin-only decoder, then installs it with its license, corresponding source, patch and integrity metadata under `/opt/repeater-box/vendor/multimon-ng`. It may install the compiler, Make and CMake first. It does not open a radio device, change PTT, retune either radio, stop services or change boot startup. A maintenance lock prevents overlapping programming or updates. Retrying the helper is safe; an intact matching installation is retained.

The SSTV helper verifies pinned MIT-licensed `sstv` source, a small compiler-compatibility patch and the receive-only stdin adapter. It uses Debian Rust 1.85 or newer, installing the compiler if needed, and builds only the core decoder without Cargo, rustup, image codecs, GUI or audio-device support. The binary, license, original source, patch, adapter and integrity metadata stay under `/opt/repeater-box/vendor/sstv`. It shares the maintenance lock and makes no radio-service, PTT or boot-startup changes.

The dashboard checks each native binary and its provenance independently. A missing engine leaves only its own decoder unavailable. Refresh Data after installing a prerequisite; no radio-service restart is required.

## Receive SSTV pictures

1. Sign in and open **Data**. Enable the receiver audio feed if it is off.
2. Turn on **Decode SSTV pictures**. Listening in the browser and audio recording can stay off.
3. From another device, send a **Robot 36** SSTV signal on the box's input channel using its required tone and channel settings. Use a matching SSTV encoder on the sending device.
4. The preview fills as scanlines are decoded. Its progress bar measures received lines out of 240, rather than elapsed time.
5. Select a picture in the gallery to view it larger and **Download PNG**.

To remove a saved picture, use its **Delete** button and confirm the reviewed item. Only signed-in admins can delete; the PNG and its matching metadata are removed together. A picture still being received cannot be deleted. Removing the most recent completed or partial picture clears its live-preview copy too, so it does not reappear after a restart. Deletion is permanent and leaves the repeater and other pictures running normally.

Robot 36 images are 320 × 240 pixels. Completed pictures and usable partial pictures are saved automatically; partial items show their received line count. Lost audio, a stopped decoder or a dashboard restart can interrupt reception. A new image starts a fresh canvas, and old preview requests cannot retrieve a different image under the previous image ID. Noise and voice alone should not create a gallery entry, but a false synchronization is possible; enable SSTV only when needed.

The gallery retains at most **50 pictures or 100 MiB** of generated PNGs and removes the oldest first. It also removes its own oldest pictures when the filesystem approaches the **256 MiB** free-space floor. If that cannot recover space, image saving and decoding pause with an error. Unrelated files and audio archives are left alone. Images and metadata are private files in the dashboard state directory; they survive a dashboard restart. An incomplete picture still downloads as a fully formed PNG, with its unreceived area blank.

Reception uses the raw RX feed, including while live audio monitors outgoing repeater audio. No browser needs to stay open. This does not change radio frequencies or transmit settings. The live repeater's existing transmit timeout still applies when it repeats a data burst: Robot 36 lasts about 36 seconds plus its header, so test the full RF path before relying on it. Longer SSTV modes are not enabled by this version, and the receive switch does not increase any timeout.

## Receive packets

1. Sign in under **Settings / Admin**, then open **Data**.
2. Enable the receiver audio feed if it is off. The commissioned SvxLink receiver must already be running.
3. Turn on **Decode APRS packets** in the APRS panel.
4. Send compatible data from another device on the box's configured input frequency, using its required tone and channel settings. The data panel never changes these settings.
5. View arriving packets, or use **Download JSON** / **Download text**.

Decoding is independent of **Start listening**, browser volume and **Record traffic**. It continues with no browser open while enabled. The existing receiver-feed switch gates reception; switching that feed off pauses data input too. The decoder always uses the raw RX feed, even when listening and audio archives use the repeater's transmit mix. Outgoing voice prompts, IDs and broadcast messages are not fed back into the decoder.

The display uses the last verified local channel plan as context; it is not a live frequency reading from the handheld. Only packets heard on the actual current input can be decoded. Receiver squelch, CTCSS, audio level and FM bandwidth can prevent a modem burst from reaching the Pi cleanly. Successful speech reception does not prove a packet signal will decode.

## Logs and limits

The panel shows the latest 100 packets. Private storage retains at most **1,000 packets or 8 MiB**, removing the oldest when either bound is reached. Downloads contain the retained history, including UTC receipt time and source, destination, path and payload. JSON also preserves original frame bytes as hex. Nonprinting payload bytes are escaped for safe display, and packet text is never treated as HTML or a command.

Packet logs remain on the Pi across dashboard restarts, even when decoding is turned off. Export them before reflashing. An offline Pi without a real-time clock can have inaccurate timestamps until its clock is synchronized. Files stay in the dashboard's private state directory, separate from audio recordings and their retention settings.

Only CRC-valid supported packets are accepted. Audio dropouts reset the decoder rather than joining unrelated samples into a packet. The data worker runs outside the audio-feed handler with bounded queues so it cannot hold up repeating, live listening or recording. It has no transmitter control and does not decode DTMF credentials.

## Decoder source

The native dependency is [multimon-ng 1.6.1](https://github.com/EliasOenal/multimon-ng/tree/83761b00d5dd181171b28e8cd2f93b6983fadbc0). A small project patch changes APRS output to a single hex-encoded frame per line after the existing CRC check. Demodulation and CRC logic are unchanged. The original source archive, applied patch and upstream COPYING file are retained beside the installed binary.

SSTV uses the MIT-licensed [`sstv` 0.3.1 core](https://github.com/unexcellent/sstv/tree/9761c4c61a6353d7ae2173e1579e434e6c1b1ce3) with a fixed Robot 36 stdin-only adapter. The compatibility patch expresses three newer Rust constructs in Rust 1.85 syntax and adjusts the declared compiler minimum; it does not alter mode timings. The adapter emits bounded RGB scanlines and completion events. The dashboard produces PNGs using the Python standard library and never loads radio-supplied HTML or filesystem paths.
