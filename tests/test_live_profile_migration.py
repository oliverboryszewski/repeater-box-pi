"""Only a trusted exact legacy profile can migrate, without new RF consent."""
import copy
import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import stat
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fixture_module = load('live_migration_fixtures', ROOT / 'tests/test_live_configuration.py')
migration = load('live_profile_migration', ROOT / 'scripts/migrate-live-profile.py')
guard = migration.guard


class LiveProfileMigrationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture_module.LiveConfigurationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.config_path = self.fixture.paths['config_path']
        self.auth_path = self.fixture.paths['auth_path']

    def migrate(self, **options):
        return migration.migrate(self.config_path, self.auth_path, self.fixture.uid, **options)

    def serialize(self, parser):
        output = io.StringIO()
        parser.write(output, space_around_delimiters=False)
        return output.getvalue().encode('utf-8')

    def legacy(self, enabled=True):
        authorization = self.fixture.prepare(tx_gain_db=-10, id_interval_minutes=30)
        parser = guard.parser_from_bytes(self.config_path.read_bytes())
        parser['RepeaterLogic']['TX'] = 'TxAIOC'
        parser['TxAIOC']['TIMEOUT'] = '15'
        parser.remove_section('TxMonitored')
        parser.remove_section('TxMonitor')
        authorization['enabled'] = enabled
        self.save_profile(parser, authorization)
        guard._validate_legacy_parser(parser, authorization)
        return parser, authorization

    def save_profile(self, parser, authorization):
        content = self.serialize(parser)
        authorization['config_sha256'] = hashlib.sha256(content).hexdigest()
        self.config_path.write_bytes(content)
        self.auth_path.write_bytes(migration.configure.json_bytes(authorization))

    def test_migration_preserves_all_private_values_and_previous_enabled_flag(self):
        for enabled in (True, False):
            with self.subTest(enabled=enabled):
                before_parser, before_auth = self.legacy(enabled)
                private_files = {key: self.fixture.paths[key].read_bytes()
                                 for key in ('state_path', 'catalog_path', 'radio_config_path')}
                with patch.object(guard, 'verified_station', side_effect=AssertionError('No radio state reads')), \
                        patch.object(guard, 'verify_runtime_devices', side_effect=AssertionError('No device access')), \
                        patch.object(migration.configure.subprocess, 'run', side_effect=AssertionError('No services')):
                    self.assertTrue(self.migrate())
                actual = json.loads(self.auth_path.read_bytes())
                self.assertEqual({k: v for k, v in actual.items() if k != 'config_sha256'},
                                 {k: v for k, v in before_auth.items() if k != 'config_sha256'})
                self.assertEqual(actual['config_sha256'], hashlib.sha256(self.config_path.read_bytes()).hexdigest())
                parser = guard.parser_from_bytes(self.config_path.read_bytes())
                guard.validate_parser(parser, actual)
                self.assertEqual(dict(parser['RxAIOC']), dict(before_parser['RxAIOC']))
                expected_tx = dict(before_parser['TxAIOC'], TIMEOUT='40')
                self.assertEqual(dict(parser['TxAIOC']), expected_tx)
                self.assertEqual(actual['tx_gain_db'], -10)
                self.assertEqual(actual['id_interval_minutes'], 30)
                for key, content in private_files.items():
                    self.assertEqual(self.fixture.paths[key].read_bytes(), content)
                self.assertEqual(stat.S_IMODE(self.config_path.stat().st_mode), 0o640)
                self.assertEqual(stat.S_IMODE(self.auth_path.stat().st_mode), 0o640)
                self.assertEqual(self.config_path.stat().st_gid, self.fixture.gid)
                self.assertEqual(self.auth_path.stat().st_gid, self.fixture.gid)

    def test_exact_legacy_is_rejected_by_normal_startup_parser(self):
        parser, authorization = self.legacy()
        with self.assertRaises(ValueError):
            guard.validate_parser(parser, authorization)

    def test_current_profile_and_fresh_installation_are_write_free_noops(self):
        with patch.object(migration.configure, 'atomic_write', side_effect=AssertionError('No write')):
            self.assertFalse(self.migrate())
        self.legacy()
        self.assertTrue(self.migrate())
        before = (self.config_path.read_bytes(), self.auth_path.read_bytes())
        with patch.object(migration.configure, 'atomic_write', side_effect=AssertionError('No write')):
            self.assertFalse(self.migrate())
        self.assertEqual(before, (self.config_path.read_bytes(), self.auth_path.read_bytes()))

    def test_malformed_untrusted_or_modified_legacy_is_rejected_before_writes(self):
        changes = [
            lambda parser, auth: parser['TxAIOC'].update(TIMEOUT='40'),
            lambda parser, auth: parser['TxAIOC'].update(PTT_PIN='RTS'),
            lambda parser, auth: parser['TxAIOC'].update(SERIAL_SET_PINS='!RTS'),
            lambda parser, auth: parser['RepeaterLogic'].update(TX='TxMonitored'),
            lambda parser, auth: parser['RxAIOC'].update(VOX_THRESH='nan'),
            lambda parser, auth: parser.add_section('TxMonitor'),
            lambda parser, auth: auth.update(enabled=1),
            lambda parser, auth: auth.update(unexpected=True),
        ]
        for change in changes:
            with self.subTest(change=change):
                parser, auth = self.legacy()
                change(parser, auth)
                self.save_profile(parser, auth)
                before = (self.config_path.read_bytes(), self.auth_path.read_bytes())
                with patch.object(migration.configure, 'atomic_write', side_effect=AssertionError('No write')):
                    with self.assertRaises(ValueError):
                        self.migrate()
                self.assertEqual(before, (self.config_path.read_bytes(), self.auth_path.read_bytes()))
        self.legacy()
        self.config_path.write_bytes(self.config_path.read_bytes() + b'\n# changed\n')
        with patch.object(migration.configure, 'atomic_write', side_effect=AssertionError('No write')):
            with self.assertRaisesRegex(ValueError, 'changed'):
                self.migrate()
        self.legacy()
        self.auth_path.chmod(0o666)
        with patch.object(migration.configure, 'atomic_write', side_effect=AssertionError('No write')):
            with self.assertRaisesRegex(ValueError, 'unsafe'):
                self.migrate()

    def test_missing_half_or_symlink_does_not_create_or_replace_files(self):
        self.legacy()
        original = self.config_path.read_bytes()
        self.auth_path.unlink()
        with self.assertRaises(ValueError):
            self.migrate()
        self.assertEqual(self.config_path.read_bytes(), original)
        self.assertFalse(self.auth_path.exists())
        self.auth_path.symlink_to(self.fixture.paths['state_path'])
        with self.assertRaises(OSError):
            self.migrate()
        self.assertTrue(self.auth_path.is_symlink())

    def test_untrusted_config_parent_or_owner_is_rejected_without_writes(self):
        self.legacy()
        before = (self.config_path.read_bytes(), self.auth_path.read_bytes())
        self.config_path.chmod(0o666)
        with self.assertRaisesRegex(ValueError, 'unsafe'):
            self.migrate()
        self.config_path.chmod(0o640)
        with self.assertRaisesRegex(ValueError, 'unsafe|protected'):
            migration.migrate(self.config_path, self.auth_path, self.fixture.uid + 1)
        self.fixture.root.chmod(0o777)
        try:
            with self.assertRaisesRegex(ValueError, 'protected'):
                self.migrate()
        finally:
            self.fixture.root.chmod(0o700)
        self.assertEqual(before, (self.config_path.read_bytes(), self.auth_path.read_bytes()))

    def test_write_order_and_faults_always_revoke_even_after_final_rename(self):
        for failure_index in (None, 1, 2, 3):
            for after_write in (False, True):
                with self.subTest(failure_index=failure_index, after_write=after_write):
                    self.legacy()
                    writes = []

                    def writer(path, content, group_id, owner_uid):
                        writes.append((path, content))
                        if len(writes) == failure_index and not after_write:
                            raise OSError('Fixture write failed before replacement')
                        migration.configure.atomic_write(path, content, group_id, owner_uid)
                        if len(writes) == failure_index and after_write:
                            raise OSError('Fixture directory flush failed after replacement')

                    if failure_index is None:
                        self.assertTrue(self.migrate(writer=writer))
                        self.assertTrue(json.loads(self.auth_path.read_bytes())['enabled'])
                    else:
                        with self.assertRaises(OSError):
                            self.migrate(writer=writer)
                        self.assertFalse(json.loads(self.auth_path.read_bytes())['enabled'])
                        with self.assertRaisesRegex(ValueError, 'not authorized'):
                            self.fixture.check()
                    self.assertEqual(writes[0][0], self.auth_path)
                    self.assertFalse(json.loads(writes[0][1])['enabled'])
                    if len(writes) > 1:
                        self.assertEqual(writes[1][0], self.config_path)
                    if len(writes) > 2:
                        self.assertEqual(writes[2][0], self.auth_path)

    def test_failed_emergency_revocation_removes_authorization(self):
        self.legacy()

        def failed_writer(*args):
            raise OSError('Fixture storage failure')

        with patch.object(migration.configure, 'atomic_write', side_effect=failed_writer):
            with self.assertRaises(OSError):
                self.migrate()
        self.assertFalse(self.auth_path.exists())

    def test_pending_radio_upload_can_migrate_but_cannot_start(self):
        self.legacy()
        pending = copy.deepcopy(self.fixture.state)
        pending['tx']['verification_required'] = True
        self.fixture.write('state_path', pending)
        self.assertTrue(self.migrate())
        self.assertTrue(json.loads(self.auth_path.read_bytes())['enabled'])
        with self.assertRaisesRegex(ValueError, 'unfinished'):
            self.fixture.check()

    def test_standalone_cli_takes_one_lock_and_requires_stopped_service(self):
        lock = self.fixture.root / 'maintenance.lock'
        lock.write_bytes(b'')
        lock.chmod(0o600)
        with patch.object(migration.configure, 'LOCK_PATH', lock), \
                patch.object(migration.os, 'geteuid', return_value=0), \
                patch.object(migration.os, 'fstat', return_value=SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_uid=0)), \
                patch.object(migration.fcntl, 'flock') as flock, \
                patch.object(migration.configure, 'live_stopped') as stopped, \
                patch.object(migration, 'migrate', return_value=False) as migrate, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(migration.main([]), 0)
        self.assertEqual(flock.call_count, 1)
        self.assertEqual(flock.call_args.args[1], migration.fcntl.LOCK_EX | migration.fcntl.LOCK_NB)
        stopped.assert_called_once_with()
        migrate.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
