"""A repair is bound to one saved pending upload, never a browser-supplied image."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "dashboard"))
from controls import write_json
from radio_programming import RadioProgramming, public_role, valid_request

SPEC = importlib.util.spec_from_file_location("repair_schema_worker", ROOT / "scripts/program-radio.py")
worker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(worker)
PENDING = "a" * 32
CONFIG = {"schema": 1, "model": "5RX", "devices": {
    "rx": "/dev/serial/by-id/usb-AIOC_All-In-One-Cable_test_rx-if04",
    "tx": "/dev/serial/by-id/usb-AIOC_All-In-One-Cable_test_tx-if04",
    "programmer": "/dev/serial/by-id/usb-test-programmer-if00-port0"}}


class SavedRepairControllerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self.config = self.state / "config.json"
        write_json(self.config, CONFIG)
        self.active = False
        self.controller = RadioProgramming(self.state, supported=True, config_file=self.config,
            runner=lambda args: "active" if args[-1].endswith(".path") or self.active else "inactive")

    def pending(self, role="rx", **overrides):
        value = {"inspected": True, "compatible": True, "programmed": True,
                 "verified": False, "verification_required": True,
                 "pending_id": PENDING, "program_kind": "repeater"}
        value.update(overrides)
        write_json(self.state / self.controller.PUBLIC_STATE, {role: value})

    def test_schema_pins_the_saved_candidate_for_each_connection(self):
        for role in CONFIG["devices"]:
            request = {"request_id": "b" * 32, "role": role,
                       "action": "repair-upload", "pending_id": PENDING}
            self.assertTrue(valid_request(request))
            self.assertTrue(worker.valid_request(request))
            for invalid in ({key: value for key, value in request.items() if key != "pending_id"},
                            {**request, "path": "/tmp/image"},
                            {**request, "pending_id": "../candidate"},
                            {**request, "pending_id": None},
                            {**request, "action": "program"}):
                self.assertFalse(valid_request(invalid))
                self.assertFalse(worker.valid_request(invalid))

    def test_queues_only_the_confirmed_pending_identifier(self):
        for role in CONFIG["devices"]:
            with self.subTest(role=role):
                (self.state / self.controller.REQUEST).unlink(missing_ok=True)
                self.pending(role, program_kind="editor" if role == "programmer" else "repeater")
                result = self.controller.request(role, "repair-upload", PENDING)
                request = json.loads((self.state / self.controller.REQUEST).read_text())
                self.assertEqual(set(request), {"request_id", "role", "action", "pending_id"})
                self.assertEqual(request["pending_id"], PENDING)
                self.assertEqual(request["role"], role)
                self.assertTrue(result["pending"])
                self.assertNotIn("/dev/", json.dumps(result))

    def test_refuses_stale_missing_or_unrepairable_pending_state(self):
        cases = ({}, {"verification_required": False}, {"pending_id": "c" * 32},
                 {"pending_id": None}, {"compatible": False}, {"program_kind": "pair"},
                 {"program_kind": None})
        for overrides in cases:
            with self.subTest(overrides=overrides):
                if overrides:
                    self.pending(**overrides)
                else:
                    write_json(self.state / self.controller.PUBLIC_STATE, {})
                with self.assertRaises(ValueError):
                    self.controller.request("rx", "repair-upload", PENDING)
                self.assertFalse((self.state / self.controller.REQUEST).exists())

    def test_refuses_active_worker_and_unfinished_pair(self):
        self.pending()
        self.active = True
        with self.assertRaises(ValueError):
            self.controller.request("rx", "repair-upload", PENDING)
        self.active = False
        saved = json.loads((self.state / self.controller.PUBLIC_STATE).read_text())
        saved["_pair"] = {"pair_id": "default", "request_id": "d" * 32,
                          "verification_required": True, "phase": "failed"}
        write_json(self.state / self.controller.PUBLIC_STATE, saved)
        with self.assertRaises(ValueError):
            self.controller.request("rx", "repair-upload", PENDING)
        self.assertFalse((self.state / self.controller.REQUEST).exists())

    def test_public_pending_token_is_bounded_and_cleared_after_verification(self):
        self.assertEqual(public_role({"verification_required": True, "pending_id": PENDING})["pending_id"], PENDING)
        for value in ({"verification_required": False, "pending_id": PENDING},
                      {"verification_required": True, "pending_id": "/private/path"}):
            self.assertIsNone(public_role(value)["pending_id"])
        self.pending()
        for role, action, token in (("rx", "repair-upload", None),
                                    ("rx", "program", PENDING),
                                    ("programmer", "program", None)):
            with self.assertRaises(ValueError):
                self.controller.request(role, action, token)

    def test_installer_reprojects_legacy_pending_state_without_hardware(self):
        private_dir = self.state / "private"
        private_dir.mkdir()
        original = {"rx": {"program_id": PENDING, "backup_id": "b" * 32,
                            "verification_required": True, "program_kind": "repeater",
                            "device": "/dev/PRIVATE"}}
        write_json(private_dir / "state.json", original)
        installed = (ROOT / "scripts/install-pi.sh").read_text()
        snippet = installed.split("<<'RADIO_STATE_PY'\n", 1)[1].split("\nRADIO_STATE_PY", 1)[0]
        refreshed = worker.Worker(None, state_dir=self.state, backup_dir=private_dir,
                                  owner_uid=os.getuid())
        factory = mock.Mock(return_value=refreshed)
        loaded = types.SimpleNamespace(Worker=factory)
        spec = types.SimpleNamespace(loader=mock.Mock())
        with mock.patch("importlib.util.spec_from_file_location", return_value=spec), \
                mock.patch("importlib.util.module_from_spec", return_value=loaded), \
                mock.patch("grp.getgrnam", return_value=types.SimpleNamespace(gr_gid=None)), \
                mock.patch.object(worker, "Hardware", side_effect=AssertionError("Migration must not open hardware")):
            exec(snippet, {})
        factory.assert_called_once_with(None, group_id=None)
        self.assertEqual(json.loads((private_dir / "state.json").read_text()), original)
        public = json.loads((self.state / self.controller.PUBLIC_STATE).read_text())
        self.assertEqual(public["rx"]["pending_id"], PENDING)
        self.assertTrue(public["rx"]["verification_required"])
        self.assertNotIn("PRIVATE", json.dumps(public))


if __name__ == "__main__":
    unittest.main()
