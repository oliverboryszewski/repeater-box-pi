"""Private station configuration and upgrade migration; no USB access."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("configure_private_pairs", ROOT / "scripts/configure-channel-pairs.py")
configure = importlib.util.module_from_spec(spec)
spec.loader.exec_module(configure)

# Synthetic validation fixtures, not operating recommendations.
CATALOG = {"schema": 1, "default_pair_id": "default", "pairs": [
    {"id": "default", "label": "Pair 1", "rx_hz": 145300000, "tx_hz": 433500000, "tone_hz": 100.0},
    {"id": "alternate", "label": "Pair 2", "rx_hz": 145320000, "tx_hz": 433520000, "tone_hz": None}]}


class PrivatePairConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = self.root / "channel-pairs.json"
        self.worker = self.root / "program-radio.py"
        self.state = self.root / "state.json"
        self.owner = os.getuid()

    def json(self, path, value):
        path.write_text(json.dumps(value))
        path.chmod(0o640)

    def legacy(self):
        plan = configure.schema.default_plan(CATALOG)
        pairs = {item["id"]: {"name": "OLD", "rx_hz": item["rx_hz"], "tx_hz": item["tx_hz"]}
                 for item in CATALOG["pairs"]}
        self.worker.write_text("raise RuntimeError('must never execute')\nPLAN = " + repr(plan)
                               + "\nPAIR_PLANS = " + repr(pairs) + "\n")
        self.worker.chmod(0o644)

    def migrate(self):
        return configure.migrate_installed(self.config, self.worker, self.state,
                                           self.owner, output=lambda *args: None)

    def save(self, catalog=CATALOG):
        return configure.save_catalog(catalog, self.config, self.state, self.owner)

    def test_fresh_install_has_no_implicit_frequencies_or_config_file(self):
        self.assertFalse(self.migrate())
        self.assertFalse(self.config.exists())
        self.worker.write_text("PLAN = {}\nPAIR_PLANS = {}\n")
        self.assertFalse(self.migrate())
        self.assertFalse(self.config.exists())

    def test_legacy_literals_migrate_without_executing_code_or_changing_sources(self):
        self.legacy()
        self.json(self.state, {"rx": {"verified": True}, "_pair": {"verification_required": False}})
        before = {path.name: path.read_bytes() for path in (self.worker, self.state)}
        self.assertTrue(self.migrate())
        catalog = json.loads(self.config.read_text())
        self.assertEqual([item["id"] for item in catalog["pairs"]], ["default", "alternate"])
        self.assertEqual([item["rx_hz"] for item in catalog["pairs"]], [item["rx_hz"] for item in CATALOG["pairs"]])
        self.assertEqual([item["tx_hz"] for item in catalog["pairs"]], [item["tx_hz"] for item in CATALOG["pairs"]])
        self.assertEqual([item["tone_hz"] for item in catalog["pairs"]], [100.0, 100.0])
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o640)
        for path in (self.worker, self.state):
            self.assertEqual(path.read_bytes(), before[path.name])
        plans = configure.schema.pair_plans(catalog)
        self.assertEqual([(item["rx_name"], item["tx_name"]) for item in plans.values()],
                         [("BOX-RX1", "BOX-TX1"), ("BOX-RX2", "BOX-TX2")])

    def test_existing_private_configuration_is_preserved_exactly(self):
        self.json(self.config, CATALOG)
        before, stamp = self.config.read_bytes(), self.config.stat().st_mtime_ns
        self.worker.write_text("this is deliberately not valid Python")
        self.assertTrue(self.migrate())
        self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual(self.config.stat().st_mtime_ns, stamp)

    def test_pending_legacy_pair_and_single_upload_block_migration_without_writes(self):
        self.legacy()
        for role in ("rx", "tx", "programmer", "_pair"):
            with self.subTest(role=role):
                self.json(self.state, {role: {"verification_required": True}})
                with self.assertRaisesRegex(ValueError, "unfinished"):
                    self.migrate()
                self.assertFalse(self.config.exists())

    def test_existing_catalog_cannot_bypass_unbound_pending_pair(self):
        self.json(self.config, CATALOG)
        self.json(self.state, {"_pair": {"verification_required": True}})
        with self.assertRaisesRegex(ValueError, "pending pair"):
            self.migrate()
        self.json(self.state, {"_pair": {"verification_required": True,
                                       "catalog_digest": configure.schema.catalog_digest(CATALOG)}})
        self.assertTrue(self.migrate())

    def test_pending_transaction_blocks_terminal_catalog_change(self):
        self.save()
        before = self.config.read_bytes()
        self.json(self.state, {"rx": {"verification_required": True}})
        changed = copy.deepcopy(CATALOG)
        changed["pairs"][0]["label"] = "Revised"
        with self.assertRaisesRegex(ValueError, "unfinished"):
            self.save(changed)
        self.assertEqual(self.config.read_bytes(), before)

    def test_symlinks_wrong_owner_and_group_writable_sources_are_rejected(self):
        self.legacy()
        self.worker.chmod(0o664)
        with self.assertRaises(ValueError): self.migrate()
        self.worker.chmod(0o644)
        with self.assertRaises(ValueError):
            configure.migrate_installed(self.config, self.worker, self.state, self.owner + 1)
        target = self.root / "outside.py"
        self.worker.rename(target)
        self.worker.symlink_to(target)
        with self.assertRaises(OSError): self.migrate()
        self.assertFalse(self.config.exists())

    def test_invalid_existing_config_and_duplicate_keys_are_not_overwritten(self):
        self.legacy()
        self.config.write_text('{"schema":1,"schema":1}')
        self.config.chmod(0o640)
        before = self.config.read_bytes()
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            self.migrate()
        self.assertEqual(self.config.read_bytes(), before)

    def test_nonliteral_or_ambiguous_legacy_tables_are_not_executed(self):
        for source in ("PLAN = __import__('os').system('false')\nPAIR_PLANS = {}",
                       "PLAN = {}\nPLAN = {}\nPAIR_PLANS = {}"):
            with self.subTest(source=source), self.assertRaises(ValueError):
                configure.legacy_catalog(source)

    def test_legacy_default_and_tone_inconsistency_fail_without_configuration(self):
        self.legacy()
        for modification in (lambda value: value.replace("100.0", "67.0", 1),
                             lambda value: value.replace("145.300000", "145.330000")):
            with self.subTest(modification=modification), self.assertRaises(ValueError):
                configure.legacy_catalog(modification(self.worker.read_text()))
        self.assertFalse(self.config.exists())

    def test_saved_catalog_is_canonical_and_unchanged_repeat_does_not_replace_file(self):
        self.assertTrue(self.save())
        self.assertEqual(configure.schema.validate_catalog(json.loads(self.config.read_text())), CATALOG)
        stamp = self.config.stat().st_mtime_ns
        self.assertFalse(self.save())
        self.assertEqual(self.config.stat().st_mtime_ns, stamp)
        self.assertFalse(list(self.root.glob(".channel-pairs-*")))

    def test_failure_before_atomic_replace_preserves_existing_configuration(self):
        self.save()
        before = self.config.read_bytes()
        changed = copy.deepcopy(CATALOG)
        changed["pairs"][0]["label"] = "Changed"
        with mock.patch.object(configure.os, "fsync", side_effect=OSError("disk full")):
            with self.assertRaises(OSError): self.save(changed)
        self.assertEqual(self.config.read_bytes(), before)
        self.assertFalse(list(self.root.glob(".channel-pairs-*")))

    def test_interactive_review_can_cancel_or_save_numbered_pairs_without_file_writes(self):
        answers = ["1", "Local", "145.300", "433.500", "", "no"]
        with mock.patch("builtins.input", side_effect=answers):
            self.assertIsNone(configure.interactive(input, lambda *args: None))
        answers[-1] = "yes"
        with mock.patch("builtins.input", side_effect=answers):
            catalog = configure.interactive(input, lambda *args: None)
        self.assertEqual(catalog["pairs"][0]["id"], "pair1")
        self.assertIsNone(catalog["pairs"][0]["tone_hz"])
        self.assertFalse(self.config.exists())

    def test_frequency_parser_preserves_decimal_precision_and_rejects_bad_resolution(self):
        self.assertEqual(configure.parse_mhz("145.300010"), 145300010)
        for value in ("nan", "Infinity", "abc", "145.300001"):
            with self.subTest(value=value), self.assertRaises(ValueError): configure.parse_mhz(value)

    def test_numbered_names_fit_driver_limit_and_default_must_be_first(self):
        catalog = copy.deepcopy(CATALOG)
        catalog["pairs"] = [{**CATALOG["pairs"][0], "id": f"pair{index}"} for index in range(1, 10)]
        catalog["default_pair_id"] = "pair1"
        for pair in configure.schema.pair_plans(catalog).values():
            self.assertEqual(len(pair["rx_name"]), 7)
            self.assertEqual(len(pair["tx_name"]), 7)
        catalog["default_pair_id"] = "pair2"
        with self.assertRaises(ValueError): configure.schema.validate_catalog(catalog)

    def test_installer_migration_precedes_privileged_helper_replacement(self):
        source = (ROOT / "scripts/install-pi.sh").read_text()
        self.assertLess(source.index("module.migrate_installed("), source.index("{program-radio.py,radio_plan.py"))
        self.assertLess(source.index("flock -n 9"), source.index("module.migrate_installed("))


if __name__ == "__main__":
    unittest.main()
