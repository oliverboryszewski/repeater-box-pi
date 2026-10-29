import configparser
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import unittest
import warnings
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def module(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), ROOT / "scripts" / (name + ".py"))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


client = module("dtmf-client")
configure = module("configure-dtmf")
guard = module("check-bench-config")
runner = module("run-svxlink")
prompts = module("build-dtmf-prompts")


class DtmfIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.handler = self.root / "events.tcl"
        self.handler.write_text("# protected test handler\n")
        self.handler.chmod(0o644)
        self.monitor = self.root / "monitor.conf"
        self.initial = (ROOT / "config/svxlink-rx-monitor.conf.example").read_text().replace("REPLACE_RX_ALSA_ID", "Stable_RX")
        self.monitor.write_text(self.initial)
        self.monitor.chmod(0o644)

    def parser(self):
        return guard.load_config(self.monitor)

    def migrate(self):
        return configure.migrate_monitor(self.monitor, os.getuid(), self.handler)

    def test_monitor_migration_preserves_receiver_and_state_and_cannot_select_tx(self):
        previous = self.parser()
        self.assertTrue(self.migrate())
        current = self.parser()
        for key in ("AUDIO_DEV", "RAW_AUDIO_UDP_DEST", "SQL_DET", "VOX_THRESH"):
            self.assertEqual(current["RxAIOC"][key], previous["RxAIOC"][key])
        self.assertEqual(current["RepeaterLogic"]["STATE_PTY"], previous["RepeaterLogic"]["STATE_PTY"])
        self.assertEqual(current["RepeaterLogic"]["TX"], "NONE")
        self.assertEqual(current["RepeaterLogic"]["NO_REPEAT"], "1")
        self.assertEqual(current["RepeaterLogic"]["ONLINE"], "1")
        self.assertEqual(current["RxAIOC"]["DTMF_DEC_TYPE"], "INTERNAL")
        self.assertEqual(current["RxAIOC"]["DTMF_MUTING"], "1")
        self.assertIn("LOGICS=RepeaterLogic", self.monitor.read_text())
        self.assertFalse(self.migrate())
        self.assertFalse((self.root / "dtmf.json").exists())

    def test_online_exception_requires_every_transmit_and_command_guard(self):
        self.migrate()
        parser = self.parser()
        guard.validate_parser(parser, True, self.handler, os.getuid())
        for section, key, value in (("RepeaterLogic", "TX", "TxAIOC"),
                                    ("RepeaterLogic", "NO_REPEAT", "0"),
                                    ("RepeaterLogic", "EVENT_HANDLER", "/tmp/other.tcl"),
                                    ("RepeaterLogic", "ONLINE_CMD", "123"),
                                    ("RepeaterLogic", "MODULES", "ModuleParrot"),
                                    ("RepeaterLogic", "MACROS", "Macros"),
                                    ("RepeaterLogic", "EXEC_CMD_ON_SQL_CLOSE", "501"),
                                    ("RepeaterLogic", "COMMAND_PTY", "/tmp/control"),
                                    ("RxAIOC", "DTMF_DEC_TYPE", "NONE"),
                                    ("RxAIOC", "DTMF_MUTING", "0"),
                                    ("GLOBAL", "LINKS", "Link"),
                                    ("GLOBAL", "CFG_DIR", "/tmp/includes")):
            with self.subTest(key=key):
                changed = self.parser(); changed[section][key] = value
                with self.assertRaises(ValueError):
                    guard.validate_parser(changed, True, self.handler, os.getuid())

    def test_migration_refuses_unsafe_sources_without_replacing_them(self):
        for text in (self.initial.replace("TX=NONE", "TX=TxAIOC"),
                     self.initial.replace("ONLINE=0", "ONLINE=1"),
                     self.initial.replace("LOGICS=RepeaterLogic", "LOGICS=Other")):
            self.monitor.write_text(text)
            with self.assertRaises((ValueError, KeyError)):
                self.migrate()
            self.assertEqual(self.monitor.read_text(), text)
        self.monitor.write_text(self.initial)
        self.handler.chmod(0o666)
        with self.assertRaises(ValueError): self.migrate()
        self.assertEqual(self.monitor.read_text(), self.initial)

    def test_migration_rejects_symlink_and_missing_monitor_is_noop(self):
        self.monitor.unlink()
        self.assertFalse(self.migrate())
        target = self.root / "target"; target.write_text(self.initial)
        self.monitor.symlink_to(target)
        with self.assertRaises(OSError): self.migrate()
        self.assertEqual(target.read_text(), self.initial)

    def test_control_record_is_hash_only_and_uses_exact_reserved_symbol_alphabet(self):
        code = "".join(str(number) for number in (8, 6, 4, 2, 0, 9))
        record = configure.code_record(code)
        self.assertEqual(set(record), {"schema", "algorithm", "iterations", "salt", "digest"})
        self.assertNotIn(code, json.dumps(record))
        self.assertEqual(record["digest"], hashlib.pbkdf2_hmac("sha256", code.encode("ascii"), bytes.fromhex(record["salt"]), 600000).hex())
        for invalid in ("12345", "1234567", "abcdef", "１２３４５６", True, None, "12345\n"):
            with self.assertRaises(ValueError): configure.code_record(invalid)
        configure.code_record("AB1C29")
        for invalid in ("AB*123", "AB#123", "ABe123"):
            with self.assertRaises(ValueError): configure.code_record(invalid)

    def test_getpass_warning_is_fatal_before_any_echoed_input_fallback(self):
        fallback = []
        def unavailable(_prompt):
            warnings.warn("Cannot control echo", configure.getpass.GetPassWarning)
            fallback.append(True)
            return "unsafe"
        with mock.patch.object(configure.getpass, "getpass", side_effect=unavailable):
            for prompt in ("first", "repeat"):
                with self.assertRaises(ValueError): configure.hidden_prompt(prompt)
        self.assertEqual(fallback, [])

    def test_root_queue_saves_only_valid_hash_then_publishes_result_before_removing_request(self):
        code = configure.code_record("AB1C29")
        request = self.root / "request.json"; result = self.root / "result.json"
        target = self.root / "dtmf.json"; lock = self.root / "lock"
        lock.touch(mode=0o600)
        request.write_text(json.dumps({"schema":1,"request_id":"a"*32,"record":code})); request.chmod(0o600)
        options = dict(request_path=request,result_path=result,request_owner_uid=os.getuid(),owner_uid=os.getuid(),
                       config_path=target,monitor_path=self.monitor,lock_path=lock,event_handler=self.handler)
        with mock.patch.object(configure,"publish_result",side_effect=OSError("disk error")):
            with self.assertRaises(OSError): configure.process_request(**options)
        self.assertTrue(request.exists())
        saved = configure.process_request(**options)
        self.assertTrue(saved["success"]); self.assertFalse(request.exists())
        self.assertEqual(json.loads(target.read_text()),code)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode),0o640)
        self.assertEqual(stat.S_IMODE(result.stat().st_mode),0o640)
        self.assertEqual(set(json.loads(result.read_text())),{"schema","request_id","success","message","monitor_restart_required"})
        self.assertNotIn("digest",result.read_text())

    def test_root_queue_rejects_malformed_records_lock_contention_and_symlinks(self):
        record = configure.code_record("AB1C29")
        request = self.root/"request.json"; result=self.root/"result.json"; target=self.root/"dtmf.json"; lock=self.root/"lock"
        lock.touch(mode=0o600)
        options=dict(request_path=request,result_path=result,request_owner_uid=os.getuid(),owner_uid=os.getuid(),
                     config_path=target,monitor_path=self.monitor,lock_path=lock,event_handler=self.handler)
        for changed in ({**record,"iterations":100000},{**record,"schema":True},{**record,"path":"/tmp/other"}, {**record,"digest":"raw"}):
            request.write_text(json.dumps({"schema":1,"request_id":"a"*32,"record":changed}));request.chmod(0o600)
            self.assertFalse(configure.process_request(**options)["success"])
            self.assertFalse(target.exists());self.assertFalse(request.exists())
        request.write_text(json.dumps({"schema":1,"request_id":"a"*32,"record":record}));request.chmod(0o600)
        descriptor=os.open(lock,os.O_RDWR)
        try:
            configure.fcntl.flock(descriptor,configure.fcntl.LOCK_EX|configure.fcntl.LOCK_NB)
            self.assertFalse(configure.process_request(**options)["success"])
            self.assertFalse(target.exists())
        finally:os.close(descriptor)
        foreign=self.root/"foreign";foreign.write_text("protected")
        request.symlink_to(foreign)
        with self.assertRaises(OSError):configure.process_request(**options)
        self.assertEqual(foreign.read_text(),"protected")

    def test_root_path_worker_does_not_weaken_dashboard_service_or_grant_sudo(self):
        dashboard=(ROOT/"systemd/repeater-dashboard.service").read_text()
        self.assertIn("NoNewPrivileges=true",dashboard)
        self.assertIn("ProtectSystem=strict",dashboard)
        service=(ROOT/"systemd/repeater-dtmf-code.service").read_text()
        self.assertIn("configure-dtmf.py --request",service)
        self.assertIn("PathExists=/var/lib/repeater-dashboard/dtmf-code-request.json",(ROOT/"systemd/repeater-dtmf-code.path").read_text())
        self.assertFalse((ROOT/"config/repeater-dtmf.sudoers").exists())

    def test_root_code_writer_refuses_held_or_malformed_radio_state_before_any_change(self):
        state=self.root/"radio-state.json"; lock=self.root/"lock"; lock.touch(mode=0o600)
        target=self.root/"code.json"; record=configure.code_record("AB1C29")
        options=dict(config_path=target,monitor_path=self.monitor,lock_path=lock,owner_uid=os.getuid(),event_handler=self.handler,state_path=state)
        for value in ({"rx":{"verification_required":True}}, {"_pair":{"verification_required":True}},
                      {"tx":{"verification_required":"false"}}, {"rx":[]}, []):
            state.write_text(json.dumps(value));state.chmod(0o640)
            with self.assertRaises(ValueError):configure.save_record(record,**options)
            self.assertFalse(target.exists());self.assertEqual(self.monitor.read_text(),self.initial)
        state.write_text('{"rx":{},"rx":{}}')
        with self.assertRaises(ValueError):configure.save_record(record,**options)
        state.write_text(json.dumps({"rx":{"verification_required":False}}))
        self.assertTrue(configure.save_record(record,**options)["ok"])

    def test_private_config_is_durable_640_and_rejects_unsafe_existing_file(self):
        path = self.root / "dtmf.json"
        configure.atomic_write(path, b"{}\n", 0o640, os.getuid())
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)
        self.assertEqual(path.read_bytes(), b"{}\n")
        path.chmod(0o644)
        with self.assertRaises(ValueError): configure.atomic_write(path, b"changed", 0o640, os.getuid())
        self.assertEqual(path.read_bytes(), b"{}\n")

    def test_client_protocol_is_bounded_and_carries_command_in_socket_payload(self):
        connection = mock.MagicMock()
        connection.__enter__.return_value = connection
        response = {"schema": 1, "result": "status", "pair_index": 2, "recording": False, "announce_status": False, "identification_enabled": True}
        connection.recv.return_value = (json.dumps(response) + "\n").encode("ascii")
        with mock.patch.object(client.socket, "socket", return_value=connection):
            self.assertEqual(client.request("864209"), response)
        self.assertEqual(json.loads(connection.sendall.call_args.args[0]), {"schema": 1, "command": "864209"})
        for invalid in ("864209#", "864209\n", "8642090", "１２３４５６０"):
            with self.assertRaises(ValueError): client.request(invalid)

    def test_client_rejects_truncated_oversized_or_injectable_responses(self):
        for data in (b"x" * 257, b"{}", b"{}\n{}\n", b'{"schema":1,"result":"status;exec x","pair_index":1,"recording":false}\n'):
            connection = mock.MagicMock(); connection.__enter__.return_value = connection
            connection.recv.side_effect = [data, b""]
            with mock.patch.object(client.socket, "socket", return_value=connection), self.assertRaises(ValueError):
                client.request("864209")
        for change in ({"pair_index": True}, {"pair_index": 10}, {"recording": 1}, {"identification_enabled": 1}, {"schema": True}, {"result": []}, {"extra": 1}):
            response = {"schema": 1, "result": "status", "pair_index": 1, "recording": False, "announce_status": False, "identification_enabled": True, **change}
            with self.assertRaises(ValueError): client.validate_response(response)

    def test_client_silences_unauthorized_responses_and_event_tokens_are_fixed(self):
        for result in ("denied", "disabled"):
            response = {"schema": 1, "result": result, "pair_index": 0, "recording": False, "announce_status": False, "identification_enabled": False}
            self.assertIsNone(client.response_event(response))
        event = client.response_event({"schema": 1, "result": "recording_on", "pair_index": 3, "recording": True, "announce_status": False, "identification_enabled": True})
        self.assertEqual(event, b"EVENT RepeaterLogic::box_control_result recording_on 3 1 0 1\n")
        regular = self.root / "ordinary"; regular.write_text("")
        self.assertFalse(client.announce({"schema": 1, "result": "status", "pair_index": 1, "recording": False, "announce_status": False, "identification_enabled": True}, [regular]))
        self.assertEqual(regular.read_text(), "")

    def test_client_identification_tokens_keep_the_exact_response_schema(self):
        for result in ("choose_identification", "identification_on", "identification_off"):
            response = {"schema": 1, "result": result, "pair_index": 2, "recording": False, "announce_status": True, "identification_enabled": True}
            self.assertEqual(client.validate_response(response), response)
            self.assertEqual(client.response_event(response),
                             ("EVENT RepeaterLogic::box_control_result " + result + " 2 0 1 1\n").encode("ascii"))
            with self.assertRaises(ValueError):
                client.validate_response({**response, "automatic_station_id": False})
        with self.assertRaises(ValueError):
            client.validate_response({**response, "result": "identification_off;exec nope"})

    def test_client_never_writes_radio_or_foreign_tty(self):
        response = {"schema": 1, "result": "status", "pair_index": 1, "recording": False, "announce_status": False, "identification_enabled": True}
        path = mock.Mock(); path.resolve.return_value = Path("/dev/ttyACM0")
        with mock.patch.object(client.os, "open") as opened:
            self.assertFalse(client.announce(response, [path]))
            opened.assert_not_called()
        path.resolve.return_value = Path("/dev/pts/5")
        with mock.patch.object(client.os, "open", return_value=4), mock.patch.object(client.os, "close"), \
                mock.patch.object(client.os, "fstat", return_value=mock.Mock(st_mode=stat.S_IFCHR|0o620, st_uid=os.getuid()+1)), \
                mock.patch.object(client.os, "write") as written:
            self.assertFalse(client.announce(response, [path]))
            written.assert_not_called()

    def test_digit_and_command_logging_is_filtered_without_hiding_engine_errors(self):
        for sensitive in ("RepeaterLogic: digit=8\n", "2026-01-01: RepeaterLogic: digit=#\n", "Unable to handle event: dtmf_cmd_received secret", "Processing macro command: D123", "DTMF command received: secret", 'RepeaterLogic: Ignoring DTMF digit "8"', 'DTMF digit "8" detected', '*** ERROR: Unable to handle event: dtmf_digit_received 7 100 in logic RepeaterLogic'):
            self.assertFalse(runner.safe_line(sensitive))
        for ordinary in ("Starting SvxLink", "RxAIOC: The squelch is OPEN", "*** ERROR: Audio device failed", "Setting logic ONLINE"):
            self.assertTrue(runner.safe_line(ordinary))

    def test_runner_accepts_no_arbitrary_binary_or_configuration(self):
        with self.assertRaises(ValueError): runner.run("/tmp/user.conf")
        for service in ("repeater-engine.service", "repeater-rx-monitor.service"):
            text = (ROOT / "systemd" / service).read_text()
            self.assertIn("/scripts/run-svxlink.py /etc/repeater-box/", text)
            self.assertIn("repeater-control", text)
        self.assertIn("repeater-box-control", (ROOT / "systemd/repeater-dashboard.service").read_text())

    def test_prompts_are_fixed_generic_and_installer_preserves_maintenance_guards(self):
        self.assertEqual(set(prompts.PROMPTS), {"status", "recording_on", "recording_off", "busy", "unavailable", "pair_unknown", "authenticated", "choose_status", "choose_recording", "choose_identification", "status_on", "status_off", "identification_on", "identification_off", "cancelled", "invalid", "expired"} | {"pair_"+str(number) for number in range(1,10)})
        self.assertTrue(all("MHz" not in text and "password" not in text for text in prompts.PROMPTS.values()))
        installer = (ROOT / "scripts/install-pi.sh").read_text()
        self.assertLess(installer.index("flock -n 9"), installer.index("module.migrate_monitor()"))
        self.assertLess(installer.index("if [[ $radio_verification_pending == 1 ]]"), installer.index("systemctl restart repeater-rx-monitor.service"))
        self.assertIn("espeak-ng sox", installer)
        self.assertNotIn("setOnline", (ROOT / "scripts/repeater-box-events.tcl").read_text())

    @unittest.skipUnless(shutil.which("tclsh"), "Tcl runtime unavailable")
    def test_native_tcl_handler_consumes_unknown_commands_and_only_queues_fixed_prompts(self):
        wrapper = (ROOT / "scripts/repeater-box-events.tcl").read_text()
        program = "proc source {path} {}\nproc playFile {path} {puts $path}\n" + wrapper + "\n"
        program += 'puts [RepeaterLogic::dtmf_cmd_received {9;exec nope}]\n'
        program += 'RepeaterLogic::box_control_result status 2 1 0 1\n'
        program += 'RepeaterLogic::box_control_result {status;exec nope} 2 1 0 1\n'
        program += 'RepeaterLogic::box_control_result status 10 1 0 1\n'
        result = subprocess.run([shutil.which("tclsh")], input=program, text=True, capture_output=True, check=True)
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.stdout.splitlines(), ["1", "/opt/repeater-box/prompts/status.wav", "/opt/repeater-box/prompts/pair_2.wav", "/opt/repeater-box/prompts/recording_on.wav", "/opt/repeater-box/prompts/identification_on.wav", "/opt/repeater-box/prompts/status_off.wav"])

    @unittest.skipUnless(shutil.which("tclsh"), "Tcl runtime unavailable")
    def test_native_identification_prompts_and_optional_status_are_fixed(self):
        wrapper = (ROOT / "scripts/repeater-box-events.tcl").read_text()
        program = "proc source {path} {}\nproc playFile {path} {puts $path}\n" + wrapper + "\n"
        program += 'RepeaterLogic::box_control_result choose_identification 2 0 0 1\n'
        program += 'RepeaterLogic::box_control_result identification_off 2 0 0 0\n'
        program += 'RepeaterLogic::box_control_result identification_on 2 0 1 1\n'
        program += 'RepeaterLogic::box_clip {identification_on;exec nope}\n'
        result = subprocess.run([shutil.which("tclsh")], input=program, text=True, capture_output=True, check=True)
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.stdout.splitlines(), [
            "/opt/repeater-box/prompts/choose_identification.wav",
            "/opt/repeater-box/prompts/identification_off.wav",
            "/opt/repeater-box/prompts/identification_on.wav",
            "/opt/repeater-box/prompts/status.wav",
            "/opt/repeater-box/prompts/pair_2.wav",
            "/opt/repeater-box/prompts/recording_off.wav"])

    @unittest.skipUnless(shutil.which("tclsh"), "Tcl runtime unavailable")
    def test_native_capture_requires_one_hash_and_one_release_per_stage_and_clears_buffer(self):
        wrapper=(ROOT/"scripts/repeater-box-events.tcl").read_text()
        program="""proc source {path} {}
namespace eval Logic {proc squelch_open {a b} {}; proc every_second {} {}}
proc exec {args} {puts "REQUEST:[string trim [lindex $args 3]]"}
set now 1000
"""+wrapper+"""
proc RepeaterLogic::box_now {} {return $::now}
proc burst {value} {
  RepeaterLogic::squelch_open rx 1
  foreach symbol [split $value ""] {
    if {[RepeaterLogic::dtmf_digit_received $symbol 100] != 1} {error "native fallback reached"}
  }
  RepeaterLogic::squelch_open rx 0
  incr ::now 499
  RepeaterLogic::every_second
  puts "BEFORE"
  incr ::now 1
  RepeaterLogic::every_second
  if {$RepeaterLogic::box_buffer ne ""} {error "raw code retained"}
  incr ::now 1000
}
burst AB1C29#
burst 2#
burst 1#
burst AB1C29#2#1#
burst AB1C29
burst 1##
burst AB*123#
burst ########
RepeaterLogic::squelch_open rx 0
incr now 600
RepeaterLogic::dtmf_digit_received A 100
incr now 100
RepeaterLogic::squelch_open rx 1
foreach symbol [split B1C29# ""] {RepeaterLogic::dtmf_digit_received $symbol 100}
RepeaterLogic::squelch_open rx 0
incr now 500
RepeaterLogic::every_second
"""
        result=subprocess.run([shutil.which("tclsh")],input=program,text=True,capture_output=True,check=True)
        self.assertEqual(result.stderr,"")
        self.assertEqual(result.stdout.splitlines(),["BEFORE","REQUEST:AB1C29","BEFORE","REQUEST:2","BEFORE","REQUEST:1","BEFORE","BEFORE","BEFORE","BEFORE","REQUEST:*","BEFORE","REQUEST:AB1C29"])


if __name__ == "__main__":
    unittest.main()
