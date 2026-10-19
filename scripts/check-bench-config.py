#!/usr/bin/env python3
"""Guard the bench and receive-only services against a real transmitter."""
import configparser
import sys

parser = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=("#", ";"))
try:
    if not parser.read(sys.argv[1]):
        raise ValueError("Bench configuration not found")
    if parser["GLOBAL"].get("CFG_DIR"):
        raise ValueError("The bench configuration must not include an external CFG_DIR")
    logics = [name.strip() for name in parser["GLOBAL"]["LOGICS"].split(",")]
    if not logics or any(not name for name in logics):
        raise ValueError("No bench logic configured")
    for name in logics:
        logic = parser[name]
        rx_monitor = "--rx-monitor" in sys.argv[2:]
        if logic.get("TX") != "NONE" or logic.get("ONLINE") != "0":
            raise ValueError("These services require TX=NONE and ONLINE=0 in every active logic")
        rx = logic.get("RX")
        if rx_monitor:
            receiver = parser[rx]
            if logic.get("TYPE") != "Repeater" or receiver.get("TYPE") != "Local" or receiver.get("SQL_DET") != "VOX":
                raise ValueError("Receive monitor requires a local audio receiver with software VOX")
            device = receiver.get("AUDIO_DEV", "")
            if not device.startswith("alsa:") or "REPLACE_" in device:
                raise ValueError("Replace the RX ALSA device placeholder with the verified device identity")
            if parser["GLOBAL"].get("CARD_SAMPLE_RATE") != "48000" or receiver.get("RAW_AUDIO_UDP_DEST") != "127.0.0.1:7355":
                raise ValueError("Receiver feed must be 48000 Hz to 127.0.0.1:7355")
        elif rx != "NONE":
            raise ValueError("Bench engine requires RX=NONE")
except (KeyError, ValueError, configparser.Error, OSError, IndexError) as error:
    sys.exit(str(error))
