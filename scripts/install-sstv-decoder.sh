#!/bin/bash
# Optional native prerequisite only. Never changes radio or dashboard services.
set -euo pipefail
exec /usr/bin/python3 - "$(dirname -- "$0")" "$@" <<'SSTV_DECODER_PY'
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

VERSION = '0.3.1'
SOURCE_COMMIT = '9761c4c61a6353d7ae2173e1579e434e6c1b1ce3'
SOURCE_SHA256 = '11729f8e83c4e6776f25e7c795ad5cf8e66f7302f04bf6f1d0851b6c50b3a716'
PATCH_SHA256 = 'c0f7fad544dd72f9cb060e6232626192380e9fa9613897264f079a565370c8e1'
WRAPPER_SHA256 = 'b693e733e30267de9c8d3578e598d9c9c75f5c32908abfea5a4016f3b4527821'
SOURCE_URL = 'https://codeload.github.com/unexcellent/sstv/tar.gz/' + SOURCE_COMMIT
INSTALL_ROOT = Path('/opt/repeater-box')
INSTALL_DIR = INSTALL_ROOT / 'vendor/sstv'
LOCK_PATH = INSTALL_ROOT / '.maintenance.lock'
MAX_ARCHIVE = 32 * 1024 * 1024
MAX_BINARY = 8 * 1024 * 1024
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
    root = 'sstv-' + SOURCE_COMMIT
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
    if not (destination / 'LICENSE').is_file() or not (destination / 'Cargo.toml').is_file():
        raise ValueError('The pinned decoder source is incomplete.')


def apply_compat_patch(source_dir, patch_bytes, expected_sha=PATCH_SHA256):
    if hashlib.sha256(patch_bytes).hexdigest() != expected_sha:
        raise ValueError('The SSTV compiler compatibility patch checksum did not match.')
    allowed = {'Cargo.toml', 'src/decode/demodulator.rs', 'src/modes/vis_code.rs', 'src/modes/mode.rs'}
    seen = set()
    for section in patch_bytes.split(b'--- a/')[1:]:
        lines = section.splitlines(keepends=True)
        name = lines[0].decode('ascii').strip()
        if (name not in allowed or name in seen or len(lines) < 4
                or lines[1] != ('+++ b/' + name + '\n').encode('ascii')
                or not re.fullmatch(rb'@@ -[0-9]+,[0-9]+ \+[0-9]+,[0-9]+ @@\n', lines[2])
                or any(line[:1] not in (b' ', b'-', b'+') for line in lines[3:])):
            raise ValueError('The SSTV compatibility patch has an unexpected format.')
        seen.add(name)
        before = b''.join(line[1:] for line in lines[3:] if line[:1] in (b' ', b'-'))
        after = b''.join(line[1:] for line in lines[3:] if line[:1] in (b' ', b'+'))
        path = source_dir / name
        data = path.read_bytes()
        if not before or data.count(before) != 1:
            raise ValueError('The SSTV compatibility patch did not match the pinned source exactly.')
        path.write_bytes(data.replace(before, after, 1))
    if seen != allowed:
        raise ValueError('The SSTV compatibility patch is incomplete.')


def metadata(binary):
    return {'schema': 1, 'engine': 'sstv-robot36', 'version': VERSION,
            'source_commit': SOURCE_COMMIT, 'source_sha256': SOURCE_SHA256,
            'compat_patch_sha256': PATCH_SHA256, 'wrapper_sha256': WRAPPER_SHA256,
            'binary_sha256': hashlib.sha256(binary).hexdigest(),
            'input': 's16le-48000', 'output': 'sstv-json-rows-v1'}


def validate_built_binary(binary):
    if not isinstance(binary, bytes) or not 0 < len(binary) <= MAX_BINARY:
        raise ValueError('The built SSTV decoder has an unexpected size.')


def existing_install(path, owner_uid=0):
    try:
        protected_directory(path, owner_uid)
        binary = protected_bytes(path / 'sstv-robot36', owner_uid, MAX_BINARY)
        actual = json.loads(protected_bytes(path / 'metadata.json', owner_uid, 4096))
        return (actual == metadata(binary) and stat.S_IMODE((path / 'sstv-robot36').lstat().st_mode) == 0o755
                and hashlib.sha256(protected_bytes(path / 'source.tar.gz', owner_uid)).hexdigest() == SOURCE_SHA256
                and hashlib.sha256(protected_bytes(path / 'sstv-rust185.patch', owner_uid, 16384)).hexdigest() == PATCH_SHA256
                and hashlib.sha256(protected_bytes(path / 'sstv-robot36.rs', owner_uid, 16384)).hexdigest() == WRAPPER_SHA256
                and bool(protected_bytes(path / 'LICENSE', owner_uid, 65536)))
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
        patch_bytes = (script_dir / 'sstv-rust185.patch').read_bytes()
        wrapper_bytes = (script_dir / 'sstv-robot36.rs').read_bytes()
        if hashlib.sha256(patch_bytes).hexdigest() != PATCH_SHA256 or hashlib.sha256(wrapper_bytes).hexdigest() != WRAPPER_SHA256:
            raise ValueError('Use the reviewed SSTV wrapper and compatibility patch distributed with this installer.')
        if existing_install(INSTALL_DIR):
            print('The pinned SSTV decoder is already installed; no changes were needed.')
            return
        missing = [name for name in ('rustc', 'gcc') if not os.access('/usr/bin/' + name, os.X_OK)]
        if missing:
            run(['/usr/bin/apt-get', 'update'])
            run(['/usr/bin/apt-get', 'install', '-y', '--no-install-recommends', *missing])
        compiler = subprocess.check_output(['/usr/bin/rustc', '--version'], env=ENVIRONMENT, timeout=10).decode('ascii')
        match = re.match(r'rustc ([0-9]+)\.([0-9]+)\.([0-9]+)', compiler)
        if not match or tuple(map(int, match.groups())) < (1, 85, 0):
            raise ValueError('The SSTV decoder requires Debian Rust 1.85 or newer.')
        with tempfile.TemporaryDirectory(prefix='repeater-sstv-build-') as temporary:
            work = Path(temporary)
            archive, source, build = work / 'source.tar.gz', work / 'source', work / 'build'
            source.mkdir(mode=0o700); build.mkdir(mode=0o700)
            print('Downloading and verifying the pinned SSTV decoder source.')
            download_source(archive)
            extract_source(archive, source)
            apply_compat_patch(source, patch_bytes)
            wrapper = work / 'sstv-robot36.rs'; wrapper.write_bytes(wrapper_bytes)
            # Direct rustc compiles only the reviewed std+alloc core. No Cargo,
            # registry resolver, rustup, optional codec, GUI or audio API runs.
            build_env = dict(ENVIRONMENT, OUT_DIR=str(build))
            def compile(arguments):
                subprocess.run(['/usr/bin/nice', '-n', '10', '/usr/bin/rustc', '--edition=2024',
                                '-C', 'opt-level=3', '-C', 'debuginfo=0', *arguments],
                               env=build_env, stdin=subprocess.DEVNULL, check=True, timeout=180)
            compile([str(source / 'build.rs'), '-o', str(build / 'build-script')])
            subprocess.run([str(build / 'build-script')], env=build_env, stdin=subprocess.DEVNULL, check=True, timeout=10)
            compile(['--crate-name', 'sstv', '--crate-type', 'rlib', '--cfg', 'feature="std"',
                     '--cfg', 'feature="alloc"', str(source / 'src/lib.rs'), '--out-dir', str(build)])
            compile(['--crate-name', 'sstv_robot36', '--extern', 'sstv=' + str(build / 'libsstv.rlib'),
                     '-C', 'strip=symbols', str(wrapper), '-o', str(build / 'sstv-robot36')])
            binary = (build / 'sstv-robot36').read_bytes()
            validate_built_binary(binary)
            probe = subprocess.run([str(build / 'sstv-robot36')], env=ENVIRONMENT,
                input=b'\0' * 48000, capture_output=True, timeout=10, check=True)
            if probe.stdout:
                raise ValueError('The SSTV decoder produced unexpected output for silence.')
            vendor = INSTALL_DIR.parent
            try:
                vendor.mkdir(mode=0o755)
            except FileExistsError:
                pass
            protected_directory(vendor)
            stage = Path(tempfile.mkdtemp(prefix='.sstv-', dir=vendor))
            backup = vendor / '.sstv.previous'
            moved = False
            try:
                stage.chmod(0o755)
                contents = {'sstv-robot36': binary, 'metadata.json': (json.dumps(metadata(binary), indent=2) + '\n').encode('ascii'),
                            'LICENSE': (source / 'LICENSE').read_bytes(), 'sstv-rust185.patch': patch_bytes,
                            'sstv-robot36.rs': wrapper_bytes,
                            'source.tar.gz': archive.read_bytes()}
                for name, content in contents.items():
                    with (stage / name).open('xb') as stream:
                        os.fchmod(stream.fileno(), 0o755 if name == 'sstv-robot36' else 0o644)
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
        print('Pinned receive-only SSTV Robot 36 decoder installed. Radio and dashboard services were not changed.')
        print('Enable SSTV decoding in the Data tab after installing the compatible dashboard update.')
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
        print('The optional SSTV decoder was not installed: ' + str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
SSTV_DECODER_PY
