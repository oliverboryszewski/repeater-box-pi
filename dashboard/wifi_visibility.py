"""Request one narrow Pi Wi-Fi change through a separate root systemd worker."""
import json
import os
from pathlib import Path
import secrets
import subprocess
import threading
import time
from controls import write_json


def command(args):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=3, check=False,
                                env={**os.environ, "LC_ALL": "C"})
        return result.stdout.strip() if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def is_pi():
    try:
        return "Raspberry Pi" in Path("/proc/device-tree/model").read_text()
    except (OSError, UnicodeError):
        return False


class WifiVisibility:
    REQUEST = "wifi-visibility-request.json"
    RESULT = "wifi-visibility-result.json"

    def __init__(self, state_dir, supported=None, runner=None, clock=None):
        self.state_dir = Path(state_dir)
        self.supported = is_pi() if supported is None else supported
        self.run = runner or command
        self.clock = clock or time.time
        self.lock = threading.RLock()

    def _read(self, name):
        try:
            with (self.state_dir / name).open() as stream:
                raw = stream.read(2049)
            if len(raw) > 2048:
                return {}
            value = json.loads(raw)
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def status(self):
        with self.lock:
            state = {"available": False, "profile_hidden": None, "pending": False,
                     "requested_hidden": None, "request_id": None, "success": None,
                     "message": None, "ssid": "Repeater-Box", "preview": not self.supported}
            if not self.supported:
                state["message"] = "Preview only — Wi-Fi visibility changes apply on the Pi."
                return state
            hidden = self.run(["/usr/bin/nmcli", "-g", "802-11-wireless.hidden", "connection", "show", "id", "repeater-ap"])
            if hidden not in ("yes", "no", "true", "false"):
                state["message"] = "Set up the Repeater-Box hotspot on the Pi first."
                return state
            state["profile_hidden"] = hidden in ("yes", "true")
            ssid = self.run(["/usr/bin/nmcli", "-g", "802-11-wireless.ssid", "connection", "show", "id", "repeater-ap"])
            if ssid:
                state["ssid"] = ssid
            worker = self.run(["/usr/bin/systemctl", "is-active", "repeater-wifi-visibility.path"])
            if worker != "active":
                state["message"] = "Wi-Fi visibility helper is unavailable. Rerun the current Pi installer."
                return state
            state["available"] = True
            request = self._read(self.REQUEST)
            result = self._read(self.RESULT)
            request_id = request.get("request_id")
            if (isinstance(request_id, str) and len(request_id) == 32
                    and all(c in "0123456789abcdefABCDEF" for c in request_id)
                    and type(request.get("hidden")) is bool):
                state.update(request_id=request_id, requested_hidden=request["hidden"])
                if result.get("request_id") == request_id and type(result.get("success")) is bool:
                    state["success"] = result["success"]
                    state["message"] = result.get("message") if isinstance(result.get("message"), str) else None
                else:
                    try:
                        age = self.clock() - (self.state_dir / self.REQUEST).stat().st_mtime
                    except OSError:
                        age = 181
                    state["pending"] = -60 <= age <= 180
                    state["message"] = ("Applying Wi-Fi visibility. Reconnect to the network if it briefly disconnects."
                                        if state["pending"] else "The Wi-Fi change was not confirmed. Check the Pi helper log, then retry.")
                    if not state["pending"]:
                        state["success"] = False
            return state

    def request(self, hidden):
        if type(hidden) is not bool:
            raise ValueError("hidden must be true or false")
        with self.lock:
            state = self.status()
            if not state["available"]:
                raise ValueError(state["message"] or "Wi-Fi visibility changes are unavailable")
            if state["pending"]:
                raise ValueError("A Wi-Fi visibility change is already pending")
            request_id = secrets.token_hex(16)
            write_json(self.state_dir / self.REQUEST, {"request_id": request_id, "hidden": hidden})
            # Return before the worker restarts Wi-Fi; do not run more NM queries.
            state.update(pending=True, requested_hidden=hidden, request_id=request_id,
                         success=None, message="Applying — Wi-Fi may disconnect briefly. Reconnect using the same network name and password.")
            return state
