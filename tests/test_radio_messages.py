"""Bounded private message storage; no real decoders, services or RF are used."""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import resource
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'dashboard'))
import radio_messages as messages
SPEC = importlib.util.spec_from_file_location('message_converter', ROOT / 'scripts/convert-message-audio.py')
worker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(worker)


def wav_bytes(frames=1600, channels=1, rate=16000, width=2):
    output = io.BytesIO()
    with wave.open(output, 'wb') as audio:
        audio.setnchannels(channels); audio.setsampwidth(width); audio.setframerate(rate)
        audio.writeframes(b'\x00' * frames * channels * width)
    return output.getvalue()


class RadioMessagesTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state, self.library = self.root / 'state', self.root / 'messages'
        self.state.mkdir(mode=0o750); self.library.mkdir(mode=0o750)
        self.native = {'schema': 1, 'invocation_id': 'a' * 32, 'heartbeat_ms': 1000,
            'online': True, 'rx_open': False, 'tx_on': False, 'control_busy': False,
            'message_job': '', 'message_state': 'idle', 'message_sequence': 0}
        self.busy = None
        self.conversions = []
        self.controller = self.controller_with()

    def convert(self, source, destination, extension):
        self.assertEqual(stat.S_IMODE(source.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(source.stat().st_mode), 0o600)
        self.assertEqual(source.name, 'source')
        self.conversions.append(extension)
        destination.write_bytes(source.read_bytes())
        destination.chmod(0o600)

    def controller_with(self, **overrides):
        options = dict(messages_dir=self.library, native_state_reader=lambda: self.native,
            busy_check=lambda: self.busy, converter=self.convert, clock=lambda: 1700000000,
            result_owner_uid=os.getuid())
        options.update(overrides)
        return messages.RadioMessages(self.state, **options)

    def upload(self, name='fixture.wav', **options):
        return self.controller.store_upload(name, wav_bytes(**options))

    def broadcast(self, item, **overrides):
        values = dict(identifier=item['id'], digest=item['sha256'], pair_id='fixture',
                      catalog_digest='b' * 64, invocation_id=self.native['invocation_id'])
        values.update(overrides)
        return self.controller.request_broadcast(**values)

    def result(self, request, phase='finished', **overrides):
        value = {'schema': 1, 'request_id': request['request_id'], 'message_id': request['message_id'],
                 'phase': phase, 'success': phase == 'finished'}
        value.update(overrides)
        messages.atomic_json(self.state / self.controller.RESULT, value)

    def test_upload_keeps_only_canonical_private_pcm_and_preview_matches_hash(self):
        item = self.upload()
        self.assertEqual(item['duration_seconds'], 0.1)
        self.assertEqual(item['bytes'], 3244)
        self.assertEqual(item['preview_url'], '/api/messages/' + item['id'] + '/audio')
        self.assertEqual(sorted(path.name for path in self.library.iterdir()), [item['id'] + '.wav', 'index.json'])
        self.assertEqual(stat.S_IMODE((self.library / (item['id'] + '.wav')).stat().st_mode), 0o640)
        self.assertEqual((self.library / (item['id'] + '.wav')).stat().st_gid, self.library.stat().st_gid)
        handle, metadata = self.controller.open_preview(item['id'])
        with handle:
            raw = handle.read()
        self.assertEqual(metadata, item)
        self.assertEqual(hashlib.sha256(raw).hexdigest(), item['sha256'])
        with wave.open(io.BytesIO(raw)) as audio:
            self.assertEqual((audio.getnchannels(), audio.getframerate(), audio.getsampwidth()), (1, 16000, 2))
        self.assertTrue(self.controller.status()['can_broadcast'])
        self.assertFalse(self.controller.active)

    def test_formats_are_forced_from_a_safe_display_name_not_used_as_a_path(self):
        for name, extension in (('recording.M4A', 'm4a'), ('voice.mp3', 'mp3'), ('$(touch unsafe).wav', 'wav')):
            with self.subTest(name=name):
                item = self.upload(name)
                self.assertEqual(item['name'], name)
                self.assertEqual(self.conversions[-1], extension)
        for name in ('../file.wav', '/tmp/file.wav', 'C:\\file.wav', 'file.wav\n', 'file.txt', '', 'x' * 257 + '.wav'):
            with self.subTest(name=name), self.assertRaises(messages.MessageError):
                self.controller.store_upload(name, wav_bytes())

    def test_empty_oversized_and_nonbinary_uploads_are_refused_before_conversion(self):
        for payload in (b'', b'x' * (messages.MAX_UPLOAD_BYTES + 1), 'audio'):
            with self.subTest(size=len(payload)), self.assertRaises(messages.MessageError):
                self.controller.store_upload('fixture.wav', payload)
        self.assertEqual(self.conversions, [])

    def test_overlength_incomplete_and_wrong_pcm_formats_leave_no_upload_files(self):
        for raw in (wav_bytes(frames=messages.MAX_FRAMES + 1), wav_bytes(channels=2),
                    wav_bytes(rate=48000), wav_bytes(width=1), wav_bytes()[:-20], b'not audio'):
            with self.subTest(size=len(raw)), self.assertRaises(messages.MessageError):
                self.controller.store_upload('fixture.wav', raw)
            self.assertEqual(list(self.library.iterdir()), [])
            self.assertFalse(self.controller.upload_pending)
        exact = self.upload(frames=messages.MAX_FRAMES)
        self.assertEqual(exact['duration_seconds'], 30)

    def test_decoder_failure_and_timeout_are_sanitized_and_clean_temporary_data(self):
        for error in (OSError('/private/hidden/source'),
                      subprocess.TimeoutExpired(['/private/hidden/converter'], 20)):
            with self.subTest(error=type(error).__name__):
                self.controller.converter = lambda *args, error=error: (_ for _ in ()).throw(error)
                with self.assertRaises(messages.MessageError) as found:
                    self.upload()
                self.assertNotIn('/private', str(found.exception))
                self.assertEqual(list(self.library.iterdir()), [])
                self.assertFalse(self.controller.active)

    def test_decoder_cannot_return_a_symlink_in_place_of_pcm(self):
        outside = self.root / 'outside.wav'
        original = wav_bytes()
        outside.write_bytes(original)
        def symlink(source, destination, extension):
            destination.symlink_to(outside)
        self.controller.converter = symlink
        with self.assertRaises(messages.MessageError):
            self.upload()
        self.assertEqual(outside.read_bytes(), original)
        self.assertEqual(list(self.library.iterdir()), [])

    def test_quota_is_ten_immutable_messages_and_delete_requires_reviewed_hash(self):
        items = [self.upload('fixture%d.wav' % index, frames=16) for index in range(10)]
        self.assertFalse(self.controller.status()['can_upload'])
        with self.assertRaises(messages.MessageError):
            self.upload()
        with self.assertRaises(messages.MessageError):
            self.controller.delete_message(items[0]['id'], 'f' * 64)
        self.controller.delete_message(items[0]['id'], items[0]['sha256'])
        self.assertEqual(len(self.controller.status()['messages']), 9)
        self.assertTrue(self.controller.status()['can_upload'])
        with self.assertRaises(messages.MessageError):
            self.controller.open_preview(items[0]['id'])

    def test_id_collision_never_overwrites_or_deletes_existing_canonical_audio(self):
        with patch.object(messages.secrets, 'token_hex', return_value='c' * 32):
            item = self.upload()
            before = (self.library / (item['id'] + '.wav')).read_bytes()
            with self.assertRaises(messages.MessageError):
                self.upload('other.wav', frames=32)
        self.assertEqual((self.library / (item['id'] + '.wav')).read_bytes(), before)
        self.assertEqual(len(self.controller.status()['messages']), 1)

    def test_failed_metadata_commit_keeps_bounded_unselectable_canonical_audio(self):
        with patch.object(messages, 'atomic_json', side_effect=OSError('storage unavailable')):
            for _ in range(10):
                with self.assertRaises(messages.MessageError):
                    self.upload(frames=16)
        state = self.controller.status()
        self.assertEqual(state['messages'], [])
        self.assertFalse(state['can_upload'])
        self.assertEqual(len(list(self.library.glob('*.wav'))), 10)
        with self.assertRaises(messages.MessageError):
            self.upload()

    def test_library_and_preview_reject_symlinks_and_corruption(self):
        item = self.upload()
        path = self.library / (item['id'] + '.wav')
        before = path.read_bytes()
        path.write_bytes(before[:-1] + b'\x01')
        with self.assertRaises(messages.MessageError):
            self.controller.open_preview(item['id'])
        with self.assertRaises(messages.MessageError):
            self.broadcast(item)
        path.unlink()
        outside = self.root / 'outside.wav'
        outside.write_bytes(before)
        path.symlink_to(outside)
        self.assertFalse(self.controller.status()['available'])
        for identifier in ('../outside', '/tmp/audio.wav', 'a' * 31, ''):
            with self.subTest(identifier=identifier), self.assertRaises(messages.MessageError):
                self.controller.open_preview(identifier)
        self.assertEqual(outside.read_bytes(), before)

    def test_symlinked_index_cannot_be_read_or_overwritten(self):
        outside = self.root / 'outside.json'
        outside.write_text('private fixture')
        (self.library / 'index.json').symlink_to(outside)
        self.assertFalse(self.controller.status()['available'])
        with self.assertRaises(messages.MessageError):
            self.upload()
        self.assertEqual(outside.read_text(), 'private fixture')

    def test_conversion_does_not_hold_library_lock_and_prevents_concurrent_tasks(self):
        item = self.upload()
        entered, release, status_done = threading.Event(), threading.Event(), threading.Event()
        outcomes = []
        def convert(source, destination, extension):
            entered.set()
            if not release.wait(5):
                raise RuntimeError('fixture decode was not released')
            self.convert(source, destination, extension)
        self.controller.converter = convert
        def upload():
            try:
                outcomes.append(self.upload('second.wav'))
            except Exception as error:
                outcomes.append(error)
        worker_thread = threading.Thread(target=upload)
        worker_thread.start()
        status_thread = threading.Thread(target=lambda: (self.controller.status(), status_done.set()))
        try:
            self.assertTrue(entered.wait(2))
            status_thread.start()
            self.assertTrue(status_done.wait(1), 'Status waited for conversion to finish')
            self.assertTrue(self.controller.active)
            for action in (lambda: self.upload(), lambda: self.broadcast(item),
                           lambda: self.controller.delete_message(item['id'], item['sha256'])):
                with self.assertRaises(messages.MessageError):
                    action()
        finally:
            release.set()
            worker_thread.join(3)
            if status_thread.ident is not None:
                status_thread.join(3)
        self.assertEqual(len(outcomes), 1)
        self.assertIsInstance(outcomes[0], dict)

    def test_busy_native_state_maintenance_and_stale_review_cannot_request_rf(self):
        item = self.upload()
        for name in ('rx_open', 'tx_on', 'control_busy'):
            self.native[name] = True
            with self.subTest(name=name), self.assertRaises(messages.MessageError):
                self.broadcast(item)
            self.assertFalse(self.controller.status()['can_broadcast'])
            self.native[name] = False
        self.native['online'] = False
        with self.assertRaises(messages.MessageError):
            self.broadcast(item)
        self.native['online'] = True
        for arguments in ({'digest': 'f' * 64}, {'invocation_id': 'f' * 32},
                          {'pair_id': '../other'}, {'catalog_digest': 'invalid'}):
            with self.subTest(arguments=arguments), self.assertRaises(messages.MessageError):
                self.broadcast(item, **arguments)
        self.busy = 'Radio programming is in progress.'
        for action in (lambda: self.broadcast(item), lambda: self.upload(),
                       lambda: self.controller.delete_message(item['id'], item['sha256'])):
            with self.assertRaises(messages.MessageError):
                action()
        self.assertFalse((self.state / self.controller.REQUEST).exists())

    def test_explicit_broadcast_spools_only_reviewed_identifiers_and_never_replays(self):
        item = self.upload()
        state = self.broadcast(item)
        self.assertTrue(state['job']['pending'])
        request_path = self.state / self.controller.REQUEST
        before = request_path.read_bytes()
        request = json.loads(before)
        self.assertEqual(set(request), {'schema', 'request_id', 'action', 'message_id', 'sha256',
            'pair_id', 'catalog_digest', 'invocation_id', 'requested_at'})
        self.assertNotIn(str(self.root).encode(), before)
        self.assertEqual(stat.S_IMODE(request_path.stat().st_mode), 0o600)
        with self.assertRaises(messages.MessageError):
            self.broadcast(item)
        reopened = self.controller_with()
        self.assertTrue(reopened.status()['job']['pending'])
        self.assertEqual(request_path.read_bytes(), before)
        self.result(request, request_id='d' * 32)
        self.assertTrue(self.controller.status()['job']['pending'])
        self.result(request, message='/private/exception text must not reach the dashboard')
        state = self.controller.status()
        self.assertTrue(state['job']['success'])
        self.assertNotIn('/private', json.dumps(state))
        self.assertFalse(self.controller.active)
        self.broadcast(item)
        self.assertNotEqual(json.loads(request_path.read_bytes())['request_id'], request['request_id'])

    def test_invalid_result_and_native_state_fail_closed(self):
        item = self.upload()
        self.broadcast(item)
        request = json.loads((self.state / self.controller.REQUEST).read_bytes())
        self.result(request, phase='arbitrary-command')
        self.assertFalse(self.controller.status()['can_broadcast'])
        self.assertTrue(self.controller.active)
        (self.state / self.controller.REQUEST).unlink()
        for native in (None, dict(self.native, online=1), dict(self.native, invocation_id='bad'),
                       dict(self.native, message_job='../path')):
            self.native = native
            self.assertFalse(self.controller.status()['can_broadcast'])

    def test_inconsistent_phase_and_success_flags_do_not_clear_a_pending_request(self):
        item = self.upload()
        self.broadcast(item)
        request = json.loads((self.state / self.controller.REQUEST).read_bytes())
        for phase, success in (('preparing', True), ('accepted', False), ('finished', False), ('failed', True)):
            with self.subTest(phase=phase, success=success):
                self.result(request, phase=phase, success=success)
                self.assertFalse(self.controller.status()['available'])
                self.assertTrue(self.controller.active)

    def test_restart_cleans_only_old_owned_private_conversion_workspaces(self):
        old = self.library / '.upload-old00000'
        old.mkdir(mode=0o700)
        (old / 'source').write_bytes(b'private original upload')
        os.utime(old, (0, 0))
        current = self.library / '.upload-new00000'
        current.mkdir(mode=0o700)
        (current / 'source').write_bytes(b'current conversion')
        unknown = self.library / '.upload-own00000'
        unknown.mkdir(mode=0o700)
        (unknown / 'other-file').write_bytes(b'unrelated file')
        os.utime(unknown, (0, 0))
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / 'source').write_bytes(b'outside data')
        (self.library / '.upload-lnk00000').symlink_to(outside)
        self.controller_with()
        self.assertFalse(old.exists())
        self.assertTrue(current.exists())
        self.assertTrue(unknown.exists())
        self.assertEqual((outside / 'source').read_bytes(), b'outside data')


class ConversionBoundaryTest(unittest.TestCase):
    def test_forced_demuxers_disable_external_references_and_bound_decoded_frames(self):
        for extension, demuxer in worker.FORMATS.items():
            arguments = worker.command(extension, '/private/generated/source', '/private/generated/decoded.wav')
            self.assertEqual(arguments[0], '/usr/bin/ffmpeg')
            self.assertEqual(arguments[arguments.index('-protocol_whitelist') + 1], 'file')
            self.assertEqual(arguments[arguments.index('-f') + 1], demuxer)
            self.assertIn('aresample=16000,atrim=end_sample=480001', arguments)
            self.assertIn('0:a:0', arguments)
            self.assertIn('-nostdin', arguments)
            self.assertEqual(arguments[-1], '/private/generated/decoded.wav')
            if extension == 'm4a':
                self.assertEqual(arguments[arguments.index('-enable_drefs') + 1], '0')
                self.assertEqual(arguments[arguments.index('-use_absolute_path') + 1], '0')
        with self.assertRaises(ValueError):
            worker.command('playlist', '/tmp/source', '/tmp/destination')

    def test_decoder_limits_are_installed_before_exec_in_a_fresh_worker(self):
        with patch.object(worker.resource, 'getrlimit', return_value=(resource.RLIM_INFINITY, resource.RLIM_INFINITY)), \
                patch.object(worker.resource, 'setrlimit') as limits:
            worker.limit_resources()
        values = dict(call.args for call in limits.call_args_list)
        self.assertEqual(values[resource.RLIMIT_CORE], (0, 0))
        self.assertEqual(values[resource.RLIMIT_CPU], (12, 15))
        self.assertEqual(values[resource.RLIMIT_FSIZE], (2 * 1024 * 1024,) * 2)
        self.assertLessEqual(values[resource.RLIMIT_AS][0], 384 * 1024 * 1024)
        self.assertEqual(values[resource.RLIMIT_NOFILE], (64, 64))
        with patch.object(worker.os, 'geteuid', return_value=0), patch.object(worker.os, 'execv') as execute:
            with self.assertRaises(ValueError):
                worker.main(['wav', '/tmp/source', '/tmp/decoded.wav'])
            execute.assert_not_called()

    def test_controller_conversion_uses_no_shell_and_sanitizes_process_failures(self):
        with patch.object(messages.subprocess, 'run', return_value=type('Result', (), {'returncode': 0})()) as run:
            messages.run_conversion('/tmp/source', '/tmp/output', 'm4a')
        self.assertEqual(run.call_args.args[0][0], '/usr/bin/python3')
        self.assertNotIn('shell', run.call_args.kwargs)
        self.assertEqual(run.call_args.kwargs['timeout'], 20)
        self.assertTrue(run.call_args.kwargs['start_new_session'])
        for failure in (subprocess.TimeoutExpired(['/private/tool'], 20), OSError('/private/missing')):
            with patch.object(messages.subprocess, 'run', side_effect=failure):
                with self.assertRaises(messages.MessageError) as found:
                    messages.run_conversion('/private/source', '/private/output', 'wav')
                self.assertNotIn('/private', str(found.exception))


if __name__ == '__main__':
    unittest.main()
