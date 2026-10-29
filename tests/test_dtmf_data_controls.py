"""Extended DTMF controls retain the legacy native protocol during upgrades."""
from contextlib import redirect_stdout
import hashlib
import io
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from test_dtmf_control import CODE, DtmfControl, fixture
import test_live_identification as live_tests

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("data_control_client", ROOT / "scripts/dtmf-client.py")
client = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(client)
SPEC = importlib.util.spec_from_file_location("data_control_prompts", ROOT / "scripts/build-dtmf-prompts.py")
prompts = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(prompts)
from dtmf_control import NATIVE_FILES, NATIVE_PROMPTS


class Decoder:
    def __init__(self):
        self.enabled = False
        self.available = True
        self.calls = []

    def status(self):
        return {"enabled": self.enabled, "available": self.available, "input_enabled": False}

    def set_enabled(self, enabled):
        self.calls.append(enabled)
        self.enabled = enabled


class DtmfDataControlsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.server, self.bridge, *_ = fixture(self.folder)
        self.addCleanup(self.bridge.close)
        self.server.settings.update({"dtmf_enabled": True})
        self.server.data_decoder, self.server.sstv_decoder = Decoder(), Decoder()
        self.now = [100.0]
        self.bridge.clock = lambda: self.now[0]
        self.bridge.native_data_status = lambda: {"available": True}

    def command(self, command=CODE, schema=2):
        return self.bridge.command({"schema": schema, "command": command}, 123)

    def toggle(self, option, value):
        self.assertEqual(self.command()["result"], "authenticated")
        selected = self.command(option)
        self.assertEqual(selected["result"], "choose_aprs" if option == "4" else "choose_sstv")
        return self.command(value)

    def test_command_diagnostic_logs_only_fixed_outcomes_without_credentials(self):
        self.bridge.prompt_feedback = True
        output = io.StringIO()
        with redirect_stdout(output):
            response = self.command()
            self.bridge.command({"schema": 2, "command": CODE}, 999)
            self.bridge.rf_command_lock.acquire()
            try:
                self.command()
            finally:
                self.bridge.rf_command_lock.release()
        self.assertEqual(response["result"], "authenticated")
        self.assertEqual(output.getvalue(), "RF control outcome: authenticated\n"
                         "RF control outcome: denied\nRF control outcome: denied\n")
        self.assertNotIn(CODE, output.getvalue())
        self.assertEqual(len(response["prompt_id"]), 32)
        self.assertNotIn(response["prompt_id"], output.getvalue())

    def test_command_diagnostic_never_echoes_an_unrecognized_response_value(self):
        for value in ("private request must not be logged", ["private payload"], None):
            output = io.StringIO()
            with mock.patch.object(self.bridge, "_command", return_value={"result": value, "body": CODE}), redirect_stdout(output):
                self.command()
            self.assertEqual(output.getvalue(), "RF control outcome: unavailable\n")

    def test_command_diagnostic_keeps_exception_details_private_and_releases_lock(self):
        output = io.StringIO()
        with mock.patch.object(self.bridge, "_command", side_effect=ValueError("private " + CODE)), redirect_stdout(output):
            with self.assertRaises(ValueError):
                self.command()
        self.assertEqual(output.getvalue(), "RF control outcome: unavailable\n")
        self.assertTrue(self.bridge.rf_command_lock.acquire(blocking=False))
        self.bridge.rf_command_lock.release()

    def test_prompt_feedback_and_its_busy_acknowledgment_are_not_logged(self):
        output = io.StringIO()
        with mock.patch.object(self.bridge, "_prompt_finished", return_value={"schema": 1, "accepted": True}), redirect_stdout(output):
            response = self.bridge.command({"schema": 1, "prompt_finished": "a" * 32}, 123)
            self.bridge.rf_command_lock.acquire()
            try:
                busy = self.bridge.command({"schema": 1, "prompt_finished": "a" * 32}, 123)
            finally:
                self.bridge.rf_command_lock.release()
        self.assertTrue(response["accepted"]);self.assertFalse(busy["accepted"])
        self.assertEqual(output.getvalue(), "")

    def test_authenticated_options_toggle_and_persist_only_the_selected_decoder(self):
        for option, attribute, setting, label in (("4", "data_decoder", "data_decode_enabled", "aprs"),
                                                ("5", "sstv_decoder", "sstv_decode_enabled", "sstv")):
            for value in ("1", "0"):
                response = self.toggle(option, value)
                self.assertEqual(response["schema"], 2)
                self.assertEqual(response["result"], label + ("_on" if value == "1" else "_off"))
                self.assertIs(response[label + "_enabled"], value == "1")
                self.assertIs(self.server.settings.get()[setting], value == "1")
                self.assertIs(getattr(self.server, attribute).enabled, value == "1")
                self.assertEqual(self.command("1")["result"], "denied")
        self.assertEqual(self.server.data_decoder.calls, [True, False])
        self.assertEqual(self.server.sstv_decoder.calls, [True, False])

    def test_unavailable_decoder_cannot_enable_but_can_still_disable(self):
        self.server.data_decoder.available = False
        self.assertEqual(self.toggle("4", "1")["result"], "unavailable")
        self.assertEqual(self.server.data_decoder.calls, [])
        self.assertFalse(self.server.settings.get()["data_decode_enabled"])
        self.server.data_decoder.enabled = True
        self.server.settings.update({"data_decode_enabled": True})
        self.assertEqual(self.toggle("4", "0")["result"], "aprs_off")
        self.assertFalse(self.server.settings.get()["data_decode_enabled"])

    def test_status_includes_both_decoder_preferences_and_does_not_mutate(self):
        self.server.sstv_decoder.enabled = True
        self.command()
        response = self.command("0")
        self.assertEqual(response["result"], "status")
        self.assertFalse(response["aprs_enabled"])
        self.assertTrue(response["sstv_enabled"])
        self.assertEqual(self.server.data_decoder.calls + self.server.sstv_decoder.calls, [])
        self.assertIsNone(self.bridge.rf_session)
        client.validate_response(response)

    def test_failed_settings_save_restores_the_previous_runtime_preference(self):
        self.command(); self.command("4")
        with mock.patch.object(self.server.settings, "update", side_effect=OSError("synthetic save failure")):
            response = self.command("1")
        self.assertEqual(response["result"], "unavailable")
        self.assertFalse(self.server.settings.get()["data_decode_enabled"])
        self.assertFalse(self.server.data_decoder.enabled)
        self.assertEqual(self.server.data_decoder.calls, [True, False])
        self.assertIsNone(self.bridge.rf_session)

    def test_legacy_sessions_never_accept_new_options_or_fields(self):
        response = self.command(schema=1)
        self.assertEqual(set(response), {"schema", "result", "pair_index", "recording", "announce_status",
                                         "identification_enabled", "prompt_id"})
        self.assertEqual(self.command("4", schema=1)["result"], "invalid")
        self.command(schema=1)
        self.assertEqual(self.command("4", schema=2)["result"], "denied")
        self.assertIsNone(self.bridge.rf_session)
        self.assertEqual(self.server.data_decoder.calls, [])

    def test_dashboard_only_upgrade_returns_the_original_menu_to_new_client(self):
        self.bridge.native_data_status = lambda: {"available": False}
        self.assertEqual(self.command()["schema"], 1)
        self.assertEqual(self.command("2")["result"], "choose_recording")
        self.assertEqual(self.command("1")["result"], "recording_on")
        self.assertEqual(self.command()["schema"], 1)
        self.assertEqual(self.command("5")["result"], "invalid")

    def test_new_options_reset_idle_after_prompt_completion_without_replaying_tokens(self):
        self.bridge.prompt_feedback = True
        opening = self.command()
        self.now[0] = 120
        self.assertTrue(self.bridge.command({"schema": 1, "prompt_finished": opening["prompt_id"]}, 123)["accepted"])
        self.assertEqual(self.bridge.rf_session["expires"], 150)
        self.now[0] = 149
        selected = self.command("5")
        self.assertEqual(self.bridge.rf_session["expires"], 194)
        self.now[0] = 160
        self.assertTrue(self.bridge.command({"schema": 1, "prompt_finished": selected["prompt_id"]}, 123)["accepted"])
        self.assertEqual(self.bridge.rf_session["expires"], 190)
        self.assertFalse(self.bridge.command({"schema": 1, "prompt_finished": opening["prompt_id"]}, 123)["accepted"])
        self.now[0] = 189.9
        self.assertEqual(self.command("1")["result"], "sstv_on")
        self.assertFalse(self.bridge.command({"schema": 1, "prompt_finished": selected["prompt_id"]}, 123)["accepted"])

    def test_wrong_peer_invalid_value_and_maintenance_cannot_change_decoders(self):
        self.assertEqual(self.bridge.command({"schema": 2, "command": CODE}, 999)["result"], "denied")
        self.command(); self.command("4")
        self.assertEqual(self.command("9")["result"], "invalid")
        self.command(); self.command("5")
        self.server.programming.status = lambda: {"pending": True}
        self.assertEqual(self.command("1")["result"], "busy")
        self.assertEqual(self.server.data_decoder.calls + self.server.sstv_decoder.calls, [])


class NativeDataManifestTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.server, self.bridge, *_ = fixture(self.folder)
        self.addCleanup(self.bridge.close)
        self.native = self.folder / "native"
        self.native.mkdir(mode=0o755)
        self.bridge.native_manifest = self.native / "dtmf-controls.json"
        self.manifest = {"schema": 1, "protocol": 2, "files": {}}
        for name in NATIVE_FILES:
            path = self.native / name
            path.parent.mkdir(mode=0o755, exist_ok=True)
            data = ("generic test native file: " + name).encode()
            path.write_bytes(data); path.chmod(0o644)
            self.manifest["files"][name] = hashlib.sha256(data).hexdigest()
        self.write_manifest()

    def write_manifest(self):
        self.bridge.native_manifest.write_text(json.dumps(self.manifest))
        self.bridge.native_manifest.chmod(0o644)

    def test_exact_installed_manifest_and_prompt_builder_agree(self):
        self.assertEqual(set(NATIVE_PROMPTS), set(prompts.PROMPTS))
        self.assertTrue(self.bridge.native_data_status()["available"])
        self.assertEqual(self.bridge.native_data_status()["protocol"], 2)
        self.assertTrue(self.bridge.status()["data_controls"]["available"])

    def test_changed_missing_and_writable_native_files_invalidate_cached_capability(self):
        path = self.native / "scripts/dtmf-client.py"
        original = path.read_bytes()
        self.assertTrue(self.bridge.native_data_status()["available"])
        path.write_bytes(b"!" + original[1:])
        self.assertFalse(self.bridge.native_data_status()["available"])
        path.write_bytes(original)
        self.assertTrue(self.bridge.native_data_status()["available"])
        path.chmod(0o664)
        self.assertFalse(self.bridge.native_data_status()["available"])
        path.chmod(0o644); path.unlink()
        self.assertFalse(self.bridge.native_data_status()["available"])

    def test_symlink_files_directories_and_incomplete_or_extra_manifests_are_refused(self):
        path = self.native / "prompts/aprs_on.wav"
        moved = self.native / "moved.wav"; path.rename(moved); path.symlink_to(moved)
        self.assertFalse(self.bridge.native_data_status()["available"])
        path.unlink(); moved.rename(path)
        scripts = self.native / "scripts"; renamed = self.native / "renamed"
        scripts.rename(renamed); scripts.symlink_to(renamed, target_is_directory=True)
        self.assertFalse(self.bridge.native_data_status()["available"])
        scripts.unlink(); renamed.rename(scripts)
        self.manifest["files"].pop("prompts/aprs_on.wav")
        self.write_manifest()
        self.assertFalse(self.bridge.native_data_status()["available"])
        self.manifest["files"]["../outside"] = "0" * 64
        self.write_manifest()
        self.assertFalse(self.bridge.native_data_status()["available"])


class DataClientTests(unittest.TestCase):
    def test_falls_back_only_when_old_dashboard_rejects_unknown_schema(self):
        legacy = DtmfControl.response("denied")
        accepted = DtmfControl.response("authenticated")
        with mock.patch.object(client, "_exchange", side_effect=[legacy, accepted]) as exchange:
            self.assertEqual(client.request(CODE), accepted)
        self.assertEqual([call.args[0]["schema"] for call in exchange.call_args_list], [2, 1])
        for response in (DtmfControl.response("recording_on"),
                         DtmfControl.response("denied", schema=2),
                         DtmfControl.response("sstv_on", schema=2)):
            with mock.patch.object(client, "_exchange", return_value=response) as exchange:
                self.assertEqual(client.request("1"), response)
            self.assertEqual(exchange.call_count, 1)

    def test_extended_fields_and_fixed_event_cannot_cross_into_legacy_schema(self):
        response = DtmfControl.response("sstv_on", 3, True, False, True,
                                        schema=2, aprs_enabled=False, sstv_enabled=True)
        self.assertEqual(client.response_event(response),
                         b"EVENT RepeaterLogic::box_control_result_data sstv_on 3 1 0 1 0 1 -\n")
        for changed in ({"schema": 1}, {"aprs_enabled": 1}, {"sstv_enabled": "true"},
                        {"result": "sstv_on;exec nope"}, {"extra": False}):
            with self.assertRaises(ValueError):
                client.validate_response({**response, **changed})
        missing = dict(response); missing.pop("sstv_enabled")
        with self.assertRaises(ValueError): client.validate_response(missing)


@unittest.skipUnless(shutil.which("tclsh"), "Tcl runtime unavailable")
class DataTclTests(unittest.TestCase):
    def test_generic_spoken_menu_and_all_current_settings_use_fixed_clips(self):
        wrapper = (ROOT / "scripts/repeater-box-events.tcl").read_text()
        program = "proc source {path} {}\nproc playFile {path} {puts $path}\n" + wrapper
        program += "\nRepeaterLogic::box_control_result_data authenticated 3 0 0 1 0 1\n"
        program += "RepeaterLogic::box_control_result_data choose_aprs 3 0 0 1 0 1\n"
        program += "RepeaterLogic::box_control_result_data status 3 0 0 1 0 1\n"
        program += "RepeaterLogic::box_control_result_data sstv_on 3 0 0 1 0 nope\n"
        result = subprocess.run([shutil.which("tclsh")], input=program, text=True, capture_output=True, check=True)
        self.assertEqual(result.stderr, "")
        self.assertEqual([Path(name).stem for name in result.stdout.splitlines()],
                         ["authenticated_data", "choose_aprs", "status", "pair_3", "recording_off",
                          "identification_on", "status_off", "aprs_off", "sstv_on"])

    def test_extended_live_queue_retains_both_protocols_and_prompt_completion(self):
        # Reuse the native policy harness without inheriting/rerunning its tests.
        harness = live_tests.LiveIdentificationTests(methodName="runTest")
        harness.run_tcl(r'''
set clips {}
proc playFile {path} {lappend ::clips [file tail $path]}
rename exec native_exec
set completions {}
proc exec {args} {lappend ::completions $args}
set token [string repeat a 32]
RepeaterLogic::box_control_result_data authenticated 2 0 0 1 1 0 $token
finish_reply 18000
assert {$clips eq {authenticated_data.wav}} "Extended menu not played"
assert {$RepeaterLogic::box_live_dtmf_until == $now + 30000} "Idle window not reset after full prompt"
assert {[llength $completions] == 1 && [lindex [lindex $completions 0] 3] eq $token} "Missing trusted completion"
set clips {}
RepeaterLogic::box_control_result_data choose_sstv 2 0 0 1 1 0
finish_reply
RepeaterLogic::box_control_result_data sstv_on 2 0 0 1 1 1
finish_reply
assert {$clips eq {choose_sstv.wav sstv_on.wav}} "Decoder confirmation changed"
set clips {}
RepeaterLogic::box_control_result authenticated 2 0 0 1
finish_reply
assert {$clips eq {authenticated.wav}} "Legacy menu changed"
RepeaterLogic::box_control_result aprs_on 2 0 0 1
assert {[llength $RepeaterLogic::box_live_control_results] == 0} "Legacy event accepted new result"
assert {$ids == 0} "ID interrupted a spoken response"
''')


if __name__ == "__main__":
    unittest.main()
