# Live-radio event policy. The shared handler retains the restricted DTMF menu.
# Native CW synthesis needs no downloaded voice clips or station data in source.
source [file join [file dirname [info script]] repeater-box-events.tcl]

if {![info exists mycall] || [string length $mycall] < 3 || [string length $mycall] > 15
    || ![regexp {^[A-Z0-9]+(/[A-Z0-9]+)?$} $mycall]
    || ![regexp {[A-Z]} $mycall] || ![regexp {[0-9]} $mycall]
    || [lindex [split $mycall /] 0] eq "N0CALL"
    || [string match "REPLACE*" $mycall]} {
  error "A valid local station callsign is required for live identification"
}

namespace eval RepeaterLogic {
  proc box_live_cw_units {call} {
    set units [expr {3 * ([string length $call] - 1)}]
    foreach letter [split $call ""] {
      set symbols $::CW::letters($letter)
      incr units [expr {[string length $symbols] - 1}]
      foreach symbol [split $symbols ""] {
        incr units [expr {$symbol eq "." ? 1 : 3}]
      }
    }
    return $units
  }
}
# At 20 WPM each unit is 60 ms. Leave headroom inside the commissioned
# 15-second native TX timeout for TX delay, silence and event scheduling.
if {[RepeaterLogic::box_live_cw_units $mycall] > 166} {
  error "The local callsign exceeds the initial CW identification time limit"
}

namespace eval RepeaterLogic {
  variable box_live_online [expr {[info exists ::Logic::CFG_ONLINE]
                                && $::Logic::CFG_ONLINE eq "1"}]
  variable box_live_interval_minutes 9
  if {[info exists ::Logic::CFG_BOX_ID_INTERVAL_MINUTES]} {
    set box_live_interval_minutes $::Logic::CFG_BOX_ID_INTERVAL_MINUTES
  }
  if {$box_live_interval_minutes ni {9 30}} {
    error "The local automatic identification interval must be 9 or 30 minutes"
  }
  variable box_live_interval_ms [expr {$box_live_interval_minutes * 60000}]
  variable box_live_auto_enabled 1
  variable box_live_up 0
  variable box_live_tx_on 0
  variable box_live_pending 0
  variable box_live_identifying 0
  variable box_live_first_tx_at -1
  variable box_live_last_activity_at -1
  variable box_live_last_id_queued_at -1
  variable box_live_prompt_queued 0
  variable box_live_dtmf_until -1
  variable box_live_manual_pending 0
  variable box_live_control_results {}

  # Tcl 8.6 clock clicks -milliseconds aliases wall time. Linux uptime supplies
  # an elapsed clock unaffected by NTP changes; no external command is run.
  proc box_live_now {} {
    set handle [open /proc/uptime r]
    set read_error [catch {read $handle 64} text]
    set close_error [catch {close $handle}]
    if {$read_error || $close_error
        || ![regexp {^([0-9]+(\.[0-9]+)?)[[:space:]]} $text -> seconds]} {
      error "The live identification elapsed clock is unavailable"
    }
    return [expr {wide(double($seconds) * 1000)}]
  }

  proc box_live_read_auto {} {
    if {[catch {file lstat /run/repeater-box-control/station-identification.json info}]
        || $info(type) ne "file" || $info(size) > 128} {
      return ""
    }
    if {[catch {open /run/repeater-box-control/station-identification.json r} handle]} {
      return ""
    }
    set read_error [catch {read $handle 129} text]
    set close_error [catch {close $handle}]
    if {$read_error || $close_error || [string length $text] > 128
        || ![regexp {^\{"schema":1,"enabled":(true|false)\}\n?$} $text -> enabled]} {
      return ""
    }
    return [expr {$enabled eq "true"}]
  }

  proc box_live_clear_conversation {} {
    variable box_live_pending; variable box_live_first_tx_at
    variable box_live_last_activity_at
    set box_live_pending 0
    set box_live_first_tx_at -1
    set box_live_last_activity_at -1
  }

  proc box_live_refresh_auto {} {
    variable box_live_auto_enabled
    set value [box_live_read_auto]
    # Dashboard restarts or malformed projections retain the last valid choice.
    if {$value eq "" || $value == $box_live_auto_enabled} { return }
    set box_live_auto_enabled $value
    # Traffic accumulated while disabled must not trigger an ID on re-enable.
    box_live_clear_conversation
  }

  proc box_live_activity {} {
    variable box_live_online; variable box_live_auto_enabled
    variable box_live_pending; variable box_live_first_tx_at
    variable box_live_last_activity_at
    if {!$box_live_online || !$box_live_auto_enabled} { return }
    set now [box_live_now]
    if {$box_live_first_tx_at < 0} { set box_live_first_tx_at $now }
    set box_live_last_activity_at $now
    set box_live_pending 1
  }

  proc box_live_safe_idle {} {
    variable box_live_online; variable box_live_up; variable box_live_tx_on
    variable box_live_identifying; variable box_live_prompt_queued
    variable box_live_dtmf_until; variable box_rx_open
    variable box_live_control_results
    return [expr {$box_live_online && !$box_live_up && !$box_live_tx_on
                  && !$box_live_identifying && !$box_live_prompt_queued
                  && [llength $box_live_control_results] == 0
                  && !$box_rx_open && [box_live_now] >= $box_live_dtmf_until}]
  }

  proc box_live_send_id {automatic} {
    global mycall
    variable box_live_auto_enabled; variable box_live_identifying
    variable box_live_last_id_queued_at; variable box_live_manual_pending
    if {($automatic && !$box_live_auto_enabled) || ![box_live_safe_idle]} { return }
    # Set the flag before queuing tones so the resulting PTT callbacks cannot
    # count this ID as fresh traffic. A real reopened receiver still can.
    set box_live_identifying 1
    set box_live_manual_pending 0
    set box_live_last_id_queued_at [box_live_now]
    box_live_clear_conversation
    # The timestamp is a queued deadline, not proof of received RF. A fresh
    # idle transmission avoids the native timeout latch from the previous turn.
    CW::play $mycall 100 800 -12
    playSilence 250
  }

  proc startup {} {
    variable box_live_interval_minutes; variable box_live_last_id_queued_at
    box_live_refresh_auto
    if {$box_live_interval_minutes == 30 && $box_live_last_id_queued_at < 0} {
      # Explicit testing mode suppresses automatic Morse for its first interval.
      set box_live_last_id_queued_at [box_live_now]
    }
    # Deliberately silent; do not register the stock wall-clock ID scheduler.
  }

  proc logic_online {online} {
    variable box_live_online; variable box_live_identifying
    variable box_live_prompt_queued; variable box_live_dtmf_until
    variable box_live_manual_pending
    variable box_live_control_results
    set box_live_online [expr {$online == 1}]
    if {!$box_live_online} {
      box_live_clear_conversation
      set box_live_identifying 0
      set box_live_prompt_queued 0
      set box_live_dtmf_until -1
      set box_live_manual_pending 0
      set box_live_control_results {}
    }
  }

  proc repeater_up {reason} {
    variable repeater_is_up; variable box_live_up; variable box_live_identifying
    set repeater_is_up 1
    set box_live_up 1
    if {$reason ne "AUDIO" || !$box_live_identifying} { box_live_activity }
  }

  proc repeater_down {reason} {
    variable repeater_is_up; variable box_live_up
    set repeater_is_up 0
    set box_live_up 0
    if {$reason eq "OFFLINE"} { box_live_clear_conversation }
    # A PTT release is a turn boundary, not the end of the conversation. Never
    # append CW to a spoken prompt or queue it during native PTT teardown.
  }

  proc transmit {is_on} {
    variable box_live_tx_on; variable box_live_identifying
    variable box_live_pending; variable box_live_prompt_queued
    set box_live_tx_on [expr {$is_on == 1}]
    if {$box_live_tx_on} {
      if {!$box_live_identifying} { box_live_activity }
    } else {
      set was_identifying $box_live_identifying
      set had_prompt $box_live_prompt_queued
      set box_live_identifying 0
      set box_live_prompt_queued 0
      if {!$was_identifying || $box_live_pending || $had_prompt} {
        box_live_activity
      }
      # No audio is queued here. Native PttCtrl still has teardown work after
      # this callback; the next second tick is the earliest safe scheduling point.
    }
  }

  rename squelch_open box_live_shared_squelch_open
  proc squelch_open {rx_id is_open} {
    variable box_live_pending; variable box_live_up; variable box_live_tx_on
    box_live_shared_squelch_open $rx_id $is_open
    if {$is_open || $box_live_pending || $box_live_up || $box_live_tx_on} {
      box_live_activity
    }
  }

  rename dtmf_digit_received box_live_shared_dtmf_digit_received
  proc dtmf_digit_received {digit duration} {
    variable box_live_dtmf_until; variable box_live_online
    if {$box_live_online} { set box_live_dtmf_until [expr {[box_live_now] + 30000}] }
    # Keep the original restricted capture; never retain or log the credential.
    return [box_live_shared_dtmf_digit_received $digit $duration]
  }

  rename box_clip box_live_shared_clip
  proc box_clip {name} {
    variable box_live_online; variable box_live_prompt_queued
    if {$box_live_online} {
      box_live_activity
      set box_live_prompt_queued 1
    }
    box_live_shared_clip $name
  }

  rename box_control_result box_live_shared_control_result
  proc box_control_result {result pair_index recording announce_status identification_enabled} {
    variable box_live_online; variable box_live_dtmf_until
    variable box_live_control_results
    if {!$box_live_online
        || $result ni {status authenticated choose_status choose_recording choose_identification status_on status_off recording_on recording_off identification_on identification_off cancelled invalid expired busy unavailable}
        || ![string is integer -strict $pair_index] || $pair_index < 0 || $pair_index > 9
        || $recording ni {0 1} || $announce_status ni {0 1} || $identification_enabled ni {0 1}
        || [llength $box_live_control_results] >= 4} { return }
    if {$result in {authenticated choose_status choose_recording choose_identification}} {
      set box_live_dtmf_until [expr {[box_live_now] + 30000}]
    } elseif {$result in {status status_on status_off recording_on recording_off identification_on identification_off cancelled invalid expired busy unavailable}} {
      set box_live_dtmf_until -1
    }
    lappend box_live_control_results [list $result $pair_index $recording $announce_status $identification_enabled]
  }

  proc box_live_flush_control_result {} {
    variable box_live_online; variable box_live_up; variable box_live_tx_on
    variable box_live_identifying; variable box_live_prompt_queued
    variable box_rx_open; variable box_live_control_results
    if {!$box_live_online || $box_live_up || $box_live_tx_on || $box_rx_open
        || $box_live_identifying || $box_live_prompt_queued
        || [llength $box_live_control_results] == 0} { return }
    set response [lindex $box_live_control_results 0]
    set box_live_control_results [lrange $box_live_control_results 1 end]
    # Incoming DTMF may have used most of the native 15-second TX timer. Start
    # every reply from a fresh idle transmitter; never append it to that turn.
    set box_live_prompt_queued 1
    box_live_shared_control_result {*}$response
  }

  proc box_live_id_tick {} {
    variable box_live_online; variable box_live_auto_enabled
    variable box_live_pending; variable box_live_first_tx_at
    variable box_live_last_activity_at; variable box_live_last_id_queued_at
    variable box_live_interval_ms; variable box_live_manual_pending
    if {!$box_live_online || ![box_live_safe_idle]} { return }
    if {$box_live_manual_pending} {
      box_live_send_id 0
      return
    }
    if {!$box_live_auto_enabled || !$box_live_pending} { return }
    set now [box_live_now]
    set minimum_met [expr {$box_live_last_id_queued_at < 0
                          || $now - $box_live_last_id_queued_at >= $box_live_interval_ms}]
    set base $box_live_last_id_queued_at
    if {$base < 0} { set base $box_live_first_tx_at }
    set interval_due [expr {$base >= 0 && $now - $base >= $box_live_interval_ms}]
    set quiet_end [expr {$box_live_last_activity_at >= 0
                        && $now - $box_live_last_activity_at >= 30000}]
    if {$minimum_met && ($quiet_end || $interval_due)} {
      box_live_send_id 1
    } elseif {$quiet_end} {
      # The station was identified recently. Close the finished conversation
      # without retaining old traffic that could become a later idle beacon.
      box_live_clear_conversation
    }
  }

  proc every_second {} {
    box_live_refresh_auto
    Logic::every_second
    box_tick
    box_live_flush_control_result
    box_live_id_tick
  }

  proc manual_identification {} {
    variable box_live_manual_pending
    set box_live_manual_pending 1
  }
  proc checkPeriodicIdentify {} {}
  proc send_rgr_sound {} {}
  proc repeater_idle {} {}
  proc identify_nag {} {}
}
