"""Exercise installer startup policy in a shell with no real systemctl calls."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / 'scripts/install-pi.sh').read_text()
STOP_BLOCK = SOURCE.split('live_mode_selected=0\n', 1)[1].split(
    '# Prevent package installation from starting', 1)[0]
STOP_BLOCK = 'live_mode_selected=0\n' + STOP_BLOCK
FINISH_BLOCK = SOURCE.split('if [[ $radio_verification_pending == 1 ]]; then\n', 1)[1].split(
    '\necho\n', 1)[0]
FINISH_BLOCK = 'if [[ $radio_verification_pending == 1 ]]; then\n' + FINISH_BLOCK

STUB = '''set -euo pipefail
live_active=$INITIAL_ACTIVE
live_enabled=$INITIAL_ENABLED
systemctl() {
  printf '%s\\n' "$*" >> "$TEST_COMMAND_LOG"
  case "$1" in
    is-active) [[ $live_active == 1 ]] ;;
    is-enabled) [[ $live_enabled == 1 ]] ;;
    stop) live_active=0 ;;
    disable) live_enabled=0; live_active=0 ;;
    *) return 99 ;;
  esac
}
'''


class InstallStartupTest(unittest.TestCase):
    def run_policy(self, commands, *, active, enabled, **variables):
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary) / 'systemctl.log'
            log.touch()
            environment = dict(os.environ, INITIAL_ACTIVE=str(active), INITIAL_ENABLED=str(enabled),
                               TEST_COMMAND_LOG=str(log), **variables)
            report = '\nprintf "POLICY:%s:%s:%s\\n" "$live_active" "$live_enabled" "${live_mode_selected:-0}"\n'
            result = subprocess.run(['/bin/bash', '-c', STUB + commands + report],
                                    env=environment, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            state = tuple(int(value) for value in result.stdout.split('POLICY:')[1].strip().split(':'))
            return state, log.read_text().splitlines()

    def test_stopping_live_preserves_each_existing_boot_startup_choice(self):
        for active in (0, 1):
            for enabled in (0, 1):
                with self.subTest(active=active, enabled=enabled):
                    state, calls = self.run_policy(STOP_BLOCK, active=active, enabled=enabled)
                    self.assertEqual(state, (0, enabled, int(bool(active or enabled))))
                    self.assertEqual('stop repeater-live.service' in calls, bool(active or enabled))
                    self.assertFalse(any(call.startswith(('start ', 'restart ', 'enable ', 'disable '))
                                         for call in calls))

    def test_successful_live_upgrade_does_not_start_or_change_enabled_state(self):
        for enabled in (0, 1):
            with self.subTest(enabled=enabled):
                state, calls = self.run_policy(FINISH_BLOCK, active=0, enabled=enabled,
                    radio_verification_pending='0', live_mode_selected='1',
                    rx_monitor_active='0', rx_monitor_enabled='0')
                self.assertEqual(state, (0, enabled, 1))
                self.assertEqual(calls, [])

    def test_unfinished_upload_still_stops_and_disables_every_radio_mode(self):
        state, calls = self.run_policy(FINISH_BLOCK, active=1, enabled=1,
            radio_verification_pending='1', live_mode_selected='1',
            rx_monitor_active='0', rx_monitor_enabled='0')
        self.assertEqual(state, (0, 0, 1))
        self.assertEqual(calls, ['disable --now repeater-engine.service repeater-rx-monitor.service repeater-live.service'])


if __name__ == '__main__':
    unittest.main()
