"""Editor data is authenticated and every draft is bound to a worker snapshot."""
import http.client
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "dashboard"))
from controls import password_record, write_json
from radio_editor import public_snapshot, valid_write
from radio_programming import RadioProgramming, public_role, valid_config, valid_request
import server as dashboard

CONFIG = {"schema": 1, "model": "5RX", "devices": {
    "rx": "/dev/serial/by-id/usb-AIOC_All-In-One-Cable_test_rx-if04",
    "tx": "/dev/serial/by-id/usb-AIOC_All-In-One-Cable_test_tx-if04"}}


def snapshot(role="rx"):
    return {"schema": 1, "role": role, "base_id": "a" * 32,
            "read_at": "2026-01-01T00:00:00+00:00", "model": "5RX",
            "firmware": "TEST", "compatible": True,
            "recovery": {"required": False, "pending_id": None, "protected_mismatch": False,
                         "message": "", "differences": []},
            "memories": [{"number": number, "empty": True} for number in range(128)],
            "settings": {"settings.beep": False, "settings.autolk": True},
            "capabilities": {"bands_hz": [[108000000, 136000000], [136000000, 174000000]],
                "tx_bands_hz": [[144000000, 148000000]], "name_length": 7,
                "valid_characters": "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 -",
                "ctcss": [67.0, 88.5], "dcs": [23, 25],
                "settings": {"settings.beep": {"label": "Beep", "type": "boolean"},
                             "settings.autolk": {"label": "Automatic keypad lock", "type": "boolean"}}}}


ROW = {"number": 1, "empty": False, "name": "TEST", "rx_hz": 145300000,
       "tx_hz": None, "mode": "NFM", "power": "Low", "skip": False,
       "tx_tone": None, "rx_tone": {"type": "CTCSS", "value": 88.5}}


class EditorValidationTests(unittest.TestCase):
    def test_external_models_are_fixed_and_box_roles_remain_5rx(self):
        value = snapshot("programmer")
        value["model"] = "UV5R"
        self.assertEqual(public_snapshot(value, "programmer")["model"], "UV5R")
        for role in ("rx", "tx"):
            value["role"] = role
            self.assertIsNone(public_snapshot(value, role))
        value["role"] = "programmer"
        for model in ("UV-82", "chirp.drivers.uv5r", "../../driver", False, None):
            value["model"] = model
            self.assertIsNone(public_snapshot(value, "programmer"))
        value["model"] = "UV5R"
        value["settings"]["settings.pttid"] = "Off"
        self.assertIsNone(public_snapshot(value, "programmer"))
        value["settings"].pop("settings.pttid")
        value["capabilities"]["settings"]["settings.pttid"] = {"label": "PTT ID", "type": "select", "options": ["Off"]}
        self.assertIsNone(public_snapshot(value, "programmer"))

    def test_model_is_only_accepted_for_external_reads(self):
        original = {"request_id": "c" * 32, "role": "programmer", "action": "editor-read"}
        self.assertTrue(valid_request(original))
        for model in ("5RX", "UV5R"):
            self.assertTrue(valid_request({**original, "model": model}))
        for value in ({**original, "model": "../driver"},
                      {**original, "model": "UV5R", "path": "/dev/ttyUSB0"},
                      {**original, "model": "UV5R", "role": "rx"},
                      {**original, "model": "5RX", "role": "tx"},
                      {**original, "model": "UV5R", "action": "verify"},
                      {**original, "model": "UV5R", "action": "repair-upload", "pending_id": "d" * 32}):
            self.assertFalse(valid_request(value))
        self.assertEqual(public_role({})["model"], "5RX")
        self.assertEqual(public_role({"model": "UV5R"})["model"], "UV5R")
        self.assertIsNone(public_role({"model": "unknown"})["model"])

    def test_snapshot_is_explicit_and_excludes_private_metadata(self):
        value = snapshot()
        value.update(device="/dev/PRIVATE", backup_id="PRIVATE", raw_image="PRIVATE")
        value["capabilities"]["private"] = "PRIVATE"
        clean = public_snapshot(value, "rx")
        self.assertNotIn("PRIVATE", json.dumps(clean))
        self.assertEqual(len(clean["memories"]), 128)
        clean["memories"][0]["number"] = 99
        self.assertEqual(value["memories"][0]["number"], 0)

    def test_snapshot_rejects_partial_corrupt_or_wrong_role_data(self):
        for field, value in (("schema", True), ("memories", []), ("base_id", "../../image"),
                             ("role", "tx"), ("settings", {"service.calibration": 1})):
            with self.subTest(field=field):
                original = snapshot()
                original[field] = value
                self.assertIsNone(public_snapshot(original, "rx"))
        original = snapshot()
        original["memories"][1] = original["memories"][0]
        self.assertIsNone(public_snapshot(original, "rx"))

    def test_write_schema_rejects_extra_paths_unsafe_values_and_duplicates(self):
        self.assertTrue(valid_write("a" * 32, [ROW], {"settings.beep": True}))
        self.assertTrue(valid_write("a" * 32, [], {"settings.autolk": True}))
        self.assertFalse(valid_write("a" * 32, [], {"wmchannel.mrcha": 1}))
        self.assertFalse(valid_write("a" * 32, [], {"wmchannel.mrchb": 1}))
        self.assertTrue(valid_write("a" * 32, [], {}, "b" * 32))
        self.assertFalse(valid_write("a" * 32, [], {}, "../candidate"))
        for base, changes, settings in (("../file", [ROW], {}), ("a" * 32, [], {}),
                ("a" * 32, [ROW] * 2, {}), ("a" * 32, [dict(ROW, number=True)], {}),
                ("a" * 32, [dict(ROW, raw_image="path")], {}),
                ("a" * 32, [dict(ROW, rx_hz=145.3)], {}),
                ("a" * 32, [dict(ROW, name="x" * 8)], {}),
                ("a" * 32, [dict(ROW, tx_tone={"type": "CTCSS", "value": float("nan")})], {}),
                ("a" * 32, [dict(ROW, tx_tone={"type": "CTCSS", "value": 10 ** 1000})], {}),
                ("a" * 32, [ROW], {"port": "/dev/ttyACM0"})):
            with self.subTest(base=base, changes=changes, settings=settings):
                self.assertFalse(valid_write(base, changes, settings))

    def test_recovery_projection_and_requests_are_bounded(self):
        value = snapshot()
        value["recovery"] = {"required": True, "pending_id": "b" * 32, "protected_mismatch": False,
            "message": "x" * 2000, "private_path": "/PRIVATE",
            "differences": [{"field": "name" * 100, "expected": "x" * 200,
                             "actual": "y" * 200, "private": "PRIVATE"}] * 30}
        clean = public_snapshot(value, "rx")
        self.assertNotIn("PRIVATE", json.dumps(clean))
        self.assertEqual(len(clean["recovery"]["message"]), 1000)
        self.assertEqual(len(clean["recovery"]["differences"]), 20)
        self.assertEqual(len(clean["recovery"]["differences"][0]["field"]), 100)
        request = {"request_id": "a" * 32, "role": "rx", "action": "editor-write",
                   "base_id": "c" * 32, "changes": [], "settings": {}, "supersede_id": "b" * 32}
        self.assertTrue(valid_request(request))
        for supersede in (None, False, "../candidate"):
            self.assertFalse(valid_request({**request, "supersede_id": supersede}))
        self.assertFalse(valid_request({"request_id": "a" * 32, "role": "programmer", "action": "program"}))

    def test_optional_programmer_config_accepts_only_unique_local_by_id_names(self):
        devices = {**CONFIG["devices"], "programmer": "/dev/serial/by-id/usb-1a86_USB_Serial-if00-port0"}
        self.assertTrue(valid_config({**CONFIG, "devices": devices}))
        self.assertTrue(valid_config({**CONFIG, "devices": {**devices,
            "programmer": "/dev/serial/by-path/platform-pci-usb-0:1:1.0-port0"}}))
        for port in ("/dev/ttyUSB0", "/tmp/file", "/dev/serial/by-id/../../image", CONFIG["devices"]["rx"]):
            with self.subTest(port=port):
                self.assertFalse(valid_config({**CONFIG, "devices": {**devices, "programmer": port}}))


class EditorControllerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self.config = self.state / "config.json"
        write_json(self.config, CONFIG)
        self.controller = RadioProgramming(self.state, supported=True, config_file=self.config,
            runner=lambda args: "active" if args[-1].endswith(".path") else "inactive")
        write_json(self.state / "radio-editor-rx.json", snapshot())

    def external(self, model="UV5R", **state):
        config = {**CONFIG, "devices": {**CONFIG["devices"],
                  "programmer": "/dev/serial/by-id/usb-external-if00-port0"}}
        write_json(self.config, config)
        value = snapshot("programmer")
        value["model"] = model
        write_json(self.state / "radio-editor-programmer.json", value)
        write_json(self.state / self.controller.PUBLIC_STATE, {"programmer": {"model": model, **state}})
        return value

    def test_external_read_queues_model_and_keeps_devices_local(self):
        self.external()
        state = self.controller.request_editor("programmer", "editor-read", model="UV5R")
        request = json.loads((self.state / self.controller.REQUEST).read_text())
        self.assertEqual(set(request), {"request_id", "role", "action", "model"})
        self.assertEqual(request["model"], "UV5R")
        self.assertEqual(state["supported_models"], {"rx": ["5RX"], "tx": ["5RX"], "programmer": ["5RX", "UV5R"]})
        self.assertNotIn("/dev/", json.dumps(state))

    def test_external_snapshot_filter_cannot_relabel_an_old_read(self):
        self.external()
        self.assertEqual(self.controller.editor_snapshot("programmer", "UV5R")["model"], "UV5R")
        self.assertIsNone(self.controller.editor_snapshot("programmer", "5RX"))
        self.assertEqual(self.controller.editor_snapshot("programmer")["model"], "UV5R")
        for role, model in (("rx", "5RX"), ("tx", "UV5R"), ("programmer", "../driver")):
            with self.subTest(role=role, model=model), self.assertRaises(ValueError):
                self.controller.editor_snapshot(role, model)

    def test_external_pending_model_cannot_change_until_resolved(self):
        self.external(verification_required=True, pending_id="b" * 32)
        for model in (None, "5RX"):
            with self.subTest(model=model), self.assertRaisesRegex(ValueError, "unfinished"):
                self.controller.request_editor("programmer", "editor-read", model=model)
        self.assertFalse((self.state / self.controller.REQUEST).exists())
        result = self.controller.request_editor("programmer", "editor-read", model="UV5R")
        self.assertTrue(result["roles"]["programmer"]["verification_required"])

    def test_external_write_uses_read_model_and_its_supported_settings(self):
        self.external()
        result = self.controller.request_editor("programmer", "editor-write", "a" * 32, [ROW], {})
        request = json.loads((self.state / self.controller.REQUEST).read_text())
        self.assertNotIn("model", request)
        self.assertTrue(result["pending"])
        (self.state / self.controller.REQUEST).unlink()
        with self.assertRaisesRegex(ValueError, "supported"):
            self.controller.request_editor("programmer", "editor-write", "a" * 32, [], {"settings.pttid": "Off"})
        write_json(self.state / self.controller.PUBLIC_STATE, {"programmer": {"model": "5RX"}})
        with self.assertRaisesRegex(ValueError, "model changed"):
            self.controller.request_editor("programmer", "editor-write", "a" * 32, [ROW], {})

    def test_profile_choice_is_rejected_for_station_or_write(self):
        for role, action, model in (("rx", "editor-read", "UV5R"), ("tx", "editor-read", "5RX"),
                                    ("programmer", "editor-read", "path.driver"), ("programmer", "editor-write", "UV5R")):
            with self.subTest(role=role, action=action), self.assertRaises(ValueError):
                self.controller.request_editor(role, action, "a" * 32, [ROW], {}, model=model)

    def test_editor_requests_share_programming_queue_and_keep_draft_out_of_status(self):
        state = self.controller.request_editor("rx", "editor-write", "a" * 32, [ROW], {})
        value = json.loads((self.state / self.controller.REQUEST).read_text())
        self.assertTrue(valid_request(value))
        self.assertEqual(value["changes"], [ROW])
        self.assertTrue(state["pending"])
        self.assertNotIn("changes", state)
        with self.assertRaises(ValueError):
            self.controller.request("tx", "inspect")

    def test_stale_draft_cannot_write_but_other_pending_radio_does_not_block_editor(self):
        with self.assertRaisesRegex(ValueError, "stale"):
            self.controller.request_editor("rx", "editor-write", "b" * 32, [ROW], {})
        write_json(self.state / self.controller.PUBLIC_STATE, {"tx": {"verification_required": True}})
        state = self.controller.request_editor("rx", "editor-write", "a" * 32, [ROW], {})
        self.assertTrue(state["roles"]["tx"]["verification_required"])

    def test_editor_reads_remain_available_without_changing_pending_candidate_state(self):
        saved = {"rx": {"verification_required": True, "backup_id": "b" * 32}}
        write_json(self.state / self.controller.PUBLIC_STATE, saved)
        for role in ("rx", "tx"):
            with self.subTest(role=role):
                state = self.controller.request_editor(role, "editor-read")
                self.assertTrue(state["pending"])
                self.assertTrue(state["roles"]["rx"]["verification_required"])
                self.assertEqual(json.loads((self.state / self.controller.PUBLIC_STATE).read_text()), saved)
                write_json(self.state / self.controller.RESULT, {"request_id": state["request_id"], "success": True})
        for action in ("inspect", "program"):
            with self.subTest(action=action), self.assertRaisesRegex(ValueError, "unfinished"):
                self.controller.request("tx", action, **({"catalog_digest": "a" * 64} if action == "program" else {}))

    def test_recovery_requires_fresh_read_and_exact_confirmed_pending_id(self):
        write_json(self.state / self.controller.PUBLIC_STATE, {"rx": {"verification_required": True}})
        with self.assertRaisesRegex(ValueError, "explicitly confirm"):
            self.controller.request_editor("rx", "editor-write", "a" * 32, [], {}, "b" * 32)
        value = snapshot()
        value["recovery"].update(required=True, pending_id="b" * 32)
        write_json(self.state / "radio-editor-rx.json", value)
        for supersede in (None, "c" * 32):
            with self.subTest(supersede=supersede), self.assertRaisesRegex(ValueError, "explicitly confirm"):
                self.controller.request_editor("rx", "editor-write", "a" * 32, [ROW], {}, supersede)
        state = self.controller.request_editor("rx", "editor-write", "a" * 32, [], {}, "b" * 32)
        request = json.loads((self.state / self.controller.REQUEST).read_text())
        self.assertTrue(valid_request(request))
        self.assertEqual(request["supersede_id"], "b" * 32)
        self.assertEqual(request["changes"], [])
        self.assertTrue(state["roles"]["rx"]["verification_required"])
        self.assertNotIn("supersede_id", state)

    def test_recovery_never_adopts_protected_mismatch_or_resolved_candidate(self):
        value = snapshot()
        value["recovery"].update(required=True, pending_id="b" * 32, protected_mismatch=True)
        write_json(self.state / "radio-editor-rx.json", value)
        write_json(self.state / self.controller.PUBLIC_STATE, {"rx": {"verification_required": True}})
        with self.assertRaisesRegex(ValueError, "Protected"):
            self.controller.request_editor("rx", "editor-write", "a" * 32, [], {}, "b" * 32)
        write_json(self.state / self.controller.PUBLIC_STATE, {})
        with self.assertRaisesRegex(ValueError, "pending upload changed"):
            self.controller.request_editor("rx", "editor-write", "a" * 32, [], {}, "b" * 32)
        self.assertFalse((self.state / self.controller.REQUEST).exists())

    def test_optional_programmer_uses_local_binding_and_only_editor_or_verify(self):
        self.assertNotIn("programmer", self.controller.status()["roles"])
        with self.assertRaisesRegex(ValueError, "Configure"):
            self.controller.request_editor("programmer", "editor-read")
        configured = {**CONFIG, "devices": {**CONFIG["devices"],
            "programmer": "/dev/serial/by-id/usb-FTDI_Radio_Programming_Cable-if00-port0"}}
        write_json(self.config, configured)
        state = self.controller.status()
        self.assertEqual(state["configured_roles"], ["rx", "tx", "programmer"])
        self.assertNotIn("FTDI", json.dumps(state))
        for action in ("inspect", "program"):
            with self.subTest(action=action), self.assertRaises(ValueError):
                self.controller.request("programmer", action)
        self.assertTrue(self.controller.request_editor("programmer", "editor-read")["pending"])

    def test_preview_never_presents_or_queues_radio_data(self):
        self.controller.supported = False
        self.assertIsNone(self.controller.editor_snapshot("rx"))
        with self.assertRaises(ValueError):
            self.controller.request_editor("rx", "editor-read")

    def test_progress_and_activity_are_bounded_in_public_status(self):
        request = {"request_id": "a" * 32, "role": "rx", "action": "editor-read"}
        write_json(self.state / self.controller.REQUEST, request)
        result = {"request_id": request["request_id"], "phase": "download",
                  "progress": {"current": 10, "total": 20, "percent": 50, "unit": "blocks"},
                  "events": [{"time": "2026-01-01", "message": "x" * 300} for _ in range(30)]}
        write_json(self.state / self.controller.RESULT, result)
        state = self.controller.status()
        self.assertEqual(state["progress"]["percent"], 50)
        self.assertEqual(len(state["events"]), 20)
        self.assertEqual(len(state["events"][0]["message"]), 240)
        result["progress"]["percent"] = 101
        write_json(self.state / self.controller.RESULT, result)
        self.assertIsNone(self.controller.status()["progress"])


class EditorHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.record = password_record("sample-local-admin")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.recordings = self.root / "recordings"
        self.recordings.mkdir()
        self.state = self.root / "state"
        self.admin = self.root / "admin.json"
        write_json(self.admin, self.record)
        self.server = dashboard.make_server("127.0.0.1", 0, self.recordings, self.state, self.admin)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.cookie = self.csrf = None

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.tmp.cleanup()

    def request(self, path, method="GET", data=None, authenticated=False, headers=None):
        supplied = dict(headers or {})
        if authenticated:
            supplied.setdefault("Cookie", self.cookie)
            supplied.setdefault("X-CSRF-Token", self.csrf)
        body = json.dumps(data) if data is not None else None
        if body is not None:
            supplied.setdefault("Content-Type", "application/json")
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        connection.request(method, path, body=body, headers=supplied)
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return result

    def login(self):
        code, headers, body = self.request("/api/admin/login", "POST", {"password": "sample-local-admin"})
        self.assertEqual(code, 200)
        self.cookie = headers["Set-Cookie"].split(";", 1)[0]
        self.csrf = json.loads(body)["csrf_token"]

    def test_radio_snapshot_requires_admin_but_get_needs_no_csrf(self):
        with patch.object(self.server.programming, "editor_snapshot", return_value=snapshot()) as read:
            self.assertEqual(self.request("/api/radio-editor?role=rx")[0], 403)
            read.assert_not_called()
            self.login()
            code, headers, body = self.request("/api/radio-editor?role=rx", headers={"Cookie": self.cookie})
            self.assertEqual(code, 200)
            self.assertEqual(json.loads(body)["editor"]["base_id"], "a" * 32)
            self.assertIn("no-store", headers["Cache-Control"])
            for query in ("", "?role=rx&role=tx", "?role=rx&file=secret", "?role=../file"):
                self.assertEqual(self.request("/api/radio-editor" + query, authenticated=True)[0], 400)

    def test_editor_mutations_require_csrf_and_narrow_fields(self):
        for route, value in (("/api/radio-editor/read", {"role": "rx"}),
            ("/api/radio-editor/write", {"role": "rx", "base_id": "a" * 32, "changes": [ROW], "settings": {}})):
            self.assertEqual(self.request(route, "POST", value)[0], 403)
        self.login()
        self.assertEqual(self.request("/api/radio-editor/read", "POST", {"role": "rx"}, headers={"Cookie": self.cookie})[0], 403)
        with patch.object(self.server.programming, "request_editor", return_value={"pending": True}) as queue:
            self.assertEqual(self.request("/api/radio-editor/read", "POST", {"role": "rx", "path": "other"}, True)[0], 400)
            queue.assert_not_called()
            self.assertEqual(self.request("/api/radio-editor/read", "POST", {"role": "rx"}, True)[0], 202)
            queue.assert_called_with("rx", "editor-read")
            body = {"role": "tx", "base_id": "a" * 32, "changes": [ROW], "settings": {}}
            self.assertEqual(self.request("/api/radio-editor/write", "POST", body, True)[0], 202)
            queue.assert_called_with("tx", "editor-write", "a" * 32, [ROW], {})

    def test_external_model_queries_are_authenticated_and_strict(self):
        route = "/api/radio-editor?role=programmer&model=UV5R"
        self.assertEqual(self.request(route)[0], 403)
        self.login()
        with patch.object(self.server.programming, "editor_snapshot", return_value=None) as read:
            code, _, body = self.request(route, authenticated=True)
            self.assertEqual(code, 200)
            self.assertIsNone(json.loads(body)["editor"])
            read.assert_called_once_with("programmer", model="UV5R")
            read.reset_mock()
            for query in ("role=rx&model=UV5R", "role=tx&model=5RX", "role=programmer&model=",
                          "role=programmer&model=5RX&model=UV5R", "role=programmer&model=driver.path",
                          "role=programmer&model=UV5R&file=secret"):
                with self.subTest(query=query):
                    self.assertEqual(self.request("/api/radio-editor?" + query, authenticated=True)[0], 400)
            read.assert_not_called()

    def test_external_model_read_requires_csrf_and_write_cannot_choose_driver(self):
        route = "/api/radio-editor/read"
        payload = {"role": "programmer", "model": "UV5R"}
        self.assertEqual(self.request(route, "POST", payload)[0], 403)
        self.login()
        self.assertEqual(self.request(route, "POST", payload, headers={"Cookie": self.cookie})[0], 403)
        with patch.object(self.server.programming, "request_editor", return_value={"pending": True}) as queue:
            self.assertEqual(self.request(route, "POST", payload, True)[0], 202)
            queue.assert_called_once_with("programmer", "editor-read", model="UV5R")
            queue.reset_mock()
            for invalid in ({**payload, "role": "rx"}, {**payload, "role": "tx", "model": "5RX"},
                            {**payload, "model": "driver.path"}, {**payload, "model": None},
                            {**payload, "driver": "uv5r"}, {**payload, "path": "/dev/ttyUSB0"}):
                with self.subTest(payload=invalid):
                    self.assertEqual(self.request(route, "POST", invalid, True)[0], 400)
            write = {**payload, "base_id": "a" * 32, "changes": [ROW], "settings": {}}
            self.assertEqual(self.request("/api/radio-editor/write", "POST", write, True)[0], 400)
            queue.assert_not_called()

    def test_recovery_and_external_programmer_routes_preserve_narrow_payloads(self):
        self.login()
        with patch.object(self.server.programming, "request_editor", return_value={"pending": True}) as queue:
            body = {"role": "rx", "base_id": "a" * 32, "changes": [], "settings": {},
                    "supersede_id": "b" * 32}
            self.assertEqual(self.request("/api/radio-editor/write", "POST", body, True)[0], 202)
            queue.assert_called_with("rx", "editor-write", "a" * 32, [], {}, "b" * 32)
            for malformed in (None, "../candidate", False):
                queue.reset_mock()
                self.assertEqual(self.request("/api/radio-editor/write", "POST", {**body, "supersede_id": malformed}, True)[0], 400)
                queue.assert_not_called()
            self.assertEqual(self.request("/api/radio-editor/read", "POST", {"role": "programmer"}, True)[0], 202)
            queue.assert_called_with("programmer", "editor-read")
            self.assertEqual(self.request("/api/radio-editor/read", "POST", {"role": "programmer", "port": "/dev/ttyUSB0"}, True)[0], 400)
        with patch.object(self.server.programming, "request", return_value={"pending": True}) as fixed:
            for action in ("inspect", "program"):
                self.assertEqual(self.request("/api/radio-programming", "POST", {"role": "programmer", "action": action}, True)[0], 400)
            fixed.assert_not_called()
            self.assertEqual(self.request("/api/radio-programming", "POST", {"role": "programmer", "action": "verify"}, True)[0], 202)
            fixed.assert_called_once_with("programmer", "verify")

    def test_editor_respects_shared_pending_guards(self):
        self.login()
        for controller in (self.server.programming, self.server.updates, self.server.wifi):
            with self.subTest(controller=type(controller).__name__), \
                    patch.object(controller, "status", return_value={"pending": True}), \
                    patch.object(self.server.programming, "request_editor") as queue:
                self.assertEqual(self.request("/api/radio-editor/read", "POST", {"role": "rx"}, True)[0], 409)
                queue.assert_not_called()

    def test_saved_repair_requires_auth_csrf_and_exact_pending_identifier(self):
        route = "/api/radio-programming"
        body = {"role": "programmer", "action": "repair-upload", "pending_id": "a" * 32}
        self.assertEqual(self.request(route, "POST", body)[0], 403)
        self.login()
        self.assertEqual(self.request(route, "POST", body, headers={"Cookie": self.cookie})[0], 403)
        with patch.object(self.server.programming, "request", return_value={"pending": True}) as queue:
            for invalid in ({"role": "rx", "action": "repair-upload"},
                            {**body, "pending_id": "../candidate"},
                            {**body, "pending_id": False},
                            {**body, "path": "/private/image"},
                            {**body, "action": "verify"}):
                self.assertEqual(self.request(route, "POST", invalid, True)[0], 400)
                queue.assert_not_called()
            self.assertEqual(self.request(route, "POST", body, True)[0], 202)
            queue.assert_called_once_with("programmer", "repair-upload", "a" * 32)
            queue.reset_mock()
            with patch.object(self.server.updates, "status", return_value={"pending": True}):
                self.assertEqual(self.request(route, "POST", body, True)[0], 409)
                queue.assert_not_called()

    def test_progress_status_does_not_scan_hardware_or_expose_snapshot(self):
        with patch.object(self.server.monitor, "status", side_effect=AssertionError("Inventory is too slow")), \
                patch.object(self.server.programming, "editor_snapshot", side_effect=AssertionError("Private contents")), \
                patch.object(self.server.programming, "status", return_value={"pending": True, "phase": "download"}):
            code, headers, body = self.request("/api/radio-programming/status")
            self.assertEqual(code, 200)
            self.assertEqual(json.loads(body), {"radio_programming": {"pending": True, "phase": "download"}})
            self.assertIn("no-store", headers["Cache-Control"])

    def test_hidden_editor_shell_uses_the_dashboard_browser_policy(self):
        for path in ("/radio-editor", "/radio-editor.js", "/radio-editor.css"):
            with self.subTest(path=path):
                code, headers, body = self.request(path)
                self.assertEqual(code, 200)
                self.assertGreater(len(body), 100)
                self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertEqual(self.request("/radio-editor/../../server.py")[0], 404)

    def test_write_body_limit_is_bounded_separately_from_other_routes(self):
        self.login()
        body = {"role": "rx", "base_id": "a" * 32, "changes": [ROW], "settings": {"settings.voice": "x" * 5000}}
        # More than 4 KiB reaches the narrow editor validator rather than the general limit.
        with patch.object(self.server.programming, "request_editor", side_effect=ValueError("Invalid settings")):
            code, _, value = self.request("/api/radio-editor/write", "POST", body, True)
            self.assertEqual(code, 400)
            self.assertIn("Invalid settings", json.loads(value)["error"])
        body["settings"]["settings.voice"] = "x" * 65536
        code, _, value = self.request("/api/radio-editor/write", "POST", body, True)
        self.assertEqual(code, 400)
        self.assertIn("too large", json.loads(value)["error"])


if __name__ == "__main__":
    unittest.main()
