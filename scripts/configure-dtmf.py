#!/usr/bin/python3
"""Set a separate local RF control code; never program or key a radio."""
import fcntl
import getpass
import grp
import hashlib
import importlib.util
import io
import json
import os
import pwd
from pathlib import Path
import re
import secrets
import stat
import sys
import tempfile
import warnings

CONFIG_PATH = Path("/etc/repeater-box/dtmf.json")
MONITOR_PATH = Path("/etc/repeater-box/svxlink-rx-monitor.conf")
LOCK_PATH = Path("/opt/repeater-box/.maintenance.lock")
EVENT_HANDLER = Path("/opt/repeater-box/scripts/repeater-box-events.tcl")
REQUEST_PATH = Path("/var/lib/repeater-dashboard/dtmf-code-request.json")
RESULT_PATH = Path("/var/lib/repeater-dashboard/dtmf-code-result.json")
RADIO_STATE_PATH = Path("/var/lib/repeater-radio-backups/state.json")


def checked_file(path, owner_uid=0, maximum=65536, confidential=False):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != owner_uid
                or info.st_mode & (0o027 if confidential else 0o022) or info.st_size > maximum):
            raise ValueError("The local control configuration has unsafe ownership, permissions, or size.")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            value = stream.read(maximum + 1)
        if len(value) > maximum:
            raise ValueError("The local configuration is too large.")
        return value
    finally:
        os.close(descriptor)


def atomic_write(path, value, mode, owner_uid=0, group_id=None):
    info = path.parent.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != owner_uid or info.st_mode & 0o022:
        raise ValueError("The local configuration directory must be protected and root-owned.")
    try:
        checked_file(path, owner_uid, confidential=mode == 0o640)
    except FileNotFoundError:
        pass
    descriptor, temporary = tempfile.mkstemp(prefix=".dtmf-", dir=path.parent)
    try:
        os.fchmod(descriptor, mode)
        if group_id is not None:
            os.fchown(descriptor, owner_uid, group_id)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def code_record(code):
    if not isinstance(code, str) or re.fullmatch(r"[0-9ABCD]{6}", code) is None:
        raise ValueError("Use six characters from 0-9 and uppercase A-D. # submits; * cancels.")
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", code.encode("ascii"), bytes.fromhex(salt), 600000)
    return {"schema": 1, "algorithm": "pbkdf2_sha256", "iterations": 600000, "salt": salt, "digest": digest.hex()}


def validate_record(value):
    if (not isinstance(value, dict) or set(value) != {"schema", "algorithm", "iterations", "salt", "digest"}
            or type(value["schema"]) is not int or value["schema"] != 1
            or value["algorithm"] != "pbkdf2_sha256"
            or type(value["iterations"]) is not int or value["iterations"] != 600000
            or not isinstance(value["salt"], str) or re.fullmatch(r"[0-9a-f]{32}", value["salt"]) is None
            or not isinstance(value["digest"], str) or re.fullmatch(r"[0-9a-f]{64}", value["digest"]) is None):
        raise ValueError("The radio control-code record is invalid.")
    return dict(value)


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate radio control-code record field.")
        value[key] = item
    return value


def hidden_prompt(prompt):
    # getpass otherwise falls back to echoed input if disabling echo fails.
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            return getpass.getpass(prompt)
        except getpass.GetPassWarning:
            raise ValueError("The terminal cannot hide the code. Nothing was saved.") from None


def require_no_radio_pending(path=RADIO_STATE_PATH, owner_uid=0):
    try:
        content = checked_file(path, owner_uid, maximum=262144, confidential=True)
    except FileNotFoundError:
        return
    state = json.loads(content, object_pairs_hook=unique_object)
    if not isinstance(state, dict) or any(not isinstance(value, dict) for value in state.values()):
        raise ValueError("The protected radio state is invalid.")
    for value in state.values():
        if "verification_required" in value and type(value["verification_required"]) is not bool:
            raise ValueError("The protected radio state is invalid.")
        if value.get("verification_required"):
            raise ValueError("Resolve unfinished radio verification before changing the control code.")


def migrate_monitor(path=MONITOR_PATH, owner_uid=0, event_handler=EVENT_HANDLER):
    """Caller holds maintenance lock. Preserve device identity and saved state.

    The only online exception remains TX=NONE, NO_REPEAT=1 and the fixed
    restricted handler; no transmitter section is created or selected.
    """
    try:
        content = checked_file(path, owner_uid)
    except FileNotFoundError:
        return False
    spec = importlib.util.spec_from_file_location("bench_guard", Path(__file__).with_name("check-bench-config.py"))
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    parser = guard.configparser.ConfigParser(interpolation=None, inline_comment_prefixes=("#", ";"))
    parser.optionxform = str
    parser.read_string(content.decode("utf-8"))
    # An older restricted monitor used explicit # command termination. Upgrade
    # only that known setting before applying the stricter release-PTT guard.
    if (parser.has_section("RepeaterLogic") and parser["RepeaterLogic"].get("ONLINE") == "1"
            and parser["RepeaterLogic"].get("EVENT_HANDLER") == str(event_handler)
            and not parser["RepeaterLogic"].get("EXEC_CMD_ON_SQL_CLOSE")):
        parser["RepeaterLogic"]["EXEC_CMD_ON_SQL_CLOSE"] = "500"
    guard.validate_parser(parser, True, event_handler, owner_uid)
    if parser["GLOBAL"]["LOGICS"].strip() != "RepeaterLogic":
        raise ValueError("The monitor must use its single RepeaterLogic before enabling restricted DTMF.")
    logic = parser["RepeaterLogic"]
    receiver = parser[logic["RX"]]
    logic["ONLINE"] = "1"
    logic["NO_REPEAT"] = "1"
    logic["MODULES"] = ""
    logic["EVENT_HANDLER"] = str(event_handler)
    logic["COMMAND_PTY"] = "/run/repeater-rx-monitor/svxlink-command"
    for key in ("ONLINE_CMD", "MACROS", "ACTIVATE_MODULE_ON_LONG_CMD"):
        logic.pop(key, None)
    logic["EXEC_CMD_ON_SQL_CLOSE"] = "500"
    receiver["DTMF_DEC_TYPE"] = "INTERNAL"
    receiver["DTMF_MUTING"] = "1"
    receiver["DTMF_HANGTIME"] = "40"
    guard.validate_parser(parser, True, event_handler, owner_uid)
    output = io.StringIO()
    parser.write(output, space_around_delimiters=False)
    new_content = output.getvalue().encode("utf-8")
    if content == new_content:
        return False
    atomic_write(path, new_content, 0o644, owner_uid)
    return True


def save_record(record, config_path=CONFIG_PATH, monitor_path=MONITOR_PATH, lock_path=LOCK_PATH,
                owner_uid=0, group_id=None, event_handler=EVENT_HANDLER, state_path=RADIO_STATE_PATH):
    record = validate_record(record)
    descriptor = os.open(lock_path, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != owner_uid or info.st_mode & 0o077:
            raise ValueError("The maintenance lock must be protected and root-owned.")
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        require_no_radio_pending(state_path, owner_uid)
        migrated = migrate_monitor(monitor_path, owner_uid, event_handler)
        atomic_write(config_path, (json.dumps(record, indent=2) + "\n").encode("ascii"), 0o640,
                     owner_uid, group_id)
    finally:
        os.close(descriptor)
    return {"ok": True, "monitor_restart_required": migrated}


def publish_result(path, value, request_owner_uid, owner_uid=0, group_id=None):
    folder = path.parent.lstat()
    if not stat.S_ISDIR(folder.st_mode) or folder.st_uid != request_owner_uid or folder.st_mode & 0o022:
        raise ValueError("The local request directory is invalid.")
    descriptor, temporary = tempfile.mkstemp(prefix=".dtmf-result-", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o640)
        if group_id is not None:
            os.fchown(descriptor, owner_uid, group_id)
        with os.fdopen(descriptor, "w") as stream:
            json.dump(value, stream, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


def process_request(request_path=REQUEST_PATH, result_path=RESULT_PATH, request_owner_uid=None,
                    owner_uid=0, group_id=None, **save_options):
    if request_owner_uid is None:
        request_owner_uid = pwd.getpwnam("repeaterdash").pw_uid
    descriptor = os.open(request_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        info = os.fstat(descriptor)
        identity = (info.st_dev, info.st_ino)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != request_owner_uid
                or info.st_mode & 0o077 or info.st_size > 8192):
            raise ValueError("The local credential request is invalid.")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            content = stream.read(8193)
        if len(content) > 8192:
            raise ValueError("The local credential request is too large.")
    finally:
        os.close(descriptor)
    request_id = None
    result = {"schema": 1, "request_id": None, "success": False,
              "message": "The radio control code could not be saved. Retry after maintenance or review the local configuration.",
              "monitor_restart_required": False}
    try:
        value = json.loads(content, object_pairs_hook=unique_object)
        if isinstance(value, dict) and isinstance(value.get("request_id"), str) and re.fullmatch(r"[0-9a-f]{32}", value["request_id"]):
            request_id = value["request_id"]
        result["request_id"] = request_id
        if (not isinstance(value, dict) or set(value) != {"schema", "request_id", "record"}
                or type(value["schema"]) is not int or value["schema"] != 1 or request_id is None):
            raise ValueError("The local credential request is invalid.")
        saved = save_record(validate_record(value["record"]), owner_uid=owner_uid, group_id=group_id, **save_options)
        result.update(success=True, message="Radio control code saved.",
                      monitor_restart_required=saved["monitor_restart_required"])
    except (OSError, ValueError, KeyError):
        pass
    publish_result(result_path, result, request_owner_uid, owner_uid, group_id)
    try:
        current = request_path.lstat()
        if (current.st_dev, current.st_ino) == identity:
            request_path.unlink()
    except FileNotFoundError:
        pass
    return result


def main():
    if os.geteuid() != 0:
        raise SystemExit("Run: sudo python3 /opt/repeater-box/scripts/configure-dtmf.py")
    group_id = grp.getgrnam("repeaterdash").gr_gid
    if sys.argv[1:] == ["--request"]:
        process_request(group_id=group_id)
        return
    if len(sys.argv) != 1:
        raise SystemExit("This command accepts no code or configuration on the command line.")
    if not sys.stdin.isatty():
        raise SystemExit("Run this command in a local interactive terminal so the code stays hidden.")
    print("Set a separate six-character radio control code: 0-9 and uppercase A-D. # submits; * cancels.")
    print("The code is audible over the radio.")
    code = hidden_prompt("New radio control code: ")
    record = code_record(code)
    repeated = hidden_prompt("Repeat radio control code: ")
    if re.fullmatch(r"[0-9ABCD]{6}", repeated) is None or not secrets.compare_digest(code, repeated):
        raise SystemExit("The codes did not match; nothing changed.")
    result = save_record(record, group_id=group_id)
    print("Radio control code saved. Enable DTMF controls in the dashboard Admin tab.")
    if result["monitor_restart_required"]:
        print("Receive-only configuration updated. Restart repeater-rx-monitor.service when no radio task is pending.")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError):
        raise SystemExit("The radio control code could not be saved. Retry after maintenance or review the local configuration.")
