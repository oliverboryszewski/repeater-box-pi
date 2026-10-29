# Live repeater commissioning

The optional live service joins the already tested receiver and transmitter. The installer supplies the service and helpers but does not activate it. Keep private callsigns, operating frequencies, USB identities and radio backups on the Pi.

Before this stage, establish persistent RX/TX device identities, a verified channel pair, clear receive audio in the dashboard, and successful isolated TX key/release and audio tests. Fit suitable antennas or a test load, keep the TX power switch accessible and check receiver isolation while transmitting. An ordinary audio listening test does not measure FM deviation.

## Prepare private station configuration

With no radio programming or software maintenance task running:

```bash
sudo python3 /opt/repeater-box/scripts/configure-live.py --prepare
```

Enter the station callsign locally. The helper derives the receiver from the validated receive-monitor configuration and the transmitter from the existing radio bindings. It writes `/etc/repeater-box/svxlink-live.conf` and a protected local authorization record. Preparation leaves live authorization disabled and does not start a service.

The initial transmitter audio gain is −40 dB and its continuous transmit timeout is 15 seconds. These are commissioning limits. Audio gain changes modulation level, not RF output power. The radio's own VOX must remain off. The factory AIOC serial path uses DTR asserted with RTS clear; initialization clears DTR before RTS.

If received speech is too quiet, stop the live service and prepare again with a modest gain increase, such as `--prepare --tx-gain-db -34`. Re-enter the callsign locally, authorize and restart for another short test. The helper accepts −40 through −10 dB and binds the chosen gain to the private authorization. Start quietly and adjust in small steps; the allowed range is not a measured calibration for every radio.

## Start a supervised test

After the separate PTT and audio checks have passed:

```bash
sudo python3 /opt/repeater-box/scripts/configure-live.py --enable --confirm-ptt-tested --confirm-audio-tested
sudo systemctl start repeater-live.service
sudo systemctl status repeater-live.service --no-pager
```

Starting live mode stops the mutually exclusive receive monitor or bench engine. The root startup guard checks local authorization, protected configuration, current device bindings, the private channel catalog and both radios' verified pair state before SvxLink opens a transmitter. On boot it waits up to 30 seconds for the specific mapped serial interfaces and ALSA names to appear. Each retry repeats the full authorization and verification checks; mismatched devices and invalid configuration fail immediately. If devices remain absent, startup stops at the deadline. The service has no automatic restart loop. It refuses unfinished programming work, changed bindings or a changed catalog. It does not reread radio frequencies or prove physical cable placement.

Live startup is silent. Automatic Morse identification groups turns separated by less than 30 seconds into one conversation. A due ID waits for RX closed, TX idle and the spoken menu to finish. The default interval is nine minutes, with recent IDs suppressing another automatic ID; idle time alone never generates a beacon. The CW policy needs no external SvxLink voice pack. Private radio-menu prompts use the project's locally generated audio files.

In **Settings / Admin**, the confirmed **Automatic station identification** switch saves whether automatic Morse is enabled. Turning it off suppresses future automatic IDs while spoken DTMF prompts continue. An ID already playing may finish. The live engine applies the preference within about one second and retains its last valid preference through a dashboard restart. Turning it on again waits for new traffic instead of transmitting immediately. Manual identification remains the operator's responsibility.

For deliberate bench testing, preparation also accepts `--id-interval-minutes 30`, for example together with a locally established audio gain. This starts a 30-minute automatic-ID cooldown without a startup transmission. It does not replace on-air identification obligations: in the US, identify on the transmitting channel at communication end and at least every ten minutes during a communication. Use manual identification as required when automatic identification is off or the testing profile is selected.

Use a brief spoken test from a handheld programmed for the configured split pair. A separate receiver on the output frequency lets you hear speech while the test handheld is transmitting. Check that the output stops promptly after release and that RX still hears a weak signal while TX is active. Then test the authenticated DTMF menu one step per transmission. The dashboard's Live audio and archives contain incoming receiver audio; they do not include the transmitter's own identification or prompts.

To stop the test immediately:

```bash
sudo systemctl stop repeater-live.service
```

The service is initially disabled for boot and has no automatic restart policy. Keep a physical TX power switch within reach during commissioning.

## Programming and later changes

The programming worker stops live RF together with the other managed radio services. A successful verified operation may restore the previously active mode. Failed verification, uncertain connection release or an incomplete paired operation leaves radio services paused and disables their boot startup. Resolve the recorded recovery before starting live RF again.

The live authorization covers the accepted private catalog and physical adapter bindings, so switching to another fully verified pair in that same catalog does not require entering the callsign again. Changing the catalog or adapter mapping requires preparing and authorizing live configuration again.

Once identification, repeated speech, DTMF responses, audio level and receiver isolation have passed physical tests, boot startup can be enabled deliberately:

```bash
sudo systemctl disable repeater-engine.service repeater-rx-monitor.service
sudo systemctl enable repeater-live.service
```

A manual installer upgrade stops live operation while replacing files and preserves an existing enabled-at-boot preference. It leaves live operation stopped until deliberately started or the Pi boots again. An unfinished radio upload still disables radio startup until recovery succeeds. Compatible dashboard-only updates preserve the running radio service.

## References

- [SvxLink 24.02 configuration manual](https://github.com/sm0svx/svxlink/blob/24.02/src/doc/man/svxlink.conf.5)
- [Native serial PTT initialization](https://github.com/sm0svx/svxlink/blob/24.02/src/svxlink/trx/PttSerialPin.cpp)
- [AIOC factory settings](https://github.com/skuep/AIOC/blob/v1.4.1/stm32/aioc-fw/Src/settings.h)
- [US amateur station identification](https://www.ecfr.gov/current/title-47/chapter-I/subchapter-D/part-97/subpart-B/section-97.119)
