"""Saved-candidate page recovery remains bounded to a private reviewed upload."""
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
spec = importlib.util.spec_from_file_location("saved_repair_worker", ROOT / "scripts" / "program-radio.py")
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)
SOURCE = Path(os.environ.get("CHIRP_TEST_SOURCE", "/opt/repeater-box/vendor/chirp"))
FIXTURE = SOURCE / "tests" / "images" / "Baofeng_5RX.img"
if FIXTURE.exists():
    sys.path.insert(0, str(SOURCE))
    from chirp import chirp_common, memmap, settings
    from chirp.drivers.baofeng_wp970i import BF5RXRadio
    from chirp.drivers import baofeng_common

CONFIG = {"schema": 1, "model": "5RX", "devices": {
    "rx": "/dev/serial/by-id/usb-AIOC_All-In-One-Cable_test_rx-if04",
    "tx": "/dev/serial/by-id/usb-AIOC_All-In-One-Cable_test_tx-if04",
    "programmer": "/dev/serial/by-path/platform-test-usb-0:1.4:1.0-port0"}}


class RepairRequestTests(unittest.TestCase):
    def test_exact_request_pins_pending_candidate_without_paths_or_offset_authority(self):
        for role in worker.ROLES:
            request = {"request_id": "a" * 32, "role": role, "action": "repair-upload", "pending_id": "b" * 32}
            self.assertTrue(worker.valid_request(request))
            for key in ("path", "image", "driver", "saved_repair", "pages", "command"):
                self.assertFalse(worker.valid_request({**request, key: "anything"}))
            for token in (None, True, "../candidate", "B" * 32, "b" * 31):
                self.assertFalse(worker.valid_request({**request, "pending_id": token}))
            self.assertFalse(worker.valid_request({key: value for key, value in request.items() if key != "pending_id"}))

    def test_public_pending_id_only_projects_valid_current_unfinished_candidate(self):
        state = {"program_id": "a" * 32, "verification_required": True, "device": "private", "repair_source_id": "b" * 32}
        self.assertEqual(worker.public_role(state)["pending_id"], "a" * 32)
        self.assertNotIn("repair_source_id", worker.public_role(state))
        self.assertIsNone(worker.public_role({**state, "verification_required": False})["pending_id"])
        self.assertIsNone(worker.public_role({**state, "program_id": "../private"})["pending_id"])


@unittest.skipUnless(FIXTURE.exists(), "Set CHIRP_TEST_SOURCE to the pinned official CHIRP source")
class SavedRepairTests(unittest.TestCase):
    def radio(self, raw=None):
        if raw is None:
            return BF5RXRadio(str(FIXTURE))
        radio = BF5RXRadio(None)
        radio._mmap = memmap.MemoryMapBytes(bytes(raw))
        radio.process_mmap()
        return radio

    def images(self):
        radio = self.radio()
        worker.apply_plan(radio, "rx", chirp_common, settings, catalog=CATALOG)
        raw = bytearray(worker.packed(radio))
        raw[0xE30:0xE40] = bytes.fromhex("03000000050101000100000000030102")
        raw[0xE28], raw[0xE4C] = 1, 0
        raw[0x1010:0x1017] = b"BOX-RXX"
        original = self.radio(raw)
        candidate = self.radio(raw)
        worker.apply_plan(candidate, "rx", chirp_common, settings, catalog=CATALOG)
        broken = bytearray(worker.packed(candidate))
        broken[0xE30:0xE40] = b"\0" * 16
        broken[0x1010:0x1017] = b"BOX-RXX"
        return original, candidate, self.radio(broken)

    def make_worker(self, temporary, role="rx", kind="repeater"):
        test = self
        state, backups = Path(temporary) / "state", Path(temporary) / "backups"
        state.mkdir(); backups.mkdir()
        class FakeHardware:
            def __init__(self):
                self.current = b""
                self.uploads = []
                self.fail_readback = False
                self.ignore_upload = False
                self.after_download = None
                self.corrupt_readback = False
            def download(self, device, progress):
                radio = test.radio(self.current)
                if self.after_download:
                    self.after_download()
                return radio
            def load(self, path):
                return BF5RXRadio(str(path))
            def release(self, radio):
                pass
            def upload(self, device, original, candidate, blocks, progress, before_write,
                       editor=False, saved_repair=None):
                proven, pages = worker.validate_saved_repair(original, candidate, saved_repair, editor)
                test.assertEqual(blocks, proven)
                test.assertEqual(instance.private[role]["program_id"], request_id[0])
                folder = backups / request_id[0]
                test.assertTrue((folder / "candidate.img").exists())
                test.assertIn("repair", json.loads((folder / "manifest.json").read_text()))
                self.uploads.append((blocks, pages, editor))
                if blocks:
                    before_write()
                if not self.ignore_upload:
                    self.current = worker.packed(candidate)
            def settle_and_verify(self, device, radio, progress):
                if self.fail_readback:
                    raise IOError("private serial details")
                if self.corrupt_readback:
                    raw = bytearray(self.current); raw[0x1F80] ^= 1
                    return test.radio(raw)
                return test.radio(self.current)
            def editor_snapshot(self, radio, selected_role, base_id):
                return worker.editor_snapshot(radio, selected_role, base_id, chirp_common)
            def recovery_differences(self, expected, observed):
                return worker.recovery_differences(expected, observed, chirp_common)
        hardware = FakeHardware()
        active, enabled = set(), set()
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
        original, candidate, broken = self.images()
        pending_id = "1" * 32
        folder = instance.backup(original, pending_id, "original", role, CONFIG["devices"][role])
        candidate.save_mmap(str(folder / "candidate.img"))
        os.chmod(folder / "candidate.img", 0o640)
        hardware.current = worker.packed(broken)
        instance.private[role] = {**worker.image_identity(original), "device": CONFIG["devices"][role],
                                  "inspected": True, "programmed": True, "verified": False,
                                  "verification_required": True, "program_id": pending_id,
                                  "backup_id": pending_id, "program_kind": kind,
                                  "resume_services": ["repeater-engine.service"],
                                  "resume_enabled": ["repeater-engine.service"]}
        instance.publish()
        request_id = ["2" * 32]
        def repair(identifier="2" * 32, pending=None, config=CONFIG):
            request_id[0] = identifier
            return instance.run({"request_id": identifier, "role": role, "action": "repair-upload",
                                 "pending_id": pending or instance.private[role]["program_id"]}, config)
        return instance, hardware, active, enabled, folder, repair

    def test_restores_zeroed_companion_page_bytes_and_preserves_every_other_page(self):
        original, saved, current = self.images()
        before, expected = worker.packed(current), worker.packed(saved)
        pages = worker.saved_repair_pages(original, saved)
        self.assertEqual(pages, [0xE20, 0xE40, 0x1000])
        blocks = worker.prepare_saved_repair(current, original, saved)
        self.assertIn(0xE30, blocks)
        self.assertEqual(worker.packed(current), expected)
        self.assertEqual(worker.packed(current)[0xE30:0xE40], bytes.fromhex("03000000050101000100000000030102"))
        for i, (old, new) in enumerate(zip(before, worker.packed(current))):
            if i - i % 0x20 not in pages:
                self.assertEqual(old, new)

    def test_runtime_bits_are_preserved_both_inside_and_outside_repaired_pages(self):
        original, saved, current = self.images()
        raw = bytearray(worker.packed(current))
        raw[0xE4D] ^= 1
        raw[0xE76] = (raw[0xE76] & 0x80) | 127
        raw[0xE77] = (raw[0xE77] & 0x80) | 17
        current = self.radio(raw)
        worker.prepare_saved_repair(current, original, saved)
        for index in worker.VOLATILE_MASKS:
            self.assertEqual(worker.packed(current)[index], raw[index])
        worker.verify_images(original, saved, current)

    def test_fresh_changes_outside_affected_pages_refuse_including_reserved_and_calibration(self):
        original, saved, current = self.images()
        for address, mask in ((0x10, 1), (0xE60, 1), (0xE76, 0x80), (0x1020, 1), (0x1F80, 1)):
            raw = bytearray(worker.packed(current)); raw[address] ^= mask
            with self.subTest(address=address), self.assertRaises(worker.ProgrammingError):
                worker.prepare_saved_repair(self.radio(raw), original, saved)

    def test_saved_proof_never_authorizes_protected_or_empty_source_diffs(self):
        original, saved, _current = self.images()
        for address, mask in ((0x1F80, 1), (0xE4A, 1), (0xE76, 1), (0x900, 1)):
            raw = bytearray(worker.packed(saved)); raw[address] ^= mask
            with self.subTest(address=address), self.assertRaises(worker.ProgrammingError):
                worker.saved_repair_pages(original, self.radio(raw))
        with self.assertRaises(worker.ProgrammingError):
            worker.saved_repair_pages(original, original)

    def test_candidate_identity_and_full_reconstruction_are_strict(self):
        original, saved, fresh = self.images()
        prepared = self.radio(worker.packed(fresh))
        worker.prepare_saved_repair(prepared, original, saved)
        for address in (0xE30, 0x1010, 0x1F80):
            raw = bytearray(worker.packed(prepared)); raw[address] ^= 1
            with self.subTest(address=address), self.assertRaises(worker.ProgrammingError):
                worker.validate_saved_repair(fresh, self.radio(raw), (original, saved))
        raw = bytearray(worker.packed(fresh)); raw[0x1EF0] ^= 1
        with self.assertRaises(worker.ProgrammingError):
            worker.prepare_saved_repair(self.radio(raw), original, saved)

    def test_durable_replacement_preserves_old_source_and_only_resumes_after_strict_verify(self):
        for role, kind in (("rx", "repeater"), ("tx", "repeater"), ("programmer", "editor")):
            with self.subTest(role=role), tempfile.TemporaryDirectory() as temporary:
                instance, hardware, active, enabled, folder, repair = self.make_worker(temporary, role, kind)
                old_files = {name: (folder / name).read_bytes() for name in ("original.img", "candidate.img")}
                result = repair()
                self.assertTrue(result["success"], result)
                self.assertFalse(instance.private[role]["verification_required"])
                self.assertEqual(instance.private[role]["superseded_program_id"], folder.name)
                self.assertEqual(instance.private[role]["repair_source_id"], folder.name)
                self.assertEqual(hardware.uploads[0][2], kind == "editor")
                self.assertEqual(active, {"repeater-engine.service"})
                self.assertEqual(enabled, {"repeater-engine.service"})
                for name, content in old_files.items():
                    self.assertEqual((folder / name).read_bytes(), content)

    def test_failed_repair_keeps_original_candidate_and_services_paused_then_retries_same_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            instance, hardware, active, enabled, folder, repair = self.make_worker(temporary)
            old_candidate = (folder / "candidate.img").read_bytes()
            hardware.fail_readback, hardware.ignore_upload = True, True
            failed = repair()
            self.assertFalse(failed["success"], failed)
            self.assertTrue(instance.private["rx"]["verification_required"])
            self.assertEqual(instance.private["rx"]["repair_source_id"], folder.name)
            self.assertEqual(instance.private["rx"]["program_id"], "2" * 32)
            self.assertFalse(active); self.assertFalse(enabled)
            self.assertEqual((folder / "candidate.img").read_bytes(), old_candidate)
            hardware.fail_readback, hardware.ignore_upload = False, False
            retried = repair("3" * 32)
            self.assertTrue(retried["success"], retried)
            self.assertEqual(instance.private["rx"]["repair_source_id"], folder.name)
            self.assertEqual(instance.private["rx"]["superseded_program_id"], "2" * 32)
            self.assertEqual(len(hardware.uploads), 2)

    def test_stale_source_digest_refuses_retry_without_retargeting_pending_candidate(self):
        with tempfile.TemporaryDirectory() as temporary:
            instance, hardware, active, enabled, folder, repair = self.make_worker(temporary)
            hardware.fail_readback, hardware.ignore_upload = True, True
            self.assertFalse(repair()["success"])
            instance.private["rx"]["repair_source_candidate_sha256"] = "0" * 64
            result = repair("3" * 32)
            self.assertFalse(result["success"], result)
            self.assertEqual(len(hardware.uploads), 1)
            self.assertEqual(instance.private["rx"]["program_id"], "2" * 32)
            self.assertFalse(active); self.assertFalse(enabled)

    def test_failed_whole_image_verification_never_accepts_protected_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            instance, hardware, active, enabled, folder, repair = self.make_worker(temporary)
            hardware.corrupt_readback = True
            result = repair()
            self.assertFalse(result["success"], result)
            self.assertIn("0x1F80", result["message"])
            self.assertTrue(instance.private["rx"]["verification_required"])
            self.assertTrue((folder / "candidate.img").exists())
            self.assertFalse(active); self.assertFalse(enabled)

    def test_candidate_durability_failure_keeps_old_pending_record_and_never_uploads(self):
        with tempfile.TemporaryDirectory() as temporary:
            instance, hardware, active, enabled, folder, repair = self.make_worker(temporary)
            old_candidate = (folder / "candidate.img").read_bytes()
            real_sync = worker.sync_path
            def sync(path):
                if path.name == "candidate.img":
                    raise OSError("durability failure")
                return real_sync(path)
            with mock.patch.object(worker, "sync_path", side_effect=sync):
                result = repair()
            self.assertFalse(result["success"], result)
            self.assertFalse(hardware.uploads)
            self.assertEqual(instance.private["rx"]["program_id"], folder.name)
            self.assertEqual((folder / "candidate.img").read_bytes(), old_candidate)
            self.assertFalse(active); self.assertFalse(enabled)

    def test_repeated_failed_repairs_keep_five_sets_and_the_first_trusted_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            instance, hardware, active, enabled, folder, repair = self.make_worker(temporary)
            hardware.fail_readback, hardware.ignore_upload = True, True
            for number in range(2, 10):
                result = repair(f"{number:032x}")
                self.assertFalse(result["success"], result)
            sets = [path for path in instance.backup_dir.iterdir() if path.is_dir()]
            self.assertEqual(len(sets), 5)
            self.assertTrue(folder.exists())
            state = instance.private["rx"]
            self.assertTrue((instance.backup_dir / state["program_id"] / "candidate.img").exists())
            self.assertTrue((instance.backup_dir / state["superseded_program_id"] / "candidate.img").exists())
            self.assertEqual(state["repair_source_id"], folder.name)
            self.assertFalse(active); self.assertFalse(enabled)

    def test_verified_current_repair_keeps_its_one_source_after_many_new_editor_reads(self):
        with tempfile.TemporaryDirectory() as temporary:
            instance, hardware, _active, _enabled, folder, repair = self.make_worker(temporary)
            self.assertTrue(repair()["success"])
            self.assertTrue(instance.private["rx"]["programmed"])
            self.assertFalse(instance.private["rx"]["verification_required"])
            for number in range(3, 10):
                result = instance.run({"request_id": f"{number:032x}", "role": "rx", "action": "editor-read"}, CONFIG)
                self.assertTrue(result["success"], result)
            sets = [path for path in instance.backup_dir.iterdir() if path.is_dir()]
            self.assertEqual(len(sets), 5)
            self.assertTrue((folder / "original.img").exists())
            self.assertTrue((folder / "candidate.img").exists())
            self.assertTrue((instance.backup_dir / instance.private["rx"]["program_id"] / "candidate.img").exists())
            self.assertEqual(instance.private["rx"]["repair_source_id"], folder.name)

    def test_missing_pending_stale_review_binding_and_pair_states_never_upload(self):
        for alteration in ("not-pending", "stale-review", "binding", "pair-kind", "pair-transaction", "kind", "identity"):
            with self.subTest(alteration=alteration), tempfile.TemporaryDirectory() as temporary:
                instance, hardware, active, enabled, folder, repair = self.make_worker(temporary)
                state = instance.private["rx"]
                config, pending = CONFIG, folder.name
                if alteration == "not-pending": state["verification_required"] = False
                if alteration == "stale-review": pending = "f" * 32
                if alteration == "binding": config = {**CONFIG, "devices": {**CONFIG["devices"], "rx": "/dev/serial/by-id/usb-AIOC_All-In-One-Cable_changed-if04"}}
                if alteration == "pair-kind": state["program_kind"] = "pair"
                if alteration == "pair-transaction": instance.private["_pair"] = {"verification_required": True}
                if alteration == "kind": state["program_kind"] = "unknown"
                if alteration == "identity": state["firmware_hex"] = "00"
                result = repair(pending=pending, config=config)
                self.assertFalse(result["success"], result)
                self.assertFalse(hardware.uploads)
                self.assertEqual(state["program_id"], folder.name)

    def test_pending_change_during_fresh_download_never_supersedes_different_candidate(self):
        with tempfile.TemporaryDirectory() as temporary:
            instance, hardware, active, enabled, folder, repair = self.make_worker(temporary)
            hardware.after_download = lambda: instance.private["rx"].update(program_id="f" * 32)
            result = repair()
            self.assertFalse(result["success"], result)
            self.assertFalse(hardware.uploads)
            self.assertEqual(instance.private["rx"]["program_id"], "f" * 32)
            self.assertTrue((folder / "candidate.img").exists())

    def test_private_metadata_original_digest_symlinks_and_permissions_are_checked(self):
        for alteration in ("manifest-device", "manifest-model", "manifest-pin", "original-digest", "candidate-symlink", "candidate-mode", "directory-mode", "candidate-protected"):
            with self.subTest(alteration=alteration), tempfile.TemporaryDirectory() as temporary:
                instance, hardware, active, enabled, folder, repair = self.make_worker(temporary)
                manifest_path = folder / "manifest.json"
                manifest = json.loads(manifest_path.read_text())
                if alteration.startswith("manifest-"):
                    key = {"manifest-device": "device", "manifest-model": "selected_model", "manifest-pin": "chirp_commit"}[alteration]
                    manifest[key] = "changed"
                    worker.atomic_json(manifest_path, manifest)
                if alteration == "original-digest":
                    manifest["sha256"] = "0" * 64; worker.atomic_json(manifest_path, manifest)
                if alteration == "candidate-symlink":
                    (folder / "candidate.img").unlink(); (folder / "candidate.img").symlink_to(folder / "original.img")
                if alteration == "candidate-mode": (folder / "candidate.img").chmod(0o666)
                if alteration == "directory-mode": folder.chmod(0o777)
                if alteration == "candidate-protected":
                    radio = hardware.load(folder / "candidate.img")
                    raw = bytearray(worker.packed(radio)); raw[0x1F80] ^= 1
                    self.radio(raw).save_mmap(str(folder / "candidate.img"))
                result = repair()
                self.assertFalse(result["success"], result)
                self.assertFalse(hardware.uploads)
                self.assertEqual(instance.private["rx"]["program_id"], folder.name)
                self.assertTrue(instance.private["rx"]["verification_required"])
                self.assertFalse(active); self.assertFalse(enabled)

    def test_real_uploader_accepts_only_proven_companion_repair_and_sends_both_halves(self):
        saved_original, saved_candidate, original = self.images()
        candidate = self.radio(worker.packed(original))
        blocks = worker.prepare_saved_repair(candidate, saved_original, saved_candidate)
        frames = []
        class Port:
            def __init__(self): self.is_open, self.dtr = True, True
            def write(self, frame): frames.append(frame); return len(frame)
            def close(self): self.is_open = False
        old_port, new_port = Port(), Port()
        candidate.pipe = old_port
        hardware = worker.Hardware.__new__(worker.Hardware)
        hardware.catalog = CATALOG
        hardware.open = mock.Mock(return_value=new_port)
        raw = worker.packed(original)
        hardware.common = types.SimpleNamespace(
            _ident_radio=lambda _: raw[0x2000:0x2008],
            _get_aux_data_from_radio=lambda _: (raw[0x1EF0:0x1EFE], raw[0x1F00:0x1F60], raw[0x1F70:0x1F80], raw[0x1F90:0x1FC0], True),
            _make_frame=baofeng_common._make_frame, _rawrecv=lambda *_: b"\x06")
        with self.assertRaises(worker.ProgrammingError):
            hardware.upload("device", original, candidate, blocks, lambda: None, lambda: None)
        hardware.open.assert_not_called()
        tampered = self.radio(worker.packed(candidate))
        tampered.get_mmap().set(0xE30, bytes([worker.packed(tampered)[0xE30] ^ 1]))
        tampered.pipe = old_port
        with self.assertRaises(worker.ProgrammingError):
            hardware.upload("device", original, tampered, blocks, lambda: None, lambda: None,
                            saved_repair=(saved_original, saved_candidate))
        hardware.open.assert_not_called()
        before_write = mock.Mock()
        with mock.patch.object(worker.time, "sleep", lambda seconds: None):
            hardware.upload("device", original, candidate, blocks, lambda: None, before_write,
                            saved_repair=(saved_original, saved_candidate))
        before_write.assert_called_once_with()
        addresses = [struct.unpack(">BHB", frame[:4])[1] for frame in frames]
        self.assertEqual(addresses, [0xE20, 0xE30, 0xE40, 0xE50, *range(0x1000, 0x1100, 16)])
        for frame, address in zip(frames, addresses):
            self.assertEqual(frame[4:], worker.packed(candidate)[address:address + 16])


if __name__ == "__main__":
    unittest.main()
