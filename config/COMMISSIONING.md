# Repeater engine commissioning

These files are a software starting point for a Pi 4 with two separate NA6D AIOCs. Configuration syntax has been checked against upstream SvxLink 24.02 and Debian's 24.02 package. No Pi, AIOC, or radio has been tested with these settings yet.

## Bench mode while parts arrive

`svxlink-bench.conf` uses `RX=NONE`, `TX=NONE`, and `ONLINE=0`. In SvxLink 24.02, the `NONE` RX/TX names instantiate dummy devices. They do not open an ALSA sound card or serial PTT port. Identification, the recorder, radio DTMF administration, scheduled messages, EchoLink, and reflector connections are not configured. `N0CALL` is a bench placeholder.

The only engine unit to enable at this stage is the project's `repeater-engine.service`. Keep the packaged `svxlink.service` masked so that an update cannot start its unrelated default configuration. The project unit must run as the unprivileged `svxlink` account and create `/run/repeater-box` writable by that account before startup. Its foreground command is:

```text
/usr/bin/svxlink --config=/etc/repeater-box/svxlink-bench.conf
```

Do not add `--daemon` to a normal systemd foreground service. Omitting `--logfile` leaves stdout/stderr available to the journal. For a manual check with the project engine stopped, use the same command as the `svxlink` account, with `/run/repeater-box` already present. There is no documented check-only CLI flag; starting the bench configuration is the useful initialization check. Confirm the log loads RX `NONE` and TX `NONE`, and reports successful event handler initialization. It should not enumerate a radio audio device or open a PTT serial port.

The Debian package installs `/usr/share/svxlink/events.tcl` and its supporting Tcl files. Voice sound clips are separate upstream downloads. They are not required for this silent bench configuration. Before voice IDs or spoken status, install the language pack matching the installed engine and verify its `Core`/`Default` clips under `/usr/share/svxlink/sounds/en_US`.

## Map the actual interfaces first

1. Keep the TX radio disconnected from its AIOC while discovering interfaces. Label one AIOC RX and the other TX; connect one at a time and save `arecord -l`, `aplay -l`, `ls -l /dev/serial/by-id/`, and `udevadm info` for each. These are discovery commands, not a PTT test.
2. Confirm each device's USB serial number, USB path, ALSA identity, and tty device. Do not rely on `hw:1`, `hw:2`, or `/dev/ttyACM0` remaining stable after reboot. Two identical audio devices can also collide in their default ALSA names. Establish persistent aliases from the actual observed identities or physical USB paths, then repeat the mapping after a reboot and unplug/replug.
3. The service account needs `audio` access and permission to the selected serial device, normally through `dialout`. Verify actual permissions. An RX AIOC using software VOX does not need its programming/PTT serial port opened.
4. Confirm the installed AIOC firmware and settings before using serial PTT. Upstream default firmware from 1.2.0 keys with DTR asserted and RTS clear. SvxLink's later live TX settings are `PTT_TYPE=SerialPin`, `PTT_PIN=DTR`, `SERIAL_SET_PINS=!RTS`, and the TX AIOC's persistent `PTT_PORT`. Opening a serial port may change line states, so initial mapping and behavior checks occur with the radio unplugged.

## Commission receive audio, then transmission

1. Keep `TX=NONE` while replacing the bench receiver with the RX AIOC configuration. Set the RX radio's intended input frequency, hardware squelch, and CTCSS decode. Turn radio VOX off and battery saving off. Audio comes from its speaker socket, so the radio has already applied audio filtering/deemphasis; leave `DEEMPHASIS=0` and do not assume its speaker output preserves CTCSS for a second software tone detector.
2. Start with a consistent RX volume and adjust capture level using real received speech. Avoid clipping; verify that an idle muted radio stays closed. `VOX_THRESH=1000` and `SQL_HANGTIME=1000` are uncalibrated starting values. Software VOX detects audio activity, so long speech pauses may close it and reopen it. Evaluate its delay and first-syllable behavior before calling the repeater ready. A properly supported hardware COS solution could improve this later.
3. `svxlink-live.conf.example` is a reference only. Do not overwrite the bench configuration or change the engine unit to it automatically. Replace its device/callsign placeholders and validate the RX and TX identities independently. Keep `ONLINE=0` and `NO_REPEAT=1` until deliberate testing. A live `Local` TX opens a real device even while offline, unlike `TX=NONE`.
4. With an appropriate RF load/antenna and controlled brief transmissions, verify idle PTT, key/unkey, service stop, reboot, USB reconnect, and TX timeout. Turn off the TX radio's VOX and disable any AIOC audio-triggered PTT feature. Use a conservative output audio level, then calibrate intelligible speech without overdeviation. The example limiter/delay settings are not measurements. Keep radio and software timeouts. Account for radio heating and transmitter interference with the nearby receiver.
5. Set the TX radio's output frequency and CTCSS encode. This template does not synthesize CTCSS in software. Install and verify station identification with the real callsign before on-air operation. Explicitly switch `NO_REPEAT=0` and bring the commissioned logic online only when those checks pass.

## Recording, messages, and control later

The commented recorder example is optional. Enable it only after audio commissioning and directory permissions are correct. With `QSO_RECORDER=:QsoRecorder`, the colon is required and RF DTMF recorder control is omitted. `MIN_TIME` is milliseconds; file durations are seconds; `MAX_DIRSIZE` is MB. SvxLink deletes its oldest `qsorec_` files when a file closes, allowing at most one additional recording beyond the limit. Recordings contain receiver/module/link audio, excluding announcements. Do not repurpose the directory for unrelated files.

SvxLink's internal decoder supports DTMF on received audio. Administrative commands must be chosen deliberately; no default RF administration password is supplied here. An over-the-air DTMF sequence is observable and should not be treated as a private web-login credential.

The configured state PTY is a local text event stream. The command PTY supports commands such as `CFG RepeaterLogic ONLINE 0` and calls to Tcl event functions. These are local integration points for a later dashboard, not HTTP endpoints. Only a trusted local service should receive access. Timed custom messages need a reviewed Tcl handler/scheduler and explicit configuration; no timer can key the transmitter in the supplied bench mode.

## Primary references

- [Debian Trixie SvxLink server package, version and architectures](https://packages.debian.org/trixie/svxlink-server)
- [Files installed by the Debian arm64 package](https://packages.debian.org/trixie/arm64/svxlink-server/filelist)
- [Debian SvxLink CLI manual](https://manpages.debian.org/trixie/svxlink-server/svxlink.1.en.html)
- [Upstream 24.02 sample configuration](https://github.com/sm0svx/svxlink/blob/24.02/src/svxlink/svxlink/svxlink.conf.in)
- [Upstream 24.02 configuration manual](https://github.com/sm0svx/svxlink/blob/24.02/src/doc/man/svxlink.conf.5)
- [24.02 receiver factory: NONE creates a dummy receiver](https://github.com/sm0svx/svxlink/blob/24.02/src/svxlink/trx/Rx.cpp)
- [24.02 transmitter factory: NONE creates a dummy transmitter](https://github.com/sm0svx/svxlink/blob/24.02/src/svxlink/trx/Tx.cpp)
- [Upstream English voice language packs](https://github.com/sm0svx/svxlink-sounds-en_US-heather/releases)
- [AIOC upstream: serial PTT, mono 48 kHz audio, firmware behavior](https://github.com/skuep/AIOC#how-to-use)
- [NA6D AIOC getting started](https://na6d.com/pages/aioc-getting-started)

The current upstream engine has features newer than Debian Trixie's package. Use the installed `man svxlink.conf` as the final authority for that Pi. In particular, do not copy new-master-only keys into an older packaged engine without checking them.
