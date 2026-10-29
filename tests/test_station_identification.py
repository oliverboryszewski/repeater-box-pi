"""Persisted automatic-ID preference and its bounded runtime projection."""
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'dashboard'))
from controls import SettingsStore
import station_identification as identification


class StationIdentificationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.runtime = self.root / 'runtime'
        self.runtime.mkdir(mode=0o755)
        self.path = self.runtime / 'station-identification.json'
        self.settings_path = self.root / 'settings.json'
        self.settings = SettingsStore(self.settings_path)

    def controller(self, **kwargs):
        return identification.StationIdentification(self.settings, self.path, **kwargs)

    def test_startup_publishes_default_true_as_exact_compact_world_readable_projection(self):
        controller = self.controller()
        self.assertEqual(controller.status(), {'available': True, 'enabled': True})
        self.assertEqual(self.path.read_bytes(), b'{"schema":1,"enabled":true}\n')
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o644)
        self.assertFalse(self.settings_path.exists())

    def test_toggle_persists_off_and_startup_republishes_it(self):
        controller = self.controller()
        self.assertEqual(controller.set_enabled(False), {'available': True, 'enabled': False})
        self.assertFalse(json.loads(self.settings_path.read_text())['automatic_station_id'])
        self.assertEqual(self.path.read_bytes(), b'{"schema":1,"enabled":false}\n')
        self.path.unlink()
        restored = identification.StationIdentification(SettingsStore(self.settings_path), self.path)
        self.assertEqual(restored.status(), {'available': True, 'enabled': False})
        restored.set_enabled(True)
        self.assertTrue(SettingsStore(self.settings_path).get()['automatic_station_id'])

    def test_only_boolean_updates_are_accepted(self):
        controller = self.controller()
        before = self.path.read_bytes()
        for value in (0, 1, 'false', None, {}, []):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'true or false'):
                controller.set_enabled(value)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(self.settings_path.exists())

    def test_preview_is_unavailable_and_creates_no_runtime_file(self):
        controller = identification.StationIdentification(self.settings, None)
        self.assertEqual(controller.status(), {'available': False, 'enabled': True})
        self.assertEqual(controller.publish(), controller.status())
        with self.assertRaisesRegex(ValueError, 'preview'):
            controller.set_enabled(False)
        self.assertFalse(self.path.exists())
        self.assertFalse(self.settings_path.exists())

    def test_missing_or_untrusted_runtime_directory_is_not_created_or_used(self):
        missing = self.root / 'missing' / 'station-identification.json'
        controller = identification.StationIdentification(self.settings, missing)
        self.assertFalse(controller.status()['available'])
        self.assertFalse(missing.parent.exists())
        with self.assertRaises(ValueError):
            controller.set_enabled(False)
        self.assertTrue(SettingsStore(self.settings_path).get()['automatic_station_id'])
        self.runtime.chmod(0o777)
        self.assertFalse(self.controller().status()['available'])

    def test_symlink_projection_and_symlink_directory_are_refused(self):
        outside = self.root / 'outside.json'
        outside.write_text('unchanged')
        self.path.symlink_to(outside)
        controller = self.controller()
        self.assertFalse(controller.status()['available'])
        with self.assertRaises(ValueError):
            controller.set_enabled(False)
        self.assertEqual(outside.read_text(), 'unchanged')
        self.path.unlink()
        linked = self.root / 'linked'
        linked.symlink_to(self.runtime)
        controller = identification.StationIdentification(self.settings, linked / self.path.name)
        self.assertFalse(controller.status()['available'])

    def test_untrusted_or_oversized_projection_is_refused(self):
        for content, mode in ((b'x' * 65, 0o644), (b'{}', 0o666)):
            with self.subTest(mode=mode):
                self.path.write_bytes(content)
                self.path.chmod(mode)
                controller = self.controller()
                self.assertFalse(controller.status()['available'])
                with self.assertRaises(ValueError):
                    controller.set_enabled(False)
                self.path.unlink()

    def test_missing_or_changed_projection_is_reported_unavailable_until_republished(self):
        controller = self.controller()
        self.path.write_bytes(identification.payload(False))
        self.assertEqual(controller.status(), {'available': False, 'enabled': True})
        controller.publish()
        self.assertTrue(controller.status()['available'])
        self.path.unlink()
        self.assertFalse(controller.status()['available'])
        controller.publish()
        self.assertTrue(controller.status()['available'])

    def test_failed_replace_rolls_back_persistent_setting_and_cleans_temporary_file(self):
        controller = self.controller()
        original_replace = identification.os.replace
        def fail_projection(source, destination):
            if Path(destination) == self.path:
                raise OSError('fixture failure')
            return original_replace(source, destination)
        with patch.object(identification.os, 'replace', side_effect=fail_projection):
            with self.assertRaises(ValueError):
                controller.set_enabled(False)
        self.assertTrue(self.settings.get()['automatic_station_id'])
        self.assertTrue(SettingsStore(self.settings_path).get()['automatic_station_id'])
        self.assertEqual(self.path.read_bytes(), identification.payload(True))
        self.assertEqual(list(self.runtime.iterdir()), [self.path])

    def test_failure_after_projection_replace_restores_previous_setting_and_projection(self):
        controller = self.controller()
        original_write = controller._write_projection
        calls = []
        def write_then_fail_once(enabled):
            original_write(enabled)
            calls.append(enabled)
            if len(calls) == 1:
                raise OSError('fixture directory sync failure')
        with patch.object(controller, '_write_projection', side_effect=write_then_fail_once):
            with self.assertRaises(ValueError):
                controller.set_enabled(False)
        self.assertEqual(calls, [False, True])
        self.assertEqual(controller.status(), {'available': True, 'enabled': True})
        self.assertTrue(SettingsStore(self.settings_path).get()['automatic_station_id'])

    def test_settings_write_failure_does_not_publish_a_new_preference(self):
        controller = self.controller()
        with patch.object(self.settings, 'update', side_effect=OSError('fixture settings failure')):
            with self.assertRaises(ValueError):
                controller.set_enabled(False)
        self.assertEqual(self.path.read_bytes(), identification.payload(True))

    def test_wrong_owner_is_unavailable(self):
        controller = self.controller(owner_uid=os.geteuid() + 1)
        self.assertFalse(controller.status()['available'])
        self.assertFalse(self.path.exists())


if __name__ == '__main__':
    unittest.main()
