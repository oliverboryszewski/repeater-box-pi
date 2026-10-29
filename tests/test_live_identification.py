"""Run the live Tcl ID policy with stubbed native audio and RF callbacks."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
HANDLER = ROOT / "scripts/repeater-live-events.tcl"
SHARED = ROOT / "scripts/repeater-box-events.tcl"


@unittest.skipUnless(shutil.which("tclsh"), "Tcl runtime unavailable")
class LiveIdentificationTests(unittest.TestCase):
    def run_tcl(self, body, callsign="K1ABC", online="1"):
        # Keep the real shared DTMF procedures; replace only the installed
        # vendor source load, audio primitives and the native event clock.
        program = r'''
set now 1000
set ids 0
set logic_ticks 0
set dtmf_ticks 0
set ::mycall @CALLSIGN@
namespace eval Logic {
  variable CFG_ONLINE @ONLINE@
  proc every_second {} {incr ::logic_ticks}
  proc squelch_open {a b} {}
}
namespace eval CW {
  array set letters {
    A .- B -... C -.-. D -.. E . F ..-. G --. H .... I .. J .---
    K -.- L .-.. M -- N -. O --- P .--. Q --.- R .-. S ... T -
    U ..- V ...- W .-- X -..- Y -.-- Z --..
    0 ----- 1 .---- 2 ..--- 3 ...-- 4 ....- 5 ..... 6 -....
    7 --... 8 ---.. 9 ----. / -..-.
  }
  proc play {call cpm pitch amp} {
    if {$call ne $::mycall || $cpm != 100 || $pitch != 800 || $amp != -12} {
      error "Unexpected CW output"
    }
    incr ::ids
  }
}
proc playSilence {duration} {}
proc playMsg {args} {error "Voice clips are forbidden"}
proc spellWord {args} {error "Voice spelling is forbidden"}
proc playFile {args} {error "Unexpected file playback"}
proc assert {condition message} {
  if {![uplevel 1 [list expr $condition]]} {error $message}
}
rename source original_source
proc source {path} {
  if {$path eq "/usr/share/svxlink/events.tcl"} {return}
  uplevel 1 [list original_source $path]
}
'''.replace("@CALLSIGN@", "{" + callsign + "}").replace("@ONLINE@", online)
        program += "source {" + str(HANDLER) + "}\n"
        program += r'''
rename RepeaterLogic::box_live_now RepeaterLogic::box_live_native_clock
proc RepeaterLogic::box_live_now {} {return $::now}
proc RepeaterLogic::box_tick {} {incr ::dtmf_ticks}
'''
        program += body
        # Tcl's interactive stdin mode can print an error and still exit 0.
        result = subprocess.run([shutil.which("tclsh")], input=program,
                                text=True, capture_output=True, check=True)
        self.assertEqual(result.stderr, "", result.stdout)
        return result.stdout

    def test_quiet_startup_idle_and_online_changes(self):
        self.run_tcl(r'''
RepeaterLogic::startup
RepeaterLogic::logic_online 1
for {set n 0} {$n < 20} {incr n} {
  incr now 60000
  RepeaterLogic::every_second
  RepeaterLogic::checkPeriodicIdentify
}
assert {$ids == 0} "Idle service transmitted"
assert {$logic_ticks == 20 && $dtmf_ticks == 20} "Existing ticks lost"
''')

    def test_native_clock_reads_only_bounded_linux_uptime(self):
        with tempfile.TemporaryDirectory() as folder:
            fixture = Path(folder) / "uptime"
            fixture.write_text("12345.67 23456.78\n")
            self.run_tcl(r'''
rename open original_open
proc open {path mode} {
  if {$path ne "/proc/uptime" || $mode ne "r"} {error "Unexpected clock file"}
  return [original_open {FIXTURE} $mode]
}
assert {[RepeaterLogic::box_live_native_clock] == 12345670} "Uptime conversion failed"
'''.replace("FIXTURE", str(fixture)))

    def test_receive_session_finishes_with_one_cw_id_without_loop(self):
        self.run_tcl(r'''
RepeaterLogic::repeater_up SQL_OPEN
RepeaterLogic::transmit 1
incr now 1000
RepeaterLogic::repeater_down IDLE
assert {$ids == 1} "Missing end ID"
RepeaterLogic::transmit 0
incr now 600000
RepeaterLogic::every_second
assert {$ids == 1} "ID caused another ID"
''')

    def test_dtmf_prompt_only_session_is_identified(self):
        self.run_tcl(r'''
RepeaterLogic::repeater_up PTY_DTMF_CMD
RepeaterLogic::transmit 1
incr now 5000
RepeaterLogic::repeater_down IDLE
RepeaterLogic::transmit 0
assert {$ids == 1} "Prompt-only transmission lacked ID"
''')

    def test_elapsed_deadline_rearms_without_ptt_edge(self):
        self.run_tcl(r'''
RepeaterLogic::repeater_up SQL_OPEN
RepeaterLogic::transmit 1
incr now 539999
RepeaterLogic::every_second
assert {$ids == 0} "ID preceded deadline"
incr now 1
RepeaterLogic::every_second
assert {$ids == 1} "Missing nine-minute ID"
incr now 540000
RepeaterLogic::every_second
assert {$ids == 2} "Sustained transmission did not re-arm"
RepeaterLogic::repeater_down IDLE
RepeaterLogic::transmit 0
assert {$ids == 3} "ID queued reentrantly during native OFF teardown"
RepeaterLogic::every_second
assert {$ids == 4} "Missing fresh ID after timeout latch cleared"
RepeaterLogic::transmit 1
incr now 5000
RepeaterLogic::transmit 0
assert {$ids == 4} "Timeout recovery ID looped"
''')

    def test_end_id_is_retried_after_native_timeout_latch_clears(self):
        self.run_tcl(r'''
RepeaterLogic::repeater_up SQL_OPEN
RepeaterLogic::transmit 1
incr now 20000
# Native timeout has unkeyed RF but emits no transmit 0 callback yet.
RepeaterLogic::repeater_down IDLE
assert {$ids == 1} "Expected the first queued end ID"
# Native LocalTx clears its timeout latch before this logical OFF callback.
RepeaterLogic::transmit 0
assert {$ids == 1} "ID queued reentrantly during native OFF teardown"
RepeaterLogic::every_second
assert {$ids == 2} "Missing fresh ID after timeout"
RepeaterLogic::transmit 1
incr now 5000
RepeaterLogic::transmit 0
assert {$ids == 2} "Fresh identification caused a retry loop"
''')

    def test_new_receive_activity_during_end_id_is_not_lost(self):
        self.run_tcl(r'''
RepeaterLogic::repeater_up SQL_OPEN
RepeaterLogic::transmit 1
RepeaterLogic::repeater_down IDLE
assert {$ids == 1} "Missing first end ID"
RepeaterLogic::repeater_up SQL_RPT_REOPEN
incr now 2000
RepeaterLogic::repeater_down IDLE
RepeaterLogic::transmit 0
assert {$ids == 2} "Reopened session was mistaken for own CW"
''')

    def test_audio_outside_repeater_session_gets_one_id(self):
        self.run_tcl(r'''
RepeaterLogic::transmit 1
RepeaterLogic::transmit 0
assert {$ids == 0} "Outside audio re-keyed during OFF teardown"
RepeaterLogic::every_second
assert {$ids == 1} "Outside audio lacked ID"
RepeaterLogic::transmit 1
RepeaterLogic::transmit 0
assert {$ids == 1} "Own CW created a loop"
''')

    def test_offline_clears_pending_and_does_not_identify(self):
        self.run_tcl(r'''
RepeaterLogic::repeater_up SQL_OPEN
RepeaterLogic::transmit 1
RepeaterLogic::logic_online 0
RepeaterLogic::repeater_down OFFLINE
RepeaterLogic::transmit 0
incr now 600000
RepeaterLogic::every_second
RepeaterLogic::manual_identification
assert {$ids == 0} "Offline service attempted identification"
''')

    def test_offline_cancels_a_deferred_id_without_a_startup_burst(self):
        self.run_tcl(r'''
RepeaterLogic::transmit 1
RepeaterLogic::transmit 0
RepeaterLogic::logic_online 0
RepeaterLogic::every_second
RepeaterLogic::logic_online 1
RepeaterLogic::every_second
assert {$ids == 0} "An old deferred ID transmitted after reactivation"
''')

    def test_new_traffic_supersedes_idle_deferred_recovery(self):
        self.run_tcl(r'''
RepeaterLogic::transmit 1
RepeaterLogic::transmit 0
RepeaterLogic::repeater_up SQL_OPEN
RepeaterLogic::transmit 1
RepeaterLogic::every_second
assert {$ids == 0} "Deferred recovery interrupted new traffic"
incr now 1000
RepeaterLogic::repeater_down IDLE
RepeaterLogic::transmit 0
RepeaterLogic::every_second
assert {$ids == 1} "New session did not consume deferred identification"
''')

    def test_all_native_voice_activation_hooks_are_replaced(self):
        self.run_tcl(r'''
foreach reason {SQL_OPEN CTCSS_OPEN SQL_RPT_REOPEN SQL_CLOSE AUDIO MODULE PTY_DTMF_CMD} {
  RepeaterLogic::repeater_up $reason
  RepeaterLogic::transmit 1
  RepeaterLogic::repeater_down IDLE
  RepeaterLogic::transmit 0
}
RepeaterLogic::logic_online 1
RepeaterLogic::manual_identification
RepeaterLogic::send_rgr_sound
RepeaterLogic::repeater_idle
RepeaterLogic::identify_nag
assert {$ids > 0} "CW output missing"
''')

    def test_callsign_is_locally_validated_before_initialization(self):
        for value in ("N0CALL", "N0CALL/P", "REPLACE1", "CALL", "1234", "A1",
                      "K1ABC;exec", "k1abc", "A1/ABC/DEF", "A1" * 8):
            with self.subTest(callsign=value):
                with self.assertRaises(AssertionError):
                    self.run_tcl("", callsign=value)
        self.run_tcl("RepeaterLogic::manual_identification\n", callsign="K1ABC/P")
        with self.assertRaises(AssertionError):
            self.run_tcl("", callsign="K00000/0000000")

    def test_status_announcement_preference_does_not_disable_identification(self):
        self.run_tcl(r'''
set announce_status 0
rename playFile forbidden_file
proc playFile {path} {lappend ::clips $path}
set clips {}
RepeaterLogic::repeater_up PTY_DTMF_CMD
RepeaterLogic::transmit 1
RepeaterLogic::box_control_result recording_off 1 0 $announce_status
assert {[llength $clips] == 1} "Optional status announcement was enabled"
RepeaterLogic::repeater_down IDLE
RepeaterLogic::transmit 0
assert {$ids == 1} "Status preference disabled station identification"
''')

    def test_handler_sources_restricted_dtmf_policy_without_rf_password_defaults(self):
        text = HANDLER.read_text()
        self.assertIn("repeater-box-events.tcl", text)
        self.assertNotRegex(text, r"(?m)^\s*after\s")
        self.assertNotIn("playMsg", text)
        self.assertNotIn("spellWord", text)
        self.assertTrue(SHARED.is_file())


if __name__ == "__main__":
    unittest.main()
