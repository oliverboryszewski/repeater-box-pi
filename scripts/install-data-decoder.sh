#!/bin/bash
# Optional native prerequisite only. Never changes radio or dashboard services.
set -euo pipefail
exec /usr/bin/python3 - "$(dirname -- "$0")" "$@" <<'DATA_DECODER_PY'
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener

VERSION = '1.6.1'
SOURCE_COMMIT = '83761b00d5dd181171b28e8cd2f93b6983fadbc0'
SOURCE_SHA256 = '39c537af9d4fb1d35c8d9bc26fd3c6d0b521440c45059b26da4758765c175b82'
PATCH_SHA256 = 'c20aa9eb27561f524058af241481a3cb2b41da3f42f4ebc2839fd56267e4bae3'
SOURCE_URL = 'https://codeload.github.com/EliasOenal/multimon-ng/tar.gz/' + SOURCE_COMMIT
INSTALL_ROOT = Path('/opt/repeater-box')
INSTALL_DIR = INSTALL_ROOT / 'vendor/multimon-ng'
LOCK_PATH = INSTALL_ROOT / '.maintenance.lock'
MAX_ARCHIVE = 32 * 1024 * 1024
ENVIRONMENT = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LANG': 'C', 'LC_ALL': 'C'}


def protected_directory(path, owner_uid=0):
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != owner_uid or info.st_mode & 0o022:
        raise ValueError('The decoder installation directory is not protected.')


def protected_bytes(path, owner_uid=0, maximum=MAX_ARCHIVE):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != owner_uid or info.st_mode & 0o022
                or not 0 < info.st_size <= maximum):
            raise ValueError('A decoder installation file is not protected.')
        with os.fdopen(descriptor, 'rb', closefd=False) as stream:
            data = stream.read(maximum + 1)
        if len(data) != info.st_size or len(data) > maximum:
            raise ValueError('A decoder installation file changed while reading.')
        return data
    finally:
        os.close(descriptor)


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def download_source(path):
    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, *arguments):
            raise ValueError('The pinned decoder download redirected unexpectedly.')
    opener = build_opener(ProxyHandler({}), NoRedirect())
    total, digest, deadline = 0, hashlib.sha256(), time.monotonic() + 90
    with opener.open(SOURCE_URL, timeout=15) as response, path.open('xb') as stream:
        if response.geturl() != SOURCE_URL:
            raise ValueError('The pinned decoder source changed unexpectedly.')
        while True:
            if time.monotonic() > deadline:
                raise ValueError('The pinned decoder download timed out.')
            chunk = response.read1(65536)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_ARCHIVE:
                raise ValueError('The pinned decoder archive is too large.')
            digest.update(chunk); stream.write(chunk)
        stream.flush(); os.fsync(stream.fileno())
    if digest.hexdigest() != SOURCE_SHA256:
        raise ValueError('The downloaded decoder source checksum did not match.')


def extract_source(archive_path, destination, expected_sha=SOURCE_SHA256):
    if hashlib.sha256(archive_path.read_bytes()).hexdigest() != expected_sha:
        raise ValueError('The decoder source checksum did not match.')
    seen, expanded, count = set(), 0, 0
    root = 'multimon-ng-' + SOURCE_COMMIT
    with tarfile.open(archive_path, 'r:gz') as archive:
        for member in archive:
            count += 1
            name = member.name.rstrip('/')
            parts = name.split('/')
            expanded += member.size
            if (count > 4096 or expanded > 64 * 1024 * 1024 or name in seen
                    or not name or name.startswith('/') or '\\' in name
                    or any(part in ('', '.', '..') for part in parts) or parts[0] != root
                    or not (member.isdir() or member.isreg()) or not 0 <= member.size <= 16 * 1024 * 1024):
                raise ValueError('The decoder source archive has an unsafe entry.')
            seen.add(name)
            if len(parts) == 1:
                if not member.isdir():
                    raise ValueError('The decoder source root must be a directory.')
                continue
            target = destination.joinpath(*parts[1:])
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if member.isdir():
                target.mkdir(mode=0o700, exist_ok=True)
            else:
                data = archive.extractfile(member).read(member.size + 1)
                if len(data) != member.size:
                    raise ValueError('The decoder source archive is incomplete.')
                with target.open('xb') as stream:
                    stream.write(data)
    if not (destination / 'COPYING').is_file() or not (destination / 'CMakeLists.txt').is_file():
        raise ValueError('The pinned decoder source is incomplete.')


def apply_output_patch(source_dir, patch_bytes, expected_sha=PATCH_SHA256):
    if hashlib.sha256(patch_bytes).hexdigest() != expected_sha:
        raise ValueError('The dedicated decoder output patch checksum did not match.')
    lines = patch_bytes.splitlines(keepends=True)
    if (len(lines) < 4 or lines[:2] != [b'--- a/hdlc.c\n', b'+++ b/hdlc.c\n']
            or not re.fullmatch(rb'@@ -[0-9]+,[0-9]+ \+[0-9]+,[0-9]+ @@\n', lines[2])
            or any(line[:1] not in (b' ', b'-', b'+') for line in lines[3:])):
        raise ValueError('The dedicated output patch has an unexpected format.')
    before = b''.join(line[1:] for line in lines[3:] if line[:1] in (b' ', b'-'))
    after = b''.join(line[1:] for line in lines[3:] if line[:1] in (b' ', b'+'))
    target = source_dir / 'hdlc.c'
    content = target.read_bytes()
    if not before or content.count(before) != 1:
        raise ValueError('The dedicated output patch did not match the pinned source exactly.')
    target.write_bytes(content.replace(before, after, 1))


def metadata(binary):
    return {'schema': 1, 'version': VERSION, 'source_commit': SOURCE_COMMIT,
            'source_sha256': SOURCE_SHA256, 'patch_sha256': PATCH_SHA256,
            'binary_sha256': hashlib.sha256(binary).hexdigest(), 'input': 'stdin-only',
            'output_format': 'ax25-hex-v1'}


def existing_install(path, owner_uid=0):
    try:
        protected_directory(path, owner_uid)
        binary = protected_bytes(path / 'multimon-ng', owner_uid, 4 * 1024 * 1024)
        actual = json.loads(protected_bytes(path / 'metadata.json', owner_uid, 4096))
        return (actual == metadata(binary) and stat.S_IMODE((path / 'multimon-ng').lstat().st_mode) == 0o755
                and hashlib.sha256(protected_bytes(path / 'source.tar.gz', owner_uid)).hexdigest() == SOURCE_SHA256
                and hashlib.sha256(protected_bytes(path / 'multimon-ax25-hex.patch', owner_uid, 16384)).hexdigest() == PATCH_SHA256
                and bool(protected_bytes(path / 'COPYING', owner_uid, 65536)))
    except FileNotFoundError:
        return False


def run(arguments, timeout=600):
    subprocess.run(arguments, env=ENVIRONMENT, stdin=subprocess.DEVNULL, check=True, timeout=timeout)


def install(script_dir):
    if (os.geteuid() != 0 or platform.system() != 'Linux' or platform.machine() != 'aarch64'
            or not Path('/proc/device-tree/model').read_text().startswith('Raspberry Pi')):
        raise ValueError('Run this optional installer with sudo on the Raspberry Pi (64-bit Pi OS).')
    protected_directory(INSTALL_ROOT)
    lock = os.open(LOCK_PATH, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(lock)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
            raise ValueError('The existing maintenance lock is not protected.')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('Another maintenance operation is active. Retry after it finishes.') from None
        patch_bytes = (script_dir / 'multimon-ax25-hex.patch').read_bytes()
        if hashlib.sha256(patch_bytes).hexdigest() != PATCH_SHA256:
            raise ValueError('Use the reviewed output patch distributed with this installer.')
        if existing_install(INSTALL_DIR):
            print('The pinned APRS decoder is already installed; no changes were needed.')
            return
        if not os.access('/usr/bin/sox', os.X_OK):
            raise ValueError('Install the project SoX runtime before adding the optional decoder.')
        missing = [name for name in ('cmake', 'gcc', 'make') if shutil.which(name, path=ENVIRONMENT['PATH']) is None]
        if missing:
            run(['/usr/bin/apt-get', 'update'])
            run(['/usr/bin/apt-get', 'install', '-y', '--no-install-recommends', *missing])
        with tempfile.TemporaryDirectory(prefix='repeater-data-build-') as temporary:
            work = Path(temporary)
            archive, source, build = work / 'source.tar.gz', work / 'source', work / 'build'
            source.mkdir(mode=0o700)
            print('Downloading and verifying the pinned decoder source.')
            download_source(archive)
            extract_source(archive, source)
            apply_output_patch(source, patch_bytes)
            run(['/usr/bin/cmake', '-S', str(source), '-B', str(build), '-G', 'Unix Makefiles',
                 '-DCMAKE_BUILD_TYPE=Release', '-DCMAKE_C_FLAGS=-DREPEATER_AX25_HEX',
                 '-DX11_SUPPORT=OFF', '-DPULSE_AUDIO_SUPPORT=OFF', '-DSDL3_SCOPE=OFF', '-DBUILD_GEN_NG=OFF',
                 '-DCMAKE_DISABLE_FIND_PACKAGE_X11=TRUE', '-DCMAKE_DISABLE_FIND_PACKAGE_PulseAudio=TRUE',
                 '-DCMAKE_DISABLE_FIND_PACKAGE_SDL3=TRUE'])
            flags = (build / 'CMakeFiles/multimon-ng.dir/flags.make').read_text()
            if (any(flag not in flags for flag in ('-DDUMMY_AUDIO', '-DNO_X11', '-DNO_SDL3', '-DREPEATER_AX25_HEX'))
                    or any(flag in flags for flag in ('-DPULSE_AUDIO', '-DCOREAUDIO', '-DWIN32_AUDIO'))):
                raise ValueError('The decoder was not configured as a stdin-only build.')
            run(['/usr/bin/nice', '-n', '10', '/usr/bin/cmake', '--build', str(build), '--target', 'multimon-ng', '--parallel', '2'])
            binary = (build / 'multimon-ng').read_bytes()
            if not 0 < len(binary) <= 4 * 1024 * 1024:
                raise ValueError('The built decoder has an unexpected size.')
            probe = subprocess.run([str(build / 'multimon-ng'), '-q', '-c', '-a', 'AFSK1200', '-A', '-t', 'raw', '-'],
                env=ENVIRONMENT, input=b'\0' * 4410, capture_output=True, timeout=10, check=True)
            if probe.stdout:
                raise ValueError('The dedicated decoder produced unexpected output for silence.')
            vendor = INSTALL_DIR.parent
            try:
                vendor.mkdir(mode=0o755)
            except FileExistsError:
                pass
            protected_directory(vendor)
            stage = Path(tempfile.mkdtemp(prefix='.multimon-ng-', dir=vendor))
            backup = vendor / '.multimon-ng.previous'
            moved = False
            try:
                stage.chmod(0o755)
                contents = {'multimon-ng': binary, 'metadata.json': (json.dumps(metadata(binary), indent=2) + '\n').encode('ascii'),
                            'COPYING': (source / 'COPYING').read_bytes(), 'multimon-ax25-hex.patch': patch_bytes,
                            'source.tar.gz': archive.read_bytes()}
                for name, content in contents.items():
                    with (stage / name).open('xb') as stream:
                        os.fchmod(stream.fileno(), 0o755 if name == 'multimon-ng' else 0o644)
                        os.fchown(stream.fileno(), 0, 0)
                        stream.write(content); stream.flush(); os.fsync(stream.fileno())
                sync_directory(stage)
                if backup.exists() or backup.is_symlink():
                    protected_directory(backup)
                    shutil.rmtree(backup)
                if INSTALL_DIR.exists() or INSTALL_DIR.is_symlink():
                    protected_directory(INSTALL_DIR)
                    os.replace(INSTALL_DIR, backup); moved = True
                os.replace(stage, INSTALL_DIR)
                sync_directory(vendor)
                if not existing_install(INSTALL_DIR):
                    raise ValueError('The installed decoder failed its integrity check.')
                if moved:
                    shutil.rmtree(backup)
                sync_directory(vendor)
            except BaseException:
                if moved and backup.exists():
                    if INSTALL_DIR.exists():
                        shutil.rmtree(INSTALL_DIR)
                    os.replace(backup, INSTALL_DIR); sync_directory(vendor)
                raise
            finally:
                if stage.exists():
                    shutil.rmtree(stage)
        print('Pinned receive-only APRS decoder installed. Radio and dashboard services were not changed.')
        print('Enable APRS decoding in Admin after installing the compatible dashboard update.')
    finally:
        os.close(lock)


def main():
    if len(sys.argv) != 2:
        print('Run this optional installer without arguments.', file=sys.stderr)
        return 1
    try:
        install(Path(sys.argv[1]).resolve())
        return 0
    except (OSError, ValueError, subprocess.SubprocessError, tarfile.TarError) as error:
        print('The optional decoder was not installed: ' + str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
DATA_DECODER_PY
