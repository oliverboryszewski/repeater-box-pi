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
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import socket
import subprocess
import queue
import time
import threading
from http.cookies import SimpleCookie, CookieError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlsplit
from controls import SettingsStore, AdminAuth
from audio_hub import AudioHub
from dtmf_control import DtmfControl
from wifi_visibility import WifiVisibility
from software_updates import SoftwareUpdates
from radio_programming import RadioProgramming
from radio_editor import MODELS

VERSION = "0.10.0"
STATIC = Path(__file__).parent / "static"
MEDIA_TYPES = {".wav": "audio/wav", ".ogg": "audio/ogg", ".mp3": "audio/mpeg", ".flac": "audio/flac"}


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
            if path.is_symlink() or not path.is_file() or path.suffix.lower() not in MEDIA_TYPES:
                continue
            stat = path.stat()
            files.append({"id": path.name, "name": path.name, "bytes": stat.st_size,
                          "modified_at": dt.datetime.fromtimestamp(stat.st_mtime, dt.timezone.utc).isoformat(),
                          "url": "/recordings/" + quote(path.name, safe="")})
    except OSError:
        pass
    return sorted(files, key=lambda item: item["modified_at"], reverse=True)


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
        state = command(["systemctl", "show", "repeater-engine.service", "--property=ActiveState", "--value"]) if linux else None
        rx_state = command(["systemctl", "show", "repeater-rx-monitor.service", "--property=ActiveState", "--value"]) if linux else None
        live_state = command(["systemctl", "show", "repeater-live.service", "--property=ActiveState", "--value"]) if linux else None
        rf_enabled = live_state == "active"
        if rx_state == "active":
            state = rx_state
        if rf_enabled:
            state = live_state
        mode = "repeater" if rf_enabled else "receive_only" if rx_state == "active" else "bench"
        installed = bool(shutil.which("svxlink"))
        capture = audio_cards(command(["arecord", "-l"])) if linux else []
        playback = audio_cards(command(["aplay", "-l"])) if linux else []
        serial = []
        for port in sorted(Path("/dev/serial/by-id").glob("*")):
            serial.append({"path": str(port), "target": os.path.realpath(port)})
        aioc_count = count_aioc_usb_devices() if linux else None
        clock = command(["timedatectl", "show", "--property=NTPSynchronized", "--value"]) if linux else None
        recordings = media_files(self.recordings)
        notes = ["The live repeater service is active." if rf_enabled else "Transmission is disabled in bench and receive-only modes.",
                 "Channel values are the planned radio settings, not live radio telemetry.",
                 "The dashboard does not measure the radio's PTT state."]
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
                            "transmitting": None},
                "audio": {"capture": capture, "playback": playback, "serial_ports": serial,
                          "usb_aioc_count": aioc_count},
                "recordings": {"count": len(recordings), "total_bytes": sum(f["bytes"] for f in recordings),
                               "files": recordings},
                "setup": {"phase": "Live repeater" if rf_enabled else "Receive monitor" if rx_state == "active" else "bench", "rf_enabled": rf_enabled,
                          "radio_plan": None, "notes": notes},
                "clock_synchronized": {"yes": True, "no": False}.get(clock)}


class Handler(BaseHTTPRequestHandler):
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
        try:
            size = int(self.headers.get("Content-Length", "0"))
            maximum = 65536 if path == "/api/radio-editor/write" else 4096
            if size < 0 or size > maximum:
                raise ValueError("Request body too large")
            if self.headers.get_content_type() != "application/json":
                raise ValueError("Send JSON")
            self.connection.settimeout(10)
            data = json.loads(self.rfile.read(size))
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
        if path not in ("/api/admin/logout", "/api/recording", "/api/settings", "/api/wifi-visibility", "/api/software-updates/check", "/api/software-updates/install", "/api/radio-programming", "/api/radio-pair", "/api/radio-editor/read", "/api/radio-editor/write", "/api/dtmf-control", "/api/dtmf-password"):
            self.json_response(404, {"error": "Unknown endpoint"})
            return
        if not self.server.auth.authorize(token, self.headers.get("X-CSRF-Token")):
            self.json_response(403, {"error": "Sign in as admin before changing settings or recording"})
            return
        # Keep cross-controller pending checks and queue writes atomic.
        with self.server.mutation_lock:
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
                           for key in ("dtmf_enabled", "dtmf_status_announcements")):
                        raise ValueError("Review the DTMF confirmation before changing its enabled state or status announcements")
                    with self.server.settings_lock:
                        settings = self.server.settings.update(data)
                        self.server.hub.update_settings(settings)
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
            except PermissionError as error:
                self.json_response(403, {"error": str(error)})
            except (ValueError, OSError) as error:
                self.json_response(400, {"error": str(error)})

    def serve(self, head=False):
        path = urlsplit(self.path).path
        if path == "/api/health":
            # Readiness for the updater must not wait for slow hardware inventory.
            self.json_response(200, {"version": VERSION, "ready": True}, head=head)
            return
        if path == "/api/radio-programming/status":
            self.json_response(200, {"radio_programming": self.server.programming.status()}, head=head)
            return
        if path == "/api/status":
            data = self.server.monitor.status()
            data.update(self.server.hub.status())
            data["settings"] = self.server.settings.get()
            data["dtmf_control"] = self.server.dtmf.status()
            data["wifi_visibility"] = self.server.wifi.status()
            data["software_updates"] = self.server.updates.status()
            data["radio_programming"] = self.server.programming.status()
            radio = data["radio_programming"]
            data.setdefault("setup", {})["radio_plan"] = None
            pair = radio.get("pair") or {}
            if pair.get("stored_verified") and pair.get("plan"):
                plan = pair["plan"]
                data["setup"]["radio_plan"] = {"rx_mhz": f"{plan['rx_hz'] / 1000000:.6f}",
                    "tx_mhz": f"{plan['tx_hz'] / 1000000:.6f}", "tone_hz": plan["tone_hz"],
                    "power": "HIGH (radio setting)"}
            elif radio.get("plan"):
                plan = radio["plan"]
                data["setup"]["radio_plan"] = {"rx_mhz": plan["rx"]["frequency_mhz"],
                    "tx_mhz": plan["tx"]["frequency_mhz"], "tone_hz": plan["rx"]["receive_tone_hz"],
                    "power": "HIGH (radio setting)"}
            data["admin"] = self.server.auth.session(self.session_token())
            data["admin"].pop("csrf_token", None)
            data["setup"]["notes"][-1] = "Live listening and RX audio recording are available after the receiver feed is commissioned."
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
        static = {"/": "index.html", "/index.html": "index.html", "/app.js": "app.js", "/style.css": "style.css",
                  "/audio-visualizer.js": "audio-visualizer.js", "/audio-visualizer.css": "audio-visualizer.css",
                  "/radio-editor": "radio-editor.html", "/radio-editor.js": "radio-editor.js",
                  "/radio-editor.css": "radio-editor.css"}
        if path in static:
            target = STATIC / static[path]
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
        server.hub = AudioHub(recordings, server.settings.get())
        server.hub.start()
        server.dtmf = DtmfControl(server, state_dir=state_dir)
        server.dtmf.start()
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
