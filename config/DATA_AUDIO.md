# Live data decoding

The optional Live audio data panel decodes **APRS / AX.25 UI packets using AFSK1200** from the box receiver. It works without internet after installation. Admin sign-in is required to enable decoding, view packets or download a log.

This is a first supported format, not a universal audio-data decoder. SSTV images, digital voice, arbitrary modem formats and file reconstruction need different decoders. JSON and text downloads contain the packets that this box successfully received; they are not recovered attachments.

## One-time installation

First install the dashboard normally and commission its receiver feed. On the Pi, from a current trusted source checkout, run:

```bash
cd ~/Repeater_Box_Pi
sudo bash scripts/install-data-decoder.sh
```

Alternatively, a fresh full installation copies the helper and its patch into `/opt/repeater-box/scripts`; run `sudo bash /opt/repeater-box/scripts/install-data-decoder.sh` there. App-only dashboard updates do not replace these native prerequisites. The helper is optional and is not run by the main installer.

Internet is needed only for this installation. The helper verifies pinned multimon-ng source and an output-only patch, builds a stdin-only decoder, then installs it with its license, corresponding source, patch and integrity metadata under `/opt/repeater-box/vendor/multimon-ng`. It may install the compiler, Make and CMake first. It does not open a radio device, change PTT, retune either radio, stop services or change boot startup. A maintenance lock prevents overlapping programming or updates. Retrying the helper is safe; an intact matching installation is retained.

The dashboard checks the native binary and its provenance. Until the compatible decoder is present, the panel explains the missing prerequisite. It never falls back to an unrelated system decoder.

## Receive packets

1. Sign in under **Settings / Admin**, then open **Live audio**.
2. Enable the receiver audio feed if it is off. The commissioned SvxLink receiver must already be running.
3. Turn on **APRS decoding** in the data panel.
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
