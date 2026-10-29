"""Narrow Wi-Fi request/status behavior without changing a real network."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "dashboard"))
from controls import write_json
from wifi_visibility import WifiVisibility


class WifiVisibilityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.commands = []
        self.hidden = "no"
        self.worker = "active"

    def runner(self, args):
        self.commands.append(args)
        if "802-11-wireless.hidden" in args:
            return self.hidden
        if "802-11-wireless.ssid" in args:
            return "Repeater-Box"
        if "repeater-wifi-visibility.path" in args:
            return self.worker
        self.fail(f"Unexpected command: {args}")

    def controller(self, supported=True):
        return WifiVisibility(self.root, supported=supported, runner=self.runner)

    def test_mac_preview_does_not_run_network_commands_or_save_a_request(self):
        control = self.controller(False)
        self.assertTrue(control.status()["preview"])
        with self.assertRaises(ValueError):
            control.request(True)
        self.assertEqual(self.commands, [])
        self.assertFalse((self.root / control.REQUEST).exists())

    def test_request_is_strict_atomic_and_acknowledges_before_more_commands(self):
        control = self.controller()
        state = control.request(True)
        self.assertTrue(state["pending"])
        self.assertFalse(state["profile_hidden"])
        saved = json.loads((self.root / control.REQUEST).read_text())
        self.assertEqual(set(saved), {"request_id", "hidden"})
        self.assertIs(saved["hidden"], True)
        self.assertEqual(len(saved["request_id"]), 32)
        self.assertEqual(len(self.commands), 3)
        with self.assertRaises(ValueError):
            control.request(False)
        with self.assertRaises(ValueError):
            control.request("yes")
        self.assertEqual(json.loads((self.root / control.REQUEST).read_text()), saved)

    def test_missing_profile_or_worker_cannot_queue_changes(self):
        control = self.controller()
        self.hidden = None
        with self.assertRaises(ValueError):
            control.request(True)
        self.hidden = "no"
        self.worker = "inactive"
        with self.assertRaises(ValueError):
            control.request(True)
        self.assertFalse((self.root / control.REQUEST).exists())

    def test_matching_worker_result_and_real_profile_are_reported(self):
        control = self.controller()
        state = control.request(True)
        self.hidden = "yes"
        write_json(self.root / control.RESULT, {"request_id": state["request_id"], "success": True,
                                               "profile_hidden": True, "message": "SSID hidden"})
        state = control.status()
        self.assertFalse(state["pending"])
        self.assertTrue(state["success"])
        self.assertTrue(state["profile_hidden"])
        self.assertEqual(state["message"], "SSID hidden")
        # Failed application must not be reported as success merely because the
        # saved profile flag matches the requested value.
        write_json(self.root / control.RESULT, {"request_id": state["request_id"], "success": False,
                                               "profile_hidden": True, "message": "Activation failed"})
        self.assertFalse(control.status()["success"])

    def test_stale_or_malformed_results_do_not_claim_success(self):
        control = self.controller()
        state = control.request(True)
        write_json(self.root / control.RESULT, {"request_id": "0" * 32, "success": True})
        self.assertTrue(control.status()["pending"])
        path = self.root / control.REQUEST
        old = time.time() - 181
        os.utime(path, (old, old))
        self.assertFalse(control.status()["pending"])
        self.assertFalse(control.status()["success"])
        path.write_text("[]")
        self.assertIsNone(control.status()["request_id"])


if __name__ == "__main__":
    unittest.main()
