# DTMF radio controls

This optional RF menu reads current control settings and changes traffic recording, automatic station identification, the preference to announce status after actions, and APRS/SSTV decoding after the native menu upgrade. It starts disabled. It cannot change channel pairs, program radios, bring the repeater online, enable transmission or run a shell command.

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

1. Hold PTT, send your six-character DTMF password followed by `#`, then release PTT. Listen for **“Control menu”** and the spaced options.
2. Send an option from the table below in a new transmission, then release PTT. Option 0 reads the settings and finishes the session. Options 1–5 ask for off or on.
3. For options 1–5, send `0#` for **off** or `1#` for **on**, then release PTT. Only this final step applies the action. A confirmation finishes the session; log in again for another action.

| Option | Function |
|---|---|
| `0#` | Read the configured pair number, recording state, automatic identification state, status-announcement preference and (with the upgraded menu) APRS/SSTV states |
| `1#` | Status announcements after successful DTMF actions |
| `2#` | Traffic recording now |
| `3#` | Automatic station identification; saves the same preference as the Admin switch |
| `4#` | APRS decoding off/on; requires the upgraded native menu and installed decoder |
| `5#` | SSTV decoding off/on; requires the upgraded native menu and installed decoder |

Option 0 is read-only and reports the pair and control states even when optional status announcements are off. Pair information is the verified stored plan, not live frequency telemetry. Changing automatic identification does not alter the selected ID interval or start a transmitter; manual identification remains the operator's responsibility when automatic identification is off.

Send `*` to cancel, then release PTT. In live mode, each opening or option prompt gives you a full 30 seconds of idle time after playback finishes. A valid option resets the window for its reply; invalid input does not extend it. A completed off/on action or option 0 report finishes the login, so enter the password again for another action. In receive-only mode, where no spoken prompt is sent, the 30-second window begins when the input is accepted. Missing playback feedback has a bounded 45-second fallback rather than leaving the menu open indefinitely. Expired, malformed or out-of-order input does not apply a setting. More than one submitted group in a single transmission is rejected.

Commands are completed after the receiver's audio squelch closes and at least 500 ms of quiet have elapsed. Native timer polling can add up to another second. The current receiver uses audio VOX rather than a direct carrier/PTT signal, so long silence while holding PTT can resemble a release. Send a complete group steadily, release PTT and wait for the prompt.

Live spoken replies also wait for the preceding transmission to finish before keying a fresh reply. This gives the prompt its own transmit-timeout window; a short unkeyed gap before the voice response is normal. Queued replies take priority over automatic Morse identification.

Recording on means the recorder waits for audio above its configured threshold. This changes its state now; it leaves **Record automatically after a restart**, recording thresholds and retention unchanged. Live listening remains independent.

**Spoken status after DTMF actions** defaults on. It appends the last verified stored pair number and current recording state after a successful off/on action. Turning it off suppresses that extra report; menu prompts and basic action confirmations remain. Stored pair status does not continuously measure the radios' active frequencies.

## Receive-only operation and privacy

The supplied monitor keeps `TX=NONE` and `NO_REPEAT=1`. The local controls can operate without keying a transmitter, but there are no on-air menu prompts or confirmations in this mode. Check the dashboard while testing the receive-only setup. Callsign identification, PTT behavior and the transmit audio path require the separate [commissioning checks](COMMISSIONING.md) before spoken responses can be sent on air.

The commissioned live service defers automatic Morse identification during a spoken menu. The Admin **Automatic station identification** switch controls automatic Morse separately; turning it off does not disable menu prompts, recording control or the spoken-status preference.

The installer generates the bounded prompt clips locally with `espeak-ng` at 120 words per minute, with explicit lead-in silence and pauses between the short opening options. It checks both individual clips and combined replies against a 12-second audio budget for the original menu and a 20-second budget for the extended menu before replacing installed clips. Only fixed response tokens cross the private bridge; arbitrary messages, paths and commands are not accepted. SvxLink's restricted handler consumes the received keypad symbols before native module or macro commands can run.

With **Include outgoing repeater audio** enabled, live listening and armed dashboard recordings use the live service's transmit-audio monitor. They include spoken menu conversations as well as forwarded traffic. This downstream feed mutes decoded DTMF digits. With that switch off or in receive-only mode, the raw receiver tap can still include received keypad tones. See [Live radio audio](LIVE_AUDIO.md) for source selection and message uploads.

DTMF passwords are transmitted as audible tones. Anyone able to hear or record them may recover the sequence. A stored hash protects local persistence; it does not encrypt radio traffic. Application logs filter native DTMF digit and command lines rather than retaining the password.

## Upgrade the native menu

The Admin updater installs the dashboard. Its separate root-owned SvxLink menu files and generated speech clips need one local upgrade for options 4 and 5. Until that upgrade finishes, existing DTMF controls 0–3 continue to work and Admin explains that decoder controls are unavailable.

Download the current reviewed source bundle, then run on the Pi:

```bash
sudo python3 scripts/install-dtmf-controls.py
```

The helper stages all scripts and generated prompts before changing services, takes the maintenance lock, preserves private settings and boot choices, briefly stops the previously running managed radio service, installs the matching files, and restarts that same service once. It does not start a service that was previously stopped. A protected file manifest enables the extended menu only after installation and restart succeed. If a failure requires recovery, it reports the problem and leaves radio services stopped for review.

Once Admin shows decoder controls ready, log in over radio as usual, send `4#` or `5#`, wait for its prompt, then send `0#` or `1#`. Enabling requires that decoder to be installed and available. Disabling is allowed even if the decoder is unavailable. The saved switch is shared with the dashboard, does not affect recording or browser listening, and resumes decoding when receiver audio becomes available. Existing received pictures and packet logs are preserved. Option 0 includes both decoder states with the extended menu.
