"""DTMF control tests use fake radios/audio and synthetic private credentials."""
import copy
import hashlib
import json
import os
from pathlib import Path
import socket
import stat
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "dashboard"))
from controls import SettingsStore, write_json
from dtmf_control import DtmfControl, RESULTS
from station_identification import StationIdentification
import test_radio_editor_api as editor_tests

CODE = "246802"
RECORD = {"schema": 1, "algorithm": "pbkdf2_sha256", "iterations": 100000,
          "salt": "11" * 16,
          "digest": hashlib.pbkdf2_hmac("sha256", CODE.encode(), bytes.fromhex("11" * 16), 100000).hex()}


class FakeConnection:
    def __init__(self, data):
        self.data, self.sent = data, b""
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def settimeout(self, value):
        pass
    def recv(self, limit):
        value, self.data = self.data[:limit], self.data[limit:]
        return value
    def sendall(self, data):
        self.sent += data


def fixture(folder):
    recording = {"enabled": False}
    calls = []
    radio = {"pending": False, "roles": {"rx": {}, "tx": {}},
             "pair": {"pair_id": "lab", "stored_verified": True},
             "pair_switching": {"presets": [{"id": "lab", "index": 3}]}}
    state = {"pending": False}
    def set_recording(enabled):
        calls.append(enabled)
        recording["enabled"] = enabled
    server = types.SimpleNamespace(settings=SettingsStore(folder / "settings.json"),
        mutation_lock=threading.RLock(), settings_lock=threading.RLock(),
        hub=types.SimpleNamespace(set_recording=set_recording, status=lambda: {"recorder": dict(recording)}),
        programming=types.SimpleNamespace(status=lambda: copy.deepcopy(radio)),
        updates=types.SimpleNamespace(status=lambda: dict(state)),
        wifi=types.SimpleNamespace(status=lambda: {"pending": False}))
    server.station_identification = StationIdentification(server.settings, folder / "station-identification.json")
    config = folder / "dtmf.json"
    write_json(config, RECORD, mode=0o640)
    bridge = DtmfControl(server, config, folder / "dtmf.sock", owner_uid=os.getuid(),
                         group_id=os.getgid(), allowed_uids={0, 123}, state_dir=folder,
                         code_supported=True, runner=lambda args: "active")
    server.dtmf = bridge
    return server, bridge, radio, state, recording, calls


class DtmfControlTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.server, self.bridge, self.radio, self.maintenance, self.recording, self.calls = fixture(self.folder)
        self.addCleanup(self.bridge.close)
    def enable(self):
        self.server.settings.update({"dtmf_enabled": True})
    def command(self, raw=CODE, uid=123):
        return self.bridge.command({"schema": 1, "command": raw}, uid)
    def action(self, option="2", value="1"):
        self.assertEqual(self.command()["result"], "authenticated")
        self.assertEqual(self.command(option)["result"], {"1": "choose_status", "2": "choose_recording", "3": "choose_identification"}[option])
        return self.command(value)

    def test_default_off_and_public_status_contains_no_credentials(self):
        self.assertFalse(self.bridge.status()["enabled"])
        self.assertEqual(self.command()["result"], "disabled")
        public = json.dumps(self.bridge.status())
        for private in (CODE, RECORD["salt"], RECORD["digest"], "iterations", "algorithm"):
            self.assertNotIn(private, public)
        self.assertFalse(self.calls)

    def test_peer_and_exact_grammar_are_checked_before_password_work(self):
        self.enable()
        malformed = [{}, {"schema": True, "command": CODE}, {"schema": 1, "command": CODE + "0"},
                     {"schema": 1, "command": CODE + "#"}, {"schema": 1, "command": "１２３４５６"},
                     {"schema": 1, "command": "abcdef"}, {"schema": 1, "command": "12*456"},
                     {"schema": 1, "command": "#"}, {"schema": 1, "command": CODE, "action": "program"}]
        with mock.patch("dtmf_control.hashlib.pbkdf2_hmac") as derive:
            for value in malformed:
                self.assertEqual(self.bridge.command(value, 123)["result"], "denied")
            self.assertEqual(self.command(uid=999)["result"], "denied")
            derive.assert_not_called()
        self.assertFalse(self.calls)

    def test_linux_peer_credentials_are_read_from_the_kernel_socket_option(self):
        import struct
        connection = mock.Mock()
        connection.getsockopt.return_value = struct.pack("3i", 456, 123, 789)
        with mock.patch.object(socket, "SO_PEERCRED", 17, create=True):
            self.assertEqual(self.bridge.peer_uid(connection), 123)
        connection.getsockopt.assert_called_once_with(socket.SOL_SOCKET, 17, struct.calcsize("3i"))

    def test_attempt_window_expires_and_a_changed_record_cannot_authorize_old_password(self):
        self.enable(); now = [100.0]; self.bridge.clock = lambda: now[0]
        for _ in range(5): self.assertEqual(self.command("000000")["result"], "denied")
        self.assertEqual(self.command()["result"], "denied")
        now[0] += 61
        self.assertEqual(self.command()["result"], "authenticated")
        def changed(code, record):
            write_json(self.bridge.config_file, {**RECORD, "salt": "22" * 16}, mode=0o640)
            return True
        with mock.patch.object(self.bridge, "_authenticate", side_effect=changed):
            self.assertEqual(self.command()["result"], "denied")
        self.assertIsNone(self.bridge.rf_session); self.assertFalse(self.calls)

    def test_recording_menu_commits_once_and_requires_full_reauthentication(self):
        self.enable()
        self.assertEqual(self.command("1")["result"], "denied")
        result = self.action()
        self.assertEqual(result, {"schema": 1, "result": "recording_on", "pair_index": 3, "recording": True,
                                  "announce_status": True, "identification_enabled": True})
        self.assertEqual(self.command("0")["result"], "denied")
        self.assertEqual(self.command("1")["result"], "denied")
        self.assertEqual(self.action(value="0")["result"], "recording_off")
        self.assertEqual(self.calls, [True, False])
        self.assertFalse(self.server.settings.get()["record_on_startup"])

    def test_status_preference_changes_only_optional_post_action_status(self):
        self.enable()
        self.assertEqual(self.command()["announce_status"], False)
        self.assertEqual(self.command("1")["result"], "choose_status")
        result = self.command("0")
        self.assertEqual(result["result"], "status_off"); self.assertFalse(result["announce_status"])
        self.assertFalse(self.server.settings.get()["dtmf_status_announcements"])
        self.assertFalse(self.action()["announce_status"])
        self.assertTrue(self.action("1", "1")["announce_status"])
        self.assertEqual(self.calls, [True])

    def test_identification_menu_requires_authentication_persists_and_cannot_replay(self):
        self.enable()
        before = self.server.settings.get()
        self.assertEqual(self.command("3")["result"], "denied")
        self.assertEqual(self.command("000000")["result"], "denied")
        self.assertEqual(self.command("3")["result"], "denied")
        result = self.action("3", "0")
        self.assertEqual(result, {"schema": 1, "result": "identification_off", "pair_index": 3,
                                  "recording": False, "announce_status": True, "identification_enabled": False})
        self.assertFalse(self.server.station_identification.status()["enabled"])
        self.assertFalse(SettingsStore(self.folder / "settings.json").get()["automatic_station_id"])
        self.assertEqual(self.server.station_identification.path.read_bytes(), b'{"schema":1,"enabled":false}\n')
        after = self.server.settings.get()
        self.assertFalse(after.pop("automatic_station_id"))
        before.pop("automatic_station_id")
        self.assertEqual(after, before)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.command("1")["result"], "denied")
        self.assertEqual(self.command("0")["result"], "denied")
        self.assertEqual(self.action("3", "1")["result"], "identification_on")
        self.assertTrue(self.server.station_identification.status()["enabled"])

    def test_identification_menu_has_absolute_expiry_and_rejects_nonbinary_values(self):
        self.enable(); now = [100.0]; self.bridge.clock = lambda: now[0]
        self.command(); now[0] += 29
        self.assertEqual(self.command("3")["result"], "choose_identification")
        now[0] += 1
        self.assertEqual(self.command("0")["result"], "expired")
        self.assertTrue(self.server.station_identification.status()["enabled"])
        self.assertEqual(self.command("0")["result"], "denied")
        self.command(); self.command("3")
        for value in (True, False, 0, 1, None, {"enabled": False}):
            self.assertEqual(self.command(value)["result"], "denied")
        self.assertEqual(self.command("2")["result"], "invalid")
        self.assertEqual(self.command("0")["result"], "denied")
        self.assertTrue(self.server.station_identification.status()["enabled"])

    def test_identification_projection_loss_at_selection_or_commit_consumes_authorization(self):
        self.enable()
        self.command(); self.server.station_identification.path.unlink()
        self.assertEqual(self.command("3")["result"], "unavailable")
        self.assertIsNone(self.bridge.rf_session)
        self.assertEqual(self.command("0")["result"], "denied")
        self.server.station_identification.publish()
        self.command(); self.command("3")
        self.server.station_identification.path.unlink()
        self.assertEqual(self.command("0")["result"], "unavailable")
        self.assertIsNone(self.bridge.rf_session)
        self.assertTrue(self.server.settings.get()["automatic_station_id"])

    def test_identification_projection_failure_rolls_back_and_does_not_leak_errors(self):
        self.enable(); self.command(); self.command("3")
        controller = self.server.station_identification
        with mock.patch.object(controller, "_write_projection", side_effect=OSError("private fixture detail")):
            result = self.command("0")
        self.assertEqual(result["result"], "unavailable")
        self.assertNotIn("private fixture detail", json.dumps(result))
        self.assertTrue(self.server.settings.get()["automatic_station_id"])
        self.assertTrue(SettingsStore(self.folder / "settings.json").get()["automatic_station_id"])
        self.assertEqual(controller.path.read_bytes(), b'{"schema":1,"enabled":true}\n')
        self.assertEqual(self.command("0")["result"], "denied")

    def test_identification_action_keeps_maintenance_and_optional_status_controls(self):
        self.enable(); self.command(); self.command("3")
        self.maintenance["pending"] = True
        self.assertEqual(self.command("0")["result"], "busy")
        self.maintenance["pending"] = False
        self.assertEqual(self.command("0")["result"], "denied")
        self.assertTrue(self.server.station_identification.status()["enabled"])
        self.server.settings.update({"dtmf_status_announcements": False})
        result = self.action("3", "0")
        self.assertEqual(result["result"], "identification_off")
        self.assertFalse(result["announce_status"])
        self.assertTrue(self.server.settings.get()["dtmf_enabled"])
        self.assertFalse(self.server.settings.get()["dtmf_status_announcements"])
        self.assertFalse(self.server.hub.status()["recorder"]["enabled"])

    def test_concurrent_identification_commit_applies_once(self):
        self.enable(); self.command(); self.command("3"); results = []
        controller = self.server.station_identification
        with mock.patch.object(controller, "set_enabled", wraps=controller.set_enabled) as change:
            threads = [threading.Thread(target=lambda: results.append(self.command("0")["result"])) for _ in range(4)]
            for thread in threads: thread.start()
            for thread in threads: thread.join(2)
        self.assertEqual(results.count("identification_off"), 1)
        change.assert_called_once_with(False)
        self.assertFalse(controller.status()["enabled"])

    def test_authenticated_zero_reports_all_current_controls_without_mutating_them(self):
        self.enable()
        self.assertEqual(self.command("0")["result"], "denied")
        for enabled in (True, False):
            self.server.station_identification.set_enabled(enabled)
            self.server.settings.update({"dtmf_status_announcements": enabled})
            self.server.hub.set_recording(enabled)
            before = (self.server.settings.get(), (self.folder / "settings.json").read_bytes(),
                      self.server.station_identification.path.read_bytes(), list(self.calls))
            self.assertEqual(self.command()["result"], "authenticated")
            with mock.patch.object(self.server.station_identification, "set_enabled") as mutate:
                response = self.command("0")
                mutate.assert_not_called()
            self.assertEqual(response, {"schema": 1, "result": "status", "pair_index": 3,
                "recording": enabled, "announce_status": enabled, "identification_enabled": enabled})
            self.assertIsNone(self.bridge.rf_session)
            self.assertEqual(self.command("0")["result"], "denied")
            self.assertEqual(self.command("1")["result"], "denied")
            after = (self.server.settings.get(), (self.folder / "settings.json").read_bytes(),
                     self.server.station_identification.path.read_bytes(), list(self.calls))
            self.assertEqual(after, before)

    def test_zero_status_requires_fresh_authentication_available_state_and_clear_maintenance(self):
        self.enable(); now = [100.0]; self.bridge.clock = lambda: now[0]
        self.command(); now[0] += 30
        self.assertEqual(self.command("0")["result"], "expired")
        self.assertEqual(self.command("0")["result"], "denied")
        self.command(); self.server.station_identification.path.unlink()
        self.assertEqual(self.command("0")["result"], "unavailable")
        self.assertIsNone(self.bridge.rf_session)
        self.server.station_identification.publish()
        self.command(); self.maintenance["pending"] = True
        self.assertEqual(self.command("0")["result"], "busy")
        self.maintenance["pending"] = False
        self.assertEqual(self.command("0")["result"], "denied")
        self.assertTrue(self.server.settings.get()["automatic_station_id"])
        self.assertFalse(self.calls)

    def test_concurrent_status_requests_consume_one_authorization(self):
        self.enable(); self.command(); results = []
        threads = [threading.Thread(target=lambda: results.append(self.command("0")["result"])) for _ in range(4)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(2)
        self.assertEqual(results.count("status"), 1)
        self.assertTrue(all(result in ("status", "denied") for result in results))
        self.assertFalse(self.calls)

    def test_absolute_timeout_does_not_extend_after_selecting_an_option(self):
        self.enable(); now = [100.0]; self.bridge.clock = lambda: now[0]
        self.command(); now[0] += 29
        self.assertEqual(self.command("2")["result"], "choose_recording")
        now[0] += 1
        self.assertEqual(self.command("1")["result"], "expired")
        self.assertEqual(self.command("1")["result"], "denied"); self.assertFalse(self.calls)

    def test_cancel_invalid_input_disable_and_record_change_invalidate_menu(self):
        self.enable()
        for raw, result in (("*", "cancelled"), ("8", "invalid"), ("A", "invalid")):
            self.command(); self.assertEqual(self.command(raw)["result"], result)
            self.assertEqual(self.command("2")["result"], "denied")
        self.command(); self.command("2")
        write_json(self.bridge.config_file, {**RECORD, "salt": "22" * 16}, mode=0o640)
        self.assertEqual(self.command("1")["result"], "denied")
        write_json(self.bridge.config_file, RECORD, mode=0o640)
        self.command(); self.bridge.set_enabled(False); self.enable()
        self.assertEqual(self.command("2")["result"], "denied"); self.assertFalse(self.calls)

    def test_authenticated_password_restarts_menu_and_different_peer_cannot_select(self):
        self.enable(); self.command(); self.command("2")
        self.assertEqual(self.command()["result"], "authenticated")
        self.assertEqual(self.command("1", uid=0)["result"], "denied")
        self.assertEqual(self.command("1")["result"], "choose_status")
        self.assertEqual(self.command("0")["result"], "status_off"); self.assertFalse(self.calls)

    def test_failed_reauthentication_ends_the_previous_authorized_menu(self):
        self.enable(); self.command(); self.command("2")
        self.assertEqual(self.command("000000")["result"], "denied")
        self.assertIsNone(self.bridge.rf_session)
        self.assertEqual(self.command("1")["result"], "denied")
        self.assertFalse(self.calls)
        self.assertEqual(self.action()["result"], "recording_on")

    def test_wrong_password_hash_work_and_successful_authentications_are_bounded(self):
        self.enable(); original = hashlib.pbkdf2_hmac
        with mock.patch("dtmf_control.hashlib.pbkdf2_hmac", wraps=original) as derive:
            for _ in range(8): self.assertEqual(self.command("000000")["result"], "denied")
            self.assertEqual(derive.call_count, 5)
        self.bridge.failures.clear(); self.bridge.attempts.clear()
        for _ in range(12): self.assertEqual(self.command()["result"], "authenticated")
        with mock.patch("dtmf_control.hashlib.pbkdf2_hmac") as derive:
            self.assertEqual(self.command()["result"], "denied"); derive.assert_not_called()
        self.assertFalse(self.calls)

    def test_missing_invalid_or_exposed_record_is_silent_before_authentication(self):
        self.enable()
        for kind in ("missing", "bad", "public", "symlink"):
            self.bridge.config_file.unlink(missing_ok=True)
            if kind == "bad": write_json(self.bridge.config_file, {**RECORD, "iterations": True})
            elif kind == "public": write_json(self.bridge.config_file, RECORD, mode=0o644)
            elif kind == "symlink":
                target = self.folder / "record.json"; write_json(target, RECORD); self.bridge.config_file.symlink_to(target)
            self.assertFalse(self.bridge.status()["configured"])
            self.assertEqual(self.command()["result"], "disabled")
        self.assertFalse(self.calls)

    def test_pending_and_unfinished_work_blocks_auth_and_consumes_existing_menu(self):
        self.enable()
        variants = [{"pending": True}, {"verification_required": True},
                    {"roles": {"tx": {"verification_required": True}}}, {"pair": {"verification_required": True}}]
        for update in variants:
            self.command(); self.command("2"); old = copy.deepcopy(self.radio); self.radio.update(update)
            self.assertEqual(self.command("1")["result"], "busy")
            self.radio.clear(); self.radio.update(old)
            self.assertEqual(self.command("1")["result"], "denied")
        self.maintenance["pending"] = True
        self.assertEqual(self.command()["result"], "busy"); self.assertFalse(self.calls)

    def test_mutation_lock_returns_busy_without_granting_menu_authorization(self):
        self.enable(); acquired, release = threading.Event(), threading.Event()
        def hold():
            with self.server.mutation_lock: acquired.set(); release.wait(3)
        thread = threading.Thread(target=hold); thread.start(); acquired.wait(1)
        try:
            self.assertEqual(self.command()["result"], "busy"); self.assertIsNone(self.bridge.rf_session)
        finally: release.set(); thread.join(2)
        self.assertFalse(self.calls)

    def test_unverified_or_stale_pair_has_no_announced_pair_number(self):
        self.enable()
        for field in ({"stored_verified": False}, {"catalog_changed": True}):
            self.radio["pair"].update(field); self.assertEqual(self.action()["pair_index"], 0)
        self.radio["pair"] = {"stored_verified": True, "pair_id": "missing"}
        self.assertEqual(self.action()["pair_index"], 0)

    def test_toggle_requires_capability_and_preserves_startup_preference(self):
        with self.assertRaisesRegex(ValueError, "DTMF password in Admin"): self.bridge.set_enabled(True)
        original = self.bridge.status; self.bridge.status = lambda: {**original(), "available": True}
        self.assertTrue(self.bridge.set_enabled(True)["enabled"])
        self.assertFalse(self.bridge.set_enabled(False)["enabled"])
        self.assertFalse(self.server.settings.get()["record_on_startup"])
        self.radio["pending"] = True
        with self.assertRaisesRegex(ValueError, "unfinished"): self.bridge.set_announcements(False)

    def test_recording_failure_consumes_session_and_returns_fixed_token(self):
        self.enable(); self.server.hub.set_recording = mock.Mock(side_effect=OSError("private error"))
        response = self.action()
        self.assertEqual(response["result"], "unavailable"); self.assertNotIn("private error", json.dumps(response))
        self.assertEqual(self.command("1")["result"], "denied")

    def test_concurrent_duplicate_commit_cannot_apply_twice(self):
        self.enable(); self.command(); self.command("2"); results = []
        threads = [threading.Thread(target=lambda: results.append(self.command("1")["result"])) for _ in range(4)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(2)
        self.assertEqual(results.count("recording_on"), 1); self.assertEqual(self.calls, [True])

    def test_code_queue_contains_only_hash_record_and_blocks_rf_until_result(self):
        self.enable(); self.command(); self.command("2")
        state = self.bridge.request_code("ABCD12")
        self.assertTrue(state["pending"]); self.assertIsNone(self.bridge.rf_session)
        request_file = self.folder / "dtmf-code-request.json"; raw = request_file.read_text(); request = json.loads(raw)
        self.assertNotIn("ABCD12", raw); self.assertEqual(stat.S_IMODE(request_file.stat().st_mode), 0o600)
        self.assertEqual(request["record"]["iterations"], 600000)
        self.assertEqual(self.command("1")["result"], "disabled")
        public = json.dumps(self.bridge.status())
        self.assertNotIn(request["record"]["digest"], public); self.assertNotIn(request["record"]["salt"], public)
        result = {"schema": 1, "request_id": state["request_id"], "success": True,
                  "message": "private helper detail ignored", "monitor_restart_required": True}
        write_json(self.folder / "dtmf-code-result.json", result, mode=0o640)
        request_file.unlink()
        done = self.bridge.code_status(); self.assertFalse(done["pending"]); self.assertTrue(done["success"])
        self.assertTrue(done["monitor_restart_required"]); self.assertNotIn("private helper", done["message"])

    def test_password_alphabet_pending_and_maintenance_guards(self):
        for value in ("12345", "1234567", "abcdef", "ABCD*1", "ABC#12", "１２３４５６", "12 456"):
            with self.assertRaises(ValueError): self.bridge.request_code(value)
        for pending in ("radio", "update"):
            if pending == "radio": self.radio["pair"] = {"verification_required": True}
            else: self.maintenance["pending"] = True
            with self.assertRaisesRegex(ValueError, "unfinished"): self.bridge.request_code("ABCD12")
            self.radio["pair"] = {}; self.maintenance["pending"] = False
        self.bridge.request_code("ABCD12")
        with self.assertRaisesRegex(ValueError, "unfinished"): self.bridge.request_code("ABCD13")

    def test_all_fourteen_password_characters_can_authenticate_but_reserved_keys_cannot(self):
        self.enable()
        for char in "0123456789ABCD":
            code = char * 6
            record = {**RECORD, "digest": hashlib.pbkdf2_hmac("sha256", code.encode(), bytes.fromhex(RECORD["salt"]), 100000).hex()}
            write_json(self.bridge.config_file, record, mode=0o640)
            self.bridge.attempts.clear()
            self.assertEqual(self.command(code)["result"], "authenticated")
        for code in ("******", "######", "12#456", "12345#", "ABcd12"):
            self.assertEqual(self.command(code)["result"], "denied")

    def test_unmatched_or_untrusted_code_result_cannot_clear_pending(self):
        state = self.bridge.request_code("ABCD12")
        result_file = self.folder / "dtmf-code-result.json"
        result = {"schema": 1, "request_id": "e" * 32, "success": True, "message": "ok", "monitor_restart_required": False}
        write_json(result_file, result, mode=0o640)
        self.assertTrue(self.bridge.code_status()["pending"])
        write_json(result_file, {**result, "request_id": state["request_id"]}, mode=0o644)
        self.assertTrue(self.bridge.code_status()["pending"])
        result_file.unlink(); target = self.folder / "other.json"; write_json(target, {**result, "request_id": state["request_id"]})
        result_file.symlink_to(target); self.assertTrue(self.bridge.code_status()["pending"])

    def test_socket_protocol_rejects_oversized_multiple_and_duplicate_frames(self):
        self.enable(); self.bridge.peer_uid = lambda connection: 123
        for payload in (b"x" * 257 + b"\n", b'{}\n{}\n',
                        ('{"schema":1,"command":"'+CODE+'","command":"'+CODE+'"}\n').encode()):
            connection = FakeConnection(payload); self.bridge._connection(connection)
            if connection.sent: self.assertEqual(json.loads(connection.sent)["result"], "denied")
            self.assertNotIn(CODE.encode(), connection.sent)
        connection = FakeConnection(json.dumps({"schema": 1, "command": CODE}).encode() + b"\n")
        self.bridge._connection(connection); self.assertEqual(json.loads(connection.sent)["result"], "authenticated")
        self.assertEqual(set(json.loads(connection.sent)), {"schema", "result", "pair_index", "recording", "announce_status", "identification_enabled"})

    def test_socket_permissions_cleanup_and_unexpected_file_preservation(self):
        self.bridge.socket_path.write_text("keep")
        with mock.patch.object(socket, "SO_PEERCRED", 17, create=True):
            self.assertFalse(self.bridge.start()); self.assertEqual(self.bridge.socket_path.read_text(), "keep")
            self.bridge.socket_path.unlink(); self.assertTrue(self.bridge.start())
            self.assertEqual(stat.S_IMODE(self.bridge.socket_path.stat().st_mode), 0o660)
            self.assertEqual(self.bridge.socket_path.stat().st_gid, os.getgid())
            self.assertTrue(self.bridge.status()["bridge_ready"])
            self.bridge.close(); self.assertFalse(self.bridge.socket_path.exists())


class DtmfHTTPTests(unittest.TestCase):
    setUpClass = classmethod(editor_tests.EditorHTTPTests.setUpClass.__func__)
    tearDown = editor_tests.EditorHTTPTests.tearDown
    request = editor_tests.EditorHTTPTests.request
    login = editor_tests.EditorHTTPTests.login
    def setUp(self):
        editor_tests.EditorHTTPTests.setUp(self)
        self.server.dtmf.close()
        config = self.state / "dtmf.json"
        write_json(config, RECORD, mode=0o640)
        self.server.dtmf = DtmfControl(self.server, config, self.state / "dtmf.sock", owner_uid=os.getuid(),
                                       state_dir=self.state, code_supported=True, runner=lambda args: "active")
        original = self.server.dtmf.status
        self.server.dtmf.status = lambda: {**original(), "available": True, "bridge_ready": True}
        self.server.programming.status = lambda: {"pending": False, "roles": {}}
        self.server.updates.status = lambda: {"pending": False}
        self.server.wifi.status = lambda: {"pending": False}

    def test_toggle_requires_admin_csrf_and_accepts_only_enabled(self):
        self.assertEqual(self.request("/api/dtmf-control", "POST", {"enabled": True})[0], 403)
        self.login()
        self.assertEqual(self.request("/api/dtmf-control", "POST", {"enabled": True}, headers={"Cookie": self.cookie})[0], 403)
        for value in ({"enabled": 1}, {"enabled": True, "code": CODE}, {"enabled": True, "command": "program"}):
            self.assertEqual(self.request("/api/dtmf-control", "POST", value, True)[0], 400)
        code, _, body = self.request("/api/dtmf-control", "POST", {"enabled": True}, True)
        self.assertEqual(code, 200)
        self.assertTrue(json.loads(body)["dtmf_control"]["enabled"])
        for item in (CODE, RECORD["salt"], RECORD["digest"]):
            self.assertNotIn(item.encode(), body)

    def test_radio_identification_change_is_immediately_shared_with_web_and_saved_settings(self):
        self.server.station_identification = StationIdentification(self.server.settings, self.state / "station-identification.json")
        self.server.settings.update({"dtmf_enabled": True})
        bridge = self.server.dtmf
        def command(value):
            return bridge.command({"schema": 1, "command": value}, 0)["result"]
        self.assertEqual(command(CODE), "authenticated")
        self.assertEqual(command("3"), "choose_identification")
        self.assertEqual(command("0"), "identification_off")
        code, _, body = self.request("/api/status")
        self.assertEqual(code, 200)
        status = json.loads(body)
        self.assertEqual(status["station_identification"], {"available": True, "enabled": False})
        self.assertFalse(status["settings"]["automatic_station_id"])
        self.assertFalse(SettingsStore(self.state / "settings.json").get()["automatic_station_id"])
        self.login()
        code, _, body = self.request("/api/station-identification", "POST", {"enabled": True}, True)
        self.assertEqual(code, 200)
        self.assertTrue(json.loads(body)["station_identification"]["enabled"])
        self.assertEqual(command("0"), "denied")

    def test_settings_endpoint_cannot_bypass_the_dtmf_toggle(self):
        self.login()
        self.assertEqual(self.request("/api/settings", "POST", {"dtmf_enabled": True}, True)[0], 400)
        self.assertFalse(self.server.settings.get()["dtmf_enabled"])
        self.assertEqual(self.request("/api/settings", "POST", {"station_name": "Lab"}, True)[0], 200)

    def test_unfinished_pair_prevents_enabling_control(self):
        self.login()
        self.server.programming.status = lambda: {"pending": False, "pair": {"verification_required": True}}
        self.assertEqual(self.request("/api/dtmf-control", "POST", {"enabled": True}, True)[0], 400)
        self.assertFalse(self.server.settings.get()["dtmf_enabled"])

    def test_password_change_requires_csrf_fresh_admin_password_and_narrow_payload(self):
        payload = {"admin_password": "sample-local-admin", "code": "ABCD12"}
        self.assertEqual(self.request("/api/dtmf-password", "POST", payload)[0], 403)
        self.login()
        self.assertEqual(self.request("/api/dtmf-password", "POST", payload, headers={"Cookie": self.cookie})[0], 403)
        with mock.patch.object(self.server.dtmf, "request_code", wraps=self.server.dtmf.request_code) as queue:
            self.assertEqual(self.request("/api/dtmf-password", "POST", {**payload, "admin_password": "wrong"}, True)[0], 403)
            queue.assert_not_called()
            for invalid in ({**payload, "path": "/etc/passwd"}, {**payload, "record": RECORD},
                            {**payload, "confirm_code": "ABCD12"},
                            {"password": "sample-local-admin", "new_code": "ABCD12", "confirm_code": "ABCD12"},
                            {"code": "ABCD12"}):
                self.assertEqual(self.request("/api/dtmf-password", "POST", invalid, True)[0], 400)
            queue.assert_not_called()
        for invalid_code in ("ＡＢＣＤ１２", "ABCd12", "ABC#12", "ABCD*1", "12345", "1234567", True, None):
            self.assertEqual(self.request("/api/dtmf-password", "POST", {**payload, "code": invalid_code}, True)[0], 400)
        code, _, body = self.request("/api/dtmf-password", "POST", payload, True)
        self.assertEqual(code, 202); self.assertTrue(json.loads(body)["code_change"]["pending"])
        for private in ("ABCD12", "sample-local-admin", "digest", "salt"):
            self.assertNotIn(private.encode(), body)
        request_file = self.state / "dtmf-code-request.json"
        self.assertTrue(request_file.exists()); self.assertNotIn(b"ABCD12", request_file.read_bytes())
        self.assertEqual(self.request("/api/settings", "POST", {"station_name": "Changed"}, True)[0], 409)
        self.assertEqual(self.request("/api/dtmf-password", "POST", payload, True)[0], 409)

    def test_status_announcements_use_confirmed_dedicated_route_and_settings_cannot_bypass(self):
        self.login()
        self.assertEqual(self.request("/api/settings", "POST", {"dtmf_status_announcements": False}, True)[0], 400)
        code, _, body = self.request("/api/dtmf-control", "POST", {"status_announcements": False}, True)
        self.assertEqual(code, 200); self.assertFalse(json.loads(body)["dtmf_control"]["status_announcements"])
        self.assertEqual(self.request("/api/dtmf-control", "POST", {"enabled": True, "status_announcements": True}, True)[0], 400)

    def test_password_change_rejects_unfinished_radio_before_queuing(self):
        self.login(); self.server.programming.status = lambda: {"pending": False, "roles": {"tx": {"verification_required": True}}}
        payload = {"admin_password": "sample-local-admin", "code": "ABCD12"}
        self.assertEqual(self.request("/api/dtmf-password", "POST", payload, True)[0], 400)
        self.assertFalse((self.state / "dtmf-code-request.json").exists())


if __name__ == "__main__":
    unittest.main()
