# Live RX audio over the box's Wi-Fi

The Pi can send incoming radio audio to the dashboard on a phone, laptop, or iPad. Listening and recording are independent: closing the browser stops that listener; recording can continue on the Pi. The browser does not use your microphone or provide radio push-to-talk.

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
2. In **Settings / Admin**, sign in and select **Enable receiver audio feed**, then save. This accepts the already configured local audio source; it does not assign an AIOC or key a radio.
3. In **Live audio**, select **Start listening**. That tap lets the browser start playback. Adjust device/player volume. A short buffer delay is normal; backgrounding or locking a phone may suspend playback.
4. In **Audio archives**, use **Record traffic** to arm or stop the Pi recorder. Admin sign-in is required to change it. Stopping finalizes the current clip. Recordings can be played or downloaded after they close.
5. Adjust the recording threshold in **Settings / Admin** using real speech and idle audio. Lower thresholds catch quieter speech but can record noise. The **Record automatically after a restart** preference controls the next dashboard-service start; the Archives switch controls recording now.

Live listening remains available with recording off, and recording does not require an open browser. Use the monitor service log and the Live audio feed status to verify this phase.

For optional keypad control from a handheld, follow [DTMF status and recording control](DTMF_CONTROL.md). The feature starts disabled and uses a separate password configured in Admin or locally on the Pi. Its recording commands change the current recorder state, leaving the startup preference unchanged. SvxLink performs the tone decoding from the same receiver input it already owns; no second process opens the AIOC. Keep the monitor receive-only: `TX=NONE` and `NO_REPEAT=1` mean there is no on-air voice confirmation during this phase.

## What this feed contains

SvxLink sends its raw **RX speaker input** to `127.0.0.1:7355` as headerless, mono, 48 kHz float32 little-endian samples. The dashboard hub shares that one feed with listeners and its own recorder, avoiding a second ALSA capture device open. Leave the rate and format matched in both components.

The tap is before SvxLink's software squelch, gain processing, and DTMF muting. It can include idle noise or control tones if the radio passes them. It excludes the repeater's outgoing IDs, messages, and other transmit audio. This is an RX monitor, not a recording of the complete transmitted mix.

The dashboard creates separate audio-level-triggered RX clips, with a short pre-roll and an approximately three-second quiet hang time. Files are 48 kHz mono **16-bit PCM WAV** converted from the float input. This is independent of SvxLink's native QSO recorder, which is not enabled here. The dashboard controls its own clips and applies its configured retention/storage limits to those clips.

## Primary references

- [SvxLink 24.02 raw UDP tap and processing order](https://github.com/sm0svx/svxlink/blob/24.02/src/svxlink/trx/LocalRxBase.cpp)
- [SvxLink 24.02 configuration reference: RAW_AUDIO_UDP_DEST and sample rate](https://github.com/sm0svx/svxlink/blob/24.02/src/doc/man/svxlink.conf.5)
- [SvxLink 24.02 dummy transmitter for TX=NONE](https://github.com/sm0svx/svxlink/blob/24.02/src/svxlink/trx/Tx.cpp)
- [SvxLink 24.02 logic online/offline behavior](https://github.com/sm0svx/svxlink/blob/24.02/src/svxlink/svxlink/Logic.cpp)
- [AIOC audio and serial interface documentation](https://github.com/skuep/AIOC#how-to-use)
