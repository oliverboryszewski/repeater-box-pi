"""Private channel-catalog validation; fixture values are laboratory-only."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
from radio_plan import validate_catalog, catalog_digest, pair_plans, default_plan
from radio_test_catalog import CATALOG, DIGEST
spec = importlib.util.spec_from_file_location("private_catalog_worker", ROOT / "scripts/program-radio.py")
w = importlib.util.module_from_spec(spec); spec.loader.exec_module(w)


class PrivateCatalogTests(unittest.TestCase):
    def test_canonical_catalog_digest_and_numbered_radio_names(self):
        canonical = validate_catalog(CATALOG)
        self.assertEqual(catalog_digest(canonical), DIGEST)
        self.assertEqual(catalog_digest(json.loads(json.dumps(canonical, sort_keys=True))), DIGEST)
        self.assertEqual([item["rx_name"] for item in pair_plans(canonical).values()],
                         [f"BOX-RX{n}" for n in range(1, 7)])
        self.assertEqual([item["tx_name"] for item in pair_plans(canonical).values()],
                         [f"BOX-TX{n}" for n in range(1, 7)])
        self.assertEqual(default_plan(canonical)["rx"]["name"], "BOX-RX1")
        self.assertEqual(default_plan(canonical)["tx"]["name"], "BOX-TX1")
        self.assertNotIn("rx_name", canonical["pairs"][0])

    def test_missing_empty_or_extra_schema_has_no_fallback(self):
        for value in (None, {}, {**CATALOG, "schema": True}, {**CATALOG, "pairs": []},
                      {**CATALOG, "pairs": CATALOG["pairs"]*2}, {**CATALOG, "region": "anything"}):
            with self.subTest(value=value), self.assertRaises(ValueError): validate_catalog(value)
        self.assertFalse(hasattr(w, "PLAN")); self.assertFalse(hasattr(w, "PAIR_PLANS"))

    def test_default_is_first_and_ids_unique(self):
        invalid = copy.deepcopy(CATALOG); invalid["default_pair_id"] = "rpt1"
        with self.assertRaises(ValueError): validate_catalog(invalid)
        invalid = copy.deepcopy(CATALOG); invalid["pairs"][1]["id"] = invalid["pairs"][0]["id"]
        with self.assertRaises(ValueError): validate_catalog(invalid)
        for identifier in ("../radio", "Upper", "", "a"*17, 1):
            invalid = copy.deepcopy(CATALOG); invalid["pairs"][1]["id"] = identifier
            with self.subTest(identifier=identifier), self.assertRaises(ValueError): validate_catalog(invalid)

    def test_frequencies_labels_and_tones_fail_closed(self):
        invalid_fields = (("rx_hz", True), ("rx_hz", "145300000"), ("rx_hz", 145300001),
                          ("rx_hz", 148000000), ("tx_hz", 450000000), ("tx_hz", 145300000),
                          ("label", " Leading"), ("label", "Line\nbreak"), ("label", "é"),
                          ("tone_hz", True), ("tone_hz", 88.6), ("tone_hz", float("nan")))
        for field, value in invalid_fields:
            invalid = copy.deepcopy(CATALOG); invalid["pairs"][0][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError): validate_catalog(invalid)

    def test_tone_off_and_nine_pairs_are_supported_without_extra_defaults(self):
        value = copy.deepcopy(CATALOG); value["pairs"] = [dict(value["pairs"][0], id=f"pair{n}", tone_hz=None) for n in range(1,10)]
        value["default_pair_id"] = "pair1"
        canonical = validate_catalog(value)
        self.assertEqual(pair_plans(canonical)["pair9"]["rx_name"], "BOX-RX9")
        plan = default_plan(canonical)
        self.assertIsNone(plan["rx"]["receive_tone_hz"])
        self.assertIsNone(plan["tx"]["transmit_tone_hz"])

    def test_every_operating_or_name_catalog_change_changes_digest(self):
        for field, value in (("label", "Changed label"), ("rx_hz", 145310000), ("tx_hz", 433510000), ("tone_hz", None)):
            changed = copy.deepcopy(CATALOG); changed["pairs"][0][field] = value
            self.assertNotEqual(catalog_digest(changed), DIGEST)
        reordered = copy.deepcopy(CATALOG); reordered["pairs"][1:3] = reversed(reordered["pairs"][1:3])
        self.assertNotEqual(catalog_digest(reordered), DIGEST)

    def test_public_pair_claim_is_invalidated_after_catalog_change_or_migration(self):
        stored = {"pair_id": "rpt1", "request_id": "a"*32, "phase": "configured", "stored_verified": True,
                  "verification_required": False, "catalog_digest": DIGEST}
        self.assertTrue(w.public_pair(stored, CATALOG)["stored_verified"])
        changed = copy.deepcopy(CATALOG); changed["pairs"][1]["label"] = "Changed"
        result = w.public_pair(stored, changed)
        self.assertFalse(result["stored_verified"]); self.assertTrue(result["catalog_changed"])
        self.assertIsNone(result["plan"])
        legacy = dict(stored); legacy.pop("catalog_digest")
        self.assertFalse(w.public_pair(legacy, CATALOG)["stored_verified"])
        self.assertTrue(w.public_pair(legacy, CATALOG)["catalog_changed"])

    def test_config_file_is_private_optional_and_digest_checks_precede_hardware(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); state, backups = root/"state",root/"backups"; state.mkdir(); backups.mkdir()
            catalog = root/"channel-pairs.json"
            config = {"schema": 1,"model":"5RX","devices":{
                "rx":"/dev/serial/by-id/usb-AIOC_All-In-One-Cable_test_rx-if04",
                "tx":"/dev/serial/by-id/usb-AIOC_All-In-One-Cable_test_tx-if04"}}
            request = {"request_id":"a"*32,"role":"rx","action":"program","catalog_digest":DIGEST}
            missing = w.Worker(None,state,backups,owner_uid=os.getuid(),catalog_file=catalog,service=lambda *args:False)
            self.assertIsNone(missing.catalog)
            self.assertFalse(missing.run(request,config)["success"])
            w.atomic_json(catalog,CATALOG)
            loaded = w.Worker(None,state,backups,owner_uid=os.getuid(),catalog_file=catalog,service=lambda *args:False)
            self.assertEqual(loaded.catalog,CATALOG)
            self.assertFalse(loaded.run({**request,"request_id":"b"*32,"catalog_digest":"f"*64},config)["success"])
            for mode in (0o644, 0o666):
                catalog.chmod(mode)
                invalid = w.Worker(None,state,backups,owner_uid=os.getuid(),catalog_file=catalog,service=lambda *args:False)
                self.assertIsNone(invalid.catalog)


if __name__ == "__main__": unittest.main()
