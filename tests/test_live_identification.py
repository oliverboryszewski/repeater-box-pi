"""Run the live Tcl ID policy with stubbed native audio and RF callbacks."""
from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
HANDLER = ROOT / "scripts/repeater-live-events.tcl"
SHARED = ROOT / "scripts/repeater-box-events.tcl"


@unittest.skipUnless(shutil.which("tclsh"), "Tcl runtime unavailable")
class LiveIdentificationTests(unittest.TestCase):
    def run_tcl(self, body, callsign="K1ABC", online="1", interval="9"):
        # Keep the real shared DTMF procedures; replace only the installed
        # vendor source load, audio primitives and the native event clock.
        program = r'''
set now 1000
set ids 0
set logic_ticks 0
set dtmf_ticks 0
set auto_value ""
set ::mycall @CALLSIGN@
namespace eval Logic {
  variable CFG_ONLINE @ONLINE@
  variable CFG_BOX_ID_INTERVAL_MINUTES @INTERVAL@
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
'''.replace("@CALLSIGN@", "{" + callsign + "}").replace("@ONLINE@", online).replace("@INTERVAL@", interval)
        program += "source {" + str(HANDLER) + "}\n"
        program += r'''
rename RepeaterLogic::box_live_now RepeaterLogic::box_live_native_clock
proc RepeaterLogic::box_live_now {} {return $::now}
rename RepeaterLogic::box_live_read_auto RepeaterLogic::box_live_native_auto_reader
proc RepeaterLogic::box_live_read_auto {} {return $::auto_value}
proc RepeaterLogic::box_tick {} {incr ::dtmf_ticks}
RepeaterLogic::startup
proc turn {{length 1000} {reason SQL_OPEN}} {
  RepeaterLogic::squelch_open rx 1
  RepeaterLogic::repeater_up $reason
  RepeaterLogic::transmit 1
  incr ::now $length
  RepeaterLogic::squelch_open rx 0
  RepeaterLogic::repeater_down IDLE
  RepeaterLogic::transmit 0
}
proc quiet {duration} {
  incr ::now $duration
  RepeaterLogic::every_second
}
proc finish_id {} {
  RepeaterLogic::transmit 1
  incr ::now 3000
  RepeaterLogic::transmit 0
}
'''
        program += body
        # Tcl's interactive stdin mode can print an error and still exit 0.
        result = subprocess.run([shutil.which("tclsh")], input=program,
                                text=True, capture_output=True, check=True)
        self.assertEqual(result.stderr, "", result.stdout)
        return result.stdout

    def test_quiet_startup_idle_and_online_changes(self):
        self.run_tcl(r'''
RepeaterLogic::logic_online 1
for {set n 0} {$n < 20} {incr n} {quiet 60000}
assert {$ids == 0} "Idle service transmitted"
assert {$logic_ticks == 20 && $dtmf_ticks == 20} "Existing ticks lost"
''')

    def test_several_speaker_turns_share_one_end_id_after_thirty_seconds(self):
        self.run_tcl(r'''
for {set n 0} {$n < 4} {incr n} {
  turn
  quiet 5000
  assert {$ids == 0} "A speaker turn triggered CW"
}
quiet 24999
assert {$ids == 0} "Conversation ended before thirty seconds"
quiet 1
assert {$ids == 1} "Missing conversation-end ID"
finish_id
quiet 600000
assert {$ids == 1} "Own identification caused another ID"
''')

    def test_recent_id_suppresses_new_short_session_and_later_idle_beacon(self):
        self.run_tcl(r'''
turn
quiet 30000
finish_id
turn
quiet 30000
assert {$ids == 1} "End ID ignored the minimum interval"
quiet 600000
assert {$ids == 1} "Old conversation became an idle beacon"
''')

    def test_nine_minute_deadline_waits_for_clear_gap(self):
        self.run_tcl(r'''
turn
quiet 30000
set last_id $now
finish_id
RepeaterLogic::squelch_open rx 1
RepeaterLogic::repeater_up SQL_OPEN
RepeaterLogic::transmit 1
set now [expr {$last_id + 539999}]
RepeaterLogic::every_second
assert {$ids == 1} "ID preceded the elapsed deadline"
incr now 1
RepeaterLogic::every_second
assert {$ids == 1} "CW interrupted an active receiver/transmitter"
RepeaterLogic::squelch_open rx 0
RepeaterLogic::repeater_down IDLE
RepeaterLogic::transmit 0
assert {$ids == 1} "CW queued inside PTT teardown"
RepeaterLogic::every_second
assert {$ids == 2} "Overdue ID did not use the clear gap"
finish_id
quiet 600000
assert {$ids == 2} "Periodic ID caused an idle loop"
''')

    def test_testing_interval_suppresses_first_thirty_minutes(self):
        self.run_tcl(r'''
for {set n 0} {$n < 179} {incr n} {
  turn 1000
  quiet 9000
  assert {$ids == 0} "Testing mode identified before thirty minutes"
}
turn 1000
quiet 9000
assert {$ids == 1} "Testing deadline did not identify active conversation"
finish_id
turn
quiet 30000
assert {$ids == 1} "Testing interval was bypassed by a short session"
quiet 1800000
assert {$ids == 1} "Testing mode emitted an idle beacon"
''', interval="30")

    def test_testing_mode_does_not_replay_finished_early_traffic_at_deadline(self):
        self.run_tcl(r'''
turn
quiet 30000
assert {$ids == 0} "Testing mode identified an early short test"
quiet 1800000
assert {$ids == 0} "Finished test traffic produced a later beacon"
''', interval="30")

    def test_dtmf_three_step_menu_and_spoken_prompts_are_not_interrupted(self):
        self.run_tcl(r'''
rename playFile forbidden_file
proc playFile {path} {lappend ::clips $path}
set clips {}
foreach result {authenticated choose_recording recording_on} {
  RepeaterLogic::squelch_open rx 1
  RepeaterLogic::repeater_up SQL_OPEN
  RepeaterLogic::transmit 1
  foreach digit [split 2# ""] {
    assert {[RepeaterLogic::dtmf_digit_received $digit 100] == 1} "DTMF fallback exposed"
  }
  RepeaterLogic::squelch_open rx 0
  RepeaterLogic::box_control_result $result 1 1 0
  incr now 3000
  RepeaterLogic::every_second
  assert {$ids == 0} "CW played over a spoken menu prompt"
  RepeaterLogic::repeater_down IDLE
  RepeaterLogic::transmit 0
  quiet 2000
  assert {$ids == 0} "CW played between menu steps"
}
assert {[llength $clips] == 3} "Spoken menu was changed"
quiet 27999
assert {$ids == 0} "Menu conversation ended too early"
quiet 1
assert {$ids == 1} "Completed menu conversation lacked end ID"
''')

    def test_overdue_periodic_id_waits_for_authenticated_menu_to_complete(self):
        self.run_tcl(r'''
rename playFile forbidden_file
proc playFile {path} {}
turn
quiet 30000
set last_id $now
finish_id
set now [expr {$last_id + 540000}]
RepeaterLogic::repeater_up PTY_DTMF_CMD
RepeaterLogic::transmit 1
RepeaterLogic::box_control_result authenticated 1 0 0
RepeaterLogic::repeater_down IDLE
RepeaterLogic::transmit 0
quiet 2000
assert {$ids == 1} "Overdue ID interrupted authenticated menu"
RepeaterLogic::repeater_up PTY_DTMF_CMD
RepeaterLogic::transmit 1
RepeaterLogic::box_control_result recording_off 1 0 0
RepeaterLogic::every_second
assert {$ids == 1} "Overdue ID played over final spoken prompt"
RepeaterLogic::repeater_down IDLE
RepeaterLogic::transmit 0
RepeaterLogic::every_second
assert {$ids == 2} "Completed menu did not release overdue ID"
''')

    def test_initial_digit_burst_holds_overdue_id_before_async_reply(self):
        self.run_tcl(r'''
turn
quiet 30000
set last_id $now
finish_id
set now [expr {$last_id + 540000}]
turn
RepeaterLogic::dtmf_digit_received 2 100
RepeaterLogic::every_second
assert {$ids == 1} "ID overtook the pending DTMF response"
''')

    def test_timeout_turn_never_queues_cw_until_released_and_quiet(self):
        self.run_tcl(r'''
RepeaterLogic::squelch_open rx 1
RepeaterLogic::repeater_up SQL_OPEN
RepeaterLogic::transmit 1
quiet 20000
assert {$ids == 0} "CW queued behind a timed-out active turn"
RepeaterLogic::squelch_open rx 0
RepeaterLogic::repeater_down IDLE
RepeaterLogic::transmit 0
assert {$ids == 0} "CW queued inside native timeout teardown"
quiet 29999
assert {$ids == 0} "Timeout recovery ignored conversation quiet period"
quiet 1
assert {$ids == 1} "Fresh idle ID did not follow timeout turn"
finish_id
quiet 600000
assert {$ids == 1} "Timeout recovery ID looped"
''')

    def test_new_receive_activity_during_id_is_identified_as_new_traffic(self):
        self.run_tcl(r'''
turn
quiet 30000
RepeaterLogic::transmit 1
RepeaterLogic::squelch_open rx 1
RepeaterLogic::repeater_up SQL_RPT_REOPEN
incr now 1000
RepeaterLogic::squelch_open rx 0
RepeaterLogic::repeater_down IDLE
RepeaterLogic::transmit 0
quiet 30000
assert {$ids == 1} "Reopened traffic bypassed minimum ID interval"
quiet 600000
assert {$ids == 1} "Reopened short turn became an idle beacon"
''')

    def test_auto_toggle_drops_off_traffic_and_reenable_is_silent(self):
        self.run_tcl(r'''
set auto_value 0
RepeaterLogic::every_second
assert {!$RepeaterLogic::box_live_auto_enabled} "Automatic ID did not turn off"
turn
quiet 900000
assert {$ids == 0} "Automatic CW transmitted while disabled"
set auto_value 1
RepeaterLogic::every_second
quiet 30000
assert {$ids == 0} "Off traffic was replayed when re-enabled"
turn
quiet 30000
assert {$ids == 1} "New enabled traffic lacked identification"
''')

    def test_toggle_off_cancels_future_auto_id_without_aborting_current_audio(self):
        self.run_tcl(r'''
turn
set auto_value 0
quiet 30000
assert {$ids == 0} "Pending ID survived disable"
set auto_value 1
RepeaterLogic::every_second
turn
quiet 30000
assert {$ids == 1} "Re-enabled new traffic lacked ID"
set auto_value 0
RepeaterLogic::every_second
finish_id
quiet 600000
assert {$ids == 1} "Playing ID repeated after disable"
''')

    def test_missing_or_malformed_projection_preserves_last_valid_choice(self):
        self.run_tcl(r'''
set auto_value 0
RepeaterLogic::every_second
set auto_value ""
RepeaterLogic::every_second
turn
quiet 600000
assert {$ids == 0 && !$RepeaterLogic::box_live_auto_enabled} "Missing file reset disabled choice"
set auto_value 1
RepeaterLogic::every_second
set auto_value ""
RepeaterLogic::every_second
assert {$RepeaterLogic::box_live_auto_enabled} "Missing file reset enabled choice"
''')

    def test_manual_id_waits_for_safe_idle_and_works_with_auto_off(self):
        self.run_tcl(r'''
set auto_value 0
RepeaterLogic::every_second
RepeaterLogic::squelch_open rx 1
RepeaterLogic::manual_identification
RepeaterLogic::every_second
assert {$ids == 0} "Manual ID interrupted receiver"
RepeaterLogic::squelch_open rx 0
RepeaterLogic::every_second
assert {$ids == 1} "Explicit manual ID was blocked by automatic toggle"
finish_id
quiet 600000
assert {$ids == 1} "Manual identification caused an automatic loop"
''')

    def test_offline_clears_pending_and_reactivation_is_silent(self):
        self.run_tcl(r'''
turn
RepeaterLogic::manual_identification
RepeaterLogic::logic_online 0
quiet 600000
assert {$ids == 0} "Offline service identified"
RepeaterLogic::logic_online 1
RepeaterLogic::every_second
assert {$ids == 0} "Old traffic replayed on reactivation"
''')

    def test_fixed_projection_reader_accepts_only_small_exact_boolean_schema(self):
        cases = [('{"schema":1,"enabled":true}\n', "1"),
                 ('{"schema":1,"enabled":false}\n', "0"),
                 ('{"schema":1,"enabled":"false"}\n', ""),
                 ('{"schema":2,"enabled":false}\n', ""),
                 ('{"schema":1,"enabled":false,"extra":1}\n', ""),
                 ("x" * 129, "")]
        with tempfile.TemporaryDirectory() as folder:
            fixture = Path(folder) / "projection"
            for value, expected in cases:
                with self.subTest(value=value):
                    fixture.write_text(value)
                    output = self.run_tcl(r'''
rename file original_file
proc file {subcommand args} {
  if {$subcommand eq "lstat" && [lindex $args 0] eq "/run/repeater-box-control/station-identification.json"} {
    set args [lreplace $args 0 0 {FIXTURE}]
  }
  return [uplevel 1 [linsert $args 0 original_file $subcommand]]
}
rename open original_open
proc open {path mode} {
  if {$path ne "/run/repeater-box-control/station-identification.json" || $mode ne "r"} {
    error "Unexpected projection file"
  }
  return [original_open {FIXTURE} $mode]
}
puts [RepeaterLogic::box_live_native_auto_reader]
'''.replace("FIXTURE", str(fixture)))
                    self.assertEqual(output.strip(), expected)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO creation unavailable")
    def test_native_projection_rejects_fifo_symlink_and_directory_before_open(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            fifo = root / "fifo"
            os.mkfifo(fifo)
            target = root / "regular"
            target.write_text('{"schema":1,"enabled":true}\n')
            link = root / "link"
            link.symlink_to(target)
            oversized = root / "oversized"
            oversized.write_text("x" * 129)
            for fixture in (fifo, link, root, oversized):
                with self.subTest(type=fixture.name):
                    self.run_tcl(r'''
set auto_value 0
RepeaterLogic::every_second
rename file original_file
proc file {subcommand args} {
  if {$subcommand eq "lstat" && [lindex $args 0] eq "/run/repeater-box-control/station-identification.json"} {
    set args [lreplace $args 0 0 {FIXTURE}]
  }
  return [uplevel 1 [linsert $args 0 original_file $subcommand]]
}
rename open original_open
proc open {args} {error "Unsafe projection was opened"}
proc RepeaterLogic::box_live_read_auto {} {return [box_live_native_auto_reader]}
RepeaterLogic::every_second
assert {!$RepeaterLogic::box_live_auto_enabled} "Rejected projection changed cached choice"
'''.replace("FIXTURE", str(fixture)))

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

    def test_all_native_voice_hooks_are_silent(self):
        self.run_tcl(r'''
foreach reason {SQL_OPEN CTCSS_OPEN SQL_RPT_REOPEN SQL_CLOSE AUDIO MODULE PTY_DTMF_CMD} {
  turn 100 $reason
}
RepeaterLogic::logic_online 1
RepeaterLogic::send_rgr_sound
RepeaterLogic::repeater_idle
RepeaterLogic::identify_nag
assert {$ids == 0} "Native hook appended unwanted CW"
''')

    def test_invalid_callsign_or_interval_is_rejected(self):
        for value in ("N0CALL", "N0CALL/P", "REPLACE1", "CALL", "1234", "A1",
                      "K1ABC;exec", "k1abc", "A1/ABC/DEF", "A1" * 8,
                      "K00000/0000000"):
            with self.subTest(callsign=value):
                with self.assertRaises(AssertionError):
                    self.run_tcl("", callsign=value)
        self.run_tcl("", callsign="K1ABC/P")
        for value in ("0", "10", "09", "60"):
            with self.subTest(interval=value):
                with self.assertRaises(AssertionError):
                    self.run_tcl("", interval=value)

    def test_status_preference_does_not_control_station_identification(self):
        self.run_tcl(r'''
rename playFile forbidden_file
proc playFile {path} {lappend ::clips $path}
set clips {}
RepeaterLogic::repeater_up PTY_DTMF_CMD
RepeaterLogic::transmit 1
RepeaterLogic::box_control_result recording_off 1 0 0
RepeaterLogic::repeater_down IDLE
RepeaterLogic::transmit 0
quiet 30000
assert {[llength $clips] == 1 && $ids == 1} "Status preference disabled identification"
''')

    def test_handler_retains_restricted_dtmf_policy_without_external_commands(self):
        text = HANDLER.read_text()
        self.assertIn("repeater-box-events.tcl", text)
        self.assertNotRegex(text, r"(?m)^\s*after\s")
        self.assertNotIn("playMsg", text)
        self.assertNotIn("spellWord", text)
        self.assertNotRegex(text, r"(?m)^\s*exec\s")
        self.assertTrue(SHARED.is_file())


if __name__ == "__main__":
    unittest.main()
