"""Pinned SSTV prerequisite integrity checks without apt, hardware or services."""
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
SCRIPT = ROOT / 'scripts/install-sstv-decoder.sh'
PATCH = ROOT / 'scripts/sstv-rust185.patch'
WRAPPER = ROOT / 'scripts/sstv-robot36.rs'


def installer():
    source = SCRIPT.read_text().split("<<'SSTV_DECODER_PY'\n", 1)[1].rsplit('\nSSTV_DECODER_PY', 1)[0]
    namespace = {'__name__': 'decoder_installer_test'}
    exec(compile(source, str(SCRIPT), 'exec'), namespace)
    return namespace


class SstvDecoderInstallTests(unittest.TestCase):
    def setUp(self):
        self.code = installer()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def archive(self, entries):
        raw = io.BytesIO()
        root = 'sstv-' + self.code['SOURCE_COMMIT']
        with tarfile.open(fileobj=raw, mode='w:gz') as tar:
            for name, data, kind in [('LICENSE', b'fixture license', None),
                                     ('Cargo.toml', b'fixture build', None)] + entries:
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
        self.assertEqual(self.code['VERSION'], '0.3.1')
        self.assertEqual(self.code['SOURCE_COMMIT'], '9761c4c61a6353d7ae2173e1579e434e6c1b1ce3')
        self.assertEqual(self.code['SOURCE_SHA256'], '11729f8e83c4e6776f25e7c795ad5cf8e66f7302f04bf6f1d0851b6c50b3a716')
        self.assertEqual(hashlib.sha256(PATCH.read_bytes()).hexdigest(), self.code['PATCH_SHA256'])
        self.assertEqual(self.code['metadata'](b'fixture')['output'], 'sstv-json-rows-v1')
        self.assertEqual(hashlib.sha256(WRAPPER.read_bytes()).hexdigest(), self.code['WRAPPER_SHA256'])
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

    def test_compatibility_patch_is_exact_and_cannot_change_another_source_file(self):
        patch = PATCH.read_bytes()
        expected = {}
        for section in patch.split(b'--- a/')[1:]:
            lines = section.splitlines(keepends=True)
            name = lines[0].decode().strip()
            before = b''.join(line[1:] for line in lines[3:] if line[:1] in (b' ', b'-'))
            after = b''.join(line[1:] for line in lines[3:] if line[:1] in (b' ', b'+'))
            path = self.root / name; path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'prefix\n' + before + b'suffix\n')
            expected[name] = b'prefix\n' + after + b'suffix\n'
        self.code['apply_compat_patch'](self.root, patch)
        self.assertEqual(set(expected), {'Cargo.toml', 'src/decode/demodulator.rs',
            'src/modes/vis_code.rs', 'src/modes/mode.rs'})
        for name, content in expected.items(): self.assertEqual((self.root / name).read_bytes(), content)
        with self.assertRaisesRegex(ValueError, 'match'):
            self.code['apply_compat_patch'](self.root, patch)
        with self.assertRaisesRegex(ValueError, 'checksum'):
            self.code['apply_compat_patch'](self.root, patch + b'edited')
        self.assertIn(b'ones % 2 != 0', patch)
        self.assertIn(b'(self.0 as i64 + other.0 as i64) / 2', patch)


    def test_existing_install_requires_matching_binary_patch_source_license_and_permissions(self):
        directory = self.root / 'install'; directory.mkdir(mode=0o755)
        source = b'pinned source fixture'
        self.code['SOURCE_SHA256'] = hashlib.sha256(source).hexdigest()
        binary = b'executable fixture'
        for name, data in {'sstv-robot36': binary, 'source.tar.gz': source,
                           'sstv-rust185.patch': PATCH.read_bytes(), 'sstv-robot36.rs': WRAPPER.read_bytes(), 'LICENSE': b'fixture license',
                           'metadata.json': json.dumps(self.code['metadata'](binary)).encode()}.items():
            path = directory / name; path.write_bytes(data); path.chmod(0o755 if name == 'sstv-robot36' else 0o644)
        self.assertTrue(self.code['existing_install'](directory, os.getuid()))
        (directory / 'sstv-robot36').write_bytes(b'changed binary')
        self.assertFalse(self.code['existing_install'](directory, os.getuid()))
        (directory / 'sstv-robot36').write_bytes(binary)
        (directory / 'sstv-robot36').chmod(0o777)
        with self.assertRaises(ValueError):
            self.code['existing_install'](directory, os.getuid())
        (directory / 'sstv-robot36').unlink()
        (directory / 'sstv-robot36').symlink_to(directory / 'LICENSE')
        with self.assertRaises(OSError):
            self.code['existing_install'](directory, os.getuid())

    def test_installer_has_no_service_or_device_management_and_retains_native_ownership(self):
        source = SCRIPT.read_text()
        for forbidden in ('systemctl', 'usermod', 'groupadd', 'udevadm', '/dev/snd', '/dev/tty', '/dev/hidraw'):
            self.assertNotIn(forbidden, source)
        self.assertIn('fcntl.LOCK_EX | fcntl.LOCK_NB', source)
        self.assertIn("'/usr/bin/rustc', '--edition=2024'", source)
        self.assertIn("'feature=\"std\"'", source)
        self.assertIn("'feature=\"alloc\"'", source)
        self.assertNotIn('cargo build', source)
        self.assertNotIn('rustup ', source)
        self.assertIn("'source.tar.gz': archive.read_bytes()", source)
        self.assertIn("'LICENSE': (source / 'LICENSE').read_bytes()", source)
        wrapper = WRAPPER.read_text()
        self.assertIn('SSTV1 ', wrapper)
        self.assertIn('output.flush()?', wrapper)
        self.assertIn('with_mode(ROBOT_36)', wrapper)
        self.assertNotIn('std::net', wrapper)
        self.assertNotIn('std::fs', wrapper)


if __name__ == '__main__':
    unittest.main()
