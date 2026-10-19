"""Admin authorization and settings persistence checks, without network access."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "dashboard"))
import controls


class AdminAuthTest(unittest.TestCase):
    PASSWORD = "test42"  # Exercise the six-character minimum through hash/login.

    @classmethod
    def setUpClass(cls):
        cls.record = controls.password_record(cls.PASSWORD)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "admin.json"
        controls.write_json(self.path, self.record)
        self.auth = controls.AdminAuth(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_password_is_salted_and_never_stored_in_plaintext(self):
        another = controls.password_record(self.PASSWORD)
        self.assertNotEqual(self.record["salt"], another["salt"])
        self.assertNotEqual(self.record["digest"], another["digest"])
        self.assertNotIn(self.PASSWORD, self.path.read_text())
        for invalid in ("short", "x" * 257, None):
            with self.subTest(invalid=type(invalid).__name__):
                with self.assertRaises(ValueError):
                    controls.password_record(invalid)

    def test_mutation_needs_both_current_session_and_matching_csrf_token(self):
        token, session = self.auth.login(self.PASSWORD, "127.0.0.1")
        csrf = session["csrf_token"]
        self.assertTrue(self.auth.authorize(token, csrf))
        for supplied_token, supplied_csrf in ((None, csrf), ("made-up", csrf),
                                               (token, None), (token, "made-up"),
                                               (token, 123)):
            self.assertFalse(self.auth.authorize(supplied_token, supplied_csrf))
        self.auth.logout(token)
        self.assertFalse(self.auth.authorize(token, csrf))

    def test_password_reset_invalidates_existing_sessions_without_restart(self):
        token, session = self.auth.login(self.PASSWORD, "127.0.0.1")
        # Even setting the same password generates a new salt and invalidates it.
        controls.write_json(self.path, controls.password_record(self.PASSWORD))
        self.assertFalse(self.auth.authorize(token, session["csrf_token"]))
        replacement, replacement_session = self.auth.login(self.PASSWORD, "127.0.0.1")
        self.assertNotEqual(token, replacement)
        self.assertTrue(self.auth.authorize(replacement, replacement_session["csrf_token"]))

    def test_sessions_expire_after_eight_hours(self):
        with mock.patch.object(controls.time, "monotonic", return_value=1000):
            token, session = self.auth.login(self.PASSWORD, "127.0.0.1")
        with mock.patch.object(controls.time, "monotonic", return_value=1000 + 8 * 3600):
            self.assertFalse(self.auth.authorize(token, session["csrf_token"]))

    def test_repeated_failures_are_limited_and_expire(self):
        with mock.patch.object(controls.time, "monotonic", return_value=1000), \
                mock.patch.object(controls.hashlib, "pbkdf2_hmac", return_value=b"\x00" * 32) as derive:
            for _ in range(5):
                with self.assertRaisesRegex(PermissionError, "Incorrect"):
                    self.auth.login("wrong", "127.0.0.1")
            with self.assertRaisesRegex(PermissionError, "Too many"):
                self.auth.login("wrong", "127.0.0.1")
            self.assertEqual(derive.call_count, 5)
        with mock.patch.object(controls.time, "monotonic", return_value=1061):
            token, session = self.auth.login(self.PASSWORD, "127.0.0.1")
            self.assertTrue(self.auth.authorize(token, session["csrf_token"]))

    def test_missing_or_corrupt_password_file_is_unconfigured(self):
        invalid_records = ("not json", "[]", "null", "1", '"text"',
                           '{"algorithm":"wrong"}',
                           '{"algorithm":"pbkdf2_sha256","iterations":600000,"salt":"nohex","digest":"ff"}')
        for body in invalid_records:
            with self.subTest(body=body):
                self.path.write_text(body)
                self.assertFalse(self.auth.session(None)["configured"])
                with self.assertRaisesRegex(ValueError, "not configured"):
                    self.auth.login(self.PASSWORD, "127.0.0.1")
        self.path.unlink()
        self.assertFalse(self.auth.session(None)["configured"])


class SettingsStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "nested" / "settings.json"
        self.store = controls.SettingsStore(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_settings_persist_and_returned_copies_cannot_mutate_the_store(self):
        values = dict(controls.DEFAULTS, station_name="  Portable Test  ",
                      retention_days=30, input_enabled=True, max_archive_mb=512,
                      keep_indefinitely=True)
        saved = self.store.update(values)
        self.assertEqual(saved["station_name"], "Portable Test")
        self.assertEqual(controls.SettingsStore(self.path).get(), saved)
        self.assertEqual(json.loads(self.path.read_text()), saved)
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)
        saved["station_name"] = "mutated copy"
        copy = self.store.get()
        copy["retention_days"] = 1
        self.assertEqual(self.store.get()["station_name"], "Portable Test")
        self.assertEqual(self.store.get()["retention_days"], 30)

    def test_invalid_types_ranges_and_unknown_fields_do_not_change_saved_settings(self):
        before = self.store.update(controls.DEFAULTS)
        disk_before = self.path.read_bytes()
        invalid = ({"input_enabled": "false"}, {"record_on_startup": 1},
                   {"keep_indefinitely": "false"}, {"keep_indefinitely": 1}, {"keep_indefinitely": None},
                   {"retention_days": True}, {"retention_days": 0}, {"retention_days": 91},
                   {"max_archive_mb": 63}, {"max_archive_mb": 16385},
                   {"recording_threshold_dbfs": -71}, {"recording_threshold_dbfs": -9},
                   {"retention_days": 7.0}, {"station_name": "  "},
                   {"station_name": "name\nwith control"}, {"station_name": "x" * 49},
                   {"unexpected": "field"}, [], None)
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.store.update(value)
                self.assertEqual(self.store.get(), before)
                self.assertEqual(self.path.read_bytes(), disk_before)

    def test_failed_atomic_write_keeps_previous_memory_and_disk_values(self):
        before = self.store.update(controls.DEFAULTS)
        disk_before = self.path.read_bytes()
        with mock.patch.object(controls.os, "replace", side_effect=OSError("disk unavailable")):
            with self.assertRaises(OSError):
                self.store.update(dict(before, station_name="Replacement"))
        self.assertEqual(self.store.get(), before)
        self.assertEqual(self.path.read_bytes(), disk_before)
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_legacy_settings_default_to_age_expiry_and_missing_update_fields_are_preserved(self):
        controls.write_json(self.path, {"record_on_startup": True})
        store = controls.SettingsStore(self.path)
        self.assertFalse(store.get()["keep_indefinitely"])
        self.assertTrue(store.get()["record_on_startup"])
        store.update({"keep_indefinitely": True})
        # An older open page knows all previous settings but omits the new flag.
        old_page = {key: value for key, value in store.get().items() if key != "keep_indefinitely"}
        old_page["station_name"] = "Updated from an older page"
        store.update(old_page)
        self.assertTrue(store.get()["keep_indefinitely"])
        self.assertTrue(controls.SettingsStore(self.path).get()["keep_indefinitely"])


if __name__ == "__main__":
    unittest.main()
