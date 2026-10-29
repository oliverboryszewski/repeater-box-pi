# Root-installed SvxLink event wrapper. No radio programming or shell commands.
set script_path /usr/share/svxlink/events.tcl
source $script_path

namespace eval RepeaterLogic {
  variable box_buffer ""
  variable box_overflow 0
  variable box_frame_reason ""
  variable box_cancel 0
  variable box_first_at -1
  variable box_closed_at -1
  variable box_rx_open 0

  proc box_now {} { return [clock milliseconds] }

  proc box_frame_outcome {outcome} {
    if {$outcome ni {frame_submitted frame_incomplete frame_expired frame_overflow frame_interrupted client_start_failed}} { return }
    # Fixed categories only: never print received symbols, counts, timing,
    # credential material, helper output or exception details.
    catch {
      puts "RF frame outcome: $outcome"
      flush stdout
    }
  }

  proc box_clear {} {
    variable box_buffer; variable box_overflow; variable box_cancel; variable box_frame_reason
    variable box_first_at; variable box_closed_at
    set box_buffer ""; set box_overflow 0; set box_cancel 0; set box_frame_reason ""
    set box_first_at -1; set box_closed_at -1
  }

  proc dtmf_digit_received {digit duration} {
    variable box_buffer; variable box_overflow; variable box_cancel
    variable box_first_at; variable box_frame_reason
    set now [box_now]
    if {$box_first_at < 0} { set box_first_at $now }
    if {$now - $box_first_at > 10000} { set box_overflow 1; set box_buffer ""; set box_frame_reason frame_expired }
    if {$digit eq "*"} { set box_cancel 1 }
    if {![regexp {^[0-9ABCD*#]$} $digit]} { set box_overflow 1; if {$box_frame_reason eq ""} { set box_frame_reason frame_overflow } }
    if {[string length $box_buffer] < 7 && !$box_overflow} {
      append box_buffer $digit
    } else { set box_overflow 1; if {$box_frame_reason eq ""} { set box_frame_reason frame_overflow } }
    # Consume every symbol before native #/macro/anti-flutter processing.
    # A single transmission can therefore submit only one menu stage.
    return 1
  }

  proc dtmf_cmd_received {cmd} { return 1 }

  proc squelch_open {rx_id is_open} {
    variable box_rx_open; variable box_closed_at; variable box_buffer; variable box_overflow; variable box_first_at; variable box_frame_reason
    Logic::squelch_open $rx_id $is_open
    set now [box_now]
    if {$is_open} {
      if {$box_closed_at >= 0 && $now - $box_closed_at >= 500 && $box_buffer ne ""
          && $box_first_at <= $box_closed_at} {
        # Rekeying before the previous response is complete cannot merge two
        # submitted stages into one credential or accidentally apply an option.
        box_clear
        set box_overflow 1
        set box_frame_reason frame_interrupted
      }
      set box_closed_at -1
      set box_rx_open 1
    } else {
      set box_rx_open 0
      set box_closed_at $now
    }
  }

  proc every_second {} {
    Logic::every_second
    box_tick
  }

  proc box_tick {} {
    variable box_buffer; variable box_overflow; variable box_cancel
    variable box_closed_at; variable box_rx_open; variable box_first_at; variable box_frame_reason
    set now [box_now]
    if {$box_first_at >= 0 && $now - $box_first_at > 10000} {
      set box_overflow 1; set box_buffer ""; set box_frame_reason frame_expired
    }
    if {$box_rx_open || $box_closed_at < 0 || $now - $box_closed_at < 500} { return }
    set cmd ""
    if {$box_cancel} {
      set cmd "*"
    } elseif {!$box_overflow && [regexp {^([0-9ABCD]{6}|[0-9ABCD])#$} $box_buffer]} {
      set cmd [string range $box_buffer 0 end-1]
    }
    set had_frame [expr {$box_first_at >= 0 || $box_overflow || $box_cancel || $box_buffer ne ""}]
    set outcome $box_frame_reason
    box_clear
    if {$cmd eq ""} {
      if {$had_frame} { box_frame_outcome [expr {$outcome eq "" ? "frame_incomplete" : $outcome}] }
      return
    }
    # The command travels only on stdin. The short-lived client returns its
    # fixed response through COMMAND_PTY; it never opens an audio device.
    if {[catch {exec /usr/bin/python3 /opt/repeater-box/scripts/dtmf-client.py << "$cmd\n" &}]} {
      box_frame_outcome client_start_failed
    } else {
      box_frame_outcome frame_submitted
    }
  }

  proc box_clip {name} {
    if {![regexp {^(status|authenticated|authenticated_data|choose_status|choose_recording|choose_identification|choose_aprs|choose_sstv|status_on|status_off|recording_on|recording_off|identification_on|identification_off|aprs_on|aprs_off|sstv_on|sstv_off|cancelled|invalid|expired|busy|unavailable|pair_unknown|pair_[1-9])$} $name]} { return }
    playFile "/opt/repeater-box/prompts/$name.wav"
  }

  proc box_control_result {result pair_index recording announce_status identification_enabled {prompt_id -}} {
    if {$result ni {status authenticated choose_status choose_recording choose_identification status_on status_off recording_on recording_off identification_on identification_off cancelled invalid expired busy unavailable}
        || ![string is integer -strict $pair_index] || $pair_index < 0 || $pair_index > 9
        || $recording ni {0 1} || $announce_status ni {0 1} || $identification_enabled ni {0 1}} { return }
    box_render_control_result $result $pair_index $recording $announce_status $identification_enabled 0 0 0
  }

  proc box_control_result_data {result pair_index recording announce_status identification_enabled aprs_enabled sstv_enabled {prompt_id -}} {
    if {$result ni {status authenticated choose_status choose_recording choose_identification choose_aprs choose_sstv status_on status_off recording_on recording_off identification_on identification_off aprs_on aprs_off sstv_on sstv_off cancelled invalid expired busy unavailable}
        || ![string is integer -strict $pair_index] || $pair_index < 0 || $pair_index > 9
        || $recording ni {0 1} || $announce_status ni {0 1} || $identification_enabled ni {0 1}
        || $aprs_enabled ni {0 1} || $sstv_enabled ni {0 1}
        || ![regexp {^(-|[0-9a-f]{32})$} $prompt_id]} { return }
    box_render_control_result $result $pair_index $recording $announce_status $identification_enabled 1 $aprs_enabled $sstv_enabled
  }

  proc box_render_control_result {result pair_index recording announce_status identification_enabled extended aprs_enabled sstv_enabled} {
    set report_settings [expr {$result eq "status" || ($announce_status == 1
      && $result in {status_on status_off recording_on recording_off identification_on identification_off aprs_on aprs_off sstv_on sstv_off})}]
    if {$result eq "authenticated" && $extended} {
      box_clip authenticated_data
    } elseif {!$report_settings} { box_clip $result }
    if {$report_settings} {
      # The complete report includes the changed setting once. Avoid a second
      # acknowledgment so legacy and extended reports keep their time bounds.
      box_clip status
      if {$pair_index == 0} { box_clip pair_unknown } else { box_clip "pair_$pair_index" }
      if {$recording == 1} { box_clip recording_on } else { box_clip recording_off }
      if {$identification_enabled == 1} { box_clip identification_on } else { box_clip identification_off }
      if {$announce_status == 1} { box_clip status_on } else { box_clip status_off }
      if {$extended} {
        if {$aprs_enabled == 1} { box_clip aprs_on } else { box_clip aprs_off }
        if {$sstv_enabled == 1} { box_clip sstv_on } else { box_clip sstv_off }
      }
    }
  }
}
