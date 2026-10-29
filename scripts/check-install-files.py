#!/usr/bin/python3
"""Check fixed installer inputs and copied files without changing any files."""
import argparse
import hashlib
from pathlib import Path
import stat
import sys


# Only files that the installer always replaces belong here. Locally editable
# configuration, generated prompts, credentials and runtime state are excluded.
COPY_GROUPS = (
    ("dashboard", "/opt/repeater-box/dashboard", (
        "server.py", "controls.py", "audio_hub.py", "admin_password.py",
        "wifi_visibility.py", "software_updates.py", "radio_programming.py",
        "radio_editor.py", "dtmf_control.py", "station_identification.py",
        "native_state.py", "radio_messages.py")),
    ("scripts", "/opt/repeater-box/scripts", (
        "apply-wifi-visibility.py", "apply-software-update.py", "program-radio.py",
        "radio_plan.py", "configure-channel-pairs.py", "configure-radios.py",
        "configure-pair-switching.py", "configure-dtmf.py", "dtmf-client.py",
        "repeater-box-events.tcl", "run-svxlink.py", "build-dtmf-prompts.py",
        "check-bench-config.py", "configure-live.py", "check-live-config.py",
        "repeater-live-events.tcl", "migrate-live-profile.py",
        "convert-message-audio.py", "broadcast-message.py",
        "install-data-decoder.sh", "multimon-ax25-hex.patch",
        "install-sstv-decoder.sh", "sstv-robot36.rs", "sstv-rust185.patch")),
    ("dashboard/static", "/opt/repeater-box/dashboard/static", (
        "index.html", "app.js", "style.css", "audio-visualizer.js",
        "audio-visualizer.css", "radio-editor.html", "radio-editor.js",
        "radio-editor.css")),
    ("systemd", "/etc/systemd/system", (
        "repeater-dashboard.service", "repeater-engine.service",
        "repeater-rx-monitor.service", "repeater-live.service",
        "repeater-wifi-visibility.service", "repeater-wifi-visibility.path",
        "repeater-software-update.service", "repeater-software-update.path",
        "repeater-radio-programming.service", "repeater-radio-programming.path",
        "repeater-dtmf-code.service", "repeater-dtmf-code.path",
        "repeater-message-broadcast.service", "repeater-message-broadcast.path")),
)
COPY_FILES = tuple(
    (f"{source}/{name}", f"{target}/{name}")
    for source, target, names in COPY_GROUPS for name in names
) + (
    ("software-update.json", "/opt/repeater-box/software-update.json"),
    ("scripts/check-bench-config.py", "/opt/repeater-box/check-bench-config.py"),
)
SOURCE_ONLY = (
    "scripts/check-install-files.py", "scripts/install-chirp.sh",
    "config/updates.json", "config/svxlink-bench.conf",
)
REQUIRED_SOURCES = tuple(sorted({source for source, _ in COPY_FILES} | set(SOURCE_ONLY)))


class IntegrityError(Exception):
    """An installer input or copied file cannot be trusted as complete."""


def fingerprint(path):
    """Refuse missing, empty and nonregular files before reading their bytes."""
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size == 0:
            raise IntegrityError(f"Expected a nonempty regular file: {path}")
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as stream:
            while chunk := stream.read(65536):
                digest.update(chunk)
                size += len(chunk)
        if size != info.st_size:
            raise IntegrityError(f"File changed while checking it: {path}")
        return size, digest.digest()
    except OSError as error:
        raise IntegrityError(f"Cannot read required file: {path}: {error.strerror}") from error


def check_sources(bundle):
    return {name: fingerprint(bundle / name) for name in REQUIRED_SOURCES}


def check_installed(bundle, installed_root=Path("/")):
    sources = check_sources(bundle)
    for source, target in COPY_FILES:
        path = installed_root / target.lstrip("/")
        if fingerprint(path) != sources[source]:
            raise IntegrityError(f"Installed file does not match the bundle: {target}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--verify-installed", action="store_true")
    arguments = parser.parse_args(argv)
    try:
        if arguments.verify_installed:
            check_installed(arguments.bundle)
            print("Verified copied software and service files against the bundle.")
        else:
            check_sources(arguments.bundle)
            print("Verified required installer inputs are nonempty regular files.")
    except IntegrityError as error:
        print(f"Installation integrity check failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
