"""Pure validation and presentation of a Pi's private channel-pair catalog.

There is deliberately no built-in channel-pair catalog. Each station supplies
its own frequencies through the local terminal configuration command.
"""
import hashlib
import json
import math
import re

PAIR_ID_RE = re.compile(r"[a-z][a-z0-9_-]{0,15}\Z")
DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
CTCSS_TONES = (67.0, 69.3, 71.9, 74.4, 77.0, 79.7, 82.5, 85.4, 88.5, 91.5,
               94.8, 97.4, 100.0, 103.5, 107.2, 110.9, 114.8, 118.8, 123.0,
               127.3, 131.8, 136.5, 141.3, 146.2, 151.4, 156.7, 159.8, 162.2,
               165.5, 167.9, 171.3, 173.8, 177.3, 179.9, 183.5, 186.2, 189.9,
               192.8, 196.6, 199.5, 203.5, 206.5, 210.7, 218.1, 225.7, 229.1,
               233.6, 241.8, 250.3, 254.1)


def validate_catalog(value):
    """Return only the canonical persisted schema, or raise ValueError."""
    if (not isinstance(value, dict) or set(value) != {"schema", "default_pair_id", "pairs"}
            or type(value.get("schema")) is not int or value["schema"] != 1
            or not isinstance(value.get("default_pair_id"), str)
            or not PAIR_ID_RE.fullmatch(value["default_pair_id"])
            or not isinstance(value.get("pairs"), list) or not 1 <= len(value["pairs"]) <= 9):
        raise ValueError("Configure one to nine private channel pairs using the local terminal command.")
    pairs, seen = [], set()
    for item in value["pairs"]:
        if (not isinstance(item, dict) or set(item) != {"id", "label", "rx_hz", "tx_hz", "tone_hz"}
                or not isinstance(item.get("id"), str) or not PAIR_ID_RE.fullmatch(item["id"])
                or item["id"] in seen or not isinstance(item.get("label"), str)
                or not 1 <= len(item["label"]) <= 80 or item["label"] != item["label"].strip()
                or any(not 32 <= ord(char) <= 126 for char in item["label"])
                or type(item.get("rx_hz")) is not int or item["rx_hz"] % 10
                or not 144000000 <= item["rx_hz"] < 148000000
                or type(item.get("tx_hz")) is not int or item["tx_hz"] % 10
                or not 420000000 <= item["tx_hz"] < 450000000):
            raise ValueError("Each pair needs a unique ID, printable label, VHF input and UHF output in the supported amateur bands, in 10 Hz increments.")
        tone = item["tone_hz"]
        if tone is not None and (type(tone) not in (int, float) or not math.isfinite(tone) or tone not in CTCSS_TONES):
            raise ValueError("Choose no tone or one of the standard CTCSS tones.")
        seen.add(item["id"])
        pairs.append({"id": item["id"], "label": item["label"], "rx_hz": item["rx_hz"],
                      "tx_hz": item["tx_hz"], "tone_hz": float(tone) if tone is not None else None})
    if value["default_pair_id"] != pairs[0]["id"]:
        raise ValueError("The first pair must be the default pair so its radios use BOX-RX1 and BOX-TX1.")
    return {"schema": 1, "default_pair_id": pairs[0]["id"], "pairs": pairs}


def catalog_digest(catalog):
    canonical = validate_catalog(catalog)
    return hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")).hexdigest()


def pair_plans(catalog):
    canonical = validate_catalog(catalog)
    return {item["id"]: {**item, "index": index, "name": item["label"],
                          "rx_name": f"BOX-RX{index}", "tx_name": f"BOX-TX{index}",
                          "mode": "NFM", "power": "High"}
            for index, item in enumerate(canonical["pairs"], 1)}


def default_plan(catalog):
    canonical = validate_catalog(catalog)
    pair = pair_plans(canonical)[canonical["default_pair_id"]]
    return {
        "rx": {"memory": 1, "name": pair["rx_name"], "frequency_mhz": f'{pair["rx_hz"] / 1000000:.6f}',
               "receive_tone_hz": pair["tone_hz"], "transmit_tone_hz": None,
               "transmit": False, "power": "High", "mode": "NFM"},
        "tx": {"memory": 1, "name": pair["tx_name"], "frequency_mhz": f'{pair["tx_hz"] / 1000000:.6f}',
               "receive_tone_hz": None, "transmit_tone_hz": pair["tone_hz"],
               "transmit": True, "power": "High", "mode": "NFM"},
    }
