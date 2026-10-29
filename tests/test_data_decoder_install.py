"""Pinned optional decoder integrity checks without apt, hardware or services."""
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/install-data-decoder.sh'
PATCH = ROOT / 'scripts/multimon-ax25-hex.patch'


def installer():
    source = SCRIPT.read_text().split("<<'DATA_DECODER_PY'\n", 1)[1].rsplit('\nDATA_DECODER_PY', 1)[0]
    namespace = {'__name__': 'decoder_installer_test'}
    exec(compile(source, str(SCRIPT), 'exec'), namespace)
    return namespace


class DataDecoderInstallTests(unittest.TestCase):
    def setUp(self):
        self.code = installer()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def archive(self, entries):
        raw = io.BytesIO()
        root = 'multimon-ng-' + self.code['SOURCE_COMMIT']
        with tarfile.open(fileobj=raw, mode='w:gz') as tar:
            for name, data, kind in [('COPYING', b'fixture license', None),
                                     ('CMakeLists.txt', b'fixture build', None)] + entries:
                member = tarfile.TarInfo(root + '/' + name)
                if kind is not None:
                    member.type = kind
                    member.linkname = '/tmp/unrelated'
                    tar.addfile(member)
                else:
                    member.size = len(data)
                    tar.addfile(member, io.BytesIO(data))
        path = self.root / 'source.tar.gz'; path.write_bytes(raw.getvalue())
        return path, hashlib.sha256(raw.getvalue()).hexdigest()

    def test_shell_syntax_and_fixed_pin_patch_contract(self):
        subprocess.run(['/bin/bash', '-n', str(SCRIPT)], check=True)
        self.assertEqual(self.code['VERSION'], '1.6.1')
        self.assertEqual(self.code['SOURCE_COMMIT'], '83761b00d5dd181171b28e8cd2f93b6983fadbc0')
        self.assertEqual(self.code['SOURCE_SHA256'], '39c537af9d4fb1d35c8d9bc26fd3c6d0b521440c45059b26da4758765c175b82')
        self.assertEqual(hashlib.sha256(PATCH.read_bytes()).hexdigest(), self.code['PATCH_SHA256'])
        self.assertEqual(self.code['metadata'](b'fixture')['output_format'], 'ax25-hex-v1')
        self.assertEqual(self.code['metadata'](b'fixture')['binary_sha256'], hashlib.sha256(b'fixture').hexdigest())

    def test_bad_source_checksum_rejects_before_extracting(self):
        path, digest = self.archive([('file.c', b'source', None)])
        destination = self.root / 'source'; destination.mkdir()
        with self.assertRaisesRegex(ValueError, 'checksum'):
            self.code['extract_source'](path, destination, '0' * 64)
        self.assertEqual(list(destination.iterdir()), [])
        self.code['extract_source'](path, destination, digest)
        self.assertEqual((destination / 'file.c').read_bytes(), b'source')

    def test_archive_rejects_links_traversal_and_duplicate_entries(self):
        for entries in ([('../outside', b'bad', None)], [('file', b'', tarfile.SYMTYPE)],
                        [('file', b'', tarfile.LNKTYPE)], [('file', b'', tarfile.FIFOTYPE)],
                        [('same', b'one', None), ('same', b'two', None)]):
            with self.subTest(entries=entries):
                path, digest = self.archive(entries)
                destination = self.root / str(len(list(self.root.iterdir()))); destination.mkdir()
                with self.assertRaises(ValueError):
                    self.code['extract_source'](path, destination, digest)
        self.assertFalse((self.root / 'outside').exists())

    def test_output_patch_is_exact_and_preserves_crc_demod_source_outside_output_hunk(self):
        patch = PATCH.read_bytes()
        lines = patch.splitlines(keepends=True)[3:]
        before = b''.join(line[1:] for line in lines if line[:1] in (b' ', b'-'))
        after = b''.join(line[1:] for line in lines if line[:1] in (b' ', b'+'))
        source = self.root / 'hdlc.c'
        prefix, suffix = b'/* CRC unchanged */\n', b'/* demod unchanged */\n'
        source.write_bytes(prefix + before + suffix)
        self.code['apply_output_patch'](self.root, patch)
        self.assertEqual(source.read_bytes(), prefix + after + suffix)
        self.assertIn(b'REPEATER_AX25_HEX', after)
        self.assertIn(b'AX25HEX:', after)
        self.assertNotIn(b'check_crc', patch)
        with self.assertRaisesRegex(ValueError, 'match'):
            self.code['apply_output_patch'](self.root, patch)
        with self.assertRaisesRegex(ValueError, 'checksum'):
            self.code['apply_output_patch'](self.root, patch + b'edited')

    def test_existing_install_requires_matching_binary_patch_source_license_and_permissions(self):
        directory = self.root / 'install'; directory.mkdir(mode=0o755)
        source = b'pinned source fixture'
        self.code['SOURCE_SHA256'] = hashlib.sha256(source).hexdigest()
        binary = b'executable fixture'
        for name, data in {'multimon-ng': binary, 'source.tar.gz': source,
                           'multimon-ax25-hex.patch': PATCH.read_bytes(), 'COPYING': b'fixture license',
                           'metadata.json': json.dumps(self.code['metadata'](binary)).encode()}.items():
            path = directory / name; path.write_bytes(data); path.chmod(0o755 if name == 'multimon-ng' else 0o644)
        self.assertTrue(self.code['existing_install'](directory, os.getuid()))
        (directory / 'multimon-ng').write_bytes(b'changed binary')
        self.assertFalse(self.code['existing_install'](directory, os.getuid()))
        (directory / 'multimon-ng').write_bytes(binary)
        (directory / 'multimon-ng').chmod(0o777)
        with self.assertRaises(ValueError):
            self.code['existing_install'](directory, os.getuid())
        (directory / 'multimon-ng').unlink()
        (directory / 'multimon-ng').symlink_to(directory / 'COPYING')
        with self.assertRaises(OSError):
            self.code['existing_install'](directory, os.getuid())

    def test_installer_has_no_service_or_device_management_and_retains_native_ownership(self):
        source = SCRIPT.read_text()
        for forbidden in ('systemctl', 'usermod', 'groupadd', 'udevadm', '/dev/snd', '/dev/tty', '/dev/hidraw'):
            self.assertNotIn(forbidden, source)
        self.assertIn('fcntl.LOCK_EX | fcntl.LOCK_NB', source)
        self.assertIn("'-DDUMMY_AUDIO'", source)
        self.assertIn("'-DREPEATER_AX25_HEX'", source)
        self.assertIn("'source.tar.gz': archive.read_bytes()", source)
        self.assertIn("'COPYING': (source / 'COPYING').read_bytes()", source)


if __name__ == '__main__':
    unittest.main()
