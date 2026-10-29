#!/usr/bin/python3
"""Fixed-origin, dashboard-only update worker; never run downloaded installers.

The dashboard writes a narrow request. This independent root worker downloads a
pinned public GitHub commit, selectively reads an allowlist of regular files,
compiles a staged dashboard, and rolls back if its HTTP health check fails. Radio,
SvxLink, Wi-Fi, system services, credentials and recordings are outside its scope.
"""
import ast
from datetime import datetime, timezone
import fcntl
import grp
import io
import json
import os
from pathlib import Path, PurePosixPath
import py_compile
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

REPOSITORY = "oliverboryszewski/repeater-box-pi"
BRANCH = "main"
STATE_DIR = Path("/var/lib/repeater-dashboard")
INSTALL_DIR = Path("/opt/repeater-box")
CONFIG_PATH = Path("/etc/repeater-box/updates.json")
REQUEST_NAME = "software-update-request.json"
RESULT_NAME = "software-update-result.json"
COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z")
REQUEST_RE = re.compile(r"[0-9a-f]{32}\Z")
VERSION_RE = re.compile(r"[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}\Z")
ALLOWED_FILES = (
    "dashboard/server.py", "dashboard/controls.py", "dashboard/audio_hub.py",
    "dashboard/admin_password.py", "dashboard/wifi_visibility.py", "dashboard/software_updates.py",
    "dashboard/radio_programming.py", "dashboard/radio_editor.py", "dashboard/dtmf_control.py",
    "dashboard/static/index.html", "dashboard/static/app.js", "dashboard/static/style.css",
    "dashboard/static/radio-editor.html", "dashboard/static/radio-editor.js", "dashboard/static/radio-editor.css",
    "dashboard/static/audio-visualizer.js", "dashboard/static/audio-visualizer.css",
)
MANIFEST = "software-update.json"
MAX_ARCHIVE = 8 * 1024 * 1024
MAX_EXPANDED = 32 * 1024 * 1024
MAX_FILE = 2 * 1024 * 1024


class UpdateError(Exception):
    """Fixed, non-secret operational message suitable for the dashboard."""


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate JSON key")
        value[key] = item
    return value


def read_json(path, maximum=16384, trusted=False):
    """Bounded regular-file read. Root configuration/state must not be writable by the app."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
            raise ValueError("Invalid JSON file")
        if trusted and (info.st_uid != 0 or info.st_mode & 0o022):
            raise ValueError("Root-owned file required")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            data = stream.read(maximum + 1)
        if len(data) > maximum:
            raise ValueError("Oversized JSON file")
    finally:
        os.close(descriptor)
    value = json.loads(data.decode("utf-8"), object_pairs_hook=unique_object)
    if not isinstance(value, dict):
        raise ValueError("JSON object required")
    return value


def read_request(path):
    request = read_json(path, 2048)
    if not isinstance(request.get("request_id"), str) or not REQUEST_RE.fullmatch(request["request_id"]):
        raise ValueError("Invalid update request ID")
    if request.get("action") == "check" and set(request) == {"request_id", "action"}:
        return request
    if (request.get("action") == "install" and set(request) == {"request_id", "action", "commit"}
            and isinstance(request.get("commit"), str) and COMMIT_RE.fullmatch(request["commit"])):
        return request
    raise ValueError("Invalid update request")


def write_json(path, value, group=None):
    """Atomic root result/metadata write; do not follow an existing result symlink."""
    descriptor, temporary = tempfile.mkstemp(prefix=".software-update-", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o640 if group is not None else 0o644)
        if os.geteuid() == 0:
            os.fchown(descriptor, 0, group if group is not None else 0)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = None
            json.dump(value, stream, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        # Persist the directory entry as well as the contents across power loss.
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def allowed_url(url):
    """Only the configured repository's HTTPS API and commit archive URLs are valid."""
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.username is not None or parsed.password is not None
                or parsed.port not in (None, 443) or parsed.query or parsed.fragment):
            return False
        if parsed.hostname == "api.github.com":
            return (parsed.path == f"/repos/{REPOSITORY}/commits/{BRANCH}"
                    or bool(re.fullmatch(r"/repos/" + re.escape(REPOSITORY) + r"/tarball/[0-9a-f]{40}", parsed.path)))
        if parsed.hostname == "codeload.github.com":
            return bool(re.fullmatch(r"/" + re.escape(REPOSITORY)
                                     + r"/(?:legacy\.tar\.gz|tar\.gz)/[0-9a-f]{40}", parsed.path))
    except ValueError:
        return False
    return False


class FixedRedirects(HTTPRedirectHandler):
    max_redirections = 3
    max_repeats = 2

    def redirect_request(self, request, fp, code, message, headers, newurl):
        if not allowed_url(newurl):
            raise UpdateError("GitHub redirected outside the configured repository's HTTPS download hosts.")
        return super().redirect_request(request, fp, code, message, headers, newurl)


def fetch_bytes(url, maximum, opener=None, clock=time.monotonic):
    if not allowed_url(url):
        raise UpdateError("The dashboard update source is invalid.")
    # Do not inherit root's proxy environment or send authentication to redirects.
    opener = opener or build_opener(ProxyHandler({}), FixedRedirects())
    request = Request(url, headers={"User-Agent": "Repeater-Box-Pi-Updater/1",
                                   "Accept": "application/vnd.github+json", "Accept-Encoding": "identity",
                                   "X-GitHub-Api-Version": "2022-11-28"})
    deadline = clock() + 45
    try:
        with opener.open(request, timeout=10) as response:
            if not allowed_url(response.geturl()):
                raise UpdateError("The dashboard download source changed unexpectedly.")
            length = response.headers.get("Content-Length")
            if length is not None:
                try:
                    if not 0 <= int(length) <= maximum:
                        raise UpdateError("The dashboard download exceeds the supported size.")
                except ValueError as exc:
                    raise UpdateError("GitHub returned an invalid download size.") from exc
            chunks, total = [], 0
            while True:
                if clock() > deadline:
                    raise UpdateError("GitHub download timed out. Check the Pi's internet connection and retry.")
                # read1 avoids an unbounded read waiting for a whole large chunk.
                chunk = response.read1(min(65536, maximum + 1 - total))
                if not chunk:
                    return b"".join(chunks)
                total += len(chunk)
                if total > maximum:
                    raise UpdateError("The dashboard download exceeds the supported size.")
                chunks.append(chunk)
    except HTTPError as exc:
        if exc.code in (403, 429):
            raise UpdateError("GitHub temporarily limited update checks. Wait a while before retrying.") from exc
        if exc.code == 404:
            raise UpdateError("The public dashboard repository or requested commit is not available on GitHub.") from exc
        raise UpdateError("GitHub could not provide the dashboard update. Retry later.") from exc
    except (URLError, OSError, TimeoutError) as exc:
        raise UpdateError("Cannot reach GitHub. Connect the Pi to the internet, then retry.") from exc


def latest_commit(fetch=fetch_bytes):
    try:
        value = json.loads(fetch(f"https://api.github.com/repos/{REPOSITORY}/commits/{BRANCH}", 2 * 1024 * 1024),
                           object_pairs_hook=unique_object)
        commit = value.get("sha") if isinstance(value, dict) else None
    except (ValueError, UnicodeError) as exc:
        raise UpdateError("GitHub returned invalid commit information.") from exc
    if not isinstance(commit, str) or not COMMIT_RE.fullmatch(commit):
        raise UpdateError("GitHub returned an invalid commit ID.")
    return commit


def source_version(source):
    """Read VERSION without importing or executing downloaded Python."""
    try:
        tree = ast.parse(source)
        versions = [node.value.value for node in tree.body
                    if isinstance(node, ast.Assign)
                    and any(isinstance(target, ast.Name) and target.id == "VERSION" for target in node.targets)
                    and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)]
    except (ValueError, SyntaxError, UnicodeError) as exc:
        raise UpdateError("The dashboard source is not valid Python.") from exc
    if len(versions) != 1 or not VERSION_RE.fullmatch(versions[0]):
        raise UpdateError("The dashboard source has no supported version identifier.")
    return versions[0]


def read_archive(data):
    """Select regular allowlisted files in memory; never extract a tar pathname."""
    if len(data) > MAX_ARCHIVE:
        raise UpdateError("The dashboard archive is too large.")
    selected, names, root, expanded, count = {}, set(), None, 0, 0
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r|gz") as archive:
            for member in archive:
                count += 1
                name = member.name.rstrip("/")
                parts = name.split("/")
                if (count > 4096 or not name or "\\" in name or "\x00" in name
                        or name.startswith("/") or any(part in ("", ".", "..") for part in parts)
                        or not (member.isdir() or member.isreg()) or name in names):
                    raise UpdateError("The dashboard archive contains an unsafe entry.")
                names.add(name)
                if root is None:
                    root = parts[0]
                if parts[0] != root:
                    raise UpdateError("The dashboard archive has inconsistent root directories.")
                if member.size < 0:
                    raise UpdateError("The dashboard archive has an invalid file size.")
                expanded += member.size
                if expanded > MAX_EXPANDED:
                    raise UpdateError("The expanded dashboard archive is too large.")
                relative = str(PurePosixPath(*parts[1:])) if len(parts) > 1 else ""
                if relative not in (*ALLOWED_FILES, MANIFEST):
                    continue
                limit = 4096 if relative == MANIFEST else MAX_FILE
                if not member.isreg() or member.size > limit or relative in selected:
                    raise UpdateError("The dashboard archive contains an invalid required file.")
                stream = archive.extractfile(member)
                if stream is None:
                    raise UpdateError("A required dashboard file could not be read.")
                selected[relative] = stream.read(limit + 1)
                if len(selected[relative]) != member.size:
                    raise UpdateError("A required dashboard file was truncated.")
    except (tarfile.TarError, OSError, EOFError, ValueError) as exc:
        raise UpdateError("The dashboard archive could not be verified.") from exc
    if set(selected) != set(ALLOWED_FILES) | {MANIFEST}:
        raise UpdateError("The update is missing a required dashboard file or manifest.")
    try:
        manifest = json.loads(selected.pop(MANIFEST), object_pairs_hook=unique_object)
    except (ValueError, UnicodeError) as exc:
        raise UpdateError("The dashboard update manifest is invalid.") from exc
    if (not isinstance(manifest, dict) or set(manifest) != {"version", "update_api"}
            or type(manifest.get("update_api")) is not int or manifest["update_api"] != 10
            or not isinstance(manifest.get("version"), str) or not VERSION_RE.fullmatch(manifest["version"])):
        raise UpdateError("This update needs a newer Pi installer. Update manually using the repository instructions.")
    if source_version(selected["dashboard/server.py"]) != manifest["version"]:
        raise UpdateError("The dashboard source and update manifest versions do not match.")
    return selected, manifest


def trusted_directory(path, require_root=True):
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o022
            or (require_root and info.st_uid != 0)):
        raise UpdateError("The dashboard installation directory must be owned by root and not writable by other users.")


def current_install(install_dir, require_root=True):
    path = install_dir / "update-installed.json"
    try:
        metadata = read_json(path, trusted=require_root)
    except FileNotFoundError:
        try:
            version = source_version((install_dir / "dashboard/server.py").read_bytes())
        except OSError as exc:
            raise UpdateError("The installed dashboard version could not be read.") from exc
        return {"version": version, "commit": None}
    except (OSError, ValueError, UnicodeError) as exc:
        raise UpdateError("The installed dashboard update metadata could not be verified.") from exc
    if (not isinstance(metadata.get("version"), str) or not VERSION_RE.fullmatch(metadata["version"])
            or (metadata.get("commit") is not None
                and (not isinstance(metadata["commit"], str) or not COMMIT_RE.fullmatch(metadata["commit"])))):
        raise UpdateError("The installed dashboard update metadata is invalid.")
    return metadata


def run_command(arguments, timeout=30):
    try:
        result = subprocess.run(arguments, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                timeout=timeout, check=False,
                                env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C", "LC_ALL": "C"})
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UpdateError("The dashboard service command could not complete.") from exc
    if result.returncode:
        raise UpdateError("The dashboard service command failed.")
    return result.stdout.strip()


def health_check(version, timeout=30, clock=time.monotonic, pause=time.sleep):
    """Use only the local unauthenticated status endpoint, with no proxy or redirects."""
    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, *arguments):
            return None

    opener = build_opener(ProxyHandler({}), NoRedirect())
    deadline = clock() + timeout
    while clock() < deadline:
        try:
            with opener.open("http://127.0.0.1/api/health", timeout=2) as response:
                data = response.read(2 * 1024 * 1024 + 1)
                if len(data) <= 2 * 1024 * 1024:
                    status = json.loads(data)
                    if (isinstance(status, dict) and status.get("version") == version
                            and status.get("ready") is True):
                        return True
        except (OSError, ValueError, URLError, TimeoutError):
            pass
        pause(0.5)
    return False


class DashboardInstaller:
    """Activation transaction. Tests supply temporary paths and fake service/HTTP functions."""
    def __init__(self, install_dir=INSTALL_DIR, runner=run_command, health=health_check,
                 require_root=True, metadata_writer=write_json):
        self.install_dir = Path(install_dir)
        self.run = runner
        self.health = health
        self.require_root = require_root
        self.write_metadata = metadata_writer

    def service(self, action):
        self.run(["/usr/bin/systemctl", action, "repeater-dashboard.service"], timeout=30)

    def install(self, payload, manifest, commit):
        if set(payload) != set(ALLOWED_FILES) or not COMMIT_RE.fullmatch(commit):
            raise UpdateError("The staged dashboard payload is invalid.")
        trusted_directory(self.install_dir, self.require_root)
        live = self.install_dir / "dashboard"
        backup = self.install_dir / "dashboard.previous"
        trusted_directory(live, self.require_root)
        previous = current_install(self.install_dir, self.require_root)
        staging = Path(tempfile.mkdtemp(prefix=".dashboard-update-", dir=self.install_dir))
        candidate = staging / "dashboard"
        old_moved = new_active = stop_attempted = metadata_attempted = False
        try:
            candidate.mkdir(mode=0o755)
            for name in ALLOWED_FILES:
                target = staging / name
                target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
                target.write_bytes(payload[name])
                target.chmod(0o644)
                if target.suffix == ".py":
                    try:
                        py_compile.compile(str(target), dfile=str(self.install_dir / name), doraise=True)
                    except (py_compile.PyCompileError, OSError) as exc:
                        raise UpdateError("The update failed Python validation; the current dashboard was left running.") from exc
            if backup.exists() or backup.is_symlink():
                trusted_directory(backup, self.require_root)
                shutil.rmtree(backup)
            stop_attempted = True
            self.service("stop")
            os.replace(live, backup)
            old_moved = True
            os.replace(candidate, live)
            new_active = True
            self.service("start")
            if not self.health(manifest["version"]):
                raise UpdateError("The updated dashboard did not pass its local health check.")
            metadata_attempted = True
            self.write_metadata(self.install_dir / "update-installed.json",
                                {"version": manifest["version"], "commit": commit, "installed_at": now_iso()})
            return "Dashboard update installed. Radio, Wi-Fi, settings and recordings were preserved."
        except (UpdateError, OSError) as exc:
            if not stop_attempted:
                if isinstance(exc, UpdateError):
                    raise
                raise UpdateError("The dashboard update could not be staged; the current dashboard was left running.") from exc
            restored = False
            metadata_repaired = True
            metadata_cleared = False
            try:
                if new_active:
                    self.service("stop")
                    failed = staging / "failed-dashboard"
                    os.replace(live, failed)
                if old_moved:
                    os.replace(backup, live)
                # Atomic metadata replacement can succeed before directory fsync
                # reports an error. Restore provenance with the previous code.
                if metadata_attempted:
                    try:
                        self.write_metadata(self.install_dir / "update-installed.json", previous)
                    except (UpdateError, OSError):
                        metadata_repaired = False
                        # Clearing uncertain provenance is safer than claiming
                        # the failed commit is installed. Unlink often succeeds
                        # even when a full filesystem prevents a new JSON file.
                        try:
                            (self.install_dir / "update-installed.json").unlink(missing_ok=True)
                            metadata_cleared = True
                        except OSError:
                            pass
                # A metadata repair failure must never prevent restarting the
                # previous dashboard that has already been restored on disk.
                self.service("start")
                restored = self.health(previous["version"])
            except (UpdateError, OSError):
                pass
            if restored:
                if not metadata_repaired:
                    detail = ("Update history was cleared because its metadata could not be saved."
                              if metadata_cleared else
                              "Update history could not be repaired. Rerun the Pi installer before another update.")
                    raise UpdateError("The dashboard update failed. The previous dashboard was restored and restarted. " + detail) from exc
                raise UpdateError("The dashboard update failed. The previous dashboard was restored automatically.") from exc
            location = (" The previous dashboard is in /opt/repeater-box/dashboard.previous."
                        if backup.exists() else " The previous files were restored in /opt/repeater-box/dashboard.")
            raise UpdateError("The dashboard update failed and automatic recovery could not be confirmed. Use Ethernet or a local console to check the dashboard service." + location) from exc
        finally:
            shutil.rmtree(staging, ignore_errors=True)


class Updater:
    def __init__(self, install_dir=INSTALL_DIR, config_path=CONFIG_PATH, fetch=fetch_bytes,
                 installer=None, require_root=True):
        self.install_dir = Path(install_dir)
        self.config_path = Path(config_path)
        self.fetch = fetch
        self.require_root = require_root
        self.installer = installer or DashboardInstaller(self.install_dir, require_root=require_root)

    def apply(self, request):
        result = {"request_id": request["request_id"], "action": request["action"], "success": False,
                  "message": "", "current_version": None, "current_commit": None,
                  "latest_version": None, "latest_commit": None, "update_available": False,
                  "checked_at": now_iso()}
        try:
            trusted_directory(self.install_dir, self.require_root)
            config = read_json(self.config_path, 4096, trusted=self.require_root)
            if config != {"repository": REPOSITORY, "branch": BRANCH}:
                raise UpdateError("The root-owned update source must match the configured public repository and main branch.")
            current = current_install(self.install_dir, self.require_root)
            result.update(current_version=current["version"], current_commit=current.get("commit"))
            commit = latest_commit(self.fetch)
            result["latest_commit"] = commit
            result["update_available"] = commit != current.get("commit")
            if request["action"] == "install" and request["commit"] != commit:
                raise UpdateError("GitHub changed since the last check. Check for updates again before installing.")
            if commit == current.get("commit"):
                result.update(success=True, latest_version=current["version"],
                              message="The latest dashboard commit is already installed.")
                return result
            payload, manifest = read_archive(self.fetch(
                f"https://api.github.com/repos/{REPOSITORY}/tarball/{commit}", MAX_ARCHIVE))
            result["latest_version"] = manifest["version"]
            if tuple(map(int, manifest["version"].split("."))) < tuple(map(int, current["version"].split("."))):
                raise UpdateError("GitHub contains an older dashboard version. Downgrades require a manual Pi installation.")
            if request["action"] == "check":
                result.update(success=True, message="Dashboard update available. Installing updates only the dashboard; Pi and radio configuration are preserved.")
            else:
                message = self.installer.install(payload, manifest, commit)
                result.update(success=True, current_version=manifest["version"], current_commit=commit,
                              update_available=False, message=message)
        except UpdateError as exc:
            result["message"] = str(exc)
        except (OSError, ValueError, UnicodeError) as exc:
            result["message"] = "The dashboard update could not be verified or saved. Check the Pi helper log and retry."
            print("Dashboard update failed: " + type(exc).__name__, file=sys.stderr)
        result["completed_at"] = now_iso()
        return result


def mark_consumed(install_dir, request_id, require_root=True):
    """Keep a bounded root-owned replay ledger, independent of app-writable state."""
    path = install_dir / ".software-update-consumed.json"
    try:
        value = read_json(path, 16384, trusted=require_root)
        identifiers = value.get("request_ids", [])
        if (set(value) != {"request_ids"} or not isinstance(identifiers, list) or len(identifiers) > 256
                or any(not isinstance(item, str) or not REQUEST_RE.fullmatch(item) for item in identifiers)):
            raise ValueError("Invalid update replay ledger")
    except FileNotFoundError:
        identifiers = []
    if request_id in identifiers:
        return False
    write_json(path, {"request_ids": (identifiers + [request_id])[-256:]})
    return True


def main():
    try:
        pi = "Raspberry Pi" in Path("/proc/device-tree/model").read_text()
    except (OSError, UnicodeError):
        pi = False
    if len(sys.argv) != 1 or os.geteuid() != 0 or not pi:
        print("This fixed worker runs as root on the Pi through repeater-software-update.service.", file=sys.stderr)
        return 1
    lock_fd = None
    try:
        trusted_directory(INSTALL_DIR)
        lock_fd = os.open(INSTALL_DIR / ".maintenance.lock",
                          os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        info = os.fstat(lock_fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError("Invalid worker lock")
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            request = read_request(STATE_DIR / REQUEST_NAME)
            write_json(STATE_DIR / RESULT_NAME,
                       {"request_id": request["request_id"], "action": request["action"],
                        "success": False, "message": "Radio programming or another maintenance task is running. Retry when it finishes.",
                        "completed_at": now_iso()}, group=grp.getgrnam("repeaterdash").gr_gid)
            return 0
        request = read_request(STATE_DIR / REQUEST_NAME)
        if not mark_consumed(INSTALL_DIR, request["request_id"]):
            return 0
        result = Updater().apply(request)
        write_json(STATE_DIR / RESULT_NAME, result, group=grp.getgrnam("repeaterdash").gr_gid)
    except (OSError, ValueError, UnicodeError, UpdateError) as exc:
        print("Dashboard update request could not be read or result could not be saved: " + type(exc).__name__, file=sys.stderr)
        return 1
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
