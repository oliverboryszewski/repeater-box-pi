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
  variable box_live_up 0
  variable box_live_tx_on 0
  variable box_live_pending 0
  variable box_live_identifying 0
  variable box_live_first_tx_at -1
  variable box_live_last_id_queued_at -1
  variable box_live_tx_started_at -1
  variable box_live_deferred_id 0

  # Linux uptime is independent of wall-clock/NTP adjustments. Tcl 8.6 clock
  # clicks -milliseconds aliases wall time. Read only this fixed kernel path.
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

  proc box_live_send_id {} {
    global mycall
    variable box_live_online; variable box_live_identifying
    variable box_live_last_id_queued_at
    variable box_live_deferred_id
    if {!$box_live_online} { return }
    # Set this before queuing tones: a resulting TX callback must not count the
    # ID itself as new traffic. A real reopened receiver still marks traffic.
    set box_live_identifying 1
    set box_live_deferred_id 0
    # This records a queued deadline, not proof that RF or an ID was received.
    set box_live_last_id_queued_at [box_live_now]
    CW::play $mycall 100 800 -12
    playSilence 250
  }

  proc startup {} {
    # Deliberately silent. Do not register the stock wall-clock ID scheduler.
  }

  proc logic_online {online} {
    variable box_live_online; variable box_live_pending
    variable box_live_identifying; variable box_live_first_tx_at
    variable box_live_deferred_id
    set box_live_online [expr {$online == 1}]
    if {!$box_live_online} {
      set box_live_pending 0
      set box_live_identifying 0
      set box_live_first_tx_at -1
      set box_live_deferred_id 0
    }
    # Bringing a service online must not itself transmit an announcement.
  }

  proc repeater_up {reason} {
    variable repeater_is_up; variable box_live_up
    variable box_live_online; variable box_live_identifying
    variable box_live_pending
    set repeater_is_up 1
    set box_live_up 1
    if {$box_live_online && ($reason ne "AUDIO" || !$box_live_identifying)} {
      set box_live_pending 1
    }
  }

  proc repeater_down {reason} {
    variable repeater_is_up; variable box_live_up
    variable box_live_online; variable box_live_pending
    set repeater_is_up 0
    set box_live_up 0
    if {$reason eq "OFFLINE" || !$box_live_online} {
      set box_live_pending 0
      return
    }
    if {$box_live_pending} {
      set box_live_pending 0
      # Native RepeaterLogic keeps TX active until this queued ID completes.
      box_live_send_id
    }
  }

  proc transmit {is_on} {
    variable box_live_tx_on; variable box_live_online
    variable box_live_identifying; variable box_live_pending
    variable box_live_first_tx_at; variable box_live_up
    variable box_live_tx_started_at
    variable box_live_deferred_id
    set box_live_tx_on [expr {$is_on == 1}]
    if {$box_live_tx_on} {
      set box_live_tx_started_at [box_live_now]
      if {$box_live_online && !$box_live_identifying} {
        set box_live_pending 1
        if {$box_live_first_tx_at < 0} {
          set box_live_first_tx_at [box_live_now]
        }
      }
    } else {
      set tx_elapsed 0
      if {$box_live_tx_started_at >= 0} {
        set tx_elapsed [expr {[box_live_now] - $box_live_tx_started_at}]
      }
      set box_live_tx_started_at -1
      set was_identifying $box_live_identifying
      set box_live_identifying 0
      # Covers audio initiated outside a native repeater-up session. Ordinary
      # RX and local DTMF prompts finish through repeater_down instead.
      # The fixed live profile has TIMEOUT=15. Native timeout unkeys without a
      # Tcl transmit callback; it clears the timeout latch before this later
      # logical OFF callback. An ID queued after cutoff may have had no RF.
      # Requeue once after a long session, now that the latch is clear. The
      # bounded (<10 s) ID-only transmission cannot trigger this retry again.
      set retry_after_timeout [expr {$tx_elapsed >= 14000}]
      if {$box_live_online && !$box_live_up
          && (($box_live_pending && !$was_identifying) || $retry_after_timeout)} {
        set box_live_pending 0
        # PttCtrl finishes changing valves after this callback returns. Queuing
        # audio here could recursively re-key inside that unfinished teardown.
        set box_live_deferred_id 1
      }
    }
  }

  proc box_live_id_tick {} {
    variable box_live_online; variable box_live_tx_on
    variable box_live_pending; variable box_live_first_tx_at
    variable box_live_last_id_queued_at
    variable box_live_up; variable box_live_deferred_id
    if {$box_live_online && $box_live_deferred_id
        && !$box_live_tx_on && !$box_live_up} {
      box_live_send_id
      return
    }
    if {!$box_live_online || !$box_live_tx_on || !$box_live_pending} { return }
    set base $box_live_last_id_queued_at
    if {$base < 0} { set base $box_live_first_tx_at }
    if {$base >= 0 && [box_live_now] - $base >= 540000} {
      # Preserve pending traffic so the session also receives an end ID.
      # Do not depend on a new PTT edge to re-arm sustained activity.
      box_live_send_id
    }
  }

  proc every_second {} {
    Logic::every_second
    box_tick
    box_live_id_tick
  }

  proc manual_identification {} { box_live_send_id }
  proc checkPeriodicIdentify {} {}
  proc send_rgr_sound {} {}
  proc repeater_idle {} {}
  proc identify_nag {} {}
}
