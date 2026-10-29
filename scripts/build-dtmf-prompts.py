#!/usr/bin/python3
"""Generate fixed generic local prompts; never read a callsign or frequency."""
import os
from pathlib import Path
import subprocess
import tempfile
import wave

SPEECH_RATE = 120
SAMPLE_RATE = 16000
LEAD_SECONDS = 0.25
MENU_GAP_SECONDS = 0.30
TAIL_SECONDS = 0.15
MAX_RESPONSE_SECONDS = 12
MENU_PARTS = ("Control menu.", "Zero, settings.", "One, status.",
              "Two, recording.", "Three, identification.")

PROMPTS = {"status": "Current settings.", "recording_on": "Recording on.",
           "recording_off": "Recording off.", "busy": "Radio maintenance in progress. Please wait.",
           "unavailable": "Control unavailable.", "pair_unknown": "Channel pair not confirmed.",
           "authenticated": " ".join(MENU_PARTS),
           "choose_status": "Status announcements. Zero off. One on.",
           "choose_recording": "Recording. Zero off. One on.",
           "choose_identification": "Automatic identification. Zero off. One on.",
           "status_on": "Announcements on.", "status_off": "Announcements off.",
           "identification_on": "Identification on.",
           "identification_off": "Identification off.",
           "cancelled": "DTMF programming cancelled.", "invalid": "Invalid option. DTMF programming cancelled.",
           "expired": "DTMF programming timed out. Enter the control code again."}
PROMPTS.update({"pair_" + str(number): "Pair " + str(number) + "." for number in range(1, 10)})


def read_pcm(path):
    with wave.open(str(path), "rb") as stream:
        if (stream.getnchannels(), stream.getsampwidth(), stream.getframerate(), stream.getcomptype()) != (1, 2, SAMPLE_RATE, "NONE"):
            raise ValueError("Generated prompt format is invalid")
        frames = stream.readframes(stream.getnframes())
        if not frames or len(frames) != stream.getnframes() * 2:
            raise ValueError("Generated prompt audio is empty or incomplete")
        return frames


def render_phrase(text, source, output):
    subprocess.run(["/usr/bin/espeak-ng", "-v", "en-us", "-s", str(SPEECH_RATE), "-z",
                    "-w", str(source), text], check=True)
    subprocess.run(["/usr/bin/sox", str(source), "-r", str(SAMPLE_RATE), "-c", "1",
                    "-b", "16", "-e", "signed-integer", str(output)], check=True)
    return read_pcm(output)


def silence(seconds):
    return b"\x00\x00" * round(SAMPLE_RATE * seconds)


def write_prompt(path, parts):
    audio = silence(LEAD_SECONDS) + silence(MENU_GAP_SECONDS).join(parts) + silence(TAIL_SECONDS)
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(SAMPLE_RATE)
        stream.writeframes(audio)
    return len(audio) / (SAMPLE_RATE * 2)


def validate_durations(durations):
    if any(not 0 < seconds <= MAX_RESPONSE_SECONDS for seconds in durations.values()):
        raise ValueError("A spoken prompt exceeds the short transmit budget")
    pair_duration = max([durations["pair_unknown"]] + [durations["pair_" + str(n)] for n in range(1, 10)])
    summary = durations["status"] + pair_duration
    summary += max(durations["recording_on"], durations["recording_off"])
    acknowledgements = ("status_on", "status_off", "recording_on", "recording_off",
                        "identification_on", "identification_off")
    if summary + max(durations[name] for name in acknowledgements) > MAX_RESPONSE_SECONDS:
        raise ValueError("The combined spoken status exceeds the short transmit budget")
    explicit_settings = summary + max(durations["identification_on"], durations["identification_off"])
    explicit_settings += max(durations["status_on"], durations["status_off"])
    if explicit_settings > MAX_RESPONSE_SECONDS:
        raise ValueError("The combined current-settings response exceeds the short transmit budget")


def build(directory=Path("/opt/repeater-box/prompts")):
    directory = Path(directory)
    directory.mkdir(mode=0o755, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".repeater-prompts-", dir=directory) as temporary:
        pending, durations = {}, {}
        for name, text in PROMPTS.items():
            texts = MENU_PARTS if name == "authenticated" else (text,)
            parts = []
            for index, phrase in enumerate(texts):
                source = Path(temporary) / f"{name}-{index}-source.wav"
                converted = Path(temporary) / f"{name}-{index}-16k.wav"
                parts.append(render_phrase(phrase, source, converted))
            output = Path(temporary) / (name + ".wav")
            durations[name] = write_prompt(output, parts)
            os.chmod(output, 0o644)
            pending[name] = output
        # Validate the complete menu and longest acknowledgement before replacing
        # any installed clip. No generated text or private station data is read.
        validate_durations(durations)
        for name, output in pending.items():
            os.replace(output, directory / (name + ".wav"))
        return durations


if __name__ == "__main__":
    if os.geteuid() != 0:
        raise SystemExit("Run this installer helper as root.")
    durations = build()
    print(f"Generated {len(durations)} radio prompts; opening menu {durations['authenticated']:.1f} seconds.")
