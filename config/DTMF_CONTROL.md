# DTMF status and recording control

This optional RF menu controls the dashboard's traffic recorder and whether successful DTMF actions are followed by a spoken status report. It starts disabled. It cannot change channel pairs, program radios, bring the repeater online, enable transmission or run a shell command.

Commission the [receive-only audio monitor](LIVE_AUDIO.md) first. SvxLink is the sole owner of the receiver's ALSA device: its native decoder handles the tones, while the existing local audio feed supplies browser listening and dashboard recordings. The feature does not add another audio capture process.

## Set or change the DTMF password

In **Settings / Admin**, sign in and find **Radio DTMF control**. Use **Change DTMF password**, enter the new password in its single visible field, then enter the current dashboard admin password. The DTMF field accepts at most six characters from `0–9` and `A–D`, converts lowercase letters to uppercase, and removes unsupported characters. Save only after all six characters are entered. The keypad's `#` means submit and `*` means cancel, so neither belongs in the password. Use a different password from the dashboard admin and Wi-Fi passwords.

There is no default password. It is never returned to the browser or displayed after saving. The local root worker receives a salted PBKDF2 hash for persistence rather than the plaintext password. Changing the password ends any active RF menu session.

The local terminal remains an alternative for initial configuration or recovery:

```bash
sudo python3 /opt/repeater-box/scripts/configure-dtmf.py
```

Enter the password twice at the hidden prompts. The protected local record is `/etc/repeater-box/dtmf.json`. Neither a password nor its hash belongs in the public checkout or chat.

The installer updates an existing mapped receive-monitor configuration and restarts an already active monitor only after checking for unfinished radio work. If Admin password setup reports that the receive monitor needs a restart, or the local command reports that it updated the receive-only configuration, finish any radio maintenance and then run this on the Pi:

```bash
sudo systemctl restart repeater-rx-monitor.service
```

This conditional restart loads the decoder hooks; it does not select or enable a transmitter. An existing monitor already migrated by the installer needs no additional restart just to set its password. Migration uses `ONLINE=1` only with the fixed restricted event handler, `TX=NONE` and `NO_REPEAT=1` so native callbacks can run. The hardware-free bench logic stays offline.

Once the password and local bridge are ready, select **Enable DTMF control** and confirm. **Not configured** means a password still needs setup; **Bridge unavailable** means the local command path is not ready. The same switch disables control and ends an active RF session.

## Use the RF menu

Use a handheld programmed for the station's private input channel and tone. Send each group in its own transmission and release PTT afterward. Wait for the response before sending the next group; do not send the whole interaction in one transmission.

1. Hold PTT, send your six-character DTMF password followed by `#`, then release PTT. The menu prompt is **“DTMF programming, enter option.”**
2. Send `1#` for **status announcements** or `2#` for **recording**, then release PTT. The next prompt confirms the selected option and requests off or on.
3. Send `0#` for **off** or `1#` for **on**, then release PTT. Only this final step applies the action. A confirmation finishes the session; log in again for another action.

Send `*` to cancel, then release PTT. A session has a 30-second absolute lifetime; the timer is not extended by each menu step. Expired, malformed or out-of-order input does not apply a setting. More than one submitted group in a single transmission is rejected.

Commands are completed after the receiver's audio squelch closes and at least 500 ms of quiet have elapsed. Native timer polling can add up to another second. The current receiver uses audio VOX rather than a direct carrier/PTT signal, so long silence while holding PTT can resemble a release. Send a complete group steadily, release PTT and wait for the prompt.

Recording on means the recorder waits for audio above its configured threshold. This changes its state now; it leaves **Record automatically after a restart**, recording thresholds and retention unchanged. Live listening remains independent.

**Spoken status after DTMF actions** defaults on. It appends the last verified stored pair number and current recording state after a successful off/on action. Turning it off suppresses that extra report; menu prompts and basic action confirmations remain. Stored pair status does not continuously measure the radios' active frequencies.

## Receive-only operation and privacy

The supplied monitor keeps `TX=NONE` and `NO_REPEAT=1`. The local controls can operate without keying a transmitter, but there are no on-air menu prompts or confirmations in this mode. Check the dashboard while testing the receive-only setup. Callsign identification, PTT behavior and the transmit audio path require the separate [commissioning checks](COMMISSIONING.md) before spoken responses can be sent on air.

The commissioned live service defers automatic Morse identification during a spoken menu. The Admin **Automatic station identification** switch controls automatic Morse separately; turning it off does not disable menu prompts, recording control or the spoken-status preference.

The installer generates the bounded prompt clips locally with `espeak-ng`. Only fixed response tokens cross the private bridge; arbitrary messages, paths and commands are not accepted. SvxLink's restricted handler consumes the received keypad symbols before native module or macro commands can run.

DTMF passwords are transmitted as audible tones. Anyone able to hear or record them may recover the sequence. A stored hash protects local persistence; it does not encrypt radio traffic. The raw live-audio tap and dashboard recordings can contain received control tones even though SvxLink mutes them in its downstream audio path. Application logs filter native DTMF digit and command lines rather than retaining the password.
