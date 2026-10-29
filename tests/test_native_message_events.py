"""Native message arbitration uses Tcl fixtures, never real files, PTT or RF."""
import os
import shutil
import unittest
from unittest.mock import patch

import test_live_identification as identification_tests


SETUP = r'''
set invocation aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
set job bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
set prompt cccccccccccccccccccccccccccccccc
set RepeaterLogic::box_invocation $invocation
set played {}
set published {}
set executions {}
set inspected {}
set clip_error 0
set play_error 0
set clip_spec [dict create type file uid 0 mode 0100640 size 3244]

# Only the fixed root snapshot pathname is synthesized. All other filesystem
# behavior remains real, and publishing is replaced before events can write.
rename file native_test_real_file
proc file {subcommand args} {
  if {$subcommand eq "lstat" && [llength $args] == 2
      && [regexp {^/run/repeater-message-playback/[0-9a-f]{32}\.wav$} [lindex $args 0]]} {
    lappend ::inspected [lindex $args 0]
    if {$::clip_error} { error "Missing fixture snapshot" }
    upvar 1 [lindex $args 1] info
    array set info $::clip_spec
    return
  }
  return [uplevel 1 [list native_test_real_file $subcommand {*}$args]]
}
rename RepeaterLogic::box_live_publish native_test_real_publish
proc RepeaterLogic::box_live_publish {} {
  lappend ::published [list $::RepeaterLogic::box_message_job \
    $::RepeaterLogic::box_message_state $::RepeaterLogic::box_message_sequence]
}
rename playFile native_test_forbidden_play
proc playFile {path} {
  if {$::play_error} { error "Fixture playback failure" }
  assert {[regexp {^(/run/repeater-message-playback/[0-9a-f]{32}|/opt/repeater-box/prompts/[a-z_0-9]+)\.wav$} $path]} \
    "Unexpected playback pathname"
  lappend ::played $path
}
rename exec native_test_forbidden_exec
proc exec {args} {lappend ::executions $args; return 0}
'''


@unittest.skipUnless(shutil.which('tclsh'), 'Tcl runtime unavailable')
class NativeMessageEventTests(unittest.TestCase):
    def run_tcl(self, body):
        # Compose the shared harness; do not inherit and rediscover its ID tests.
        harness = identification_tests.LiveIdentificationTests(methodName='runTest')
        # Even under a service runner, startup must not publish to a real /run
        # directory before our event-only fixture replaces the publisher.
        with patch.dict(os.environ, {'INVOCATION_ID': ''}):
            return harness.run_tcl(SETUP + body)

    def test_message_acceptance_playback_and_finish_have_distinct_acknowledgments(self):
        self.run_tcl(r'''
RepeaterLogic::box_message $invocation $job
assert {$RepeaterLogic::box_message_state eq "accepted"} "Message did not enter accepted phase"
assert {$RepeaterLogic::box_message_sequence == 1} "Accepted sequence missing"
assert {[llength $played] == 1 && [file tail [lindex $played 0]] eq "$job.wav"} "Wrong snapshot played"
RepeaterLogic::transmit 0
assert {$RepeaterLogic::box_message_state eq "accepted"} "Early TX OFF falsely finished unstarted audio"
RepeaterLogic::transmit 1
assert {$RepeaterLogic::box_message_state eq "playing" && $RepeaterLogic::box_message_sequence == 2} "TX ON did not acknowledge playback"
incr now 30000
RepeaterLogic::transmit 0
assert {$RepeaterLogic::box_message_state eq "finished" && $RepeaterLogic::box_message_sequence == 3} "TX OFF did not finish playback"
assert {[lindex [lindex $published 0] 1] eq "accepted"} "Accepted state was not published first"
assert {[lindex [lindex $published end] 1] eq "finished"} "Finished state not published"
assert {[llength $executions] == 0} "Uploaded audio acknowledged an unrelated DTMF prompt"
assert {$ids == 0 && [llength $played] == 1} "Other audio overlapped the one-shot"
''')

    def test_rx_tx_repeater_id_prompt_and_menu_busy_conditions_reject_without_queueing(self):
        states = (
            'set RepeaterLogic::box_rx_open 1',
            'set RepeaterLogic::box_live_tx_on 1',
            'set RepeaterLogic::box_live_up 1',
            'set RepeaterLogic::box_live_identifying 1',
            'set RepeaterLogic::box_live_prompt_queued 1',
            'set RepeaterLogic::box_live_dtmf_until [expr {$now + 30000}]',
            'set RepeaterLogic::box_live_control_results {{status 1 0 0 1 -}}',
            'set RepeaterLogic::box_live_online 0',
        )
        for busy in states:
            with self.subTest(busy=busy):
                self.run_tcl(busy + r'''
RepeaterLogic::box_message $invocation $job
assert {$RepeaterLogic::box_message_state eq "rejected_busy"} "Busy message was not rejected"
assert {[llength $played] == 0 && [llength $inspected] == 0} "Busy request reached file playback"
set RepeaterLogic::box_rx_open 0
set RepeaterLogic::box_live_tx_on 0
set RepeaterLogic::box_live_up 0
set RepeaterLogic::box_live_identifying 0
set RepeaterLogic::box_live_prompt_queued 0
set RepeaterLogic::box_live_dtmf_until -1
set RepeaterLogic::box_live_control_results {}
set RepeaterLogic::box_live_online 1
RepeaterLogic::box_message $invocation $job
assert {[llength $played] == 0} "Rejected request replayed when the channel cleared"
''')

    def test_another_request_cannot_replace_an_accepted_or_playing_job(self):
        for phase in ('accepted', 'playing'):
            with self.subTest(phase=phase):
                self.run_tcl(r'''
set previous dddddddddddddddddddddddddddddddd
set RepeaterLogic::box_message_job $previous
set RepeaterLogic::box_message_state PHASE
set RepeaterLogic::box_message_sequence 7
RepeaterLogic::box_message $invocation $job
assert {$RepeaterLogic::box_message_job eq $previous && $RepeaterLogic::box_message_state eq "PHASE"} "Active message was overwritten"
assert {$RepeaterLogic::box_message_sequence == 7 && [llength $played] == 0} "Concurrent request affected active playback"
'''.replace('PHASE', phase))

    def test_invocation_and_job_tokens_reject_stale_or_malicious_events_before_file_access(self):
        self.run_tcl(r'''
set injected 0
foreach bad {{} ../snapshot {[exec forbidden]} {x;set injected 1} AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb} {
  RepeaterLogic::box_message $bad $job
  RepeaterLogic::box_message $invocation $bad
}
RepeaterLogic::box_message dddddddddddddddddddddddddddddddd $job
assert {[llength $played] == 0 && [llength $inspected] == 0} "Invalid event reached a snapshot"
assert {$RepeaterLogic::box_message_state eq "idle" && $injected == 0} "Invalid event changed live state"
assert {[llength $executions] == 0} "Invalid token executed a command"
''')

    def test_snapshot_requires_root_regular_nonwritable_bounded_audio(self):
        cases = (
            ('dict set clip_spec uid 501', 'wrong owner'),
            ('dict set clip_spec type link', 'symlink'),
            ('dict set clip_spec type directory', 'directory'),
            ('dict set clip_spec type fifo', 'fifo'),
            ('dict set clip_spec mode 0100660', 'group write'),
            ('dict set clip_spec mode 0100642', 'other write'),
            ('dict set clip_spec size 45', 'empty audio'),
            ('dict set clip_spec size 1000001', 'oversized audio'),
            ('set clip_error 1', 'missing snapshot'),
        )
        for setting, label in cases:
            with self.subTest(kind=label):
                self.run_tcl(setting + r'''
RepeaterLogic::box_message $invocation $job
assert {$RepeaterLogic::box_message_state eq "failed"} "Unsafe snapshot not rejected"
assert {[llength $played] == 0 && [llength $inspected] == 1} "Unsafe snapshot reached playback"
assert {$RepeaterLogic::box_message_sequence == 1} "Snapshot failure lacked acknowledgment"
''')

    def test_playback_error_and_offline_transition_never_report_success(self):
        self.run_tcl(r'''
set play_error 1
RepeaterLogic::box_message $invocation $job
assert {$RepeaterLogic::box_message_state eq "failed"} "playFile failure remained accepted"
set play_error 0
set next_job dddddddddddddddddddddddddddddddd
RepeaterLogic::box_message $invocation $next_job
RepeaterLogic::transmit 1
RepeaterLogic::logic_online 0
assert {$RepeaterLogic::box_message_state eq "failed"} "Offline playback remained active"
RepeaterLogic::transmit 0
assert {$RepeaterLogic::box_message_state eq "failed"} "Later TX OFF disguised failed playback"
RepeaterLogic::logic_online 1
RepeaterLogic::box_message $invocation $next_job
assert {[llength $played] == 1} "Offline job replayed on reactivation"
''')

    def test_completed_request_replay_does_not_play_twice(self):
        self.run_tcl(r'''
RepeaterLogic::box_message $invocation $job
RepeaterLogic::box_message $invocation $job
assert {[llength $played] == 1} "Accepted request replayed"
RepeaterLogic::transmit 1
RepeaterLogic::transmit 0
set sequence $RepeaterLogic::box_message_sequence
RepeaterLogic::box_message $invocation $job
RepeaterLogic::every_second
assert {[llength $played] == 1 && $RepeaterLogic::box_message_sequence == $sequence} "Finished request replayed"
''')

    def test_message_blocks_queued_dtmf_and_cw_until_its_transmit_turn_finishes(self):
        self.run_tcl(r'''
RepeaterLogic::box_message $invocation $job
RepeaterLogic::box_control_result recording_on 1 1 0 1 $prompt
RepeaterLogic::manual_identification
RepeaterLogic::every_second
assert {[llength $played] == 1 && $ids == 0} "Another announcement overlapped accepted message"
RepeaterLogic::transmit 1
quiet 30000
assert {[llength $played] == 1 && $ids == 0} "Another announcement overlapped playing message"
RepeaterLogic::transmit 0
RepeaterLogic::every_second
assert {[llength $played] == 2 && [file tail [lindex $played end]] eq "recording_on.wav"} "Queued prompt did not wait for message completion"
assert {$ids == 0} "CW overlapped the deferred DTMF prompt"
RepeaterLogic::transmit 1
incr now 1000
RepeaterLogic::transmit 0
quiet 29999
assert {$ids == 0} "CW interrupted the new entry interval"
quiet 1
assert {$ids == 1} "Deferred manual identification did not resume after the full idle interval"
''')

    def test_prompt_acknowledgment_requires_actual_tx_on_then_off(self):
        self.run_tcl(r'''
RepeaterLogic::box_control_result authenticated 1 0 0 1 $prompt
assert {[llength $executions] == 0} "Prompt acknowledged when only queued"
RepeaterLogic::every_second
assert {$RepeaterLogic::box_live_prompt_queued && [llength $played] == 1} "Prompt did not queue audio"
RepeaterLogic::transmit 0
assert {[llength $executions] == 0} "Early TX OFF falsely acknowledged an unstarted prompt"
assert {$RepeaterLogic::box_live_prompt_queued} "Unstarted prompt was lost on duplicate TX OFF"
RepeaterLogic::transmit 1
quiet 10220
assert {[llength $executions] == 0} "Prompt acknowledged before native TX OFF"
RepeaterLogic::transmit 0
assert {[llength $executions] == 1} "Finished prompt lacked acknowledgment"
set command [lindex $executions 0]
assert {[lindex $command 0] eq "/usr/bin/python3"
        && [lindex $command 1] eq "/opt/repeater-box/scripts/dtmf-client.py"
        && [lindex $command 2] eq "--prompt-finished"
        && [lindex $command 3] eq $prompt && [lindex $command 4] eq "&"} "Unexpected prompt callback"
assert {$RepeaterLogic::box_live_dtmf_until == $now + 30000} "Entry window began before the prompt ended"
RepeaterLogic::transmit 0
assert {[llength $executions] == 1} "Duplicate TX OFF repeated prompt acknowledgment"
quiet 29999
assert {![RepeaterLogic::box_live_safe_idle]} "Entry window ended before thirty seconds"
incr now 1
assert {[RepeaterLogic::box_live_safe_idle]} "Entry window exceeded the exact completed-prompt interval"
''')

    def test_invalid_prompt_id_never_executes_a_callback(self):
        self.run_tcl(r'''
foreach bad {../path {[exec forbidden]} {x;set injected 1} AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA} {
  RepeaterLogic::box_control_result authenticated 1 0 0 1 $bad
}
assert {[llength $RepeaterLogic::box_live_control_results] == 0} "Invalid prompt token entered FIFO"
assert {[llength $executions] == 0} "Invalid prompt token executed a callback"
''')


if __name__ == '__main__':
    unittest.main()
