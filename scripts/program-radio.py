#!/usr/bin/python3
"""Reviewed-profile AIOC programming worker, invoked only by systemd.path.

Inspection is read only. Programming uses a fresh download from the attached
radio and transfers complete 32-byte channel/global groups and experimental
256-byte name groups containing reviewed changes. Companion bytes are preserved. The editor accepts strictly validated
operating settings, never images, paths, drivers or commands.
"""
from datetime import datetime, timezone
import fcntl
import grp
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import time
import termios

APP_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from radio_plan import validate_catalog, catalog_digest, pair_plans, default_plan, PAIR_ID_RE, DIGEST_RE
# Do not import the remotely updatable dashboard from this privileged process.
# Keep this fixed profile and the narrow request validator in the installed
# worker. A change here requires the local Pi installer.
REPEATER_ROLES = ("rx", "tx")
ROLES = (*REPEATER_ROLES, "programmer")
MODELS = ("5RX", "UV5R")
REQUEST_RE = re.compile(r"[0-9a-f]{32}\Z")
DEVICE_RE = re.compile(r"/dev/serial/by-id/usb-AIOC_All-In-One-Cable_[A-Za-z0-9_-]{1,64}-if04\Z")
PROGRAMMER_DEVICE_RE = re.compile(r"/dev/serial/by-(?:id|path)/[A-Za-z0-9_.:-]{1,200}\Z")


def valid_request(value):
    if isinstance(value, dict) and value.get("action") == "change-pair":
        return (set(value) == {"request_id", "action", "pair_id", "catalog_digest"}
                and isinstance(value.get("request_id"), str)
                and bool(REQUEST_RE.fullmatch(value["request_id"]))
                and isinstance(value.get("pair_id"), str) and bool(PAIR_ID_RE.fullmatch(value["pair_id"]))
                and isinstance(value.get("catalog_digest"), str) and bool(DIGEST_RE.fullmatch(value["catalog_digest"])))
    if not (isinstance(value, dict) and isinstance(value.get("request_id"), str)
            and REQUEST_RE.fullmatch(value["request_id"]) and value.get("role") in ROLES):
        return False
    if value.get("action") == "repair-upload":
        return (set(value) == {"request_id", "role", "action", "pending_id"}
                and isinstance(value.get("pending_id"), str)
                and bool(REQUEST_RE.fullmatch(value["pending_id"])))
    if value.get("action") == "editor-read" and value["role"] == "programmer":
        return (set(value) in ({"request_id", "role", "action"}, {"request_id", "role", "action", "model"})
                and value.get("model", "5RX") in MODELS)
    if value.get("action") == "program":
        return (value["role"] in REPEATER_ROLES and set(value) == {"request_id", "role", "action", "catalog_digest"}
                and isinstance(value.get("catalog_digest"), str) and bool(DIGEST_RE.fullmatch(value["catalog_digest"])))
    if value.get("action") in ("inspect", "verify", "editor-read"):
        return (set(value) == {"request_id", "role", "action"}
                and (value["role"] != "programmer" or value["action"] not in ("inspect", "program")))
    return (value.get("action") == "editor-write"
            and set(value) in ({"request_id", "role", "action", "base_id", "changes", "settings"},
                               {"request_id", "role", "action", "base_id", "changes", "settings", "supersede_id"})
            and isinstance(value["base_id"], str) and bool(REQUEST_RE.fullmatch(value["base_id"]))
            and isinstance(value["changes"], list) and len(value["changes"]) <= 128
            and isinstance(value["settings"], dict) and len(value["settings"]) <= 16
            and ("supersede_id" not in value or isinstance(value["supersede_id"], str)
                 and bool(REQUEST_RE.fullmatch(value["supersede_id"]))))


def valid_config(value):
    return (isinstance(value, dict) and set(value) == {"schema", "model", "devices"}
            and type(value["schema"]) is int and value["schema"] == 1 and value["model"] == "5RX"
            and isinstance(value["devices"], dict) and set(REPEATER_ROLES) <= set(value["devices"]) <= set(ROLES)
            and all(isinstance(value["devices"][role], str) and DEVICE_RE.fullmatch(value["devices"][role]) for role in REPEATER_ROLES)
            and ("programmer" not in value["devices"] or isinstance(value["devices"]["programmer"], str)
                 and bool(PROGRAMMER_DEVICE_RE.fullmatch(value["devices"]["programmer"])))
            and len(set(value["devices"].values())) == len(value["devices"]))


def public_role(value):
    value = value if isinstance(value, dict) else {}
    selected_model = (value.get("program_model", "5RX") if value.get("verification_required")
                      else value.get("editor_model", value.get("selected_model", "5RX")))
    return {"model": selected_model if selected_model in MODELS else "5RX",
            "inspected": value.get("inspected") is True, "compatible": value.get("compatible") is True,
            "programmed": value.get("programmed") is True, "verified": value.get("verified") is True,
            "verification_required": value.get("verification_required") is True,
            "firmware": str(value.get("firmware", ""))[:80], "identity": str(value.get("identity", ""))[:32],
            "program_kind": value.get("program_kind") if value.get("program_kind") in ("repeater", "editor", "pair") else None,
            "pending_id": value.get("program_id") if value.get("verification_required") is True
            and isinstance(value.get("program_id"), str) and REQUEST_RE.fullmatch(value["program_id"]) else None,
            "backup_id": value.get("backup_id") if isinstance(value.get("backup_id"), str)
            and REQUEST_RE.fullmatch(value["backup_id"]) else None}


def public_pair(value, catalog=None):
    """Project only the root worker's configured-pair record, never RF telemetry."""
    value = value if isinstance(value, dict) else {}
    pair_id = value.get("pair_id")
    plans = pair_plans(catalog) if catalog is not None else {}
    current_digest = catalog_digest(catalog) if catalog is not None else None
    catalog_changed = bool(value) and (current_digest is None or value.get("catalog_digest") != current_digest)
    if not isinstance(pair_id, str) or not PAIR_ID_RE.fullmatch(pair_id):
        pair_id = None
    request_id = value.get("request_id")
    if not isinstance(request_id, str) or not REQUEST_RE.fullmatch(request_id):
        request_id = None
    phase = value.get("phase")
    if phase not in ("prepared", "writing-rx", "verifying-rx", "writing-tx", "verifying-tx",
                     "configured", "failed", "invalidated"):
        phase = None
    roles = value.get("roles") if isinstance(value.get("roles"), dict) else {}
    plan = dict(plans[pair_id]) if pair_id in plans and not catalog_changed else None
    return {"pair_id": pair_id, "request_id": request_id, "phase": phase,
            "catalog_digest": value.get("catalog_digest") if isinstance(value.get("catalog_digest"), str)
            and DIGEST_RE.fullmatch(value["catalog_digest"]) else None,
            "catalog_changed": catalog_changed,
            "stored_verified": value.get("stored_verified") is True and not catalog_changed and plan is not None,
            "activation_required": value.get("activation_required") is True,
            "verification_required": value.get("verification_required") is True,
            "roles": {role: {"written": isinstance(roles.get(role), dict) and roles[role].get("written") is True,
                             "verified": isinstance(roles.get(role), dict) and roles[role].get("verified") is True}
                      for role in REPEATER_ROLES}, "plan": plan}

STATE_DIR = Path("/var/lib/repeater-dashboard")
BACKUP_DIR = Path("/var/lib/repeater-radio-backups")
CONFIG_PATH = Path("/etc/repeater-box/radio-programming.json")
PAIR_CONFIG_PATH = Path("/etc/repeater-box/radio-pair.json")
CATALOG_PATH = Path("/etc/repeater-box/channel-pairs.json")
LOCK_PATH = Path("/opt/repeater-box/.maintenance.lock")
VENDOR_PATH = Path("/opt/repeater-box/vendor/chirp")
CHIRP_COMMIT = "0b4076d9b19a2a133d11f31546cff8153b39eb89"
SERVICES = ("repeater-engine.service", "repeater-rx-monitor.service")
KNOWN_IDENT = bytes.fromhex("aa307604000520dd")
KNOWN_FIRMWARE_PREFIX = b"HN5RV011"
GLOBAL_SETTINGS = {
    "settings.save": "Off", "settings.vox": "Off", "settings.tdr": False,
    "settings.beep": False, "settings.timeout": "60 sec", "settings.voice": "Off",
    "settings.roger": False, "settings.pttid": "Off", "settings.autolk": True, "settings.mdfa": "Name",
    "settings.mdfb": "Name", "settings.workmode": "Channel",
    "settings.displayab": "A",
}
# These offsets are validated against the pinned driver's actual field layout.
GLOBAL_OFFSETS = {0xE23, 0xE24, 0xE27, 0xE28, 0xE29, 0xE2E, 0xE33,
                  0xE35, 0xE36, 0xE38, 0xE47, 0xE4A, 0xE4C}
ALLOWED_OFFSETS = set(range(0x10, 0x20)) | set(range(0x1010, 0x1020)) | GLOBAL_OFFSETS
PARTIAL_MASKS = {0xE4A: 0x80}
# The current keypad-lock bit and seven-bit channel cursors change during
# ordinary operation. AUTOLK at 0xE38 is a persistent, strictly verified setting.
# Every reserved bit in these runtime bytes remains part of the comparison.
VOLATILE_MASKS = {0xE4D: 0x01, 0xE76: 0x7F, 0xE77: 0x7F}
EDITOR_GLOBAL_OFFSETS = GLOBAL_OFFSETS | {0xE20}
EDITOR_ALLOWED_OFFSETS = set(range(0x0000, 0x0800)) | set(range(0x1000, 0x1800)) | EDITOR_GLOBAL_OFFSETS
TX_BANDS = ((144000000, 148000000), (222000000, 225000000), (420000000, 450000000))
EDITOR_SETTINGS = {
    "settings.squelch": {"label": "Squelch", "type": "select", "options": ["Off", *map(str, range(1, 10))]},
    "settings.save": {"label": "Battery saver", "type": "select", "options": ["Off", "1:1", "1:2", "1:3", "1:4"]},
    "settings.vox": {"label": "VOX", "type": "select", "options": ["Off", *map(str, range(1, 11))]},
    "settings.tdr": {"label": "Dual watch", "type": "boolean"},
    "settings.beep": {"label": "Keypad beep", "type": "boolean"},
    "settings.autolk": {"label": "Automatic keypad lock", "type": "boolean"},
    "settings.timeout": {"label": "Transmit timeout", "type": "select", "options": [f"{n} sec" for n in range(15, 601, 15)]},
    "settings.voice": {"label": "Voice prompts", "type": "select", "options": ["Off", "English", "Chinese"]},
    "settings.roger": {"label": "Roger beep", "type": "boolean"},
    "settings.pttid": {"label": "PTT ID", "type": "select", "options": ["Off", "BOT", "EOT", "Both"]},
    "settings.mdfa": {"label": "Display A format", "type": "select", "options": ["Channel", "Name", "Frequency"]},
    "settings.mdfb": {"label": "Display B format", "type": "select", "options": ["Channel", "Name", "Frequency"]},
    "settings.workmode": {"label": "Operating mode", "type": "select", "options": ["Frequency", "Channel"]},
    "settings.displayab": {"label": "Active display", "type": "select", "options": ["A", "B"]},
}


class ProgrammingError(Exception):
    """An operational error with a dashboard-safe explanation."""


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate JSON key")
        value[key] = item
    return value


def read_json(path, maximum=16384, private=False, owner_uid=0, confidential=False):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
            raise ProgrammingError("The radio request or local configuration is invalid.")
        forbidden_mode = 0o027 if confidential else 0o022
        if private and (info.st_uid != owner_uid or info.st_mode & forbidden_mode):
            raise ProgrammingError("The local radio configuration must be root-owned and not writable by other users.")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            data = stream.read(maximum + 1)
        if len(data) > maximum:
            raise ProgrammingError("The radio request is too large.")
        value = json.loads(data, object_pairs_hook=unique_object)
        if not isinstance(value, dict):
            raise ValueError("JSON must be an object")
        return value
    finally:
        os.close(descriptor)


def atomic_json(path, value, group_id=None):
    """Replace a fixed filename without following an app-controlled symlink."""
    descriptor, temporary = tempfile.mkstemp(prefix=".radio-", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o640)
        if group_id is not None:
            os.fchown(descriptor, 0, group_id)
        with os.fdopen(descriptor, "w") as stream:
            json.dump(value, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_path(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def sync_path(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def packed(radio):
    return bytes(radio.get_mmap().get_packed())


def report_progress(callback, current, total):
    if getattr(callback, "radio_metrics", False):
        callback(current=current, total=total, unit="blocks")
    else:
        callback()


def radio_model(radio):
    return "UV5R" if radio.MODEL == "UV-5R" else "5RX"


def image_base(radio):
    return 8 if radio_model(radio) == "UV5R" else 0


def editor_settings(radio):
    if radio_model(radio) == "5RX":
        return EDITOR_SETTINGS
    descriptions = {name: dict(value) for name, value in EDITOR_SETTINGS.items()
                    if name != "settings.pttid"}
    descriptions["settings.squelch"] = {"label": "Squelch", "type": "integer", "min": 0, "max": 9}
    descriptions["settings.vox"]["options"] = ["OFF", *map(str, range(1, 11))]
    return descriptions


def setting_fields(radio):
    fields = {item.get_name(): item for item in flatten_settings(radio.get_settings())}
    if radio_model(radio) == "UV5R":
        return {"settings." + name: field for name, field in fields.items() if "." not in name and "/" not in name}
    return fields


def powers(radio):
    return radio.get_features().valid_power_levels


def profile_global_offsets(radio):
    base = image_base(radio)
    return {i + base for i in EDITOR_GLOBAL_OFFSETS if radio_model(radio) != "UV5R" or i != 0xE33}


def profile_allowed(radio, editor=False):
    if radio_model(radio) == "UV5R":
        if not editor:
            raise ProgrammingError("The UV-5R profile is available only on the external editor connection.")
        return {i + 8 for i in EDITOR_ALLOWED_OFFSETS if i != 0xE33}
    return EDITOR_ALLOWED_OFFSETS if editor else ALLOWED_OFFSETS


def profile_partial(radio):
    return {index + image_base(radio): mask for index, mask in PARTIAL_MASKS.items()}


def profile_volatile(radio):
    return {index + image_base(radio): mask for index, mask in VOLATILE_MASKS.items()}


def changed_blocks(radio, before, after):
    base = image_base(radio)
    return sorted({(i - base) // 16 * 16 for i, (old, new) in enumerate(zip(before, after)) if old != new})


def image_identity(radio):
    raw = packed(radio)
    model = radio_model(radio)
    if model == "UV5R":
        if len(raw) != 0x1948 or raw[0] != 0xAA or raw[7] != 0xDD:
            raise ProgrammingError("This download does not have the selected UV-5R memory layout. No upload was attempted.")
        identity, firmware = raw[:8], raw[0x1838:0x1846]
    else:
        if len(raw) != 0x2008 or raw[0x2000] != 0xAA or raw[0x2007] != 0xDD:
            raise ProgrammingError("This download does not have the selected 5RX memory layout. No upload was attempted.")
        identity, firmware = raw[0x2000:0x2008], raw[0x1EF0:0x1EFE]
    return {"selected_model": model, "identity": identity.hex(), "firmware_hex": firmware.hex(),
            "firmware": "".join(chr(item) if 32 <= item <= 126 else "." for item in firmware),
            "compatible": identity == KNOWN_IDENT and firmware.startswith(KNOWN_FIRMWARE_PREFIX)}


def same_identity(first, second):
    return (first.get("selected_model", "5RX") == second.get("selected_model", "5RX")
            and first.get("identity") == second.get("identity")
            and first.get("firmware_hex") == second.get("firmware_hex"))


def flatten_settings(group):
    for item in group:
        if hasattr(item, "value"):
            yield item
        else:
            yield from flatten_settings(item)


def editor_layout(radio):
    """Fail closed if the pinned driver's reviewed operating layout changes."""
    features = radio.get_features()
    if (not image_identity(radio)["compatible"] or radio.MODEL not in ("5RX", "UV-5R")
            or features.memory_bounds != (0, 127) or features.valid_name_length != 7):
        raise ProgrammingError("The radio does not match the reviewed editor profile. No upload was attempted.")
    for number in range(128):
        if (radio._memobj.memory[number].get_offset() != image_base(radio) + number * 16
                or radio._memobj.names[number].get_offset() != image_base(radio) + 0x1000 + number * 16):
            raise ProgrammingError("The installed memory layout differs from the reviewed editor profile.")
    offsets = set()
    for name in editor_settings(radio):
        obj = radio._memobj
        for part in name.split("."):
            obj = getattr(obj, part)
        offsets.add(obj.get_offset())
    if offsets != profile_global_offsets(radio):
        raise ProgrammingError("The installed setting layout differs from the reviewed editor profile.")
    return features


def editor_tone(spec):
    mode, value, polarity = spec
    if not mode:
        return None
    if mode == "Tone":
        return {"type": "CTCSS", "value": value}
    return {"type": "DCS", "value": value, "polarity": polarity}


def editor_memory(radio, number, chirp_common):
    memory = radio.get_memory(number)
    row = {"number": number, "empty": bool(memory.empty)}
    if memory.empty:
        return row
    transmit = memory.freq
    if memory.duplex == "off":
        transmit = None
    elif memory.duplex == "split":
        transmit = memory.offset
    elif memory.duplex == "+":
        transmit += memory.offset
    elif memory.duplex == "-":
        transmit -= memory.offset
    tx_tone, rx_tone = chirp_common.split_tone_encode(memory)
    row.update(name=memory.name, rx_hz=memory.freq, tx_hz=transmit,
               mode=memory.mode, power=str(memory.power), skip=memory.skip == "S",
               tx_tone=editor_tone(tx_tone), rx_tone=editor_tone(rx_tone))
    return row


def editor_snapshot(radio, role, base_id, chirp_common):
    features = editor_layout(radio)
    fields = setting_fields(radio)
    values = {}
    for name, descriptor in editor_settings(radio).items():
        value = fields[name].value
        values[name] = (bool(value) if descriptor["type"] == "boolean" else
                        int(value) if descriptor["type"] == "integer" else str(value))
    return {"schema": 1, "role": role, "base_id": base_id,
            "read_at": datetime.now(timezone.utc).isoformat(), "model": radio_model(radio),
            "firmware": image_identity(radio)["firmware"], "compatible": True,
            "memories": [editor_memory(radio, n, chirp_common) for n in range(128)],
            "settings": values,
            "capabilities": {"bands_hz": [list(band) for band in features.valid_bands],
                             "tx_bands_hz": [list(band) for band in TX_BANDS],
                             "name_length": features.valid_name_length,
                             "valid_characters": features.valid_characters,
                             "ctcss": list(chirp_common.TONES),
                             "dcs": list(features.valid_dtcs_codes),
                             "powers": [str(level) for level in powers(radio)],
                             "settings": editor_settings(radio)}}


def protected_editor_changes(expected, observed):
    """Compare data the editor cannot change, including reserved record bits."""
    first, second = packed(expected), packed(observed)
    if len(first) != len(second) or not same_identity(image_identity(expected), image_identity(observed)):
        return ["identity"]
    changed = []
    base = image_base(expected)
    allowed_globals = profile_global_offsets(expected)
    for index, (old, new) in enumerate(zip(first, second)):
        if old == new:
            continue
        mask = profile_volatile(expected).get(index, 0)
        physical = index - base
        if index in allowed_globals:
            mask = profile_partial(expected).get(index, 0xFF)
        elif 0 <= physical < 0x800 or 0x1000 <= physical < 0x1800:
            number, position = (physical % 0x1000) // 16, physical % 16
            # Creating/deleting a row legitimately initializes the complete
            # record. Existing populated records keep unknown and extra bits.
            either_empty = first[base + number * 16] == 0xFF or second[base + number * 16] == 0xFF
            if either_empty:
                mask = 0xFF
            elif physical < 0x800:
                mask = 0xFF if position < 12 else {12: 0, 13: 0, 14: 0x03, 15: 0x44}[position]
            else:
                mask = 0xFF if position < 7 else 0
        if (old ^ new) & ~mask:
            changed.append(index)
    return changed


def protected_pair_changes(expected, observed):
    """A pair retry may reconcile only working memory 1 and runtime bits.

    The general editor permits creating other empty records. Pair recovery has
    no such authority: unrelated records, including padding in unused names,
    must still match its protected candidate before a replacement is prepared.
    Keep the editor's reserved-bit checks inside populated memory 1 as well.
    """
    if radio_model(expected) != "5RX" or radio_model(observed) != "5RX":
        return ["identity"]
    protected = protected_editor_changes(expected, observed)
    if protected:
        return protected
    working = set(range(0x10, 0x20)) | set(range(0x1010, 0x1020))
    return [index for index, (old, new) in enumerate(zip(packed(expected), packed(observed)))
            if index not in working and (old ^ new) & ~VOLATILE_MASKS.get(index, 0)]


def recovery_differences(expected, observed, chirp_common):
    """Expose operating values for review, never private image or device data."""
    differences = []
    for number in range(128):
        first = editor_memory(expected, number, chirp_common)
        second = editor_memory(observed, number, chirp_common)
        for name in sorted(set(first) | set(second)):
            if name == "number" or first.get(name) == second.get(name):
                continue
            differences.append({"field": f"Memory {number} · {name}",
                                "expected": json.dumps(first.get(name), ensure_ascii=True)[:160],
                                "actual": json.dumps(second.get(name), ensure_ascii=True)[:160]})
    expected_fields, observed_fields = setting_fields(expected), setting_fields(observed)
    for name, description in editor_settings(expected).items():
        old, new = str(expected_fields[name].value), str(observed_fields[name].value)
        if old != new:
            differences.append({"field": name, "expected": old[:160], "actual": new[:160]})
    return differences[:20]


def valid_editor_tone(value, chirp_common, features):
    if value is None:
        return (None, None, None)
    if not isinstance(value, dict):
        raise ProgrammingError("Each tone must be off, CTCSS or DCS.")
    if (set(value) == {"type", "value"} and value["type"] == "CTCSS"
            and type(value["value"]) in (int, float) and value["value"] in chirp_common.TONES):
        return ("Tone", value["value"], None)
    if (set(value) == {"type", "value", "polarity"} and value["type"] == "DCS"
            and type(value["value"]) is int and value["value"] in features.valid_dtcs_codes
            and value["polarity"] in ("N", "R")):
        return ("DTCS", value["value"], value["polarity"])
    raise ProgrammingError("A tone value is not supported by the reviewed radio profile.")


def valid_frequency(value, bands):
    # The EEPROM stores frequencies in 10 Hz units. Never silently truncate.
    return (type(value) is int and value % 10 == 0
            and any(lower <= value <= upper for lower, upper in bands))


def apply_editor(radio, changes, changed_settings, chirp_common, settings_module):
    """Apply explicit rows and operating settings without normalizing neighbors."""
    before = packed(radio)
    features = editor_layout(radio)
    if not isinstance(changes, list) or len(changes) > 128 or not isinstance(changed_settings, dict):
        raise ProgrammingError("The editor proposal is invalid.")
    if not changes and not changed_settings:
        raise ProgrammingError("There are no radio changes to apply.")
    numbers = set()
    selected_offsets = set()
    row_keys = {"number", "empty", "name", "rx_hz", "tx_hz", "mode", "power", "skip", "tx_tone", "rx_tone"}
    for row in changes:
        if (not isinstance(row, dict) or type(row.get("number")) is not int
                or not 0 <= row["number"] <= 127 or type(row.get("empty")) is not bool
                or row["number"] in numbers
                or set(row) != ({"number", "empty"} if row["empty"] else row_keys)):
            raise ProgrammingError("The editor contains an invalid or repeated memory row.")
        number = row["number"]
        numbers.add(number)
        existing = radio.get_memory(number)
        old_row = editor_memory(radio, number, chirp_common)
        if row == old_row:
            continue
        base = image_base(radio)
        selected_offsets.update(range(base + number * 16, base + (number + 1) * 16))
        selected_offsets.update(range(base + 0x1000 + number * 16, base + 0x1000 + (number + 1) * 16))
        if row["empty"]:
            memory = chirp_common.Memory()
            memory.number, memory.empty = number, True
            radio.set_memory(memory)
            continue
        if (not isinstance(row["name"], str) or len(row["name"]) > 7
                or any(char not in features.valid_characters for char in row["name"])
                or not valid_frequency(row["rx_hz"], features.valid_bands)
                or row["mode"] not in ("FM", "NFM")
                or row["power"] not in [str(level) for level in powers(radio)]
                or type(row["skip"]) is not bool):
            raise ProgrammingError("A channel name, receive frequency, mode, power or scan value is invalid.")
        tx_hz = row["tx_hz"]
        if tx_hz is not None:
            unchanged_tx = not existing.empty and tx_hz == old_row["tx_hz"]
            if (not valid_frequency(tx_hz, features.valid_bands)
                    or (not unchanged_tx and not valid_frequency(tx_hz, TX_BANDS))):
                raise ProgrammingError("New or changed transmit frequencies must be within this project's amateur-band profile; choose transmit disabled for receive-only channels.")
        memory = chirp_common.Memory()
        memory.number, memory.name, memory.freq = number, row["name"], row["rx_hz"]
        memory.mode, memory.skip = row["mode"], "S" if row["skip"] else ""
        memory.power = next(level for level in powers(radio) if str(level) == row["power"])
        memory.duplex, memory.offset = ("off", 0) if tx_hz is None else ("split", tx_hz)
        chirp_common.split_tone_decode(memory,
                                      valid_editor_tone(row["tx_tone"], chirp_common, features),
                                      valid_editor_tone(row["rx_tone"], chirp_common, features))
        errors = radio.validate_memory(memory)
        if any(isinstance(error, chirp_common.ValidationError) for error in errors):
            raise ProgrammingError("CHIRP rejected a proposed memory value. No upload was attempted.")
        raw_memory = radio._memobj.memory[number].get_raw()
        raw_name = radio._memobj.names[number].get_raw()
        # Keep the existing BCL, PTT-ID and S-CODE, plus all undocumented bits.
        # CHIRP otherwise zeroes the complete memory record on every edit.
        if not existing.empty:
            memory.extra = existing.extra
        radio.set_memory(memory)
        if not existing.empty:
            edited = bytearray(radio._memobj.memory[number].get_raw())
            for key, start, end in (("rx_hz", 0, 4), ("tx_hz", 4, 8),
                                    ("rx_tone", 8, 10), ("tx_tone", 10, 12)):
                if row[key] == old_row[key]:
                    edited[start:end] = raw_memory[start:end]
            for index, mask in ((12, 0x00), (13, 0x00), (14, 0x03), (15, 0x44)):
                edited[index] = (raw_memory[index] & ~mask) | (edited[index] & mask)
            for key, index, mask in (("power", 14, 0x03), ("mode", 15, 0x40), ("skip", 15, 0x04)):
                if row[key] == old_row[key]:
                    edited[index] = (raw_memory[index] & mask) | (edited[index] & ~mask)
            radio._memobj.memory[number].set_raw(bytes(edited))
            edited_name = radio._memobj.names[number].get_raw()
            radio._memobj.names[number].set_raw(raw_name if row["name"] == old_row["name"]
                                               else edited_name[:7] + raw_name[7:])
    fields = setting_fields(radio)
    selected = settings_module.RadioSettingGroup("editor", "Editor operating settings")
    for name, value in changed_settings.items():
        descriptor = editor_settings(radio).get(name)
        if descriptor is None or name not in fields:
            raise ProgrammingError("The editor requested a protected or unknown radio setting.")
        kind = descriptor["type"]
        if ((kind == "boolean" and type(value) is not bool)
                or (kind == "select" and (not isinstance(value, str) or value not in descriptor["options"]))
                or (kind == "integer" and (type(value) is not int or not descriptor["min"] <= value <= descriptor["max"]))):
            raise ProgrammingError("An operating setting has an invalid value.")
        fields[name].value = value
        selected.append(fields[name])
        obj = radio._memobj
        for part in name.split("."):
            obj = getattr(obj, part)
        selected_offsets.add(obj.get_offset())
    if changed_settings:
        radio.set_settings(settings_module.RadioSettings(selected))
    after = packed(radio)
    changed = {i for i, (old, new) in enumerate(zip(before, after)) if old != new}
    if len(before) != len(after) or not changed <= selected_offsets or not changed <= profile_allowed(radio, True):
        raise ProgrammingError("The editor proposal changes protected radio data. No upload was attempted.")
    if any((before[index] ^ after[index]) & ~mask for index, mask in profile_partial(radio).items()):
        raise ProgrammingError("The editor proposal changes protected setting bits. No upload was attempted.")
    return changed_blocks(radio, before, after)


def apply_plan(radio, role, chirp_common, settings_module, catalog=None):
    """Modify only memory 1 and a minimal tree of named global settings."""
    if catalog is None or role not in REPEATER_ROLES:
        raise ProgrammingError("Configure this Pi's private channel pairs locally before using default programming.")
    plan = default_plan(catalog)
    before = packed(radio)
    identity = image_identity(radio)
    if not identity["compatible"]:
        raise ProgrammingError("This radio revision needs a profile review before automatic programming. Its read-only backup is available on the Pi.")
    features = radio.get_features()
    if (features.memory_bounds != (0, 127) or features.valid_name_length != 7
            or radio.MODEL != "5RX" or radio._memobj.memory[1].get_offset() != 0x10
            or radio._memobj.names[1].get_offset() != 0x1010):
        raise ProgrammingError("The installed CHIRP driver layout differs from the reviewed profile.")
    fields = dict((item.get_name(), item) for item in flatten_settings(radio.get_settings()))
    actual_offsets = set()
    selected = settings_module.RadioSettingGroup("repeater", "Repeater settings")
    for name, value in GLOBAL_SETTINGS.items():
        if name not in fields:
            raise ProgrammingError("The selected radio is missing an expected setting. No upload was attempted.")
        obj = radio._memobj
        for part in name.split("."):
            obj = getattr(obj, part)
        actual_offsets.add(obj.get_offset())
        fields[name].value = value
        selected.append(fields[name])
    if actual_offsets != GLOBAL_OFFSETS:
        raise ProgrammingError("The installed global setting layout differs from the reviewed profile.")
    desired = plan[role]
    memory = chirp_common.Memory()
    memory.number = 1
    memory.name = desired["name"]
    memory.freq = int(round(float(desired["frequency_mhz"]) * 1_000_000))
    memory.duplex = "" if desired["transmit"] else "off"
    memory.offset = 0
    memory.mode = "NFM"
    memory.skip = ""
    memory.power = next(level for level in radio.POWER_LEVELS if str(level) == desired["power"])
    if role == "rx" and desired["receive_tone_hz"] is not None:
        memory.tmode = "Cross"
        memory.cross_mode = "->Tone"
        memory.ctone = desired["receive_tone_hz"]
    elif role == "tx" and desired["transmit_tone_hz"] is not None:
        memory.tmode = "Tone"
        memory.rtone = desired["transmit_tone_hz"]
    else:
        memory.tmode = ""
    errors = radio.validate_memory(memory)
    if any(isinstance(error, chirp_common.ValidationError) for error in errors):
        raise ProgrammingError("CHIRP rejected the fixed repeater channel plan.")
    radio.set_memory(memory)
    radio.set_settings(settings_module.RadioSettings(selected))
    after = packed(radio)
    if len(after) != len(before):
        raise ProgrammingError("The radio image length changed unexpectedly.")
    changed = {index for index, (old, new) in enumerate(zip(before, after)) if old != new}
    if not changed <= ALLOWED_OFFSETS:
        raise ProgrammingError("The proposed image changes protected radio data. No upload was attempted.")
    if any((before[index] ^ after[index]) & ~mask for index, mask in PARTIAL_MASKS.items()):
        raise ProgrammingError("The proposed image changes protected setting bits. No upload was attempted.")
    return sorted({index - index % 16 for index in changed})


def pair_memory(pair_id, role, catalog):
    """Copy a private pair into working memory 1; never select a cursor."""
    plans = pair_plans(catalog)
    if pair_id not in plans or role not in REPEATER_ROLES:
        raise ProgrammingError("Choose a configured private channel pair.")
    plan = plans[pair_id]
    tone = {"type": "CTCSS", "value": plan["tone_hz"]} if plan["tone_hz"] is not None else None
    return {"number": 1, "empty": False, "name": plan["rx_name"] if role == "rx" else plan["tx_name"],
            "rx_hz": plan["rx_hz"] if role == "rx" else plan["tx_hz"],
            "tx_hz": None if role == "rx" else plan["tx_hz"],
            "tx_tone": None if role == "rx" else tone, "rx_tone": tone if role == "rx" else None,
            "mode": "NFM", "power": "High", "skip": False}


def pair_preflight(radio):
    """Require the qualified operating configuration without changing globals."""
    if radio_model(radio) != "5RX" or not image_identity(radio)["compatible"]:
        raise ProgrammingError("Both radios must match the reviewed 5RX profile before changing a pair.")
    fields = {item.get_name(): item for item in flatten_settings(radio.get_settings())}
    required = {"settings.workmode": "Channel", "settings.displayab": "A",
                "settings.tdr": False, "settings.vox": "Off", "settings.save": "Off",
                "settings.autolk": True}
    for name, desired in required.items():
        if name not in fields:
            raise ProgrammingError("An expected operating setting is missing. Review this radio locally.")
        actual = bool(fields[name].value) if type(desired) is bool else str(fields[name].value)
        if actual != desired:
            raise ProgrammingError("Both radios must use Channel mode, active display A, dual watch off, VOX off, battery saver off, and Auto Lock on before changing a pair. Use the editor to review their operating settings.")
    timeout = str(fields["settings.timeout"].value) if "settings.timeout" in fields else ""
    match = re.fullmatch(r"([0-9]+) sec", timeout)
    if not match or not 15 <= int(match.group(1)) <= 60:
        raise ProgrammingError("Both radios need a transmit timeout of 15–60 seconds before changing a pair.")


def semantic_values(radio):
    memory = radio.get_memory(1)
    values = {"empty": memory.empty, "frequency": memory.freq, "name": memory.name,
              "duplex": memory.duplex, "offset": memory.offset, "mode": memory.mode,
              "power": str(memory.power), "tone_mode": memory.tmode,
              "cross_mode": memory.cross_mode if memory.tmode == "Cross" else None,
              "rx_tone": memory.ctone if memory.tmode == "Cross" else None,
              "tx_tone": memory.rtone if memory.tmode == "Tone" else None}
    fields = setting_fields(radio)
    names = editor_settings(radio) if radio_model(radio) == "UV5R" else GLOBAL_SETTINGS
    values["settings"] = {name: str(fields[name].value) for name in names}
    return values


def verify_images(original, candidate, observed):
    """Verify intended meaning and unchanged unrelated data, including calibration."""
    if not same_identity(image_identity(original), image_identity(observed)):
        raise ProgrammingError("The connected radio identity differs from the inspected radio.")
    expected, actual = semantic_values(candidate), semantic_values(observed)
    if expected != actual:
        differences = []
        for name in expected:
            if name == "settings":
                for setting, value in expected[name].items():
                    if value != actual[name].get(setting):
                        differences.append(f"{setting}: expected {value!r}, read {actual[name].get(setting)!r}")
            elif expected[name] != actual.get(name):
                differences.append(f"{name}: expected {expected[name]!r}, read {actual.get(name)!r}")
        details = "; ".join(differences[:6])[:650]
        raise ProgrammingError("The channel or global settings did not verify. " + details + ".")
    changed = [i for i, (a, b) in enumerate(zip(packed(candidate), packed(observed)))
               if (a ^ b) & ~profile_volatile(candidate).get(i, 0)]
    if changed:
        details = ", ".join(f"0x{i:04X}" for i in changed[:8])
        raise ProgrammingError(f"The readback image differs from the saved candidate at {len(changed)} bytes ({details}).")


def saved_repair_pages(saved_original, saved_candidate, editor=False):
    """Derive repair authority solely from a previously reviewed private image.

    The saved operation must obey its original semantic write policy. Companion
    bytes are recoverable only in the same 32-byte operating pages as those
    reviewed differences; service and calibration pages are never eligible.
    """
    original, candidate = packed(saved_original), packed(saved_candidate)
    first, second = image_identity(saved_original), image_identity(saved_candidate)
    if not first["compatible"] or not second["compatible"] or not same_identity(first, second):
        raise ProgrammingError("The saved repair images do not match the reviewed radio profile.")
    allowed = profile_allowed(saved_original, editor)
    changed = {i for i, (old, new) in enumerate(zip(original, candidate)) if old != new}
    if not changed or not changed <= allowed:
        raise ProgrammingError("The saved candidate does not contain a valid reviewed operating change.")
    if any((original[i] ^ candidate[i]) & ~mask for i, mask in profile_partial(saved_original).items()):
        raise ProgrammingError("The saved candidate changes protected setting bits.")
    base = image_base(saved_original)
    pages = sorted({(i - base) // 0x20 * 0x20 for i in changed})
    operating_ranges = ((0x0000, 0x0800), (0x0E20, 0x0E60), (0x1000, 0x1800))
    if any(not any(start <= page and page + 0x20 <= end for start, end in operating_ranges)
           for page in pages):
        raise ProgrammingError("The saved repair would extend outside reviewed operating data.")
    return pages


def saved_repair_image(current, saved_original, saved_candidate, editor=False):
    """Return the exact reconstruction, leaving current runtime bits intact."""
    pages = saved_repair_pages(saved_original, saved_candidate, editor)
    actual, wanted = packed(current), packed(saved_candidate)
    identity = image_identity(current)
    if not identity["compatible"] or not same_identity(identity, image_identity(saved_candidate)):
        raise ProgrammingError("The attached radio does not match the saved repair profile.")
    base = image_base(current)
    offsets = {i + base for page in pages for i in range(page, page + 0x20)}
    if any((old ^ new) & ~profile_volatile(current).get(i, 0)
           for i, (old, new) in enumerate(zip(wanted, actual)) if i not in offsets):
        raise ProgrammingError("Radio data outside the saved upload's affected pages changed. Review the private backups locally; no repair was attempted.")
    repaired = bytearray(actual)
    for i in offsets:
        mask = profile_volatile(current).get(i, 0)
        repaired[i] = (wanted[i] & ~mask) | (actual[i] & mask)
    return bytes(repaired), pages


def prepare_saved_repair(radio, saved_original, saved_candidate, editor=False):
    """Apply only the trusted saved pages to a fresh, already-backed-up read."""
    before = packed(radio)
    repaired, _pages = saved_repair_image(radio, saved_original, saved_candidate, editor)
    radio.get_mmap().set(0, repaired)
    radio.process_mmap()
    return changed_blocks(radio, before, repaired)


def validate_saved_repair(original, candidate, saved_repair, editor=False):
    """Independently prove upload bytes came only from private saved images."""
    if not isinstance(saved_repair, tuple) or len(saved_repair) != 2:
        raise ProgrammingError("The saved repair proof is invalid.")
    expected, pages = saved_repair_image(original, *saved_repair, editor=editor)
    proposed, before = packed(candidate), packed(original)
    if proposed != expected:
        raise ProgrammingError("The repair candidate differs from the protected saved upload.")
    blocks = changed_blocks(original, before, proposed)
    return blocks, pages


def physical_write_groups(blocks):
    """Bounded contiguous transfers, independent of the logical edit authority.

    Names use an experimental 256-byte contiguous transfer, pending physical
    qualification. It does not establish internal erase-page geometry. Channels
    and global settings keep their previously qualified 32-byte transfers.
    """
    groups = set()
    for address in blocks:
        if type(address) is not int or address % 16:
            raise ProgrammingError("The upload contains an invalid physical block.")
        width = 0x100 if 0x1000 <= address < 0x1800 else 0x20
        start = address // width * width
        if not any(lower <= start and start + width <= upper
                   for lower, upper in ((0, 0x800), (0xE20, 0xE60), (0x1000, 0x1800))):
            raise ProgrammingError("The upload page extends outside reviewed operating data.")
        groups.add((start, width))
    return sorted(groups)


class Hardware:
    """The only hardware-facing adapter; tests replace this with a fake."""
    def __init__(self, vendor=VENDOR_PATH):
        marker = vendor / "PINNED_COMMIT"
        if marker.read_text().strip() != CHIRP_COMMIT:
            raise ProgrammingError("The reviewed CHIRP runtime is missing. Rerun the current installer.")
        sys.path.insert(0, str(vendor))
        import serial
        from chirp import chirp_common, settings, memmap
        from chirp.drivers import baofeng_common
        from chirp.drivers.baofeng_wp970i import BF5RXRadio
        from chirp.drivers import uv5r
        self.uv5r = uv5r
        self.drivers = {"5RX": BF5RXRadio, "UV5R": uv5r.BaofengUV5RGeneric}
        self.model = "5RX"
        self.catalog = None
        self.serial = serial
        self.driver = BF5RXRadio
        self.common = baofeng_common
        self.chirp_common = chirp_common
        self.settings = settings
        self.memmap = memmap

    def select_model(self, model):
        if model not in MODELS:
            raise ProgrammingError("Choose a supported radio model.")
        self.model, self.driver = model, self.drivers[model]

    def open(self, device):
        # AIOC firmware >=1.2 keys PTT when DTR=1, RTS=0. Apply deasserted
        # DTR and asserted RTS before open; pyserial defaults can briefly key it.
        port = self.serial.Serial(port=None, baudrate=9600, timeout=1,
                                  write_timeout=3, exclusive=True)
        port.dtr = False
        port.rts = True
        port.port = device
        try:
            target = os.stat(device)
            # flock alone is advisory. Refuse existing owners even if they do
            # not cooperate with pyserial, then use kernel tty exclusivity.
            for process in Path("/proc").iterdir():
                if not process.name.isdigit() or process.name == str(os.getpid()):
                    continue
                try:
                    descriptors = list((process / "fd").iterdir())
                except (FileNotFoundError, PermissionError):
                    continue
                for descriptor in descriptors:
                    try:
                        info = descriptor.stat()
                    except (FileNotFoundError, PermissionError):
                        continue
                    if stat.S_ISCHR(info.st_mode) and info.st_rdev == target.st_rdev:
                        raise ProgrammingError("Another process is using this AIOC serial port. Close it before programming.")
            port.open()
            fcntl.ioctl(port.fileno(), termios.TIOCEXCL)
            port.dtr = True  # Programming mode, with RTS held high throughout.
            return port
        except Exception:
            self.close(port)
            raise

    @staticmethod
    def close(port):
        try:
            if port.is_open:
                port.dtr = False
        finally:
            port.close()

    def download(self, device, progress):
        port = self.open(device)
        try:
            radio = self.driver(port)
            radio.status_fn = lambda status: report_progress(progress, int(status.cur), int(status.max))
            radio.sync_in()
            image_identity(radio)
            return radio
        except Exception:
            self.close(port)
            raise

    def release(self, radio):
        if radio is not None and getattr(radio, "pipe", None) is not None:
            self.close(radio.pipe)

    def readback(self, radio, progress):
        if radio_model(radio) == "UV5R":
            # UV-5R packs the ident before main memory and compresses its aux
            # address gap. Use this driver's reader, never the 5RX image shape.
            data = bytearray(packed(radio)[:8])
            addresses = [(address, 0x40) for address in range(0, 0x1800, 0x40)]
            addresses += [(address, 0x40) for address in range(0x1EC0, 0x1FC0, 0x40)]
            addresses += [(address, 0x10) for address in range(0x1FC0, 0x2000, 0x10)]
            for completed, (address, size) in enumerate(addresses, 1):
                data.extend(self.uv5r._read_block(radio, address, size, False))
                report_progress(progress, completed, len(addresses))
            observed = self.driver(None)
            observed._mmap = self.memmap.MemoryMapBytes(bytes(data))
            observed.process_mmap()
            return observed
        # Continue an existing clone session. Normal post-upload verification
        # instead closes it and opens a fresh session in settle_and_verify.
        data = bytearray()
        completed = 0
        for address in range(0, 0x2000, 0x40):
            if address == 0x1FC0:
                # Several revisions drop a byte in the final 64-byte block.
                for tail in range(address, address + 0x40, 0x10):
                    data.extend(self.common._read_block(radio, tail, 0x10, False))
                    completed += 1
                    report_progress(progress, completed, 131)
            else:
                data.extend(self.common._read_block(radio, address, 0x40, False))
                completed += 1
                report_progress(progress, completed, 131)
        observed = self.driver(None)
        observed._mmap = self.memmap.MemoryMapBytes(bytes(data) + packed(radio)[0x2000:0x2008])
        observed.process_mmap()
        return observed

    def settle_and_verify(self, device, radio, progress):
        # A clone read immediately after the final write can interrupt a radio
        # that is returning to normal operation. Release the upload connection
        # and leave the radio quiet before a new identification/read session.
        self.release(radio)
        radio.pipe = None
        progress("settling", "Upload complete. Waiting 5 quiet seconds before reconnecting for verification.", True)
        time.sleep(5)
        progress("verifying", "Reconnecting and reading the radio to verify its saved settings.", True)
        observed = self.download(device, progress)
        try:
            return observed
        finally:
            self.release(observed)
            observed.pipe = None

    def load(self, path):
        return self.driver(str(path))

    def prepare(self, radio, role):
        return apply_plan(radio, role, self.chirp_common, self.settings, getattr(self, "catalog", None))

    def editor_snapshot(self, radio, role, base_id):
        return editor_snapshot(radio, role, base_id, self.chirp_common)

    def recovery_differences(self, expected, observed):
        return recovery_differences(expected, observed, self.chirp_common)

    def prepare_editor(self, radio, changes, changed_settings):
        return apply_editor(radio, changes, changed_settings, self.chirp_common, self.settings)

    def upload(self, device, original, candidate, blocks, progress, before_write, editor=False,
               saved_repair=None):
        # Deliberately do not call sync_out(): that driver's generic upload can
        # expand into service/calibration ranges even when bytes are unchanged.
        # Match the stock upload's fresh identification and auxiliary preflight.
        # Logical changes remain allowlisted. Contiguous physical transfers
        # preserve every companion byte from the freshly prepared candidate.
        radio = candidate
        raw = packed(original)
        proposed = packed(candidate)
        identity = image_identity(original)
        if (not same_identity(identity, image_identity(candidate)) or not identity["compatible"]
                or len(proposed) != len(raw)):
            raise ProgrammingError("The upload has an invalid reviewed-profile image size.")
        expected = changed_blocks(original, raw, proposed)
        if blocks != expected:
            raise ProgrammingError("The upload contains an unapproved block.")
        if saved_repair is None:
            allowed = profile_allowed(original, editor)
            base = image_base(original)
            if any(address not in {(i - base) // 16 * 16 for i in allowed} for address in blocks):
                raise ProgrammingError("The upload contains an unapproved block.")
            if any(raw[i] != proposed[i] for i in range(len(raw)) if i not in allowed):
                raise ProgrammingError("The upload changes protected radio data.")
            if any((raw[index] ^ proposed[index]) & ~mask for index, mask in profile_partial(original).items()):
                raise ProgrammingError("The upload changes protected setting bits.")
            page_starts = sorted({address - address % 0x20 for address in blocks})
        else:
            # The worker supplies root-private saved images, never caller
            # offsets. Reconstruct and validate their original allowed change
            # independently before repairing a damaged companion half.
            logical, page_starts = validate_saved_repair(original, candidate, saved_repair, editor=editor)
            if blocks != logical:
                raise ProgrammingError("The repair upload differs from its reviewed saved candidate.")
        if not blocks:
            return
        # Names use a complete contiguous 256-byte transfer, preserving the
        # fresh candidate's other names and reserved bytes. Channels and globals
        # retain 32-byte transfers. This is an empirical transfer strategy, not
        # a claim about the firmware's internal erase-page implementation.
        groups = physical_write_groups(page_starts)
        physical_blocks = [address for start, width in groups for address in range(start, start + width, 16)]
        group_ends = {start + width - 16 for start, width in groups}
        if getattr(radio, "pipe", None) is None or not radio.pipe.is_open:
            raise ProgrammingError("The fresh radio clone session is no longer open.")
        # A download session and an upload session are separate in stock CHIRP.
        # Closing the old handle also deasserts AIOC PTT before the quiet period.
        self.release(radio)
        radio.pipe = None
        if getattr(progress, "radio_metrics", False):
            progress("settling", "Original backup saved. Waiting 5 quiet seconds before a fresh upload session.", True)
        else:
            progress()
        time.sleep(5)
        try:
            radio.pipe = self.open(device)
            if getattr(progress, "radio_metrics", False):
                progress("upload-preflight", "Identifying the radio again and checking its protected auxiliary data before writing.", True)
            else:
                progress()
            protocol = self.uv5r if radio_model(radio) == "UV5R" else self.common
            identity = protocol._ident_radio(radio)
            expected_ident = raw[:8] if radio_model(radio) == "UV5R" else raw[0x2000:0x2008]
            if identity != expected_ident:
                raise ProgrammingError("The radio identity changed after its fresh backup. No upload was attempted.")
            firmware, aux1, aux2, aux3, _dropped_byte = protocol._get_aux_data_from_radio(radio)
            if radio_model(radio) == "UV5R":
                expected_aux = (raw[0x1838:0x1846], raw[0x1848:0x18A8],
                                raw[0x18B8:0x18C8], raw[0x18D8:0x1908])
            else:
                expected_aux = (raw[0x1EF0:0x1EFE], raw[0x1F00:0x1F60],
                                raw[0x1F70:0x1F80], raw[0x1F90:0x1FC0])
            if (firmware, aux1, aux2, aux3) != expected_aux:
                raise ProgrammingError("The radio's protected data changed after its fresh backup. No upload was attempted.")
            before_write()
            for index, address in enumerate(physical_blocks):
                native = address + image_base(radio)
                frame = self.common._make_frame("X", address, 16, proposed[native:native + 16])
                sent = radio.pipe.write(frame)
                if type(sent) is not int or sent != len(frame):
                    raise ProgrammingError("A serial upload write was incomplete. Keep services paused; power-cycle and verify the radio.")
                time.sleep(0.05)
                if self.common._rawrecv(radio, 1) != b"\x06":
                    raise ProgrammingError("An upload block was not acknowledged. Keep services paused; power-cycle and verify the radio.")
                # A progress callback can fsync state. Finish the complete
                # contiguous transfer before allowing that host work.
                if address in group_ends:
                    report_progress(progress, index + 1, len(physical_blocks))
        except Exception:
            try:
                self.release(radio)
            except Exception:
                # Keep an unclosed handle reachable for the worker's finally.
                pass
            if getattr(radio, "pipe", None) is not None and not radio.pipe.is_open:
                radio.pipe = None
            raise


def systemctl(*args):
    result = subprocess.run(["/usr/bin/systemctl", *args], stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, timeout=20, check=False,
                            env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"})
    if args[0] in ("is-active", "is-enabled"):
        return result.returncode == 0 and result.stdout.strip() in ("active", "enabled", "enabled-runtime")
    if result.returncode:
        raise ProgrammingError("A radio service could not be paused or resumed. Check the Pi locally.")
    return True


class Worker:
    def __init__(self, hardware, state_dir=STATE_DIR, backup_dir=BACKUP_DIR,
                 group_id=None, service=systemctl, owner_uid=0,
                 pair_config_file=PAIR_CONFIG_PATH, catalog_file=CATALOG_PATH):
        self.hardware = hardware
        self.state_dir = Path(state_dir)
        self.backup_dir = Path(backup_dir)
        self.group_id = group_id
        self.service = service
        self.owner_uid = owner_uid
        self.pair_config_file = Path(pair_config_file)
        self.catalog_file = Path(catalog_file)
        try:
            self.catalog = validate_catalog(read_json(
                self.catalog_file, 8192, private=True, owner_uid=owner_uid, confidential=True))
        except (OSError, ValueError, ProgrammingError):
            self.catalog = None
        self.private_path = self.backup_dir / "state.json"
        try:
            self.private = read_json(self.private_path, private=True, owner_uid=owner_uid)
        except FileNotFoundError:
            self.private = {}

    def publish(self):
        atomic_json(self.private_path, self.private, self.group_id)
        atomic_json(self.state_dir / "radio-programming-state.json",
                    {**{role: public_role(self.private.get(role)) for role in ROLES},
                     "_pair": public_pair(self.private.get("_pair"), self.catalog)}, self.group_id)

    def invalidate_pair(self, role):
        """An independent radio write makes a former paired label stale."""
        state = self.private.get("_pair")
        if role in REPEATER_ROLES and isinstance(state, dict) and state.get("stored_verified"):
            state.update(stored_verified=False, phase="invalidated")
            self.publish()

    def invalidate_editor(self, role):
        previous = self.private.get(role)
        if isinstance(previous, dict):
            for name in ("editor_base_id", "editor_image_kind", "editor_image_sha256", "editor_device", "editor_pending_id", "editor_model"):
                previous.pop(name, None)
            self.publish()
        (self.state_dir / f"radio-editor-{role}.json").unlink(missing_ok=True)

    def publish_editor(self, radio, role, base_id, device, image_kind="original"):
        snapshot = self.hardware.editor_snapshot(radio, role, base_id)
        state = self.private[role]
        pending_id = state.get("program_id") if state.get("verification_required") else None
        recovery = {"required": bool(state.get("verification_required")), "pending_id": pending_id,
                    "message": str(state.get("verification_message", ""))[:1000] if pending_id else "",
                    "differences": [], "protected_mismatch": False}
        if pending_id:
            if not isinstance(pending_id, str) or not REQUEST_RE.fullmatch(pending_id):
                raise ProgrammingError("The unfinished upload record needs local review before editing.")
            candidate = self.hardware.load(self.backup_dir / pending_id / "candidate.img")
            recovery["differences"] = self.hardware.recovery_differences(candidate, radio)
            recovery["protected_mismatch"] = bool(protected_editor_changes(candidate, radio))
            if recovery["protected_mismatch"]:
                recovery["message"] = "Protected radio data differs from the unfinished candidate. Review the private backups locally before writing or accepting current values."
        snapshot["recovery"] = recovery
        self.private[role].update(editor_base_id=base_id, editor_image_kind=image_kind,
                                  editor_image_sha256=hashlib.sha256(packed(radio)).hexdigest(),
                                  editor_device=device, editor_pending_id=pending_id,
                                  editor_model=radio_model(radio))
        self.publish()
        atomic_json(self.state_dir / f"radio-editor-{role}.json", snapshot, self.group_id)

    def backup(self, radio, request_id, kind, role, device):
        folder = self.backup_dir / request_id
        folder.mkdir(mode=0o750, exist_ok=False)
        if self.group_id is not None:
            os.chown(folder, 0, self.group_id)
        path = folder / (kind + ".img")
        try:
            radio.save_mmap(str(path))
            os.chmod(path, 0o640)
            if self.group_id is not None:
                os.chown(path, 0, self.group_id)
            sync_path(path)
            manifest = {"schema": 1, "created_at": datetime.now(timezone.utc).isoformat(),
                        "role": role, "device": device, "selected_model": radio_model(radio),
                        "chirp_commit": CHIRP_COMMIT, **image_identity(radio),
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            atomic_json(folder / "manifest.json", manifest, self.group_id)
            sync_path(folder)
            sync_path(self.backup_dir)
        except Exception:
            # Before a durable complete backup exists, no write is possible.
            # Remove only files created by this invocation so retries cannot
            # accumulate malformed sets outside the normal retention policy.
            for entry in (path, folder / "manifest.json"):
                entry.unlink(missing_ok=True)
            folder.rmdir()
            raise
        return folder

    def save_observed(self, radio, folder):
        """Keep a failed readback available privately without exposing image bytes."""
        path = folder / "observed.img"
        radio.save_mmap(str(path))
        os.chmod(path, 0o640)
        if self.group_id is not None:
            os.chown(path, 0, self.group_id)
        sync_path(path)
        sync_path(folder)

    def load_saved_repair(self, previous, role, device, pending_id):
        """Load a bounded root-private repair source, never a request path.

        Legacy candidates have no historical digest. Their ownership, original
        manifest, identity and original-operation diff are checked; subsequent
        repairs additionally pin both source file digests in private state.
        """
        pair = self.private.get("_pair")
        if (previous.get("program_kind") == "pair"
                or isinstance(pair, dict) and pair.get("verification_required")):
            raise ProgrammingError("A paired-radio transaction needs local review before a saved upload can be repaired.")
        if (previous.get("verification_required") is not True or previous.get("program_id") != pending_id
                or not isinstance(pending_id, str) or not REQUEST_RE.fullmatch(pending_id)
                or previous.get("device") != device
                or previous.get("program_kind") not in ("repeater", "editor")):
            raise ProgrammingError("The unfinished upload changed. Refresh and review its saved candidate before repairing it.")

        def private_file(path):
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
            try:
                info = os.fstat(descriptor)
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != self.owner_uid
                        or info.st_mode & 0o022 or info.st_size > 65536):
                    raise ProgrammingError("The private saved repair files have invalid ownership or permissions.")
                with os.fdopen(descriptor, "rb", closefd=False) as stream:
                    content = stream.read(65537)
                if len(content) > 65536:
                    raise ProgrammingError("The private saved repair image is too large.")
                return content
            finally:
                os.close(descriptor)

        def saved_images(identifier):
            if not isinstance(identifier, str) or not REQUEST_RE.fullmatch(identifier):
                raise ProgrammingError("The private saved repair source is invalid.")
            folder = self.backup_dir / identifier
            info = folder.lstat()
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != self.owner_uid or info.st_mode & 0o022):
                raise ProgrammingError("The private saved repair folder has invalid ownership or permissions.")
            manifest = read_json(folder / "manifest.json", private=True, owner_uid=self.owner_uid)
            if (type(manifest.get("schema")) is not int or manifest["schema"] != 1
                    or manifest.get("role") != role or manifest.get("device") != device
                    or manifest.get("selected_model", "5RX") != previous.get("program_model", "5RX") or manifest.get("chirp_commit") != CHIRP_COMMIT):
                raise ProgrammingError("The saved repair metadata differs from this radio's configured connection.")
            original_path, candidate_path = folder / "original.img", folder / "candidate.img"
            original_digest = hashlib.sha256(private_file(original_path)).hexdigest()
            candidate_digest = hashlib.sha256(private_file(candidate_path)).hexdigest()
            if original_digest != manifest.get("sha256"):
                raise ProgrammingError("The original saved repair backup failed its integrity check.")
            original, candidate = self.hardware.load(original_path), self.hardware.load(candidate_path)
            if (not same_identity(manifest, image_identity(original))
                    or not same_identity(previous, image_identity(original))):
                raise ProgrammingError("The saved repair identity differs from the pending radio.")
            return original, candidate, original_digest, candidate_digest

        source_id = previous.get("repair_source_id", pending_id)
        source_kind = previous.get("repair_source_kind", previous["program_kind"])
        if source_kind not in ("repeater", "editor") or source_kind != previous["program_kind"]:
            raise ProgrammingError("The saved repair operation type is invalid.")
        original, candidate, original_digest, candidate_digest = saved_images(source_id)
        saved_repair_pages(original, candidate, editor=source_kind == "editor")
        if "repair_source_id" in previous:
            if (previous.get("repair_source_original_sha256") != original_digest
                    or previous.get("repair_source_candidate_sha256") != candidate_digest):
                raise ProgrammingError("The protected saved repair source failed its integrity check.")
            pending_original, pending_candidate, _digest, pending_digest = saved_images(pending_id)
            if previous.get("repair_candidate_sha256") != pending_digest:
                raise ProgrammingError("The pending repair candidate failed its integrity check.")
            validate_saved_repair(pending_original, pending_candidate, (original, candidate),
                                  editor=source_kind == "editor")
        return (original, candidate), {"repair_source_id": source_id, "repair_source_kind": source_kind,
                                     "repair_source_original_sha256": original_digest,
                                     "repair_source_candidate_sha256": candidate_digest}

    def prune_backups(self):
        """Keep five sets per role, including the current verification candidate.

        Only worker-created, well-formed directories with known regular files
        are eligible. Symlinks, unknown files and unrelated folders are retained.
        """
        protected = {item.get("program_id") for item in self.private.values()
                     if isinstance(item, dict) and (item.get("programmed") or item.get("verification_required"))}
        protected.update(item.get("editor_base_id") for item in self.private.values() if isinstance(item, dict))
        protected.update(item.get("superseded_program_id") for item in self.private.values()
                         if isinstance(item, dict) and item.get("verification_required"))
        protected.update(item.get("repair_source_id") for item in self.private.values()
                         if isinstance(item, dict) and (item.get("programmed") or item.get("verification_required")))
        pair = self.private.get("_pair")
        if isinstance(pair, dict) and pair.get("verification_required"):
            protected.update(pair.get("protected_backups", []))
        sets = {role: [] for role in ROLES}
        for folder in self.backup_dir.iterdir():
            if not REQUEST_RE.fullmatch(folder.name):
                continue
            info = folder.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != self.owner_uid or info.st_mode & 0o022:
                continue
            try:
                manifest = read_json(folder / "manifest.json", private=True, owner_uid=self.owner_uid)
                if (manifest.get("schema") != 1 or manifest.get("role") not in ROLES
                        or manifest.get("selected_model", "5RX") not in MODELS
                        or manifest.get("role") in REPEATER_ROLES and manifest.get("selected_model", "5RX") != "5RX"):

                    continue
                entries = list(folder.iterdir())
                if any(entry.name not in {"original.img", "upload-base.img", "candidate.img", "verified.img", "observed.img", "manifest.json"}
                       or not stat.S_ISREG(entry.lstat().st_mode) or entry.lstat().st_uid != self.owner_uid
                       for entry in entries):
                    continue
                sets[manifest["role"]].append((info.st_mtime_ns, folder))
            except (OSError, ValueError, ProgrammingError):
                continue
        for folders in sets.values():
            folders.sort(key=lambda value: value[0], reverse=True)
            retained = len(folders)
            for _, folder in reversed(folders):
                if retained <= 5:
                    break
                if folder.name in protected:
                    continue
                for entry in folder.iterdir():
                    entry.unlink()
                folder.rmdir()
                retained -= 1
        sync_path(self.backup_dir)

    def run_pair(self, request, config):
        """Back up both connected radios, then change working memory 1 serially.

        The root-only qualification gate attests to the physical memory-1
        operating procedure. This records verified stored configuration, never
        a live channel reading. A partial pair is a durable service interlock.
        """
        if not valid_request(request) or request.get("action") != "change-pair" or not valid_config(config):
            raise ProgrammingError("The paired-radio request or configuration is invalid.")
        if hasattr(self.hardware, "select_model"):
            self.hardware.select_model("5RX")
        pair_id, request_id = request["pair_id"], request["request_id"]
        plans = pair_plans(self.catalog) if self.catalog is not None else {}
        digest = catalog_digest(self.catalog) if self.catalog is not None else None
        result = {"request_id": request_id, "action": "change-pair", "role": "pair",
                  "pair_id": pair_id, "success": None, "phase": "preflight",
                  "message": "Checking paired-radio configuration.", "backup_id": None,
                  "verification_required": False, "progress": None, "events": []}
        last_progress = 0

        def progress(phase=None, message=None, force=False, current=None, total=None, unit="blocks"):
            nonlocal last_progress
            if phase and phase != result["phase"]:
                result["phase"] = phase
                result["progress"] = None
            if message:
                result["message"] = message
                result["events"] = [*result["events"][-19:],
                                    {"time": datetime.now(timezone.utc).isoformat(), "message": message[:1000]}]
            if type(current) is int and type(total) is int and 0 <= current <= total <= 1_000_000 and total:
                result["progress"] = {"current": current, "total": total,
                                      "percent": current * 100 // total,
                                      "unit": unit if unit in ("blocks", "steps") else "blocks"}
            if force or time.monotonic() - last_progress >= 0.5:
                atomic_json(self.state_dir / "radio-programming-result.json", result, self.group_id)
                last_progress = time.monotonic()
        progress.radio_metrics = True

        radios, originals, folders, blocks, child_ids = {}, {}, {}, {}, {}
        released_at = {}
        active, enabled = [], []
        prepared, verified, release_failed = False, False, False
        old_pair = self.private.get("_pair")
        old_pair = dict(old_pair) if isinstance(old_pair, dict) else {}
        retry = bool(old_pair.get("verification_required"))
        try:
            progress(message=result["message"], force=True)
            if not plans or pair_id not in plans or request["catalog_digest"] != digest:
                raise ProgrammingError("Configure private channel pairs locally, then refresh and review the current catalog before changing a pair.")
            if retry and old_pair.get("catalog_digest") != digest:
                raise ProgrammingError("The pending pair belongs to a different private catalog. Restore that catalog locally before retrying.")
            try:
                qualification = read_json(self.pair_config_file, 4096, private=True, owner_uid=self.owner_uid)
            except FileNotFoundError:
                qualification = {}
            if (set(qualification) != {"schema", "enabled"} or type(qualification.get("schema")) is not int
                    or qualification["schema"] != 1 or qualification.get("enabled") is not True):
                raise ProgrammingError("Paired channel changes are not enabled. Complete the local memory-1 operating qualification first.")
            unfinished = {role for role in ROLES if isinstance(self.private.get(role), dict)
                          and self.private[role].get("verification_required")}
            if unfinished and (not retry or "programmer" in unfinished):
                raise ProgrammingError("Resolve the unfinished radio upload before changing a pair.")
            if retry and (old_pair.get("devices") != {role: config["devices"][role] for role in REPEATER_ROLES}
                          or old_pair.get("pair_id") not in plans):
                raise ProgrammingError("Restore the pending pair's local RX/TX bindings before retrying.")
            if retry and unfinished:
                previous_ids = old_pair.get("backup_ids")
                if (not isinstance(previous_ids, dict)
                        or any(self.private[role].get("program_kind") != "pair"
                               or self.private[role].get("program_id") != previous_ids.get(role)
                               for role in unfinished)):
                    raise ProgrammingError("Resolve the separate unfinished radio upload before retrying the pair change.")
            if self.service("is-active", "svxlink.service"):
                raise ProgrammingError("The vendor SvxLink service is active. Stop it locally before changing a pair.")
            active = [name for name in SERVICES if self.service("is-active", name)]
            enabled = [name for name in SERVICES if self.service("is-enabled", name)]
            for name in active:
                self.service("stop", name)
            # Capture both original images before any write. Release each
            # clone session as soon as its backup/checks finish, so that radio
            # can settle while the other radio is being read.
            for role in REPEATER_ROLES:
                progress(f"reading-{role}", f"Reading {role.upper()} before changing either radio.", True)
                radio = self.hardware.download(config["devices"][role], progress)
                radios[role] = radio
                pair_preflight(radio)
                child_id = hashlib.sha256(f"{request_id}:{role}".encode("ascii")).hexdigest()[:32]
                child_ids[role] = child_id
                progress(f"backing-up-{role}", f"Saving the {role.upper()} original backup.", True)
                folder = self.backup(radio, child_id, "original", role, config["devices"][role])
                folders[role] = folder
                originals[role] = self.hardware.load(folder / "original.img")
                if retry:
                    previous = self.private.get(role, {})
                    previous_id = previous.get("program_id")
                    saved_ids = old_pair.get("backup_ids")
                    saved_id = saved_ids.get(role) if isinstance(saved_ids, dict) else None
                    # A successful individual Verify does not resolve the pair
                    # transaction or authorize a fresh baseline for that role.
                    # Compare both bound candidates until the pair completes.
                    # A legacy release failure before candidate preparation has
                    # no saved role candidates and may take fresh originals.
                    if previous.get("verification_required") or saved_id is not None:
                        if (previous.get("device") != config["devices"][role]
                                or previous.get("program_kind") != "pair" or previous_id != saved_id
                                or not isinstance(previous_id, str) or not REQUEST_RE.fullmatch(previous_id)):
                            raise ProgrammingError("The unfinished pair candidate needs local review before replacement.")
                        expected = self.hardware.load(self.backup_dir / previous_id / "candidate.img")
                        if protected_pair_changes(expected, radio):
                            raise ProgrammingError("Protected radio data differs from the pending pair candidate. Review the private backups locally before retrying.")
                try:
                    self.hardware.release(radio)
                except Exception:
                    # A later cleanup retry must not hide an uncertain close
                    # and accidentally resume radio services.
                    release_failed = True
                    raise
                radio.pipe = None
                released_at[config["devices"][role]] = time.monotonic()
            progress("preparing", "Both original backups are saved. Preparing only working memory 1.", True)
            for role in REPEATER_ROLES:
                radio = radios[role]
                blocks[role] = self.hardware.prepare_editor(radio, [pair_memory(pair_id, role, self.catalog)], {})
                before, after = packed(originals[role]), packed(radio)
                allowed = set(range(0x10, 0x20)) | set(range(0x1010, 0x1017))
                if len(before) != len(after) or any(old != new for index, (old, new)
                                                  in enumerate(zip(before, after)) if index not in allowed):
                    raise ProgrammingError("The paired candidate changes data outside working memory 1. No upload was attempted.")
                path = folders[role] / "candidate.img"
                radio.save_mmap(str(path))
                os.chmod(path, 0o640)
                if self.group_id is not None:
                    os.chown(path, 0, self.group_id)
                sync_path(path)
                sync_path(folders[role])
            resume_services = list(dict.fromkeys([*old_pair.get("resume_services", []), *active]))
            resume_enabled = list(dict.fromkeys([*old_pair.get("resume_enabled", []), *enabled]))
            # Until this point the previous pair's candidates stay protected.
            # Both replacement originals and candidates are now durable, so
            # retaining every older failed retry would only defeat the cap.
            protected = set(child_ids.values())
            prepared = True
            self.private["_pair"] = {"pair_id": pair_id, "request_id": request_id, "catalog_digest": digest, "phase": "prepared",
                                     "stored_verified": False, "activation_required": False,
                                     "verification_required": True, "roles": {role: {"written": False, "verified": False}
                                                                               for role in REPEATER_ROLES},
                                     "devices": {role: config["devices"][role] for role in REPEATER_ROLES},
                                     "backup_ids": child_ids, "protected_backups": sorted(value for value in protected
                                                                                       if isinstance(value, str)),
                                     "resume_services": resume_services, "resume_enabled": resume_enabled}
            for role in REPEATER_ROLES:
                previous = self.private.get(role, {})
                self.private[role] = {**previous, **image_identity(radios[role]),
                                      "device": config["devices"][role], "inspected": True,
                                      "programmed": False, "verified": False, "program_kind": "pair",
                                      "backup_id": child_ids[role], "program_id": child_ids[role],
                                      "verification_required": True,
                                      "resume_services": [], "resume_enabled": []}
                self.invalidate_editor(role)
            self.publish()
            # Persist the complete transaction and both candidates first. No
            # radio write is allowed until service autostart is also disabled.
            for name in resume_enabled:
                if name in SERVICES:
                    self.service("disable", name)
            result["backup_ids"] = dict(child_ids)
            pair_state = self.private["_pair"]
            for role in REPEATER_ROLES:
                # Quiet time belongs to the physical connection being reused,
                # not the preceding role. Reading/programming its partner has
                # usually already satisfied the five-second minimum.
                elapsed = time.monotonic() - released_at[config["devices"][role]]
                remaining = min(5.0, max(0.0, 5.0 - elapsed))
                if remaining:
                    progress("settling", f"Waiting {remaining:g} quiet seconds before reconnecting to {role.upper()}.", True)
                    time.sleep(remaining)
                progress(f"reading-{role}", f"Reconnecting to {role.upper()} and checking that its saved original still matches.", True)
                fresh = self.hardware.download(config["devices"][role], progress)
                radios[role] = fresh
                before, current = packed(originals[role]), packed(fresh)
                if (len(before) != len(current) or any((old ^ new) & ~VOLATILE_MASKS.get(index, 0)
                                                      for index, (old, new) in enumerate(zip(before, current)))):
                    raise ProgrammingError(f"{role.upper()} changed after its original backup. No further upload was attempted; review the pair transaction.")
                # The original backup remains immutable. Store a fresh upload
                # base separately so volatile lock/cursor bits need no writes.
                base_path = folders[role] / "upload-base.img"
                fresh.save_mmap(str(base_path))
                os.chmod(base_path, 0o640)
                if self.group_id is not None:
                    os.chown(base_path, 0, self.group_id)
                sync_path(base_path)
                originals[role] = self.hardware.load(base_path)
                blocks[role] = self.hardware.prepare_editor(fresh, [pair_memory(pair_id, role, self.catalog)], {})
                old_candidate = self.hardware.load(folders[role] / "candidate.img")
                if any((old ^ new) & ~VOLATILE_MASKS.get(index, 0)
                       for index, (old, new) in enumerate(zip(packed(old_candidate), packed(fresh)))):
                    raise ProgrammingError("The refreshed pair candidate differs from its durable proposal. No further upload was attempted.")
                candidate_path = folders[role] / "candidate.img"
                fresh.save_mmap(str(candidate_path))
                os.chmod(candidate_path, 0o640)
                if self.group_id is not None:
                    os.chown(candidate_path, 0, self.group_id)
                sync_path(candidate_path)
                sync_path(folders[role])
                pair_state["phase"] = f"writing-{role}"
                self.publish()
                progress(f"writing-{role}", f"Writing {role.upper()} working memory 1. Radio services remain paused.", True)

                def before_write(selected=role):
                    pair_state["roles"][selected]["written"] = True
                    self.publish()

                self.hardware.upload(config["devices"][role], originals[role], radios[role],
                                     blocks[role], progress, before_write, editor=True)
                pair_state["phase"] = f"verifying-{role}"
                self.publish()
                progress(f"verifying-{role}", f"Reading back {role.upper()} to verify its stored settings.", True)
                observed = self.hardware.settle_and_verify(config["devices"][role], radios[role], progress)
                self.save_observed(observed, folders[role])
                verify_images(originals[role], radios[role], observed)
                self.private[role].update(programmed=True, verified=True, verification_required=False,
                                          verification_message="")
                pair_state["roles"][role]["verified"] = True
                self.publish()
            pair_state.update(phase="configured", stored_verified=True, verification_required=False,
                              configured_at=datetime.now(timezone.utc).isoformat(), protected_backups=[])
            self.publish()
            verified = True
            result.update(success=True, phase="configured", verification_required=False,
                          message="Both radios' working memory 1 settings are programmed and verified. The configured pair is recorded; this is not live radio telemetry.")
        except Exception as exc:
            message = str(exc) if isinstance(exc, ProgrammingError) else "The paired operation did not complete. Check both radios' power and cable connections."
            if prepared:
                self.private["_pair"].update(phase="failed", stored_verified=False, verification_required=True)
                try:
                    self.publish()
                except Exception:
                    message += " Check the private transaction state locally before restarting services."
            pending = prepared or retry
            if pending:
                message += " Radio services remain paused. Retry the reviewed pair change to reconnect, or resolve the pending transaction locally. If a radio cannot reconnect or stays unresponsive, power it off and on before retrying."
            result.update(success=False, phase="failed", verification_required=pending,
                          message=message, progress=None)
        finally:
            # Neither role may resume audio while the other serial session is
            # still in cloning mode, even after both stored images verified.
            for radio in radios.values():
                try:
                    self.hardware.release(radio)
                except Exception:
                    release_failed = True
            if release_failed:
                verified = False
                if not prepared:
                    self.private["_pair"] = {**old_pair, "pair_id": pair_id, "request_id": request_id, "catalog_digest": digest,
                                             "roles": old_pair.get("roles", {}),
                                             "devices": {role: config["devices"][role] for role in REPEATER_ROLES},
                                             "backup_ids": old_pair.get("backup_ids", {}),
                                             "protected_backups": list(dict.fromkeys([
                                                 *old_pair.get("protected_backups", []), *child_ids.values()])),
                                             "resume_services": list(dict.fromkeys([
                                                 *old_pair.get("resume_services", []), *active])),
                                             "resume_enabled": list(dict.fromkeys([
                                                 *old_pair.get("resume_enabled", []), *enabled]))}
                self.private["_pair"].update(phase="failed", stored_verified=False,
                                             activation_required=False, verification_required=True)
                self.publish()
                for name in self.private["_pair"].get("resume_enabled", []):
                    if name in SERVICES:
                        self.service("disable", name)
                result.update(success=False, phase="failed", verification_required=True,
                              message="A radio programming connection did not close. Disconnect and power-cycle both radios; services remain paused.")
            any_unverified = any(isinstance(item, dict) and item.get("verification_required")
                                 for item in self.private.values())
            if not any_unverified and (verified or not prepared):
                try:
                    for name in (self.private["_pair"]["resume_enabled"] if verified else enabled):
                        if name in SERVICES:
                            self.service("enable", name)
                    for name in (self.private["_pair"]["resume_services"] if verified else active):
                        if name in SERVICES:
                            self.service("start", name)
                    if verified:
                        self.private["_pair"].update(resume_services=[], resume_enabled=[])
                        self.publish()
                except Exception:
                    result["message"] += " Radio services could not resume; check their status locally."
            try:
                self.prune_backups()
            except OSError:
                result["message"] += " Old backup cleanup could not finish; check backup storage locally."
            if result["success"] is True:
                result["progress"] = {"current": 1, "total": 1, "percent": 100, "unit": "steps"}
            progress(message=result["message"], force=True)
        return result

    def run(self, request, config):
        if not valid_request(request) or not valid_config(config):
            raise ProgrammingError("The radio request or configuration is invalid.")
        if request["action"] == "change-pair":
            return self.run_pair(request, config)
        role, action, request_id = request["role"], request["action"], request["request_id"]
        if role not in config["devices"]:
            raise ProgrammingError("Configure this programming connection locally before using it.")
        previous = dict(self.private.get(role, {}))
        device = config["devices"][role]
        model = "5RX"
        if role == "programmer":
            if action == "editor-read":
                model = request.get("model", "5RX")
            elif action == "editor-write":
                model = previous.get("editor_model", "5RX")
            else:
                model = previous.get("program_model", "5RX")
        if model not in MODELS:
            raise ProgrammingError("The saved radio model needs local review before continuing.")
        result = {"request_id": request_id, "role": role, "action": action,
                  "success": None, "phase": "preflight", "message": "Checking the configured radio connection.",
                  "backup_id": None, "verification_required": False, "progress": None, "events": []}
        last_progress = 0

        def progress(phase=None, message=None, force=False, current=None, total=None, unit="blocks"):
            nonlocal last_progress
            previous_phase = result["phase"]
            if phase:
                result["phase"] = phase
                if phase != previous_phase:
                    result["progress"] = None
            if message:
                result["message"] = message
                result["events"] = [*result["events"][-19:],
                                    {"time": datetime.now(timezone.utc).isoformat(), "message": message[:1000]}]
            if type(current) is int and type(total) is int and 0 <= current <= total <= 1_000_000 and total:
                result["progress"] = {"current": current, "total": total,
                                      "percent": min(100, current * 100 // total),
                                      "unit": unit if unit in ("blocks", "steps") else "blocks"}
            if force or time.monotonic() - last_progress >= 0.5:
                atomic_json(self.state_dir / "radio-programming-result.json", result, self.group_id)
                last_progress = time.monotonic()
        progress.radio_metrics = True

        active, enabled, write_started, verified, radio = [], [], False, False, None
        verify_attempted, recovery_write, replacement_ready = False, False, False
        saved_repair, repair_metadata = None, {}
        try:
            progress(message=result["message"], force=True)
            if action == "program":
                if self.catalog is None or request["catalog_digest"] != catalog_digest(self.catalog):
                    raise ProgrammingError("Configure private channel pairs locally, then refresh and review the current catalog before default programming.")
                self.hardware.catalog = self.catalog
            if (role == "programmer" and previous.get("verification_required")
                    and (model != previous.get("program_model", "5RX")
                         or previous.get("selected_model", "5RX") != previous.get("program_model", "5RX"))):
                raise ProgrammingError("Resolve the unfinished upload with its saved radio model before selecting a different profile.")
            if (role == "programmer" and action == "editor-read" and not previous.get("verification_required")
                    and model != previous.get("selected_model", "5RX")):
                # A profile change invalidates old candidates; it cannot erase an
                # unfinished upload, and cannot reuse images decoded by another driver.
                self.invalidate_editor(role)
                previous = {key: value for key, value in previous.items() if key in ("resume_services", "resume_enabled")}
                self.private[role] = previous
                self.publish()
            if hasattr(self.hardware, "select_model"):
                self.hardware.select_model(model)
            elif model != "5RX":
                raise ProgrammingError("The selected radio profile is unavailable.")
            if action in ("inspect", "program") and any(item.get("verification_required") for item in self.private.values() if isinstance(item, dict)):
                raise ProgrammingError("Verify the unfinished upload before inspecting or programming either radio.")
            if previous.get("verification_required") and previous.get("device") != device:
                raise ProgrammingError("The configured connection differs from the unfinished upload. Restore its local binding before continuing.")
            if action == "repair-upload":
                progress("repair-preflight", "Checking the protected saved candidate and its affected operating pages.", True)
                saved_repair, repair_metadata = self.load_saved_repair(previous, role, device, request["pending_id"])
                recovery_write = True
            editor_base = None
            if action == "editor-write":
                base_id = previous.get("editor_base_id")
                if (previous.get("editor_model", "5RX") != model
                        or request["base_id"] != base_id or not isinstance(base_id, str)
                        or not REQUEST_RE.fullmatch(base_id) or previous.get("editor_device") != device
                        or previous.get("editor_image_kind") not in ("original", "verified")):
                    raise ProgrammingError("Read this radio in the editor again before applying changes.")
                base_path = self.backup_dir / base_id / (previous["editor_image_kind"] + ".img")
                editor_base = self.hardware.load(base_path)
                if hashlib.sha256(packed(editor_base)).hexdigest() != previous.get("editor_image_sha256"):
                    raise ProgrammingError("The private editor base failed its integrity check. Read the radio again.")
                recovery_write = bool(previous.get("verification_required"))
                if recovery_write:
                    if (not isinstance(previous.get("program_id"), str)
                            or request.get("supersede_id") != previous["program_id"]
                            or previous.get("editor_pending_id") != previous["program_id"]):
                        raise ProgrammingError("Review a fresh recovery read and explicitly confirm replacing this unfinished candidate.")
                elif "supersede_id" in request:
                    raise ProgrammingError("The recovery candidate changed. Read this radio again before applying changes.")
            if action in ("inspect", "program", "editor-read", "editor-write", "repair-upload"):
                self.invalidate_editor(role)
                for name in ("editor_base_id", "editor_image_kind", "editor_image_sha256", "editor_device", "editor_pending_id", "editor_model"):
                    previous.pop(name, None)
            if action == "program" and (not previous.get("inspected") or not previous.get("compatible")):
                raise ProgrammingError("Inspect this radio before programming it.")
            if action == "program" and previous.get("verification_required"):
                raise ProgrammingError("Verify the unfinished upload before programming again.")
            if action == "inspect" and previous.get("verification_required"):
                raise ProgrammingError("Verify the unfinished upload first; a new inspection would lose the pending comparison.")
            if action not in ("inspect", "editor-read") and previous.get("device") != device:
                raise ProgrammingError("The configured AIOC changed. Inspect the attached radio again.")
            if self.service("is-active", "svxlink.service"):
                raise ProgrammingError("The vendor SvxLink service is active. Stop it locally before radio programming.")
            active = [name for name in SERVICES if self.service("is-active", name)]
            for name in active:
                self.service("stop", name)
            progress("reading", "Reading the attached radio. Keep its cable connected and power on.", True)
            radio = self.hardware.download(device, progress)
            identity = image_identity(radio)
            progress("backing-up", "Saving the local radio backup.", True)
            folder = self.backup(radio, request_id, "original", role, device)
            result["backup_id"] = request_id
            if action == "editor-read":
                if not identity["compatible"]:
                    raise ProgrammingError("Read-only backup saved. This revision needs a profile review before using the editor.")
                self.private[role] = {**previous, "device": device, "inspected": True,
                                      "backup_id": request_id, **identity}
                self.publish_editor(radio, role, request_id, device)
                result.update(success=True, phase="editor-ready",
                              verification_required=bool(previous.get("verification_required")),
                              message=("Recovery read saved. The unfinished candidate is preserved and radio services remain paused. Review the current values before confirming a replacement."
                                       if previous.get("verification_required") else
                                       "Radio memories and operating settings read. Review your changes before writing; the worker reconnects automatically."))
                verified = True
            elif action == "inspect":
                self.private[role] = {"device": device, "inspected": True,
                                      "programmed": False, "verified": False,
                                      "verification_required": False, "backup_id": request_id,
                                      **identity}
                self.publish()
                message = ("Read-only backup saved. The selected 5RX profile matches the reviewed revision. Ready to program; the worker reconnects automatically."
                           if identity["compatible"] else
                           "Read-only backup saved. This revision needs a profile review before automatic programming; no radio settings were changed.")
                result.update(success=True, phase="inspected", message=message)
                verified = True  # Read-only work may safely restore previous services.
            elif action == "verify":
                program_id = previous.get("program_id")
                if not isinstance(program_id, str) or len(program_id) != 32 or any(c not in "0123456789abcdef" for c in program_id):
                    raise ProgrammingError("No saved candidate exists for this radio. Inspect it locally before continuing.")
                original = self.hardware.load(self.backup_dir / program_id / "original.img")
                candidate = self.hardware.load(self.backup_dir / program_id / "candidate.img")
                self.save_observed(radio, self.backup_dir / program_id)
                verify_attempted = True
                verify_images(original, candidate, radio)
                self.private[role] = {**previous, "verification_required": False, "verified": True,
                                      "verification_message": ""}
                if previous.get("program_kind") == "editor":
                    verified_path = self.backup_dir / program_id / "verified.img"
                    radio.save_mmap(str(verified_path))
                    os.chmod(verified_path, 0o640)
                    if self.group_id is not None:
                        os.chown(verified_path, 0, self.group_id)
                    sync_path(verified_path)
                    sync_path(verified_path.parent)
                    self.publish_editor(radio, role, program_id, device, "verified")
                self.publish()
                result.update(success=True, phase="verified", message="The programmed channel and global settings verified. Protected radio data matches the saved candidate. Select memory 1 on the radio manually; its current-channel cursor is managed by the firmware.")
                verified = True
            else:
                if not same_identity(previous, identity) or not identity["compatible"]:
                    raise ProgrammingError("The current identity differs from the inspected radio revision. Inspect it again before programming.")
                original = self.hardware.load(folder / "original.img")
                if action == "repair-upload":
                    current_pending = self.private.get(role, {})
                    if (current_pending.get("program_id") != request["pending_id"]
                            or current_pending.get("verification_required") is not True
                            or current_pending.get("device") != device):
                        raise ProgrammingError("The unfinished upload changed during the repair read. Refresh and review it again.")
                    progress("preparing", "Restoring only the saved candidate's affected operating pages; current runtime bits are preserved.", True)
                    blocks = prepare_saved_repair(radio, *saved_repair,
                                                  editor=repair_metadata["repair_source_kind"] == "editor")
                elif action == "editor-write":
                    if (len(packed(editor_base)) != len(packed(radio))
                            or any((old ^ new) & ~profile_volatile(radio).get(index, 0)
                                   for index, (old, new) in enumerate(zip(packed(editor_base), packed(radio))))):
                        raise ProgrammingError("The radio changed since the editor read. Read it again and review your changes; no upload was attempted.")
                    if recovery_write:
                        old_candidate = self.hardware.load(self.backup_dir / previous["program_id"] / "candidate.img")
                        if protected_editor_changes(old_candidate, radio):
                            raise ProgrammingError("Protected radio data differs from the unfinished candidate. Review the private backups locally before writing or accepting current values.")
                    progress("preparing", "Validating the requested memories and operating settings.", True)
                    blocks = (self.hardware.prepare_editor(radio, request["changes"], request["settings"])
                              if request["changes"] or request["settings"] else [])
                    if not request["changes"] and not request["settings"] and not recovery_write:
                        raise ProgrammingError("There are no radio changes to apply.")
                else:
                    blocks = self.hardware.prepare(radio, role)
                candidate_path = folder / "candidate.img"
                radio.save_mmap(str(candidate_path))
                os.chmod(candidate_path, 0o640)
                if self.group_id is not None:
                    os.chown(candidate_path, 0, self.group_id)
                sync_path(candidate_path)
                sync_path(folder)
                if action == "repair-upload":
                    repair_metadata["repair_candidate_sha256"] = hashlib.sha256(candidate_path.read_bytes()).hexdigest()
                    manifest = read_json(folder / "manifest.json", private=True, owner_uid=self.owner_uid)
                    atomic_json(folder / "manifest.json", {**manifest, "repair": {
                        **repair_metadata, "superseded_program_id": request["pending_id"],
                        "pages": saved_repair_pages(*saved_repair,
                                                    editor=repair_metadata["repair_source_kind"] == "editor")}}, self.group_id)
                self.private[role] = {**previous, **identity, "device": device,
                                      "backup_id": request_id, "program_id": request_id, "program_model": model,
                                      "resume_services": list(dict.fromkeys([*previous.get("resume_services", []), *active])),
                                      "resume_enabled": previous.get("resume_enabled", []),
                                      "programmed": False, "verified": False,
                                      "program_kind": (previous["program_kind"] if action == "repair-upload"
                                                       else "editor" if action == "editor-write" else "repeater"),
                                      "verification_required": recovery_write,
                                      "superseded_program_id": previous.get("program_id") if recovery_write else None}
                for name in ("repair_source_id", "repair_source_kind", "repair_source_original_sha256",
                             "repair_source_candidate_sha256", "repair_candidate_sha256"):
                    self.private[role].pop(name, None)
                self.private[role].update(repair_metadata)
                self.publish()
                replacement_ready = True

                def before_write():
                    nonlocal write_started, enabled
                    self.invalidate_pair(role)
                    enabled = [name for name in SERVICES if self.service("is-enabled", name)]
                    self.private[role]["resume_enabled"] = list(dict.fromkeys([
                        *self.private[role].get("resume_enabled", []), *enabled]))
                    self.publish()
                    for name in enabled:
                        self.service("disable", name)
                    write_started = True
                    self.private[role]["verification_required"] = True
                    self.publish()

                progress("writing", ("Replaying the protected saved candidate with both halves of each affected operating page."
                                     if action == "repair-upload" else
                                     "Writing complete operating pages containing the reviewed channel and global setting changes."), True)
                if action == "repair-upload":
                    self.hardware.upload(device, original, radio, blocks, progress, before_write,
                                         editor=repair_metadata["repair_source_kind"] == "editor",
                                         saved_repair=saved_repair)
                elif action == "editor-write":
                    self.hardware.upload(device, original, radio, blocks, progress, before_write, editor=True)
                else:
                    self.hardware.upload(device, original, radio, blocks, progress, before_write)
                self.private[role]["programmed"] = True
                self.private[role]["verification_required"] = True
                self.publish()
                progress("verifying", "Reading the radio again to verify the result.", True)
                observed = self.hardware.settle_and_verify(device, radio, progress) if blocks else original
                self.save_observed(observed, folder)
                verify_images(original, radio, observed)
                self.private[role].update(verification_required=False, verified=True, verification_message="")
                if action == "editor-write" or action == "repair-upload" and previous["program_kind"] == "editor":
                    verified_path = folder / "verified.img"
                    observed.save_mmap(str(verified_path))
                    os.chmod(verified_path, 0o640)
                    if self.group_id is not None:
                        os.chown(verified_path, 0, self.group_id)
                    sync_path(verified_path)
                    sync_path(folder)
                    self.publish_editor(observed, role, request_id, device, "verified")
                self.publish()
                result.update(success=True, phase="verified", message=(
                    ("The saved upload was repaired and verified. Unrelated operating pages and protected radio data match the fresh backup."
                     if action == "repair-upload" else
                     "Reviewed current values accepted and verified. No radio bytes were written."
                     if recovery_write and not blocks else
                     "Radio programmed and verified. Unrelated channels and calibration data match the original backup.")
                    + (" Select memory 1 on the radio manually; its current-channel cursor is managed by the firmware."
                       if action == "program" else "")))
                verified = True
        except Exception as exc:
            needs_verification = write_started or bool(previous.get("verification_required")) or verify_attempted
            # Never expose driver exceptions, which may include image bytes or device IDs.
            message = str(exc) if isinstance(exc, ProgrammingError) else "The radio did not complete the operation. Check its power, cable fit, and selected model."
            if needs_verification and (write_started or verify_attempted or replacement_ready):
                if verify_attempted:
                    self.invalidate_pair(role)
                state = self.private.get(role, previous)
                state.update(verified=False, verification_required=True, verification_message=message[:1000],
                             resume_services=list(dict.fromkeys([*state.get("resume_services", []), *active])))
                self.private[role] = state
                try:
                    still_enabled = [name for name in SERVICES if self.service("is-enabled", name)]
                    state["resume_enabled"] = list(dict.fromkeys([*state.get("resume_enabled", []), *still_enabled]))
                    self.publish()
                    for name in still_enabled:
                        self.service("disable", name)
                except Exception:
                    message += " Check the radio service state locally before restarting the Pi."
            if needs_verification:
                message += " Radio services remain paused. Use Verify to reconnect and check this radio, or read it in the editor to review a recovery. If it cannot reconnect or stays unresponsive, power it off and on before retrying."
            result.update(success=False, phase="verification-required" if needs_verification else "failed",
                          verification_required=bool(needs_verification), message=message, progress=None)
        finally:
            try:
                self.hardware.release(radio)
            except Exception:
                result["message"] += " Disconnect and power-cycle the radio to release its programming connection."
            any_unverified = any(item.get("verification_required") for item in self.private.values() if isinstance(item, dict))
            should_resume = not any_unverified and (verified or not write_started)
            if should_resume:
                resume = active
                completed_maintenance = verified and action in ("program", "verify", "editor-write", "repair-upload")
                saved_roles = [state for state in self.private.values() if isinstance(state, dict)]
                if completed_maintenance:
                    resume = list(dict.fromkeys([*active, *(name for state in saved_roles for name in state.get("resume_services", []))]))
                try:
                    restore_enabled = list(dict.fromkeys([*enabled, *(name for state in saved_roles for name in state.get("resume_enabled", []))])) if completed_maintenance else enabled
                    for name in restore_enabled:
                        if name in SERVICES:
                            self.service("enable", name)
                    for name in resume:
                        if name in SERVICES:
                            self.service("start", name)
                    if completed_maintenance:
                        for state in saved_roles:
                            state["resume_services"] = []
                            state["resume_enabled"] = []
                        self.publish()
                except Exception:
                    result["message"] += " The radio service did not resume; check its service status locally."
            try:
                self.prune_backups()
            except OSError:
                result["message"] += " Old backup cleanup could not finish; check backup storage locally."
            if result["success"] is True:
                result["progress"] = {"current": 1, "total": 1, "percent": 100, "unit": "steps"}
            progress(message=result["message"], force=True)
        return result


def main():
    if os.geteuid() != 0:
        print("Run this worker through its root systemd service.", file=sys.stderr)
        return 1
    request = None
    lock_fd = None
    group_id = grp.getgrnam("repeaterdash").gr_gid
    try:
        request = read_json(STATE_DIR / "radio-programming-request.json", 65536)
        if not valid_request(request):
            raise ProgrammingError("The radio request is invalid.")
        backup_info = BACKUP_DIR.lstat()
        if not stat.S_ISDIR(backup_info.st_mode) or backup_info.st_uid != 0 or backup_info.st_mode & 0o022:
            raise ProgrammingError("The private radio backup directory permissions are invalid.")
        lock_fd = os.open(LOCK_PATH, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
        lock_info = os.fstat(lock_fd)
        if not stat.S_ISREG(lock_info.st_mode) or lock_info.st_uid != 0 or lock_info.st_mode & 0o077:
            raise ProgrammingError("The maintenance lock permissions are invalid.")
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ProgrammingError("Another maintenance operation is running. Wait for it to finish, then retry.") from exc
        consumed_path = BACKUP_DIR / "consumed.json"
        try:
            consumed = read_json(consumed_path, private=True)
        except FileNotFoundError:
            consumed = {"requests": []}
        if request["request_id"] in consumed.get("requests", []):
            return 0
        # Mark before any hardware work. A failed or interrupted upload is never replayed.
        atomic_json(consumed_path, {"requests": [*consumed.get("requests", [])[-255:], request["request_id"]]}, group_id)
        config = read_json(CONFIG_PATH, 4096, private=True)
        if not valid_config(config):
            raise ProgrammingError("Configure the fixed RX/TX AIOC bindings locally before programming.")
        selected_roles = REPEATER_ROLES if request["action"] == "change-pair" else (request["role"],)
        for role in selected_roles:
            selected = config["devices"].get(role)
            if selected is None or not stat.S_ISCHR(os.stat(selected).st_mode):
                raise ProgrammingError("The selected programming connection is not present.")
        connected = [device for device in config["devices"].values() if os.path.exists(device)]
        if any(os.path.samefile(first, second) for index, first in enumerate(connected)
               for second in connected[index + 1:]):
            raise ProgrammingError("Configured radio connections must use different serial devices.")
        worker = Worker(Hardware(), group_id=group_id)
        worker.run(request, config)
        return 0
    except Exception as exc:
        if request is not None and valid_request(request):
            message = str(exc) if isinstance(exc, ProgrammingError) else "The radio helper could not start. Check the local configuration and installed runtime."
            atomic_json(STATE_DIR / "radio-programming-result.json", {"request_id": request["request_id"],
                        "role": request.get("role", "pair"), "action": request["action"], "success": False,
                        "phase": "failed", "message": message, "verification_required": False}, group_id)
        return 1
    finally:
        if lock_fd is not None:
            os.close(lock_fd)


if __name__ == "__main__":
    raise SystemExit(main())
