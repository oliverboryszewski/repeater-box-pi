"""Bounded TX name-page experiment and contiguous physical transfer policy."""
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
spec = importlib.util.spec_from_file_location("name_page_experiment", ROOT / "scripts/check-pair-name-page.py")
experiment = importlib.util.module_from_spec(spec); spec.loader.exec_module(experiment)
w = experiment.w
SOURCE = Path(os.environ.get("CHIRP_TEST_SOURCE", "/opt/repeater-box/vendor/chirp"))
FIXTURE = SOURCE / "tests/images/Baofeng_5RX.img"
if FIXTURE.exists():
    sys.path.insert(0, str(SOURCE))
    from chirp import chirp_common, memmap, settings
    from chirp.drivers import baofeng_common, uv5r
    from chirp.drivers.baofeng_wp970i import BF5RXRadio

CONFIG = {"schema": 1, "model": "5RX", "devices": {
    "rx": "/dev/serial/by-id/usb-AIOC_All-In-One-Cable_test_rx-if04",
    "tx": "/dev/serial/by-id/usb-AIOC_All-In-One-Cable_test_tx-if04"}}
PAIR_ID, SOURCE_ID, RX_ID = "a"*32, "b"*32, "c"*32


class TransferPolicyTests(unittest.TestCase):
    def test_names_are_256_channels_and_globals_stay_32(self):
        self.assertEqual(w.physical_write_groups([0x10, 0xE20, 0xE30, 0x1010, 0x10B0, 0x17F0]),
                         [(0, 0x20), (0xE20, 0x20), (0x1000, 0x100), (0x1700, 0x100)])
        self.assertEqual(w.physical_write_groups([0x7F0, 0xE50]), [(0x7E0, 0x20), (0xE40, 0x20)])
        for block in (-16, True, 1, 0x800, 0xE00, 0xE60, 0x1800, 0x1FC0):
            with self.subTest(block=block), self.assertRaises(w.ProgrammingError): w.physical_write_groups([block])

    def test_cli_has_no_arbitrary_device_offset_path_or_role_arguments(self):
        for args in (("--role", "rx"), ("--offset", "0x1000"), ("--path", "/tmp/image"), ("--device", "port")):
            with self.subTest(args=args), mock.patch("sys.stderr"), self.assertRaises(SystemExit):
                experiment.main(args)
        with mock.patch("sys.stderr"), self.assertRaises(SystemExit):
            experiment.main(["--check", "--pair-id", PAIR_ID])

    def test_maintenance_lock_rejects_symlinks_permissions_and_concurrent_owner(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/"lock"; path.touch(mode=0o600)
            with experiment.maintenance_lock(path, os.getuid()):
                with self.assertRaises(BlockingIOError):
                    with experiment.maintenance_lock(path, os.getuid()): pass
            path.chmod(0o644)
            with self.assertRaises(w.ProgrammingError):
                with experiment.maintenance_lock(path, os.getuid()): pass
            alias = Path(temp)/"alias"; alias.symlink_to(path)
            with self.assertRaises(OSError):
                with experiment.maintenance_lock(alias, os.getuid()): pass


@unittest.skipUnless(FIXTURE.exists(), "Set CHIRP_TEST_SOURCE to the pinned official source")
class NamePageTests(unittest.TestCase):
    def radio(self, raw=None):
        if raw is None: return BF5RXRadio(str(FIXTURE))
        radio = BF5RXRadio(None); radio._mmap = memmap.MemoryMapBytes(bytes(raw)); radio.process_mmap(); return radio

    def images(self):
        original = self.radio(); w.apply_plan(original, "tx", chirp_common, settings, catalog=CATALOG)
        memory = chirp_common.Memory(); memory.number, memory.empty = 11, True; original.set_memory(memory)
        raw = bytearray(w.packed(original)); raw[0x10BD:0x10C0] = experiment.WANTED
        original = self.radio(raw); candidate = self.radio(raw)
        w.apply_editor(candidate, [w.pair_memory("rpt1", "tx", CATALOG)], {}, chirp_common, settings)
        actual = bytearray(w.packed(candidate)); actual[0x10BD:0x10C0] = experiment.CURRENT
        actual[0xE4D] ^= 1; actual[0xE76] ^= 3
        return original, candidate, bytes(actual)

    def setup_experiment(self, temp):
        test = self
        state, backups = Path(temp)/"state", Path(temp)/"backups"; state.mkdir(); backups.mkdir(mode=0o750)
        original, candidate, actual = self.images()
        class Hardware:
            current = actual
            reads = 0
            uploads = 0
            fail = None
            after_download = None
            def select_model(self, model): test.assertEqual(model, "5RX")
            def load(self, path): return BF5RXRadio(str(path))
            def prepare_editor(self, radio, rows, values): return w.apply_editor(radio, rows, values, chirp_common, settings)
            def download(self, device, progress):
                test.assertEqual(device, CONFIG["devices"]["tx"]); self.reads += 1
                radio = test.radio(self.current)
                if self.after_download: self.after_download()
                return radio
            def release(self, radio): pass
            def upload(self, device, before, proposed, blocks, progress, before_write, editor=False):
                test.assertTrue(editor); test.assertEqual(blocks, [0x10B0])
                test.assertEqual(w.physical_write_groups(blocks), [(0x1000, 0x100)])
                metadata = instance.private["_pair"][experiment.EXPERIMENT_KEY]
                folder = backups/metadata["backup_id"]
                test.assertIn(folder.name, instance.private["_pair"]["protected_backups"])
                test.assertTrue((folder/"original.img").is_file()); test.assertTrue((folder/"candidate.img").is_file())
                durable = json.loads((backups/"state.json").read_text())
                test.assertEqual(durable["_pair"][experiment.EXPERIMENT_KEY]["phase"], "prepared")
                test.assertEqual(durable["tx"]["program_id"], SOURCE_ID)
                test.assertEqual(durable["tx"]["program_kind"], "pair")
                test.assertTrue(durable["_pair"]["verification_required"])
                before_write(); self.uploads += 1
                if self.fail == "upload": raise IOError("synthetic upload failure")
                self.current = w.packed(proposed)
            def settle_and_verify(self, device, radio, progress):
                if self.fail == "readback": raise IOError("synthetic readback failure")
                raw = bytearray(self.current)
                if self.fail == "calibration": raw[0x1F80] ^= 1
                return test.radio(raw)
        hardware = Hardware()
        instance = w.Worker(hardware, state_dir=state, backup_dir=backups, owner_uid=os.getuid(),
                            service=lambda *args: False)
        instance.catalog = CATALOG
        folder = instance.backup(original, SOURCE_ID, "original", "tx", CONFIG["devices"]["tx"])
        candidate.save_mmap(str(folder/"candidate.img")); (folder/"candidate.img").chmod(0o640)
        original.save_mmap(str(folder/"upload-base.img")); (folder/"upload-base.img").chmod(0o640)
        identity = w.image_identity(original)
        instance.private = {
            "rx": {**identity, "device": CONFIG["devices"]["rx"], "program_kind": "pair", "program_id": RX_ID,
                   "verified": True, "verification_required": False},
            "tx": {**identity, "device": CONFIG["devices"]["tx"], "program_kind": "pair", "program_id": SOURCE_ID,
                   "verified": False, "verification_required": True, "program_model": "5RX"},
            "_pair": {"request_id": PAIR_ID, "pair_id": "rpt1", "catalog_digest": DIGEST, "phase": "failed", "stored_verified": False,
                      "verification_required": True, "devices": CONFIG["devices"].copy(),
                      "backup_ids": {"rx": RX_ID, "tx": SOURCE_ID}, "protected_backups": [RX_ID, SOURCE_ID],
                      "roles": {"rx": {"written": True, "verified": True}, "tx": {"written": True, "verified": False}}}}
        instance.publish()
        return experiment.Experiment(instance, copy.deepcopy(CONFIG)), instance, hardware, state, backups

    def run_apply(self, job): return job.run(True, PAIR_ID, SOURCE_ID, output=lambda *args: None)

    def test_check_is_read_only_and_prints_reviewable_fixed_plan(self):
        with tempfile.TemporaryDirectory() as temp:
            job, instance, hardware, state, backups = self.setup_experiment(temp)
            files = {str(path.relative_to(temp)): path.read_bytes() for path in Path(temp).rglob("*") if path.is_file()}
            before = copy.deepcopy(instance.private); lines = []
            result = job.run(output=lines.append)
            self.assertEqual(result, {"pair_id": PAIR_ID, "source_id": SOURCE_ID, "applied": False})
            self.assertEqual(hardware.uploads, 0); self.assertEqual(instance.private, before)
            self.assertEqual(files, {str(path.relative_to(temp)): path.read_bytes() for path in Path(temp).rglob("*") if path.is_file()})
            self.assertIn("Pending pair request: "+PAIR_ID, lines)
            self.assertTrue(any("FFFFFF -> 50FAFE" in line for line in lines))

    def test_apply_protects_durable_backup_and_changes_only_three_bytes_preserving_runtime(self):
        with tempfile.TemporaryDirectory() as temp:
            job, instance, hardware, state, backups = self.setup_experiment(temp)
            before = hardware.current; old_tx = copy.deepcopy(instance.private["tx"])
            result = self.run_apply(job)
            self.assertTrue(result["verified"]); self.assertEqual(hardware.uploads, 1)
            self.assertEqual({i for i, (old, new) in enumerate(zip(before, hardware.current)) if old != new}, set(experiment.OFFSETS))
            self.assertEqual(hardware.current[0xE4D], before[0xE4D]); self.assertEqual(hardware.current[0xE76], before[0xE76])
            self.assertEqual(instance.private["tx"], old_tx)
            pair = instance.private["_pair"]
            self.assertTrue(pair["verification_required"]); self.assertFalse(pair["stored_verified"])
            self.assertFalse(pair["roles"]["tx"]["verified"])
            self.assertEqual(pair[experiment.EXPERIMENT_KEY]["phase"], "verified")
            self.assertTrue((backups/result["backup_id"]/"observed.img").is_file())

    def test_stale_missing_or_arbitrary_tokens_never_read_or_write(self):
        for pair_id, source_id in ((None, None), ("../path", SOURCE_ID), (PAIR_ID, "f"*32), ("d"*32, SOURCE_ID)):
            with self.subTest(pair_id=pair_id, source_id=source_id), tempfile.TemporaryDirectory() as temp:
                job, instance, hardware, state, backups = self.setup_experiment(temp)
                with self.assertRaises(w.ProgrammingError): job.run(True, pair_id, source_id)
                self.assertEqual((hardware.reads, hardware.uploads), (0, 0))

    def test_pending_pair_binding_profile_and_role_guards_stop_before_hardware(self):
        mutations = [lambda s: s["_pair"].update(verification_required=False),
                     lambda s: s["_pair"].update(stored_verified=True),
                     lambda s: s["tx"].update(program_kind="editor"),
                     lambda s: s["tx"].update(program_model="UV5R"),
                     lambda s: s["tx"].update(device="wrong"),
                     lambda s: s["_pair"]["backup_ids"].update(tx="f"*32),
                     lambda s: s["rx"].update(verified=False),
                     lambda s: s["_pair"]["roles"]["tx"].update(verified=True),
                     lambda s: s.update(programmer={"verification_required": True})]
        for mutate in mutations:
            with self.subTest(mutate=mutate), tempfile.TemporaryDirectory() as temp:
                job, instance, hardware, state, backups = self.setup_experiment(temp); mutate(instance.private)
                with self.assertRaises(w.ProgrammingError): job.run()
                self.assertEqual((hardware.reads, hardware.uploads), (0, 0))

    def test_active_or_autostart_services_stop_before_hardware(self):
        for action in ("is-active", "is-enabled"):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as temp:
                job, instance, hardware, state, backups = self.setup_experiment(temp)
                instance.service = lambda operation, name: operation == action
                with self.assertRaises(w.ProgrammingError): job.run()
                self.assertEqual(hardware.reads, 0)

    def test_source_hash_candidate_recompute_and_unexpected_files_are_strict(self):
        for kind in ("original-hash", "candidate", "upload-base", "unexpected"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temp:
                job, instance, hardware, state, backups = self.setup_experiment(temp); folder = backups/SOURCE_ID
                if kind == "unexpected": (folder/"unknown.dat").write_bytes(b"x")
                else:
                    name = "original.img" if kind == "original-hash" else kind+".img"
                    path = folder/name; radio = hardware.load(path); raw = bytearray(w.packed(radio)); raw[0x1F80] ^= 1
                    self.radio(raw).save_mmap(str(path)); path.chmod(0o640)
                with self.assertRaises(w.ProgrammingError): job.run()
                self.assertEqual(hardware.reads, 0)

    def test_symlink_sources_and_world_writable_files_are_rejected(self):
        for kind in ("symlink", "permissions"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temp:
                job, instance, hardware, state, backups = self.setup_experiment(temp); path = backups/SOURCE_ID/"candidate.img"
                if kind == "symlink":
                    saved = Path(temp)/"outside.img"; path.rename(saved); path.symlink_to(saved)
                else: path.chmod(0o666)
                with self.assertRaises((OSError, w.ProgrammingError)): job.run()
                self.assertEqual(hardware.reads, 0)

    def test_extra_fresh_drift_wrong_record_or_wrong_three_values_never_writes(self):
        for offset in (0x1F80, 0x10A0, 0x10BD, 0xB0):
            with self.subTest(offset=offset), tempfile.TemporaryDirectory() as temp:
                job, instance, hardware, state, backups = self.setup_experiment(temp)
                raw = bytearray(hardware.current); raw[offset] ^= 1; hardware.current = bytes(raw)
                before_files = set(backups.iterdir())
                with self.assertRaises(w.ProgrammingError): self.run_apply(job)
                self.assertEqual(hardware.uploads, 0); self.assertEqual(set(backups.iterdir()), before_files)

    def test_source_change_during_read_stops_before_backup_or_upload(self):
        with tempfile.TemporaryDirectory() as temp:
            job, instance, hardware, state, backups = self.setup_experiment(temp)
            hardware.after_download = lambda: instance.private["_pair"].update(request_id="f"*32)
            with self.assertRaises(w.ProgrammingError): self.run_apply(job)
            self.assertEqual(hardware.uploads, 0)

    def test_durability_failure_cannot_enter_upload(self):
        with tempfile.TemporaryDirectory() as temp:
            job, instance, hardware, state, backups = self.setup_experiment(temp)
            with mock.patch.object(instance, "publish", side_effect=OSError("disk full")), self.assertRaises(OSError): self.run_apply(job)
            self.assertEqual(hardware.uploads, 0)
            durable = json.loads((backups/"state.json").read_text())
            self.assertTrue(durable["_pair"]["verification_required"])
            self.assertEqual(durable["tx"]["program_id"], SOURCE_ID)

    def test_upload_readback_or_calibration_failure_keeps_original_interlocks_and_one_attempt(self):
        for failure in ("upload", "readback", "calibration"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temp:
                job, instance, hardware, state, backups = self.setup_experiment(temp); hardware.fail = failure
                with self.assertRaises((OSError, w.ProgrammingError)): self.run_apply(job)
                self.assertTrue(instance.private["_pair"]["verification_required"])
                self.assertTrue(instance.private["tx"]["verification_required"])
                self.assertEqual(instance.private["tx"]["program_id"], SOURCE_ID)
                self.assertEqual(instance.private["_pair"][experiment.EXPERIMENT_KEY]["phase"], "failed")
                reads, uploads = hardware.reads, hardware.uploads
                with self.assertRaises(w.ProgrammingError): self.run_apply(job)
                self.assertEqual((hardware.reads, hardware.uploads), (reads, uploads))

    def test_retention_keeps_five_sets_and_protects_source_and_experiment(self):
        with tempfile.TemporaryDirectory() as temp:
            job, instance, hardware, state, backups = self.setup_experiment(temp)
            original = self.images()[0]
            for number in range(1, 9): instance.backup(original, f'{number:032x}', "original", "tx", CONFIG["devices"]["tx"])
            result = self.run_apply(job)
            self.assertEqual(len([folder for folder in backups.iterdir() if folder.is_dir()]), 5)
            self.assertTrue((backups/SOURCE_ID).is_dir()); self.assertTrue((backups/result["backup_id"]).is_dir())

    def test_real_upload_256_byte_groups_preserve_payload_and_delay_callbacks_for_each_profile(self):
        for model in ("5RX", "UV5R"):
            with self.subTest(model=model):
                if model == "5RX": original = self.images()[1]
                else:
                    raw = w.packed(self.images()[1]); native = raw[0x2000:0x2008]+raw[:0x1800]+raw[0x1EC0:0x2000]
                    original = uv5r.BaofengUV5RGeneric(None); original._mmap = memmap.MemoryMapBytes(native); original.process_mmap()
                driver = type(original); raw = w.packed(original); base = w.image_base(original)
                proposed = bytearray(raw); proposed[base+0x1010:base+0x1017] = b"NEWNAME"
                candidate = driver(None); candidate._mmap = memmap.MemoryMapBytes(bytes(proposed)); candidate.process_mmap()
                frames, callbacks = [], []
                class Port:
                    is_open = True; dtr = True
                    def write(self, frame): frames.append(frame); return len(frame)
                    def close(self): self.is_open = False
                hardware = w.Hardware.__new__(w.Hardware); hardware.open = lambda device: Port()
                hardware.catalog = CATALOG
                hardware.common = types.SimpleNamespace(_make_frame=baofeng_common._make_frame, _rawrecv=lambda *args: b"\x06")
                protocol = types.SimpleNamespace(_ident_radio=lambda r: raw[:8] if base else raw[0x2000:0x2008],
                    _get_aux_data_from_radio=lambda r: (raw[0x1838:0x1846], raw[0x1848:0x18A8],raw[0x18B8:0x18C8],raw[0x18D8:0x1908],True)
                    if base else (raw[0x1EF0:0x1EFE],raw[0x1F00:0x1F60],raw[0x1F70:0x1F80],raw[0x1F90:0x1FC0],True))
                if base: hardware.uv5r = protocol
                else:
                    hardware.common._ident_radio, hardware.common._get_aux_data_from_radio = protocol._ident_radio, protocol._get_aux_data_from_radio
                candidate.pipe = Port()
                with mock.patch.object(w.time, "sleep", lambda _: None):
                    hardware.upload("device", original, candidate, [0x1010], lambda: callbacks.append(len(frames)), lambda: None, editor=True)
                self.assertEqual(len(frames), 16)
                self.assertEqual([struct.unpack(">BHB", frame[:4])[1] for frame in frames], list(range(0x1000, 0x1100, 16)))
                for frame in frames:
                    address = struct.unpack(">BHB", frame[:4])[1]
                    self.assertEqual(frame[4:], bytes(proposed)[base+address:base+address+16])
                self.assertTrue(all(count in (0, 16) for count in callbacks))
                self.assertEqual(frames[11][4+13:4+16], experiment.WANTED)


if __name__ == "__main__": unittest.main()
