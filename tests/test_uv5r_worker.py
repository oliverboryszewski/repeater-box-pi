"""External UV-5R profile boundaries, native layout and guarded transactions.

Synthetic identity/firmware markers use only the official pinned CHIRP fixture;
no private radio image, device serial or user channel plan is embedded here.
"""
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
spec = importlib.util.spec_from_file_location("uv5r_worker", ROOT / "scripts/program-radio.py")
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)
SOURCE = Path(os.environ.get("CHIRP_TEST_SOURCE", "/opt/repeater-box/vendor/chirp"))
FIXTURE = SOURCE / "tests/images/Baofeng_UV-5R.img"
if FIXTURE.exists():
    sys.path.insert(0, str(SOURCE))
    from chirp import chirp_common, memmap, settings
    from chirp.drivers import baofeng_common, uv5r
    from chirp.drivers.baofeng_wp970i import BF5RXRadio

CONFIG = {"schema": 1, "model": "5RX", "devices": {
    "rx": "/dev/serial/by-id/usb-AIOC_All-In-One-Cable_test_rx-if04",
    "tx": "/dev/serial/by-id/usb-AIOC_All-In-One-Cable_test_tx-if04",
    "programmer": "/dev/serial/by-path/platform-test-usb-0:1.4:1.0-port0"}}


class ModelRequestTests(unittest.TestCase):
    def test_model_is_bounded_and_available_only_on_external_reads(self):
        request = {"request_id": "a" * 32, "role": "programmer", "action": "editor-read"}
        for model in worker.MODELS:
            self.assertTrue(worker.valid_request({**request, "model": model}))
        for model in (None, True, [], {}, "uv5r", "UV-5R", "chirp.drivers.uv5r.BaofengUV5RGeneric"):
            self.assertFalse(worker.valid_request({**request, "model": model}))
        for role in worker.REPEATER_ROLES:
            self.assertFalse(worker.valid_request({**request, "role": role, "model": "UV5R"}))
        for action in ("verify", "repair-upload", "editor-write"):
            self.assertFalse(worker.valid_request({**request, "action": action, **({"catalog_digest": DIGEST} if action == "program" else {}), "model": "UV5R"}))

    def test_public_model_is_pinned_to_pending_upload_and_legacy_is_5rx(self):
        self.assertEqual(worker.public_role({})["model"], "5RX")
        state = {"selected_model": "UV5R", "editor_model": "UV5R", "program_model": "5RX",
                 "verification_required": True}
        self.assertEqual(worker.public_role(state)["model"], "5RX")
        state["verification_required"] = False
        self.assertEqual(worker.public_role(state)["model"], "UV5R")


@unittest.skipUnless(FIXTURE.exists(), "Set CHIRP_TEST_SOURCE to the pinned official source")
class UV5RTests(unittest.TestCase):
    def radio(self, raw=None):
        if raw is None:
            raw = bytearray(worker.packed(uv5r.BaofengUV5RGeneric(str(FIXTURE))))
            raw[:8] = worker.KNOWN_IDENT
            raw[0x1838:0x1846] = worker.KNOWN_FIRMWARE_PREFIX.ljust(14, b"\xff")
        radio = uv5r.BaofengUV5RGeneric(None)
        radio._mmap = memmap.MemoryMapBytes(bytes(raw))
        radio.process_mmap()
        return radio

    def row(self, number=40, **changes):
        row = {"number": number, "empty": False, "name": "BOX", "rx_hz": 433500000,
               "tx_hz": 145300000, "mode": "NFM", "power": "High", "skip": False,
               "tx_tone": {"type": "CTCSS", "value": 88.5}, "rx_tone": None}
        row.update(changes)
        return row

    def apply(self, radio, rows=None, changed_settings=None):
        return worker.apply_editor(radio, rows or [], changed_settings or {}, chirp_common, settings)

    def test_native_identity_is_prefix_and_model_is_distinct_from_same_ident_5rx(self):
        radio = self.radio()
        identity = worker.image_identity(radio)
        self.assertEqual(identity["selected_model"], "UV5R")
        self.assertTrue(identity["compatible"])
        self.assertEqual(len(worker.packed(radio)), 0x1948)
        self.assertFalse(worker.same_identity(identity, {**identity, "selected_model": "5RX"}))
        raw = bytearray(worker.packed(radio)); raw[0x1838] ^= 1
        self.assertFalse(worker.image_identity(self.radio(raw))["compatible"])
        with self.assertRaises(worker.ProgrammingError):
            worker.editor_layout(self.radio(raw))

    def test_snapshot_uses_uv5r_bounds_power_charset_and_settings_adapter(self):
        snapshot = worker.editor_snapshot(self.radio(), "programmer", "a" * 32, chirp_common)
        self.assertEqual(snapshot["model"], "UV5R")
        self.assertEqual(snapshot["capabilities"]["bands_hz"], [[130000000, 176000000], [400000000, 520000000]])
        self.assertEqual(snapshot["capabilities"]["powers"], ["High", "Low"])
        self.assertEqual(snapshot["capabilities"]["settings"]["settings.squelch"],
                         {"label": "Squelch", "type": "integer", "min": 0, "max": 9})
        self.assertEqual(snapshot["capabilities"]["settings"]["settings.vox"]["options"][0], "OFF")
        self.assertNotIn("settings.pttid", snapshot["settings"])
        self.assertNotIn("a", snapshot["capabilities"]["valid_characters"])

    def test_channel40_odd_split_and_tones_round_trip_preserving_calibration(self):
        radio = self.radio(); before = worker.packed(radio)
        self.apply(radio, [self.row()])
        self.assertEqual(worker.editor_memory(radio, 40, chirp_common), self.row())
        after = worker.packed(radio)
        allowed = set(range(8 + 40 * 16, 8 + 41 * 16)) | set(range(0x1008 + 40 * 16, 0x1008 + 41 * 16))
        self.assertTrue({i for i, (old, new) in enumerate(zip(before, after)) if old != new} <= allowed)
        self.assertEqual(before[:8], after[:8])
        self.assertEqual(before[0x1808:], after[0x1808:])

    def test_settings_map_bare_driver_keys_and_native_offsets_without_neighbors(self):
        radio = self.radio(); before = worker.packed(radio)
        self.apply(radio, changed_settings={"settings.squelch": 3, "settings.vox": "OFF",
                                          "settings.autolk": True, "settings.displayab": "B"})
        after = worker.packed(radio)
        self.assertTrue({i for i, (old, new) in enumerate(zip(before, after)) if old != new}
                        <= {0xE28, 0xE2C, 0xE40, 0xE52})
        self.assertEqual((before[0xE52] ^ after[0xE52]) & 0x7F, 0)
        for settings_change in ({"settings.pttid": "Off"}, {"settings.vox": "Off"},
                                {"settings.squelch": "3"}, {"settings.squelch": True}):
            with self.assertRaises(worker.ProgrammingError):
                self.apply(self.radio(), changed_settings=settings_change)

    def test_existing_name_preserves_memory_extras_and_reserved_name_bytes(self):
        radio = self.radio(); self.apply(radio, [self.row()])
        raw = bytearray(worker.packed(radio))
        start, name_start = 8 + 40 * 16, 0x1008 + 40 * 16
        raw[start + 12:start + 14] = b"\xb5\xa5"
        raw[name_start + 7:name_start + 16] = b"reserved!"
        radio = self.radio(raw); row = worker.editor_memory(radio, 40, chirp_common); row["name"] = "TEST123"
        blocks = self.apply(radio, [row])
        self.assertEqual(blocks, [0x1280])
        after = worker.packed(radio)
        self.assertEqual(after[start:start+16], raw[start:start+16])
        self.assertEqual(after[name_start + 7:name_start + 16], b"reserved!")
        row["name"] = "BOX"
        self.apply(radio, [row])
        self.assertEqual(worker.packed(radio)[name_start:name_start+7], b"BOX\xff\xff\xff\xff")

    def test_profile_cannot_change_station_plan_or_qualify_paired_switch(self):
        with self.assertRaises(worker.ProgrammingError):
            worker.apply_plan(self.radio(), "rx", chirp_common, settings, catalog=CATALOG)
        with self.assertRaises(worker.ProgrammingError):
            worker.pair_preflight(self.radio())

    def upload_hardware(self, original, candidate, aux_change=False):
        raw = worker.packed(original); frames = []
        class Port:
            is_open = True
            dtr = True
            def write(self, frame): frames.append(frame); return len(frame)
            def close(self): self.is_open = False
        hardware = worker.Hardware.__new__(worker.Hardware)
        hardware.catalog = CATALOG
        hardware.driver = uv5r.BaofengUV5RGeneric
        hardware.common = types.SimpleNamespace(_make_frame=baofeng_common._make_frame, _rawrecv=lambda _, size: b"\x06")
        hardware.uv5r = types.SimpleNamespace(
            _ident_radio=lambda _: raw[:8],
            _get_aux_data_from_radio=lambda _: (raw[0x1838:0x1846], raw[0x1848:0x18A8],
                b"X" * 16 if aux_change else raw[0x18B8:0x18C8], raw[0x18D8:0x1908], True))
        hardware.open = mock.Mock(side_effect=lambda _: Port())
        candidate.pipe = Port()
        return hardware, frames

    def test_upload_physical_pages_use_plus_eight_payload_and_no_aux_writes(self):
        original = self.radio(); self.apply(original, [self.row(1)])
        candidate = self.radio(worker.packed(original))
        row = worker.editor_memory(candidate, 1, chirp_common); row["name"] = "TEST123"
        blocks = self.apply(candidate, [row])
        hardware, frames = self.upload_hardware(original, candidate)
        before_write = mock.Mock()
        with mock.patch.object(worker.time, "sleep", lambda _: None):
            hardware.upload("device", original, candidate, blocks, lambda: None, before_write, editor=True)
        before_write.assert_called_once_with()
        self.assertEqual([struct.unpack(">BHB", frame[:4]) for frame in frames],
                         [(ord("X"), address, 16) for address in range(0x1000, 0x1100, 16)])
        for frame in frames:
            address = struct.unpack(">BHB", frame[:4])[1]
            self.assertEqual(frame[4:], worker.packed(candidate)[8+address:8+address+16])
        self.assertEqual(frames[1][4:11], b"TEST123")

    def test_changed_protected_aux_stops_before_write_callback(self):
        original = self.radio(); candidate = self.radio(worker.packed(original))
        blocks = self.apply(candidate, [self.row(1)])
        hardware, frames = self.upload_hardware(original, candidate, aux_change=True)
        before_write = mock.Mock()
        with mock.patch.object(worker.time, "sleep", lambda _: None), self.assertRaises(worker.ProgrammingError):
            hardware.upload("device", original, candidate, blocks, lambda: None, before_write, editor=True)
        self.assertEqual(frames, [])
        before_write.assert_not_called()

    def test_same_session_readback_rebuilds_prefix_main_and_compressed_aux(self):
        original = self.radio(); raw = worker.packed(original); calls = []
        def read_block(radio, address, size, first):
            calls.append((address, size, first))
            native = address + 8 if address < 0x1800 else 0x1808 + address - 0x1EC0
            return raw[native:native+size]
        hardware = worker.Hardware.__new__(worker.Hardware)
        hardware.catalog = CATALOG
        hardware.driver, hardware.memmap = uv5r.BaofengUV5RGeneric, memmap
        hardware.uv5r = types.SimpleNamespace(_read_block=read_block)
        result = hardware.readback(original, lambda: None)
        self.assertEqual(worker.packed(result), raw)
        self.assertEqual(calls[:2], [(0, 0x40, False), (0x40, 0x40, False)])
        self.assertIn((0x1EC0, 0x40, False), calls)
        self.assertEqual(calls[-4:], [(address, 0x10, False) for address in range(0x1FC0, 0x2000, 0x10)])
        self.assertFalse(any(0x1800 <= address < 0x1EC0 for address, _, _ in calls))

    def test_settings_page_keeps_both_halves_adjacent_before_progress(self):
        original = self.radio(); candidate = self.radio(worker.packed(original))
        value = not bool(candidate._memobj.settings.autolk)
        blocks = self.apply(candidate, changed_settings={"settings.autolk": value})
        hardware, frames = self.upload_hardware(original, candidate)
        metrics = []
        def progress(): metrics.append(len(frames))
        with mock.patch.object(worker.time, "sleep", lambda _: None):
            hardware.upload("device", original, candidate, blocks, progress, lambda: None, editor=True)
        self.assertEqual([struct.unpack(">BHB", frame[:4])[1] for frame in frames], [0xE20, 0xE30])
        self.assertEqual(frames[0][4:], worker.packed(candidate)[0xE28:0xE38])
        self.assertEqual(frames[1][4:], worker.packed(candidate)[0xE38:0xE48])
        self.assertTrue(all(count in (0, 2) for count in metrics))

    def test_short_serial_write_is_detected_and_not_retried(self):
        original = self.radio(); candidate = self.radio(worker.packed(original))
        blocks = self.apply(candidate, [self.row(1)])
        hardware, frames = self.upload_hardware(original, candidate)
        port = hardware.open("device")
        calls = []
        def write(frame): calls.append(frame); return len(frame) - 1
        port.write = write
        hardware.open = mock.Mock(return_value=port)
        before_write = mock.Mock()
        with mock.patch.object(worker.time, "sleep", lambda _: None), self.assertRaises(worker.ProgrammingError):
            hardware.upload("device", original, candidate, blocks, lambda: None, before_write, editor=True)
        self.assertEqual(len(calls), 1)
        before_write.assert_called_once_with()
        self.assertFalse(port.is_open)

    def test_calibration_prefix_and_unreviewed_global_edits_refused(self):
        for offset in (0, 0x808, 0xE3B, 0x1808, 0x18B8):
            original = self.radio(); raw = bytearray(worker.packed(original)); raw[offset] ^= 1
            candidate = self.radio(raw)
            hardware, frames = self.upload_hardware(original, candidate)
            with self.assertRaises(worker.ProgrammingError):
                hardware.upload("device", original, candidate,
                                worker.changed_blocks(original, worker.packed(original), raw),
                                lambda: None, lambda: None, editor=True)
            self.assertEqual(frames, [])

    def test_saved_repair_pages_align_physical_not_native_prefix(self):
        original = self.radio(); self.apply(original, [self.row(1)])
        candidate = self.radio(worker.packed(original))
        row = worker.editor_memory(candidate, 1, chirp_common); row["name"] = "TEST123"
        self.apply(candidate, [row])
        self.assertEqual(worker.saved_repair_pages(original, candidate, editor=True), [0x1000])
        damaged = bytearray(worker.packed(candidate)); damaged[0x1008:0x1018] = b"\0"*16
        actual = self.radio(damaged)
        repaired, pages = worker.saved_repair_image(actual, original, candidate, editor=True)
        self.assertEqual(repaired, worker.packed(candidate))
        self.assertEqual(pages, [0x1000])
        damaged[0x1808] ^= 1
        with self.assertRaises(worker.ProgrammingError):
            worker.saved_repair_image(self.radio(damaged), original, candidate, editor=True)

    def test_verify_masks_only_runtime_bits_at_uv5r_native_offsets(self):
        original = self.radio(); candidate = self.radio(worker.packed(original))
        self.apply(candidate, [self.row()])
        actual = bytearray(worker.packed(candidate))
        for offset, mask in ((0xE55, 1), (0xE7E, 0x7F), (0xE7F, 0x7F)): actual[offset] ^= mask
        worker.verify_images(original, candidate, self.radio(actual))
        actual[0xE40] ^= 1
        with self.assertRaises(worker.ProgrammingError):
            worker.verify_images(original, candidate, self.radio(actual))

    def make_worker(self, temporary):
        test = self
        state, backups = Path(temporary)/"state", Path(temporary)/"backups"; state.mkdir(); backups.mkdir()
        class Hardware:
            model = "5RX"
            uploads = 0
            reads = 0
            fail_verify = False
            current = worker.packed(test.radio())
            def select_model(self, model): self.model = model
            def download(self, device, progress):
                self.reads += 1
                if self.model != "UV5R": raise IOError("unsupported synthetic hardware")
                return test.radio(self.current)
            def load(self, path):
                return uv5r.BaofengUV5RGeneric(str(path)) if self.model == "UV5R" else BF5RXRadio(str(path))
            def release(self, radio): pass
            def editor_snapshot(self, radio, role, base_id): return worker.editor_snapshot(radio, role, base_id, chirp_common)
            def recovery_differences(self, expected, observed): return worker.recovery_differences(expected, observed, chirp_common)
            def prepare_editor(self, radio, changes, values): return test.apply(radio, changes, values)
            def upload(self, device, original, candidate, blocks, progress, before_write, editor=False, saved_repair=None):
                if saved_repair: worker.validate_saved_repair(original, candidate, saved_repair, editor)
                if blocks: before_write()
                self.current = worker.packed(candidate); self.uploads += 1
            def settle_and_verify(self, device, radio, progress):
                if self.fail_verify: raise IOError("synthetic connection failure")
                return test.radio(self.current)
        hardware = Hardware()
        instance = worker.Worker(hardware, state_dir=state, backup_dir=backups, owner_uid=os.getuid(),
                                 service=lambda *args: False)
        instance.catalog = CATALOG
        return instance, hardware, state, backups

    def request(self, number, action, **extra):
        return {"request_id": f"{number:032x}", "role": "programmer", "action": action, **({"catalog_digest": DIGEST} if action == "program" else {}), **extra}

    def test_external_read_write_verify_derive_model_from_root_private_base(self):
        with tempfile.TemporaryDirectory() as temporary:
            instance, hardware, state, backups = self.make_worker(temporary)
            read = self.request(1, "editor-read", model="UV5R")
            self.assertTrue(instance.run(read, CONFIG)["success"])
            self.assertEqual(instance.private["programmer"]["editor_model"], "UV5R")
            result = instance.run(self.request(2, "editor-write", base_id=read["request_id"],
                                              changes=[self.row()], settings={}), CONFIG)
            self.assertTrue(result["success"], result)
            saved = instance.private["programmer"]
            self.assertEqual(saved["program_model"], "UV5R")
            self.assertEqual(worker.public_role(saved)["model"], "UV5R")
            manifest = json.loads((backups/f'{2:032x}'/"manifest.json").read_text())
            self.assertEqual(manifest["selected_model"], "UV5R")
            self.assertTrue(instance.run(self.request(3, "verify"), CONFIG)["success"])
            self.assertEqual(hardware.model, "UV5R")
            self.assertEqual(json.loads((state/"radio-editor-programmer.json").read_text())["model"], "UV5R")

    def test_pending_profile_switch_refused_before_read_and_repair_keeps_original_model(self):
        with tempfile.TemporaryDirectory() as temporary:
            instance, hardware, state, backups = self.make_worker(temporary)
            self.assertTrue(instance.run(self.request(1, "editor-read", model="UV5R"), CONFIG)["success"])
            hardware.fail_verify = True
            result = instance.run(self.request(2, "editor-write", base_id=f'{1:032x}', changes=[self.row()], settings={}), CONFIG)
            self.assertFalse(result["success"]); self.assertTrue(result["verification_required"])
            reads = hardware.reads
            result = instance.run(self.request(3, "editor-read", model="5RX"), CONFIG)
            self.assertFalse(result["success"])
            self.assertEqual(hardware.reads, reads)
            self.assertEqual(instance.private["programmer"]["program_model"], "UV5R")
            hardware.fail_verify = False
            result = instance.run(self.request(4, "repair-upload", pending_id=f'{2:032x}'), CONFIG)
            self.assertTrue(result["success"], result)
            self.assertEqual(instance.private["programmer"]["program_model"], "UV5R")

    def test_inconsistent_pending_model_cannot_be_erased_by_a_profile_read(self):
        with tempfile.TemporaryDirectory() as temporary:
            instance, hardware, state, backups = self.make_worker(temporary)
            instance.private["programmer"] = {"verification_required": True,
                "program_id": "f" * 32, "program_model": "UV5R", "selected_model": "5RX"}
            result = instance.run(self.request(1, "editor-read", model="UV5R"), CONFIG)
            self.assertFalse(result["success"])
            self.assertEqual(hardware.reads, 0)
            self.assertTrue(instance.private["programmer"]["verification_required"])
            self.assertEqual(instance.private["programmer"]["program_id"], "f" * 32)

    def test_unreviewed_firmware_is_backed_up_read_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            instance, hardware, state, backups = self.make_worker(temporary)
            raw = bytearray(hardware.current); raw[0x1838] ^= 1; hardware.current = bytes(raw)
            result = instance.run(self.request(1, "editor-read", model="UV5R"), CONFIG)
            self.assertFalse(result["success"])
            self.assertEqual(hardware.uploads, 0)
            self.assertTrue((backups / f'{1:032x}' / "original.img").exists())
            self.assertFalse((state / "radio-editor-programmer.json").exists())

    def test_repair_manifest_cannot_reinterpret_pending_image_as_another_model(self):
        with tempfile.TemporaryDirectory() as temporary:
            instance, hardware, state, backups = self.make_worker(temporary)
            self.assertTrue(instance.run(self.request(1, "editor-read", model="UV5R"), CONFIG)["success"])
            hardware.fail_verify = True
            self.assertFalse(instance.run(self.request(2, "editor-write", base_id=f'{1:032x}',
                                                       changes=[self.row()], settings={}), CONFIG)["success"])
            path = backups / f'{2:032x}' / "manifest.json"
            manifest = json.loads(path.read_text()); manifest["selected_model"] = "5RX"
            path.write_text(json.dumps(manifest))
            reads, uploads = hardware.reads, hardware.uploads
            result = instance.run(self.request(3, "repair-upload", pending_id=f'{2:032x}'), CONFIG)
            self.assertFalse(result["success"])
            self.assertEqual((hardware.reads, hardware.uploads), (reads, uploads))
            self.assertEqual(instance.private["programmer"]["program_model"], "UV5R")

    def test_retention_counts_both_profiles_together_and_keeps_active_base(self):
        with tempfile.TemporaryDirectory() as temporary:
            instance, hardware, state, backups = self.make_worker(temporary)
            radio = self.radio()
            for number in range(1, 9):
                instance.backup(radio, f'{number:032x}', "original", "programmer", CONFIG["devices"]["programmer"])
            instance.private["programmer"] = {"editor_base_id": f'{1:032x}', "editor_model": "UV5R"}
            # A former 5RX set is also eligible in the same role's five-set cap.
            manifest_path = backups / f'{2:032x}' / "manifest.json"
            manifest = json.loads(manifest_path.read_text()); manifest["selected_model"] = "5RX"
            manifest_path.write_text(json.dumps(manifest))
            instance.prune_backups()
            folders = [folder for folder in backups.iterdir() if folder.is_dir()]
            self.assertEqual(len(folders), 5)
            self.assertTrue((backups / f'{1:032x}').is_dir())
            self.assertFalse((backups / f'{2:032x}').exists())

    def test_nonpending_profile_switch_invalidates_prior_candidate_and_editor(self):
        with tempfile.TemporaryDirectory() as temporary:
            instance, hardware, state, backups = self.make_worker(temporary)
            self.assertTrue(instance.run(self.request(1, "editor-read", model="UV5R"), CONFIG)["success"])
            instance.private["programmer"].update(program_id="f"*32, programmed=True, program_model="UV5R")
            result = instance.run(self.request(2, "editor-read", model="5RX"), CONFIG)
            self.assertFalse(result["success"])
            self.assertNotIn("program_id", instance.private["programmer"])
            self.assertFalse((state/"radio-editor-programmer.json").exists())


if __name__ == "__main__":
    unittest.main()
