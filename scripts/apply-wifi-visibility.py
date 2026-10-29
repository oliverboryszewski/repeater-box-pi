#!/usr/bin/python3
"""Narrow root worker: change only the fixed portable AP's hidden-SSID flag.

The unprivileged dashboard queues a request; systemd.path runs this independently
of the HTTP connection, which can disappear while the AP is reactivated. There
are deliberately no command-line options, shell commands or user-chosen profiles.
"""
from datetime import datetime, timezone
import grp
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile

STATE_DIR = Path("/var/lib/repeater-dashboard")
REQUEST_PATH = STATE_DIR / "wifi-visibility-request.json"
RESULT_PATH = STATE_DIR / "wifi-visibility-result.json"
PROFILE_ID = "repeater-ap"
INTERFACE = "wlan0"
REQUEST_ID_RE = re.compile(r"[0-9a-fA-F]{32}\Z")
UUID_RE = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\Z")
PROFILE_FIELDS = (
    "connection.type", "connection.interface-name", "802-11-wireless.mode",
    "802-11-wireless-security.key-mgmt", "802-11-wireless-security.proto",
    "802-11-wireless.hidden",
)


class ApplyError(Exception):
    """An operational failure whose fixed message is safe for the dashboard."""


def _unique_object(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError("Duplicate request key")
        obj[key] = value
    return obj


def read_request(path=REQUEST_PATH):
    """Read a bounded, regular file without following an app-supplied symlink."""
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    descriptor = os.open(path, flags)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 2048:
            raise ValueError("Request must be a regular file of at most 2048 bytes")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            data = stream.read(2049)
        if len(data) > 2048:
            raise ValueError("Request exceeds 2048 bytes")
    finally:
        os.close(descriptor)
    request = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_object)
    if (not isinstance(request, dict) or set(request) != {"request_id", "hidden"}
            or type(request["hidden"]) is not bool
            or not isinstance(request["request_id"], str)
            or not REQUEST_ID_RE.fullmatch(request["request_id"])):
        raise ValueError("Invalid Wi-Fi visibility request")
    return request


def nmcli(*arguments, wait=5):
    """Invoke only the system binary, with bounded waits and no shell or secrets."""
    try:
        result = subprocess.run(
            ["/usr/bin/nmcli", "--wait", str(wait), "--colors", "no", *arguments],
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=wait + 5, check=False,
            env={"LC_ALL": "C", "LANG": "C", "PATH": "/usr/sbin:/usr/bin:/sbin:/bin"},
        )
    except subprocess.TimeoutExpired as exc:
        raise ApplyError("NetworkManager timed out.") from exc
    except OSError as exc:
        raise ApplyError("NetworkManager command is unavailable.") from exc
    if result.returncode:
        # Do not return nmcli output: profile metadata can contain private data.
        raise ApplyError("NetworkManager could not complete the operation.")
    return result.stdout.rstrip("\n")


def find_profile(run):
    profiles = run("--terse", "--escape", "yes", "--fields", "UUID,NAME", "connection", "show")
    matches = []
    for line in profiles.splitlines():
        uuid, separator, name = line.partition(":")
        # Our fixed ID has no escapable characters; other IDs cannot match it.
        if separator and name == PROFILE_ID:
            if not UUID_RE.fullmatch(uuid):
                raise ApplyError("The hotspot profile UUID is invalid.")
            matches.append(uuid)
    if not matches:
        raise ApplyError("Configure the repeater-ap hotspot first.")
    if len(matches) != 1:
        raise ApplyError("Multiple repeater-ap profiles exist; resolve them locally first.")
    return matches[0]


def read_profile(uuid, run):
    values = run("--escape", "no", "--get-values", ",".join(PROFILE_FIELDS),
                 "connection", "show", "uuid", uuid).splitlines()
    if len(values) != len(PROFILE_FIELDS):
        raise ApplyError("The hotspot profile could not be verified.")
    profile = dict(zip(PROFILE_FIELDS, values))
    if profile["802-11-wireless.hidden"] not in ("yes", "no"):
        raise ApplyError("The hotspot visibility value could not be verified.")
    profile["hidden"] = profile["802-11-wireless.hidden"] == "yes"
    return profile


def validate_profile(profile):
    if (profile["connection.type"] not in ("802-11-wireless", "wifi")
            or profile["connection.interface-name"] != INTERFACE
            or profile["802-11-wireless.mode"] != "ap"):
        raise ApplyError("The repeater-ap profile must be a Wi-Fi access point on wlan0.")
    protocols = set(re.split(r"[,\s]+", profile["802-11-wireless-security.proto"].strip()))
    if (profile["802-11-wireless-security.key-mgmt"] not in ("wpa-psk", "sae")
            or protocols != {"rsn"}):
        raise ApplyError("The hotspot must retain WPA/RSN security; review its local setup.")


def is_active(uuid, run):
    active = run("--terse", "--escape", "yes", "--fields", "UUID,DEVICE",
                 "connection", "show", "--active")
    return any(line == uuid + ":" + INTERFACE for line in active.splitlines())


def set_hidden(uuid, hidden, run):
    run("connection", "modify", "uuid", uuid,
        "802-11-wireless.hidden", "yes" if hidden else "no")


def reactivate(uuid, run):
    # Re-activation applies AP settings which device reapply may not support.
    run("connection", "up", "uuid", uuid, "ifname", INTERFACE, wait=25)


def apply_request(request, run=nmcli):
    """Return a complete result. Tests inject a fake runner, never a real network."""
    result = {
        "request_id": request["request_id"], "requested_hidden": request["hidden"],
        "success": False, "profile_hidden": None, "message": "",
    }
    uuid = None
    old_hidden = None
    modified = False
    reconnect = False
    try:
        uuid = find_profile(run)
        profile = read_profile(uuid, run)
        result["profile_hidden"] = profile["hidden"]
        validate_profile(profile)
        old_hidden = profile["hidden"]
        if old_hidden == request["hidden"]:
            result.update(success=True, message="The hotspot already has this visibility setting.")
        else:
            reconnect = is_active(uuid, run)
            # Even a timed-out modification may have succeeded; attempt rollback.
            modified = True
            set_hidden(uuid, request["hidden"], run)
            # A currently inactive AP must never displace the Pi's home Wi-Fi.
            if reconnect and is_active(uuid, run):
                reactivate(uuid, run)
                if not is_active(uuid, run):
                    raise ApplyError("The hotspot did not return on wlan0.")
                message = "Hotspot visibility changed. Reconnect your device if needed."
            else:
                reconnect = False
                message = "Hotspot profile updated; its inactive Wi-Fi connection was left inactive."
            current = read_profile(uuid, run)
            result["profile_hidden"] = current["hidden"]
            if current["hidden"] != request["hidden"]:
                raise ApplyError("The requested hotspot visibility was not saved.")
            result.update(success=True, message=message)
    except ApplyError as exc:
        message = str(exc)
        if modified and uuid is not None:
            restored = False
            try:
                set_hidden(uuid, old_hidden, run)
                if reconnect:
                    reactivate(uuid, run)
                    if not is_active(uuid, run):
                        raise ApplyError("The hotspot did not reconnect.")
                restored = read_profile(uuid, run)["hidden"] == old_hidden
            except ApplyError:
                pass
            message += (" Previous visibility was restored." if restored else
                        " Automatic recovery could not be confirmed; use Ethernet or a local console.")
        if uuid is not None:
            try:
                result["profile_hidden"] = read_profile(uuid, run)["hidden"]
            except ApplyError:
                result["profile_hidden"] = None
        result["message"] = message
    result["completed_at"] = datetime.now(timezone.utc).isoformat()
    return result


def write_result(result, path=RESULT_PATH):
    """Atomic root-owned result; never follow a pre-existing result symlink."""
    descriptor, temporary = tempfile.mkstemp(prefix=".wifi-result-", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o640)
        os.fchown(descriptor, 0, grp.getgrnam("repeaterdash").gr_gid)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = None
            json.dump(result, stream, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def main():
    if len(sys.argv) != 1 or os.geteuid() != 0:
        print("This fixed worker runs as root through repeater-wifi-visibility.service.", file=sys.stderr)
        return 1
    try:
        request = read_request()
        result = apply_request(request)
        write_result(result)
    except (OSError, ValueError) as exc:
        print("Wi-Fi visibility request could not be read or result could not be saved: "
              + type(exc).__name__, file=sys.stderr)
        return 1
    # Operational failures are recorded, not retried indefinitely by systemd.path.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
