"""A held name-page restoration survives installation and strict pair recovery.

This uses the existing simulated radio transport and the real bounded experiment
and worker paths. It never opens a USB device or starts a real service.
"""
import copy
import json
import os
import unittest

import test_name_page_repair as experiment_tests
import test_radio_pair_worker as pair_tests

w = pair_tests.worker
experiment = experiment_tests.experiment


@unittest.skipUnless(pair_tests.FIXTURE.exists(), "Set CHIRP_TEST_SOURCE to the pinned official source")
class NamePagePairRecoveryTests(unittest.TestCase):
    def test_restoration_install_projection_pruning_and_two_pair_changes_preserve_unrelated_bytes(self):
        fixture = pair_tests.PairTransactionTests("test_pair_backups_precede_all_writes_and_only_memory_one_changes")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        catalog_file = fixture.root / "channel-pairs.json"
        w.atomic_json(catalog_file, pair_tests.CATALOG)
        hardware = fixture.hardware
        tx = fixture.radio(hardware.current["tx"])
        memory = pair_tests.chirp_common.Memory()
        memory.number, memory.empty = 11, True
        tx.set_memory(memory)
        raw = bytearray(w.packed(tx))
        raw[0x10BD:0x10C0] = experiment.WANTED
        hardware.current["tx"] = bytes(raw)
        original_images = copy.deepcopy(hardware.current)

        # Capture the same durable partial-pair shape as the physical failure:
        # RX verified, TX written but unverified, and both candidates protected.
        hardware.fail_verify = "tx"
        first = fixture.instance.run(fixture.request(), pair_tests.CONFIG)
        self.assertFalse(first["success"])
        hardware.fail_verify = None
        pair_before = copy.deepcopy(fixture.instance.private["_pair"])
        old_ids = pair_before["backup_ids"].copy()
        raw = bytearray(hardware.current["tx"])
        raw[0x10BD:0x10C0] = experiment.CURRENT
        hardware.current["tx"] = bytes(raw)
        source_images = {
            str(path.relative_to(fixture.backups)): path.read_bytes()
            for token in old_ids.values() for path in (fixture.backups / token).glob("*.img")}

        # Adapt progress signatures without replacing any image, validation,
        # backup, restoration, pair, or verification logic under test.
        class Transport:
            def __getattr__(self, name):
                return getattr(hardware, name)

            def select_model(self, model):
                if model != "5RX":
                    raise AssertionError("The box radios must retain the 5RX profile")

            def download(self, device, progress):
                return hardware.download(device, lambda **value: w.report_progress(
                    progress, value["current"], value["total"]))

            def upload(self, device, original, candidate, blocks, progress, before_write, editor=False):
                return hardware.upload(device, original, candidate, blocks,
                                       lambda **value: w.report_progress(progress, value["current"], value["total"]),
                                       before_write, editor=editor)

            def settle_and_verify(self, device, radio, progress):
                self.release(radio)
                radio.pipe = None
                w.time.sleep(5)
                observed = self.download(device, progress)
                self.release(observed)
                observed.pipe = None
                return observed

        transport = Transport()
        fixture.instance.hardware = transport
        job = experiment.Experiment(fixture.instance, copy.deepcopy(pair_tests.CONFIG))
        result = job.run(True, pair_before["request_id"], old_ids["tx"], output=lambda *args: None)
        self.assertTrue(result["verified"])
        self.assertEqual(hardware.current["tx"][0x10BD:0x10C0], experiment.WANTED)
        self.assertFalse(fixture.active)
        self.assertFalse(fixture.enabled)
        self.assertTrue(fixture.instance.private["_pair"]["verification_required"])
        self.assertTrue(fixture.instance.private["tx"]["verification_required"])
        experimental_id = result["backup_id"]
        private_before_install = copy.deepcopy(fixture.instance.private)

        def worker(adapter):
            return w.Worker(adapter, fixture.state, fixture.backups,
                            service=fixture.instance.service, owner_uid=os.getuid(),
                            pair_config_file=fixture.qualification, catalog_file=catalog_file)

        # install-pi.sh performs exactly this hardware-free state reprojection.
        installer_projection = worker(None)
        installer_projection.publish()
        self.assertEqual(installer_projection.private, private_before_install)
        self.assertEqual(json.loads((fixture.backups / "state.json").read_text()), private_before_install)
        for relative, content in source_images.items():
            self.assertEqual((fixture.backups / relative).read_bytes(), content)
        self.assertFalse(fixture.active)
        self.assertFalse(fixture.enabled)

        recovered = worker(transport)
        self.assertEqual(recovered.private, private_before_install)
        for number in range(1, 9):
            recovered.backup(fixture.radio(hardware.current["tx"]), f"{0x1000 + number:032x}",
                             "original", "tx", pair_tests.CONFIG["devices"]["tx"])
        recovered.prune_backups()
        for token in (*old_ids.values(), experimental_id):
            self.assertTrue((fixture.backups / token).is_dir())
        for relative, content in source_images.items():
            self.assertEqual((fixture.backups / relative).read_bytes(), content)
        self.assertEqual(recovered.private["_pair"], private_before_install["_pair"])
        self.assertFalse(fixture.active)
        self.assertFalse(fixture.enabled)

        service = recovered.service

        def guarded_service(action, name):
            if action in ("enable", "start"):
                durable = json.loads((fixture.backups / "state.json").read_text())
                self.assertFalse(durable["_pair"]["verification_required"])
                self.assertTrue(durable["_pair"]["stored_verified"])
                self.assertTrue(all(durable["_pair"]["roles"][role]["verified"]
                                    and durable[role]["verified"] for role in w.REPEATER_ROLES))
            return service(action, name)

        recovered.service = guarded_service
        for request_number, pair_id in ((2, "rpt1"), (3, "default")):
            with self.subTest(pair=pair_id):
                completed = recovered.run(fixture.request(request_number, pair_id), pair_tests.CONFIG)
                self.assertTrue(completed["success"], completed)
                self.assertEqual(recovered.private["_pair"]["pair_id"], pair_id)
                self.assertFalse(recovered.private["_pair"]["verification_required"])
                self.assertEqual(hardware.current["tx"][0x10BD:0x10C0], experiment.WANTED)
                working = set(range(0x10, 0x20)) | set(range(0x1010, 0x1017))
                for role in w.REPEATER_ROLES:
                    self.assertEqual(w.editor_memory(fixture.radio(hardware.current[role]), 1,
                                                     pair_tests.chirp_common), w.pair_memory(pair_id, role, pair_tests.CATALOG))
                    for index, (before, after) in enumerate(zip(original_images[role], hardware.current[role])):
                        if index not in working:
                            self.assertEqual((before ^ after) & ~w.VOLATILE_MASKS.get(index, 0), 0,
                                             f"{role} unrelated byte changed at 0x{index:04X}")
                self.assertEqual(fixture.active, {"repeater-engine.service"})
                self.assertEqual(fixture.enabled, fixture.active)


if __name__ == "__main__":
    unittest.main()
