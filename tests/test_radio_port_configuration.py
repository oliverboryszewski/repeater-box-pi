"""Local programming-port binding cannot replace a paused radio's device."""
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch
import stat
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("radio_port_config", ROOT / "scripts/configure-radios.py")
config = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(config)
PORT = "/dev/serial/by-path/platform-test-usb-0:1.4:1.0-port0"


class RadioPortTests(unittest.TestCase):
    def initial(self):
        return config.build_config("5RX", "test_rx", "test_tx", validate_device=False)

    def test_adding_programmer_preserves_rx_tx_and_does_not_mutate_input(self):
        old = self.initial()
        new = config.update_programmer(old, PORT, validate_device=False)
        self.assertNotIn("programmer", old["devices"])
        self.assertEqual(new["devices"]["rx"], old["devices"]["rx"])
        self.assertEqual(new["devices"]["tx"], old["devices"]["tx"])
        self.assertEqual(new["devices"]["programmer"], PORT)
        config.check_rebinding(old, new, {"rx": {"verification_required": True}})

    def test_programmer_pending_binding_cannot_change_or_be_removed(self):
        old = config.update_programmer(self.initial(), PORT, validate_device=False)
        for new in (config.update_programmer(old, PORT + "-other", validate_device=False),
                    config.update_programmer(old, remove=True)):
            with self.assertRaises(ValueError):
                config.check_rebinding(old, new, {"programmer": {"verification_required": True}})
        config.check_rebinding(old, old, {"programmer": {"verification_required": True}})

    def test_normal_unbound_programmer_can_be_removed(self):
        old = config.update_programmer(self.initial(), PORT, validate_device=False)
        new = config.update_programmer(old, remove=True)
        self.assertEqual(new, self.initial())

    def test_pending_pair_protects_both_bindings_even_if_one_role_verified(self):
        old = self.initial()
        new = config.build_config("5RX", "other_rx", "test_tx", validate_device=False)
        with self.assertRaisesRegex(ValueError, "paired"):
            config.check_rebinding(old, new, {"_pair": {"verification_required": True}, "rx": {"verified": True}})

    def test_rebinding_disables_qualification_before_invalidating_saved_pair(self):
        old = self.initial()
        new = config.build_config("5RX", "other_rx", "test_tx", validate_device=False)
        state = {"_pair": {"stored_verified": True}}
        public = {"_pair": {"stored_verified": True}}
        with patch.object(config, "write_local") as write, patch.object(config, "read_local", return_value=public):
            config.reset_pair_on_rebinding(old, new, state)
            self.assertEqual(write.call_args_list[0].args, (config.PAIR_CONFIG, {"schema": 1, "enabled": False}))
            self.assertFalse(state["_pair"]["stored_verified"])
            self.assertFalse(public["_pair"]["stored_verified"])
            self.assertEqual(write.call_count, 3)
        with patch.object(config, "write_local") as write:
            config.reset_pair_on_rebinding(old, config.update_programmer(old, PORT, validate_device=False), state)
            write.assert_not_called()

    def test_paths_are_limited_to_stable_serial_aliases(self):
        for port in ("/dev/ttyUSB0", "/tmp/port", "/dev/serial/by-id/../secret", PORT + "/other", "x;reboot"):
            with self.subTest(port=port), self.assertRaises(ValueError):
                config.update_programmer(self.initial(), port, validate_device=False)
        self.assertEqual(config.update_programmer(self.initial(),
            "/dev/serial/by-id/usb-test_USB_Serial-if00-port0", validate_device=False)["model"], "5RX")

    def test_same_device_through_another_alias_is_rejected(self):
        with patch.object(config.os, "stat", return_value=SimpleNamespace(st_mode=stat.S_IFCHR)), \
                patch.object(config.os.path, "samefile", return_value=True):
            with self.assertRaisesRegex(ValueError, "separate"):
                config.update_programmer(self.initial(), PORT)

    def test_binding_requires_present_serial_character_device(self):
        with patch.object(config.os, "stat", side_effect=FileNotFoundError):
            with self.assertRaisesRegex(ValueError, "Plug"):
                config.update_programmer(self.initial(), PORT)
        with patch.object(config.os, "stat", return_value=SimpleNamespace(st_mode=stat.S_IFREG)):
            with self.assertRaisesRegex(ValueError, "character"):
                config.update_programmer(self.initial(), PORT)
