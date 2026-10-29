"""Bounded browser data for the optional, authenticated radio editor.

Device paths, image files and driver selection stay in the privileged worker.
This module accepts channel values only and exposes an explicit snapshot schema.
The worker independently validates every value against the downloaded radio.
"""
import copy
import math
import re

TOKEN_RE = re.compile(r"[0-9a-f]{32}\Z")
MODELS = ("5RX", "UV5R")
SETTING_KEYS = frozenset((
    "settings.squelch", "settings.save", "settings.vox", "settings.tdr",
    "settings.beep", "settings.timeout", "settings.voice", "settings.roger",
    "settings.pttid", "settings.mdfa", "settings.mdfb", "settings.workmode",
    "settings.displayab", "settings.autolk",
))
CHANNEL_KEYS = frozenset(("number", "empty", "name", "rx_hz", "tx_hz", "mode",
                          "power", "skip", "tx_tone", "rx_tone"))


def bounded_number(value, minimum, maximum):
    return (type(value) in (int, float) and minimum <= value <= maximum
            and math.isfinite(value))


def valid_tone(value):
    if value is None:
        return True
    if not isinstance(value, dict):
        return False
    if value.get("type") == "CTCSS":
        return (set(value) == {"type", "value"}
                and bounded_number(value.get("value"), 60, 260))
    return (set(value) == {"type", "value", "polarity"}
            and value.get("type") == "DCS" and type(value.get("value")) is int
            and 0 <= value["value"] <= 777 and value.get("polarity") in ("N", "R"))


def valid_channel(value):
    if (not isinstance(value, dict) or type(value.get("number")) is not int
            or not 0 <= value["number"] <= 127 or type(value.get("empty")) is not bool):
        return False
    if value["empty"]:
        return set(value) == {"number", "empty"}
    return (set(value) == CHANNEL_KEYS
            and isinstance(value["name"], str) and len(value["name"]) <= 7
            and all(32 <= ord(letter) <= 126 for letter in value["name"])
            and type(value["rx_hz"]) is int and 100000000 <= value["rx_hz"] <= 600000000
            and (value["tx_hz"] is None or type(value["tx_hz"]) is int
                 and 100000000 <= value["tx_hz"] <= 600000000)
            and value["mode"] in ("FM", "NFM") and value["power"] in ("High", "Low")
            and type(value["skip"]) is bool
            and valid_tone(value["tx_tone"]) and valid_tone(value["rx_tone"]))


def valid_settings(value):
    return (isinstance(value, dict) and set(value) <= SETTING_KEYS
            and all(type(item) in (bool, int, str) and
                    (len(item) <= 80 if isinstance(item, str) else
                     -1 <= item <= 255 if type(item) is int else True)
                    for item in value.values()))


def valid_write(base_id, changes, settings, supersede_id=None):
    return (isinstance(base_id, str) and bool(TOKEN_RE.fullmatch(base_id))
            and (supersede_id is None or isinstance(supersede_id, str)
                 and bool(TOKEN_RE.fullmatch(supersede_id)))
            and isinstance(changes, list) and len(changes) <= 128
            and all(valid_channel(row) for row in changes)
            and len({row["number"] for row in changes}) == len(changes)
            and valid_settings(settings) and bool(changes or settings or supersede_id))


def public_snapshot(value, role):
    """Return a complete safe snapshot, or None for missing/invalid worker data."""
    if (not isinstance(value, dict) or type(value.get("schema")) is not int or value["schema"] != 1
            or value.get("role") != role or value.get("model") not in MODELS
            or role != "programmer" and value.get("model") != "5RX"
            or not isinstance(value.get("base_id"), str)
            or not TOKEN_RE.fullmatch(value["base_id"])
            or not isinstance(value.get("read_at"), str) or len(value["read_at"]) > 80
            or not isinstance(value.get("firmware"), str) or len(value["firmware"]) > 80
            or type(value.get("compatible")) is not bool
            or not isinstance(value.get("memories"), list) or len(value["memories"]) != 128
            or not all(valid_channel(row) for row in value["memories"])
            or {row["number"] for row in value["memories"]} != set(range(128))
            or not valid_settings(value.get("settings"))
            or value.get("model") == "UV5R" and "settings.pttid" in value.get("settings", {})):
        return None
    capabilities = value.get("capabilities")
    if not isinstance(capabilities, dict):
        return None
    clean_caps = {}
    for key in ("bands_hz", "tx_bands_hz"):
        bands = capabilities.get(key)
        if (not isinstance(bands, list) or len(bands) > 16
                or not all(isinstance(band, list) and len(band) == 2
                           and all(type(number) is int and 0 <= number <= 1000000000
                                   for number in band) and band[0] < band[1]
                           for band in bands)):
            return None
        clean_caps[key] = bands
    if (type(capabilities.get("name_length")) is not int or capabilities["name_length"] != 7
            or not isinstance(capabilities.get("valid_characters"), str)
            or len(capabilities["valid_characters"]) > 256
            or not isinstance(capabilities.get("ctcss"), list) or len(capabilities["ctcss"]) > 64
            or not all(bounded_number(item, 60, 260) for item in capabilities["ctcss"])
            or not isinstance(capabilities.get("dcs"), list) or len(capabilities["dcs"]) > 128
            or not all(type(item) is int and 0 <= item <= 777 for item in capabilities["dcs"])):
        return None
    for key in ("name_length", "valid_characters", "ctcss", "dcs"):
        clean_caps[key] = capabilities[key]
    descriptions = capabilities.get("settings")
    model_settings = SETTING_KEYS - {"settings.pttid"} if value["model"] == "UV5R" else SETTING_KEYS
    if not isinstance(descriptions, dict) or not set(descriptions) <= model_settings:
        return None
    clean_descriptions = {}
    for key, description in descriptions.items():
        if (not isinstance(description, dict) or not isinstance(description.get("label"), str)
                or len(description["label"]) > 100 or description.get("type") not in ("boolean", "integer", "select")):
            return None
        clean = {"label": description["label"], "type": description["type"]}
        if description["type"] == "select":
            options = description.get("options")
            if (not isinstance(options, list) or len(options) > 256
                    or not all(isinstance(option, str) and len(option) <= 80 for option in options)):
                return None
            clean["options"] = options
        if description["type"] == "integer":
            if (type(description.get("min")) is not int or type(description.get("max")) is not int
                    or not -1 <= description["min"] <= description["max"] <= 255):
                return None
            clean.update(min=description["min"], max=description["max"])
        clean_descriptions[key] = clean
    clean_caps["settings"] = clean_descriptions
    clean = {key: value[key] for key in ("schema", "role", "base_id", "read_at", "model",
                                          "firmware", "compatible", "memories", "settings")}
    clean["capabilities"] = clean_caps
    recovery = value.get("recovery", {"required": False, "pending_id": None})
    if (not isinstance(recovery, dict) or type(recovery.get("required")) is not bool
            or (recovery["required"] and (not isinstance(recovery.get("pending_id"), str)
                or not TOKEN_RE.fullmatch(recovery["pending_id"])))
            or (not recovery["required"] and recovery.get("pending_id") is not None)
            or (recovery["required"] and type(recovery.get("protected_mismatch")) is not bool)):
        return None
    clean["recovery"] = {"required": recovery["required"], "pending_id": recovery.get("pending_id"),
                         "protected_mismatch": recovery.get("protected_mismatch") is True}
    if isinstance(recovery.get("message"), str):
        clean["recovery"]["message"] = recovery["message"][:1000]
    if isinstance(recovery.get("differences"), list):
        clean["recovery"]["differences"] = [
            {"field": item["field"][:100], "expected": item["expected"][:160], "actual": item["actual"][:160]}
            for item in recovery["differences"][:20] if isinstance(item, dict)
            and all(isinstance(item.get(key), str) for key in ("field", "expected", "actual"))]
    return copy.deepcopy(clean)
