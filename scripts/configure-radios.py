#!/usr/bin/python3
"""Bind the box's 5RX radios to AIOCs and an optional external programming port.

Run locally once after the installer. This writes configuration only; it never
opens a serial port, transmits, downloads, or programs a radio.
"""
import argparse
import fcntl
import grp
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile

SERIAL_RE = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
PROGRAMMER_RE = re.compile(r"/dev/serial/by-(?:id|path)/[A-Za-z0-9_.:-]{1,200}\Z")
CONFIG_PATH = Path("/etc/repeater-box/radio-programming.json")
LOCK_PATH = Path("/opt/repeater-box/.maintenance.lock")
PRIVATE_STATE = Path("/var/lib/repeater-radio-backups/state.json")
PAIR_CONFIG = Path("/etc/repeater-box/radio-pair.json")
PUBLIC_STATE = Path("/var/lib/repeater-dashboard/radio-programming-state.json")


def device_path(serial):
    if not isinstance(serial, str) or not SERIAL_RE.fullmatch(serial):
        raise ValueError("Use the AIOC serial number from /dev/serial/by-id, without a path")
    return "/dev/serial/by-id/usb-AIOC_All-In-One-Cable_" + serial + "-if04"


def build_config(model, rx_serial, tx_serial, validate_device=True):
    if model != "5RX":
        raise ValueError("Only the reviewed Baofeng UV-5RX / 5RX profile is supported")
    devices = {"rx": device_path(rx_serial), "tx": device_path(tx_serial)}
    if devices["rx"] == devices["tx"]:
        raise ValueError("RX and TX must use different AIOC boards")
    if validate_device:
        for device in devices.values():
            try:
                info = os.stat(device)
            except FileNotFoundError as exc:
                raise ValueError("Both configured AIOC boards must be plugged into the Pi") from exc
            if not stat.S_ISCHR(info.st_mode):
                raise ValueError("An AIOC serial link does not point to a character device")
        if os.path.samefile(devices["rx"], devices["tx"]):
            raise ValueError("RX and TX links must refer to different devices")
    return {"schema": 1, "model": "5RX", "devices": devices}


def validate_config(value):
    if (not isinstance(value, dict) or set(value) != {"schema", "model", "devices"}
            or type(value["schema"]) is not int or value["schema"] != 1 or value["model"] != "5RX"
            or not isinstance(value["devices"], dict)
            or not {"rx", "tx"} <= set(value["devices"]) <= {"rx", "tx", "programmer"}):
        raise ValueError("Configure both RX and TX first using --model 5RX --rx-serial and --tx-serial")
    for role in ("rx", "tx"):
        if not isinstance(value["devices"][role], str) or not re.fullmatch(
                r"/dev/serial/by-id/usb-AIOC_All-In-One-Cable_[A-Za-z0-9_-]{1,64}-if04", value["devices"][role]):
            raise ValueError("RX and TX must retain their locally configured AIOC links")
    if "programmer" in value["devices"] and (not isinstance(value["devices"]["programmer"], str)
            or not PROGRAMMER_RE.fullmatch(value["devices"]["programmer"])):
        raise ValueError("Use a complete /dev/serial/by-id/ or /dev/serial/by-path/ programming-port link")
    if len(set(value["devices"].values())) != len(value["devices"]):
        raise ValueError("Each role must have a separate serial device")
    return value


def update_programmer(config, port=None, remove=False, validate_device=True):
    config = json.loads(json.dumps(validate_config(config)))
    if remove:
        config["devices"].pop("programmer", None)
    elif port is not None:
        if not isinstance(port, str) or not PROGRAMMER_RE.fullmatch(port):
            raise ValueError("Use a complete /dev/serial/by-id/ or /dev/serial/by-path/ programming-port link")
        if validate_device:
            try:
                target = os.stat(port)
            except FileNotFoundError as exc:
                raise ValueError("Plug the external programming cable into its assigned USB port first") from exc
            if not stat.S_ISCHR(target.st_mode):
                raise ValueError("The programming-port link must point to a serial character device")
            for role in ("rx", "tx"):
                try:
                    if os.path.samefile(port, config["devices"][role]):
                        raise ValueError("The external programmer must be separate from the RX and TX AIOCs")
                except FileNotFoundError:
                    pass
        config["devices"]["programmer"] = port
    return validate_config(config)


def read_local(path):
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return {}
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022 or info.st_size > 16384:
            raise ValueError("Local radio state permissions are invalid")
        with os.fdopen(descriptor, "r", closefd=False) as stream:
            value = json.load(stream)
        if not isinstance(value, dict):
            raise ValueError("Local radio state must be an object")
        return value
    finally:
        os.close(descriptor)


def check_rebinding(previous_config, new_config, private_state):
    old = previous_config.get("devices", {})
    new = new_config.get("devices", {})
    pair = private_state.get("_pair", {})
    if isinstance(pair, dict) and pair.get("verification_required") and any(old.get(role) != new.get(role) for role in ("rx", "tx")):
        raise ValueError("Resolve the unfinished paired change before changing either box radio binding")
    for role in ("rx", "tx", "programmer"):
        pending = private_state.get(role, {})
        if isinstance(pending, dict) and pending.get("verification_required") and old.get(role) != new.get(role):
            raise ValueError("Repair or verify the unfinished upload before changing that role's serial binding")


def write_local(path, value):
    descriptor, temporary = tempfile.mkstemp(prefix=".radio-config-", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o640)
        os.fchown(descriptor, 0, grp.getgrnam("repeaterdash").gr_gid)
        with os.fdopen(descriptor, "w") as stream:
            json.dump(value, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def reset_pair_on_rebinding(previous_config, new_config, private_state):
    old, new = previous_config.get("devices", {}), new_config["devices"]
    if not any(old.get(role) != new.get(role) for role in ("rx", "tx")):
        return
    # Disable before committing new bindings. A crash cannot leave the new
    # cables qualified from a former radio's successful readback.
    write_local(PAIR_CONFIG, {"schema": 1, "enabled": False})
    if isinstance(private_state.get("_pair"), dict):
        private_state["_pair"].update(stored_verified=False, phase="invalidated")
        write_local(PRIVATE_STATE, private_state)
        public = read_local(PUBLIC_STATE)
        if isinstance(public.get("_pair"), dict):
            public["_pair"].update(stored_verified=False, phase="invalidated")
            write_local(PUBLIC_STATE, public)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", choices=["5RX"], default="5RX", help="Selected CHIRP profile for Baofeng UV-5RX / 5RX")
    parser.add_argument("--rx-serial")
    parser.add_argument("--tx-serial")
    external = parser.add_mutually_exclusive_group()
    external.add_argument("--programmer-port", help="Stable serial by-id or by-path link for the optional USB programming cable")
    external.add_argument("--remove-programmer", action="store_true")
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("Run this local configuration command with sudo")
    lock_fd = None
    try:
        lock_fd = os.open(LOCK_PATH, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
        lock_info = os.fstat(lock_fd)
        if not stat.S_ISREG(lock_info.st_mode) or lock_info.st_uid != 0 or lock_info.st_mode & 0o077:
            raise ValueError("The maintenance lock permissions are invalid; rerun the installer")
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("A maintenance operation is running; retry after it finishes") from exc
        previous = read_local(CONFIG_PATH)
        if bool(args.rx_serial) != bool(args.tx_serial):
            raise ValueError("Supply both --rx-serial and --tx-serial together")
        config = build_config(args.model, args.rx_serial, args.tx_serial) if args.rx_serial else validate_config(previous)
        if args.rx_serial and "programmer" in previous.get("devices", {}):
            config["devices"]["programmer"] = previous["devices"]["programmer"]
        config = update_programmer(config, args.programmer_port, args.remove_programmer)
        private_state = read_local(PRIVATE_STATE)
        check_rebinding(previous, config, private_state)
        directory = CONFIG_PATH.parent
        directory.mkdir(mode=0o755, parents=True, exist_ok=True)
        info = directory.stat()
        if info.st_uid != 0 or info.st_mode & 0o022 or directory.is_symlink():
            raise ValueError("The configuration directory must be root-owned and not writable by other users")
        reset_pair_on_rebinding(previous, config, private_state)
        descriptor, temporary = tempfile.mkstemp(prefix=".radio-config-", dir=directory)
        try:
            os.fchmod(descriptor, 0o640)
            os.fchown(descriptor, 0, grp.getgrnam("repeaterdash").gr_gid)
            with os.fdopen(descriptor, "w") as stream:
                json.dump(config, stream, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, CONFIG_PATH)
            directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    except (OSError, KeyError, ValueError) as exc:
        print("Configuration was not saved: " + str(exc), file=sys.stderr)
        return 1
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
    print("Local radio-port bindings saved. No radio settings were changed.")
    print("Use /radio-editor for the optional Programming port; select Baofeng 5RX or UV-5R before reading.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
