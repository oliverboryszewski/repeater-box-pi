#!/usr/bin/env python3
"""Guard bench/receive-only SvxLink services against a real transmitter."""
import configparser
import os
from pathlib import Path
import stat
import sys

EVENT_HANDLER = Path("/opt/repeater-box/scripts/repeater-box-events.tcl")


def trusted_handler(path=EVENT_HANDLER, owner_uid=0):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != owner_uid or info.st_mode & 0o022:
            raise ValueError("The restricted DTMF event handler must be root-owned and protected")
    finally:
        os.close(descriptor)


def validate_parser(parser, rx_monitor=False, event_handler=EVENT_HANDLER, owner_uid=0):
    if parser["GLOBAL"].get("CFG_DIR") or parser["GLOBAL"].get("LINKS"):
        raise ValueError("Bench services must not load external configuration or links")
    logics = [name.strip() for name in parser["GLOBAL"]["LOGICS"].split(",")]
    if not logics or any(not name for name in logics):
        raise ValueError("No bench logic configured")
    for name in logics:
        logic = parser[name]
        if logic.get("TX") != "NONE":
            raise ValueError("These services require TX=NONE in every active logic")
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
            if logic.get("ONLINE") == "1":
                if (len(logics) != 1 or name != "RepeaterLogic" or logic.get("NO_REPEAT") != "1"
                        or logic.get("EVENT_HANDLER") != str(event_handler)
                        or receiver.get("DTMF_DEC_TYPE") != "INTERNAL" or receiver.get("DTMF_MUTING") != "1"
                        or logic.get("MODULES") or logic.get("MACROS") or logic.get("ONLINE_CMD")
                        or logic.get("ACTIVATE_MODULE_ON_LONG_CMD") or logic.get("EXEC_CMD_ON_SQL_CLOSE") != "500"
                        or logic.get("COMMAND_PTY") != "/run/repeater-rx-monitor/svxlink-command"):
                    raise ValueError("Online receive monitor requires the restricted DTMF handler and no repeating or native RF commands")
                trusted_handler(event_handler, owner_uid)
            elif logic.get("ONLINE") != "0":
                raise ValueError("Receive monitor ONLINE must be 0 or restricted DTMF mode 1")
        elif rx != "NONE" or logic.get("ONLINE") != "0":
            raise ValueError("Bench engine requires RX=NONE and ONLINE=0")


def load_config(path):
    parser = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=("#", ";"))
    parser.optionxform = str
    if not parser.read(path):
        raise ValueError("Bench configuration not found")
    return parser


def main(argv):
    try:
        validate_parser(load_config(argv[0]), rx_monitor="--rx-monitor" in argv[1:])
    except (KeyError, ValueError, configparser.Error, OSError, IndexError) as error:
        sys.exit(str(error))


if __name__ == "__main__":
    main(sys.argv[1:])
