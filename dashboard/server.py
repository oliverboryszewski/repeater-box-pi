#!/usr/bin/env python3
"""Local dashboard, RX monitor, and audio archive for a Raspberry Pi repeater.

Hardware changes are queued to narrowly scoped, separately installed helpers.
The web process does not open serial ports or control PTT.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import mimetypes
import math
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import socket
import stat
import subprocess
import queue
import time
import threading
from http.cookies import SimpleCookie, CookieError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlsplit
from controls import SettingsStore, AdminAuth
from audio_hub import AudioHub, DataDecoder, SstvDecoder
from dtmf_control import DtmfControl
from station_identification import StationIdentification
from wifi_visibility import WifiVisibility
from software_updates import SoftwareUpdates
from radio_programming import RadioProgramming
from radio_editor import MODELS
from native_state import NativeState
from radio_messages import RadioMessages, MessageError, MAX_UPLOAD_BYTES, upload_name

VERSION = "0.15.1"
STATIC = Path(__file__).parent / "static"
STATIC_ROUTES = {
    "/": "index.html", "/index.html": "index.html", "/app.js": "app.js", "/style.css": "style.css",
    "/audio-visualizer.js": "audio-visualizer.js", "/audio-visualizer.css": "audio-visualizer.css",
    "/radio-editor": "radio-editor.html", "/radio-editor.js": "radio-editor.js",
    "/radio-editor.css": "radio-editor.css",
}
MEDIA_TYPES = {".wav": "audio/wav", ".ogg": "audio/ogg", ".mp3": "audio/mpeg", ".flac": "audio/flac"}


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def radio_work_unfinished(radio):
    roles = radio.get("roles") if isinstance(radio.get("roles"), dict) else {}
    pair = radio.get("pair") if isinstance(radio.get("pair"), dict) else {}
    return (radio.get("pending") is True or radio.get("verification_required") is True
            or any(isinstance(role, dict) and role.get("verification_required") is True
                   for role in roles.values())
            or pair.get("verification_required") is True or pair.get("activation_required") is True)


def valid_saved_plan(plan):
    return (isinstance(plan, dict)
            and all(type(plan.get(key)) is int and 0 < plan[key] < 10**10 for key in ("rx_hz", "tx_hz"))
            and "tone_hz" in plan and (plan["tone_hz"] is None or type(plan["tone_hz"]) in (int, float)
                 and math.isfinite(plan["tone_hz"]) and 0 < plan["tone_hz"] < 1000)
            and plan.get("power", "High") in ("High", "Low"))


def verified_pair(radio):
    pair = radio.get("pair") if isinstance(radio.get("pair"), dict) else {}
    roles = pair.get("roles") if isinstance(pair.get("roles"), dict) else {}
    if (radio_work_unfinished(radio) or pair.get("stored_verified") is not True
            or pair.get("catalog_changed") is True or not valid_saved_plan(pair.get("plan"))
            or not isinstance(pair.get("pair_id"), str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,15}", pair["pair_id"])
            or not isinstance(radio.get("catalog_digest"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", radio["catalog_digest"])
            or not all(isinstance(roles.get(role), dict) and roles[role].get("verified") is True for role in ("rx", "tx"))):
        return None
    return pair


def public_radio_plan(radio):
    pair = radio.get("pair") if isinstance(radio.get("pair"), dict) else {}
    saved = pair.get("plan")
    if valid_saved_plan(saved):
        switching = radio.get("pair_switching") if isinstance(radio.get("pair_switching"), dict) else {}
        presets = switching.get("presets") if isinstance(switching.get("presets"), list) else []
        preset = next((item for item in presets if isinstance(item, dict) and item.get("id") == pair.get("pair_id")), {})
        return {"rx_mhz": f"{saved['rx_hz'] / 1000000:.6f}", "tx_mhz": f"{saved['tx_hz'] / 1000000:.6f}",
                "tone_hz": saved["tone_hz"], "power": saved.get("power", "High"),
                "verified": verified_pair(radio) is not None, "pair_id": pair.get("pair_id"),
                "label": preset.get("label"), "rx_name": saved.get("rx_name", preset.get("rx_name")),
                "tx_name": saved.get("tx_name", preset.get("tx_name"))}
    plan = radio.get("plan")
    if (isinstance(plan, dict) and all(isinstance(plan.get(role), dict)
            and isinstance(plan[role].get("frequency_mhz"), str)
            and re.fullmatch(r"[0-9]{2,4}\.[0-9]{3,6}", plan[role]["frequency_mhz"]) for role in ("rx", "tx"))
            and "receive_tone_hz" in plan["rx"]):
        return {"rx_mhz": plan["rx"]["frequency_mhz"], "tx_mhz": plan["tx"]["frequency_mhz"],
                "tone_hz": plan["rx"]["receive_tone_hz"], "power": plan["tx"].get("power", "High"),
                "verified": False, "pair_id": None, "label": None,
                "rx_name": plan["rx"].get("name"), "tx_name": plan["tx"].get("name")}
    return None


def message_busy_reason(server):
    # This callback deliberately never calls messages.status/active: status()
    # invokes it itself, and active is the separate mutation exclusion check.
    if radio_work_unfinished(server.programming.status()):
        return "Finish the current radio work before managing messages."
    if server.updates.status().get("pending") is True or server.wifi.status().get("pending") is True:
        return "Wait for the current maintenance task before managing messages."
    if server.dtmf.code_status(probe=False).get("pending") is True:
        return "Wait for the DTMF password change before managing messages."
    return None


def radio_message_status(server, radio=None):
    state = server.messages.status()
    radio = server.programming.status() if radio is None else radio
    pair = verified_pair(radio)
    state.update(pair_id=pair["pair_id"] if pair else None,
                 catalog_digest=radio.get("catalog_digest") if pair else None)
    if not pair:
        state["can_broadcast"] = False
        state["reason"] = state.get("reason") or "A verified channel pair is required to broadcast."
    return state


def radio_activity(server):
    value = server.native_state.read()
    available = bool(value and value["online"])
    return {"available": available, "receiving": value["rx_open"] if available else None,
            "transmitting": value["tx_on"] if available else None}


def data_status(server):
    """Receiver data only: no subprocess inventory or radio connection probe."""
    value = server.data_decoder.status()
    value["packets"] = server.data_decoder.list_packets(limit=100)
    plan = getattr(server, "data_radio_plan", None)
    confirmed = isinstance(plan, dict) and plan.get("verified") is True
    value["rx_name"] = plan.get("rx_name") if confirmed else None
    value["pair_label"] = plan.get("label") if confirmed else None
    return value


def sstv_status(server):
    """Cached private image reception only; never probe radio hardware."""
    value = dict(server.sstv_decoder.status())
    value["images"] = [dict(image) for image in server.sstv_decoder.list_images(limit=50)
                       if isinstance(image, dict)]
    current = value.get("current")
    if isinstance(current, dict):
        current = value["current"] = dict(current)
        current.pop("preview_url", None)
    if isinstance(current, dict) and re.fullmatch(r"[0-9a-f]{32}", str(current.get("id", ""))):
        revision = current.get("revision")
        if type(revision) is int and 0 < revision < 2**31:
            current["preview_url"] = f"/api/sstv/current.png?id={current['id']}&revision={revision}"
        else:
            current.pop("preview_url", None)
    for image in value["images"]:
        if isinstance(image, dict):
            image.pop("preview_url", None)
            image.pop("download_url", None)
        if isinstance(image, dict) and re.fullmatch(r"[0-9a-f]{32}", str(image.get("id", ""))):
            image["preview_url"] = f"/api/sstv/images/{image['id']}.png"
            image["download_url"] = image["preview_url"] + "?download=1"
            image["can_delete"] = bool(re.fullmatch(r"[0-9a-f]{64}", str(image.get("revision", "")))
                and not (value.get("phase") == "receiving" and isinstance(current, dict)
                         and current.get("id") == image["id"]))
    plan = getattr(server, "data_radio_plan", None)
    confirmed = isinstance(plan, dict) and plan.get("verified") is True
    value["rx_name"] = plan.get("rx_name") if confirmed else None
    value["pair_label"] = plan.get("label") if confirmed else None
    return value


def static_files_ready():
    """A running web process is not ready if its interface files are missing."""
    try:
        for name in set(STATIC_ROUTES.values()):
            info = (STATIC / name).stat()
            if not stat.S_ISREG(info.st_mode) or info.st_size == 0:
                return False
    except OSError:
        return False
    return True


def read_text(path):
    try:
        return Path(path).read_text().strip().strip("\x00")
    except (OSError, UnicodeError):
        return None


def command(args):
    """Bounded, read-only commands. Never invoke a shell."""
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=3, check=False)
        return result.stdout.strip() if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def audio_cards(text):
    if not text:
        return []
    return [{"card": int(m[0]), "id": m[1], "name": m[2].strip()}
            for m in re.findall(r"^card (\d+):\s+(\S+)\s+\[([^\]]+)\]", text, re.M)]


def count_aioc_usb_devices(root=Path("/sys/bus/usb/devices")):
    """Inventory physical AIOCs; product text is only a missing-ID fallback."""
    try:
        devices = list(Path(root).iterdir())
    except OSError:
        return None
    count = 0
    for device in devices:
        # USB interfaces belong to an already counted physical device.
        if ":" in device.name:
            continue
        vendor = read_text(device / "idVendor")
        product_id = read_text(device / "idProduct")
        if vendor is not None and product_id is not None:
            detected = (vendor.lower(), product_id.lower()) == ("1209", "7388")
        else:
            name = read_text(device / "product") or ""
            detected = ((vendor is None or vendor.lower() == "1209")
                        and (product_id is None or product_id.lower() == "7388")
                        and name.casefold() in ("aioc", "all-in-one-cable"))
        count += int(detected)
    return count


def media_files(root):
    """SvxLink's configured recording directory; no traversal or symlink files."""
    files = []
    try:
        for path in root.iterdir():
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or path.suffix.lower() not in MEDIA_TYPES:
                continue
            item = {"id": path.name, "name": path.name, "bytes": info.st_size,
                    "modified_at": dt.datetime.fromtimestamp(info.st_mtime, dt.timezone.utc).isoformat(),
                    "url": "/recordings/" + quote(path.name, safe="")}
            item["can_delete"] = valid_recording_id(path.name) and info.st_nlink == 1
            if item["can_delete"]:
                item["delete_revision"] = AudioHub.recording_revision(path.name, info)
            files.append(item)
    except OSError:
        pass
    return sorted(files, key=lambda item: item["modified_at"], reverse=True)


def valid_recording_id(name):
    if (not isinstance(name, str) or not name or name.startswith(".") or len(name) > 255
            or any(ord(value) < 32 or ord(value) == 127 for value in name)
            or "/" in name or "\\" in name or Path(name).suffix.lower() not in MEDIA_TYPES):
        return False
    try:
        return len(name.encode("utf-8")) <= 255
    except UnicodeError:
        return False


def recording_status(server):
    """Fresh media inventory for admin review; no radio/system inventory."""
    files = media_files(server.monitor.recordings)
    return {"recordings": {"count": len(files), "total_bytes": sum(item["bytes"] for item in files), "files": files},
            "recorder": server.hub.status()["recorder"]}


def parse_range(value, size):
    """Single HTTP byte range, including suffix ranges; None means full response."""
    if not value:
        return None
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", value)
    if not match or size <= 0 or not any(match.groups()):
        raise ValueError("Invalid range")
    first, last = match.groups()
    if not first:
        count = int(last)
        if count <= 0:
            raise ValueError("Invalid suffix")
        return max(0, size - count), size - 1
    start = int(first)
    end = min(int(last), size - 1) if last else size - 1
    if start >= size or end < start:
        raise ValueError("Unsatisfiable range")
    return start, end


class Monitor:
    def __init__(self, recordings):
        self.recordings = Path(recordings)

    def status(self):
        linux = platform.system() == "Linux"
        model = read_text("/proc/device-tree/model") or ""
        is_pi = "Raspberry Pi" in model
        uptime = read_text("/proc/uptime") if linux else None
        try:
            uptime_seconds = float(uptime.split()[0]) if uptime else None
        except (ValueError, IndexError):
            uptime_seconds = None
        temp = read_text("/sys/class/thermal/thermal_zone0/temp") if is_pi else None
        try:
            temp_c = round(int(temp) / 1000, 1) if temp else None
        except ValueError:
            temp_c = None
        memory = None
        meminfo = read_text("/proc/meminfo") if linux else None
        if meminfo:
            values = dict(re.findall(r"^(MemTotal|MemAvailable):\s+(\d+) kB", meminfo, re.M))
            if "MemTotal" in values and "MemAvailable" in values:
                memory = {"total_bytes": int(values["MemTotal"]) * 1024,
                          "available_bytes": int(values["MemAvailable"]) * 1024}
        disk_path = self.recordings if self.recordings.exists() else Path("/")
        disk = shutil.disk_usage(disk_path)
        power = {"available": False, "raw": None, "under_voltage_now": None,
                 "throttled_now": None, "under_voltage_occurred": None}
        if is_pi:
            value = command(["vcgencmd", "get_throttled"])
            try:
                flags = int(value.split("=", 1)[1], 16) if value else None
            except (ValueError, IndexError):
                flags = None
            if flags is not None:
                power.update(available=True, raw=value, under_voltage_now=bool(flags & 1),
                             throttled_now=bool(flags & 4), under_voltage_occurred=bool(flags & 0x10000))
        addresses = []
        if linux:
            value = command(["ip", "-j", "-4", "address", "show"])
            try:
                addresses = [item["local"] for iface in json.loads(value or "[]")
                             for item in iface.get("addr_info", []) if item.get("scope") == "global"]
            except (ValueError, KeyError, TypeError):
                pass
        active = command(["nmcli", "-t", "-f", "NAME", "connection", "show", "--active"]) if linux else None
        hotspot = bool(active and "repeater-ap" in active.splitlines())
        ssid = command(["nmcli", "-g", "802-11-wireless.ssid", "connection", "show", "repeater-ap"]) if hotspot else None
        bench_state = command(["systemctl", "show", "repeater-engine.service", "--property=ActiveState", "--value"]) if linux else None
        rx_state = command(["systemctl", "show", "repeater-rx-monitor.service", "--property=ActiveState", "--value"]) if linux else None
        live_state = command(["systemctl", "show", "repeater-live.service", "--property=ActiveState", "--value"]) if linux else None
        unit_states = {"bench": bench_state, "receive_only": rx_state, "repeater": live_state}
        rf_enabled = live_state == "active"
        if rf_enabled:
            mode, state, phase = "repeater", live_state, "Live repeater"
        elif rx_state == "active":
            mode, state, phase = "receive_only", rx_state, "Receive monitor"
        elif bench_state == "active":
            mode, state, phase = "bench", bench_state, "bench"
        elif linux:
            mode = "stopped"
            if "failed" in unit_states.values():
                state, phase = "failed", "Radio service failed"
            elif "activating" in unit_states.values():
                state, phase = "activating", "Radio service starting"
            elif "deactivating" in unit_states.values():
                state, phase = "deactivating", "Radio service stopping"
            else:
                state = "inactive" if "inactive" in unit_states.values() else "unavailable"
                phase = "Radio service stopped"
        else:
            mode, state, phase = "unavailable", "unavailable", "Dashboard preview"
        installed = bool(shutil.which("svxlink"))
        capture = audio_cards(command(["arecord", "-l"])) if linux else []
        playback = audio_cards(command(["aplay", "-l"])) if linux else []
        serial = []
        for port in sorted(Path("/dev/serial/by-id").glob("*")):
            serial.append({"path": str(port), "target": os.path.realpath(port)})
        aioc_count = count_aioc_usb_devices() if linux else None
        clock = command(["timedatectl", "show", "--property=NTPSynchronized", "--value"]) if linux else None
        recordings = media_files(self.recordings)
        notes = ["The live repeater service is active." if rf_enabled else "The selected radio service does not enable transmission.",
                 "Channel values come from configured settings, not continuous radio telemetry.",
                 "Activity indicators show receiver squelch and the Pi's transmit command."]
        if not is_pi:
            notes.insert(0, "Development preview: this server is not running on a Raspberry Pi.")
        return {"version": VERSION, "hostname": socket.gethostname(), "platform": model or platform.system(),
                "is_pi": is_pi, "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "uptime_seconds": uptime_seconds, "cpu_temp_c": temp_c,
                "load_1m": os.getloadavg()[0] if hasattr(os, "getloadavg") else None,
                "memory": memory, "disk": {"total_bytes": disk.total, "free_bytes": disk.free},
                "power": power, "network": {"hotspot_active": hotspot, "ssid": ssid,
                    "addresses": addresses, "dashboard_url": "http://10.42.0.1", "internet_required": False},
                "svxlink": {"installed": installed, "state": state if state else "unavailable",
                            "mode": mode, "transmit_control": "enabled" if rf_enabled else "disabled",
                            "transmitting": None, "unit_states": unit_states},
                "audio": {"capture": capture, "playback": playback, "serial_ports": serial,
                          "usb_aioc_count": aioc_count},
                "recordings": {"count": len(recordings), "total_bytes": sum(f["bytes"] for f in recordings),
                               "files": recordings},
                "setup": {"phase": phase, "rf_enabled": rf_enabled,
                          "radio_plan": None, "notes": notes},
                "clock_synchronized": {"yes": True, "no": False}.get(clock)}


class Handler(BaseHTTPRequestHandler):
    def log_request(self, code="-", size="-"):
        # Visible dashboards poll activity and receive-data progress. Keep
        # errors without writing every successful refresh to the SD card.
        if code == 200 and urlsplit(self.path).path in (
                "/api/activity", "/api/data/status", "/api/sstv/status", "/api/sstv/current.png"):
            return
        super().log_request(code, size)

    protocol_version = "HTTP/1.1"

    def headers_for(self, code, content_type, size, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        if size is not None:
            self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; media-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()

    def do_HEAD(self):
        self.serve(head=True)

    def do_GET(self):
        self.serve(head=False)

    def session_token(self):
        try:
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
            return cookie["repeater_admin"].value if "repeater_admin" in cookie else None
        except CookieError:
            return None

    def json_response(self, code, value, extra=None, head=False):
        data = json.dumps(value).encode()
        self.headers_for(code, "application/json", len(data), extra)
        if not head:
            self.wfile.write(data)

    def do_POST(self):
        # Cookie + CSRF are required for changes. A login may not cross origins.
        path = urlsplit(self.path).path
        origin = self.headers.get("Origin")
        if origin and origin != "http://" + self.headers.get("Host", ""):
            self.close_connection = True
            self.json_response(403, {"error": "Cross-origin changes are not allowed"})
            return
        if path == "/api/messages/upload":
            self.upload_message()
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            maximum = (131072 if path in ("/api/recordings/delete-many", "/api/sstv/delete-many")
                       else 65536 if path == "/api/radio-editor/write" else 4096)
            if size < 0 or size > maximum:
                raise ValueError("Request body too large")
            if self.headers.get_content_type() != "application/json":
                raise ValueError("Send JSON")
            self.connection.settimeout(10)
            data = json.loads(self.rfile.read(size), object_pairs_hook=unique_object)
            if not isinstance(data, dict):
                raise ValueError("Expected a JSON object")
        except (ValueError, TimeoutError, OSError) as error:
            self.close_connection = True
            self.json_response(400, {"error": str(error)})
            return
        token = self.session_token()
        if path == "/api/admin/login":
            try:
                token, session = self.server.auth.login(data.get("password"), self.client_address[0])
                self.json_response(200, session, {"Set-Cookie": f"repeater_admin={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=28800"})
            except (ValueError, PermissionError) as error:
                self.json_response(401, {"error": str(error)})
            return
        if path not in ("/api/admin/logout", "/api/recording", "/api/settings", "/api/wifi-visibility", "/api/software-updates/check", "/api/software-updates/install", "/api/radio-programming", "/api/radio-pair", "/api/radio-editor/read", "/api/radio-editor/write", "/api/dtmf-control", "/api/dtmf-password", "/api/station-identification", "/api/messages/delete", "/api/messages/broadcast", "/api/data/decoding", "/api/sstv/decoding", "/api/recordings/delete", "/api/sstv/delete", "/api/recordings/delete-many", "/api/sstv/delete-many"):
            self.json_response(404, {"error": "Unknown endpoint"})
            return
        if not self.server.auth.authorize(token, self.headers.get("X-CSRF-Token")):
            self.json_response(403, {"error": "Sign in as admin before changing settings or recording"})
            return
        if path in ("/api/recordings/delete", "/api/sstv/delete", "/api/recordings/delete-many", "/api/sstv/delete-many"):
            # Delete saved media only. Native audio, PTT and radio maintenance
            # are independent; the storage objects serialize against writers.
            with self.server.mutation_lock:
                audio = path.startswith("/api/recordings/")
                bulk = path.endswith("/delete-many")
                items = data.get("items") if bulk else [data]
                limit = 100 if audio else 50
                if (bulk and set(data) != {"items"} or not isinstance(items, list)
                        or not 1 <= len(items) <= limit
                        or any(not isinstance(item, dict) or set(item) != {"id", "revision"}
                               or not isinstance(item.get("revision"), str)
                               or not re.fullmatch(r"[0-9a-f]{64}", item["revision"])
                               or (not valid_recording_id(item.get("id")) if audio else
                                   not isinstance(item.get("id"), str)
                                   or not re.fullmatch(r"[0-9a-f]{32}", item["id"]))
                               for item in items)
                        or len({item["id"] for item in items}) != len(items)):
                    self.json_response(400, {"error": "Send only the reviewed saved items and their revisions"})
                    return
                try:
                    if bulk:
                        # Storage preflights the entire bounded list before any
                        # removal. Action-time I/O failures are reported with a
                        # fresh inventory; a partial batch is never retried here.
                        report = (self.server.hub.delete_recordings(items) if audio
                                  else self.server.sstv_decoder.delete_images(items))
                        value = recording_status(self.server) if audio else {"sstv": sstv_status(self.server)}
                        value["deletion"] = report
                        self.json_response(200, value)
                    elif audio:
                        self.server.hub.delete_recording(data["id"], data["revision"])
                        self.json_response(200, recording_status(self.server))
                    else:
                        self.server.sstv_decoder.delete_image(data["id"], data["revision"])
                        self.json_response(200, {"sstv": sstv_status(self.server)})
                except FileNotFoundError:
                    self.json_response(404, {"error": "That saved item is no longer available. Refresh the list."})
                except ValueError:
                    self.json_response(409, {"error": "That item changed or is still being recorded or received. Refresh and review it again."})
                except OSError:
                    self.json_response(503, {"error": "The saved item could not be removed safely. Refresh the list before trying again."})
            return
        if path in ("/api/data/decoding", "/api/sstv/decoding"):
            # A receiver-only preference does not alter radio/audio services,
            # and may be changed while live RF or other maintenance is active.
            with self.server.mutation_lock:
                try:
                    if set(data) != {"enabled"} or type(data["enabled"]) is not bool:
                        raise ValueError("Send only the decoding enabled flag as true or false")
                    with self.server.settings_lock:
                        if path == "/api/sstv/decoding":
                            settings = self.server.settings.update({"sstv_decode_enabled": data["enabled"]})
                            self.server.sstv_decoder.set_enabled(settings["sstv_decode_enabled"])
                        else:
                            settings = self.server.settings.update({"data_decode_enabled": data["enabled"]})
                            self.server.data_decoder.set_enabled(settings["data_decode_enabled"])
                    self.json_response(200, {"sstv": sstv_status(self.server)} if path == "/api/sstv/decoding"
                                       else {"data": data_status(self.server)})
                except (ValueError, OSError):
                    self.json_response(400, {"error": "Data decoding could not be updated. Check its status and try again."})
            return
        if path != "/api/admin/logout" and self.server.messages.active:
            self.json_response(409, {"error": "Wait for the current audio message task before changing settings."})
            return
        # Keep cross-controller pending checks and queue writes atomic.
        with self.server.mutation_lock:
            if path != "/api/admin/logout" and self.server.messages.active:
                self.json_response(409, {"error": "Wait for the current audio message task before changing settings."})
                return
            if path != "/api/admin/logout" and self.server.programming.status()["pending"]:
                self.json_response(409, {"error": "A radio programming task is in progress. Wait for its result before changing settings."})
                return
            if path != "/api/admin/logout" and self.server.dtmf.code_status(probe=False)["pending"]:
                self.json_response(409, {"error": "A DTMF password change is pending. Wait for its result before changing settings."})
                return
            try:
                if path == "/api/admin/logout":
                    self.server.auth.logout(token)
                    self.json_response(200, self.server.auth.session(None), {"Set-Cookie": "repeater_admin=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"})
                elif path in ("/api/messages/delete", "/api/messages/broadcast"):
                    fields = {"id", "sha256"}
                    if path.endswith("/broadcast"):
                        fields |= {"pair_id", "catalog_digest", "invocation_id"}
                    if (set(data) != fields or not isinstance(data["id"], str) or not re.fullmatch(r"[0-9a-f]{32}", data["id"])
                            or not isinstance(data["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", data["sha256"])):
                        raise ValueError("Send only the reviewed message and current broadcast identifiers")
                    if path.endswith("/delete"):
                        self.server.messages.delete_message(data["id"], data["sha256"])
                        code = 200
                    else:
                        if (not isinstance(data["pair_id"], str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,15}", data["pair_id"])
                                or not isinstance(data["catalog_digest"], str) or not re.fullmatch(r"[0-9a-f]{64}", data["catalog_digest"])
                                or not isinstance(data["invocation_id"], str) or not re.fullmatch(r"[0-9a-f]{32}", data["invocation_id"])):
                            raise ValueError("Review the current channel pair and live repeater before broadcasting")
                        radio = self.server.programming.status()
                        pair = verified_pair(radio)
                        if not pair or pair["pair_id"] != data["pair_id"] or radio["catalog_digest"] != data["catalog_digest"]:
                            self.json_response(409, {"error": "The verified channel pair changed. Review the message broadcast again."})
                            return
                        self.server.messages.request_broadcast(data["id"], data["sha256"], data["pair_id"], data["catalog_digest"], data["invocation_id"])
                        code = 202
                    self.json_response(code, {"radio_messages": radio_message_status(self.server)})
                elif path == "/api/station-identification":
                    if set(data) != {"enabled"}:
                        raise ValueError("Send only the automatic identification enabled flag")
                    if self.server.updates.status()["pending"] or self.server.wifi.status()["pending"]:
                        self.json_response(409, {"error": "Wait for the current maintenance task before changing identification."})
                        return
                    with self.server.settings_lock:
                        state = self.server.station_identification.set_enabled(data["enabled"])
                    self.json_response(200, {"station_identification": state})
                elif path == "/api/dtmf-control":
                    if set(data) == {"enabled"}:
                        state = self.server.dtmf.set_enabled(data["enabled"])
                    elif set(data) == {"status_announcements"}:
                        state = self.server.dtmf.set_announcements(data["status_announcements"])
                    else:
                        raise ValueError("Send only the enabled or status_announcements flag")
                    self.json_response(200, {"dtmf_control": state})
                elif path == "/api/dtmf-password":
                    if set(data) != {"code", "admin_password"}:
                        raise ValueError("Send only the new DTMF code and current admin password")
                    self.server.auth.reauthenticate(token, self.headers.get("X-CSRF-Token"),
                                                    data.pop("admin_password"), self.client_address[0])
                    state = self.server.dtmf.request_code(data.pop("code"))
                    self.json_response(202, {"code_change": state})
                elif path == "/api/recording":
                    if set(data) != {"enabled"} or type(data["enabled"]) is not bool:
                        raise ValueError("enabled must be true or false")
                    self.server.hub.set_recording(data["enabled"])
                    self.json_response(200, self.server.hub.status())
                elif path == "/api/settings":
                    if any(key in data and data[key] != self.server.settings.get()[key]
                           for key in ("data_decode_enabled", "sstv_decode_enabled")):
                        raise ValueError("Use the Data tab controls to change packet or picture decoding")
                    if "automatic_station_id" in data and data["automatic_station_id"] != self.server.settings.get()["automatic_station_id"]:
                        raise ValueError("Review the station identification confirmation before changing automatic identification")
                    if any(key in data and data[key] != self.server.settings.get()[key]
                           for key in ("dtmf_enabled", "dtmf_status_announcements")):
                        raise ValueError("Review the DTMF confirmation before changing its enabled state or status announcements")
                    with self.server.settings_lock:
                        settings = self.server.settings.update(data)
                        self.server.hub.update_settings(settings)
                        self.server.data_decoder.set_enabled(settings["data_decode_enabled"])
                        self.server.sstv_decoder.set_enabled(settings["sstv_decode_enabled"])
                    self.json_response(200, {"settings": settings})
                elif path == "/api/wifi-visibility":
                    if set(data) != {"hidden"}:
                        raise ValueError("Send only the hidden flag")
                    state = self.server.wifi.request(data["hidden"])
                    self.json_response(202, {"wifi_visibility": state})
                elif path == "/api/software-updates/check":
                    if data:
                        raise ValueError("Send an empty object to check for updates")
                    state = self.server.updates.request_check()
                    self.json_response(202, {"software_updates": state})
                elif path == "/api/software-updates/install":
                    if set(data) != {"commit"}:
                        raise ValueError("Send only the checked update commit")
                    state = self.server.updates.request_install(data["commit"])
                    self.json_response(202, {"software_updates": state})
                elif path == "/api/radio-pair":
                    if set(data) != {"pair_id", "password", "catalog_digest"}:
                        raise ValueError("Send the selected pair, reviewed catalog digest and admin password")
                    if not isinstance(data["catalog_digest"], str) or not re.fullmatch(r"[0-9a-f]{64}", data["catalog_digest"]):
                        raise ValueError("Review the configured channel catalog before changing pairs")
                    if self.server.updates.status()["pending"] or self.server.wifi.status()["pending"]:
                        self.json_response(409, {"error": "Wait for the current software or Wi-Fi task before changing the pair."})
                        return
                    try:
                        self.server.auth.reauthenticate(token, self.headers.get("X-CSRF-Token"),
                                                        data.pop("password"), self.client_address[0])
                    except PermissionError as error:
                        self.json_response(400, {"error": str(error)})
                        return
                    state = self.server.programming.request_pair(data["pair_id"], catalog_digest=data["catalog_digest"])
                    self.json_response(202, {"radio_programming": state})
                elif path == "/api/radio-programming":
                    repair = data.get("action") == "repair-upload"
                    program = data.get("action") == "program"
                    if set(data) != ({"role", "action", "pending_id"} if repair else {"role", "action", "catalog_digest"} if program else {"role", "action"}):
                        raise ValueError("Send only the radio role and programming action")
                    if (data["role"] not in ("rx", "tx", "programmer")
                            or data["action"] not in ("inspect", "program", "verify", "repair-upload")
                            or data["role"] == "programmer" and data["action"] not in ("verify", "repair-upload")):
                        raise ValueError("Choose a configured radio and a supported programming action")
                    if repair and (not isinstance(data["pending_id"], str)
                                   or not re.fullmatch(r"[0-9a-f]{32}", data["pending_id"])):
                        raise ValueError("Send the exact unfinished upload identifier after reviewing repair")
                    if program and (not isinstance(data["catalog_digest"], str) or not re.fullmatch(r"[0-9a-f]{64}", data["catalog_digest"])):
                        raise ValueError("Review the configured channel catalog before programming")
                    if self.server.updates.status()["pending"] or self.server.wifi.status()["pending"]:
                        self.json_response(409, {"error": "Wait for the current software or Wi-Fi task before programming a radio."})
                        return
                    state = (self.server.programming.request(data["role"], data["action"], data["pending_id"])
                             if repair else self.server.programming.request(data["role"], data["action"], catalog_digest=data["catalog_digest"])
                             if program else self.server.programming.request(data["role"], data["action"]))
                    self.json_response(202, {"radio_programming": state})
                elif path in ("/api/radio-editor/read", "/api/radio-editor/write"):
                    fields = {"role"} if path.endswith("/read") else {"role", "base_id", "changes", "settings"}
                    allowed_fields = (fields, fields | {"model"}) if path.endswith("/read") else (fields, fields | {"supersede_id"})
                    if set(data) not in allowed_fields or data["role"] not in ("rx", "tx", "programmer"):
                        raise ValueError("Send the radio role and only the requested editor fields")
                    if "model" in data and (data["role"] != "programmer" or data["model"] not in MODELS):
                        raise ValueError("Choose a supported external radio model")
                    if "supersede_id" in data and (not isinstance(data["supersede_id"], str)
                            or not re.fullmatch(r"[0-9a-f]{32}", data["supersede_id"])):
                        raise ValueError("Send the exact unfinished upload identifier after confirming recovery")
                    if self.server.updates.status()["pending"] or self.server.wifi.status()["pending"]:
                        self.json_response(409, {"error": "Wait for the current software or Wi-Fi task before editing a radio."})
                        return
                    if path.endswith("/read"):
                        state = (self.server.programming.request_editor(data["role"], "editor-read", model=data["model"])
                                 if "model" in data else self.server.programming.request_editor(data["role"], "editor-read"))
                    else:
                        arguments = [data["role"], "editor-write", data["base_id"], data["changes"], data["settings"]]
                        if "supersede_id" in data:
                            arguments.append(data["supersede_id"])
                        state = self.server.programming.request_editor(*arguments)
                    self.json_response(202, {"radio_programming": state})
            except MessageError as error:
                self.json_response(409, {"error": str(error)})
            except PermissionError as error:
                self.json_response(403, {"error": str(error)})
            except (ValueError, OSError) as error:
                self.json_response(400, {"error": str(error)})

    def upload_message(self):
        # Authenticate before reading any part of a potentially large body.
        if not self.server.auth.authorize(self.session_token(), self.headers.get("X-CSRF-Token")):
            self.close_connection = True
            self.json_response(403, {"error": "Sign in as admin before uploading a radio message"})
            return
        try:
            lengths = self.headers.get_all("Content-Length", [])
            if (self.headers.get("Transfer-Encoding") is not None or len(lengths) != 1
                    or not re.fullmatch(r"[0-9]+", lengths[0]) or not 0 < int(lengths[0]) <= MAX_UPLOAD_BYTES):
                raise ValueError("Upload a nonempty audio file no larger than 8 MiB with a fixed Content-Length")
            if self.headers.get_content_type() != "application/octet-stream":
                raise ValueError("Send the audio file as application/octet-stream")
            encoded = self.headers.get("X-File-Name", "")
            if len(encoded) > 1024 or re.search(r"%(?![0-9a-fA-F]{2})", encoded):
                raise ValueError("Send a valid encoded audio filename")
            name, _extension = upload_name(unquote(encoded, encoding="utf-8", errors="strict"))
            with self.server.mutation_lock:
                reason = message_busy_reason(self.server)
                if reason or self.server.messages.active:
                    self.close_connection = True
                    self.json_response(409, {"error": reason or "Wait for the current audio message task."})
                    return
            size = int(lengths[0])
            self.connection.settimeout(15)
            payload = self.rfile.read(size)
            if len(payload) != size:
                raise ValueError("The audio upload was incomplete")
            if not self.server.auth.authorize(self.session_token(), self.headers.get("X-CSRF-Token")):
                self.close_connection = True
                self.json_response(403, {"error": "The admin session changed. Sign in again before uploading."})
                return
            # The module reserves under the shared lock, then releases it for
            # bounded conversion while its active flag excludes mutations.
            entry = self.server.messages.store_upload(name, payload)
            self.json_response(201, {"radio_messages": radio_message_status(self.server), "message": entry})
        except MessageError as error:
            self.close_connection = True
            self.json_response(400, {"error": str(error)})
        except (TimeoutError, OSError):
            self.close_connection = True
            self.json_response(400, {"error": "The audio upload could not be read or saved. Check the connection and try again."})
        except (ValueError, UnicodeError) as error:
            self.close_connection = True
            self.json_response(400, {"error": str(error)})

    def serve(self, head=False):
        path = urlsplit(self.path).path
        if path == "/api/health":
            # Readiness for the updater must not wait for slow hardware inventory.
            ready = static_files_ready()
            self.json_response(200 if ready else 503, {"version": VERSION, "ready": ready}, head=head)
            return
        if path == "/api/activity":
            self.json_response(200, radio_activity(self.server), head=head)
            return
        if path == "/api/recordings/status":
            if not self.server.auth.session(self.session_token())["authenticated"]:
                self.json_response(403, {"error": "Sign in as admin to manage saved recordings"}, head=head)
                return
            if urlsplit(self.path).query:
                self.json_response(400, {"error": "Recording status does not accept query fields"}, head=head)
                return
            self.json_response(200, recording_status(self.server), head=head)
            return
        if path.startswith("/api/sstv/"):
            self.serve_sstv(path, head)
            return
        if path in ("/api/data/status", "/api/data/export"):
            if not self.server.auth.session(self.session_token())["authenticated"]:
                self.json_response(403, {"error": "Sign in as admin to view received data"}, head=head)
                return
            if path == "/api/data/status":
                self.json_response(200, {"data": data_status(self.server), "sstv": sstv_status(self.server)}, head=head)
                return
            try:
                query = urlsplit(self.path).query
                if len(query) > 32:
                    raise ValueError("Choose JSON or text export")
                fields = parse_qs(query, strict_parsing=True, keep_blank_values=True)
                if set(fields) != {"format"} or len(fields["format"]) != 1 or fields["format"][0] not in ("json", "txt"):
                    raise ValueError("Choose JSON or text export")
                payload, filename, content_type = self.server.data_decoder.export(fields["format"][0])
                self.headers_for(200, content_type, len(payload),
                    {"Content-Disposition": "attachment; filename*=UTF-8''" + quote(filename, safe="")})
                if not head:
                    self.wfile.write(payload)
            except ValueError:
                self.json_response(400, {"error": "Choose JSON or text export"}, head=head)
            except OSError:
                self.json_response(503, {"error": "The received-data export is temporarily unavailable"}, head=head)
            return
        if path == "/api/software-updates/status":
            if not self.server.auth.session(self.session_token())["authenticated"]:
                self.json_response(403, {"error": "Sign in as admin to view software update progress"}, head=head)
                return
            self.json_response(200, {"software_updates": self.server.updates.status()}, head=head)
            return
        if path == "/api/messages":
            if not self.server.auth.session(self.session_token())["authenticated"]:
                self.json_response(403, {"error": "Sign in as admin to view radio messages"}, head=head)
                return
            self.json_response(200, {"radio_messages": radio_message_status(self.server)}, head=head)
            return
        preview = re.fullmatch(r"/api/messages/([0-9a-f]{32})/audio", path)
        if preview:
            self.serve_message_preview(preview[1], head)
            return
        if path == "/api/radio-programming/status":
            self.json_response(200, {"radio_programming": self.server.programming.status()}, head=head)
            return
        if path == "/api/status":
            data = self.server.monitor.status()
            data.update(self.server.hub.status())
            data["settings"] = self.server.settings.get()
            data["dtmf_control"] = self.server.dtmf.status()
            data["station_identification"] = self.server.station_identification.status()
            data["wifi_visibility"] = self.server.wifi.status()
            data["software_updates"] = self.server.updates.status()
            data["radio_programming"] = self.server.programming.status()
            radio = data["radio_programming"]
            data.setdefault("setup", {})["radio_plan"] = public_radio_plan(radio)
            self.server.data_radio_plan = data["setup"]["radio_plan"]
            data["admin"] = self.server.auth.session(self.session_token())
            data["admin"].pop("csrf_token", None)
            if not data["admin"]["authenticated"]:
                for item in data.get("recordings", {}).get("files", []):
                    item.pop("delete_revision", None)
                    item.pop("can_delete", None)
            data["radio_messages"] = radio_message_status(self.server, radio) if data["admin"]["authenticated"] else {"admin_required": True}
            data["data"] = data_status(self.server) if data["admin"]["authenticated"] else {"admin_required": True}
            data["sstv"] = sstv_status(self.server) if data["admin"]["authenticated"] else {"admin_required": True}
            data["radio_activity"] = radio_activity(self.server)
            data["setup"]["notes"] = ["Channel values come from the last verified pair." if verified_pair(radio)
                                      else "Configure and verify a channel pair to confirm the displayed settings.",
                                      "Activity dots show receiver squelch and the Pi's transmit command."]
            self.json_response(200, data, head=head)
            return
        if path == "/api/session":
            self.json_response(200, self.server.auth.session(self.session_token()), head=head)
            return
        if path == "/api/radio-editor":
            if not self.server.auth.session(self.session_token())["authenticated"]:
                self.json_response(403, {"error": "Sign in as admin to read radio settings"}, head=head)
                return
            try:
                query = urlsplit(self.path).query
                if len(query) > 128:
                    raise ValueError("Choose RX or TX")
                values = parse_qs(query, strict_parsing=True, keep_blank_values=True)
                if (set(values) not in ({"role"}, {"role", "model"}) or len(values["role"]) != 1
                        or values["role"][0] not in ("rx", "tx", "programmer")):
                    raise ValueError("Choose RX or TX")
                role = values["role"][0]
                if "model" in values and (role != "programmer" or len(values["model"]) != 1
                                           or values["model"][0] not in MODELS):
                    raise ValueError("Choose a supported external radio model")
                snapshot = (self.server.programming.editor_snapshot(role, model=values["model"][0])
                            if "model" in values else self.server.programming.editor_snapshot(role))
                self.json_response(200, {"editor": snapshot,
                                       "radio_programming": self.server.programming.status()}, head=head)
            except ValueError as error:
                self.json_response(400, {"error": str(error)}, head=head)
            return
        if path == "/api/settings":
            self.json_response(200, {"settings": self.server.settings.get()}, head=head)
            return
        if path == "/api/live.pcm":
            self.serve_live(head)
            return
        if path in STATIC_ROUTES:
            target = STATIC / STATIC_ROUTES[path]
            try:
                data = target.read_bytes()
            except OSError:
                self.send_error(404)
                return
            mime = mimetypes.guess_type(target)[0] or "application/octet-stream"
            self.headers_for(200, mime + "; charset=utf-8", len(data))
            if not head:
                self.wfile.write(data)
            return
        if path.startswith("/recordings/"):
            self.serve_recording(unquote(path[len("/recordings/"):]), head)
            return
        self.send_error(404)

    def serve_sstv(self, path, head):
        if not self.server.auth.session(self.session_token())["authenticated"]:
            self.json_response(403, {"error": "Sign in as admin to view received pictures"}, head=head)
            return
        query = urlsplit(self.path).query
        if path == "/api/sstv/status":
            if query:
                self.json_response(400, {"error": "SSTV status does not accept query fields"}, head=head)
            else:
                self.json_response(200, {"sstv": sstv_status(self.server)}, head=head)
            return
        current = path == "/api/sstv/current.png"
        saved = re.fullmatch(r"/api/sstv/images/([0-9a-f]{32})\.png", path)
        if not current and saved is None:
            self.json_response(404, {"error": "The received picture was not found"}, head=head)
            return
        try:
            if len(query) > 96:
                raise ValueError("Unsupported picture query")
            fields = parse_qs(query, strict_parsing=True, keep_blank_values=True)
            download = False
            if current:
                if (set(fields) != {"id", "revision"} or any(len(items) != 1 for items in fields.values())
                        or not re.fullmatch(r"[0-9a-f]{32}", fields["id"][0])
                        or not re.fullmatch(r"[1-9][0-9]{0,9}", fields["revision"][0])
                        or not 0 < int(fields["revision"][0]) < 2**31):
                    raise ValueError("A current picture ID and revision are required")
                identifier = None
                current_id, expected_revision = fields["id"][0], int(fields["revision"][0])
            else:
                if fields not in ({}, {"download": ["1"]}):
                    raise ValueError("Unsupported picture query")
                download = bool(fields)
                identifier, current_id, expected_revision = saved[1], None, None
        except ValueError:
            self.json_response(400, {"error": "Choose a current preview or a saved PNG picture"}, head=head)
            return
        try:
            payload, revision = self.server.sstv_decoder.image(identifier, revision=expected_revision, current_id=current_id)
            if (not isinstance(payload, bytes) or not 32 <= len(payload) <= 2 * 1024 * 1024
                    or not payload.startswith(b"\x89PNG\r\n\x1a\n")
                    or type(revision) is not int or not 0 < revision < 2**31
                    or current and revision != expected_revision):
                raise OSError("Invalid private image response")
        except FileNotFoundError:
            self.json_response(404, {"error": "The received picture is no longer available"}, head=head)
            return
        except ValueError:
            self.json_response(409, {"error": "The live picture changed. Refresh its preview."}, head=head)
            return
        except OSError:
            self.json_response(503, {"error": "The received-picture store is temporarily unavailable"}, head=head)
            return
        filename = "sstv-" + (current_id if current else identifier) + ".png"
        self.headers_for(200, "image/png", len(payload),
                         {"Content-Disposition": ("attachment" if download else "inline")
                          + "; filename*=UTF-8''" + quote(filename, safe=""),
                          "X-SSTV-Revision": str(revision)})
        if not head:
            try:
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError, OSError):
                self.close_connection = True

    def serve_message_preview(self, identifier, head):
        if not self.server.auth.session(self.session_token())["authenticated"]:
            self.json_response(403, {"error": "Sign in as admin to preview radio messages"}, head=head)
            return
        try:
            stream, entry = self.server.messages.open_preview(identifier)
        except (MessageError, OSError):
            self.json_response(404, {"error": "The message preview is unavailable"}, head=head)
            return
        with stream:
            size = entry["bytes"]
            try:
                span = parse_range(self.headers.get("Range"), size)
            except ValueError:
                self.headers_for(416, "audio/wav", 0, {"Content-Range": f"bytes */{size}"})
                return
            extra = {"Accept-Ranges": "bytes", "Content-Disposition": "inline; filename*=UTF-8''" + quote(Path(entry["name"]).stem + ".wav", safe="")}
            if span:
                first, last = span
                length = last - first + 1
                extra["Content-Range"] = f"bytes {first}-{last}/{size}"
            else:
                first, length = 0, size
            self.headers_for(206 if span else 200, "audio/wav", length, extra)
            if not head:
                stream.seek(first)
                while length:
                    data = stream.read(min(65536, length))
                    if not data:
                        break
                    self.wfile.write(data)
                    length -= len(data)

    def serve_live(self, head):
        live = self.server.hub.status()["live_audio"]
        if not live["configured"] or live.get("last_error"):
            self.json_response(503, {"error": live.get("last_error") or "Receiver audio feed has not been enabled"}, head=head)
            return
        if head:
            self.headers_for(200, "application/octet-stream", 0,
                             {"X-Audio-Format": "f32le", "X-Sample-Rate": str(live["sample_rate"])})
            return
        try:
            key, packets = self.server.hub.subscribe()
        except ValueError as error:
            self.json_response(503, {"error": str(error)})
            return
        self.connection.settimeout(10)
        self.headers_for(200, "application/octet-stream", None,
                         {"Transfer-Encoding": "chunked", "X-Audio-Format": "f32le",
                          "X-Sample-Rate": str(live["sample_rate"]), "X-Audio-Channels": "1"})
        last_packet = time.monotonic()
        try:
            while True:
                try:
                    packet = packets.get(timeout=1)
                except queue.Empty:
                    if time.monotonic() - last_packet > 15:
                        break
                    continue
                if packet is None:
                    break
                if packet:
                    last_packet = time.monotonic()
                    self.wfile.write(f"{len(packet):x}\r\n".encode() + packet + b"\r\n")
                    self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
        except (BrokenPipeError, ConnectionResetError, OSError):
            self.close_connection = True
        finally:
            self.server.hub.unsubscribe(key)

    def serve_recording(self, name, head):
        if not name or "/" in name or "\\" in name or "\x00" in name or name in (".", ".."):
            self.send_error(404)
            return
        path = self.server.monitor.recordings / name
        if path.suffix.lower() not in MEDIA_TYPES or path.is_symlink():
            self.send_error(404)
            return
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
            import stat
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                os.close(fd)
                self.send_error(404)
                return
            stream = os.fdopen(fd, "rb")
        except (OSError, ValueError):
            self.send_error(404)
            return
        with stream:
            size = info.st_size
            try:
                span = parse_range(self.headers.get("Range"), size)
            except ValueError:
                self.headers_for(416, "text/plain", 0, {"Content-Range": f"bytes */{size}"})
                return
            extra = {"Accept-Ranges": "bytes", "Content-Disposition": "inline; filename*=UTF-8''" + quote(name, safe="")}
            if span:
                first, last = span
                extra["Content-Range"] = f"bytes {first}-{last}/{size}"
                length = last - first + 1
            else:
                first, length = 0, size
            self.headers_for(206 if span else 200, MEDIA_TYPES[path.suffix.lower()], length, extra)
            if not head:
                stream.seek(first)
                while length:
                    data = stream.read(min(65536, length))
                    if not data:
                        break
                    self.wfile.write(data)
                    length -= len(data)


class DashboardServer(ThreadingHTTPServer):
    def server_close(self):
        if hasattr(self, "sstv_decoder"):
            self.sstv_decoder.close()
        if hasattr(self, "data_decoder"):
            self.data_decoder.close()
        if hasattr(self, "dtmf"):
            self.dtmf.close()
        if hasattr(self, "hub"):
            self.hub.close()
        super().server_close()


def make_server(host, port, recordings, state_dir=None, admin_file=None):
    server = DashboardServer((host, port), Handler)
    server.daemon_threads = True
    server.monitor = Monitor(recordings)
    state_dir = Path(state_dir) if state_dir else Path(recordings).parent / "dashboard-state"
    try:
        server.settings = SettingsStore(state_dir / "settings.json")
        server.settings_lock = threading.RLock()
        server.mutation_lock = threading.RLock()
        server.auth = AdminAuth(admin_file or state_dir / "admin.json")
        server.wifi = WifiVisibility(state_dir)
        server.updates = SoftwareUpdates(state_dir, current_version=VERSION)
        server.programming = RadioProgramming(state_dir)
        server.native_state = NativeState()
        server.hub = AudioHub(recordings, server.settings.get(), live_mode_probe=server.native_state.live)
        server.hub.start()
        server.data_radio_plan = None
        server.data_decoder = DataDecoder(state_dir, server.hub, enabled=server.settings.get()["data_decode_enabled"])
        server.data_decoder.start()
        server.sstv_decoder = SstvDecoder(state_dir, server.hub, enabled=server.settings.get()["sstv_decode_enabled"])
        server.sstv_decoder.start()
        server.dtmf = DtmfControl(server, state_dir=state_dir, prompt_feedback=server.native_state.live)
        server.messages = RadioMessages(state_dir, native_state_reader=server.native_state.read,
                                       busy_check=lambda: message_busy_reason(server), mutation_lock=server.mutation_lock)
        bridge_started = server.dtmf.start()
        projection_path = server.dtmf.socket_path.with_name("station-identification.json") if bridge_started else None
        server.station_identification = StationIdentification(server.settings, projection_path=projection_path)
    except Exception:
        server.server_close()
        raise
    return server


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=80)
    parser.add_argument("--recordings", default="/var/lib/svxlink/recordings")
    parser.add_argument("--state-dir")
    parser.add_argument("--admin-file")
    args = parser.parse_args()
    server = make_server(args.host, args.port, args.recordings, args.state_dir, args.admin_file)
    # systemd stops the dashboard during an update. Finish the current WAV before
    # replacing code; shutdown() must run outside the serve_forever thread.
    def request_shutdown(signum, frame):
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)
    print(f"Repeater Box dashboard: http://{args.host}:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
