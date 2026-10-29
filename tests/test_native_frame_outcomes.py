"""Native frame diagnostics use synthetic keypad input and a stubbed client."""
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
HANDLER = ROOT / "scripts/repeater-box-events.tcl"
TCL = shutil.which("tclsh")
PREFIX = "RF frame outcome: "
HARNESS = r'''
proc source {path} {}
namespace eval Logic {proc squelch_open {a b} {}; proc every_second {} {}}
set now 1000
set requests 0
set fail_client 0
proc exec {args} {
  if {$::fail_client} {error "private synthetic failure must never appear"}
  incr ::requests
  return 123
}
'''
SETUP = r'''
proc RepeaterLogic::box_now {} {return $::now}
proc burst {symbols} {
  RepeaterLogic::squelch_open rx 1
  foreach symbol [split $symbols ""] {RepeaterLogic::dtmf_digit_received $symbol 100}
  RepeaterLogic::squelch_open rx 0
  incr ::now 500
  RepeaterLogic::every_second
  incr ::now 1000
}
'''


@unittest.skipUnless(TCL, "Tcl unavailable")
class NativeFrameDiagnosticTests(unittest.TestCase):
    def run_case(self, body, expected_requests, expected_outcomes):
        script = HARNESS + HANDLER.read_text() + SETUP + body
        script += '\nif {$requests != ' + str(expected_requests) + '} {error "request behavior changed"}\nputs PASS\n'
        result = subprocess.run([TCL], input=script, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        lines = result.stdout.splitlines()
        self.assertEqual(lines[-1:], ["PASS"])
        self.assertEqual(lines[:-1], [PREFIX + item for item in expected_outcomes])

    def test_valid_login_option_and_cancel_preserve_dispatch(self):
        self.run_case("\nburst AB1C29#\nburst 2#\nburst *\n", 3,
                      ["frame_submitted"] * 3)

    def test_incomplete_or_multiple_submissions_are_fixed_categories(self):
        self.run_case("\nburst AB1C29\nburst 1##\nburst AB1C29#2#\n", 0,
                      ["frame_incomplete", "frame_incomplete", "frame_overflow"])

    def test_no_input_and_repeated_idle_ticks_are_silent(self):
        self.run_case(r'''
RepeaterLogic::squelch_open rx 1
RepeaterLogic::squelch_open rx 0
incr now 500
RepeaterLogic::every_second
incr now 1000
RepeaterLogic::every_second
''', 0, [])

    def test_existing_timeout_can_expire_complete_frame_while_waiting_for_release(self):
        self.run_case(r'''
RepeaterLogic::squelch_open rx 1
RepeaterLogic::dtmf_digit_received A 100
incr now 9000
foreach symbol [split B1C29# ""] {RepeaterLogic::dtmf_digit_received $symbol 100}
incr now 1000
RepeaterLogic::squelch_open rx 0
incr now 500
RepeaterLogic::every_second
''', 0, ["frame_expired"])

    def test_squelch_close_reopen_preserves_existing_interruption_guard(self):
        self.run_case(r'''
RepeaterLogic::squelch_open rx 1
RepeaterLogic::dtmf_digit_received A 100
RepeaterLogic::squelch_open rx 0
incr now 600
RepeaterLogic::squelch_open rx 1
foreach symbol [split B1C29# ""] {RepeaterLogic::dtmf_digit_received $symbol 100}
RepeaterLogic::squelch_open rx 0
incr now 500
RepeaterLogic::every_second
''', 0, ["frame_interrupted"])

    def test_helper_launch_failure_never_prints_exception(self):
        self.run_case("\nset fail_client 1\nburst AB1C29#\n", 0, ["client_start_failed"])

    def test_only_known_category_tokens_can_be_printed(self):
        source = HANDLER.read_text()
        script = HARNESS + source + SETUP + r'''
RepeaterLogic::box_frame_outcome {private payload must not appear}
RepeaterLogic::box_frame_outcome {frame_submitted;exec private}
RepeaterLogic::box_frame_outcome frame_submitted
puts PASS
'''
        result = subprocess.run([TCL], input=script, text=True, capture_output=True)
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.stdout.splitlines(), [PREFIX + "frame_submitted", "PASS"])

    def test_broken_logging_does_not_change_client_dispatch(self):
        source = HANDLER.read_text()
        script = HARNESS + source + SETUP + r'''
rename puts native_puts
proc puts {args} {error "private synthetic journal failure"}
burst AB1C29#
if {$requests != 1} {error "request dispatch changed"}
native_puts PASS
'''
        result = subprocess.run([TCL], input=script, text=True, capture_output=True)
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.stdout.splitlines(), ["PASS"])


if __name__ == "__main__":
    unittest.main()
