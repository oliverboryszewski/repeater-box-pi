# Live radio audio over the box's Wi-Fi

The Pi can send radio audio to the dashboard on a phone, laptop, or iPad. In commissioned live mode it includes received traffic and outgoing menu replies, identification and broadcast messages by default. Listening and recording are independent: closing the browser stops that listener; recording can continue on the Pi. The browser does not use your microphone or provide radio push-to-talk.

The normal installer starts the hardware-free bench engine. The separate `repeater-rx-monitor.service` is installed **disabled**. These manual steps enable only the receiving AIOC; `TX=NONE` creates a dummy transmitter. The template is checked against SvxLink 24.02 source, but its levels and device mapping still need verification on your hardware.

## Assign the real receive interface on the Pi

First install the current bundle using the main README. Run the following from its `Repeater_Box_Pi` directory on the Pi. Keep the **TX radio disconnected from its AIOC** during this phase. Label the interfaces and connect one at a time to map which device belongs to RX:

```bash
arecord -l
arecord -L
```

Use the observed RX ALSA identity, following the persistent-device mapping steps in [COMMISSIONING.md](COMMISSIONING.md). Do not guess `hw:1` or assume two AIOCs have unique default names. Confirm the mapping again after reboot and USB reconnect.

Copy the example and edit its `AUDIO_DEV` entry:

```bash
sudo cp config/svxlink-rx-monitor.conf.example /etc/repeater-box/svxlink-rx-monitor.conf
sudo nano /etc/repeater-box/svxlink-rx-monitor.conf
```

Replace only `REPLACE_RX_ALSA_ID` in `AUDIO_DEV=alsa:plughw:CARD=REPLACE_RX_ALSA_ID,DEV=0` with the actual mapped identity. Keep `TX=NONE`, `ONLINE=0`, `NO_REPEAT=1`, mono audio, `CARD_SAMPLE_RATE=48000`, and the localhost UDP destination unchanged. The configuration guard rejects an unmapped placeholder or an enabled transmitter.

Set the RX radio's intended input channel, squelch, and CTCSS decode from the station's private plan. On a fresh setup, configure the Pi's local catalog and program the radios as described in [RADIO_PROGRAMMING.md](RADIO_PROGRAMMING.md#configure-the-private-channel-pairs). The public package supplies no default radio frequencies. Turn radio VOX and battery saving off. Begin at a consistent speaker volume and adjust using received speech; watch for clipping. Its hardware squelch must mute idle speaker audio. The software VOX values in the example are starting points for calibration.

## Start the receive-only service

Stop and disable the bench unit before starting the monitor. Keep the distributor's `svxlink.service` masked. The monitor unit conflicts with both other engine units so they cannot run alongside it.

```bash
sudo systemctl disable --now repeater-engine.service
sudo systemctl start repeater-rx-monitor.service
sudo systemctl status repeater-rx-monitor.service --no-pager
sudo journalctl -u repeater-rx-monitor.service -n 50 --no-pager
```

Confirm that the log loads RX `RxAIOC`, TX `NONE`, and your intended sound device without initialization or distortion errors. A connected USB device alone does not confirm valid received speech. If the service fails, stop it and correct the mapping or permissions before retrying. The service requires the existing `svxlink` account's audio access; it does not open a serial/PTT port.

Once receive audio is verified, you may make this mode start after reboot:

```bash
sudo systemctl enable repeater-rx-monitor.service
```

To return to hardware-free bench mode:

```bash
sudo systemctl disable --now repeater-rx-monitor.service
sudo systemctl enable --now repeater-engine.service
```

## Listen and record in the dashboard

1. Join **Repeater-Box** Wi-Fi and open `http://10.42.0.1` (or the Pi's current dashboard address). Keep the Wi-Fi connection if the device reports no internet.
2. In **Settings / Admin → Audio and recordings**, sign in and select **Enable radio audio feed**, then save. This accepts the already configured local audio source; it does not assign an AIOC or key a radio. **Include outgoing repeater audio** defaults on and selects the complete repeater feed while live mode is running.
3. In **Live audio**, select **Start listening**. That tap lets the browser start playback. Adjust device/player volume. A short buffer delay is normal; backgrounding or locking a phone may suspend playback.
4. In **Audio archives**, use **Record traffic** to arm or stop the Pi recorder. Admin sign-in is required to change it. Stopping finalizes the current clip. Recordings can be played or downloaded after they close.
5. Adjust the recording threshold in **Settings / Admin** using real speech and idle audio. Lower thresholds catch quieter speech but can record noise. The **Record automatically after a restart** preference controls the next dashboard-service start; the Archives switch controls recording now.

Live listening remains available with recording off, and recording does not require an open browser. Use the monitor service log and the Live audio feed status to verify this phase.

For optional keypad control from a handheld, follow [DTMF status and recording control](DTMF_CONTROL.md). The feature starts disabled and uses a separate password configured in Admin or locally on the Pi. Its recording commands change the current recorder state, leaving the startup preference unchanged. SvxLink performs the tone decoding from the same receiver input it already owns; no second process opens the AIOC. Keep the monitor receive-only: `TX=NONE` and `NO_REPEAT=1` mean there is no on-air voice confirmation during this phase.

## What the selected feed contains

The dashboard uses one selected source for both live listeners and new recordings. When commissioned live mode is running and **Include outgoing repeater audio** is on, it selects SvxLink's transmit-audio monitor. This includes the forwarded received speech, DTMF menu prompts and confirmations, automatic station identification when enabled, and uploaded broadcast messages. The native audio path mutes decoded DTMF digits before this feed. The hub selects this source exclusively, so received speech is not doubled by adding the raw receiver feed to it.

With outgoing audio off, or in bench/receive-only mode, the selected source is the raw **RX speaker input**. SvxLink sends it to `127.0.0.1:7355` as headerless, mono, 48 kHz float32 little-endian samples. This tap is before software squelch, gain processing and DTMF muting, so it can include idle noise or keypad tones passed by the radio. It excludes outgoing audio. The separate native transmit monitor feeds `127.0.0.1:7356`; the dashboard converts its mono PCM samples into the browser stream. No additional process opens the AIOC capture device.

The Live audio panel names the current source. Its audio comes from the Pi's processing path; hearing a prompt here does not measure the radio's RF output or reception at another location. Use a second receiver for those checks.

The Overview shows the selected pair's last verified settings. Its small RX and TX activity dots update from the native receiver-squelch and transmit-command state while the dashboard is visible. An unavailable state clears the activity indication rather than leaving an old ON state displayed. These dots do not measure RF power or query the radios' displays.

The dashboard creates audio-level-triggered clips, with a short pre-roll and an approximately three-second quiet hang time. Files are 48 kHz mono **16-bit PCM WAV**. Outgoing replies are saved only while traffic recording is armed and outgoing audio is included. Changing source closes the current clip before starting a new one. Existing archives stay unchanged. This is independent of SvxLink's native QSO recorder, which is not enabled here. The dashboard applies its configured retention and storage limits to its own clips.

## Upload and broadcast a radio message

In **Settings / Admin → Daily operation → Radio messages**, choose a WAV, MP3 or M4A file and select **Upload message**. Each file may be up to 8 MiB and 30 seconds; the Pi decodes and checks its duration before saving it. The private library stores up to ten messages. Uploaded clips remain on the Pi rather than entering the public source repository.

Select a saved message to see its duration and preview it on your current device. Upload and preview do not transmit. **Broadcast once** opens a review; confirmation sends the selected clip once using the current configured pair and running live repeater. The radio channel and control menu must be idle. A changed clip, pair, live service or admin session requires a new review. The job panel shows preparation, playback and the result. There are no scheduled or repeating broadcasts, and an interrupted request is not replayed after a restart.

Use **Delete saved message** to free a library slot. Deleting a library clip does not remove previous traffic recordings that contain it. Uploaded names, previews and message controls require an admin session; ordinary live listening remains independent.

## Primary references

- [SvxLink 24.02 raw UDP tap and processing order](https://github.com/sm0svx/svxlink/blob/24.02/src/svxlink/trx/LocalRxBase.cpp)
- [SvxLink 24.02 configuration reference: RAW_AUDIO_UDP_DEST and sample rate](https://github.com/sm0svx/svxlink/blob/24.02/src/doc/man/svxlink.conf.5)
- [SvxLink 24.02 dummy transmitter for TX=NONE](https://github.com/sm0svx/svxlink/blob/24.02/src/svxlink/trx/Tx.cpp)
- [SvxLink 24.02 logic online/offline behavior](https://github.com/sm0svx/svxlink/blob/24.02/src/svxlink/svxlink/Logic.cpp)
- [AIOC audio and serial interface documentation](https://github.com/skuep/AIOC#how-to-use)
