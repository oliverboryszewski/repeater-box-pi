"""Exercise the privileged worker with fake NetworkManager; no real Wi-Fi changes."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("wifi_visibility_worker", ROOT / "scripts/apply-wifi-visibility.py")
worker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(worker)
UUID = "12345678-1234-1234-1234-123456789abc"
OTHER_UUID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
REQUEST = {"request_id": "a" * 32, "hidden": True}


class FakeNetworkManager:
    def __init__(self, active=True, hidden=False):
        self.profiles = UUID + ":repeater-ap"
        self.active = active
        self.hidden = hidden
        self.fields = ["802-11-wireless", "wlan0", "ap", "wpa-psk", "rsn"]
        self.commands = []
        self.activation_failures = 0
        self.modification_failures = 0
        self.read_failure_after = None
        self.profile_reads = 0
        self.stop_before_apply = False
        self.active_reads = 0

    def __call__(self, *args, wait=5):
        self.commands.append((args, wait))
        if args == ("--terse", "--escape", "yes", "--fields", "UUID,NAME", "connection", "show"):
            return self.profiles
        if args[:3] == ("--escape", "no", "--get-values"):
            self.profile_reads += 1
            if self.read_failure_after is not None and self.profile_reads > self.read_failure_after:
                raise worker.ApplyError("NetworkManager timed out.")
            assert args[3] == ",".join(worker.PROFILE_FIELDS)
            assert args[4:] == ("connection", "show", "uuid", UUID)
            return "\n".join(self.fields + ["yes" if self.hidden else "no"])
        if args == ("--terse", "--escape", "yes", "--fields", "UUID,DEVICE", "connection", "show", "--active"):
            self.active_reads += 1
            if self.stop_before_apply and self.active_reads == 2:
                self.active = False
            return (UUID + ":wlan0") if self.active else (OTHER_UUID + ":wlan0")
        if args[:5] == ("connection", "modify", "uuid", UUID, "802-11-wireless.hidden"):
            assert args[5] in ("yes", "no")
            self.hidden = args[5] == "yes"
            if self.modification_failures:
                self.modification_failures -= 1
                raise worker.ApplyError("NetworkManager timed out.")
            return ""
        if args == ("connection", "up", "uuid", UUID, "ifname", "wlan0"):
            assert wait == 25
            if self.activation_failures:
                self.activation_failures -= 1
                self.active = False
                raise worker.ApplyError("NetworkManager could not complete the operation.")
            self.active = True
            return ""
        raise AssertionError("Unexpected NetworkManager command: " + repr(args))

    def mutations(self):
        return [args for args, _ in self.commands if args[:2] in (("connection", "modify"), ("connection", "up"))]


class RequestValidationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "request.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_exact_request_schema_and_types(self):
        self.path.write_text(json.dumps(REQUEST))
        self.assertEqual(worker.read_request(self.path), REQUEST)
        for value in ({"request_id": "f" * 31, "hidden": True},
                      {"request_id": "g" * 32, "hidden": True},
                      {"request_id": "a" * 32, "hidden": "false"},
                      {"request_id": "a" * 32, "hidden": 1},
                      {"request_id": "a" * 32, "hidden": True, "profile": "home"},
                      {"request_id": "a" * 32}, [], None):
            with self.subTest(value=value):
                self.path.write_text(json.dumps(value))
                with self.assertRaises(ValueError):
                    worker.read_request(self.path)
        self.path.write_text('{"request_id":"' + "a" * 32 + '","hidden":true,"hidden":false}')
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            worker.read_request(self.path)

    def test_symlink_fifo_large_file_and_invalid_utf8_are_rejected(self):
        target = self.path.with_name("target.json")
        target.write_text(json.dumps(REQUEST))
        self.path.symlink_to(target)
        with self.assertRaises(OSError):
            worker.read_request(self.path)
        self.path.unlink()
        os.mkfifo(self.path)
        with self.assertRaises(ValueError):
            worker.read_request(self.path)
        self.path.unlink()
        for data in (b"x" * 2049, b"\xff"):
            self.path.write_bytes(data)
            with self.assertRaises(ValueError):
                worker.read_request(self.path)

    def test_result_is_atomic_root_group_owned_and_does_not_follow_symlink(self):
        target = self.path.with_name("do-not-touch")
        target.write_text("original")
        self.path.symlink_to(target)
        result = worker.apply_request(REQUEST, FakeNetworkManager(active=False))
        fake_group = mock.Mock(gr_gid=123)
        with mock.patch.object(worker.grp, "getgrnam", return_value=fake_group), \
                mock.patch.object(worker.os, "fchown") as chown:
            worker.write_result(result, self.path)
        self.assertEqual(chown.call_args.args[1:], (0, 123))
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o640)
        self.assertFalse(self.path.is_symlink())
        self.assertEqual(json.loads(self.path.read_text()), result)
        self.assertEqual(target.read_text(), "original")
        self.assertEqual(list(self.path.parent.glob(".wifi-result-*")), [])


class ApplyVisibilityTest(unittest.TestCase):
    def test_active_ap_changes_only_hidden_then_reactivates_and_verifies(self):
        fake = FakeNetworkManager()
        result = worker.apply_request(REQUEST, fake)
        self.assertTrue(result["success"])
        self.assertIs(result["profile_hidden"], True)
        self.assertEqual(fake.mutations(), [
            ("connection", "modify", "uuid", UUID, "802-11-wireless.hidden", "yes"),
            ("connection", "up", "uuid", UUID, "ifname", "wlan0"),
        ])
        self.assertEqual(result["request_id"], REQUEST["request_id"])
        self.assertEqual(set(result), {"request_id", "requested_hidden", "success", "profile_hidden", "message", "completed_at"})
        self.assertIn("+00:00", result["completed_at"])

    def test_inactive_ap_does_not_displace_home_wifi(self):
        fake = FakeNetworkManager(active=False)
        result = worker.apply_request(REQUEST, fake)
        self.assertTrue(result["success"])
        self.assertFalse(fake.active)
        self.assertEqual(len(fake.mutations()), 1)
        self.assertIn("inactive", result["message"])

    def test_ap_stopped_by_someone_else_is_not_reactivated(self):
        fake = FakeNetworkManager()
        fake.stop_before_apply = True
        result = worker.apply_request(REQUEST, fake)
        self.assertTrue(result["success"])
        self.assertFalse(fake.active)
        self.assertEqual(len(fake.mutations()), 1)

    def test_matching_setting_needs_no_mutation_or_reconnection(self):
        fake = FakeNetworkManager(hidden=True)
        self.assertTrue(worker.apply_request(REQUEST, fake)["success"])
        self.assertEqual(fake.mutations(), [])
        fake = FakeNetworkManager(hidden=True)
        result = worker.apply_request(dict(REQUEST, hidden=False), fake)
        self.assertTrue(result["success"])
        self.assertIs(result["profile_hidden"], False)

    def test_missing_or_duplicate_profile_and_malformed_uuid_fail_closed(self):
        for profiles in (OTHER_UUID + ":home", UUID + ":repeater-ap\n" + OTHER_UUID + ":repeater-ap", "--options:repeater-ap"):
            with self.subTest(profiles=profiles):
                fake = FakeNetworkManager()
                fake.profiles = profiles
                result = worker.apply_request(REQUEST, fake)
                self.assertFalse(result["success"])
                self.assertIsNone(result["profile_hidden"])
                self.assertEqual(fake.mutations(), [])

    def test_wrong_mode_interface_or_security_never_changes_profile(self):
        for index, value in ((0, "802-3-ethernet"), (1, "wlan1"), (2, "infrastructure"),
                             (3, "none"), (3, "owe"), (4, ""), (4, "wpa,rsn")):
            with self.subTest(index=index, value=value):
                fake = FakeNetworkManager()
                fake.fields[index] = value
                result = worker.apply_request(REQUEST, fake)
                self.assertFalse(result["success"])
                self.assertEqual(fake.mutations(), [])

    def test_failed_reactivation_restores_previous_setting_and_reconnects(self):
        fake = FakeNetworkManager()
        fake.activation_failures = 1
        result = worker.apply_request(REQUEST, fake)
        self.assertFalse(result["success"])
        self.assertIs(result["profile_hidden"], False)
        self.assertTrue(fake.active)
        self.assertIn("restored", result["message"])
        self.assertEqual(len(fake.mutations()), 4)

    def test_failed_inactive_modification_rolls_back_without_activation(self):
        fake = FakeNetworkManager(active=False)
        fake.modification_failures = 1
        result = worker.apply_request(REQUEST, fake)
        self.assertFalse(result["success"])
        self.assertIs(result["profile_hidden"], False)
        self.assertFalse(fake.active)
        self.assertEqual(len(fake.mutations()), 2)

    def test_failed_recovery_is_reported_honestly(self):
        fake = FakeNetworkManager()
        fake.activation_failures = 2
        result = worker.apply_request(REQUEST, fake)
        self.assertFalse(result["success"])
        self.assertIn("could not be confirmed", result["message"])
        self.assertIn("Ethernet", result["message"])
        self.assertIs(result["profile_hidden"], False)

    def test_unreadable_final_state_is_unknown_not_claimed_applied(self):
        fake = FakeNetworkManager()
        fake.read_failure_after = 1
        result = worker.apply_request(REQUEST, fake)
        self.assertFalse(result["success"])
        self.assertIsNone(result["profile_hidden"])


class InvocationTest(unittest.TestCase):
    def test_nmcli_uses_fixed_binary_no_shell_safe_environment_and_timeout(self):
        with mock.patch.object(worker.subprocess, "run", return_value=mock.Mock(returncode=0, stdout="yes\n")) as run:
            self.assertEqual(worker.nmcli("connection", "show"), "yes")
        args, kwargs = run.call_args
        self.assertEqual(args[0][:5], ["/usr/bin/nmcli", "--wait", "5", "--colors", "no"])
        self.assertNotIn("shell", kwargs)
        self.assertEqual(kwargs["timeout"], 10)
        self.assertEqual(kwargs["env"]["LC_ALL"], "C")
        with mock.patch.object(worker.subprocess, "run", side_effect=subprocess.TimeoutExpired("nmcli", 10)):
            with self.assertRaisesRegex(worker.ApplyError, "timed out"):
                worker.nmcli("connection", "show")
        with mock.patch.object(worker.subprocess, "run", return_value=mock.Mock(returncode=1, stdout="private metadata", stderr="secret")):
            with self.assertRaisesRegex(worker.ApplyError, "could not complete") as failure:
                worker.nmcli("connection", "show")
            self.assertNotIn("secret", str(failure.exception))


if __name__ == "__main__":
    unittest.main()
