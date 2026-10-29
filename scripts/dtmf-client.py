#!/usr/bin/python3
"""Pass one RF command on stdin to the local restricted dashboard bridge."""
import json
import os
from pathlib import Path
import re
import socket
import stat
import sys

SOCKET_PATH = "/run/repeater-box-control/dtmf.sock"
COMMAND_PATHS = (Path("/run/repeater-rx-monitor/svxlink-command"),
                 Path("/run/repeater-box/svxlink-command"))
RESULTS = frozenset(("status", "authenticated", "choose_status", "choose_recording", "choose_identification", "status_on", "status_off",
                     "recording_on", "recording_off", "identification_on", "identification_off", "cancelled", "invalid", "expired", "disabled", "denied", "busy", "unavailable"))
DATA_RESULTS = RESULTS | frozenset(("choose_aprs", "choose_sstv", "aprs_on", "aprs_off", "sstv_on", "sstv_off"))
PROMPT_RE = re.compile(r"[0-9a-f]{32}\Z")


def validate_response(value):
    schema = value.get("schema") if isinstance(value, dict) else None
    fields = {"schema", "result", "pair_index", "recording", "announce_status", "identification_enabled", "prompt_id"}
    if schema == 2:
        fields.update(("aprs_enabled", "sstv_enabled"))
    if (not isinstance(value, dict) or set(value) != fields
            or type(schema) is not int or schema not in (1, 2)
            or not isinstance(value["result"], str) or value["result"] not in (DATA_RESULTS if schema == 2 else RESULTS) or type(value["pair_index"]) is not int
            or not 0 <= value["pair_index"] <= 9 or type(value["recording"]) is not bool
            or type(value["announce_status"]) is not bool or type(value["identification_enabled"]) is not bool
            or schema == 2 and (type(value["aprs_enabled"]) is not bool or type(value["sstv_enabled"]) is not bool)
            or not isinstance(value["prompt_id"], str)
            or value["prompt_id"] != "" and PROMPT_RE.fullmatch(value["prompt_id"]) is None):
        raise ValueError("Invalid local control response")
    return value


def _exchange(payload, socket_path, validator):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(2)
        connection.connect(socket_path)
        connection.sendall((json.dumps(payload, separators=(",", ":")) + "\n").encode("ascii"))
        data = bytearray()
        while b"\n" not in data and len(data) <= 512:
            chunk = connection.recv(513 - len(data))
            if not chunk:
                break
            data.extend(chunk)
        if len(data) > 512 or not data.endswith(b"\n") or data.count(b"\n") != 1:
            raise ValueError("Invalid local response length")
    return validator(json.loads(data))


def request(command, socket_path=SOCKET_PATH):
    if not isinstance(command, str) or re.fullmatch(r"(?:[0-9ABCD]{6}|[0-9ABCD]|\*)", command) is None:
        raise ValueError("Invalid radio command")
    value = _exchange({"schema": 2, "command": command}, socket_path, validate_response)
    # An older dashboard rejects the unknown schema before authentication or
    # mutation. Retry only that fixed rejection; never repeat an accepted job.
    if value["schema"] == 1 and value["result"] == "denied":
        return _exchange({"schema": 1, "command": command}, socket_path, validate_response)
    return value


def validate_prompt_ack(value):
    if (not isinstance(value, dict) or set(value) != {"schema", "accepted"}
            or type(value["schema"]) is not int or value["schema"] != 1
            or type(value["accepted"]) is not bool):
        raise ValueError("Invalid prompt completion response")
    return value


def prompt_finished(prompt_id, socket_path=SOCKET_PATH):
    if not isinstance(prompt_id, str) or PROMPT_RE.fullmatch(prompt_id) is None:
        raise ValueError("Invalid prompt completion identifier")
    return _exchange({"schema": 1, "prompt_finished": prompt_id}, socket_path, validate_prompt_ack)


def response_event(response):
    value = validate_response(response)
    # Failed authentication and a disabled feature produce no RF response.
    if value["result"] in ("disabled", "denied"):
        return None
    extended = value["schema"] == 2
    return ("EVENT RepeaterLogic::" + ("box_control_result_data " if extended else "box_control_result ") + value["result"] + " "
            + str(value["pair_index"]) + " " + str(int(value["recording"])) + " "
            + str(int(value["announce_status"])) + " " + str(int(value["identification_enabled"])) + " "
            + (str(int(value["aprs_enabled"])) + " " + str(int(value["sstv_enabled"])) + " " if extended else "")
            + (value["prompt_id"] or "-") + "\n").encode("ascii")


def announce(response, command_paths=COMMAND_PATHS, owner_uid=None):
    event = response_event(response)
    if event is None:
        return False
    owner_uid = os.getuid() if owner_uid is None else owner_uid
    for path in command_paths:
        try:
            # SvxLink creates this fixed symlink; never accept an arbitrary
            # file, radio serial adapter, or another user's terminal.
            target = path.resolve(strict=True)
            if re.fullmatch(r"/dev/pts/[0-9]+", str(target)) is None:
                continue
            descriptor = os.open(path, os.O_WRONLY | os.O_NOCTTY | os.O_NONBLOCK | os.O_CLOEXEC)
            try:
                info = os.fstat(descriptor)
                if (not stat.S_ISCHR(info.st_mode) or info.st_uid != owner_uid
                        or info.st_mode & 0o002 or not os.isatty(descriptor)):
                    continue
                if os.write(descriptor, event) != len(event):
                    raise OSError("Incomplete control event")
                return True
            finally:
                os.close(descriptor)
        except (OSError, ValueError):
            continue
    return False


def main(argv=None):
    try:
        argv = sys.argv[1:] if argv is None else argv
        if argv:
            if len(argv) != 2 or argv[0] != "--prompt-finished":
                return 1
            # A duplicate/stale acknowledgment is a valid no-op. Never write a
            # control PTY or consume radio-command stdin in this callback mode.
            prompt_finished(argv[1])
            return 0
        line = sys.stdin.buffer.readline(65)
        if not line.endswith(b"\n") or len(line) not in (2, 7):
            return 1
        command = line[:-1].decode("ascii")
        announce(request(command))
        return 0
    except (OSError, ValueError, UnicodeError, TypeError, KeyError):
        # Never echo a command, PIN, response body, or exception to the journal.
        return 1


if __name__ == "__main__":
    sys.exit(main())
