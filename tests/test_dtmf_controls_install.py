"""Native DTMF installation with synthetic WAVs and fake service commands only."""
import ast
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock
import wave

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("dtmf_native_installer", ROOT / "scripts/install-dtmf-controls.py")
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


class DtmfControlsInstallTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(); self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.bundle, self.target = self.base/'bundle', self.base/'installed'
        for path in (self.bundle/'scripts', self.target/'scripts', self.target/'prompts', self.base/'state', self.base/'backups'):
            path.mkdir(parents=True, exist_ok=True); path.chmod(0o755)
        for name in (*module.NATIVE_SCRIPTS, 'scripts/build-dtmf-prompts.py'):
            shutil.copyfile(ROOT/name, self.bundle/name)
        self.previous = {}
        for name in module.NATIVE_FILES:
            raw = b'# previous native file\n'
            (self.target/name).write_bytes(raw); (self.target/name).chmod(0o644)
            self.previous[name] = raw
        self.old_marker = b'{"schema":1,"protocol":1}\n'
        self.marker = self.target/'dtmf-controls.json'; self.marker.write_bytes(self.old_marker)
        self.states = {unit: 'inactive' for unit in (*module.SERVICES, *module.BUSY_SERVICES)}
        self.events = []; self.invocation = 'a'*32
        self.installer = module.Installer(self.bundle, install_root=self.target,
            radio_state=self.base/'backups/state.json', state_dir=self.base/'state',
            owner_uid=os.getuid(), owner_gid=os.getgid(), runner=self.service,
            builder=self.build, pause=lambda seconds: self.events.append(('pause', seconds)),
            native_reader=lambda: {'online': True, 'invocation_id': self.invocation})

    def service(self, action, unit):
        self.events.append((action, unit))
        if action == 'is-active': return self.states[unit]
        if action == 'invocation': return self.invocation
        if action == 'stop': self.states[unit] = 'inactive'
        elif action == 'start':
            self.states[unit] = 'active'; self.invocation = 'b'*32
        else: raise AssertionError('Unexpected service mutation')

    def build(self, destination):
        self.events.append(('build', 'begin'))
        destination.mkdir(parents=True)
        for name in module.NATIVE_PROMPTS:
            with wave.open(str(destination/(name+'.wav')), 'wb') as sound:
                sound.setnchannels(1); sound.setsampwidth(2); sound.setframerate(16000)
                sound.writeframes(b'\0\0'*1600)
        self.events.append(('build', 'end'))

    def mutations(self):
        return [event for event in self.events if event[0] in ('start', 'stop')]

    def assert_original(self):
        self.assertEqual(self.marker.read_bytes(), self.old_marker)
        for name, raw in self.previous.items(): self.assertEqual((self.target/name).read_bytes(), raw)

    def assert_manifest(self):
        marker = json.loads(self.marker.read_text())
        self.assertEqual(set(marker), {'schema', 'protocol', 'files'})
        self.assertEqual((marker['schema'], marker['protocol']), (1, 2))
        self.assertEqual(set(marker['files']), module.NATIVE_FILES)
        for name, digest in marker['files'].items():
            self.assertEqual(digest, hashlib.sha256((self.target/name).read_bytes()).hexdigest())
            self.assertEqual((self.target/name).stat().st_mode & 0o777, 0o644)
        self.assertEqual(self.marker.stat().st_mode & 0o777, 0o644)

    def test_native_allowlist_matches_dashboard_capability_contract(self):
        names = {'NATIVE_SCRIPTS', 'NATIVE_PROMPTS', 'NATIVE_FILES'}
        tree = ast.parse((ROOT/'dashboard/dtmf_control.py').read_text())
        definitions = [node for node in tree.body if isinstance(node, ast.Assign)
                       and any(isinstance(target, ast.Name) and target.id in names for target in node.targets)]
        constants = {}; exec(compile(ast.Module(body=definitions, type_ignores=[]), 'native_constants', 'exec'), constants)
        for name in names: self.assertEqual(getattr(module, name), constants[name])

    def test_complete_stage_precedes_stop_previous_live_mode_restarts_once_and_marker_is_last(self):
        live = 'repeater-live.service'; self.states[live] = 'active'
        replace = self.installer._replace
        def write(path, *args, **kwargs):
            self.events.append(('write', str(path.relative_to(self.target))))
            return replace(path, *args, **kwargs)
        with mock.patch.object(self.installer, '_replace', side_effect=write):
            self.assertEqual(self.installer.install(), live)
        self.assertLess(self.events.index(('build', 'end')), self.events.index(('stop', live)))
        self.assertEqual(self.mutations(), [('stop', live), ('start', live)])
        self.assertEqual([value for kind, value in self.events if kind == 'write'][-1], 'dtmf-controls.json')
        self.assertEqual(self.states[live], 'active'); self.assert_manifest()
        self.assertFalse(any(path.name.startswith('.dtmf-controls-stage-') for path in self.target.iterdir()))

    def test_no_previous_active_service_installs_without_starting_or_enabling_any_mode(self):
        self.assertIsNone(self.installer.install()); self.assertEqual(self.mutations(), [])
        self.assert_manifest()
        self.assertEqual(set(path.name for path in self.target.iterdir()),
                         {'scripts', 'prompts', '.maintenance.lock', 'dtmf-controls.json'})

    def test_receive_only_mode_restarts_only_itself_and_survives_readiness_probes(self):
        unit = 'repeater-rx-monitor.service'; self.states[unit] = 'active'
        self.assertEqual(self.installer.install(), unit)
        self.assertEqual(self.mutations(), [('stop', unit), ('start', unit)])
        self.assertEqual(len([event for event in self.events if event[0] == 'pause']), 4)
        self.assert_manifest()

    def test_prompt_generation_failure_never_stops_service_or_changes_installed_files(self):
        self.states['repeater-live.service'] = 'active'
        self.installer.builder = mock.Mock(side_effect=ValueError('synthetic generation failure'))
        with self.assertRaises(ValueError): self.installer.install()
        self.assertEqual(self.mutations(), []); self.assert_original()

    def test_invalid_staged_audio_is_rejected_before_service_stop(self):
        self.states['repeater-live.service'] = 'active'
        def corrupt(destination):
            self.build(destination); (destination/'choose_aprs.wav').write_bytes(b'bad wav')
        self.installer.builder = corrupt
        with self.assertRaises((EOFError, wave.Error)): self.installer.install()
        self.assertEqual(self.mutations(), []); self.assert_original()

    def test_refuses_vendor_multiple_managed_transitional_and_active_message_services(self):
        cases = [{'svxlink.service': 'active'}, {'repeater-engine.service': 'active', 'repeater-live.service': 'active'},
                 {'repeater-live.service': 'activating'}, {'repeater-message-broadcast.service': 'active'}]
        for states in cases:
            self.states.update({unit: 'inactive' for unit in self.states}); self.states.update(states)
            with self.subTest(states=states), self.assertRaises(module.InstallError): self.installer.install()
            self.assertEqual(self.mutations(), []); self.assert_original()

    def test_pending_radio_verification_or_unconsumed_queue_prevents_targeted_update(self):
        path = self.installer.radio_state
        for value in ({'rx': {'verification_required': True}}, {'tx': {'release_verification_required': True}}, {'rx': {'verification_required': 'yes'}}):
            path.write_text(json.dumps(value))
            with self.subTest(state=value), self.assertRaises(module.InstallError): self.installer.install()
            self.assertEqual(self.mutations(), []); self.assert_original()
        path.unlink()
        request = self.installer.state_dir/'radio-programming-request.json'
        request.write_text(json.dumps({'request_id': 'd'*32}))
        with self.assertRaises(module.InstallError): self.installer.install()
        self.assertEqual(self.mutations(), []); self.assert_original()

    def test_copy_failure_restores_files_and_marker_and_leaves_previous_service_stopped(self):
        unit = 'repeater-live.service'; self.states[unit] = 'active'
        replace = self.installer._replace; failed = False
        def fail_once(path, *args, **kwargs):
            nonlocal failed
            if path.name == 'choose_aprs.wav' and not failed:
                failed = True; raise OSError('synthetic copy fault')
            return replace(path, *args, **kwargs)
        with mock.patch.object(self.installer, '_replace', side_effect=fail_once):
            with self.assertRaisesRegex(module.InstallError, 'Previous files were restored'): self.installer.install()
        self.assertEqual(self.states[unit], 'inactive'); self.assertNotIn(('start', unit), self.mutations())
        self.assert_original()

    def test_failed_service_stop_does_not_replace_any_native_file(self):
        unit = 'repeater-live.service'; self.states[unit] = 'active'; run = self.installer.run
        def fail_stop(action, target):
            if action == 'stop': raise module.InstallError('synthetic stop fault')
            return run(action, target)
        self.installer.run = fail_stop
        with mock.patch.object(self.installer, '_replace') as replace:
            with self.assertRaisesRegex(module.InstallError, 'files were not changed'): self.installer.install()
            replace.assert_not_called()
        self.assert_original()

    def test_stale_live_heartbeat_cannot_publish_marker_even_when_service_reports_active(self):
        unit = 'repeater-live.service'; self.states[unit] = 'active'
        self.installer.native_reader = lambda: {'online': True, 'invocation_id': 'a'*32}
        with self.assertRaisesRegex(module.InstallError, 'Previous files were restored'): self.installer.install()
        self.assertEqual(self.states[unit], 'inactive'); self.assert_original()
        self.assertEqual(len([event for event in self.events if event[0] == 'pause']), 20)

    def test_late_tcl_startup_failure_stops_service_and_restores_previous_capability(self):
        unit = 'repeater-rx-monitor.service'; self.states[unit] = 'active'
        def pause(seconds):
            self.events.append(('pause', seconds))
            if len([event for event in self.events if event[0] == 'pause']) == 2: self.states[unit] = 'failed'
        self.installer.pause = pause
        with self.assertRaisesRegex(module.InstallError, 'Previous files were restored'): self.installer.install()
        self.assertEqual(self.states[unit], 'inactive'); self.assert_original()

    def test_marker_write_failure_after_restart_rolls_back_and_stops_live_mode(self):
        unit = 'repeater-live.service'; self.states[unit] = 'active'
        replace = self.installer._replace; failed = False
        def fail_marker(path, *args, **kwargs):
            nonlocal failed
            if path == self.marker and not failed:
                failed = True; raise OSError('synthetic marker sync fault')
            return replace(path, *args, **kwargs)
        with mock.patch.object(self.installer, '_replace', side_effect=fail_marker):
            with self.assertRaisesRegex(module.InstallError, 'Previous files were restored'): self.installer.install()
        self.assertEqual(self.states[unit], 'inactive'); self.assert_original()

    def test_symlink_hardlink_unprotected_target_and_empty_source_fail_before_stop(self):
        path = self.target/'scripts/dtmf-client.py'; outside = self.base/'outside'
        outside.write_bytes(b'outside stays unchanged'); path.unlink(); path.symlink_to(outside)
        with self.assertRaises(OSError): self.installer.install()
        self.assertEqual(outside.read_bytes(), b'outside stays unchanged'); self.assertEqual(self.mutations(), [])
        path.unlink(); os.link(outside, path)
        with self.assertRaises(module.InstallError): self.installer.install()
        path.unlink(); path.write_bytes(self.previous['scripts/dtmf-client.py']); path.chmod(0o666)
        with self.assertRaises(module.InstallError): self.installer.install()
        path.chmod(0o644); (self.bundle/'scripts/dtmf-client.py').write_bytes(b'')
        with self.assertRaises(module.InstallError): self.installer.install()
        self.assertEqual(self.mutations(), []); self.assert_original()

    def test_lock_contention_and_wrong_inherited_descriptor_are_rejected(self):
        descriptor = os.open(self.installer.lock, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(module.InstallError, 'Another radio'): self.installer.install()
        finally: os.close(descriptor)
        wrong = os.open(self.base/'wrong-lock', os.O_CREAT | os.O_RDWR, 0o600)
        try:
            self.installer.lock_fd = wrong
            with self.assertRaises(module.InstallError): self.installer.install()
        finally: os.close(wrong)
        self.assertEqual(self.mutations(), []); self.assert_original()

    def test_register_only_under_inherited_lock_accepts_pending_verification_without_starting(self):
        self.installer.install(); self.events.clear()
        self.installer.radio_state.write_text(json.dumps({'rx': {'verification_required': True}}))
        descriptor = os.open(self.installer.lock, os.O_RDWR)
        contender = os.open(self.installer.lock, os.O_RDWR)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.installer.lock_fd = descriptor
            self.installer.builder = mock.Mock(side_effect=AssertionError('must not build in register-only'))
            self.installer.install(register_only=True)
            with self.assertRaises(BlockingIOError): fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally: os.close(contender); os.close(descriptor)
        self.assertEqual(self.mutations(), []); self.assert_manifest()

    def test_register_only_refuses_active_mode_and_different_installed_source(self):
        self.installer.install(); self.events.clear()
        self.states['repeater-engine.service'] = 'active'
        with self.assertRaises(module.InstallError): self.installer.install(register_only=True)
        self.states['repeater-engine.service'] = 'inactive'
        path = self.target/'scripts/dtmf-client.py'; path.write_bytes(b'# changed native source\n')
        with self.assertRaises(module.InstallError): self.installer.install(register_only=True)
        self.assertEqual(self.mutations(), [])


if __name__ == '__main__': unittest.main()
