"""Review fixed audio-message installation boundaries without running services."""
import ast
import importlib.util
import json
from pathlib import Path
import re
import shlex
import unittest

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = (ROOT / 'scripts/install-pi.sh').read_text()


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


checker = module('message_install_checker', ROOT / 'scripts/check-install-files.py')
updater = module('message_install_updater', ROOT / 'scripts/apply-software-update.py')


def copied_files():
    result = set()
    for line in INSTALLER.splitlines():
        if not line.startswith('install ') or '"$bundle_dir/' not in line:
            continue
        arguments = shlex.split(line)
        destination = arguments[-1]
        for argument in arguments[1:-1]:
            if not argument.startswith('$bundle_dir/'):
                continue
            source = argument[len('$bundle_dir/'):]
            brace = re.search(r'\{([^{}]+)\}', source)
            names = [source] if brace is None else [source[:brace.start()] + part + source[brace.end():]
                                                    for part in brace[1].split(',')]
            for name in names:
                target = destination + name.rsplit('/', 1)[1] if destination.endswith('/') else destination
                result.add((name, target))
    return result


class InstallAudioMessagesTest(unittest.TestCase):
    def test_every_fixed_copy_is_checked_including_message_helpers_and_units(self):
        private = {('config/updates.json', '/etc/repeater-box/updates.json'),
                   ('config/svxlink-bench.conf', '/etc/repeater-box/svxlink-bench.conf')}
        self.assertEqual(copied_files() - private, set(checker.COPY_FILES))
        required = {'dashboard/radio_messages.py', 'dashboard/native_state.py',
                    'scripts/convert-message-audio.py', 'scripts/migrate-live-profile.py',
                    'scripts/broadcast-message.py', 'systemd/repeater-message-broadcast.service',
                    'systemd/repeater-message-broadcast.path'}
        self.assertTrue(required <= set(checker.REQUIRED_SOURCES))
        self.assertTrue({source for source, _target in private} <= set(checker.REQUIRED_SOURCES))
        checker.check_sources(ROOT)

    def test_private_migration_runs_under_existing_lock_after_live_is_stopped(self):
        lock = INSTALLER.index('flock -n 9')
        stop = INSTALLER.index('systemctl stop repeater-live.service')
        migrate = INSTALLER.index("spec_from_file_location('live_profile_migration'")
        verify = INSTALLER.index('--verify-installed')
        flush = INSTALLER.index('sync -f /opt/repeater-box /etc/systemd/system')
        self.assertLess(lock, stop)
        self.assertLess(stop, migrate)
        self.assertLess(migrate, verify)
        self.assertLess(verify, flush)
        self.assertLess(flush, INSTALLER.index('systemctl daemon-reload'))
        body = INSTALLER.split("<<'LIVE_PROFILE_PY'\n", 1)[1].split('\nLIVE_PROFILE_PY', 1)[0]
        self.assertIn('module.migrate()', body)
        self.assertNotIn('flock', body)
        self.assertNotIn('systemctl', body)
        self.assertNotRegex(INSTALLER, r'systemctl (?:start|restart|enable --now) repeater-live\.service')

    def test_decoder_and_private_library_permissions_are_installed_explicitly(self):
        packages = next(line for line in INSTALLER.splitlines() if 'apt-get install ' in line)
        self.assertIn('ffmpeg', shlex.split(packages))
        self.assertIn('install -d -o repeaterdash -g repeater-control -m 2750 /var/lib/repeater-messages', INSTALLER)
        self.assertIn('usermod -a -G repeater-recordings,audio,repeater-control repeaterdash', INSTALLER)
        self.assertIn('usermod -a -G repeater-recordings,audio,dialout,repeater-control svxlink', INSTALLER)
        unit = (ROOT / 'systemd/repeater-dashboard.service').read_text()
        self.assertIn('User=repeaterdash\n', unit)
        self.assertIn('Group=repeaterdash\n', unit)
        self.assertIn('ReadWritePaths=/var/lib/svxlink/recordings /var/lib/repeater-messages\n', unit)
        self.assertIn('ProtectSystem=strict\n', unit)
        self.assertIn('NoNewPrivileges=true\n', unit)
        self.assertIn('UMask=0027\n', unit)

    def test_message_runtime_directory_remains_distinct_from_command_pty_directory(self):
        unit = (ROOT / 'systemd/repeater-live.service').read_text()
        self.assertIn('RuntimeDirectory=repeater-box repeater-message-status\n', unit)
        self.assertIn('RuntimeDirectoryMode=0750\n', unit)
        self.assertIn('Restart=no\n', unit)
        event = (ROOT / 'scripts/repeater-live-events.tcl').read_text()
        self.assertIn('set folder /run/repeater-message-status', event)
        self.assertIn('file attributes $folder -permissions 0755', event)
        self.assertIn('file attributes $temporary -permissions 0644', event)

    def test_dispatcher_is_fixed_oneshot_without_an_rf_start_dependency(self):
        unit = (ROOT / 'systemd/repeater-message-broadcast.service').read_text()
        self.assertIn('Type=oneshot\n', unit)
        self.assertIn('ExecStart=/usr/bin/python3 /opt/repeater-box/scripts/broadcast-message.py\n', unit)
        self.assertIn('Group=repeater-control\n', unit)
        self.assertIn('RuntimeDirectory=repeater-message-playback\n', unit)
        self.assertIn('RuntimeDirectoryMode=0750\n', unit)
        restarts = [line for line in unit.splitlines() if line.startswith('Restart=')]
        self.assertIn(restarts, ([], ['Restart=no']))  # The systemd default is no restart.
        timeout = next(line.partition('=')[2] for line in unit.splitlines() if line.startswith('TimeoutStartSec='))
        self.assertTrue(timeout.isdigit() and 40 <= int(timeout) <= 90)
        self.assertIn('StateDirectory=repeater-message-dispatcher\n', unit)
        self.assertIn('StateDirectoryMode=0700\n', unit)
        self.assertIn('NoNewPrivileges=true\n', unit)
        self.assertIn('ProtectSystem=strict\n', unit)
        for line in unit.splitlines():
            if line.startswith(('Wants=', 'Requires=', 'BindsTo=')):
                self.assertNotIn('repeater-live.service', line)
        self.assertNotRegex(unit, r'Exec\w*=.*(?:start|restart|enable).*repeater-live')

    def test_request_watcher_and_boot_dispatcher_are_enabled_after_integrity_checks(self):
        unit = (ROOT / 'systemd/repeater-message-broadcast.path').read_text()
        self.assertIn('PathChanged=/var/lib/repeater-dashboard/message-broadcast-request.json\n', unit)
        self.assertIn('WantedBy=multi-user.target\n', unit)
        verify = INSTALLER.index('--verify-installed')
        self.assertGreater(INSTALLER.index('systemctl enable repeater-message-broadcast.service'), verify)
        self.assertGreater(INSTALLER.index('systemctl enable --now repeater-message-broadcast.path'), verify)
        self.assertNotIn('systemctl enable --now repeater-message-broadcast.service', INSTALLER)

    def test_api17_requires_manual_privileged_upgrade_but_browser_payload_stays_dashboard_only(self):
        manifest = json.loads((ROOT / 'software-update.json').read_bytes())
        self.assertEqual(manifest['update_api'], 17)
        allowed = set(updater.ALLOWED_FILES)
        self.assertTrue({'dashboard/radio_messages.py', 'dashboard/native_state.py'} <= allowed)
        self.assertTrue(all(name.startswith('dashboard/') for name in allowed))
        self.assertFalse(any(name.endswith(('.wav', '.mp3', '.m4a', '.img', '.json')) for name in allowed))
        source = (ROOT / 'scripts/apply-software-update.py').read_text()
        self.assertIn('manifest["update_api"] != 17', source)
        tree = ast.parse((ROOT / 'dashboard/server.py').read_text())
        versions = [node.value.value for node in tree.body if isinstance(node, ast.Assign)
                    and any(isinstance(target, ast.Name) and target.id == 'VERSION' for target in node.targets)
                    and isinstance(node.value, ast.Constant)]
        self.assertEqual(versions, [manifest['version']])

    def test_private_audio_and_station_state_are_never_installed_from_public_bundle(self):
        sources = {source for source, _target in copied_files()}
        self.assertFalse(any(name.endswith(('.wav', '.mp3', '.m4a', '.img', '.key', '.pem')) for name in sources))
        self.assertFalse(any(Path(name).name in {'index.json', 'live.json', 'admin.json', 'dtmf.json',
            'settings.json', 'channel-pairs.json', 'message-broadcast-request.json'} for name in sources))


if __name__ == '__main__':
    unittest.main()
