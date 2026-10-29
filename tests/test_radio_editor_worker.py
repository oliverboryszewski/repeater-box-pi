"""Offline pinned-driver and transaction tests; no radio is opened."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
from radio_test_catalog import CATALOG, DIGEST, PLAN, PAIR_PLANS
spec = importlib.util.spec_from_file_location("editor_worker", ROOT / "scripts" / "program-radio.py")
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)
SOURCE = Path(os.environ.get("CHIRP_TEST_SOURCE", "/opt/repeater-box/vendor/chirp"))
FIXTURE = SOURCE / "tests" / "images" / "Baofeng_5RX.img"
if FIXTURE.exists():
    sys.path.insert(0, str(SOURCE))
    from chirp import chirp_common, settings, memmap
    from chirp.drivers.baofeng_wp970i import BF5RXRadio
    from chirp.drivers import baofeng_common

CONFIG = {"schema": 1, "model": "5RX", "devices": {
    "rx": "/dev/serial/by-id/usb-AIOC_All-In-One-Cable_test_rx-if04",
    "tx": "/dev/serial/by-id/usb-AIOC_All-In-One-Cable_test_tx-if04"}}


class RequestTests(unittest.TestCase):
    def test_editor_requests_cannot_choose_files_drivers_or_commands(self):
        request = {"request_id": "a" * 32, "role": "rx", "action": "editor-read"}
        self.assertTrue(worker.valid_request(request))
        proposal = {**request, "action": "editor-write", "base_id": "b" * 32,
                    "changes": [], "settings": {"settings.beep": True}}
        self.assertTrue(worker.valid_request(proposal))
        for key in ("path", "image", "driver", "command"):
            self.assertFalse(worker.valid_request({**proposal, key: "anything"}))
        self.assertFalse(worker.valid_request({**proposal, "base_id": "../original"}))
        self.assertFalse(worker.valid_request({**proposal, "changes": [{}] * 129}))

    def test_programmer_is_optional_local_binding_with_narrow_operations(self):
        for device in ("/dev/serial/by-id/usb-1a86_USB_Serial-if00-port0",
                       "/dev/serial/by-path/platform-test-usb-0:1.4:1.0-port0"):
            config = {**CONFIG, "devices": {**CONFIG["devices"], "programmer": device}}
            self.assertTrue(worker.valid_config(config))
        for device in ("/dev/ttyUSB0", "/dev/serial/by-id/../ttyUSB0", "/tmp/radio", "/dev/serial/by-id/a/b"):
            self.assertFalse(worker.valid_config({**CONFIG, "devices": {**CONFIG["devices"], "programmer": device}}))
        for action in ("editor-read", "verify"):
            self.assertTrue(worker.valid_request({"request_id": "a" * 32, "role": "programmer", "action": action, **({"catalog_digest": DIGEST} if action == "program" else {})}))
        for action in ("inspect", "program"):
            self.assertFalse(worker.valid_request({"request_id": "a" * 32, "role": "programmer", "action": action, **({"catalog_digest": DIGEST} if action == "program" else {})}))


@unittest.skipUnless(FIXTURE.exists(), "Set CHIRP_TEST_SOURCE to the pinned official CHIRP source")
class EditorDriverTests(unittest.TestCase):
    def radio(self, raw=None):
        if raw is None:
            return BF5RXRadio(str(FIXTURE))
        radio = BF5RXRadio(None)
        radio._mmap = memmap.MemoryMapBytes(raw)
        radio.process_mmap()
        return radio

    def row(self, number=1, **updates):
        row = {"number": number, "empty": False, "name": "EDIT-RX", "rx_hz": 145300000,
               "tx_hz": None, "mode": "NFM", "power": "High", "skip": False,
               "tx_tone": None, "rx_tone": {"type": "CTCSS", "value": 88.5}}
        row.update(updates)
        return row

    def apply(self, radio, rows=None, changed_settings=None):
        return worker.apply_editor(radio, rows or [], changed_settings or {}, chirp_common, settings)

    def test_snapshot_contains_all_memories_and_only_operating_settings(self):
        snapshot = worker.editor_snapshot(self.radio(), "rx", "a" * 32, chirp_common)
        self.assertEqual([row["number"] for row in snapshot["memories"]], list(range(128)))
        self.assertEqual(set(snapshot["settings"]), set(worker.EDITOR_SETTINGS))
        self.assertEqual(set(snapshot["capabilities"]["settings"]), set(worker.EDITOR_SETTINGS))
        self.assertIn("settings.autolk", snapshot["settings"])
        self.assertNotIn("device", snapshot)
        self.assertNotIn("serial", json.dumps(snapshot))
        self.assertNotIn("calibration", json.dumps(snapshot))

    def test_existing_memory_preserves_extras_reserved_bits_and_other_channels(self):
        radio = self.radio()
        raw = bytearray(worker.packed(radio))
        # Exercise bits CHIRP otherwise discards on a normal set_memory call.
        raw[0x1C], raw[0x1D] = 0xB5, 0xA5
        raw[0x1E] = (raw[0x1E] & 3) | 0xAC
        raw[0x1F] = (raw[0x1F] & 0x44) | 0xAB
        raw[0x1017:0x1020] = b"reserved!"
        radio = self.radio(bytes(raw))
        original = worker.packed(radio)
        self.apply(radio, [self.row()])
        edited = worker.packed(radio)
        self.assertEqual(edited[0x1C:0x1E], original[0x1C:0x1E])
        self.assertEqual(edited[0x1E] & 0xFC, original[0x1E] & 0xFC)
        self.assertEqual(edited[0x1F] & ~0x44, original[0x1F] & ~0x44)
        self.assertEqual(edited[0x1017:0x1020], original[0x1017:0x1020])
        changed = {i for i, pair in enumerate(zip(original, edited)) if pair[0] != pair[1]}
        self.assertLessEqual(changed, set(range(0x10, 0x20)) | set(range(0x1010, 0x1017)))
        self.assertEqual(edited[0x1E80:], original[0x1E80:])

    def test_tone_modes_round_trip_including_dcs_polarity_and_split(self):
        tones = [None, {"type": "CTCSS", "value": 88.5},
                 {"type": "DCS", "value": 23, "polarity": "N"},
                 {"type": "DCS", "value": 25, "polarity": "R"}]
        for tx in tones:
            for rx in tones:
                with self.subTest(tx=tx, rx=rx):
                    radio = self.radio()
                    row = self.row(tx_hz=433500000, tx_tone=tx, rx_tone=rx)
                    self.apply(radio, [row])
                    self.assertEqual(worker.editor_memory(radio, 1, chirp_common), row)

    def test_name_only_edit_preserves_unchanged_field_encodings(self):
        raw = bytearray(worker.packed(self.radio()))
        raw[0x14:0x18] = b"\x00" * 4  # Alternate firmware TX-inhibit encoding.
        raw[0x18:0x1C] = b"\xFF" * 4  # Alternate no-tone encoding.
        radio = self.radio(bytes(raw))
        row = worker.editor_memory(radio, 1, chirp_common)
        row["name"] = "RENAME"
        self.apply(radio, [row])
        self.assertEqual(worker.packed(radio)[:0x800], bytes(raw[:0x800]))

    def test_admin_rx_then_test_rx_name_edit_replaces_all_seven_characters(self):
        radio = self.radio()
        worker.apply_plan(radio, "rx", chirp_common, settings, catalog=CATALOG)
        self.assertEqual(radio.get_memory(1).name, "BOX-RX1")
        before = worker.packed(radio)
        row = worker.editor_memory(radio, 1, chirp_common)
        row["name"] = "TEST-RX"

        blocks = self.apply(radio, [row])

        after = worker.packed(radio)
        self.assertEqual(radio.get_memory(1).name, "TEST-RX")
        self.assertEqual(worker.editor_memory(radio, 1, chirp_common), row)
        self.assertEqual(after[0x1010:0x1017], b"TEST-RX")
        self.assertEqual(after[:0x1010], before[:0x1010])
        self.assertEqual(after[0x1017:], before[0x1017:])
        changed = {index for index, (old, new) in enumerate(zip(before, after)) if old != new}
        self.assertEqual(changed, set(range(0x1010, 0x1017)))
        self.assertEqual(blocks, [0x1010])

    def test_successive_seven_and_six_character_names_clear_the_old_tail(self):
        radio = self.radio()
        worker.apply_plan(radio, "rx", chirp_common, settings, catalog=CATALOG)
        for name, encoded in (("TEST-RX", b"TEST-RX"), ("BOX-RX", b"BOX-RX\xff"),
                              ("TEST-RX", b"TEST-RX")):
            with self.subTest(name=name):
                before = worker.packed(radio)
                row = worker.editor_memory(radio, 1, chirp_common)
                row["name"] = name
                self.assertEqual(self.apply(radio, [row]), [0x1010])
                after = worker.packed(radio)
                self.assertEqual(radio.get_memory(1).name, name)
                self.assertEqual(after[0x1010:0x1017], encoded)
                self.assertEqual(after[:0x1010], before[:0x1010])
                self.assertEqual(after[0x1017:], before[0x1017:])

    def test_name_only_upload_frame_contains_complete_test_rx_name(self):
        original = self.radio()
        worker.apply_plan(original, "rx", chirp_common, settings, catalog=CATALOG)
        self.assertEqual(original.get_memory(1).name, "BOX-RX1")
        raw = worker.packed(original)
        candidate = self.radio(raw)
        row = worker.editor_memory(candidate, 1, chirp_common)
        row["name"] = "TEST-RX"
        blocks = self.apply(candidate, [row])
        frames = []
        class Port:
            def __init__(self):
                self.is_open, self.dtr = True, True
            def write(self, frame):
                frames.append(frame)
                return len(frame)
            def close(self):
                self.is_open = False
        old_port, upload_port = Port(), Port()
        candidate.pipe = old_port
        hardware = worker.Hardware.__new__(worker.Hardware)
        hardware.catalog = CATALOG
        hardware.driver = BF5RXRadio
        hardware.open = mock.Mock(return_value=upload_port)
        hardware.common = types.SimpleNamespace(
            _ident_radio=lambda _: raw[0x2000:0x2008],
            _get_aux_data_from_radio=lambda _: (raw[0x1EF0:0x1EFE], raw[0x1F00:0x1F60],
                raw[0x1F70:0x1F80], raw[0x1F90:0x1FC0], True),
            _make_frame=baofeng_common._make_frame,
            _rawrecv=lambda _, size: b"\x06")
        before_write = mock.Mock()
        with mock.patch.object(worker.time, "sleep", lambda seconds: None):
            hardware.upload("device", original, candidate, blocks, lambda: None,
                            before_write, editor=True)
        before_write.assert_called_once_with()
        hardware.open.assert_called_once_with("device")
        self.assertFalse(old_port.is_open)
        self.assertFalse(old_port.dtr)
        self.assertEqual(len(frames), 16)
        self.assertEqual(struct.unpack(">BHB", frames[0][:4]), (ord("X"), 0x1000, 16))
        self.assertEqual(frames[0][4:], raw[0x1000:0x1010])
        command, address, size = struct.unpack(">BHB", frames[1][:4])
        self.assertEqual((command, address, size), (ord("X"), 0x1010, 16))
        self.assertEqual(frames[1][4:11], b"TEST-RX")
        self.assertEqual(frames[1][4:], worker.packed(candidate)[0x1010:0x1020])
        self.assertEqual(frames[1][11:], raw[0x1017:0x1020])
        for frame in frames:
            address = struct.unpack(">BHB", frame[:4])[1]
            self.assertEqual(frame[4:], worker.packed(candidate)[address:address + 16])

    def test_empty_channels_default_no_transmit_and_delete_is_explicit(self):
        radio = self.radio()
        self.apply(radio, [{"number": 127, "empty": True}])
        self.assertTrue(radio.get_memory(127).empty)
        created = self.row(127, name="NEW")
        self.apply(radio, [created])
        self.assertEqual(worker.editor_memory(radio, 127, chirp_common), created)
        self.assertEqual(radio.get_memory(127).duplex, "off")

    def test_bad_channels_and_frequencies_are_rejected_before_upload(self):
        bad = [self.row(number=True), self.row(number=128), self.row(name="TOO-LONG"),
               self.row(rx_hz=145300001), self.row(rx_hz=True), self.row(rx_hz=300000000),
               self.row(tx_hz=462562500), self.row(tx_hz=121500000), self.row(power="Turbo"),
               self.row(skip="yes"), self.row(rx_tone={"type": "CTCSS", "value": 88.51}),
               self.row(rx_tone={"type": "DCS", "value": 999, "polarity": "N"}),
               self.row(rx_tone={"type": "DCS", "value": 23, "polarity": "X"}),
               {**self.row(), "calibration": 1}]
        for row in bad:
            with self.subTest(row=row), self.assertRaises(worker.ProgrammingError):
                self.apply(self.radio(), [row])
        with self.assertRaises(worker.ProgrammingError):
            self.apply(self.radio(), [self.row(), self.row()])

    def test_unchanged_factory_transmit_is_preserved_when_renaming(self):
        radio = self.radio()
        # Direct fixture setup models an existing factory channel outside the
        # project's new-transmit profile. An ordinary name edit preserves it.
        radio._memobj.memory[1].txfreq = 46256250
        row = worker.editor_memory(radio, 1, chirp_common)
        self.assertEqual(row["tx_hz"], 462562500)
        row["name"] = "RENAME"
        self.apply(radio, [row])
        self.assertEqual(worker.editor_memory(radio, 1, chirp_common)["tx_hz"], 462562500)

    def test_operating_globals_are_narrow_and_preserve_neighbor_bits(self):
        radio = self.radio()
        before = worker.packed(radio)
        changes = {"settings.autolk": True, "settings.squelch": "5",
                   "settings.beep": True, "settings.displayab": "B"}
        self.apply(radio, changed_settings=changes)
        after = worker.packed(radio)
        changed = {i for i in range(len(before)) if before[i] != after[i]}
        self.assertLessEqual(changed, {0xE20, 0xE28, 0xE38, 0xE4A})
        self.assertEqual((before[0xE4A] ^ after[0xE4A]) & 0x7F, 0)
        self.assertEqual(before[0xE76:0xE78], after[0xE76:0xE78])
        for proposal in ({"limits.vhf.lower": 100}, {"settings.autolk": "yes"},
                         {"wmchannel.mrcha": 1}, {"settings.timeout": "never"}):
            with self.subTest(proposal=proposal), self.assertRaises(worker.ProgrammingError):
                self.apply(self.radio(), changed_settings=proposal)

    def test_upload_rejects_service_data_even_with_editor_enabled(self):
        original, candidate = self.radio(), self.radio()
        raw = bytearray(worker.packed(candidate)); raw[0x1F80] ^= 1
        candidate = self.radio(bytes(raw)); candidate.pipe = types.SimpleNamespace(is_open=True)
        hardware = worker.Hardware.__new__(worker.Hardware)
        hardware.catalog = CATALOG
        hardware.common = types.SimpleNamespace(_read_block=lambda _, address, size, first:
                                               worker.packed(original)[address:address + size])
        with self.assertRaises(worker.ProgrammingError):
            hardware.upload("device", original, candidate, [0x1F80], lambda: None, lambda: None, editor=True)

    def test_named_verification_diagnostics_do_not_relax_comparison(self):
        original, candidate, observed = self.radio(), self.radio(), self.radio()
        worker.apply_plan(candidate, "rx", chirp_common, settings, catalog=CATALOG)
        raw = bytearray(worker.packed(candidate)); raw[0xE38] = 0
        observed = self.radio(bytes(raw))
        with self.assertRaisesRegex(worker.ProgrammingError, "settings.autolk: expected 'True', read 'False'"):
            worker.verify_images(original, candidate, observed)

    def test_old_pending_candidate_accepts_only_volatile_cursor_low_bits(self):
        original, candidate = self.radio(), self.radio()
        worker.apply_plan(candidate, "rx", chirp_common, settings, catalog=CATALOG)
        saved = bytearray(worker.packed(candidate))
        # Model a candidate saved by v0.4, which used to select memory 1.
        saved[0xE76] = (saved[0xE76] & 0x80) | 1
        saved[0xE77] = (saved[0xE77] & 0x80) | 1
        candidate = self.radio(bytes(saved))
        observed_raw = bytearray(saved)
        observed_raw[0xE76] = (observed_raw[0xE76] & 0x80) | 127
        observed_raw[0xE77] = (observed_raw[0xE77] & 0x80) | 127
        worker.verify_images(original, candidate, self.radio(bytes(observed_raw)))
        observed_raw[0xE4D] ^= 1
        worker.verify_images(original, candidate, self.radio(bytes(observed_raw)))
        for address, mask in ((0xE76, 0x80), (0xE77, 0x80), (0x10, 1),
                              (0x18, 1), (0x1F80, 1), (0xE4A, 1), (0xE4D, 2), (0xE4D, 0x80)):
            changed = bytearray(observed_raw); changed[address] ^= mask
            with self.subTest(address=address), self.assertRaises(worker.ProgrammingError):
                worker.verify_images(original, candidate, self.radio(bytes(changed)))

    def test_new_fixed_plan_preserves_working_cursors_and_uses_high_autolock(self):
        for role in ("rx", "tx"):
            with self.subTest(role=role):
                radio = self.radio()
                before = worker.packed(radio)
                blocks = worker.apply_plan(radio, role, chirp_common, settings, catalog=CATALOG)
                after = worker.packed(radio)
                self.assertEqual(before[0xE76:0xE78], after[0xE76:0xE78])
                self.assertNotIn(0xE70, blocks)
                self.assertEqual(str(radio.get_memory(1).power), "High")
                self.assertTrue(bool(radio._memobj.settings.autolk))
                self.assertNotIn("wmchannel.mrcha", worker.EDITOR_SETTINGS)
                self.assertNotIn("wmchannel.mrchb", worker.EDITOR_SETTINGS)

    def make_worker(self, state, backups):
        test = self
        class FakeHardware:
            def __init__(self):
                self.current = worker.packed(test.radio())
                self.uploads = 0
                self.fail_readback = False
            def download(self, device, progress):
                progress(current=64, total=128)
                return test.radio(self.current)
            def load(self, path):
                return BF5RXRadio(str(path))
            def editor_snapshot(self, radio, role, base_id):
                return worker.editor_snapshot(radio, role, base_id, chirp_common)
            def recovery_differences(self, expected, observed):
                return worker.recovery_differences(expected, observed, chirp_common)
            def prepare_editor(self, radio, changes, values):
                return worker.apply_editor(radio, changes, values, chirp_common, settings)
            def prepare(self, radio, role):
                return worker.apply_plan(radio, role, chirp_common, settings, catalog=CATALOG)
            def upload(self, device, original, candidate, blocks, progress, before_write, editor=False):
                self.assert_editor = editor
                if blocks:
                    before_write()
                self.current = worker.packed(candidate)
                self.uploads += 1
                self.last_blocks = blocks
                progress(current=len(blocks), total=len(blocks))
            def readback(self, radio, progress):
                if self.fail_readback:
                    raise IOError("private exception content")
                return test.radio(self.current)
            def settle_and_verify(self, device, radio, progress):
                return self.readback(radio, progress)
            def release(self, radio):
                pass
        hardware = FakeHardware()
        active, enabled = {"repeater-engine.service"}, {"repeater-engine.service"}
        def service(action, name):
            if action == "is-active": return name in active
            if action == "is-enabled": return name in enabled
            if action == "stop": active.discard(name)
            if action == "start": active.add(name)
            if action == "disable": enabled.discard(name)
            if action == "enable": enabled.add(name)
            return True
        instance = worker.Worker(hardware, state_dir=state, backup_dir=backups,
                                 service=service, owner_uid=os.getuid())
        instance.catalog = CATALOG
        return instance, hardware, active, enabled

    def request(self, number, action, **extra):
        return {"request_id": f"{number:032x}", "role": "rx", "action": action, **({"catalog_digest": DIGEST} if action == "program" else {}), **extra}

    def test_live_service_read_only_work_pauses_then_restores_only_prior_live_service(self):
        with tempfile.TemporaryDirectory() as temporary:
            state, backups = Path(temporary) / "state", Path(temporary) / "backups"
            state.mkdir(); backups.mkdir()
            instance, hardware, active, enabled = self.make_worker(state, backups)
            active.clear(); active.add("repeater-live.service")
            enabled.clear(); enabled.add("repeater-live.service")
            download = hardware.download

            def read_while_paused(*args):
                self.assertFalse(active)
                return download(*args)

            with mock.patch.object(hardware, "download", side_effect=read_while_paused):
                result = instance.run(self.request(1, "inspect"), CONFIG)
            self.assertTrue(result["success"], result)
            self.assertEqual(active, {"repeater-live.service"})
            self.assertEqual(enabled, active)

    def test_live_service_failed_read_only_release_is_durable_and_recovers_by_exact_saved_read(self):
        with tempfile.TemporaryDirectory() as temporary:
            state, backups = Path(temporary) / "state", Path(temporary) / "backups"
            state.mkdir(); backups.mkdir()
            instance, hardware, active, enabled = self.make_worker(state, backups)
            active.clear(); active.add("repeater-live.service")
            enabled.clear(); enabled.add("repeater-live.service")
            with mock.patch.object(hardware, "release", side_effect=OSError("private port error")):
                result = instance.run(self.request(1, "inspect"), CONFIG)
            self.assertFalse(result["success"], result)
            self.assertTrue(result["verification_required"])
            self.assertNotIn("private port error", result["message"])
            self.assertFalse(active); self.assertFalse(enabled)
            saved = json.loads((backups / "state.json").read_text())["rx"]
            self.assertTrue(saved["verification_required"])
            self.assertFalse(saved["verified"])
            self.assertEqual(saved["release_backup_id"], f"{1:032x}")
            self.assertEqual(saved["resume_services"], ["repeater-live.service"])
            self.assertEqual(saved["resume_enabled"], ["repeater-live.service"])

            recovered = worker.Worker(hardware, state, backups, service=instance.service, owner_uid=os.getuid())
            recovered.catalog = CATALOG
            with mock.patch.object(hardware, "download", wraps=hardware.download) as download:
                self.assertFalse(recovered.run(self.request(2, "inspect"), CONFIG)["success"])
                download.assert_not_called()
            for number in range(3, 9):
                self.assertTrue(recovered.run(self.request(number, "editor-read"), CONFIG)["success"])
            self.assertEqual(recovered.private["rx"]["release_backup_id"], f"{1:032x}")
            self.assertTrue((backups / f"{1:032x}" / "original.img").is_file())
            self.assertLessEqual(len(list(backups.glob("*/manifest.json"))), 5)
            self.assertFalse(active); self.assertFalse(enabled)
            result = recovered.run(self.request(9, "verify"), CONFIG)
            self.assertTrue(result["success"], result)
            self.assertFalse(recovered.private["rx"]["verification_required"])
            self.assertNotIn("release_verification_required", recovered.private["rx"])
            self.assertEqual(active, {"repeater-live.service"})
            self.assertEqual(enabled, active)

    def test_verified_upload_with_failed_release_preserves_candidate_and_keeps_live_disabled(self):
        with tempfile.TemporaryDirectory() as temporary:
            state, backups = Path(temporary) / "state", Path(temporary) / "backups"
            state.mkdir(); backups.mkdir()
            instance, hardware, active, enabled = self.make_worker(state, backups)
            active.clear(); active.add("repeater-live.service")
            enabled.clear(); enabled.add("repeater-live.service")
            self.assertTrue(instance.run(self.request(1, "editor-read"), CONFIG)["success"])
            with mock.patch.object(hardware, "release", side_effect=OSError):
                result = instance.run(self.request(2, "editor-write", base_id=f"{1:032x}",
                                                   changes=[self.row()], settings={}), CONFIG)
            self.assertFalse(result["success"], result)
            self.assertTrue(result["verification_required"])
            self.assertFalse(active); self.assertFalse(enabled)
            saved = json.loads((backups / "state.json").read_text())["rx"]
            self.assertEqual(saved["program_id"], f"{2:032x}")
            self.assertTrue((backups / saved["program_id"] / "candidate.img").is_file())
            recovered = worker.Worker(hardware, state, backups, service=instance.service, owner_uid=os.getuid())
            recovered.catalog = CATALOG
            result = recovered.run(self.request(3, "verify"), CONFIG)
            self.assertTrue(result["success"], result)
            self.assertEqual(active, {"repeater-live.service"})
            self.assertEqual(enabled, active)

    def test_recovery_read_cannot_replace_failed_release_comparison_or_restart_live_on_drift(self):
        with tempfile.TemporaryDirectory() as temporary:
            state, backups = Path(temporary) / "state", Path(temporary) / "backups"
            state.mkdir(); backups.mkdir()
            instance, hardware, active, enabled = self.make_worker(state, backups)
            active.clear(); active.add("repeater-live.service")
            enabled.clear(); enabled.add("repeater-live.service")
            with mock.patch.object(hardware, "release", side_effect=OSError):
                instance.run(self.request(1, "inspect"), CONFIG)
            changed = bytearray(hardware.current); changed[0x1F80] ^= 1
            hardware.current = bytes(changed)
            self.assertTrue(instance.run(self.request(2, "editor-read"), CONFIG)["success"])
            result = instance.run(self.request(3, "verify"), CONFIG)
            self.assertFalse(result["success"], result)
            self.assertTrue(result["verification_required"])
            self.assertFalse(active); self.assertFalse(enabled)
            saved = json.loads((backups / "state.json").read_text())["rx"]
            self.assertTrue(saved["verification_required"])
            self.assertEqual(saved["release_backup_id"], f"{1:032x}")

    def test_read_write_transaction_private_base_verified_export_and_progress(self):
        with tempfile.TemporaryDirectory() as temporary:
            state, backups = Path(temporary) / "state", Path(temporary) / "backups"
            state.mkdir(); backups.mkdir()
            instance, hardware, active, enabled = self.make_worker(state, backups)
            result = instance.run(self.request(1, "editor-read"), CONFIG)
            self.assertTrue(result["success"], result)
            self.assertEqual(hardware.uploads, 0)
            snapshot = json.loads((state / "radio-editor-rx.json").read_text())
            self.assertEqual(snapshot["base_id"], f"{1:032x}")
            result = instance.run(self.request(2, "editor-write", base_id=snapshot["base_id"],
                                              changes=[self.row()], settings={"settings.autolk": True}), CONFIG)
            self.assertTrue(result["success"], result)
            self.assertTrue(hardware.assert_editor)
            self.assertEqual(hardware.uploads, 1)
            self.assertEqual(instance.private["rx"]["program_kind"], "editor")
            self.assertEqual(instance.private["rx"]["editor_base_id"], f"{2:032x}")
            self.assertTrue((backups / f"{2:032x}" / "observed.img").exists())
            self.assertTrue((backups / f"{2:032x}" / "verified.img").exists())
            self.assertEqual(result["progress"]["percent"], 100)
            self.assertGreaterEqual(len(result["events"]), 5)
            self.assertLessEqual(len(result["events"]), 20)
            self.assertIn("repeater-engine.service", active)
            self.assertIn("repeater-engine.service", enabled)

    def test_normal_inspect_program_rename_restore_and_verify_have_no_manual_restart_gate(self):
        # Only the normal narrow requests are supplied. There is no restart
        # acknowledgement, reset callback, candidate adoption or state repair.
        # This tests orchestration, not whether any untested firmware reloads
        # its working frequency; paired-change qualification stays separate.
        with tempfile.TemporaryDirectory() as temporary:
            state, backups = Path(temporary) / "state", Path(temporary) / "backups"
            state.mkdir(); backups.mkdir()
            instance, hardware, active, enabled = self.make_worker(state, backups)
            for number, action in ((1, "inspect"), (2, "program"), (3, "editor-read")):
                request = self.request(number, action)
                self.assertTrue(worker.valid_request(request))
                result = instance.run(request, CONFIG)
                self.assertTrue(result["success"], result)
                self.assertNotIn("Power-cycle", result["message"])
                self.assertFalse(result["verification_required"])
            self.assertEqual(self.radio(hardware.current).get_memory(1).name, "BOX-RX1")
            for number, name in ((4, "TEST123"), (6, "BOX-RX1")):
                snapshot = json.loads((state / "radio-editor-rx.json").read_text())
                row = worker.editor_memory(self.radio(hardware.current), 1, chirp_common)
                row["name"] = name
                request = self.request(number, "editor-write", base_id=snapshot["base_id"],
                                       changes=[row], settings={})
                self.assertTrue(worker.valid_request(request))
                result = instance.run(request, CONFIG)
                self.assertTrue(result["success"], result)
                self.assertFalse(result["verification_required"])
                self.assertEqual(self.radio(hardware.current).get_memory(1).name, name)
                # A fresh read-only verification also needs no extra flag and
                # must retain the candidate before the next ordinary write.
                result = instance.run(self.request(number + 1, "verify"), CONFIG)
                self.assertTrue(result["success"], result)
                self.assertFalse(instance.private["rx"]["verification_required"])
                self.assertEqual(instance.private["rx"]["program_id"], f"{number:032x}")
                self.assertTrue((backups / f"{number:032x}" / "candidate.img").exists())
                self.assertTrue((backups / f"{number:032x}" / "verified.img").exists())
            self.assertEqual(hardware.uploads, 3)
            self.assertEqual(hardware.current[0x1010:0x1017], b"BOX-RX1")
            self.assertIn("repeater-engine.service", active)
            self.assertIn("repeater-engine.service", enabled)

    def test_stale_full_image_or_forged_public_base_never_uploads(self):
        for alter_radio in (False, True):
            with self.subTest(alter_radio=alter_radio), tempfile.TemporaryDirectory() as temporary:
                state, backups = Path(temporary) / "state", Path(temporary) / "backups"
                state.mkdir(); backups.mkdir()
                instance, hardware, _, _ = self.make_worker(state, backups)
                instance.run(self.request(1, "editor-read"), CONFIG)
                if alter_radio:
                    raw = bytearray(hardware.current); raw[0x1F80] ^= 1; hardware.current = bytes(raw)
                    base_id = f"{1:032x}"
                else:
                    public = json.loads((state / "radio-editor-rx.json").read_text())
                    public["base_id"] = "f" * 32
                    (state / "radio-editor-rx.json").write_text(json.dumps(public))
                    base_id = "f" * 32
                result = instance.run(self.request(2, "editor-write", base_id=base_id,
                                                  changes=[self.row()], settings={}), CONFIG)
                self.assertFalse(result["success"], result)
                self.assertEqual(hardware.uploads, 0)

    def test_failed_upload_allows_other_read_but_preserves_pending_until_verify(self):
        with tempfile.TemporaryDirectory() as temporary:
            state, backups = Path(temporary) / "state", Path(temporary) / "backups"
            state.mkdir(); backups.mkdir()
            instance, hardware, active, enabled = self.make_worker(state, backups)
            instance.run(self.request(1, "editor-read"), CONFIG)
            hardware.fail_readback = True
            result = instance.run(self.request(2, "editor-write", base_id=f"{1:032x}",
                                              changes=[self.row()], settings={}), CONFIG)
            self.assertFalse(result["success"])
            self.assertTrue(result["verification_required"])
            self.assertNotIn("private exception content", result["message"])
            self.assertFalse(active); self.assertFalse(enabled)
            pending = copy.deepcopy(instance.private["rx"])
            read_other = instance.run({**self.request(3, "editor-read"), "role": "tx"}, CONFIG)
            self.assertTrue(read_other["success"])
            self.assertEqual(instance.private["rx"], pending)
            self.assertFalse(active); self.assertFalse(enabled)
            result = instance.run(self.request(4, "verify"), CONFIG)
            self.assertTrue(result["success"], result)
            self.assertFalse(instance.private["rx"]["verification_required"])
            self.assertEqual(instance.private["rx"]["editor_base_id"], f"{2:032x}")
            self.assertIn("repeater-engine.service", active)
            self.assertIn("repeater-engine.service", enabled)

    def create_pending(self, instance, hardware, settings_change=None):
        instance.run(self.request(1, "editor-read"), CONFIG)
        hardware.fail_readback = True
        result = instance.run(self.request(2, "editor-write", base_id=f"{1:032x}",
                                          changes=[self.row()], settings=settings_change or {}), CONFIG)
        self.assertTrue(result["verification_required"], result)
        hardware.fail_readback = False
        return f"{2:032x}"

    def test_recovery_read_preserves_candidate_and_shows_autolock_difference(self):
        with tempfile.TemporaryDirectory() as temporary:
            state, backups = Path(temporary) / "state", Path(temporary) / "backups"
            state.mkdir(); backups.mkdir()
            instance, hardware, active, enabled = self.make_worker(state, backups)
            pending_id = self.create_pending(instance, hardware, {"settings.autolk": True})
            old_candidate = (backups / pending_id / "candidate.img").read_bytes()
            raw = bytearray(hardware.current); raw[0xE38] = 0; hardware.current = bytes(raw)
            result = instance.run(self.request(3, "editor-read"), CONFIG)
            self.assertTrue(result["success"], result)
            self.assertEqual(instance.private["rx"]["program_id"], pending_id)
            self.assertTrue(instance.private["rx"]["verification_required"])
            self.assertEqual((backups / pending_id / "candidate.img").read_bytes(), old_candidate)
            recovery = json.loads((state / "radio-editor-rx.json").read_text())["recovery"]
            self.assertTrue(recovery["required"])
            self.assertEqual(recovery["pending_id"], pending_id)
            self.assertFalse(recovery["protected_mismatch"])
            self.assertIn({"field": "settings.autolk", "expected": "True", "actual": "False"}, recovery["differences"])
            self.assertFalse(active); self.assertFalse(enabled)

    def test_confirmed_adopt_current_values_has_no_writes_and_restores_saved_services(self):
        with tempfile.TemporaryDirectory() as temporary:
            state, backups = Path(temporary) / "state", Path(temporary) / "backups"
            state.mkdir(); backups.mkdir()
            instance, hardware, active, enabled = self.make_worker(state, backups)
            pending_id = self.create_pending(instance, hardware, {"settings.autolk": True})
            raw = bytearray(hardware.current); raw[0xE38] = 0; hardware.current = bytes(raw)
            instance.run(self.request(3, "editor-read"), CONFIG)
            before = hardware.current
            result = instance.run(self.request(4, "editor-write", base_id=f"{3:032x}",
                                              supersede_id=pending_id, changes=[], settings={}), CONFIG)
            self.assertTrue(result["success"], result)
            self.assertEqual(hardware.last_blocks, [])
            self.assertEqual(hardware.current, before)
            self.assertFalse(instance.private["rx"]["verification_required"])
            self.assertEqual(instance.private["rx"]["program_id"], f"{4:032x}")
            self.assertTrue((backups / pending_id / "candidate.img").exists())
            self.assertIn("repeater-engine.service", active)
            self.assertIn("repeater-engine.service", enabled)

    def test_recovery_requires_exact_confirmation_and_fresh_base(self):
        for token in (None, "f" * 32):
            with self.subTest(token=token), tempfile.TemporaryDirectory() as temporary:
                state, backups = Path(temporary) / "state", Path(temporary) / "backups"
                state.mkdir(); backups.mkdir()
                instance, hardware, active, enabled = self.make_worker(state, backups)
                pending_id = self.create_pending(instance, hardware)
                instance.run(self.request(3, "editor-read"), CONFIG)
                payload = {"supersede_id": token} if token is not None else {}
                result = instance.run(self.request(4, "editor-write", base_id=f"{3:032x}",
                                                  changes=[self.row(name="REPAIR")], settings={}, **payload), CONFIG)
                self.assertFalse(result["success"], result)
                self.assertEqual(instance.private["rx"]["program_id"], pending_id)
                self.assertTrue(instance.private["rx"]["verification_required"])
                self.assertFalse(active); self.assertFalse(enabled)

    def test_recovery_rejects_calibration_and_reserved_bits_even_with_real_edits(self):
        for address, mask in ((0x1F80, 1), (0xE4A, 1), (0xE76, 0x80), (0x1D, 0x20), (0x1018, 1)):
            with self.subTest(address=address), tempfile.TemporaryDirectory() as temporary:
                state, backups = Path(temporary) / "state", Path(temporary) / "backups"
                state.mkdir(); backups.mkdir()
                instance, hardware, active, enabled = self.make_worker(state, backups)
                pending_id = self.create_pending(instance, hardware)
                raw = bytearray(hardware.current); raw[address] ^= mask; hardware.current = bytes(raw)
                read = instance.run(self.request(3, "editor-read"), CONFIG)
                self.assertTrue(read["success"], read)
                snapshot = json.loads((state / "radio-editor-rx.json").read_text())
                self.assertTrue(snapshot["recovery"]["protected_mismatch"])
                result = instance.run(self.request(4, "editor-write", base_id=f"{3:032x}",
                                                  supersede_id=pending_id, changes=[self.row(name="REPAIR")], settings={}), CONFIG)
                self.assertFalse(result["success"], result)
                self.assertEqual(hardware.uploads, 1)
                self.assertEqual(instance.private["rx"]["program_id"], pending_id)
                self.assertFalse(active); self.assertFalse(enabled)

    def test_editor_stale_check_accepts_only_known_runtime_bits(self):
        for address, mask, accepted in ((0xE4D, 1, True), (0xE76, 0x7F, True),
                                       (0xE4D, 2, False), (0xE76, 0x80, False),
                                       (0xE38, 1, False), (0x10, 1, False)):
            with self.subTest(address=address, mask=mask), tempfile.TemporaryDirectory() as temporary:
                state, backups = Path(temporary) / "state", Path(temporary) / "backups"
                state.mkdir(); backups.mkdir()
                instance, hardware, _, _ = self.make_worker(state, backups)
                instance.run(self.request(1, "editor-read"), CONFIG)
                raw = bytearray(hardware.current); raw[address] ^= mask; hardware.current = bytes(raw)
                result = instance.run(self.request(2, "editor-write", base_id=f"{1:032x}",
                                                  changes=[self.row()], settings={}), CONFIG)
                self.assertEqual(result["success"], accepted, result)

    def test_last_resolved_role_restores_services_saved_by_another_recovery(self):
        with tempfile.TemporaryDirectory() as temporary:
            state, backups = Path(temporary) / "state", Path(temporary) / "backups"
            state.mkdir(); backups.mkdir()
            instance, hardware, active, enabled = self.make_worker(state, backups)
            rx_pending = self.create_pending(instance, hardware)
            tx_pending = f"{10:032x}"
            current = self.radio(hardware.current)
            folder = instance.backup(current, tx_pending, "original", "tx", CONFIG["devices"]["tx"])
            current.save_mmap(str(folder / "candidate.img"))
            instance.private["tx"] = {"device": CONFIG["devices"]["tx"], "program_id": tx_pending,
                                      "verification_required": True, "programmed": True, "program_kind": "editor",
                                      "resume_services": [], "resume_enabled": [], **worker.image_identity(current)}
            instance.publish()
            instance.run(self.request(3, "editor-read"), CONFIG)
            result = instance.run(self.request(4, "editor-write", base_id=f"{3:032x}",
                                              supersede_id=rx_pending, changes=[], settings={}), CONFIG)
            self.assertTrue(result["success"], result)
            self.assertFalse(active); self.assertFalse(enabled)
            self.assertIn("repeater-engine.service", instance.private["rx"]["resume_services"])
            result = instance.run({**self.request(5, "verify"), "role": "tx"}, CONFIG)
            self.assertTrue(result["success"], result)
            self.assertIn("repeater-engine.service", active)
            self.assertIn("repeater-engine.service", enabled)
            self.assertEqual(instance.private["rx"]["resume_services"], [])

    def test_repeated_recovery_reads_and_replacements_stay_within_five_sets(self):
        with tempfile.TemporaryDirectory() as temporary:
            state, backups = Path(temporary) / "state", Path(temporary) / "backups"
            state.mkdir(); backups.mkdir()
            instance, hardware, active, enabled = self.make_worker(state, backups)
            pending_id = self.create_pending(instance, hardware)
            for number in range(3, 15, 2):
                result = instance.run(self.request(number, "editor-read"), CONFIG)
                self.assertTrue(result["success"], result)
                hardware.fail_readback = True
                result = instance.run(self.request(number + 1, "editor-write", base_id=f"{number:032x}",
                                                  supersede_id=pending_id, changes=[self.row(name=f"R{number}")], settings={}), CONFIG)
                self.assertFalse(result["success"], result)
                pending_id = f"{number + 1:032x}"
                hardware.fail_readback = False
                self.assertEqual(instance.private["rx"]["program_id"], pending_id)
                self.assertLessEqual(len([entry for entry in backups.iterdir() if entry.is_dir()]), 5)
                self.assertTrue((backups / instance.private["rx"]["superseded_program_id"] / "candidate.img").exists())
            self.assertFalse(active); self.assertFalse(enabled)

    def test_failed_reverify_marks_real_drift_pending_and_pauses_services(self):
        with tempfile.TemporaryDirectory() as temporary:
            state, backups = Path(temporary) / "state", Path(temporary) / "backups"
            state.mkdir(); backups.mkdir()
            instance, hardware, active, enabled = self.make_worker(state, backups)
            instance.run(self.request(1, "editor-read"), CONFIG)
            result = instance.run(self.request(2, "editor-write", base_id=f"{1:032x}",
                                              changes=[self.row()], settings={"settings.autolk": True}), CONFIG)
            self.assertTrue(result["success"], result)
            raw = bytearray(hardware.current); raw[0xE38] = 0; hardware.current = bytes(raw)
            result = instance.run(self.request(3, "verify"), CONFIG)
            self.assertFalse(result["success"])
            self.assertTrue(result["verification_required"])
            self.assertTrue(instance.private["rx"]["verification_required"])
            self.assertFalse(instance.private["rx"]["verified"])
            self.assertIn("settings.autolk", instance.private["rx"]["verification_message"])
            self.assertFalse(active); self.assertFalse(enabled)

    def test_programmer_works_without_affecting_unfinished_repeater_role(self):
        with tempfile.TemporaryDirectory() as temporary:
            state, backups = Path(temporary) / "state", Path(temporary) / "backups"
            state.mkdir(); backups.mkdir()
            instance, hardware, active, enabled = self.make_worker(state, backups)
            self.create_pending(instance, hardware)
            old_rx = copy.deepcopy(instance.private["rx"])
            config = {**CONFIG, "devices": {**CONFIG["devices"], "programmer": "/dev/serial/by-path/platform-test-usb-0:1.4:1.0-port0"}}
            result = instance.run({**self.request(3, "editor-read"), "role": "programmer"}, config)
            self.assertTrue(result["success"], result)
            result = instance.run({**self.request(4, "editor-write", base_id=f"{3:032x}",
                                                changes=[self.row(name="SPARE")], settings={}), "role": "programmer"}, config)
            self.assertTrue(result["success"], result)
            self.assertEqual(instance.private["rx"], old_rx)
            self.assertFalse(active); self.assertFalse(enabled)

    def test_replacement_is_durable_before_writes_and_keeps_prior_candidate_on_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            state, backups = Path(temporary) / "state", Path(temporary) / "backups"
            state.mkdir(); backups.mkdir()
            instance, hardware, active, enabled = self.make_worker(state, backups)
            pending_id = self.create_pending(instance, hardware)
            instance.run(self.request(3, "editor-read"), CONFIG)
            old_candidate = (backups / pending_id / "candidate.img").read_bytes()
            hardware.fail_readback = True
            result = instance.run(self.request(4, "editor-write", base_id=f"{3:032x}", supersede_id=pending_id,
                                              changes=[self.row(name="REPAIR")], settings={}), CONFIG)
            self.assertFalse(result["success"])
            self.assertEqual(instance.private["rx"]["program_id"], f"{4:032x}")
            self.assertEqual(instance.private["rx"]["superseded_program_id"], pending_id)
            self.assertEqual((backups / pending_id / "candidate.img").read_bytes(), old_candidate)
            self.assertTrue((backups / f"{4:032x}" / "original.img").exists())
            self.assertTrue((backups / f"{4:032x}" / "candidate.img").exists())
            self.assertTrue(instance.private["rx"]["verification_required"])
            self.assertFalse(active); self.assertFalse(enabled)

    def test_editor_read_retention_remains_five_sets_and_preserves_current_base(self):
        with tempfile.TemporaryDirectory() as temporary:
            state, backups = Path(temporary) / "state", Path(temporary) / "backups"
            state.mkdir(); backups.mkdir()
            instance, _, _, _ = self.make_worker(state, backups)
            for number in range(1, 9):
                result = instance.run(self.request(number, "editor-read"), CONFIG)
                self.assertTrue(result["success"], result)
            sets = [folder for folder in backups.iterdir() if folder.is_dir()]
            self.assertEqual(len(sets), 5)
            self.assertTrue((backups / instance.private["rx"]["editor_base_id"]).exists())

    def protocol_hardware(self, initial=None):
        shared = bytearray((initial or worker.packed(self.radio()))[:0x2000])
        ports, events = [], []
        class Port:
            def __init__(self):
                self.number = len(ports)
                self.queue = bytearray(); self.stage = "new"; self.first_read = True
                self.is_open = True; self.written = []; self.dtr = True; self.read_addresses = []
            def read(self, size):
                if not self.is_open: raise AssertionError("Reading a closed serial session")
                data = bytes(self.queue[:size]); del self.queue[:size]; return data
            def write(self, data):
                if not self.is_open: raise AssertionError("Writing a closed serial session")
                if data == BF5RXRadio._magic[0]:
                    if self.stage != "new": raise AssertionError("Second identification in one clone session")
                    self.stage = "ident"; self.queue.extend(b"\x06")
                    events.append(("identify", self.number))
                elif data == b"\x02" and self.stage == "ident":
                    self.queue.extend(worker.KNOWN_IDENT); self.stage = "ident-ack"
                elif data == b"\x06":
                    if self.stage == "ident-ack": self.queue.extend(b"\x06"); self.stage = "clone"
                elif self.stage == "clone" and data[:1] == b"S":
                    _, address, size = struct.unpack(">BHB", data)
                    self.read_addresses.append(address)
                    if not self.first_read: self.queue.extend(b"\x06")
                    self.first_read = False
                    self.queue.extend(struct.pack(">BHB", ord("X"), address, size))
                    self.queue.extend(shared[address:address + size])
                elif self.stage == "clone" and data[:1] == b"X":
                    _, address, size = struct.unpack(">BHB", data[:4])
                    if self.number != 1:
                        raise AssertionError("An upload must use its own newly opened session")
                    if self.read_addresses[:6] != [0x1E80, 0x1EC0, 0x1F00, 0x1F40, 0x1F80, 0x1FC0]:
                        raise AssertionError("Stock identification and auxiliary preflight must precede writes")
                    if not (address < 0x800 or 0x1000 <= address < 0x1800 or address in {0xE20, 0xE30, 0xE40, 0xE50}):
                        raise AssertionError("Protected block write")
                    if len(data[4:]) != size:
                        raise AssertionError("Incomplete payload frame")
                    self.written.append(address); shared[address:address + size] = data[4:]
                    events.append(("write", self.number, address))
                    self.queue.extend(b"\x06")
                else: raise AssertionError("Unexpected clone command")
                return len(data)
            def close(self):
                self.is_open = False
                events.append(("close", self.number))
        hardware = worker.Hardware.__new__(worker.Hardware)
        hardware.catalog = CATALOG
        hardware.driver, hardware.common = BF5RXRadio, baofeng_common
        hardware.chirp_common, hardware.settings, hardware.memmap = chirp_common, settings, memmap
        def open_port(device):
            port = Port()
            ports.append(port)
            events.append(("open", port.number))
            return port
        hardware.open = open_port
        return hardware, ports, events, shared

    def test_full_clone_protocol_editor_never_writes_protected_ranges(self):
        hardware, ports, events, shared = self.protocol_hardware()
        progress = lambda *args, **kwargs: None
        with mock.patch.object(worker.time, "sleep", side_effect=lambda seconds: events.append(("quiet", seconds)) if seconds == 5 else None):
            radio = hardware.download("device", progress)
            original = self.radio(worker.packed(radio))
            blocks = hardware.prepare_editor(radio, [self.row(100)], {"settings.autolk": True})
            hardware.upload("device", original, radio, blocks, progress,
                            lambda: events.append(("before-write",)), editor=True)
            observed = hardware.settle_and_verify("device", radio, progress)
            worker.verify_images(original, radio, observed)
        self.assertEqual(len(ports), 3)
        page_blocks = sorted({start
                              for address in blocks
                              for width in (0x100 if 0x1000 <= address < 0x1800 else 0x20,)
                              for start in range(address // width * width, address // width * width + width, 16)})
        self.assertEqual([port.written for port in ports], [[], page_blocks, []])
        self.assertTrue(all(not port.is_open and not port.dtr for port in ports))
        self.assertLess(events.index(("close", 0)), events.index(("quiet", 5)))
        self.assertLess(events.index(("quiet", 5)), events.index(("open", 1)))
        self.assertLess(events.index(("identify", 1)), events.index(("before-write",)))
        self.assertEqual(bytes(shared), worker.packed(radio)[:0x2000])

    def test_name_only_edit_uses_fresh_sessions_and_preserves_its_page_companion(self):
        starting = self.radio()
        worker.apply_plan(starting, "rx", chirp_common, settings, catalog=CATALOG)
        hardware, ports, events, shared = self.protocol_hardware(worker.packed(starting))
        progress = lambda *args, **kwargs: None
        with mock.patch.object(worker.time, "sleep", side_effect=lambda seconds: events.append(("quiet", seconds)) if seconds == 5 else None):
            candidate = hardware.download("device", progress)
            original = self.radio(worker.packed(candidate))
            row = worker.editor_memory(candidate, 1, chirp_common)
            row["name"] = "TEST-RX"
            blocks = hardware.prepare_editor(candidate, [row], {})
            self.assertEqual(blocks, [0x1010])
            hardware.upload("device", original, candidate, blocks, progress,
                            lambda: events.append(("before-write",)), editor=True)
            observed = hardware.settle_and_verify("device", candidate, progress)
            worker.verify_images(original, candidate, observed)
        self.assertEqual(len(ports), 3)
        self.assertEqual([port.written for port in ports], [[], list(range(0x1000, 0x1100, 16)), []])
        self.assertEqual([event for event in events if event[0] == "identify"],
                         [("identify", 0), ("identify", 1), ("identify", 2)])
        self.assertEqual(events.count(("quiet", 5)), 2)
        self.assertTrue(all(not port.is_open and not port.dtr for port in ports))
        self.assertEqual(bytes(shared[0x1010:0x1017]), b"TEST-RX")
        self.assertEqual(observed.get_memory(1).name, "TEST-RX")
        self.assertEqual(worker.packed(observed)[:0x1010], worker.packed(original)[:0x1010])
        self.assertEqual(worker.packed(observed)[0x1017:], worker.packed(original)[0x1017:])


if __name__ == "__main__":
    unittest.main()
