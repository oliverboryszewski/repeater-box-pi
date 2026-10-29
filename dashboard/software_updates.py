"""Queue fixed-origin dashboard updates for the separate root systemd worker.

This module never downloads code, runs installers, or accepts a repository URL
from the browser. The worker owns the origin, validation, activation and rollback.
"""
import json
import os
from pathlib import Path
import re
import secrets
import stat
import threading
import time

from controls import write_json
from wifi_visibility import command, is_pi

REPOSITORY = "oliverboryszewski/repeater-box-pi"
BRANCH = "main"
COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z")
REQUEST_RE = re.compile(r"[0-9a-f]{32}\Z")
VERSION_RE = re.compile(r"[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}\Z")


def version_parts(value):
    """Compare the supported three-component release versions numerically."""
    return tuple(map(int, value.split("."))) if isinstance(value, str) and VERSION_RE.fullmatch(value) else None


def read_json(path, maximum=16384):
    """Read bounded regular files only, including when a stale symlink exists."""
    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
            return {}
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            data = stream.read(maximum + 1)
        if len(data) > maximum:
            return {}
        value = json.loads(data)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, UnicodeError):
        return {}
    finally:
        if descriptor is not None:
            os.close(descriptor)


def valid_request(value):
    if not isinstance(value, dict) or not REQUEST_RE.fullmatch(str(value.get("request_id", ""))):
        return False
    if value.get("action") == "check":
        return set(value) == {"request_id", "action"}
    return (value.get("action") == "install" and set(value) == {"request_id", "action", "commit"}
            and isinstance(value.get("commit"), str) and bool(COMMIT_RE.fullmatch(value["commit"])))


class SoftwareUpdates:
    REQUEST = "software-update-request.json"
    RESULT = "software-update-result.json"
    PENDING_SECONDS = 600

    def __init__(self, state_dir, supported=None, runner=None, clock=None,
                 install_dir="/opt/repeater-box", config_file="/etc/repeater-box/updates.json",
                 current_version=None):
        self.state_dir = Path(state_dir)
        self.install_dir = Path(install_dir)
        self.config_file = Path(config_file)
        self.supported = is_pi() if supported is None else supported
        self.run = runner or command
        self.clock = clock or time.time
        self.current_version = current_version
        self.lock = threading.RLock()
        # A persisted result is display history, not installation authority for
        # a new dashboard process or a changed installed version/commit.
        self._check = None

    def status(self):
        with self.lock:
            installed = read_json(self.install_dir / "update-installed.json")
            version = self.current_version or installed.get("version")
            commit = installed.get("commit")
            metadata_matches = installed.get("version") in (None, version)
            state = {"available": False, "preview": not self.supported, "pending": False,
                     "action": None, "request_id": None, "current_version": version,
                     "current_commit": commit if metadata_matches and isinstance(commit, str) and COMMIT_RE.fullmatch(commit) else None,
                     "latest_version": None, "latest_commit": None, "update_available": False,
                     "success": None, "message": None, "checked_at": None,
                     "check_stale": False, "can_install": False}
            if not self.supported:
                self._check = None
                state["message"] = "Preview only — dashboard updates run on the Pi."
                return state
            config = read_json(self.config_file, 4096)
            if config != {"repository": REPOSITORY, "branch": BRANCH}:
                self._check = None
                state["message"] = "Install the current Pi update helper to enable dashboard updates."
                return state
            if self.run(["/usr/bin/systemctl", "is-active", "repeater-software-update.path"]) != "active":
                self._check = None
                state["message"] = "Dashboard update helper is unavailable. Rerun the current Pi installer."
                return state
            state["available"] = True
            result = read_json(self.state_dir / self.RESULT)
            for name, pattern in (("latest_version", VERSION_RE), ("latest_commit", COMMIT_RE)):
                value = result.get(name)
                if isinstance(value, str) and pattern.fullmatch(value):
                    state[name] = value
            if isinstance(result.get("checked_at"), str):
                state["checked_at"] = result["checked_at"][:80]
            request = read_json(self.state_dir / self.REQUEST, 2048)
            if valid_request(request):
                state.update(action=request["action"], request_id=request["request_id"])
                if result.get("request_id") == request["request_id"] and type(result.get("success")) is bool:
                    state["success"] = result["success"]
                    if isinstance(result.get("message"), str):
                        state["message"] = result["message"][:1000]
                else:
                    try:
                        age = self.clock() - (self.state_dir / self.REQUEST).stat().st_mtime
                    except OSError:
                        age = self.PENDING_SECONDS + 1
                    state["pending"] = -60 <= age <= self.PENDING_SECONDS
                    if state["pending"]:
                        state["message"] = ("Installing dashboard update. This page will reconnect after the dashboard restarts."
                                            if request["action"] == "install" else "Checking GitHub for a dashboard update.")
                    else:
                        state.update(success=False, message="The update request was not confirmed. Check the Pi helper log, then retry.")
            identity = (state["current_version"], state["current_commit"])
            matching_result = result.get("request_id") == state["request_id"] and state["request_id"] is not None
            result_history = bool(result) and (result.get("action") in ("check", "install") or state["action"] in ("check", "install"))
            fingerprint_matches = ({"current_version", "current_commit"} <= set(result)
                                   and (result["current_version"], result["current_commit"]) == identity)
            cache_stale = bool(result_history and (not metadata_matches or not fingerprint_matches
                               or not state["pending"] and (not matching_result or result.get("action") != state["action"])))
            # A newly queued check replaces stale history; do not revoke the
            # browser's new check token while its result is still pending.
            state["check_stale"] = cache_stale and not (state["pending"] and state["action"] == "check")
            if cache_stale:
                # An older cache describes the previous installation. Clearing
                # its target prevents clients from presenting it as current.
                state.update(latest_version=None, latest_commit=None, checked_at=None, success=None)
                if not state["pending"]:
                    state["message"] = "The installed dashboard changed or its saved check is outdated. Check for updates again."
            if state["pending"] and state["action"] == "check":
                state.update(latest_version=None, latest_commit=None, checked_at=None)
            current_parts, latest_parts = version_parts(state["current_version"]), version_parts(state["latest_version"])
            checked = (not state["pending"] and not state["check_stale"] and matching_result
                       and state["action"] == "check" and result.get("action") == "check" and state["success"] is True)
            state["update_available"] = bool(checked and current_parts is not None and latest_parts is not None
                and latest_parts >= current_parts and state["latest_commit"]
                and state["latest_commit"] != state["current_commit"])
            if checked and current_parts is not None and latest_parts is not None and latest_parts < current_parts:
                state["message"] = "This dashboard is newer than the checked release. No update is available."
            if self._check:
                context = self._check
                identity = (state["current_version"], state["current_commit"])
                target = (state["latest_version"], state["latest_commit"])
                if (state["check_stale"] or identity != context["installed"] or installed.get("version") != context["metadata_version"]
                        or state["request_id"] != context["request_id"]
                        or state["action"] != "check" or state["success"] is False):
                    self._check = None
                elif not state["pending"]:
                    if (state["success"] is not True or not all(target) or result.get("action") != "check"
                            or not {"current_version", "current_commit"} <= set(result)
                            or (result["current_version"], result["current_commit"]) != identity):
                        self._check = None
                    elif context["target"] is None:
                        context["target"] = target
                    elif target != context["target"]:
                        self._check = None
                elif context["target"] is not None:
                    self._check = None
            state["can_install"] = bool(state["update_available"] and self._check
                and self._check["installed"] == identity and self._check["request_id"] == state["request_id"]
                and self._check["target"] == (state["latest_version"], state["latest_commit"]))
            return state

    def _request(self, action, commit=None):
        with self.lock:
            state = self.status()
            if not state["available"]:
                raise ValueError(state["message"] or "Dashboard updates are unavailable")
            if state["pending"]:
                raise ValueError("A dashboard update request is already pending")
            if action == "install":
                context = self._check
                if (not context or state["action"] != "check" or state["success"] is not True
                        or context["request_id"] != state["request_id"]
                        or context["installed"] != (state["current_version"], state["current_commit"])
                        or context["target"] != (state["latest_version"], state["latest_commit"])):
                    raise ValueError("Run a fresh successful update check before installing")
                if commit != state["latest_commit"] or not state["latest_version"]:
                    raise ValueError("Check for updates again before installing this commit")
                current_parts, latest_parts = version_parts(state["current_version"]), version_parts(state["latest_version"])
                if current_parts is None or latest_parts is None:
                    raise ValueError("Run a fresh successful update check with valid release versions before installing")
                if latest_parts < current_parts:
                    raise ValueError("The checked dashboard version is older than the installed version. Downgrades require a manual Pi installation")
                if commit == state["current_commit"]:
                    raise ValueError("This dashboard commit is already installed")
                if not state["can_install"]:
                    raise ValueError("Run a fresh successful update check before installing")
            request = {"request_id": secrets.token_hex(16), "action": action}
            if commit is not None:
                request["commit"] = commit
            self._check = None
            write_json(self.state_dir / self.REQUEST, request)
            if action == "check":
                self._check = {"request_id": request["request_id"],
                               "installed": (state["current_version"], state["current_commit"]),
                               "metadata_version": read_json(self.install_dir / "update-installed.json").get("version"),
                               "target": None}
            state.update(pending=True, action=action, request_id=request["request_id"], success=None, can_install=False,
                         message=("Installing dashboard update. The dashboard will restart briefly."
                                  if action == "install" else "Checking GitHub for a dashboard update."))
            if action == "check":
                state["check_stale"] = False
                state.update(latest_version=None, latest_commit=None, checked_at=None)
            return state

    def request_check(self):
        return self._request("check")

    def request_install(self, commit):
        if not isinstance(commit, str) or not COMMIT_RE.fullmatch(commit):
            raise ValueError("commit must be a full, lowercase 40-character Git commit ID")
        return self._request("install", commit)
