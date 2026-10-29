#!/usr/bin/python3
"""Install fixed DTMF handlers and prompts; preserve the selected radio mode.

Run the reviewed bundle locally as root. The app updater cannot replace native
SvxLink files. This helper stages every prompt before stopping the one active
managed radio service, and publishes its protected capability marker last.
"""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import time
import types
import wave


# Keep this explicit native allowlist in agreement with dashboard/dtmf_control.py.
NATIVE_SCRIPTS = ("scripts/dtmf-client.py", "scripts/repeater-box-events.tcl",
                  "scripts/repeater-live-events.tcl")
NATIVE_PROMPTS = ("status", "recording_on", "recording_off", "busy", "unavailable", "pair_unknown",
                  "authenticated", "authenticated_data", "choose_status", "choose_recording", "choose_identification",
                  "choose_aprs", "choose_sstv", "status_on", "status_off", "identification_on", "identification_off",
                  "aprs_on", "aprs_off", "sstv_on", "sstv_off", "cancelled", "invalid", "expired",
                  *("pair_" + str(number) for number in range(1, 10)))
NATIVE_FILES = frozenset(NATIVE_SCRIPTS) | frozenset("prompts/" + name + ".wav" for name in NATIVE_PROMPTS)
SERVICES = ("repeater-engine.service", "repeater-rx-monitor.service", "repeater-live.service")
BUSY_SERVICES = ("svxlink.service", "repeater-radio-programming.service", "repeater-message-broadcast.service")
INACTIVE = frozenset(("inactive", "failed", "unknown"))
TOKEN = re.compile(r"[0-9a-f]{32}\Z")
MAX_TOTAL = 32 * 1024 * 1024


class InstallError(Exception):
    """A bounded local check failed; no private settings are included."""


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise InstallError("A local state file has duplicate fields.")
        value[key] = item
    return value


def systemctl(action, unit):
    command = (["/usr/bin/systemctl", "show", "--property=InvocationID", "--value", unit]
               if action == "invocation" else ["/usr/bin/systemctl", action, unit])
    result = subprocess.run(command, check=False,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=60)
    if action == "is-active":
        if result.returncode not in (0, 3, 4):
            raise InstallError("Cannot determine the current radio service state.")
        return result.stdout.strip()
    if result.returncode != 0:
        raise InstallError("A managed radio service could not be stopped or started.")
    if action == "invocation":
        value = result.stdout.strip()
        if TOKEN.fullmatch(value) is None:
            raise InstallError("The live radio service invocation could not be verified.")
        return value


class Installer:
    def __init__(self, bundle, *, install_root=Path("/opt/repeater-box"),
                 radio_state=Path("/var/lib/repeater-radio-backups/state.json"),
                 state_dir=Path("/var/lib/repeater-dashboard"), owner_uid=0, owner_gid=0,
                 runner=systemctl, builder=None, lock_fd=None, pause=time.sleep, native_reader=None):
        self.bundle, self.root = Path(bundle), Path(install_root)
        self.radio_state, self.state_dir = Path(radio_state), Path(state_dir)
        self.owner_uid, self.owner_gid = owner_uid, owner_gid
        self.run, self.builder, self.lock_fd, self.pause = runner, builder, lock_fd, pause
        self.native_reader = native_reader
        self.marker = self.root / "dtmf-controls.json"
        self.lock = self.root / ".maintenance.lock"

    @staticmethod
    def _no_symlink_parents(path):
        for parent in (path, *path.parents):
            if stat.S_ISLNK(parent.lstat().st_mode):
                raise InstallError("A native installation path must not contain symlinks.")

    def _directory(self, path):
        self._no_symlink_parents(path)
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != self.owner_uid or info.st_mode & 0o022:
            raise InstallError("Native installation directories must be protected and root-owned.")

    def _read(self, path, maximum, *, protected=False, optional=False, nonempty=True):
        self._no_symlink_parents(path.parent)
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        except FileNotFoundError:
            if optional:
                return None
            raise
        try:
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > maximum
                    or nonempty and info.st_size == 0
                    or protected and (info.st_uid != self.owner_uid or info.st_mode & 0o022)):
                raise InstallError("A native installation file is unsafe, empty, or too large.")
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                raw = stream.read(maximum + 1)
            after = os.fstat(descriptor)
            if (len(raw) != info.st_size or len(raw) > maximum
                    or (info.st_mtime_ns, info.st_ctime_ns) != (after.st_mtime_ns, after.st_ctime_ns)):
                raise InstallError("A native installation file changed while being checked.")
            return raw
        finally:
            os.close(descriptor)

    @contextmanager
    def _maintenance(self):
        self._directory(self.root)
        descriptor = (os.dup(self.lock_fd) if self.lock_fd is not None else
                      os.open(self.lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600))
        try:
            info, entry = os.fstat(descriptor), self.lock.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != self.owner_uid
                    or info.st_mode & 0o077 or info.st_nlink != 1
                    or (info.st_dev, info.st_ino) != (entry.st_dev, entry.st_ino)):
                raise InstallError("The maintenance lock must be a private root-owned regular file.")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise InstallError("Another radio or software maintenance task is running.") from exc
            yield
        finally:
            os.close(descriptor)

    @staticmethod
    def _maximum(name):
        return 2 * 1024 * 1024 if name.endswith(".wav") else 65536

    def _source(self):
        self._no_symlink_parents(self.bundle)
        source = {name: self._read(self.bundle / name, self._maximum(name)) for name in NATIVE_SCRIPTS}
        compile(source["scripts/dtmf-client.py"], "dtmf-client.py", "exec")
        raw = self._read(self.bundle / "scripts/build-dtmf-prompts.py", 65536)
        module = types.ModuleType("reviewed_dtmf_prompt_builder")
        exec(compile(raw, "build-dtmf-prompts.py", "exec"), module.__dict__)
        if set(module.PROMPTS) != set(NATIVE_PROMPTS):
            raise InstallError("The prompt builder does not match the native DTMF menu.")
        return source, module

    def _native_content(self, files, prompt_module):
        if set(files) != NATIVE_FILES or sum(map(len, files.values())) > MAX_TOTAL:
            raise InstallError("The staged DTMF files are incomplete or too large.")
        durations = {}
        for name in NATIVE_PROMPTS:
            with wave.open(io.BytesIO(files["prompts/" + name + ".wav"]), "rb") as sound:
                if (sound.getnchannels(), sound.getsampwidth(), sound.getframerate(), sound.getcomptype()) != (1, 2, 16000, "NONE"):
                    raise InstallError("A generated DTMF prompt has the wrong audio format.")
                frames = sound.getnframes()
                if not 0 < frames <= 20 * 16000 or len(sound.readframes(frames)) != frames * 2:
                    raise InstallError("A generated DTMF prompt is empty, incomplete, or too long.")
                durations[name] = frames / 16000
        prompt_module.validate_durations(durations)

    def _snapshot(self):
        values = {}
        total = 0
        for name in (*sorted(NATIVE_FILES), "dtmf-controls.json"):
            path = self.root / name
            raw = self._read(path, 16384 if name == "dtmf-controls.json" else self._maximum(name),
                             protected=True, optional=True, nonempty=False)
            info = path.lstat() if raw is not None else None
            values[name] = (raw, stat.S_IMODE(info.st_mode) if info else 0o644,
                            info.st_gid if info else self.owner_gid)
            total += len(raw) if raw is not None else 0
        if total > MAX_TOTAL:
            raise InstallError("Existing native DTMF files exceed the rollback limit.")
        return values

    def _json(self, path, *, protected=True, maximum=262144):
        raw = self._read(path, maximum, protected=protected, optional=True)
        if raw is None:
            return None
        value = json.loads(raw, object_pairs_hook=unique_object)
        if not isinstance(value, dict):
            raise InstallError("A local maintenance state file is invalid.")
        return value

    def _no_pending(self):
        state = self._json(self.radio_state)
        if state is not None:
            if any(not isinstance(value, dict) for value in state.values()):
                raise InstallError("The protected radio state is invalid.")
            for value in state.values():
                for flag in ("verification_required", "release_verification_required"):
                    if flag in value and type(value[flag]) is not bool:
                        raise InstallError("The protected radio state is invalid.")
                    if value.get(flag):
                        raise InstallError("Resolve unfinished radio verification before installing DTMF controls.")
        for prefix in ("radio-programming", "message-broadcast"):
            request = self._json(self.state_dir / (prefix + "-request.json"), protected=False)
            if request is None:
                continue
            token = request.get("request_id")
            result = self._json(self.state_dir / (prefix + "-result.json"))
            if (not isinstance(token, str) or TOKEN.fullmatch(token) is None or result is None
                    or result.get("request_id") != token or type(result.get("success")) is not bool):
                raise InstallError("A queued radio or message task must finish before this installation.")

    def _active(self):
        for unit in BUSY_SERVICES:
            if self.run("is-active", unit) not in INACTIVE:
                raise InstallError("Stop other SvxLink, programming, or message work before this installation.")
        active = []
        for unit in SERVICES:
            state = self.run("is-active", unit)
            if state == "active":
                active.append(unit)
            elif state not in INACTIVE:
                raise InstallError("Wait for the radio service transition to finish.")
        if len(active) > 1:
            raise InstallError("Only one managed radio mode may be active during this installation.")
        return active[0] if active else None

    def _sync(self, directory):
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _replace(self, path, raw, mode=0o644, group=None):
        self._directory(path.parent)
        self._read(path, 2 * 1024 * 1024, protected=True, optional=True, nonempty=False)
        descriptor, temporary = tempfile.mkstemp(prefix=".dtmf-controls-", dir=path.parent)
        try:
            os.fchmod(descriptor, mode)
            os.fchown(descriptor, self.owner_uid, self.owner_gid if group is None else group)
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = None
                stream.write(raw); stream.flush(); os.fsync(stream.fileno())
            os.replace(temporary, path)
            self._sync(path.parent)
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if os.path.lexists(temporary):
                os.unlink(temporary)

    def _remove(self, path):
        self._directory(path.parent)
        if self._read(path, 2 * 1024 * 1024, protected=True, optional=True, nonempty=False) is not None:
            path.unlink(); self._sync(path.parent)

    def _manifest(self, files):
        return (json.dumps({"schema": 1, "protocol": 2, "files": {
            name: hashlib.sha256(files[name]).hexdigest() for name in sorted(NATIVE_FILES)}},
            separators=(",", ":")) + "\n").encode("ascii")

    def _live_reader(self):
        if self.native_reader is not None:
            return self.native_reader
        raw = self._read(self.bundle / "dashboard/native_state.py", 65536)
        module = types.ModuleType("reviewed_native_state")
        exec(compile(raw, "native_state.py", "exec"), module.__dict__)
        return module.NativeState().read

    def _ready(self, active, previous_invocation, native_reader):
        if active == "repeater-live.service":
            invocation = self.run("invocation", active)
            if TOKEN.fullmatch(invocation or "") is None or invocation == previous_invocation:
                raise InstallError("The live radio service did not create a new invocation.")
            for _ in range(20):
                self.pause(0.25)
                if self._active() != active:
                    raise InstallError("The previous radio service did not restart successfully.")
                native = native_reader()
                if (isinstance(native, dict) and native.get("online") is True
                        and native.get("invocation_id") == invocation):
                    return
            raise InstallError("The restarted live handler did not publish a fresh ready state.")
        else:
            # Receive/bench modes have no on-air output. Catch immediate Tcl
            # loading failures before registering those installed handlers.
            for _ in range(4):
                self.pause(0.3)
                if self._active() != active:
                    raise InstallError("The previous radio service did not restart successfully.")

    def _rollback(self, previous, active):
        recovered = True
        if active:
            try:
                self.run("stop", active)
                if self._active() is not None:
                    raise InstallError("A radio service did not stop during recovery.")
            except Exception:
                # Never restore handlers underneath a service that might still
                # be running. Removing the marker prevents a false capability.
                try:
                    self._remove(self.marker)
                except Exception:
                    pass
                return False
        for name in sorted(NATIVE_FILES):
            raw, mode, group = previous[name]
            try:
                if raw is None:
                    self._remove(self.root / name)
                else:
                    self._replace(self.root / name, raw, mode, group)
            except Exception:
                recovered = False
        try:
            self._remove(self.marker)
            if recovered and previous["dtmf-controls.json"][0] is not None:
                self._replace(self.marker, *previous["dtmf-controls.json"])
        except Exception:
            recovered = False
        return recovered

    def install(self, *, register_only=False):
        with self._maintenance():
            for directory in (self.root / "scripts", self.root / "prompts"):
                self._directory(directory)
            source, prompt_module = self._source()
            previous = self._snapshot()
            if register_only:
                if self._active() is not None:
                    raise InstallError("Stop managed radio services before registering native DTMF files.")
                files = {name: previous[name][0] for name in NATIVE_FILES}
                if any(files[name] != source[name] for name in NATIVE_SCRIPTS) or any(raw is None for raw in files.values()):
                    raise InstallError("Installed native DTMF files do not match the reviewed bundle.")
                self._native_content(files, prompt_module)
                try:
                    self._replace(self.marker, self._manifest(files))
                except Exception:
                    old, mode, group = previous["dtmf-controls.json"]
                    self._remove(self.marker)
                    if old is not None:
                        self._replace(self.marker, old, mode, group)
                    raise
                return None
            self._no_pending()
            # Build only in a private same-filesystem directory. No installed
            # handler, radio configuration, boot preference or service changes.
            with tempfile.TemporaryDirectory(prefix=".dtmf-controls-stage-", dir=self.root) as staging:
                prompts = Path(staging) / "prompts"
                (self.builder or prompt_module.build)(prompts)
                files = dict(source)
                for name in NATIVE_PROMPTS:
                    relative = "prompts/" + name + ".wav"
                    files[relative] = self._read(Path(staging) / relative, self._maximum(relative))
                if {path.name for path in prompts.iterdir()} != {name + ".wav" for name in NATIVE_PROMPTS}:
                    raise InstallError("The prompt staging directory contains unexpected files.")
                self._native_content(files, prompt_module)
                self._no_pending()
                active = self._active()
                previous_invocation = self.run("invocation", active) if active == "repeater-live.service" else None
                native_reader = self._live_reader() if active == "repeater-live.service" else None
                changed = False
                try:
                    if active:
                        self.run("stop", active)
                        if self._active() is not None:
                            raise InstallError("The previous radio service did not stop.")
                    changed = True
                    self._remove(self.marker)
                    for name in sorted(NATIVE_FILES):
                        self._replace(self.root / name, files[name])
                    for name in sorted(NATIVE_FILES):
                        if self._read(self.root / name, self._maximum(name), protected=True) != files[name]:
                            raise InstallError("An installed native DTMF file failed verification.")
                    if active:
                        self.run("start", active)
                        self._ready(active, previous_invocation, native_reader)
                    self._replace(self.marker, self._manifest(files))
                except Exception as error:
                    if changed:
                        restored = self._rollback(previous, active)
                        raise InstallError("Native DTMF installation failed; radio services remain stopped. " +
                            ("Previous files were restored." if restored else "Check local files before restarting a radio service.")) from error
                    raise InstallError("The radio service could not be stopped. Native DTMF files were not changed.") from error
                return active


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--register-only", action="store_true")
    parser.add_argument("--lock-fd", type=int, help="Inherited maintenance lock from the full installer")
    args = parser.parse_args(argv)
    if os.geteuid() != 0:
        print("Run this reviewed installer on the Pi with sudo.", file=sys.stderr)
        return 1
    try:
        if b"Raspberry Pi" not in Path("/proc/device-tree/model").read_bytes()[:256]:
            raise InstallError("This installer runs only on a Raspberry Pi.")
        active = Installer(args.bundle, lock_fd=args.lock_fd).install(register_only=args.register_only)
        print("Registered native DTMF APRS and SSTV controls." if args.register_only else
              "Installed native DTMF controls; " + ("restored the previous radio mode." if active else "radio services remain stopped."))
        return 0
    except InstallError as error:
        print(str(error), file=sys.stderr)
        return 1
    except (OSError, ValueError, SyntaxError, subprocess.SubprocessError, wave.Error):
        print("Native DTMF controls were not installed. Review the local installation and service state before retrying.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
