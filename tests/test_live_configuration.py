"""Local RF authorization is explicit, bound to hardware, and closed on errors."""
import copy
import contextlib
import importlib.util
import json
import io
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('configure_live', ROOT / 'scripts/configure-live.py')
configure = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(configure)
guard = configure.guard

MONITOR = b'''[GLOBAL]
LOGICS=RepeaterLogic
[RepeaterLogic]
RX=RxAIOC
TX=NONE
[RxAIOC]
AUDIO_DEV=alsa:plughw:CARD=AIOC_RX,DEV=0
SQL_START_DELAY=0
SQL_DELAY=0
SQL_HANGTIME=1000
VOX_FILTER_DEPTH=20
VOX_THRESH=1000
PREAMP=-2.5
'''


class LiveConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.uid, self.gid = os.getuid(), os.getgid()
        self.paths = {name: self.root / filename for name, filename in {
            'config_path': 'svxlink-live.conf', 'auth_path': 'live.json',
            'monitor_path': 'monitor.conf', 'radio_config_path': 'radios.json',
            'state_path': 'state.json', 'catalog_path': 'catalog.json',
            'event_handler': 'live.tcl', 'dtmf_handler': 'dtmf.tcl'}.items()}
        self.devices = {role: '/dev/serial/by-id/usb-AIOC_All-In-One-Cable_' + role + '_fixture-if04'
                        for role in ('rx', 'tx')}
        self.radio_config = {'schema': 1, 'model': '5RX', 'devices': self.devices}
        self.catalog = {'schema': 1, 'default_pair_id': 'first', 'pairs': [
            {'id': 'first', 'label': 'First fixture', 'rx_hz': 145510000, 'tx_hz': 433510000, 'tone_hz': None},
            {'id': 'second', 'label': 'Second fixture', 'rx_hz': 145520000, 'tx_hz': 433520000, 'tone_hz': None}]}
        ids = {'rx': '1' * 32, 'tx': '2' * 32}
        self.state = {role: {'compatible': True, 'programmed': True, 'verified': True,
                            'verification_required': False, 'identity': 'aabbccdd',
                            'firmware': 'Fixture firmware', 'device': device,
                            'program_kind': 'pair', 'program_id': ids[role]}
                      for role, device in self.devices.items()}
        self.state['_pair'] = {'pair_id': 'first', 'phase': 'configured', 'stored_verified': True,
            'verification_required': False, 'activation_required': False,
            'catalog_digest': guard.catalog_digest(self.catalog), 'devices': self.devices,
            'backup_ids': ids, 'roles': {role: {'written': True, 'verified': True} for role in self.devices}}
        self.write('radio_config_path', self.radio_config)
        self.write('catalog_path', self.catalog)
        self.write('state_path', self.state)
        self.paths['monitor_path'].write_bytes(MONITOR)
        self.paths['monitor_path'].chmod(0o644)
        for name in ('event_handler', 'dtmf_handler'):
            self.paths[name].write_text('# fixture\n')
            self.paths[name].chmod(0o644)

    def write(self, name, value):
        self.paths[name].write_text(json.dumps(value))
        self.paths[name].chmod(0o640)

    def prepare(self, **overrides):
        options = {key: value for key, value in self.paths.items() if key not in ('event_handler', 'dtmf_handler')}
        options.update(overrides)
        return configure.prepare('K1TEST', owner_uid=self.uid, auth_group_id=self.gid,
            engine_group_id=self.gid, device_check=lambda auth: None, **options)

    def check(self, **overrides):
        options = {key: value for key, value in self.paths.items() if key != 'monitor_path'}
        options.update(owner_uid=self.uid, device_check=lambda auth: None)
        options.update(overrides)
        return guard.check(**options)

    def enable(self):
        options = {key: value for key, value in self.paths.items() if key not in ('monitor_path', 'auth_path')}
        return configure.set_enabled(True, self.paths['auth_path'], owner_uid=self.uid,
            group_id=self.gid, device_check=lambda auth: None, **options)

    def test_prepare_is_disabled_and_preserves_receive_tuning(self):
        authorization = self.prepare()
        self.assertFalse(authorization['enabled'])
        self.assertEqual(authorization['id_interval_minutes'], 9)
        self.assertEqual(stat.S_IMODE(self.paths['auth_path'].stat().st_mode), 0o640)
        self.assertEqual(stat.S_IMODE(self.paths['config_path'].stat().st_mode), 0o640)
        with self.assertRaisesRegex(ValueError, 'not authorized'):
            self.check()
        parser = guard.parser_from_bytes(self.paths['config_path'].read_bytes())
        self.assertEqual(parser['RxAIOC']['PREAMP'], '-2.5')
        self.assertEqual(parser['RxAIOC']['RAW_AUDIO_UDP_DEST'], '127.0.0.1:7355')
        self.assertEqual(parser['TxAIOC']['TIMEOUT'], '15')
        self.assertEqual(parser['TxAIOC']['SERIAL_SET_PINS'], '!DTR!RTS')
        self.assertEqual(parser['TxAIOC']['MASTER_GAIN'], '-40')
        self.assertEqual(parser['RepeaterLogic']['BOX_ID_INTERVAL_MINUTES'], '9')

    def test_explicit_enable_and_disable_only_change_authorization(self):
        self.prepare()
        with patch.object(configure.subprocess, 'run') as subprocess_run:
            self.enable()
            self.check()
            configure.set_enabled(False, self.paths['auth_path'], self.uid, self.gid)
            subprocess_run.assert_not_called()
        with self.assertRaisesRegex(ValueError, 'not authorized'):
            self.check()

    def test_local_tx_gain_is_bounded_authorized_and_exactly_matched(self):
        authorization = self.prepare(tx_gain_db=-34.5)
        self.assertEqual(authorization['tx_gain_db'], -34.5)
        self.enable(); self.check()
        parser = guard.parser_from_bytes(self.paths['config_path'].read_bytes())
        self.assertEqual(parser['TxAIOC']['MASTER_GAIN'], '-34.5')
        for value in (-40, -10, -22.5):
            self.assertEqual(guard.tx_gain(value), float(value))
        for value in (True, '-20', -40.1, -9.9, float('nan'), float('inf')):
            with self.subTest(value=value), self.assertRaises(ValueError):
                guard.tx_gain(value)
        parser['TxAIOC']['MASTER_GAIN'] = '-28.5'
        with self.assertRaises(ValueError):
            guard.validate_parser(parser, authorization)

    def test_testing_interval_is_authorized_exactly_and_retains_tx_gain(self):
        authorization = self.prepare(tx_gain_db=-10, id_interval_minutes=30)
        self.assertEqual(authorization['id_interval_minutes'], 30)
        self.assertEqual(authorization['tx_gain_db'], -10)
        self.enable(); self.check()
        parser = guard.parser_from_bytes(self.paths['config_path'].read_bytes())
        self.assertEqual(parser['RepeaterLogic']['BOX_ID_INTERVAL_MINUTES'], '30')
        self.assertEqual(parser['TxAIOC']['MASTER_GAIN'], '-10')
        parser['RepeaterLogic']['BOX_ID_INTERVAL_MINUTES'] = '9'
        with self.assertRaises(ValueError):
            guard.validate_parser(parser, authorization)
        for value in (True, '30', 30.0, 0, 10, 31):
            with self.subTest(value=value), self.assertRaises(ValueError):
                guard.id_interval(value)

    def test_testing_mode_prints_manual_identification_notice(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            configure.prepared_notice(30)
        self.assertIn('transmission remains unauthorized', output.getvalue())
        self.assertIn('Identify manually on the output', output.getvalue())
        self.assertIn('ten minutes', output.getvalue())
        self.assertIn('end of testing', output.getvalue())

    def test_any_verified_pair_in_same_catalog_can_resume(self):
        self.prepare(); self.enable()
        self.state['_pair']['pair_id'] = 'second'
        self.write('state_path', self.state)
        self.check()

    def test_every_pending_role_and_release_flag_blocks_start(self):
        self.prepare(); self.enable()
        for role in ('rx', 'tx', '_pair', 'programmer', 'unexpected'):
            for key in ('verification_required', 'release_verification_required'):
                with self.subTest(role=role, key=key):
                    state = copy.deepcopy(self.state)
                    state.setdefault(role, {})[key] = True
                    self.write('state_path', state)
                    with self.assertRaisesRegex(ValueError, 'unfinished'):
                        self.check()
        self.write('state_path', self.state)

    def test_changed_catalog_binding_identity_or_pair_record_blocks_start(self):
        self.prepare(); self.enable()
        modifications = [
            lambda value: value['rx'].update(device=self.devices['tx']),
            lambda value: value['rx'].update(identity='11223344'),
            lambda value: value['_pair'].update(stored_verified=False),
            lambda value: value['_pair'].update(pair_id='missing'),
            lambda value: value['_pair'].update(catalog_digest='0' * 64),
            lambda value: value['_pair']['roles']['tx'].update(verified=1),
            lambda value: value['tx'].update(program_id='3' * 32),
            lambda value: value.update(programmer={'verification_required': 'false'})]
        for change in modifications:
            state = copy.deepcopy(self.state)
            change(state)
            self.write('state_path', state)
            with self.assertRaises(ValueError):
                self.check()
        self.write('state_path', self.state)
        catalog = copy.deepcopy(self.catalog)
        catalog['pairs'][0]['rx_hz'] += 10000
        self.write('catalog_path', catalog)
        with self.assertRaises(ValueError):
            self.check()

    def test_missing_corrupt_duplicate_and_untrusted_files_fail_closed(self):
        self.prepare(); self.enable()
        for data in ('{', '{"enabled":true,"enabled":false}', '[]'):
            self.paths['auth_path'].write_text(data)
            with self.assertRaises((ValueError, KeyError)):
                self.check()
        self.prepare(); self.enable()
        self.paths['auth_path'].chmod(0o666)
        with self.assertRaisesRegex(ValueError, 'unsafe'):
            self.check()
        self.paths['auth_path'].unlink()
        self.paths['auth_path'].symlink_to(self.paths['state_path'])
        with self.assertRaises(OSError):
            self.check()

    def test_config_digest_and_exact_profile_protect_all_sections(self):
        self.prepare(); self.enable()
        original = self.paths['config_path'].read_bytes()
        self.paths['config_path'].write_bytes(original + b'\n# edited\n')
        with self.assertRaisesRegex(ValueError, 'configuration changed'):
            self.check()
        parser = guard.parser_from_bytes(original)
        authorization = json.loads(self.paths['auth_path'].read_text())
        modifications = [
            lambda value: value['GLOBAL'].update(CFG_DIR='/tmp/extra'),
            lambda value: value['RepeaterLogic'].update(MACROS='Macros'),
            lambda value: value['RepeaterLogic'].update(CALLSIGN='N0CALL'),
            lambda value: value['RxAIOC'].update(RAW_AUDIO_UDP_DEST='192.0.2.1:7355'),
            lambda value: value['TxAIOC'].update(PTT_PIN='RTS'),
            lambda value: value['TxAIOC'].update(SERIAL_SET_PINS='!RTS'),
            lambda value: value['TxAIOC'].update(TIMEOUT='0'),
            lambda value: value['TxAIOC'].update(MASTER_GAIN='0'),
            lambda value: value.add_section('Other'),
            lambda value: value['RxAIOC'].update(VOX_THRESH='nan'),
            lambda value: value['RxAIOC'].update(VOX_FILTER_DEPTH='1.5')]
        for change in modifications:
            changed = copy.deepcopy(parser)
            change(changed)
            with self.assertRaises(ValueError):
                guard.validate_parser(changed, authorization)

    def test_callsign_validation_rejects_placeholders_injection_and_long_cw(self):
        for value in ('N0CALL', 'N0CALL/P', 'REPLACE9', '', 'AB', '12345', 'ABCDE', 'k1test',
                      'K1TEST\nTX=Other', 'K1TEST;reboot', 'K1TEST/P/Q', 'K00000000000000'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                guard.callsign(value)
        self.assertEqual(guard.callsign('K1TEST/P'), 'K1TEST/P')

    def test_prepare_revokes_before_replacing_config(self):
        self.prepare(); self.enable()
        old_config = self.paths['config_path'].read_bytes()
        original_write = configure.atomic_write
        def fail_config(path, *args, **kwargs):
            if path == self.paths['config_path']:
                raise OSError('fixture write failure')
            return original_write(path, *args, **kwargs)
        with patch.object(configure, 'atomic_write', side_effect=fail_config):
            with self.assertRaises(OSError):
                self.prepare()
        self.assertFalse(json.loads(self.paths['auth_path'].read_text())['enabled'])
        self.assertEqual(old_config, self.paths['config_path'].read_bytes())

    def test_running_or_unknown_live_service_refuses_configuration_changes(self):
        for result in (SimpleNamespace(returncode=0, stdout='active\n'),
                       SimpleNamespace(returncode=0, stdout='activating\n'),
                       SimpleNamespace(returncode=1, stdout='')):
            with self.assertRaisesRegex(ValueError, 'Stop repeater-live'):
                configure.live_stopped(lambda *args, **kwargs: result)
        configure.live_stopped(lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout='inactive\n'))

    def test_runtime_checks_card_identity_and_serial_identity_without_opening_ports(self):
        authorization = self.prepare()
        sound_root = self.root / 'sound'
        sound_root.mkdir()
        for index, role in enumerate(('rx', 'tx')):
            card = sound_root / ('card' + str(index))
            usb = self.root / ('usb_' + role)
            card.mkdir(); usb.mkdir()
            (card / 'id').write_text('AIOC_' + role.upper())
            (card / 'device').symlink_to(usb)
            for name, value in (('idVendor', '1209'), ('idProduct', '7388'), ('serial', role + '_fixture')):
                (usb / name).write_text(value)
        original_stat = guard.os.stat
        def device_stat(path, *args, **kwargs):
            if str(path) in self.devices.values():
                return SimpleNamespace(st_mode=stat.S_IFCHR)
            return original_stat(path, *args, **kwargs)
        def serial_identity(path):
            return '1209', '7388', guard.DEVICE_RE.fullmatch(path).group(1)
        with patch.object(guard.os, 'stat', side_effect=device_stat), \
                patch.object(guard.os.path, 'samefile', return_value=False), \
                patch.object(guard, 'serial_usb_identity', side_effect=serial_identity):
            guard.verify_runtime_devices(authorization, sound_root)
            (self.root / 'usb_tx' / 'serial').write_text('other')
            with self.assertRaisesRegex(ValueError, 'ALSA'):
                guard.verify_runtime_devices(authorization, sound_root)
            (self.root / 'usb_tx' / 'serial').write_text('tx_fixture')
            with patch.object(guard, 'serial_usb_identity', return_value=('1209', '7388', 'other')):
                with self.assertRaisesRegex(ValueError, 'serial radio'):
                    guard.verify_runtime_devices(authorization, sound_root)

    def test_service_is_manual_and_preflight_is_privileged_without_another_lock(self):
        unit = (ROOT / 'systemd/repeater-live.service').read_text()
        self.assertIn('ExecStartPre=+/usr/bin/python3 /opt/repeater-box/scripts/check-live-config.py', unit)
        self.assertIn('Restart=no', unit)
        self.assertIn('Conflicts=repeater-engine.service repeater-rx-monitor.service svxlink.service', unit)
        self.assertNotIn('flock', unit)
        self.assertNotIn('fcntl', (ROOT / 'scripts/check-live-config.py').read_text())


if __name__ == '__main__':
    unittest.main()
