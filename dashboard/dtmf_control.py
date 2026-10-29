"""Local authenticated DTMF bridge; fixed status and recording actions only.

SvxLink owns radio audio and spoken responses. This process never opens ALSA,
serial, PTT or command PTYs, and never publishes the private control-code record.
"""
from collections import deque
from controls import password_record, write_json
from wifi_visibility import command as system_command, is_pi
import grp
import hashlib
import hmac
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import socket
import stat
import struct
import threading
import time

RESULTS = frozenset(("authenticated", "choose_status", "choose_recording", "status_on", "status_off",
                     "recording_on", "recording_off", "cancelled", "invalid", "expired",
                     "disabled", "denied", "busy", "unavailable"))
CODE_RE = re.compile(r"[0-9ABCD]{6}\Z")
CONFIG_PATH = Path("/etc/repeater-box/dtmf.json")
SOCKET_PATH = Path("/run/repeater-box-control/dtmf.sock")


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate DTMF protocol field")
        value[key] = item
    return value


class DtmfControl:
    def __init__(self, server, config_file=CONFIG_PATH, socket_path=SOCKET_PATH,
                 owner_uid=0, group_id=None, allowed_uids=None, clock=time.monotonic,
                 state_dir="/var/lib/repeater-dashboard", code_supported=None, runner=None):
        self.server = server
        self.config_file, self.socket_path = Path(config_file), Path(socket_path)
        self.owner_uid, self.clock = owner_uid, clock
        self.state_dir = Path(state_dir)
        self.code_supported = is_pi() if code_supported is None else code_supported
        self.run = runner or system_command
        self.rf_session = None
        self.group_id = group_id
        if self.group_id is None:
            try:
                self.group_id = grp.getgrnam("repeater-control").gr_gid
            except KeyError:
                pass
        if allowed_uids is None:
            allowed_uids = {0}
            try:
                allowed_uids.add(pwd.getpwnam("svxlink").pw_uid)
            except KeyError:
                pass
        self.allowed_uids = frozenset(allowed_uids)
        self.failures, self.attempts = deque(), deque()
        self.security_lock = threading.Lock()
        self.rf_command_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.listener, self.thread = None, None
        self.socket_identity = None

    def _record(self):
        try:
            descriptor = os.open(self.config_file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
            with os.fdopen(descriptor, "rb") as stream:
                info = os.fstat(stream.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != self.owner_uid
                        or info.st_mode & 0o027 or info.st_size > 4096):
                    return None
                value = json.loads(stream.read(4097), object_pairs_hook=unique_object)
            if (not isinstance(value, dict) or set(value) != {"schema", "algorithm", "iterations", "salt", "digest"}
                    or type(value.get("schema")) is not int or value["schema"] != 1
                    or value.get("algorithm") != "pbkdf2_sha256"
                    or type(value.get("iterations")) is not int or not 100000 <= value["iterations"] <= 1000000
                    or not isinstance(value.get("salt"), str) or not re.fullmatch(r"[0-9a-f]{32}", value["salt"])
                    or not isinstance(value.get("digest"), str) or not re.fullmatch(r"[0-9a-f]{64}", value["digest"])):
                return None
            return value
        except (OSError, ValueError, TypeError):
            return None

    def _private_json(self, path, owner, mode_mask):
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
            with os.fdopen(descriptor, "rb") as stream:
                info = os.fstat(stream.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != owner
                        or info.st_mode & mode_mask or info.st_size > 8192):
                    return None
                return json.loads(stream.read(8193), object_pairs_hook=unique_object)
        except (OSError, ValueError, TypeError):
            return None

    def code_status(self, probe=True):
        # Only public progress leaves this method. The queued PBKDF2 record is
        # private and is removed by the root worker after its durable result.
        request = self._private_json(self.state_dir / "dtmf-code-request.json", os.geteuid(), 0o077)
        result = self._private_json(self.state_dir / "dtmf-code-result.json", self.owner_uid, 0o027)
        valid_id = lambda value: isinstance(value, str) and re.fullmatch(r"[0-9a-f]{32}", value)
        request_id = (request.get("request_id") if isinstance(request, dict)
                      and set(request) == {"schema", "request_id", "record"}
                      and type(request.get("schema")) is int and request["schema"] == 1
                      and valid_id(request.get("request_id")) else None)
        valid_result = (isinstance(result, dict)
                        and set(result) == {"schema", "request_id", "success", "message", "monitor_restart_required"}
                        and type(result.get("schema")) is int and result["schema"] == 1
                        and valid_id(result.get("request_id"))
                        and type(result.get("success")) is bool
                        and type(result.get("monitor_restart_required")) is bool)
        matched = valid_result and (request_id is None or result["request_id"] == request_id)
        pending = request_id is not None and not matched
        available = probe and self.code_supported and self.run(["/usr/bin/systemctl", "is-active", "repeater-dtmf-code.path"]) == "active"
        state = {"available": available, "pending": pending,
                 "request_id": request_id or (result["request_id"] if matched else None),
                 "success": result["success"] if matched else None,
                 "monitor_restart_required": result["monitor_restart_required"] if matched else False,
                 "message": "Changing the private DTMF password." if pending else None}
        if matched:
            state["message"] = ("DTMF password changed. Authenticate again over the radio."
                                if result["success"] else "The DTMF password change failed. Check the Pi helper, then retry.")
            if result["success"] and result["monitor_restart_required"]:
                state["message"] += " Restart the receive monitor on the Pi when no radio task is pending, before using the updated radio menu."
        elif not available:
            state["message"] = "Install and start the Pi DTMF password helper before changing its password here."
        return state

    def status(self):
        configured = self._record() is not None
        bridge_ready = self.listener is not None and self.thread is not None and self.thread.is_alive()
        settings = self.server.settings.get()
        return {"configured": configured, "bridge_ready": bridge_ready,
                "available": configured and bridge_ready,
                "enabled": settings.get("dtmf_enabled") is True,
                "status_announcements": settings.get("dtmf_status_announcements") is True,
                "code_change": self.code_status(),
                "message": "Spoken responses use SvxLink’s transmitter. Receive-only mode (TX=NONE) cannot broadcast responses."}

    def request_code(self, code):
        if not isinstance(code, str) or not CODE_RE.fullmatch(code):
            raise ValueError("Use exactly six DTMF characters: 0–9, uppercase A–D")
        with self.server.mutation_lock:
            state = self.code_status()
            if state["pending"] or self._busy():
                raise ValueError("Resolve current maintenance or unfinished radio work before changing the DTMF password")
            if not state["available"]:
                raise ValueError(state["message"])
            record = {"schema": 1, **password_record(code)}
            request_id = secrets.token_hex(16)
            # Invalidate authorization before queuing: a delayed root worker
            # must never leave a previously authorized RF menu active.
            self.rf_session = None
            write_json(self.state_dir / "dtmf-code-request.json",
                       {"schema": 1, "request_id": request_id, "record": record})
            return {**state, "pending": True, "request_id": request_id, "success": None,
                    "monitor_restart_required": False, "message": "Changing the private DTMF password."}

    def _busy(self):
        radio = self.server.programming.status()
        return (radio.get("pending") is True or radio.get("verification_required") is True
                or any(isinstance(item, dict) and item.get("verification_required")
                       for item in radio.get("roles", {}).values())
                or (radio.get("pair") or {}).get("verification_required") is True
                or self.server.updates.status().get("pending") is True
                or self.server.wifi.status().get("pending") is True
                or self.code_status(probe=False)["pending"])

    def set_enabled(self, enabled):
        if type(enabled) is not bool:
            raise ValueError("enabled must be true or false")
        with self.server.mutation_lock:
            if self._busy():
                raise ValueError("Resolve current maintenance or unfinished radio work before changing DTMF control")
            if enabled and not self.status()["available"]:
                raise ValueError("Set a DTMF password in Admin and start the local bridge before enabling control")
            with self.server.settings_lock:
                self.server.settings.update({"dtmf_enabled": enabled})
            self.rf_session = None
            return self.status()

    def set_announcements(self, enabled):
        if type(enabled) is not bool:
            raise ValueError("status_announcements must be true or false")
        with self.server.mutation_lock:
            if self._busy():
                raise ValueError("Resolve current maintenance or unfinished radio work before changing DTMF control")
            with self.server.settings_lock:
                self.server.settings.update({"dtmf_status_announcements": enabled})
            return self.status()

    @staticmethod
    def response(result, pair_index=0, recording=False, announce_status=False):
        return {"schema": 1, "result": result if result in RESULTS else "unavailable",
                "pair_index": pair_index if type(pair_index) is int and 0 <= pair_index <= 9 else 0,
                "recording": recording is True, "announce_status": announce_status is True}

    def _current_response(self, result, committed=False):
        recording = self.server.hub.status().get("recorder", {}).get("enabled") is True
        radio = self.server.programming.status()
        pair = radio.get("pair") or {}
        index = 0
        if pair.get("stored_verified") is True and not pair.get("catalog_changed"):
            selected = next((item for item in radio.get("pair_switching", {}).get("presets", [])
                             if item.get("id") == pair.get("pair_id")), {})
            index = selected.get("index", 0)
        announce = committed and self.server.settings.get().get("dtmf_status_announcements") is True
        return self.response(result, index, recording, announce)

    def _authenticate(self, code, record):
        with self.security_lock:
            now = self.clock()
            for values in (self.failures, self.attempts):
                while values and values[0] <= now - 60:
                    values.popleft()
            if len(self.failures) >= 5 or len(self.attempts) >= 12:
                return False
            self.attempts.append(now)
            derived = hashlib.pbkdf2_hmac("sha256", code.encode("ascii"), bytes.fromhex(record["salt"]), record["iterations"])
            if not hmac.compare_digest(derived.hex(), record["digest"]):
                self.failures.append(now)
                return False
            return True

    def command(self, request, peer_uid):
        # A single RX source has one menu. Concurrent duplicate frames cannot
        # erase or reuse the authorization being consumed by another frame.
        if not self.rf_command_lock.acquire(blocking=False):
            return self.response("denied")
        try:
            return self._command(request, peer_uid)
        finally:
            self.rf_command_lock.release()

    def _command(self, request, peer_uid):
        if peer_uid not in self.allowed_uids:
            return self.response("denied")
        if (not isinstance(request, dict) or set(request) != {"schema", "command"}
                or type(request.get("schema")) is not int or request["schema"] != 1
                or not isinstance(request.get("command"), str)
                or not (CODE_RE.fullmatch(request["command"]) or re.fullmatch(r"[0-9ABCD*]", request["command"]))):
            return self.response("denied")
        if not self.server.settings.get().get("dtmf_enabled") or self.code_status(probe=False)["pending"]:
            self.rf_session = None
            return self.response("disabled")
        record = self._record()
        if record is None:
            self.rf_session = None
            return self.response("disabled")
        command = request["command"]
        password = len(command) == 6
        if password:
            # A fresh password attempt replaces the previous authorization,
            # including when the replacement password is wrong or throttled.
            self.rf_session = None
        if password and not self._authenticate(command, record):
            return self.response("denied")
        if not self.server.mutation_lock.acquire(blocking=False):
            # Do not consume or grant RF authorization while another action
            # owns the mutation lock. The next attempt needs its full password.
            self.rf_session = None
            return self.response("busy") if password else self.response("denied")
        try:
            if not self.server.settings.get().get("dtmf_enabled") or self.code_status(probe=False)["pending"]:
                self.rf_session = None
                return self.response("disabled")
            if self._record() != record:
                self.rf_session = None
                return self.response("denied")
            if self._busy():
                authorized = password or self.rf_session is not None
                self.rf_session = None
                return self.response("busy" if authorized else "denied")
            now = self.clock()
            if password:
                self.rf_session = {"stage": "option", "expires": now + 30, "record": record, "peer_uid": peer_uid}
                return self._current_response("authenticated")
            session = self.rf_session
            if not session or session["peer_uid"] != peer_uid:
                return self.response("denied")
            if session["record"] != record:
                self.rf_session = None
                return self.response("denied")
            if now >= session["expires"]:
                self.rf_session = None
                return self._current_response("expired")
            if command == "*":
                self.rf_session = None
                return self._current_response("cancelled")
            if session["stage"] == "option" and command in ("1", "2"):
                session["stage"] = "value"
                session["option"] = command
                return self._current_response("choose_status" if command == "1" else "choose_recording")
            if session["stage"] == "value" and command in ("0", "1"):
                # Consume authorization before applying; a failed action or a
                # repeated frame cannot reuse the menu to change anything.
                self.rf_session = None
                enabled = command == "1"
                if session["option"] == "1":
                    with self.server.settings_lock:
                        self.server.settings.update({"dtmf_status_announcements": enabled})
                    result = "status_on" if enabled else "status_off"
                else:
                    self.server.hub.set_recording(enabled)
                    if (self.server.hub.status().get("recorder", {}).get("enabled") is True) != enabled:
                        return self.response("unavailable")
                    result = "recording_on" if enabled else "recording_off"
                return self._current_response(result, committed=True)
            self.rf_session = None
            return self._current_response("invalid")
        except Exception:
            self.rf_session = None
            return self.response("unavailable")
        finally:
            self.server.mutation_lock.release()

    @staticmethod
    def peer_uid(connection):
        if not hasattr(socket, "SO_PEERCRED"):
            return None
        _pid, uid, _gid = struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
        return uid

    def _connection(self, connection):
        with connection:
            connection.settimeout(2)
            try:
                uid = self.peer_uid(connection)
                if uid not in self.allowed_uids:
                    response = self.response("denied")
                else:
                    payload = b""
                    while b"\n" not in payload and len(payload) <= 256:
                        chunk = connection.recv(min(257 - len(payload), 257))
                        if not chunk:
                            break
                        payload += chunk
                    if len(payload) > 256 or not payload.endswith(b"\n") or payload.count(b"\n") != 1:
                        response = self.response("denied")
                    else:
                        response = self.command(json.loads(payload, object_pairs_hook=unique_object), uid)
                connection.sendall(json.dumps(response, separators=(",", ":")).encode("ascii") + b"\n")
            except (OSError, ValueError, TypeError):
                # No payload or exception details enter logs or radio responses.
                pass

    def start(self):
        if self.listener is not None or self.group_id is None or not hasattr(socket, "SO_PEERCRED"):
            return False
        listener = None
        try:
            folder = self.socket_path.parent.lstat()
            if (not stat.S_ISDIR(folder.st_mode) or folder.st_uid != os.geteuid()
                    or folder.st_mode & 0o022):
                return False
            if self.socket_path.exists() or self.socket_path.is_symlink():
                old = self.socket_path.lstat()
                if not stat.S_ISSOCK(old.st_mode) or old.st_uid != os.geteuid():
                    return False
                self.socket_path.unlink()
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(self.socket_path))
            os.chown(self.socket_path, -1, self.group_id)
            os.chmod(self.socket_path, 0o660)
            info = self.socket_path.lstat()
            self.socket_identity = (info.st_dev, info.st_ino)
            listener.listen(4)
            listener.settimeout(0.5)
            self.listener = listener
            self.stop_event.clear()
            self.thread = threading.Thread(target=self._serve, name="dtmf-control", daemon=True)
            self.thread.start()
            return True
        except OSError:
            if listener is not None:
                listener.close()
            self.listener = None
            return False

    def _serve(self):
        while not self.stop_event.is_set():
            try:
                connection, _ = self.listener.accept()
                self._connection(connection)
            except socket.timeout:
                continue
            except OSError:
                return

    def close(self):
        self.stop_event.set()
        listener = self.listener
        if listener is not None:
            listener.close()
        if self.thread is not None:
            self.thread.join(timeout=3)
        self.listener = None
        try:
            current = self.socket_path.lstat()
            if (current.st_dev, current.st_ino) == self.socket_identity and stat.S_ISSOCK(current.st_mode):
                self.socket_path.unlink()
        except FileNotFoundError:
            pass
