import importlib.util
import json
import os
from pathlib import Path
import stat
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
sys.path.insert(0, str(ROOT / "dashboard"))
from controls import write_json
from radio_programming import RadioProgramming, valid_config, valid_request, LEGACY_ROUTINE_SUCCESS


def load_script(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), ROOT / "scripts" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


worker_module = load_script("program-radio")
configure = load_script("configure-radios")
CONFIG = {"schema": 1, "model": "5RX", "devices": {
    "rx": "/dev/serial/by-id/usb-AIOC_All-In-One-Cable_test_rx-if04",
    "tx": "/dev/serial/by-id/usb-AIOC_All-In-One-Cable_test_tx-if04"}}


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.config = self.folder / "config.json"
        write_json(self.config, CONFIG)
        self.catalog = self.folder / "channel-pairs.json"
        write_json(self.catalog, CATALOG)
        self.active_worker = False
        def run(args):
            if args[-1] == "repeater-radio-programming.path":
                return "active"
            return "active" if self.active_worker else "inactive"
        self.controller = RadioProgramming(self.folder, supported=True, runner=run,
                                          config_file=self.config, catalog_file=self.catalog, catalog_owner_uid=os.getuid())

    def ready(self, role="rx"):
        write_json(self.folder / self.controller.PUBLIC_STATE,
                   {role: {"inspected": True, "compatible": True, "device": "PRIVATE"}})

    def test_preview_never_queues(self):
        controller = RadioProgramming(self.folder, supported=False, config_file=self.config)
        self.assertTrue(controller.status()["preview"])
        with self.assertRaises(ValueError):
            controller.request("rx", "inspect")

    def test_config_and_requests_are_narrow(self):
        self.assertTrue(valid_config(CONFIG))
        for value in ({**CONFIG, "model": "UV-5R"}, {**CONFIG, "schema": True},
                      {**CONFIG, "devices": {"rx": "/dev/ttyACM0", "tx": "/dev/ttyACM1"}}):
            self.assertFalse(valid_config(value))
        request = {"request_id": "a" * 32, "role": "rx", "action": "inspect"}
        self.assertTrue(valid_request(request))
        self.assertFalse(valid_request({**request, "path": "/tmp/image"}))
        self.assertFalse(valid_request({**request, "action": "restore"}))
        self.assertEqual(valid_request(request), worker_module.valid_request(request))
        self.assertEqual(PLAN, worker_module.default_plan(CATALOG))
        self.assertFalse(hasattr(worker_module, "PLAN"))

    def test_program_requires_inspection(self):
        with self.assertRaises(ValueError):
            self.controller.request("rx", "program", catalog_digest=DIGEST)
        self.ready()
        state = self.controller.request("rx", "program", catalog_digest=DIGEST)
        self.assertTrue(state["pending"])
        self.assertEqual(state["role"], "rx")
        self.assertEqual(set(json.loads((self.folder / self.controller.REQUEST).read_text())),
                         {"request_id", "role", "action", "catalog_digest"})
        self.assertNotIn("PRIVATE", json.dumps(state))

    def test_any_unfinished_upload_blocks_inspect_and_program(self):
        write_json(self.folder / self.controller.PUBLIC_STATE,
                   {"rx": {"inspected": True, "compatible": True},
                    "tx": {"verification_required": True}})
        for action in ("inspect", "program"):
            with self.assertRaises(ValueError):
                self.controller.request("rx", action, catalog_digest=DIGEST if action == "program" else None)
        self.assertTrue(self.controller.request("tx", "verify")["pending"])

    def test_active_worker_remains_pending_after_timeout(self):
        write_json(self.folder / self.controller.REQUEST,
                   {"request_id": "a" * 32, "role": "rx", "action": "inspect"})
        os.utime(self.folder / self.controller.REQUEST, (0, 0))
        self.active_worker = True
        self.assertTrue(self.controller.status()["pending"])
        with self.assertRaises(ValueError):
            self.controller.request("tx", "inspect")

    def test_result_text_and_backup_are_bounded(self):
        request = {"request_id": "a" * 32, "role": "rx", "action": "inspect"}
        write_json(self.folder / self.controller.REQUEST, request)
        write_json(self.folder / self.controller.RESULT,
                   {**request, "success": True, "phase": "inspected", "message": "x" * 2000,
                    "backup_id": "../../private"})
        state = self.controller.status()
        self.assertFalse(state["pending"])
        self.assertEqual(len(state["message"]), 1000)
        self.assertIsNone(state["backup_id"])

    def test_legacy_helper_normal_success_hints_update_without_changing_saved_result(self):
        for (action, phase), (legacy, current) in LEGACY_ROUTINE_SUCCESS.items():
            with self.subTest(action=action):
                request = {"request_id": "a" * 32, "role": "rx", "action": action, **({"catalog_digest": DIGEST} if action == "program" else {})}
                write_json(self.folder / self.controller.REQUEST, request)
                result = {"request_id": request["request_id"], "success": True,
                          "phase": phase, "message": legacy,
                          "events": [{"time": "2026-10-28T20:00:00Z", "message": legacy}]}
                write_json(self.folder / self.controller.RESULT, result)
                state = self.controller.status()
                self.assertEqual(state["message"], current)
                self.assertEqual(state["events"][0]["message"], current)
                self.assertTrue(state["success"])
                self.assertFalse(state["pending"])
                self.assertFalse(state["verification_required"])
                self.assertEqual(json.loads((self.folder / self.controller.RESULT).read_text()), result)
                self.assertEqual(json.loads((self.folder / self.controller.REQUEST).read_text()), request)

    def test_legacy_success_adapter_preserves_failures_pending_recovery_and_unknown_messages(self):
        for (action, phase), (legacy, _) in LEGACY_ROUTINE_SUCCESS.items():
            request = {"request_id": "a" * 32, "role": "rx", "action": action, **({"catalog_digest": DIGEST} if action == "program" else {})}
            write_json(self.folder / self.controller.REQUEST, request)
            cases = ({"success": False}, {"success": None}, {"success": 1},
                     {"verification_required": True},
                     {"phase": "failed"}, {"phase": []},
                     {"message": legacy + " Extra recovery instructions."},
                     {"message": "Recovery read saved. Keep the unfinished candidate protected."})
            for overrides in cases:
                with self.subTest(action=action, overrides=overrides):
                    result = {"request_id": request["request_id"], "success": True,
                              "phase": phase, "message": legacy,
                              "verification_required": False,
                              "events": [{"time": "2026-10-28T20:00:00Z", "message": legacy}],
                              **overrides}
                    # An event with different text must also remain verbatim.
                    if "message" in overrides:
                        result["events"][0]["message"] = result["message"]
                    write_json(self.folder / self.controller.RESULT, result)
                    state = self.controller.status()
                    self.assertEqual(state["message"], result["message"])
                    self.assertEqual(state["events"][0]["message"], result["events"][0]["message"])
                    self.assertEqual(state["verification_required"], result["verification_required"])
            # Neither a successful unrelated action nor a stale result may translate it.
            other = {**request, "action": "verify"}
            write_json(self.folder / self.controller.REQUEST, other)
            write_json(self.folder / self.controller.RESULT,
                       {"request_id": request["request_id"], "success": True, "phase": phase,
                        "message": legacy})
            self.assertEqual(self.controller.status()["message"], legacy)
            write_json(self.folder / self.controller.REQUEST, {**request, "request_id": "b" * 32})
            self.assertNotEqual(self.controller.status()["message"], legacy)


class LocalConfigTests(unittest.TestCase):
    def test_config_does_not_open_or_program_devices(self):
        value = configure.build_config("5RX", "test_rx", "test_tx", validate_device=False)
        self.assertEqual(value, CONFIG)
        for serial in ("../ttyACM0", "hello world", "", "a" * 65):
            with self.assertRaises(ValueError):
                configure.device_path(serial)
        with self.assertRaises(ValueError):
            configure.build_config("5RX", "same", "same", validate_device=False)

    def test_pending_upload_prevents_rebinding(self):
        configure.check_rebinding(CONFIG, CONFIG, {"rx": {"verification_required": True}})
        new_config = configure.build_config("5RX", "other", "test_tx", validate_device=False)
        with self.assertRaises(ValueError):
            configure.check_rebinding(CONFIG, new_config, {"rx": {"verification_required": True}})

    def test_worker_reader_refuses_symlinks_and_duplicate_keys(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "request.json"
            path.write_text('{"role":"rx","role":"tx"}')
            with self.assertRaises(ValueError):
                worker_module.read_json(path)
            path.unlink()
            path.symlink_to(Path(folder) / "elsewhere")
            with self.assertRaises(OSError):
                worker_module.read_json(path)

    def test_privileged_helper_does_not_import_updatable_dashboard(self):
        source = (ROOT / "scripts" / "program-radio.py").read_text()
        self.assertNotIn("from radio_programming import", source)
        self.assertNotIn('sys.path.insert(0, str(APP_ROOT / "dashboard"))', source)


SOURCE = Path(os.environ.get("CHIRP_TEST_SOURCE", "/opt/repeater-box/vendor/chirp"))
FIXTURE = SOURCE / "tests" / "images" / "Baofeng_5RX.img"
if FIXTURE.exists():
    sys.path.insert(0, str(SOURCE))
    from chirp import chirp_common, settings, memmap
    from chirp.drivers.baofeng_wp970i import BF5RXRadio
    from chirp.drivers import baofeng_common


@unittest.skipUnless(FIXTURE.exists(), "Set CHIRP_TEST_SOURCE to the pinned official CHIRP source for fixture tests")
class PinnedDriverTests(unittest.TestCase):
    def radio(self, raw=None):
        if raw is None:
            return BF5RXRadio(str(FIXTURE))
        radio = BF5RXRadio(None)
        radio._mmap = memmap.MemoryMapBytes(raw)
        radio.process_mmap()
        return radio

    def test_fixed_plan_only_changes_approved_bytes(self):
        for role, frequency in (("rx", 145300000), ("tx", 433500000)):
            with self.subTest(role=role):
                radio = self.radio()
                original = worker_module.packed(radio)
                blocks = worker_module.apply_plan(radio, role, chirp_common, settings, catalog=CATALOG)
                candidate = worker_module.packed(radio)
                self.assertEqual(blocks, [0x10, 0xE20, 0xE30, 0xE40, 0x1010])
                changed = {index for index in range(len(original)) if original[index] != candidate[index]}
                self.assertLessEqual(changed, worker_module.ALLOWED_OFFSETS)
                for offset, mask in worker_module.PARTIAL_MASKS.items():
                    self.assertEqual((original[offset] ^ candidate[offset]) & ~mask, 0)
                self.assertEqual(original[0x1E80:], candidate[0x1E80:])
                self.assertEqual(radio.get_memory(1).freq, frequency)
                self.assertEqual(str(radio.get_memory(1).power), "High")
                self.assertEqual(radio.get_memory(1).duplex, "off" if role == "rx" else "")

    def test_unreviewed_identity_is_readable_but_cannot_be_changed(self):
        raw = bytearray(worker_module.packed(self.radio()))
        raw[0x2002] ^= 1
        radio = self.radio(bytes(raw))
        self.assertFalse(worker_module.image_identity(radio)["compatible"])
        with self.assertRaises(worker_module.ProgrammingError):
            worker_module.apply_plan(radio, "rx", chirp_common, settings, catalog=CATALOG)

    def test_readback_detects_unrelated_bits_and_channels(self):
        original = self.radio()
        candidate = self.radio()
        worker_module.apply_plan(candidate, "rx", chirp_common, settings, catalog=CATALOG)
        worker_module.verify_images(original, candidate, self.radio(worker_module.packed(candidate)))
        for index in (0xE4A, 0xE76, 0x100, 0x1F80):
            raw = bytearray(worker_module.packed(candidate))
            raw[index] ^= 1 if index != 0xE76 else 0x80
            with self.subTest(index=index), self.assertRaises(worker_module.ProgrammingError):
                worker_module.verify_images(original, candidate, self.radio(bytes(raw)))

    def upload_hardware(self, original, candidate):
        raw = worker_module.packed(original)
        events, writes = [], []
        class Port:
            def __init__(self, label):
                self.label, self.is_open, self.dtr = label, True, True
            def close(self):
                events.append(("close", self.label)); self.is_open = False
            def write(self, frame):
                events.append(("write", self.label)); writes.append(frame)
                return len(frame)
        old, fresh = Port("download"), Port("upload")
        candidate.pipe = old
        common = types.SimpleNamespace(
            _ident_radio=lambda radio: events.append(("identify", radio.pipe.label)) or raw[0x2000:0x2008],
            _get_aux_data_from_radio=lambda radio: events.append(("aux", radio.pipe.label)) or
                (raw[0x1EF0:0x1EFE], raw[0x1F00:0x1F60], raw[0x1F70:0x1F80], raw[0x1F90:0x1FC0], True),
            _make_frame=baofeng_common._make_frame,
            _rawrecv=lambda radio, size: events.append(("ack", radio.pipe.label)) or b"\x06")
        hardware = worker_module.Hardware.__new__(worker_module.Hardware)
        hardware.catalog = CATALOG
        hardware.common = common
        hardware.open = lambda device: events.append(("open", device)) or fresh
        return hardware, old, fresh, events, writes

    def test_upload_reidentifies_and_preserves_complete_operating_pages(self):
        original, candidate = self.radio(), self.radio()
        blocks = worker_module.apply_plan(candidate, "tx", chirp_common, settings, catalog=CATALOG)
        hardware, old, fresh, events, writes = self.upload_hardware(original, candidate)
        with mock.patch.object(worker_module.time, "sleep", lambda seconds: events.append(("quiet", seconds))):
            hardware.upload(CONFIG["devices"]["tx"], original, candidate, blocks, lambda: None,
                            lambda: events.append(("before-write",)))
        self.assertEqual(events[:5], [("close", "download"), ("quiet", 5),
                                     ("open", CONFIG["devices"]["tx"]),
                                     ("identify", "upload"), ("aux", "upload")])
        self.assertEqual(events.count(("before-write",)), 1)
        self.assertLess(events.index(("aux", "upload")), events.index(("before-write",)))
        self.assertLess(events.index(("before-write",)), events.index(("write", "upload")))
        physical = [0x0000, 0x0010, 0x0E20, 0x0E30, 0x0E40, 0x0E50, *range(0x1000, 0x1100, 16)]
        self.assertEqual([struct.unpack(">BHB", frame[:4])[1] for frame in writes], physical)
        proposed = worker_module.packed(candidate)
        self.assertEqual([frame[4:] for frame in writes], [proposed[address:address + 16] for address in physical])
        self.assertTrue(all(struct.unpack(">BHB", frame[:4])[::2] == (ord("X"), 16) for frame in writes))
        self.assertTrue(all(len(frame) == 20 for frame in writes))
        self.assertFalse(any(struct.unpack(">BHB", frame[:4])[1] >= 0x1E80 for frame in writes))
        self.assertFalse(old.is_open)
        self.assertFalse(old.dtr)
        self.assertIs(candidate.pipe, fresh)

    def test_page_buffer_requires_prefix_and_preserves_unmodified_companion_bytes(self):
        # Model the observed firmware as accepting complete 32-byte pages:
        # the lower frame starts its buffer and the upper frame commits it.
        # This is a regression model for the hardware-derived requirement,
        # not a claim that the firmware's implementation has been documented.
        cases = ((0xE28, 0x01, [0xE20, 0xE30]),
                 (0xE38, 0x01, [0xE20, 0xE30]),
                 (0xE4A, 0x80, [0xE40, 0xE50]),
                 (0x1010, 0x01, list(range(0x1000, 0x1100, 16))))
        for changed, mask, physical in cases:
            with self.subTest(changed=hex(changed)):
                original = self.radio()
                before = worker_module.packed(original)
                proposed = bytearray(before)
                proposed[changed] ^= mask
                candidate = self.radio(bytes(proposed))
                hardware, _, fresh, events, writes = self.upload_hardware(original, candidate)
                eeprom = bytearray(before)
                pending = None

                def page_write(frame):
                    nonlocal pending
                    command, address, size = struct.unpack(">BHB", frame[:4])
                    self.assertEqual((command, size), (ord("X"), 16))
                    events.append(("frame", address))
                    writes.append(frame)
                    if address % 32 == 0:
                        pending = (address, bytearray(32))
                        pending[1][:16] = frame[4:]
                    else:
                        self.assertIsNotNone(pending, "An upper-half write requires its page prefix")
                        self.assertEqual(address, pending[0] + 16)
                        pending[1][16:] = frame[4:]
                        eeprom[pending[0]:pending[0] + 32] = pending[1]
                        pending = None
                    return len(frame)

                fresh.write = page_write
                progress_calls = []
                def progress(*args, **kwargs):
                    progress_calls.append(kwargs)
                    if "current" in kwargs:
                        events.append(("progress", kwargs["current"]))
                progress.radio_metrics = True
                with mock.patch.object(worker_module.time, "sleep", lambda seconds: None):
                    hardware.upload("device", original, candidate, [changed - changed % 16],
                                    progress, lambda: None)
                self.assertIsNone(pending)
                self.assertEqual(eeprom, proposed)
                self.assertEqual([struct.unpack(">BHB", frame[:4])[1] for frame in writes], physical)
                untouched = next(address for address in physical if not address <= changed < address + 16)
                companion = next(frame[4:] for frame in writes if struct.unpack(">BHB", frame[:4])[1] == untouched)
                self.assertEqual(companion, before[untouched:untouched + 16])
                self.assertEqual([call for call in progress_calls if "current" in call],
                                 [{"current": len(physical), "total": len(physical), "unit": "blocks"}])
                self.assertEqual([event for event in events if event[0] in ("frame", "progress")],
                                 [("frame", address) for address in physical] + [("progress", len(physical))])

    def test_page_companions_do_not_relax_logical_protected_byte_checks(self):
        for offset in (0x0000, 0x0E30, 0x0E50, 0x1000, 0x1F80):
            with self.subTest(offset=hex(offset)):
                original = self.radio()
                proposed = bytearray(worker_module.packed(original))
                proposed[0x1010] ^= 1
                proposed[offset] ^= 1
                candidate = self.radio(bytes(proposed))
                hardware, old, _, events, writes = self.upload_hardware(original, candidate)
                blocks = sorted({0x1010, offset - offset % 16})
                with self.assertRaises(worker_module.ProgrammingError):
                    hardware.upload("device", original, candidate, blocks, lambda: None, lambda: None)
                self.assertTrue(old.is_open)
                self.assertEqual(events, [])
                self.assertEqual(writes, [])

    def test_reported_three_byte_update_includes_settings_and_name_companions(self):
        before = bytearray(worker_module.packed(self.radio()))
        for address, encoded in (
                (0xE20, "03000000000005000103000000000000"),
                (0xE30, "03000000050101000100000000030102"),
                (0xE40, "000000010500010001009f0001018000"),
                (0x1010, "424f582d525858ffffffffffffffffff")):
            before[address:address + 16] = bytes.fromhex(encoded)
        proposed = bytearray(before)
        proposed[0xE28] = 0
        proposed[0xE4A] = 0x1F
        proposed[0x1016] = 0xFF
        original, candidate = self.radio(bytes(before)), self.radio(bytes(proposed))
        hardware, _, _, _, writes = self.upload_hardware(original, candidate)
        with mock.patch.object(worker_module.time, "sleep", lambda seconds: None):
            hardware.upload("device", original, candidate, [0xE20, 0xE40, 0x1010],
                            lambda: None, lambda: None)
        frames = {struct.unpack(">BHB", frame[:4])[1]: frame[4:] for frame in writes}
        self.assertEqual(list(frames), [0xE20, 0xE30, 0xE40, 0xE50, *range(0x1000, 0x1100, 16)])
        self.assertEqual(frames[0xE30], bytes.fromhex("03000000050101000100000000030102"))
        self.assertEqual(frames[0x1000], before[0x1000:0x1010])
        self.assertEqual(frames[0xE50], before[0xE50:0xE60])
        self.assertEqual(frames[0x1010][:7], b"BOX-RX\xff")
        for address, payload in frames.items():
            self.assertEqual(payload, proposed[address:address + 16])

    def test_invalid_upload_is_rejected_before_closing_or_opening_a_serial_session(self):
        original, candidate = self.radio(), self.radio()
        worker_module.apply_plan(candidate, "rx", chirp_common, settings, catalog=CATALOG)
        hardware, old, _, events, writes = self.upload_hardware(original, candidate)
        with self.assertRaises(worker_module.ProgrammingError):
            hardware.upload("device", original, candidate, [0x1F80], lambda: None, lambda: None)
        self.assertTrue(old.is_open)
        self.assertEqual(events, [])
        self.assertEqual(writes, [])

    def test_identity_or_protected_aux_mismatch_in_fresh_upload_session_never_writes(self):
        for mismatch in ("identity", 0, 1, 2, 3):
            with self.subTest(mismatch=mismatch):
                original, candidate = self.radio(), self.radio()
                blocks = worker_module.apply_plan(candidate, "rx", chirp_common, settings, catalog=CATALOG)
                hardware, old, fresh, events, writes = self.upload_hardware(original, candidate)
                if mismatch == "identity":
                    hardware.common._ident_radio = lambda radio: b"different"
                else:
                    getter = hardware.common._get_aux_data_from_radio
                    def changed_aux(radio, getter=getter, index=mismatch):
                        values = list(getter(radio))
                        values[index] = bytes([values[index][0] ^ 1]) + values[index][1:]
                        return tuple(values)
                    hardware.common._get_aux_data_from_radio = changed_aux
                before_write = mock.Mock()
                with mock.patch.object(worker_module.time, "sleep", lambda seconds: None), \
                        self.assertRaisesRegex(worker_module.ProgrammingError, "changed after its fresh backup"):
                    hardware.upload("device", original, candidate, blocks, lambda: None, before_write)
                before_write.assert_not_called()
                self.assertEqual(writes, [])
                self.assertFalse(old.is_open)
                self.assertFalse(fresh.is_open)
                self.assertIsNone(candidate.pipe)

    def test_short_serial_payload_write_is_failed_after_marking_upload_pending(self):
        original, candidate = self.radio(), self.radio()
        blocks = worker_module.apply_plan(candidate, "rx", chirp_common, settings, catalog=CATALOG)
        hardware, _, fresh, events, writes = self.upload_hardware(original, candidate)
        def short_write(frame):
            writes.append(frame)
            return len(frame) - 1
        fresh.write = short_write
        before_write = mock.Mock()
        with mock.patch.object(worker_module.time, "sleep", lambda seconds: None), \
                self.assertRaisesRegex(worker_module.ProgrammingError, "serial upload write was incomplete"):
            hardware.upload("device", original, candidate, blocks, lambda: None, before_write)
        before_write.assert_called_once_with()
        self.assertEqual(len(writes), 1)
        self.assertNotIn(("ack", "upload"), events)
        self.assertFalse(fresh.is_open)
        self.assertIsNone(candidate.pipe)

    def test_unchanged_upload_needs_no_serial_session_or_write(self):
        original, candidate = self.radio(), self.radio()
        hardware = worker_module.Hardware.__new__(worker_module.Hardware)
        hardware.catalog = CATALOG
        hardware.upload("device", original, candidate, [], lambda: None, mock.Mock())

    def test_existing_session_readback_uses_safe_tail_blocks(self):
        radio = self.radio()
        raw = worker_module.packed(radio)
        reads = []
        hardware = worker_module.Hardware.__new__(worker_module.Hardware)
        hardware.catalog = CATALOG
        hardware.driver, hardware.memmap = BF5RXRadio, memmap
        hardware.common = types.SimpleNamespace(_read_block=lambda _, address, size, first:
                                               reads.append((address, size, first)) or raw[address:address + size])
        self.assertEqual(worker_module.packed(hardware.readback(radio, lambda: None)), raw)
        self.assertEqual(reads[-4:], [(0x1FC0, 16, False), (0x1FD0, 16, False),
                                     (0x1FE0, 16, False), (0x1FF0, 16, False)])

    def test_real_protocol_separates_download_upload_and_verification_sessions(self):
        test = self
        eeprom = bytearray(worker_module.packed(self.radio())[:0x2000])
        events, ports = [], []
        class ClonePort:
            def __init__(self):
                self.data = eeprom
                self.queue = bytearray()
                self.stage = "new"
                self.first_read = True
                self.is_open = True
                self.written = []
                self.dtr = True
            def read(self, amount):
                value = bytes(self.queue[:amount]); del self.queue[:amount]
                return value
            def write(self, data):
                if data == BF5RXRadio._magic[0]:
                    if self.stage != "new":
                        raise RuntimeError("Second identification on the same clone connection")
                    events.append(("identify", ports.index(self)))
                    self.stage = "ident"; self.queue.extend(b"\x06")
                elif data == b"\x02" and self.stage == "ident":
                    self.queue.extend(worker_module.KNOWN_IDENT); self.stage = "ident-ack"
                elif data == b"\x06":
                    if self.stage == "ident-ack":
                        self.queue.extend(b"\x06"); self.stage = "clone"
                elif self.stage == "clone" and data[:1] == b"S":
                    _, address, size = struct.unpack(">BHB", data)
                    if not self.first_read:
                        self.queue.extend(b"\x06")
                    self.first_read = False
                    self.queue.extend(struct.pack(">BHB", ord("X"), address, size))
                    self.queue.extend(self.data[address:address + size])
                elif self.stage == "clone" and data[:1] == b"X":
                    _, address, size = struct.unpack(">BHB", data[:4])
                    if address not in {0x0000, 0x0010, 0x0E20, 0x0E30, 0x0E40, 0x0E50, *range(0x1000, 0x1100, 16)} or size != 16:
                        raise RuntimeError("Attempted protected block write")
                    events.append(("write", ports.index(self), address))
                    self.written.append(address)
                    self.data[address:address + size] = data[4:]
                    self.queue.extend(b"\x06")
                else:
                    raise RuntimeError("Unexpected clone protocol command")
                return len(data)
            def close(self):
                events.append(("close", ports.index(self)))
                self.is_open = False
        hardware = worker_module.Hardware.__new__(worker_module.Hardware)
        hardware.catalog = CATALOG
        hardware.driver, hardware.common = BF5RXRadio, baofeng_common
        hardware.chirp_common, hardware.settings, hardware.memmap = chirp_common, settings, memmap
        opens = []
        def open_port(device):
            opens.append(device)
            port = ClonePort()
            ports.append(port)
            events.append(("open", len(ports) - 1))
            return port
        hardware.open = open_port
        with mock.patch.object(worker_module.time, "sleep", lambda seconds: events.append(("quiet", seconds))):
            radio = hardware.download(CONFIG["devices"]["rx"], lambda: None)
            original = self.radio(worker_module.packed(radio))
            blocks = hardware.prepare(radio, "rx")
            hardware.upload(CONFIG["devices"]["rx"], original, radio, blocks, lambda: None, lambda: None)
            observed = hardware.settle_and_verify(CONFIG["devices"]["rx"], radio, lambda *args, **kwargs: None)
            worker_module.verify_images(original, radio, observed)
        self.assertEqual(len(opens), 3)
        self.assertEqual([port.written for port in ports],
                         [[], [0x0000, 0x0010, 0x0E20, 0x0E30, 0x0E40, 0x0E50, *range(0x1000, 0x1100, 16)], []])
        self.assertTrue(all(not port.is_open and not port.dtr for port in ports))
        self.assertEqual([event for event in events if event[0] == "identify"],
                         [("identify", 0), ("identify", 1), ("identify", 2)])
        self.assertLess(events.index(("close", 0)), events.index(("open", 1)))
        self.assertLess(events.index(("close", 1)), events.index(("open", 2)))
        self.assertEqual(events.count(("quiet", 5)), 2)
        self.assertIsNone(radio.pipe)
        self.assertIsNone(observed.pipe)

    def test_serial_pin_order_is_safe_before_open(self):
        events = []
        class Port:
            is_open = False
            def __setattr__(self, name, value):
                events.append((name, value)); object.__setattr__(self, name, value)
            def open(self):
                events.append(("open", (self.dtr, self.rts))); self.is_open = True
            def fileno(self):
                return 123
            def close(self):
                events.append(("close", (self.dtr, self.rts))); self.is_open = False
        port = Port()
        hardware = worker_module.Hardware.__new__(worker_module.Hardware)
        hardware.catalog = CATALOG
        hardware.serial = types.SimpleNamespace(Serial=lambda **kwargs: port)
        with mock.patch.object(worker_module.os, "stat", return_value=types.SimpleNamespace(st_rdev=123)), \
                mock.patch.object(Path, "iterdir", return_value=[]), \
                mock.patch.object(worker_module.fcntl, "ioctl") as ioctl:
            hardware.open(CONFIG["devices"]["rx"])
            hardware.release(types.SimpleNamespace(pipe=port))
        self.assertIn(("open", (False, True)), events)
        self.assertIn(("close", (False, True)), events)
        ioctl.assert_called_once_with(123, worker_module.termios.TIOCEXCL)

    def make_worker(self, state, backups, failed_readback=False):
        test = self
        class FakeHardware:
            def __init__(self):
                self.current = worker_module.packed(test.radio())
                self.fail = failed_readback
                self.released = 0
                self.uploads = 0
            def download(self, device, progress):
                return test.radio(self.current)
            def load(self, path):
                return BF5RXRadio(str(path))
            def prepare(self, radio, role):
                return worker_module.apply_plan(radio, role, chirp_common, settings, catalog=CATALOG)
            def upload(self, device, original, candidate, blocks, progress, before_write):
                if blocks:
                    before_write()
                self.current = worker_module.packed(candidate)
                self.uploads += 1
            def readback(self, radio, progress):
                if self.fail:
                    raise IOError("fake power cycle needed")
                return test.radio(self.current)
            def settle_and_verify(self, device, radio, progress):
                return self.readback(radio, progress)
            def release(self, radio):
                self.released += 1
        hardware = FakeHardware()
        active, enabled, calls = {"repeater-engine.service"}, {"repeater-engine.service"}, []
        def service(action, name):
            calls.append((action, name))
            if action == "is-active":
                return name in active
            if action == "is-enabled":
                return name in enabled
            if action == "stop":
                active.discard(name)
            if action == "start":
                active.add(name)
            if action == "disable":
                enabled.discard(name)
            if action == "enable":
                enabled.add(name)
            return True
        worker = worker_module.Worker(hardware, state_dir=state, backup_dir=backups,
                                      service=service, owner_uid=os.getuid())
        worker.catalog = CATALOG
        return worker, hardware, active, enabled, calls

    def test_default_private_plan_can_turn_off_tones_without_changing_other_memories(self):
        catalog = json.loads(json.dumps(CATALOG)); catalog["pairs"][0]["tone_hz"] = None
        for role in ("rx", "tx"):
            radio = self.radio(); before = worker_module.packed(radio)
            worker_module.apply_plan(radio, role, chirp_common, settings, catalog=catalog)
            memory = radio.get_memory(1)
            self.assertEqual(memory.name, "BOX-RX1" if role == "rx" else "BOX-TX1")
            self.assertEqual(memory.tmode, "")
            after = worker_module.packed(radio)
            self.assertEqual(before[:0x10], after[:0x10])
            self.assertEqual(before[0x20:0x800], after[0x20:0x800])
            self.assertEqual(before[0x1020:0x1800], after[0x1020:0x1800])

    def test_worker_inspect_program_and_failure_verify_recovery(self):
        with tempfile.TemporaryDirectory() as temporary:
            state, backups = Path(temporary) / "state", Path(temporary) / "backups"
            state.mkdir(); backups.mkdir()
            worker, hardware, active, enabled, calls = self.make_worker(state, backups, True)
            request = lambda ident, action, role="rx": {"request_id": f"{ident:032x}", "role": role, "action": action, **({"catalog_digest": DIGEST} if action == "program" else {})}
            inspected = worker.run(request(1, "inspect"), CONFIG)
            self.assertTrue(inspected["success"])
            self.assertIn("reconnects automatically", inspected["message"])
            self.assertNotIn("Power-cycle", inspected["message"])
            self.assertEqual(hardware.uploads, 0)
            self.assertIn("repeater-engine.service", active)
            programmed = worker.run(request(2, "program"), CONFIG)
            self.assertFalse(programmed["success"])
            self.assertTrue(programmed["verification_required"])
            self.assertIn("Use Verify to reconnect", programmed["message"])
            self.assertIn("If it cannot reconnect or stays unresponsive", programmed["message"])
            self.assertFalse(active)
            self.assertFalse(enabled)
            self.assertTrue((backups / ("0" * 31 + "2") / "original.img").exists())
            blocked = worker.run(request(3, "inspect", "tx"), CONFIG)
            self.assertFalse(blocked["success"])
            self.assertFalse(active)
            verified = worker.run(request(4, "verify"), CONFIG)
            self.assertTrue(verified["success"])
            self.assertFalse(worker.private["rx"]["verification_required"])
            self.assertIn("repeater-engine.service", active)
            self.assertIn("repeater-engine.service", enabled)
            self.assertLess(calls.index(("disable", "repeater-engine.service")), calls.index(("enable", "repeater-engine.service")))
            self.assertEqual(hardware.released, 4)

    def test_worker_never_writes_before_private_inspection(self):
        with tempfile.TemporaryDirectory() as temporary:
            state, backups = Path(temporary) / "state", Path(temporary) / "backups"
            state.mkdir(); backups.mkdir()
            worker, hardware, active, enabled, _ = self.make_worker(state, backups)
            result = worker.run({"request_id": "a" * 32, "role": "rx", "action": "program", "catalog_digest": DIGEST}, CONFIG)
            self.assertFalse(result["success"])
            self.assertEqual(hardware.uploads, 0)
            self.assertTrue(enabled)

    def test_backup_flush_failure_never_writes_or_accumulates_incomplete_sets(self):
        with tempfile.TemporaryDirectory() as temporary:
            state, backups = Path(temporary) / "state", Path(temporary) / "backups"
            state.mkdir(); backups.mkdir()
            worker, hardware, active, enabled, _ = self.make_worker(state, backups)
            request = {"request_id": "a" * 32, "role": "rx", "action": "inspect"}
            real_sync = worker_module.sync_path
            def fail_image_flush(path):
                if path.suffix == ".img":
                    raise OSError("fake SD card flush failure")
                return real_sync(path)
            with mock.patch.object(worker_module, "sync_path", side_effect=fail_image_flush):
                result = worker.run(request, CONFIG)
            self.assertFalse(result["success"])
            self.assertEqual(hardware.uploads, 0)
            self.assertFalse((backups / ("a" * 32)).exists())
            self.assertIn("repeater-engine.service", active)
            self.assertIn("repeater-engine.service", enabled)

    def test_backup_retention_keeps_five_per_role_and_protects_candidate(self):
        with tempfile.TemporaryDirectory() as temporary:
            state, backups = Path(temporary) / "state", Path(temporary) / "backups"
            state.mkdir(); backups.mkdir()
            worker, _, _, _, _ = self.make_worker(state, backups)
            for role, base in (("rx", 0), ("tx", 100)):
                for number in range(1, 9):
                    folder = worker.backup(self.radio(), f"{number + base:032x}", "original", role, CONFIG["devices"][role])
                    os.utime(folder, (number, number))
            worker.private["rx"] = {"program_id": f"{1:032x}", "verification_required": True}
            unrelated = backups / "notes"; unrelated.mkdir(); (unrelated / "keep.txt").write_text("keep")
            link = backups / ("f" * 32); link.symlink_to(unrelated, target_is_directory=True)
            worker.prune_backups()
            remaining = [folder for folder in backups.iterdir() if folder.is_dir() and not folder.is_symlink() and folder.name != "notes"]
            rx = [folder for folder in remaining if json.loads((folder / "manifest.json").read_text())["role"] == "rx"]
            tx = [folder for folder in remaining if json.loads((folder / "manifest.json").read_text())["role"] == "tx"]
            self.assertEqual(len(rx), 5)
            self.assertEqual(len(tx), 5)
            self.assertIn(backups / f"{1:032x}", rx)
            self.assertEqual({int(folder.name, 16) for folder in tx}, {104, 105, 106, 107, 108})
            self.assertTrue(link.is_symlink())
            self.assertEqual((unrelated / "keep.txt").read_text(), "keep")


if __name__ == "__main__":
    unittest.main()
