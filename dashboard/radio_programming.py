"""Queue narrowly scoped radio work for the separate privileged worker.

The browser selects a role and operation, plus a fixed model choice for reads
through the external programmer. Device identities and driver implementations
remain local; writes are bound to the model in the worker's private read token.
"""
import copy
import importlib.util
import json
import os
import stat
import re
import secrets
import threading
import time
from pathlib import Path

from controls import write_json
from software_updates import read_json
from wifi_visibility import command, is_pi
from radio_editor import MODELS, public_snapshot, valid_write

REQUEST_RE = re.compile(r"[0-9a-f]{32}\Z")
DEVICE_RE = re.compile(r"/dev/serial/by-id/usb-AIOC_All-In-One-Cable_[A-Za-z0-9_-]{1,64}-if04\Z")
PROGRAMMER_RE = re.compile(r"/dev/serial/(?:by-id|by-path)/[A-Za-z0-9_.:-]{1,200}\Z")
ACTIONS = ("inspect", "program", "verify", "editor-read", "editor-write", "repair-upload")
STATION_ROLES = ("rx", "tx")
ROLES = (*STATION_ROLES, "programmer")
SUPPORTED_MODELS = {"rx": ["5RX"], "tx": ["5RX"], "programmer": list(MODELS)}
_catalog_spec = importlib.util.spec_from_file_location(
    "repeater_channel_catalog", Path(__file__).resolve().parents[1] / "scripts" / "radio_plan.py")
_catalog_module = importlib.util.module_from_spec(_catalog_spec)
_catalog_spec.loader.exec_module(_catalog_module)
validate_catalog = _catalog_module.validate_catalog
catalog_digest = _catalog_module.catalog_digest
pair_plans = _catalog_module.pair_plans
default_plan = _catalog_module.default_plan
CATALOG_MESSAGE = "Configure channel pairs locally on the Pi before programming a default plan or changing pairs."


def read_catalog(path, owner_uid=0):
    """Read only a private local catalog; missing or unsafe files have no defaults."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != owner_uid
                    or info.st_mode & 0o027 or info.st_size > 16384):
                return None
            return validate_catalog(json.load(stream))
    except (OSError, ValueError, TypeError):
        return None


def public_presets(catalog):
    return [{"id": item["id"], "label": item["label"], "index": item["index"],
             "name": item["rx_name"] + " / " + item["tx_name"],
             "rx_name": item["rx_name"], "tx_name": item["tx_name"],
             "input_hz": item["rx_hz"], "output_hz": item["tx_hz"], "tone_hz": item["tone_hz"]}
            for item in pair_plans(catalog).values()] if catalog else []

# An app-only update can run alongside the installed 0.7.3 helper. Translate
# only its two known normal-success hints; errors and recovery instructions
# remain verbatim, and this presentation adapter cannot authorize radio work.
LEGACY_ROUTINE_SUCCESS = {
    ("inspect", "inspected"): (
        "Read-only backup saved. The selected 5RX profile matches the reviewed revision. Power-cycle the radio before choosing Program.",
        "Read-only backup saved. The selected 5RX profile matches the reviewed revision. Ready to program; the worker reconnects automatically."),
    ("editor-read", "editor-ready"): (
        "Radio memories and operating settings read. Power-cycle the radio before applying editor changes.",
        "Radio memories and operating settings read. Review your changes before writing; the worker reconnects automatically."),
}


def routine_success_message(message, action, result):
    phase = result.get("phase")
    replacement = LEGACY_ROUTINE_SUCCESS.get((action, phase)) if isinstance(phase, str) else None
    if (result.get("success") is True and result.get("verification_required") is not True
            and replacement and message == replacement[0]):
        return replacement[1]
    return message


def public_pair(value, catalog=None):
    if not isinstance(value, dict) or not isinstance(value.get("pair_id"), str):
        return None
    preset = next((item for item in public_presets(catalog) if item["id"] == value["pair_id"]), None)
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,15}", value["pair_id"]) or not isinstance(value.get("request_id"), str) or not REQUEST_RE.fullmatch(value["request_id"]):
        return None
    roles = value.get("roles") if isinstance(value.get("roles"), dict) else {}
    roles = {role: roles.get(role) if isinstance(roles.get(role), dict) else {} for role in STATION_ROLES}
    return {"pair_id": value["pair_id"], "request_id": value["request_id"],
            "phase": str(value.get("phase", "unknown"))[:40],
            "stored_verified": value.get("stored_verified") is True and catalog is not None and value.get("catalog_digest") == catalog_digest(catalog),
            "catalog_changed": catalog is None or value.get("catalog_digest") != catalog_digest(catalog),
            "activation_required": value.get("activation_required") is True,
            "verification_required": value.get("verification_required") is True,
            "roles": {role: {"written": roles[role].get("written") is True,
                             "verified": roles[role].get("verified") is True}
                      for role in STATION_ROLES},
            "plan": {"name": preset["name"], "rx_hz": preset["input_hz"], "tx_hz": preset["output_hz"],
                     "rx_name": preset["rx_name"], "tx_name": preset["tx_name"],
                     "tone_hz": preset["tone_hz"], "mode": "NFM", "power": "High"} if preset and value.get("catalog_digest") == catalog_digest(catalog) else None}


def valid_request(value):
    if isinstance(value, dict) and value.get("action") == "change-pair":
        return (set(value) == {"request_id", "action", "pair_id", "catalog_digest"}
                and isinstance(value.get("catalog_digest"), str) and bool(re.fullmatch(r"[0-9a-f]{64}", value["catalog_digest"]))
                and isinstance(value.get("request_id"), str) and bool(REQUEST_RE.fullmatch(value["request_id"]))
                and isinstance(value.get("pair_id"), str) and bool(re.fullmatch(r"[a-z][a-z0-9_-]{0,15}", value["pair_id"])))
    if (not isinstance(value, dict) or not isinstance(value.get("request_id"), str)
            or not REQUEST_RE.fullmatch(value["request_id"])
            or value.get("role") not in ROLES or value.get("action") not in ACTIONS):
        return False
    if value["action"] == "program":
        return (set(value) == {"request_id", "role", "action", "catalog_digest"}
                and value["role"] in STATION_ROLES and isinstance(value.get("catalog_digest"), str)
                and bool(re.fullmatch(r"[0-9a-f]{64}", value["catalog_digest"])))
    if value["action"] == "editor-write":
        fields = {"request_id", "role", "action", "base_id", "changes", "settings"}
        return (set(value) in (fields, fields | {"supersede_id"})
                and ("supersede_id" not in value or isinstance(value["supersede_id"], str)
                     and bool(REQUEST_RE.fullmatch(value["supersede_id"])))
                and valid_write(value["base_id"], value["changes"], value["settings"], value.get("supersede_id")))
    if value["action"] == "repair-upload":
        return (set(value) == {"request_id", "role", "action", "pending_id"}
                and isinstance(value.get("pending_id"), str)
                and bool(REQUEST_RE.fullmatch(value["pending_id"])))
    if value["action"] == "editor-read" and "model" in value:
        return (set(value) == {"request_id", "role", "action", "model"}
                and value["role"] == "programmer" and value["model"] in MODELS)
    return (set(value) == {"request_id", "role", "action"}
            and (value["role"] != "programmer" or value["action"] in ("verify", "editor-read")))


def valid_config(value):
    return (isinstance(value, dict) and set(value) == {"schema", "model", "devices"}
            and type(value["schema"]) is int and value["schema"] == 1
            and value["model"] == "5RX" and isinstance(value["devices"], dict)
            and set(value["devices"]) in (set(STATION_ROLES), set(ROLES))
            and all(isinstance(value["devices"][role], str)
                    and DEVICE_RE.fullmatch(value["devices"][role]) for role in STATION_ROLES)
            and ("programmer" not in value["devices"] or isinstance(value["devices"]["programmer"], str)
                 and PROGRAMMER_RE.fullmatch(value["devices"]["programmer"]))
            and len(set(value["devices"].values())) == len(value["devices"]))


def public_role(value):
    value = value if isinstance(value, dict) else {}
    model = value.get("model", "5RX")
    return {"model": model if model in MODELS else None,
            "inspected": value.get("inspected") is True,
            "compatible": value.get("compatible") is True,
            "programmed": value.get("programmed") is True,
            "verified": value.get("verified") is True,
            "verification_required": value.get("verification_required") is True,
            "pending_id": (value.get("pending_id") if value.get("verification_required") is True
                           and isinstance(value.get("pending_id"), str)
                           and REQUEST_RE.fullmatch(value["pending_id"]) else None),
            "program_kind": value.get("program_kind") if value.get("program_kind") in ("editor", "repeater", "pair") else None,
            "firmware": str(value.get("firmware", ""))[:80],
            "identity": str(value.get("identity", ""))[:32],
            "backup_id": (value.get("backup_id") if isinstance(value.get("backup_id"), str)
                          and REQUEST_RE.fullmatch(value["backup_id"]) else None)}


def public_progress(value):
    if (not isinstance(value, dict) or set(value) != {"current", "total", "percent", "unit"}
            or not all(type(value.get(key)) is int for key in ("current", "total", "percent"))
            or not 0 <= value["current"] <= value["total"] <= 1000000
            or value["total"] < 1 or not 0 <= value["percent"] <= 100
            or value.get("unit") not in ("blocks", "steps")):
        return None
    return copy.deepcopy(value)


def public_events(value):
    if not isinstance(value, list):
        return []
    return [{"time": item["time"][:80], "message": item["message"][:240]}
            for item in value[-20:] if isinstance(item, dict)
            and isinstance(item.get("time"), str) and isinstance(item.get("message"), str)]


class RadioProgramming:
    REQUEST = "radio-programming-request.json"
    RESULT = "radio-programming-result.json"
    PUBLIC_STATE = "radio-programming-state.json"
    PENDING_SECONDS = 600

    def __init__(self, state_dir, supported=None, runner=None, clock=None,
                 config_file="/etc/repeater-box/radio-programming.json",
                 pair_config_file="/etc/repeater-box/radio-pair.json",
                 catalog_file="/etc/repeater-box/channel-pairs.json", catalog_owner_uid=0):
        self.state_dir = Path(state_dir)
        self.config_file = Path(config_file)
        self.pair_config_file = Path(pair_config_file)
        self.catalog_file = Path(catalog_file)
        self.catalog_owner_uid = catalog_owner_uid
        self.supported = is_pi() if supported is None else supported
        self.run = runner or command
        self.clock = clock or time.time
        self.lock = threading.RLock()

    def status(self):
        with self.lock:
            config = read_json(self.config_file, 4096)
            catalog = read_catalog(self.catalog_file, self.catalog_owner_uid)
            configured_roles = [role for role in ROLES if valid_config(config) and role in config["devices"]]
            state = {"available": False, "configured": valid_config(config),
                     "preview": not self.supported, "pending": False, "busy": False,
                     "request_id": None, "role": None, "action": None,
                     "phase": None, "success": None, "message": None,
                     "progress": None, "events": [],
                     "backup_id": None, "verification_required": False,
                     "model": "Baofeng UV-5RX / 5RX", "plan": default_plan(catalog) if catalog else None,
                     "catalog_configured": catalog is not None, "catalog_digest": catalog_digest(catalog) if catalog else None,
                     "supported_models": copy.deepcopy(SUPPORTED_MODELS),
                     "pair": None, "pair_switching": {"enabled": False, "presets": public_presets(catalog),
                         "message": CATALOG_MESSAGE if not catalog else "Complete the local memory-1 and live reload bench checks before enabling paired changes."},
                     "configured_roles": configured_roles,
                     "roles": {role: public_role({}) for role in (configured_roles or STATION_ROLES)}}
            if not self.supported:
                state["message"] = "Preview only — radio programming runs on the Pi."
                return state
            if not state["configured"]:
                state["message"] = "Configure the RX and TX AIOC connections locally on the Pi first."
                return state
            if self.run(["/usr/bin/systemctl", "is-active", "repeater-radio-programming.path"]) != "active":
                state["message"] = "Install the current Pi radio programming helper first."
                return state
            state["available"] = True
            pair_config = read_json(self.pair_config_file, 4096)
            state["pair_switching"]["enabled"] = (set(pair_config) == {"schema", "enabled"}
                and type(pair_config.get("schema")) is int and pair_config["schema"] == 1
                and pair_config.get("enabled") is True and catalog is not None)
            worker_active = self.run(["/usr/bin/systemctl", "is-active", "repeater-radio-programming.service"]) == "active"
            saved = read_json(self.state_dir / self.PUBLIC_STATE)
            state["pair"] = public_pair(saved.get("_pair"), catalog)
            state["roles"] = {role: public_role(saved.get(role)) for role in configured_roles}
            request = read_json(self.state_dir / self.REQUEST, 65536)
            result = read_json(self.state_dir / self.RESULT)
            if valid_request(request):
                state.update({name: request.get(name) for name in ("request_id", "role", "action")})
                if result.get("request_id") == request["request_id"]:
                    for key in ("message", "phase"):
                        if isinstance(result.get(key), str):
                            state[key] = (routine_success_message(result[key], request["action"], result)
                                          if key == "message" else result[key])[:1000]
                    state["backup_id"] = public_role(result)["backup_id"]
                    state["verification_required"] = result.get("verification_required") is True
                    state["progress"] = public_progress(result.get("progress"))
                    state["events"] = public_events(result.get("events"))
                    for event in state["events"]:
                        event["message"] = routine_success_message(event["message"], request["action"], result)
                    if type(result.get("success")) is bool:
                        state["success"] = result["success"]
                if state["success"] is None:
                    try:
                        age = self.clock() - (self.state_dir / self.REQUEST).stat().st_mtime
                    except OSError:
                        age = self.PENDING_SECONDS + 1
                    state["pending"] = -60 <= age <= self.PENDING_SECONDS
                    if state["pending"]:
                        state["message"] = state["message"] or "Radio request queued. Keep the radio connected and powered on."
                    else:
                        state.update(success=False, message="Radio work was interrupted. Check the helper log before retrying; verify any unfinished upload.")
            state["pending"] = state["pending"] or worker_active
            state["busy"] = state["pending"]
            return state

    def request(self, role, action, pending_id=None, catalog_digest=None):
        if (role not in ROLES or action not in ("inspect", "program", "verify", "repair-upload")
                or role == "programmer" and action not in ("verify", "repair-upload")):
            raise ValueError("Select a configured radio and a supported programming action")
        if action == "program":
            if pending_id is not None or not isinstance(catalog_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", catalog_digest):
                raise ValueError("Review the configured channel catalog before programming")
            return self._request(role, action, {"catalog_digest": catalog_digest})
        if catalog_digest is not None:
            raise ValueError("A channel catalog digest is accepted only for programming")
        if action == "repair-upload":
            if not isinstance(pending_id, str) or not REQUEST_RE.fullmatch(pending_id):
                raise ValueError("Review the exact unfinished upload before repairing it")
            return self._request(role, action, {"pending_id": pending_id})
        if pending_id is not None:
            raise ValueError("A saved-upload identifier is only accepted for repair")
        return self._request(role, action)

    def request_pair(self, pair_id, catalog_digest=None):
        if not isinstance(pair_id, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,15}", pair_id):
            raise ValueError("Choose a configured channel pair")
        with self.lock:
            state = self.status()
            if not isinstance(catalog_digest, str) or catalog_digest != state["catalog_digest"]:
                raise ValueError("The channel catalog changed. Review the pair again")
            if not state["catalog_configured"] or pair_id not in {item["id"] for item in state["pair_switching"]["presets"]}:
                raise ValueError(CATALOG_MESSAGE)
            if not state["available"] or not state["pair_switching"]["enabled"]:
                raise ValueError("Enable paired changes locally after completing the radio bench checks")
            if state["pending"]:
                raise ValueError("A radio operation is already pending")
            if not all(state["roles"].get(role, {}).get("compatible") for role in STATION_ROLES):
                raise ValueError("Inspect both supported UV-5RX radios before changing their pair")
            if state["pair"] and state["pair"]["verification_required"] and state["pair"]["catalog_changed"]:
                raise ValueError("Restore the pending pair's private catalog before retrying")
            unfinished = [role for role, value in state["roles"].items() if value["verification_required"]]
            if unfinished and ("programmer" in unfinished or not state["pair"] or not state["pair"]["verification_required"]):
                raise ValueError("Resolve the separate unfinished radio upload before changing the pair")
            request = {"request_id": secrets.token_hex(16), "action": "change-pair", "pair_id": pair_id, "catalog_digest": catalog_digest}
            write_json(self.state_dir / self.REQUEST, request)
            state.update(request_id=request["request_id"], role=None, action="change-pair", pending=True,
                         busy=True, success=None, phase="queued", progress=None, events=[], backup_id=None,
                         message="Paired change queued. Both radios stay paused until the operation finishes.")
            return state

    def editor_snapshot(self, role, model=None):
        if role not in ROLES:
            raise ValueError("Choose RX or TX")
        if model is not None and (role != "programmer" or model not in MODELS):
            raise ValueError("Choose a supported external radio model")
        # A development preview never supplies pretend radio contents.
        if not self.supported:
            return None
        config = read_json(self.config_file, 4096)
        if not valid_config(config) or role not in config["devices"]:
            raise ValueError("Configure this programming connection locally on the Pi first")
        snapshot = public_snapshot(read_json(self.state_dir / f"radio-editor-{role}.json", 131072), role)
        return snapshot if model is None or snapshot is None or snapshot["model"] == model else None

    def request_editor(self, role, action, base_id=None, changes=None, settings=None, supersede_id=None, model=None):
        if role not in ROLES or action not in ("editor-read", "editor-write"):
            raise ValueError("Choose RX or TX and read or write its radio settings")
        payload = None
        if model is not None:
            if role != "programmer" or action != "editor-read" or model not in MODELS:
                raise ValueError("Choose a supported external radio model")
            payload = {"model": model}
        if action == "editor-write":
            if not valid_write(base_id, changes, settings, supersede_id):
                raise ValueError("Send a current read token and valid channel or setting changes")
            payload = {"base_id": base_id, "changes": changes, "settings": settings}
            if supersede_id is not None:
                payload["supersede_id"] = supersede_id
        return self._request(role, action, payload)

    def _request(self, role, action, payload=None):
        with self.lock:
            state = self.status()
            if not state["available"]:
                raise ValueError(state["message"])
            if state["pending"]:
                raise ValueError("A radio operation is already pending")
            if role not in state["configured_roles"]:
                raise ValueError("Configure this programming connection locally on the Pi first")
            selected = state["roles"][role]
            if (action == "editor-read" and role == "programmer"
                    and selected["verification_required"]
                    and selected["model"] != (payload or {}).get("model", "5RX")):
                raise ValueError("Verify or repair the unfinished upload before selecting another radio model")
            if action in ("inspect", "program") and any(item["verification_required"] for item in state["roles"].values()):
                raise ValueError("Verify the unfinished upload before inspecting or programming either radio")
            if action == "program" and (not state["catalog_configured"] or (payload or {}).get("catalog_digest") != state["catalog_digest"]):
                raise ValueError(CATALOG_MESSAGE)
            if action == "program" and (not selected["inspected"] or not selected["compatible"]):
                raise ValueError("Inspect this radio first; its identity must match the supported 5RX profile")
            if action == "program" and selected["verification_required"]:
                raise ValueError("Verify the unfinished upload before programming again")
            if action == "repair-upload":
                if (not selected["verification_required"] or not selected["compatible"]
                        or selected["program_kind"] not in ("editor", "repeater")
                        or payload["pending_id"] != selected["pending_id"]):
                    raise ValueError("The saved upload changed or is not repairable. Refresh and review this radio again")
                if state["pair"] and state["pair"]["verification_required"]:
                    raise ValueError("An unfinished paired change needs local review before saved-upload repair")
            if action == "editor-write":
                snapshot = self.editor_snapshot(role)
                if not snapshot or snapshot["base_id"] != payload["base_id"]:
                    raise ValueError("This draft is stale. Read the connected radio again before writing")
                if snapshot["model"] != selected["model"]:
                    raise ValueError("The radio model changed. Read the connected radio again before writing")
                if not snapshot["compatible"]:
                    raise ValueError("This radio is not compatible with the selected radio profile")
                if not set(payload["settings"]) <= set(snapshot["capabilities"]["settings"]):
                    raise ValueError("Change only settings supported by this radio model")
                recovery = snapshot["recovery"]
                if selected["verification_required"]:
                    if (not recovery["required"] or payload.get("supersede_id") != recovery["pending_id"]):
                        raise ValueError("Read this radio again and explicitly confirm replacing its unfinished upload")
                    if recovery["protected_mismatch"]:
                        raise ValueError("Protected radio data differs from the pending candidate. Keep services paused and inspect the backup locally")
                elif payload.get("supersede_id") is not None or recovery["required"]:
                    raise ValueError("The pending upload changed. Read the radio and review this draft again")
            if action == "verify" and not (selected["programmed"] or selected["verification_required"]):
                raise ValueError("There is no saved programming result to verify for this radio")
            request = {"request_id": secrets.token_hex(16), "role": role, "action": action}
            if payload:
                request.update(copy.deepcopy(payload))
            write_json(self.state_dir / self.REQUEST, request)
            state.update({key: request[key] for key in ("request_id", "role", "action")},
                         pending=True, busy=True, success=None, phase="queued",
                         progress=None, events=[], backup_id=None, verification_required=False,
                         message="Radio request queued. Keep the radio connected and powered on.")
            return state
