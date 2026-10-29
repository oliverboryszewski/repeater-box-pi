"""Paired working-memory transactions use simulated radios, never USB devices."""
import copy
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
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
from radio_test_catalog import CATALOG, DIGEST, PLAN, PAIR_PLANS
spec = importlib.util.spec_from_file_location("pair_worker", ROOT / "scripts" / "program-radio.py")
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)
SOURCE = Path(os.environ.get("CHIRP_TEST_SOURCE", "/opt/repeater-box/vendor/chirp"))
FIXTURE = SOURCE / "tests" / "images" / "Baofeng_5RX.img"
if FIXTURE.exists():
    sys.path.insert(0, str(SOURCE))
    from chirp import chirp_common, memmap, settings
    from chirp.drivers.baofeng_wp970i import BF5RXRadio

CONFIG = {"schema": 1, "model": "5RX", "devices": {
    "rx": "/dev/serial/by-id/usb-AIOC_All-In-One-Cable_test_rx-if04",
    "tx": "/dev/serial/by-id/usb-AIOC_All-In-One-Cable_test_tx-if04"}}


class PairSchemaTests(unittest.TestCase):
    def test_requests_accept_only_a_fixed_pair_identifier(self):
        request = {"request_id": "a" * 32, "action": "change-pair", "catalog_digest": DIGEST, "pair_id": "rpt1"}
        self.assertTrue(worker.valid_request(request))
        for updates in ({"pair_id": "../rpt6"}, {"pair_id": []}, {"request_id": "../../"},
                        {"role": "rx"}, {"password": "secret"}, {"rx_hz": 145300000},
                        {"image": "/tmp/original"}, {"command": "anything"}):
            with self.subTest(updates=updates):
                self.assertFalse(worker.valid_request({**request, **updates}))

    def test_fixed_catalog_and_role_channels_are_exact(self):
        expected = [(key, value["rx_hz"], value["tx_hz"]) for key, value in PAIR_PLANS.items()]
        self.assertFalse(hasattr(worker, "PAIR_PLANS"))
        self.assertEqual(worker.pair_plans(CATALOG), PAIR_PLANS)
        for pair_id, rx_hz, tx_hz in expected:
            rx, tx = (worker.pair_memory(pair_id, role, CATALOG) for role in ("rx", "tx"))
            self.assertEqual(rx["number"], 1)
            self.assertEqual(tx["number"], 1)
            self.assertEqual(rx["rx_hz"], rx_hz)
            self.assertIsNone(rx["tx_hz"])
            self.assertEqual(tx["rx_hz"], tx_hz)
            self.assertEqual(tx["tx_hz"], tx_hz)
            self.assertEqual(rx["rx_tone"], {"type": "CTCSS", "value": 88.5})
            self.assertIsNone(rx["tx_tone"])
            self.assertEqual(tx["tx_tone"], {"type": "CTCSS", "value": 88.5})
            self.assertIsNone(tx["rx_tone"])
            for row in (rx, tx):
                self.assertEqual((row["power"], row["mode"]), ("High", "NFM"))
                self.assertLessEqual(len(row["name"]), 7)

    def test_public_projection_excludes_credentials_devices_and_backup_paths(self):
        projected = worker.public_pair({"pair_id": "rpt1", "request_id": "a" * 32,
                                        "phase": "failed", "verification_required": True,
                                        "password": "PRIVATE", "devices": CONFIG["devices"],
                                        "protected_backups": ["PRIVATE"], "resume_services": ["PRIVATE"]})
        self.assertEqual(projected["pair_id"], "rpt1")
        self.assertTrue(projected["verification_required"])
        self.assertNotIn("PRIVATE", json.dumps(projected))
        self.assertNotIn("All-In-One", json.dumps(projected))
        self.assertIsNone(worker.public_pair({"pair_id": []})["pair_id"])


@unittest.skipUnless(FIXTURE.exists(), "Set CHIRP_TEST_SOURCE to the pinned official CHIRP source")
class PairTransactionTests(unittest.TestCase):
    def radio(self, raw=None):
        if raw is None:
            return BF5RXRadio(str(FIXTURE))
        radio = BF5RXRadio(None)
        radio._mmap = memmap.MemoryMapBytes(raw)
        radio.process_mmap()
        return radio

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.state, self.backups = self.root / "state", self.root / "backups"
        self.state.mkdir()
        self.backups.mkdir()
        self.qualification = self.root / "radio-pair.json"
        worker.atomic_json(self.qualification, {"schema": 1, "enabled": True})
        self.events = []
        self.active, self.enabled = {"repeater-engine.service"}, {"repeater-engine.service"}
        test = self

        class FakeHardware:
            def __init__(self):
                self.current, self.downloads = {}, {"rx": 0, "tx": 0}
                self.fail_download = set()
                self.mutations = {}
                self.fail_upload = None
                self.fail_verify = None
                self.fail_release = set()
                self.on_upload = None
                for index, role in enumerate(("rx", "tx")):
                    radio = test.radio()
                    worker.apply_plan(radio, role, chirp_common, settings, catalog=CATALOG)
                    raw = bytearray(worker.packed(radio))
                    raw[0x1F82] ^= index  # Different protected data per physical radio.
                    self.current[role] = bytes(raw)

            def download(self, device, progress):
                role = next(role for role, value in CONFIG["devices"].items() if value == device)
                self.downloads[role] += 1
                generation = self.downloads[role]
                test.events.append(("download", role, generation))
                if (role, generation) in self.fail_download:
                    raise worker.ProgrammingError("The attached radio did not complete its download.")
                raw = bytearray(self.current[role])
                for address, mask in self.mutations.get((role, generation), {}).items():
                    raw[address] ^= mask
                self.current[role] = bytes(raw)
                radio = test.radio(bytes(raw))
                radio.test_role, radio.test_generation = role, generation
                radio.pipe = types.SimpleNamespace(is_open=True)
                progress(current=128, total=128)
                return radio

            def load(self, path):
                return BF5RXRadio(str(path))

            def prepare_editor(self, radio, changes, values):
                return worker.apply_editor(radio, changes, values, chirp_common, settings)

            def upload(self, device, original, candidate, blocks, progress, before_write, editor=False):
                role = candidate.test_role
                test.events.append(("upload", role))
                test.assertTrue(editor)
                test.assertFalse(test.active)
                test.assertFalse(test.enabled)
                state = json.loads((test.backups / "state.json").read_text())
                test.assertTrue(state["_pair"]["verification_required"])
                for selected in ("rx", "tx"):
                    folder = test.backups / state["_pair"]["backup_ids"][selected]
                    test.assertTrue((folder / "original.img").is_file())
                    test.assertTrue((folder / "candidate.img").is_file())
                if self.on_upload:
                    self.on_upload(role, original, candidate)
                if blocks:
                    before_write()
                if role == self.fail_upload:
                    raw = bytearray(worker.packed(original))
                    if blocks:
                        address = blocks[0]
                        raw[address:address + 16] = worker.packed(candidate)[address:address + 16]
                    self.current[role] = bytes(raw)
                    raise worker.ProgrammingError("An upload block was not acknowledged.")
                self.current[role] = worker.packed(candidate)
                progress(current=len(blocks), total=max(1, len(blocks)))

            def settle_and_verify(self, device, radio, progress):
                self.release(radio)
                radio.pipe = None
                worker.time.sleep(5)
                observed = self.download(device, progress)
                if observed.test_role == self.fail_verify:
                    raw = bytearray(worker.packed(observed))
                    raw[0x1F80] ^= 1
                    changed = test.radio(bytes(raw))
                    changed.test_role, changed.test_generation = observed.test_role, observed.test_generation
                    changed.pipe = observed.pipe
                    observed = changed
                self.release(observed)
                observed.pipe = None
                return observed

            def release(self, radio):
                if getattr(radio, "pipe", None) is not None:
                    key = (radio.test_role, radio.test_generation)
                    test.events.append(("release", *key))
                    radio.pipe = None
                    if key in self.fail_release:
                        self.fail_release.remove(key)
                        raise worker.ProgrammingError("Could not close radio session.")

        def service(action, name):
            self.events.append((action, name))
            if action == "is-active":
                return name in self.active
            if action == "is-enabled":
                return name in self.enabled
            if action == "stop":
                self.active.discard(name)
            elif action == "start":
                self.active.add(name)
            elif action == "disable":
                self.enabled.discard(name)
            elif action == "enable":
                self.enabled.add(name)
            return True

        self.hardware = FakeHardware()
        self.instance = worker.Worker(self.hardware, self.state, self.backups, service=service,
                                      owner_uid=os.getuid(), pair_config_file=self.qualification)
        self.instance.catalog = CATALOG
        self.pause = mock.patch.object(worker.time, "sleep", side_effect=lambda seconds: self.events.append(("quiet", seconds)))
        self.pause.start()
        self.addCleanup(self.pause.stop)

    def request(self, number=1, pair_id="rpt1"):
        return {"request_id": f"{number:032x}", "action": "change-pair", "catalog_digest": DIGEST, "pair_id": pair_id}

    def run_timed_pair(self, read_seconds):
        """Advance a fake monotonic clock only for radio I/O and quiet waits."""
        now, trace = [100.0], []
        download, release = self.hardware.download, self.hardware.release

        def timed_download(device, progress):
            role = next(role for role, value in CONFIG["devices"].items() if value == device)
            generation = self.hardware.downloads[role] + 1
            trace.append(("download", role, generation, now[0]))
            radio = download(device, progress)
            now[0] += read_seconds.get((role, generation), 0)
            return radio

        def timed_release(radio):
            if getattr(radio, "pipe", None) is not None:
                trace.append(("release", radio.test_role, radio.test_generation, now[0]))
            release(radio)

        def sleep(seconds):
            self.assertGreater(seconds, 0)
            self.assertLessEqual(seconds, 5)
            trace.append(("quiet", seconds, now[0]))
            now[0] += seconds

        with mock.patch.object(worker.time, "monotonic", side_effect=lambda: now[0]), \
                mock.patch.object(worker.time, "sleep", side_effect=sleep), \
                mock.patch.object(self.hardware, "download", side_effect=timed_download), \
                mock.patch.object(self.hardware, "release", side_effect=timed_release):
            result = self.instance.run(self.request(), CONFIG)
        self.assertTrue(result["success"], result)
        return trace

    def test_partner_read_counts_toward_rx_quiet_time(self):
        trace = self.run_timed_pair({("rx", 1): 18, ("tx", 1): 18})
        rx_release = next(row for row in trace if row[:3] == ("release", "rx", 1))
        tx_read = next(row for row in trace if row[:3] == ("download", "tx", 1))
        rx_reconnect = next(row for row in trace if row[:3] == ("download", "rx", 2))
        self.assertLess(trace.index(rx_release), trace.index(tx_read))
        self.assertEqual(rx_reconnect[-1] - rx_release[-1], 18)
        # These are the fake adapter's two post-upload verification waits.
        # Neither physical radio needs another wait merely for changing roles.
        self.assertEqual([row[1] for row in trace if row[0] == "quiet"], [5, 5])
        self.assertFalse(any("before reconnecting to" in event["message"]
                             for event in json.loads((self.state / "radio-programming-result.json").read_text())["events"]))

    def test_short_partner_read_waits_only_remaining_rx_quiet_time(self):
        trace = self.run_timed_pair({("tx", 1): 2})
        rx_release = next(row for row in trace if row[:3] == ("release", "rx", 1))
        rx_reconnect = next(row for row in trace if row[:3] == ("download", "rx", 2))
        self.assertEqual(rx_reconnect[-1] - rx_release[-1], 5)
        self.assertEqual([row[1] for row in trace if row[0] == "quiet"], [3, 5, 5])

    def test_immediate_reuse_waits_five_but_tx_settles_during_rx_work(self):
        trace = self.run_timed_pair({})
        rx_release = next(row for row in trace if row[:3] == ("release", "rx", 1))
        rx_reconnect = next(row for row in trace if row[:3] == ("download", "rx", 2))
        tx_release = next(row for row in trace if row[:3] == ("release", "tx", 1))
        tx_reconnect = next(row for row in trace if row[:3] == ("download", "tx", 2))
        self.assertEqual(rx_reconnect[-1] - rx_release[-1], 5)
        self.assertEqual(tx_reconnect[-1] - tx_release[-1], 10)
        self.assertEqual([row[1] for row in trace if row[0] == "quiet"], [5, 5, 5])

    def test_initial_release_failure_stops_before_reading_the_partner(self):
        self.hardware.fail_release.add(("rx", 1))
        result = self.instance.run(self.request(), CONFIG)
        self.assertFalse(result["success"])
        self.assertTrue(result["verification_required"])
        self.assertEqual(self.hardware.downloads, {"rx": 1, "tx": 0})
        self.assertFalse(self.active)
        self.assertFalse(self.enabled)
        self.assertFalse(any(event[0] == "upload" for event in self.events))

    def test_unconfigured_unknown_pair_and_stale_digest_never_touch_hardware(self):
        requests = [dict(self.request(), pair_id="pair9"), dict(self.request(), catalog_digest="f"*64)]
        for request in requests:
            self.assertFalse(self.instance.run(request, CONFIG)["success"])
            self.assertEqual(self.hardware.downloads, {"rx": 0, "tx": 0})
        self.instance.catalog = None
        self.assertFalse(self.instance.run(self.request(), CONFIG)["success"])
        self.assertEqual(self.hardware.downloads, {"rx": 0, "tx": 0})

    def test_catalog_change_cannot_rebaseline_an_unfinished_pair(self):
        self.hardware.fail_verify = "tx"
        first = self.instance.run(self.request(), CONFIG)
        self.assertFalse(first["success"])
        self.assertTrue(self.instance.private["_pair"]["verification_required"])
        before = copy.deepcopy(self.hardware.downloads)
        self.instance.catalog = copy.deepcopy(CATALOG)
        self.instance.catalog["pairs"][1]["label"] = "Changed after failure"
        request = dict(self.request(2), catalog_digest=worker.catalog_digest(self.instance.catalog))
        self.assertFalse(self.instance.run(request, CONFIG)["success"])
        self.assertEqual(self.hardware.downloads, before)
        self.assertEqual(self.instance.private["_pair"]["catalog_digest"], DIGEST)

    def test_disabled_missing_or_untrusted_qualification_never_touches_hardware(self):
        for content in ({"schema": 1, "enabled": False}, {"schema": True, "enabled": True},
                        {"schema": 1, "enabled": True, "extra": 1}, None):
            with self.subTest(content=content):
                if content is None:
                    self.qualification.unlink(missing_ok=True)
                else:
                    worker.atomic_json(self.qualification, content)
                result = self.instance.run(self.request(), CONFIG)
                self.assertFalse(result["success"])
                self.assertEqual(self.hardware.downloads, {"rx": 0, "tx": 0})
        worker.atomic_json(self.qualification, {"schema": 1, "enabled": True})
        self.qualification.chmod(0o666)
        self.assertFalse(self.instance.run(self.request(), CONFIG)["success"])
        self.assertEqual(self.hardware.downloads, {"rx": 0, "tx": 0})

    def test_pair_backups_precede_all_writes_and_only_memory_one_changes(self):
        original = copy.deepcopy(self.hardware.current)
        result = self.instance.run(self.request(), CONFIG)
        self.assertTrue(result["success"], result)
        self.assertEqual(result["phase"], "configured")
        self.assertEqual(self.hardware.downloads, {"rx": 3, "tx": 3})
        self.assertEqual([event for event in self.events if event[0] == "upload"], [("upload", "rx"), ("upload", "tx")])
        for role in ("rx", "tx"):
            row = worker.editor_memory(self.radio(self.hardware.current[role]), 1, chirp_common)
            self.assertEqual(row, worker.pair_memory("rpt1", role, CATALOG))
            allowed = set(range(0x10, 0x20)) | set(range(0x1010, 0x1017))
            changed = {i for i, (old, new) in enumerate(zip(original[role], self.hardware.current[role])) if old != new}
            self.assertLessEqual(changed, allowed)
            self.assertEqual(original[role][0xE76:0xE78], self.hardware.current[role][0xE76:0xE78])
        first_write = self.events.index(("upload", "rx"))
        self.assertLess(self.events.index(("download", "tx", 1)), first_write)
        self.assertLess(self.events.index(("release", "rx", 1)), self.events.index(("quiet", 5)))
        start = self.events.index(("start", "repeater-engine.service"))
        self.assertGreater(start, self.events.index(("release", "tx", 3)))
        self.assertEqual(self.active, {"repeater-engine.service"})
        self.assertEqual(self.enabled, self.active)
        public = json.loads((self.state / "radio-programming-state.json").read_text())["_pair"]
        self.assertEqual(public["pair_id"], "rpt1")
        self.assertTrue(public["stored_verified"])
        self.assertFalse(public["verification_required"])
        self.assertNotIn("live frequency", result["message"])

    def test_second_original_failure_writes_nothing_and_restores_previous_services(self):
        self.hardware.fail_download.add(("tx", 1))
        result = self.instance.run(self.request(), CONFIG)
        self.assertFalse(result["success"])
        self.assertFalse(any(event[0] == "upload" for event in self.events))
        self.assertFalse(result["verification_required"])
        self.assertEqual(self.active, {"repeater-engine.service"})

    def test_release_failure_before_candidate_preparation_persists_interlock(self):
        self.hardware.fail_download.add(("tx", 1))
        self.hardware.fail_release.add(("rx", 1))
        result = self.instance.run(self.request(), CONFIG)
        self.assertFalse(result["success"])
        self.assertTrue(result["verification_required"])
        self.assertFalse(self.active)
        self.assertFalse(self.enabled)
        saved = json.loads((self.backups / "state.json").read_text())
        self.assertTrue(saved["_pair"]["verification_required"])
        self.assertFalse(any(event[0] == "upload" for event in self.events))
        self.hardware.fail_download.clear()
        self.assertTrue(self.instance.run(self.request(2), CONFIG)["success"])

    def test_backup_flush_failure_writes_nothing(self):
        original = worker.sync_path
        def sync(path):
            if path.name == "original.img" and len(list(self.backups.glob("*/original.img"))) == 2:
                raise OSError("disk full")
            original(path)
        with mock.patch.object(worker, "sync_path", side_effect=sync):
            result = self.instance.run(self.request(), CONFIG)
        self.assertFalse(result["success"])
        self.assertFalse(any(event[0] == "upload" for event in self.events))
        self.assertEqual(self.active, {"repeater-engine.service"})

    def test_unsafe_globals_fail_before_any_upload_without_rewriting_them(self):
        for changed_settings in ({"settings.vox": "1"}, {"settings.tdr": True},
                                 {"settings.save": "1:1"}, {"settings.workmode": "Frequency"},
                                 {"settings.displayab": "B"}, {"settings.autolk": False},
                                 {"settings.timeout": "75 sec"}):
            with self.subTest(settings=changed_settings):
                radio = self.radio(self.hardware.current["tx"])
                worker.apply_editor(radio, [], changed_settings, chirp_common, settings)
                self.hardware.current["tx"] = worker.packed(radio)
                self.hardware.downloads = {"rx": 0, "tx": 0}
                result = self.instance.run(self.request(len(self.events) + 1), CONFIG)
                self.assertFalse(result["success"])
                self.assertFalse(any(event[0] == "upload" for event in self.events))
                worker.apply_plan(radio, "tx", chirp_common, settings, catalog=CATALOG)
                self.hardware.current["tx"] = worker.packed(radio)

    def test_fresh_prewrite_drift_aborts_and_interlocks_the_transaction(self):
        self.hardware.mutations[("rx", 2)] = {0xE28: 1}
        result = self.instance.run(self.request(), CONFIG)
        self.assertFalse(result["success"])
        self.assertTrue(result["verification_required"])
        self.assertFalse(any(event[0] == "upload" for event in self.events))
        self.assertFalse(self.active)
        self.assertFalse(self.enabled)
        self.assertEqual(self.instance.private["_pair"]["phase"], "failed")

    def test_runtime_drift_is_preserved_without_uploading_cursor_or_lock_bytes(self):
        self.hardware.mutations[("rx", 2)] = {0xE4D: 1, 0xE76: 0x7F, 0xE77: 0x7F}
        def check(role, original, candidate):
            if role == "rx":
                self.assertEqual(worker.packed(original)[0xE4D], worker.packed(candidate)[0xE4D])
                self.assertEqual(worker.packed(original)[0xE76:0xE78], worker.packed(candidate)[0xE76:0xE78])
        self.hardware.on_upload = check
        self.assertTrue(self.instance.run(self.request(), CONFIG)["success"])

    def test_rx_and_tx_upload_or_verify_failure_never_resumes_half_a_pair(self):
        for field, role in (("fail_upload", "rx"), ("fail_upload", "tx"), ("fail_verify", "rx"), ("fail_verify", "tx")):
            with self.subTest(field=field, role=role):
                setattr(self.hardware, field, role)
                result = self.instance.run(self.request(len(self.events) + 1), CONFIG)
                self.assertFalse(result["success"])
                self.assertTrue(result["verification_required"])
                self.assertFalse(self.active)
                self.assertFalse(self.enabled)
                saved = json.loads((self.backups / "state.json").read_text())
                self.assertTrue(saved["_pair"]["verification_required"])
                self.assertFalse(saved["_pair"]["stored_verified"])
                setattr(self.hardware, field, None)

    def test_retry_reloads_durable_pair_interlock_and_resumes_only_after_both_finish(self):
        self.hardware.fail_upload = "tx"
        first = self.instance.run(self.request(), CONFIG)
        self.assertFalse(first["success"])
        old_ids = copy.deepcopy(self.instance.private["_pair"]["backup_ids"])
        self.hardware.fail_upload = None
        recovered = worker.Worker(self.hardware, self.state, self.backups, service=self.instance.service,
                                  owner_uid=os.getuid(), pair_config_file=self.qualification)
        recovered.catalog = CATALOG
        result = recovered.run(self.request(2, "rpt4"), CONFIG)
        self.assertTrue(result["success"], result)
        self.assertEqual(self.active, {"repeater-engine.service"})
        self.assertEqual(recovered.private["_pair"]["pair_id"], "rpt4")
        self.assertNotEqual(old_ids, recovered.private["_pair"]["backup_ids"])

    def test_process_interruption_between_radios_leaves_a_recoverable_durable_interlock(self):
        def interrupt(role, original, candidate):
            if role == "tx":
                raise KeyboardInterrupt()
        self.hardware.on_upload = interrupt
        with self.assertRaises(KeyboardInterrupt):
            self.instance.run(self.request(), CONFIG)
        saved = json.loads((self.backups / "state.json").read_text())
        self.assertTrue(saved["_pair"]["verification_required"])
        self.assertTrue(saved["_pair"]["roles"]["rx"]["verified"])
        self.assertFalse(saved["_pair"]["roles"]["tx"]["verified"])
        self.assertFalse(self.active)
        self.assertFalse(self.enabled)
        self.hardware.on_upload = None
        recovered = worker.Worker(self.hardware, self.state, self.backups, service=self.instance.service,
                                  owner_uid=os.getuid(), pair_config_file=self.qualification)
        recovered.catalog = CATALOG
        result = recovered.run(self.request(2), CONFIG)
        self.assertTrue(result["success"], result)
        self.assertEqual(self.active, {"repeater-engine.service"})

    def test_retry_refuses_changed_bindings_or_protected_radio_data(self):
        self.hardware.fail_upload = "tx"
        self.instance.run(self.request(), CONFIG)
        altered = copy.deepcopy(CONFIG)
        altered["devices"]["tx"] = "/dev/serial/by-id/usb-AIOC_All-In-One-Cable_different-if04"
        self.assertFalse(self.instance.run(self.request(2), altered)["success"])
        self.hardware.fail_upload = None
        raw = bytearray(self.hardware.current["tx"])
        raw[0x1F80] ^= 1
        self.hardware.current["tx"] = bytes(raw)
        result = self.instance.run(self.request(3), CONFIG)
        self.assertFalse(result["success"])
        self.assertRegex(result["message"], "Protected radio data")
        self.assertFalse(self.active)

    def test_retry_never_rebaselines_unrelated_empty_names_or_companion_records(self):
        # An unused channel's complete name record is editable by the general
        # editor, but a paired change has authority over memory 1 only.
        raw = bytearray(self.hardware.current["tx"])
        raw[0xB0:0xC0] = b"\xff" * 16
        self.hardware.current["tx"] = bytes(raw)
        self.hardware.fail_upload = "tx"
        self.assertFalse(self.instance.run(self.request(), CONFIG)["success"])
        self.hardware.fail_upload = None
        original_current = self.hardware.current["tx"]
        old_ids = copy.deepcopy(self.instance.private["_pair"]["backup_ids"])
        original_candidates = {role: (self.backups / token / "candidate.img").read_bytes()
                               for role, token in old_ids.items()}
        for attempt, offsets in enumerate(((0x10B0,), (0x10BD, 0x10BE, 0x10BF), (0x1000,),
                                           (0x1F80,), (0xE28,), (0x101D,), (0x1C,)), 2):
            with self.subTest(offsets=offsets):
                changed = bytearray(original_current)
                for offset in offsets:
                    changed[offset] ^= 1
                self.hardware.current["tx"] = bytes(changed)
                previous_uploads = sum(event[0] == "upload" for event in self.events)
                result = self.instance.run(self.request(attempt), CONFIG)
                self.assertFalse(result["success"])
                self.assertRegex(result["message"], "Protected radio data")
                self.assertTrue(result["verification_required"])
                self.assertEqual(sum(event[0] == "upload" for event in self.events), previous_uploads)
                self.assertEqual(self.instance.private["_pair"]["backup_ids"], old_ids)
                self.assertTrue(self.instance.private["_pair"]["verification_required"])
                self.assertFalse(self.instance.private["_pair"]["stored_verified"])
                self.assertFalse(self.active)
                self.assertFalse(self.enabled)
                for role, token in old_ids.items():
                    self.assertEqual((self.backups / token / "candidate.img").read_bytes(), original_candidates[role])
        self.hardware.current["tx"] = original_current

    def test_retry_accepts_partial_memory_one_and_preserves_only_existing_runtime_masks(self):
        self.hardware.fail_upload = "tx"
        self.assertFalse(self.instance.run(self.request(), CONFIG)["success"])
        self.hardware.fail_upload = None
        # TX's failed upload left one of memory 1's intended blocks applied.
        # A retry to another reviewed pair may reconcile that working record.
        raw = bytearray(self.hardware.current["tx"])
        for offset, mask in worker.VOLATILE_MASKS.items():
            raw[offset] ^= mask
        self.hardware.current["tx"] = bytes(raw)
        result = self.instance.run(self.request(2, "rpt4"), CONFIG)
        self.assertTrue(result["success"], result)
        final = self.hardware.current["tx"]
        self.assertEqual(worker.editor_memory(self.radio(final), 1, chirp_common), worker.pair_memory("rpt4", "tx", CATALOG))
        for offset in worker.VOLATILE_MASKS:
            self.assertEqual(final[offset], raw[offset])

    def test_pair_retry_comparator_keeps_name_padding_and_reserved_bits_protected(self):
        expected = self.radio(self.hardware.current["tx"])
        before = worker.packed(expected)
        for offset, mask in ((0x101D, 1), (0x1C, 1), (0xE4D, 0x80), (0xE76, 0x80)):
            with self.subTest(offset=offset):
                changed = bytearray(before); changed[offset] ^= mask
                self.assertTrue(worker.protected_pair_changes(expected, self.radio(bytes(changed))))
        allowed = self.radio(before)
        worker.apply_editor(allowed, [worker.pair_memory("rpt4", "tx", CATALOG)], {}, chirp_common, settings)
        changed = bytearray(worker.packed(allowed))
        for offset, mask in worker.VOLATILE_MASKS.items():
            changed[offset] ^= mask
        self.assertEqual(worker.protected_pair_changes(expected, self.radio(bytes(changed))), [])

    def test_retry_checks_verified_roles_while_pair_interlock_remains(self):
        self.hardware.fail_upload = "tx"
        self.assertFalse(self.instance.run(self.request(), CONFIG)["success"])
        self.hardware.fail_upload = None
        old_ids = copy.deepcopy(self.instance.private["_pair"]["backup_ids"])
        original_current = copy.deepcopy(self.hardware.current)
        # Model separately verifying each role while the pair's interlock is
        # still pending. Neither role may silently baseline later drift.
        for state in self.instance.private.values():
            if isinstance(state, dict) and state.get("program_kind") == "pair":
                state.update(verified=True, verification_required=False)
        for attempt, role in enumerate(("rx", "tx"), 2):
            with self.subTest(role=role):
                self.hardware.current = copy.deepcopy(original_current)
                changed = bytearray(self.hardware.current[role]); changed[0x10BD] ^= 1
                self.hardware.current[role] = bytes(changed)
                uploads_before = sum(event[0] == "upload" for event in self.events)
                result = self.instance.run(self.request(attempt), CONFIG)
                self.assertFalse(result["success"])
                self.assertRegex(result["message"], "Protected radio data")
                self.assertEqual(sum(event[0] == "upload" for event in self.events), uploads_before)
                self.assertEqual(self.instance.private["_pair"]["backup_ids"], old_ids)
                self.assertTrue(self.instance.private["_pair"]["verification_required"])
                self.assertFalse(self.active)
                self.assertFalse(self.enabled)

    def test_retry_cannot_replace_a_verified_pair_role_with_an_independent_candidate(self):
        self.hardware.fail_upload = "tx"
        self.assertFalse(self.instance.run(self.request(), CONFIG)["success"])
        self.hardware.fail_upload = None
        self.instance.private["rx"].update(program_kind="editor", verified=True,
                                           verification_required=False, program_id="f" * 32)
        uploads_before = sum(event[0] == "upload" for event in self.events)
        result = self.instance.run(self.request(2), CONFIG)
        self.assertFalse(result["success"])
        self.assertRegex(result["message"], "unfinished pair candidate")
        self.assertEqual(sum(event[0] == "upload" for event in self.events), uploads_before)
        self.assertTrue(self.instance.private["_pair"]["verification_required"])

    def test_unrelated_programmer_pending_work_blocks_pair_change(self):
        self.instance.private["programmer"] = {"verification_required": True}
        result = self.instance.run(self.request(), CONFIG)
        self.assertFalse(result["success"])
        self.assertEqual(self.hardware.downloads, {"rx": 0, "tx": 0})

    def test_pair_retry_does_not_supersede_an_independent_editor_upload(self):
        self.hardware.fail_upload = "tx"
        self.instance.run(self.request(), CONFIG)
        self.instance.private["tx"].update(program_kind="editor", program_id="f" * 32)
        downloads = dict(self.hardware.downloads)
        result = self.instance.run(self.request(2), CONFIG)
        self.assertFalse(result["success"])
        self.assertEqual(self.hardware.downloads, downloads)
        self.assertRegex(result["message"], "separate unfinished")

    def test_no_op_pair_still_reads_both_and_does_not_write_runtime_fields(self):
        result = self.instance.run(self.request(pair_id="default"), CONFIG)
        self.assertTrue(result["success"], result)
        self.assertFalse(any(value["written"] for value in self.instance.private["_pair"]["roles"].values()))
        self.assertEqual(self.hardware.downloads, {"rx": 3, "tx": 3})

    def test_independent_radio_write_invalidates_a_configured_pair_label(self):
        self.assertTrue(self.instance.run(self.request(), CONFIG)["success"])
        self.instance.invalidate_pair("programmer")
        self.assertTrue(self.instance.private["_pair"]["stored_verified"])
        self.instance.invalidate_pair("rx")
        self.assertFalse(self.instance.private["_pair"]["stored_verified"])
        self.assertEqual(self.instance.private["_pair"]["phase"], "invalidated")

    def test_repeated_pair_retries_keep_five_sets_per_role(self):
        self.hardware.fail_upload = "tx"
        for number in range(1, 10):
            self.assertFalse(self.instance.run(self.request(number), CONFIG)["success"])
        self.hardware.fail_upload = None
        self.assertTrue(self.instance.run(self.request(10), CONFIG)["success"])
        for role in ("rx", "tx"):
            folders = [folder for folder in self.backups.iterdir() if folder.is_dir()
                       and json.loads((folder / "manifest.json").read_text())["role"] == role]
            self.assertLessEqual(len(folders), 5)
            self.assertTrue((self.backups / self.instance.private[role]["program_id"]).is_dir())


if __name__ == "__main__":
    unittest.main()
