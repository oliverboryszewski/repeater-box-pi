#!/usr/bin/python3
"""Generate fixed generic local prompts; never read a callsign or frequency."""
import os
from pathlib import Path
import subprocess
import tempfile
import wave

PROMPTS = {"status": "Repeater box status.", "recording_on": "Recording on.",
           "recording_off": "Recording off.", "busy": "Radio maintenance in progress. Please wait.",
           "unavailable": "Control unavailable.", "pair_unknown": "Channel pair not confirmed.",
           "authenticated": "DTMF programming. Enter option. One, status announcements. Two, recording.",
           "choose_status": "Status announcements. Zero off. One on.",
           "choose_recording": "Recording. Zero off. One on.",
           "status_on": "Status announcements on.", "status_off": "Status announcements off.",
           "cancelled": "DTMF programming cancelled.", "invalid": "Invalid option. DTMF programming cancelled.",
           "expired": "DTMF programming timed out. Enter the control code again."}
PROMPTS.update({"pair_" + str(number): "Channel pair " + str(number) + " configured." for number in range(1, 10)})


def build(directory=Path("/opt/repeater-box/prompts")):
    directory = Path(directory)
    directory.mkdir(mode=0o755, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".repeater-prompts-", dir=directory) as temporary:
        for name, text in PROMPTS.items():
            source = Path(temporary) / (name + ".wav")
            output = Path(temporary) / (name + "-16k.wav")
            subprocess.run(["/usr/bin/espeak-ng", "-v", "en-us", "-s", "145", "-w", str(source), text], check=True)
            subprocess.run(["/usr/bin/sox", str(source), "-r", "16000", "-c", "1", "-b", "16", "-e", "signed-integer", str(output)], check=True)
            with wave.open(str(output), "rb") as stream:
                if (stream.getnchannels(), stream.getsampwidth(), stream.getframerate(), stream.getcomptype()) != (1, 2, 16000, "NONE"):
                    raise ValueError("Generated prompt format is invalid")
            os.chmod(output, 0o644)
            os.replace(output, directory / (name + ".wav"))


if __name__ == "__main__":
    if os.geteuid() != 0:
        raise SystemExit("Run this installer helper as root.")
    build()
