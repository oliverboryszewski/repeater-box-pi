"""Local settings and admin sessions. No shell commands or radio control."""
from collections import defaultdict, deque
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import threading
import time

DEFAULTS = {"station_name": "Repeater Box", "input_enabled": False, "monitor_transmit_audio": True, "data_decode_enabled": False,
            "dtmf_enabled": False, "dtmf_status_announcements": True, "automatic_station_id": True, "record_on_startup": False, "keep_indefinitely": False, "retention_days": 7,
            "max_archive_mb": 4096, "recording_threshold_dbfs": -45}
ITERATIONS = 600_000


def password_record(password):
    if not isinstance(password, str) or not 6 <= len(password) <= 256:
        raise ValueError("Use an admin password of 6–256 characters")
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), ITERATIONS)
    return {"algorithm": "pbkdf2_sha256", "iterations": ITERATIONS,
            "salt": salt, "digest": digest.hex()}


def write_json(path, value, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + secrets.token_hex(6) + ".tmp")
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class SettingsStore:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.value = dict(DEFAULTS)
        if self.path.exists():
            self.value = self.validate(json.loads(self.path.read_text()))

    @staticmethod
    def validate(value):
        if not isinstance(value, dict) or set(value) - set(DEFAULTS):
            raise ValueError("Unknown settings field")
        merged = dict(DEFAULTS)
        merged.update(value)
        name = merged["station_name"]
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 48 or any(ord(c) < 32 for c in name):
            raise ValueError("Station name must contain 1–48 printable characters")
        merged["station_name"] = name.strip()
        for key in ("input_enabled", "monitor_transmit_audio", "data_decode_enabled", "dtmf_enabled", "dtmf_status_announcements", "automatic_station_id", "record_on_startup", "keep_indefinitely"):
            if type(merged[key]) is not bool:
                raise ValueError(f"{key} must be true or false")
        for key, low, high in (("retention_days", 1, 90), ("max_archive_mb", 64, 16384),
                               ("recording_threshold_dbfs", -70, -10)):
            if type(merged[key]) is not int or not low <= merged[key] <= high:
                raise ValueError(f"{key} must be an integer from {low} to {high}")
        return merged

    def get(self):
        with self.lock:
            return dict(self.value)

    def update(self, value):
        with self.lock:
            if not isinstance(value, dict):
                raise ValueError("Settings must be an object")
            # Older open dashboard pages may omit newly introduced fields.
            # Preserve saved retention choices instead of resetting defaults.
            checked = self.validate({**self.value, **value})
            write_json(self.path, checked)
            self.value = checked
            return dict(checked)


class AdminAuth:
    """Per-process sessions, CSRF tokens, and bounded login attempts."""
    def __init__(self, password_file):
        self.password_file = Path(password_file)
        self.sessions = {}
        self.failures = defaultdict(deque)
        self.global_failures = deque()
        self.lock = threading.RLock()

    def _record(self):
        try:
            record = json.loads(self.password_file.read_text())
            if not isinstance(record, dict):
                return None
            if record.get("algorithm") != "pbkdf2_sha256" or not 100_000 <= record["iterations"] <= 1_000_000:
                return None
            bytes.fromhex(record["salt"])
            if len(bytes.fromhex(record["digest"])) != 32:
                return None
            return record
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def session(self, token):
        with self.lock:
            now = time.monotonic()
            record = self._record()
            self.sessions = {key: value for key, value in self.sessions.items()
                             if value["expires"] > now and record and value["digest"] == record["digest"]}
            session = self.sessions.get(token)
            return {"configured": bool(record), "authenticated": bool(session),
                    "csrf_token": session["csrf"] if session else None}

    def _check_password(self, password, address):
        # Caller holds the lock. Login and sensitive operations share limits.
        now = time.monotonic()
        # Expire old source records too, so the table cannot grow forever.
        for key in list(self.failures):
            while self.failures[key] and self.failures[key][0] < now - 60:
                self.failures[key].popleft()
            if not self.failures[key]:
                del self.failures[key]
        while self.global_failures and self.global_failures[0] < now - 60:
            self.global_failures.popleft()
        if len(self.failures[address]) >= 5 or len(self.global_failures) >= 30:
            raise PermissionError("Too many attempts. Wait one minute and try again.")
        record = self._record()
        if not record:
            raise ValueError("Admin access is not configured. Set its password on the Pi first.")
        valid = False
        if isinstance(password, str) and len(password) <= 256:
            derived = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(record["salt"]), record["iterations"])
            valid = hmac.compare_digest(derived.hex(), record["digest"])
        if not valid:
            self.failures[address].append(now)
            self.global_failures.append(now)
            raise PermissionError("Incorrect admin password")
        return record

    def reauthenticate(self, token, csrf, password, address):
        with self.lock:
            if not self.authorize(token, csrf):
                raise PermissionError("Sign in as admin again before this sensitive change")
            self._check_password(password, address)
            return True

    def login(self, password, address):
        with self.lock:
            record = self._check_password(password, address)
            now = time.monotonic()
            self.session(None)  # Prune expired sessions.
            if len(self.sessions) >= 128:
                self.sessions.pop(next(iter(self.sessions)))
            token = secrets.token_urlsafe(32)
            self.sessions[token] = {"expires": now + 8 * 3600, "csrf": secrets.token_urlsafe(32), "digest": record["digest"]}
            return token, self.session(token)

    def authorize(self, token, csrf):
        session = self.session(token)
        return bool(session["authenticated"] and isinstance(csrf, str)
                    and hmac.compare_digest(csrf, session["csrf_token"]))

    def logout(self, token):
        with self.lock:
            self.sessions.pop(token, None)
