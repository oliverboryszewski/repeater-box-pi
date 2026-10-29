"""Paired radio changes require current authorization and qualified hardware.

These checks use temporary state and a loopback HTTP server. They never open a
serial device or transmit, and the programming-session check uses mock radios.
"""
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
sys.path.insert(0, str(ROOT / "dashboard"))
sys.path.insert(0, str(ROOT / "scripts"))
from radio_test_catalog import CATALOG, DIGEST, PLAN, PAIR_PLANS
import controls
import radio_programming as programming
import test_radio_editor_api as editor_tests


def load_script(name):
    spec = importlib.util.spec_from_file_location(
        "pair_tests_" + name.replace("-", "_"), ROOT / "scripts" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


worker = load_script("program-radio")
qualification = load_script("configure-pair-switching")
CONFIG = copy.deepcopy(editor_tests.CONFIG)
PAIR_ID = "a" * 32
PASSWORD = "sample-local-admin"


def ready_state():
    return {role: {"inspected": True, "compatible": True, "verified": True,
                   "programmed": True, "verification_required": False,
                   "firmware": "HN5RV011", "identity": "fixture-" + role,
                   "device": "PRIVATE_DEVICE", "candidate_path": "PRIVATE_PATH"}
            for role in ("rx", "tx")}


def pair_record(**updates):
    value = {"pair_id": "rpt1", "request_id": PAIR_ID, "phase": "configured",
             "stored_verified": True, "activation_required": False, "catalog_digest": DIGEST,
             "verification_required": False,
             "roles": {role: {"written": True, "verified": True}
                       for role in ("rx", "tx")}}
    value.update(updates)
    return value


def make_controller(folder):
    config = folder / "radio-config.json"
    pair_config = folder / "pair-config.json"
    controls.write_json(config, CONFIG)
    controls.write_json(pair_config, {"schema": 1, "enabled": True})
    catalog = folder / "channel-pairs.json"
    controls.write_json(catalog, CATALOG)
    controller = programming.RadioProgramming(
        folder, supported=True, config_file=config, pair_config_file=pair_config,
        catalog_file=catalog, catalog_owner_uid=os.getuid(),
        runner=lambda args: "active" if args[-1].endswith(".path") else "inactive")
    controls.write_json(folder / controller.PUBLIC_STATE, ready_state())
    return controller


class PairAuthorizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.record = controls.password_record(PASSWORD)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.password_file = Path(temporary.name) / "admin.json"
        controls.write_json(self.password_file, self.record)
        self.auth = controls.AdminAuth(self.password_file)

    def test_reauthentication_checks_password_without_renewing_or_creating_sessions(self):
        with mock.patch.object(controls.time, "monotonic", return_value=1000):
            token, session = self.auth.login(PASSWORD, "test-address")
        original = copy.deepcopy(self.auth.sessions)
        with mock.patch.object(controls.time, "monotonic", return_value=2000):
            self.assertTrue(self.auth.reauthenticate(
                token, session["csrf_token"], PASSWORD, "test-address"))
        self.assertEqual(self.auth.sessions, original)
        self.assertEqual(len(self.auth.sessions), 1)
        self.assertNotIn(PASSWORD, json.dumps(self.auth.sessions))

    def test_reauthentication_requires_current_session_and_csrf_before_password_work(self):
        token, session = self.auth.login(PASSWORD, "test-address")
        for supplied_token, csrf in ((None, session["csrf_token"]),
                                     ("forged", session["csrf_token"]),
                                     (token, None), (token, "forged"), (token, 1)):
            with self.subTest(token=supplied_token, csrf=csrf), \
                    mock.patch.object(self.auth, "_check_password") as password_check:
                with self.assertRaises(PermissionError):
                    self.auth.reauthenticate(supplied_token, csrf, PASSWORD, "test-address")
                password_check.assert_not_called()
        self.auth.logout(token)
        with self.assertRaises(PermissionError):
            self.auth.reauthenticate(token, session["csrf_token"], PASSWORD, "test-address")

    def test_wrong_fresh_password_preserves_the_existing_session(self):
        token, session = self.auth.login(PASSWORD, "test-address")
        before = copy.deepcopy(self.auth.sessions)
        with self.assertRaisesRegex(PermissionError, "Incorrect"):
            self.auth.reauthenticate(token, session["csrf_token"], "wrong-password", "test-address")
        self.assertTrue(self.auth.authorize(token, session["csrf_token"]))
        self.assertEqual(self.auth.sessions, before)

    def test_login_and_fresh_password_share_source_throttle_and_expiry(self):
        with mock.patch.object(controls.time, "monotonic", return_value=1000):
            token, session = self.auth.login(PASSWORD, "test-address")
            with mock.patch.object(controls.hashlib, "pbkdf2_hmac", return_value=b"\0" * 32) as derive:
                for attempt in range(5):
                    with self.assertRaisesRegex(PermissionError, "Incorrect"):
                        if attempt % 2:
                            self.auth.login("wrong-password", "test-address")
                        else:
                            self.auth.reauthenticate(token, session["csrf_token"], "wrong-password", "test-address")
                for action in (lambda: self.auth.login(PASSWORD, "test-address"),
                               lambda: self.auth.reauthenticate(token, session["csrf_token"], PASSWORD, "test-address")):
                    with self.assertRaisesRegex(PermissionError, "Too many"):
                        action()
                self.assertEqual(derive.call_count, 5)
        with mock.patch.object(controls.time, "monotonic", return_value=1061):
            self.assertTrue(self.auth.reauthenticate(token, session["csrf_token"], PASSWORD, "test-address"))

    def test_fresh_password_failures_also_share_the_global_login_throttle(self):
        with mock.patch.object(controls.time, "monotonic", return_value=1000):
            token, session = self.auth.login(PASSWORD, "session-address")
            with mock.patch.object(controls.hashlib, "pbkdf2_hmac", return_value=b"\0" * 32) as derive:
                for number in range(30):
                    with self.assertRaisesRegex(PermissionError, "Incorrect"):
                        self.auth.reauthenticate(token, session["csrf_token"], "wrong-password", f"source-{number}")
                with self.assertRaisesRegex(PermissionError, "Too many"):
                    self.auth.login(PASSWORD, "new-address")
                self.assertEqual(derive.call_count, 30)

    def test_changed_password_record_invalidates_pair_reauthentication(self):
        token, session = self.auth.login(PASSWORD, "test-address")
        controls.write_json(self.password_file, controls.password_record(PASSWORD))
        with self.assertRaises(PermissionError):
            self.auth.reauthenticate(token, session["csrf_token"], PASSWORD, "test-address")
        self.assertFalse(self.auth.session(token)["authenticated"])


class PairControllerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.controller = make_controller(self.folder)

    def assert_no_request(self):
        self.assertFalse((self.folder / self.controller.REQUEST).exists())

    def test_catalog_comes_only_from_the_private_file_and_names_follow_its_order(self):
        state = self.controller.status()
        self.assertTrue(state["catalog_configured"])
        self.assertEqual(state["catalog_digest"], DIGEST)
        self.assertEqual(state["plan"], PLAN)
        presets = state["pair_switching"]["presets"]
        self.assertEqual([(item["rx_name"], item["tx_name"]) for item in presets],
                         [(f"BOX-RX{i}", f"BOX-TX{i}") for i in range(1, 7)])
        presets[0]["input_hz"] = 0
        self.assertEqual(self.controller.status()["pair_switching"]["presets"][0]["input_hz"], CATALOG["pairs"][0]["rx_hz"])

    def test_missing_invalid_or_untrusted_catalog_has_no_frequency_fallback(self):
        for mode in ("missing", "invalid", "world-readable", "symlink"):
            with self.subTest(mode=mode):
                path = self.controller.catalog_file
                path.unlink(missing_ok=True)
                if mode == "invalid":
                    controls.write_json(path, {"schema": 1})
                elif mode == "world-readable":
                    controls.write_json(path, CATALOG)
                    path.chmod(0o644)
                elif mode == "symlink":
                    target = self.folder / "catalog-target.json"
                    controls.write_json(target, CATALOG)
                    path.symlink_to(target)
                state = self.controller.status()
                self.assertFalse(state["catalog_configured"])
                self.assertIsNone(state["plan"])
                self.assertEqual(state["pair_switching"]["presets"], [])
                self.assertFalse(state["pair_switching"]["enabled"])
                with self.assertRaises(ValueError):
                    self.controller.request_pair("rpt1", catalog_digest=DIGEST)
                with self.assertRaises(ValueError):
                    self.controller.request("rx", "program", catalog_digest=DIGEST)
                self.assert_no_request()
        path.unlink()
        self.assertTrue(self.controller.request("rx", "inspect")["pending"])

    def test_changed_catalog_rejects_reviewed_program_and_pair_digests(self):
        changed = copy.deepcopy(CATALOG)
        changed["pairs"][1]["tone_hz"] = None
        controls.write_json(self.controller.catalog_file, changed)
        for operation in (lambda: self.controller.request("rx", "program", catalog_digest=DIGEST),
                          lambda: self.controller.request_pair("rpt1", catalog_digest=DIGEST)):
            with self.assertRaises(ValueError):
                operation()
            self.assert_no_request()

    def test_catalog_change_invalidates_a_saved_pair_without_dropping_its_interlock(self):
        saved = ready_state()
        saved["_pair"] = pair_record(verification_required=True)
        controls.write_json(self.folder / self.controller.PUBLIC_STATE, saved)
        changed = copy.deepcopy(CATALOG)
        changed["pairs"][1]["tone_hz"] = None
        controls.write_json(self.controller.catalog_file, changed)
        state = self.controller.status()
        self.assertTrue(state["pair"]["verification_required"])
        self.assertTrue(state["pair"]["catalog_changed"])
        self.assertFalse(state["pair"]["stored_verified"])
        self.assertIsNone(state["pair"]["plan"])
        with self.assertRaisesRegex(ValueError, "Restore"):
            self.controller.request_pair("rpt1", catalog_digest=state["catalog_digest"])
        self.assert_no_request()

    def test_pair_queue_contains_only_the_fixed_action_identifier_and_random_request_id(self):
        state = self.controller.request_pair("rpt3", catalog_digest=DIGEST)
        request = json.loads((self.folder / self.controller.REQUEST).read_text())
        self.assertEqual(set(request), {"request_id", "action", "pair_id", "catalog_digest"})
        self.assertRegex(request["request_id"], r"^[0-9a-f]{32}$")
        self.assertEqual((request["action"], request["pair_id"]), ("change-pair", "rpt3"))
        self.assertTrue(programming.valid_request(request))
        self.assertTrue(worker.valid_request(request))
        self.assertTrue(state["pending"])
        self.assertIsNone(state["role"])
        self.assertNotIn("PRIVATE", json.dumps(state))

    def test_pair_request_schema_rejects_extra_fields_and_malformed_identifiers(self):
        request = {"request_id": PAIR_ID, "action": "change-pair", "pair_id": "rpt1", "catalog_digest": DIGEST}
        for replacement in ({"request_id": True}, {"request_id": []}, {"request_id": "../image"},
                            {"pair_id": []}, {"pair_id": None}, {"pair_id": True},
                            {"pair_id": "INVALID"}, {"catalog_digest": "bad"},
                            {"password": "PRIVATE"}, {"role": "rx"}, {"port": "/dev/ttyUSB0"},
                            {"rx_hz": 145300000}, {"image": "/tmp/image"}):
            with self.subTest(replacement=replacement):
                value = dict(request, **replacement)
                self.assertFalse(programming.valid_request(value))
                self.assertFalse(worker.valid_request(value))

    def test_unknown_or_unplanned_pair_never_creates_a_queue_file(self):
        for pair_id in ("unlisted", "rpt6", "", [], {}, None, True, 1):
            with self.subTest(pair_id=pair_id), self.assertRaises(ValueError):
                self.controller.request_pair(pair_id, catalog_digest=DIGEST)
            self.assert_no_request()

    def test_preview_and_unavailable_helper_never_queue_pair_work(self):
        self.controller.supported = False
        self.assertFalse(self.controller.status()["pair_switching"]["enabled"])
        with self.assertRaises(ValueError):
            self.controller.request_pair("rpt1", catalog_digest=DIGEST)
        self.controller.supported = True
        self.controller.run = lambda args: "inactive"
        with self.assertRaises(ValueError):
            self.controller.request_pair("rpt1", catalog_digest=DIGEST)
        self.assert_no_request()

    def test_missing_disabled_or_corrupt_qualification_is_closed_by_default(self):
        for value in ({}, {"schema": 1, "enabled": False}, {"schema": True, "enabled": True},
                      {"schema": "1", "enabled": True}, {"schema": 1, "enabled": 1},
                      {"schema": 1, "enabled": "true"}, {"schema": 1, "enabled": True, "other": True},
                      [], None):
            with self.subTest(value=value):
                self.controller.pair_config_file.write_text(json.dumps(value))
                self.assertFalse(self.controller.status()["pair_switching"]["enabled"])
                with self.assertRaises(ValueError):
                    self.controller.request_pair("rpt1", catalog_digest=DIGEST)
                self.assert_no_request()
        self.controller.pair_config_file.unlink()
        with self.assertRaises(ValueError):
            self.controller.request_pair("rpt1", catalog_digest=DIGEST)
        self.assert_no_request()

    def test_both_supported_radios_are_required_before_pair_work(self):
        for role in ("rx", "tx"):
            for invalid in (False, None, "true", 1, []):
                with self.subTest(role=role, invalid=invalid):
                    value = ready_state()
                    value[role]["compatible"] = invalid
                    controls.write_json(self.folder / self.controller.PUBLIC_STATE, value)
                    with self.assertRaises(ValueError):
                        self.controller.request_pair("rpt1", catalog_digest=DIGEST)
                    self.assert_no_request()

    def test_unrelated_unfinished_uploads_block_pair_changes(self):
        for role in ("rx", "tx", "programmer"):
            value = ready_state()
            value[role] = dict(value.get(role, {}), verification_required=True)
            if role == "programmer":
                config = copy.deepcopy(CONFIG)
                config["devices"]["programmer"] = "/dev/serial/by-path/platform-pci-usb-0:1.4:1.0-port0"
                controls.write_json(self.controller.config_file, config)
                value["_pair"] = pair_record(verification_required=True)
            controls.write_json(self.folder / self.controller.PUBLIC_STATE, value)
            with self.subTest(role=role), self.assertRaisesRegex(ValueError, "separate unfinished"):
                self.controller.request_pair("rpt1", catalog_digest=DIGEST)
            self.assert_no_request()

    def test_owned_partial_pair_can_be_retried_without_discarding_its_record(self):
        saved = ready_state()
        saved["rx"]["verification_required"] = True
        saved["_pair"] = pair_record(phase="failed", verification_required=True, stored_verified=False)
        controls.write_json(self.folder / self.controller.PUBLIC_STATE, saved)
        state = self.controller.request_pair("rpt2", catalog_digest=DIGEST)
        self.assertTrue(state["pending"])
        self.assertEqual(json.loads((self.folder / self.controller.PUBLIC_STATE).read_text()), saved)
        self.assertTrue(state["pair"]["verification_required"])

    def test_active_or_queued_worker_blocks_a_second_pair_request(self):
        first = self.controller.request_pair("rpt1", catalog_digest=DIGEST)
        request_file = self.folder / self.controller.REQUEST
        original = request_file.read_bytes()
        with self.assertRaisesRegex(ValueError, "already pending"):
            self.controller.request_pair("rpt2", catalog_digest=DIGEST)
        self.assertEqual(request_file.read_bytes(), original)
        controls.write_json(self.folder / self.controller.RESULT, {"request_id": first["request_id"], "success": True})
        self.controller.run = lambda args: "active"
        with self.assertRaisesRegex(ValueError, "already pending"):
            self.controller.request_pair("rpt2", catalog_digest=DIGEST)
        self.assertEqual(request_file.read_bytes(), original)

    def test_saved_pair_projection_drops_private_fields_and_uses_the_fixed_plan(self):
        saved = ready_state()
        saved["_pair"] = pair_record(password="PRIVATE_PASSWORD", devices="PRIVATE_DEVICE",
            original="PRIVATE_BACKUP", resume_services=["PRIVATE_SERVICE"],
            plan={"rx_hz": 1, "tx_hz": 2},
            roles={"rx": {"written": True, "verified": True, "path": "PRIVATE_PATH"},
                   "tx": {"written": True, "verified": False, "raw": "PRIVATE_RAW"}})
        controls.write_json(self.folder / self.controller.PUBLIC_STATE, saved)
        state = self.controller.status()
        self.assertNotIn("PRIVATE", json.dumps(state))
        self.assertEqual(state["pair"]["plan"]["rx_hz"], 145330000)
        self.assertEqual(state["pair"]["plan"]["tx_hz"], 433525000)
        self.assertEqual(state["pair"]["roles"]["tx"], {"written": True, "verified": False})

    def test_corrupt_saved_pairs_and_boolean_lookalikes_are_not_trusted(self):
        for value in (None, [], True, {}, pair_record(pair_id=[]),
                      pair_record(pair_id="INVALID"), pair_record(request_id=[]),
                      pair_record(request_id="../candidate")):
            with self.subTest(value=value):
                self.assertIsNone(programming.public_pair(value, CATALOG))
        clean = programming.public_pair(pair_record(
            stored_verified=1, activation_required="true", verification_required=[],
            roles={"rx": {"written": 1, "verified": "true"}, "tx": True}), CATALOG)
        self.assertFalse(clean["stored_verified"])
        self.assertFalse(clean["activation_required"])
        self.assertFalse(clean["verification_required"])
        self.assertEqual(clean["roles"], {role: {"written": False, "verified": False} for role in ("rx", "tx")})

    def test_existing_single_radio_requests_still_work_with_pair_switching_disabled(self):
        self.controller.pair_config_file.unlink()
        state = self.controller.request("rx", "inspect")
        self.assertTrue(state["pending"])
        self.assertEqual(json.loads((self.folder / self.controller.REQUEST).read_text())["role"], "rx")


class PairQualificationTests(unittest.TestCase):
    def test_gate_requires_both_explicit_bench_confirmations(self):
        qualification.validate_qualification(ready_state(), True, True)
        for memory_one, live_reload in ((False, True), (True, False), (False, False),
                                        (1, True), (True, "yes"), (None, True)):
            with self.subTest(memory_one=memory_one, live_reload=live_reload), self.assertRaises(ValueError):
                qualification.validate_qualification(ready_state(), memory_one, live_reload)

    def test_gate_requires_supported_verified_radios_and_no_unfinished_role(self):
        for role in ("rx", "tx"):
            for key in ("compatible", "verified"):
                for invalid in (False, None, 1, "true", []):
                    value = ready_state()
                    value[role][key] = invalid
                    with self.subTest(role=role, key=key, invalid=invalid), self.assertRaises(ValueError):
                        qualification.validate_qualification(value, True, True)
        for role in ("rx", "tx", "programmer", "_pair"):
            value = ready_state()
            value[role] = dict(value.get(role, {}), verification_required=True)
            with self.subTest(role=role), self.assertRaisesRegex(ValueError, "unfinished"):
                qualification.validate_qualification(value, True, True)
        for value in (None, [], {}, {"rx": True, "tx": {}}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                qualification.validate_qualification(value, True, True)

    def test_qualification_is_bound_to_the_current_supported_aioc_assignments(self):
        state = ready_state()
        for role in ("rx", "tx"):
            state[role]["device"] = CONFIG["devices"][role]
        qualification.validate_qualification(state, True, True, CONFIG)
        for role in ("rx", "tx"):
            for stale in (None, "other-device", CONFIG["devices"]["tx" if role == "rx" else "rx"]):
                value = copy.deepcopy(state)
                value[role]["device"] = stale
                with self.subTest(role=role, stale=stale), self.assertRaises(ValueError):
                    qualification.validate_qualification(value, True, True, CONFIG)
        # The CLI checks the complete worker schema before calling this helper;
        # its own guard must still reject malformed binding containers.
        for config in ([], True, "5RX", {}, {"devices": []},
                       {"devices": {"rx": CONFIG["devices"]["rx"]}}):
            with self.subTest(config=config), self.assertRaises(ValueError):
                qualification.validate_qualification(state, True, True, config)


class PairProgrammingSessionTests(unittest.TestCase):
    def test_upload_is_released_then_quiet_before_a_fresh_download_and_final_release(self):
        hardware = worker.Hardware.__new__(worker.Hardware)
        upload = types.SimpleNamespace(pipe="upload-session")
        observed = types.SimpleNamespace(pipe="verification-session")
        events = []
        hardware.release = lambda radio: events.append(("release", radio.pipe))
        def download(device, progress):
            self.assertIsNone(upload.pipe)
            events.append(("download", device))
            return observed
        hardware.download = download
        hardware.readback = mock.Mock(side_effect=AssertionError("Do not reuse the upload clone session"))
        def progress(phase, message, reset):
            events.append(("progress", phase))
        with mock.patch.object(worker.time, "sleep", side_effect=lambda seconds: events.append(("quiet", seconds))):
            result = hardware.settle_and_verify("fixture-device", upload, progress)
        self.assertIs(result, observed)
        self.assertEqual(events, [("release", "upload-session"), ("progress", "settling"),
                                  ("quiet", 5), ("progress", "verifying"),
                                  ("download", "fixture-device"), ("release", "verification-session")])
        self.assertIsNone(result.pipe)
        hardware.readback.assert_not_called()

    def test_failed_fresh_download_does_not_reuse_or_leave_open_the_upload_session(self):
        hardware = worker.Hardware.__new__(worker.Hardware)
        upload = types.SimpleNamespace(pipe="upload-session")
        hardware.release = mock.Mock()
        hardware.download = mock.Mock(side_effect=worker.ProgrammingError("Unplugged"))
        hardware.readback = mock.Mock()
        with mock.patch.object(worker.time, "sleep") as quiet:
            with self.assertRaisesRegex(worker.ProgrammingError, "Unplugged"):
                hardware.settle_and_verify("fixture-device", upload, mock.Mock())
        quiet.assert_called_once_with(5)
        self.assertIsNone(upload.pipe)
        hardware.release.assert_called_once_with(upload)
        hardware.readback.assert_not_called()


class PairHTTPTests(unittest.TestCase):
    # Reuse the loopback fixture, not the EditorHTTPTests inheritance tree.
    setUpClass = classmethod(editor_tests.EditorHTTPTests.setUpClass.__func__)
    tearDown = editor_tests.EditorHTTPTests.tearDown
    request = editor_tests.EditorHTTPTests.request
    login = editor_tests.EditorHTTPTests.login

    def setUp(self):
        editor_tests.EditorHTTPTests.setUp(self)
        self.server.programming = make_controller(self.state)

    def test_current_session_csrf_and_same_origin_are_required_before_pair_queue(self):
        body = {"pair_id": "rpt1", "password": PASSWORD, "catalog_digest": DIGEST}
        with mock.patch.object(self.server.programming, "request_pair") as queue:
            self.assertEqual(self.request("/api/radio-pair", "POST", body)[0], 403)
            self.login()
            self.assertEqual(self.request("/api/radio-pair", "POST", body, headers={"Cookie": self.cookie})[0], 403)
            self.assertEqual(self.request("/api/radio-pair", "POST", body, True,
                                          {"X-CSRF-Token": "forged"})[0], 403)
            self.assertEqual(self.request("/api/radio-pair", "POST", body, True,
                                          {"Origin": "http://elsewhere.invalid"})[0], 403)
            queue.assert_not_called()

    def test_password_is_required_for_every_pair_request_and_never_enters_state_or_response(self):
        self.login()
        with mock.patch.object(editor_tests.dashboard.Handler, "log_message") as log:
            code, headers, body = self.request("/api/radio-pair", "POST", {"pair_id": "rpt1", "password": PASSWORD, "catalog_digest": DIGEST}, True)
        self.assertEqual(code, 202)
        self.assertNotIn("Set-Cookie", headers)
        request = json.loads((self.state / self.server.programming.REQUEST).read_text())
        self.assertEqual(set(request), {"request_id", "action", "pair_id", "catalog_digest"})
        self.assertEqual(request["pair_id"], "rpt1")
        self.assertNotIn(PASSWORD, body.decode())
        self.assertNotIn(PASSWORD, repr(log.call_args_list))
        for path in self.state.glob("*.json"):
            self.assertNotIn(PASSWORD, path.read_text(), path.name)
        controls.write_json(self.state / self.server.programming.RESULT, {"request_id": request["request_id"], "success": True})
        self.assertEqual(self.request("/api/radio-pair", "POST", {"pair_id": "rpt2"}, True)[0], 400)

    def test_wrong_password_is_400_keeps_session_and_does_not_queue(self):
        self.login()
        with mock.patch.object(self.server.programming, "request_pair") as queue:
            for password in ("wrong-password", "", None, 123, []):
                with self.subTest(password=password):
                    code, _, body = self.request("/api/radio-pair", "POST", {"pair_id": "rpt1", "password": password, "catalog_digest": DIGEST}, True)
                    self.assertEqual(code, 400)
                    self.assertNotIn("wrong-password", body.decode())
            queue.assert_not_called()
        code, _, body = self.request("/api/session", headers={"Cookie": self.cookie})
        self.assertEqual(code, 200)
        self.assertTrue(json.loads(body)["authenticated"])
        self.assertFalse((self.state / self.server.programming.REQUEST).exists())

    def test_repeated_wrong_fresh_password_is_rate_limited_without_queuing(self):
        self.login()
        with mock.patch.object(controls.hashlib, "pbkdf2_hmac", return_value=b"\0" * 32) as derive, \
                mock.patch.object(self.server.programming, "request_pair") as queue:
            for _ in range(5):
                code, _, body = self.request("/api/radio-pair", "POST", {"pair_id": "rpt1", "password": "wrong-password", "catalog_digest": DIGEST}, True)
                self.assertEqual(code, 400)
                self.assertIn("Incorrect", json.loads(body)["error"])
            code, _, body = self.request("/api/radio-pair", "POST", {"pair_id": "rpt1", "password": PASSWORD, "catalog_digest": DIGEST}, True)
            self.assertEqual(code, 400)
            self.assertIn("Too many", json.loads(body)["error"])
            self.assertEqual(derive.call_count, 5)
            queue.assert_not_called()

    def test_http_pair_schema_rejects_extra_fields_and_unknown_pairs(self):
        self.login()
        base = {"pair_id": "rpt1", "password": PASSWORD, "catalog_digest": DIGEST}
        for value in ({"pair_id": "rpt1"}, {"password": PASSWORD, "catalog_digest": DIGEST},
                      dict(base, role="rx"), dict(base, command="program"),
                      dict(base, port="/dev/ttyUSB0"), dict(base, rx_hz=145300000),
                      dict(base, pair_id="unlisted"), dict(base, pair_id=[]), dict(base, pair_id=None)):
            with self.subTest(value=value):
                self.assertEqual(self.request("/api/radio-pair", "POST", value, True)[0], 400)
                self.assertFalse((self.state / self.server.programming.REQUEST).exists())

    def test_pending_radio_software_and_wifi_work_prevent_reauthentication_and_queue(self):
        self.login()
        for controller in (self.server.programming, self.server.updates, self.server.wifi):
            with self.subTest(controller=type(controller).__name__), \
                    mock.patch.object(controller, "status", return_value={"pending": True}), \
                    mock.patch.object(self.server.auth, "reauthenticate") as reauthenticate, \
                    mock.patch.object(self.server.programming, "request_pair") as queue:
                self.assertEqual(self.request("/api/radio-pair", "POST", {"pair_id": "rpt1", "password": PASSWORD, "catalog_digest": DIGEST}, True)[0], 409)
                reauthenticate.assert_not_called()
                queue.assert_not_called()

    def test_preview_or_unqualified_pi_never_queues_even_with_the_correct_password(self):
        self.login()
        self.server.programming.supported = False
        self.assertEqual(self.request("/api/radio-pair", "POST", {"pair_id": "rpt1", "password": PASSWORD, "catalog_digest": DIGEST}, True)[0], 400)
        self.server.programming.supported = True
        self.server.programming.pair_config_file.unlink()
        self.assertEqual(self.request("/api/radio-pair", "POST", {"pair_id": "rpt1", "password": PASSWORD, "catalog_digest": DIGEST}, True)[0], 400)
        self.assertFalse((self.state / self.server.programming.REQUEST).exists())

    def test_http_pair_rejects_missing_stale_or_malformed_catalog_digests(self):
        self.login()
        for digest in (None, "bad", "0" * 64):
            body = {"pair_id": "rpt1", "password": PASSWORD}
            if digest is not None:
                body["catalog_digest"] = digest
            self.assertEqual(self.request("/api/radio-pair", "POST", body, True)[0], 400)
            self.assertFalse((self.state / self.server.programming.REQUEST).exists())

    def test_http_default_program_requires_and_queues_the_reviewed_catalog_digest(self):
        self.login()
        for digest in (None, "bad", "0" * 64):
            body = {"role": "rx", "action": "program"}
            if digest is not None:
                body["catalog_digest"] = digest
            self.assertEqual(self.request("/api/radio-programming", "POST", body, True)[0], 400)
            self.assertFalse((self.state / self.server.programming.REQUEST).exists())
        code, _, _ = self.request("/api/radio-programming", "POST", {
            "role": "rx", "action": "program", "catalog_digest": DIGEST}, True)
        self.assertEqual(code, 202)
        queued = json.loads((self.state / self.server.programming.REQUEST).read_text())
        self.assertEqual(queued["catalog_digest"], DIGEST)
        self.assertTrue(worker.valid_request(queued))

    def test_single_radio_endpoint_does_not_require_the_new_pair_password_field(self):
        self.login()
        code, _, _ = self.request("/api/radio-programming", "POST", {"role": "rx", "action": "inspect"}, True)
        self.assertEqual(code, 202)
        value = json.loads((self.state / self.server.programming.REQUEST).read_text())
        self.assertEqual(set(value), {"request_id", "role", "action"})


if __name__ == "__main__":
    unittest.main()
