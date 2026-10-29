"""Privileged one-shot dispatch is hardware-free, bounded and never replayed."""
from datetime import datetime, timezone
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import wave

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('message_dispatcher', ROOT / 'scripts/broadcast-message.py')
worker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(worker)


class MessageDispatcherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.uid, self.gid = os.getuid(), os.getgid()
        self.state, self.library = self.root / 'state', self.root / 'library'
        self.playback, self.consumed = self.root / 'playback', self.root / 'consumed'
        for path in (self.state, self.library, self.playback):
            path.mkdir(mode=0o750)
        self.consumed.mkdir(mode=0o700)
        self.wall, self.elapsed = 1800000000.0, 0.0
        self.events, self.stops, self.checks = [], [], []
        buffer = io.BytesIO()
        with wave.open(buffer, 'wb') as audio:
            audio.setnchannels(1); audio.setsampwidth(2); audio.setframerate(16000)
            audio.writeframes(b'\x00\x10' * 16000)
        self.raw = buffer.getvalue()
        self.request = {'schema': 1, 'request_id': '1' * 32, 'action': 'broadcast',
            'message_id': '2' * 32, 'sha256': hashlib.sha256(self.raw).hexdigest(),
            'pair_id': 'fixture', 'catalog_digest': '3' * 64, 'invocation_id': '4' * 32,
            'requested_at': self.date(self.wall)}
        self.item = {'id': self.request['message_id'], 'sha256': self.request['sha256'],
            'name': 'fixture.wav', 'frames': 16000, 'bytes': len(self.raw), 'created_at': self.date(self.wall)}
        self.save(self.state / worker.REQUEST_NAME, self.request)
        self.save(self.library / 'index.json', {'schema': 1, 'messages': [self.item]})
        self.asset = self.library / (self.item['id'] + '.wav')
        self.asset.write_bytes(self.raw); self.asset.chmod(0o640)
        self.start_state = {'schema': 1, 'invocation_id': self.request['invocation_id'], 'heartbeat_ms': 1000,
            'online': True, 'rx_open': False, 'tx_on': False, 'control_busy': False,
            'message_job': '', 'message_state': 'idle', 'message_sequence': 10}
        self.behavior = 'success'
        self.worker = self.make_worker()

    def date(self, value):
        return datetime.fromtimestamp(value, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

    def save(self, path, value):
        path.write_text(json.dumps(value)); path.chmod(0o640)

    def make_worker(self, **overrides):
        options = dict(state_dir=self.state, messages_dir=self.library, playback_dir=self.playback,
            consumed_dir=self.consumed, owner_uid=self.uid, dashboard_uid=self.uid, engine_uid=self.uid,
            dashboard_gid=self.gid, control_gid=self.gid, preflight_check=self.preflight,
            native_reader=self.native, event_sender=self.send, stop=lambda: self.stops.append(True),
            clock=lambda: self.wall + self.elapsed, monotonic=lambda: self.elapsed,
            pause=lambda seconds: setattr(self, 'elapsed', self.elapsed + seconds))
        options.update(overrides)
        return worker.Dispatcher(**options)

    def preflight(self, request):
        self.checks.append(copy.deepcopy(request))
        return request['invocation_id']

    def send(self, request):
        # A durable consumption and preparing result precede the ONLY event.
        result = json.loads((self.state / worker.RESULT_NAME).read_text())
        self.assertEqual(result['phase'], 'preparing')
        ledger = json.loads((self.consumed / 'consumed.json').read_text())
        self.assertIn(request['request_id'], [item['request_id'] for item in ledger['requests']])
        snapshot = self.playback / (request['request_id'] + '.wav')
        self.assertEqual(snapshot.read_bytes(), self.raw)
        self.assertEqual(stat.S_IMODE(snapshot.stat().st_mode), 0o640)
        self.events.append(copy.deepcopy(request))

    def native(self):
        value = copy.deepcopy(self.start_state)
        if not self.events:
            return value
        value['message_job'] = self.request['request_id']
        value['message_sequence'] += 1
        if self.behavior == 'stale':
            return None
        if self.behavior == 'restarted':
            value['invocation_id'] = 'a' * 32
        if self.behavior == 'busy':
            value['message_state'] = 'rejected_busy'
        elif self.behavior == 'no-playing':
            value['message_state'] = 'finished'
        elif self.behavior == 'timeout':
            value.update(message_state='accepted', control_busy=True)
        elif self.elapsed < 0.1:
            value.update(message_state='accepted', control_busy=True)
        elif self.elapsed < (0.2 if self.behavior == 'too-short' else 1.4):
            value.update(message_state='playing', tx_on=True, control_busy=True, message_sequence=12)
        else:
            value.update(message_state='finished', message_sequence=13)
        return value

    def test_success_sends_once_after_durable_snapshot_and_consume_then_cleans_up(self):
        result = self.worker.run()
        self.assertEqual(result['phase'], 'finished')
        self.assertTrue(result['success'])
        self.assertEqual(len(self.events), 1)
        self.assertEqual(len(self.checks), 2)
        self.assertEqual(self.stops, [])
        self.assertEqual(list(self.playback.iterdir()), [])
        self.assertEqual(stat.S_IMODE((self.state / worker.RESULT_NAME).stat().st_mode), 0o640)
        worker.valid_result(result)
        self.assertEqual(self.worker.run(), result)
        self.assertEqual(len(self.events), 1)

    def test_busy_offline_or_changed_invocation_never_sends(self):
        for update in ({'rx_open': True}, {'tx_on': True}, {'control_busy': True}, {'online': False},
                       {'message_state': 'accepted'}, {'invocation_id': 'f' * 32}):
            with self.subTest(update=update):
                controller = self.make_worker(native_reader=lambda: dict(self.start_state, **update))
                result = controller.run()
                self.assertEqual(result['phase'], 'rejected')
                self.assertEqual(self.events, [])
                (self.state / worker.RESULT_NAME).unlink()
        changed = self.make_worker(preflight_check=lambda request: 'f' * 32)
        self.assertEqual(changed.run()['phase'], 'rejected')
        self.assertEqual(self.events, [])

    def test_old_or_excessively_future_request_is_cancelled_without_preflight(self):
        for offset in (-31, 6):
            self.request['requested_at'] = self.date(self.wall + offset)
            self.save(self.state / worker.REQUEST_NAME, self.request)
            self.assertEqual(self.worker.run()['phase'], 'cancelled')
            self.assertEqual(self.checks, [])
            (self.state / worker.RESULT_NAME).unlink()
        self.assertEqual(self.events, [])

    def test_asset_symlink_wrong_hash_wrong_pcm_format_and_unindexed_asset_never_send(self):
        original_index = (self.library / 'index.json').read_bytes()
        for corruption in ('symlink', 'hash', 'format', 'size', 'index'):
            with self.subTest(corruption=corruption):
                if self.asset.is_symlink():
                    self.asset.unlink()
                self.asset.write_bytes(self.raw); self.asset.chmod(0o640)
                (self.library / 'index.json').write_bytes(original_index)
                if corruption == 'symlink':
                    other = self.root / 'other.wav'; other.write_bytes(self.raw)
                    self.asset.unlink(); self.asset.symlink_to(other)
                elif corruption == 'hash':
                    self.asset.write_bytes(self.raw[:-2] + b'\xff\xff')
                elif corruption == 'format':
                    raw = bytearray(self.raw); raw[22] = 2
                    self.asset.write_bytes(raw)
                elif corruption == 'size':
                    self.asset.write_bytes(self.raw + b'extra')
                else:
                    self.save(self.library / 'index.json', {'schema': 1, 'messages': []})
                self.assertEqual(self.worker.run()['phase'], 'rejected')
                self.assertEqual(self.events, [])
                (self.state / worker.RESULT_NAME).unlink()

    def test_request_changed_during_copy_is_rejected(self):
        real_snapshot = self.worker.snapshot
        def changed(request, raw):
            path = real_snapshot(request, raw)
            self.save(self.state / worker.REQUEST_NAME, dict(request, pair_id='changed'))
            return path
        with patch.object(self.worker, 'snapshot', side_effect=changed):
            self.assertEqual(self.worker.run()['phase'], 'rejected')
        self.assertEqual(self.events, [])
        self.assertEqual(list(self.playback.iterdir()), [])

    def test_native_failure_restart_missing_playing_or_early_finish_stops_live(self):
        for behavior in ('stale', 'restarted', 'no-playing', 'too-short', 'timeout'):
            with self.subTest(behavior=behavior):
                self.behavior = behavior
                self.elapsed = 0
                self.events.clear(); self.stops.clear()
                result = self.worker.run()
                self.assertEqual(result['phase'], 'failed')
                self.assertFalse(result['success'])
                self.assertEqual(len(self.events), 1)
                self.assertEqual(self.stops, [True])
                self.assertLessEqual(self.elapsed, 40.001)
                self.assertEqual(list(self.playback.iterdir()), [])
                # Next fixture gets a new explicit request; no automatic retry.
                self.request['request_id'] = format(int(self.request['request_id'], 16) + 1, '032x')
                self.save(self.state / worker.REQUEST_NAME, self.request)

    def test_native_busy_rejects_without_stopping_or_retrying(self):
        self.behavior = 'busy'
        self.assertEqual(self.worker.run()['phase'], 'rejected')
        self.assertEqual(self.stops, [])
        self.worker.run()
        self.assertEqual(len(self.events), 1)

    def test_partial_event_failure_is_consumed_and_stops_live_without_replay(self):
        def send(request):
            self.events.append(request)
            raise OSError('partial event')
        controller = self.make_worker(event_sender=send)
        self.assertEqual(controller.run()['phase'], 'failed')
        controller.run()
        self.assertEqual(len(self.events), 1)
        self.assertEqual(self.stops, [True])

    def test_boot_pending_result_is_terminal_failed_without_replaying(self):
        for phase in ('preparing', 'accepted', 'playing'):
            with self.subTest(phase=phase):
                self.worker.result(self.request, phase)
                self.assertEqual(self.worker.run()['phase'], 'failed')
        self.assertEqual(self.events, [])
        self.assertEqual(len(self.stops), 3)

    def test_maintenance_contention_rejects_immediately_without_any_rf_operation(self):
        result = self.worker.reject_locked()
        self.assertEqual(result['phase'], 'rejected')
        self.assertEqual(self.checks, [])
        self.assertEqual(self.events, [])
        self.assertEqual(self.stops, [])
        self.worker.run()
        self.assertEqual(self.events, [])

    def test_lock_contention_does_not_overwrite_an_active_dispatch_result(self):
        self.worker.result(self.request, 'playing')
        self.assertEqual(self.worker.reject_locked()['phase'], 'playing')
        self.assertEqual(self.events, [])
        self.assertEqual(self.stops, [])

    def test_main_uses_nonblocking_maintenance_lock_and_no_delayed_dispatch(self):
        lock = self.root / 'maintenance.lock'; lock.touch(); lock.chmod(0o600)
        with patch.object(worker, 'LOCK_PATH', lock), patch.object(worker.os, 'geteuid', return_value=0), \
                patch.object(worker.os, 'fstat', return_value=SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_uid=0)), \
                patch.object(worker.fcntl, 'flock', side_effect=BlockingIOError) as flock, \
                patch.object(worker, 'Dispatcher') as dispatcher:
            self.assertEqual(worker.main([]), 0)
        self.assertEqual(flock.call_args.args[1], worker.fcntl.LOCK_EX | worker.fcntl.LOCK_NB)
        dispatcher.return_value.reject_locked.assert_called_once_with()
        dispatcher.return_value.run.assert_not_called()
        self.assertEqual(self.events, [])
        self.assertEqual(self.stops, [])

    def test_short_clip_can_prove_playing_transition_between_polls(self):
        states = [dict(self.start_state, message_job=self.request['request_id'],
                       message_state='finished', message_sequence=13)]
        controller = self.make_worker(native_reader=lambda: states[0])
        # The exact native +3 sequence proves accepted→playing→finished, even
        # when all three transitions occurred before this first polling read.
        self.assertEqual(controller.poll(self.request, 0.01, 10, 40), 'finished')
        states[0]['message_sequence'] = 12
        with self.assertRaises(ValueError):
            controller.poll(self.request, 0.01, 10, 40)
        states[0]['message_sequence'] = 14
        with self.assertRaises(ValueError):
            controller.poll(self.request, 0.01, 10, 40)

    def test_ledger_stops_reinserted_request_after_intervening_result(self):
        self.assertTrue(self.worker.consume(self.request))
        other = dict(self.request, request_id='e' * 32)
        self.worker.result(other, 'finished')
        self.assertEqual(self.worker.run()['phase'], 'cancelled')
        self.assertEqual(self.events, [])

    def test_ledger_has_fixed_bound_and_backward_clock_cannot_prune(self):
        entries = [{'request_id': format(i, '032x'), 'consumed_at': int(self.wall)} for i in range(256)]
        self.save(self.consumed / 'consumed.json', {'schema': 1, 'requests': entries})
        with self.assertRaisesRegex(worker.DispatchError, 'full'):
            self.worker.consume(self.request)
        self.wall -= 100
        with self.assertRaisesRegex(worker.DispatchError, 'full'):
            self.worker.consume(self.request)
        self.wall += 161
        self.assertTrue(self.worker.consume(self.request))
        data = json.loads((self.consumed / 'consumed.json').read_text())
        self.assertEqual(len(data['requests']), 1)

    def test_preparing_result_flush_failure_never_sends_and_consumed_job_never_replays(self):
        original = worker.atomic_json
        def fail(path, *arguments, **options):
            if path == self.state / worker.RESULT_NAME:
                raise OSError('storage unavailable')
            return original(path, *arguments, **options)
        with patch.object(worker, 'atomic_json', side_effect=fail):
            with self.assertRaises(OSError):
                self.worker.run()
        self.assertEqual(self.events, [])
        self.assertEqual(self.worker.run()['phase'], 'cancelled')
        self.assertEqual(self.events, [])

    def test_slow_preparation_never_starts_a_clip_that_exceeds_remaining_watchdog_budget(self):
        buffer = io.BytesIO()
        with wave.open(buffer, 'wb') as audio:
            audio.setnchannels(1); audio.setsampwidth(2); audio.setframerate(16000)
            audio.writeframes(b'\0\0' * worker.MAX_FRAMES)
        self.raw = buffer.getvalue()
        self.item.update(frames=worker.MAX_FRAMES, bytes=len(self.raw), sha256=hashlib.sha256(self.raw).hexdigest())
        self.request['sha256'] = self.item['sha256']
        self.asset.write_bytes(self.raw)
        self.save(self.library / 'index.json', {'schema': 1, 'messages': [self.item]})
        self.save(self.state / worker.REQUEST_NAME, self.request)
        def slow_preflight(request):
            self.elapsed += 6
            return self.preflight(request)
        controller = self.make_worker(preflight_check=slow_preflight)
        self.assertEqual(controller.run()['phase'], 'rejected')
        self.assertEqual(self.elapsed, 12)
        self.assertEqual(self.events, [])
        self.assertEqual(self.stops, [])
        self.assertFalse((self.consumed / 'consumed.json').exists())

    def test_slow_durable_consume_is_checked_again_before_event(self):
        original = self.worker.consume
        def slow_consume(request):
            consumed = original(request)
            self.elapsed = 39
            return consumed
        with patch.object(self.worker, 'consume', side_effect=slow_consume):
            self.assertEqual(self.worker.run()['phase'], 'cancelled')
        self.assertEqual(self.events, [])
        self.assertEqual(self.stops, [])

    def test_unknown_request_fields_and_untrusted_request_fail_before_root_result(self):
        self.save(self.state / worker.REQUEST_NAME, dict(self.request, command='danger'))
        with self.assertRaises(ValueError):
            self.worker.run()
        self.assertFalse((self.state / worker.RESULT_NAME).exists())
        self.save(self.state / worker.REQUEST_NAME, self.request)
        (self.state / worker.REQUEST_NAME).chmod(0o666)
        with self.assertRaises(ValueError):
            self.worker.run()
        self.assertEqual(self.events, [])

    def test_current_service_invocation_and_verified_pair_are_mandatory(self):
        result = SimpleNamespace(returncode=0, stdout='ActiveState=active\nInvocationID=' + '4' * 32 + '\nMainPID=123\n')
        runner = lambda *arguments, **options: result
        self.assertEqual(worker.live_invocation(runner), '4' * 32)
        result.stdout = result.stdout.replace('active', 'inactive')
        with self.assertRaises(ValueError):
            worker.live_invocation(runner)
        with patch.object(worker.guard, 'check', return_value={'catalog_digest': self.request['catalog_digest']}), \
                patch.object(worker.guard, 'trusted_json', return_value={'_pair': {
                    'pair_id': 'changed', 'catalog_digest': self.request['catalog_digest']}}), \
                patch.object(worker, 'live_invocation') as live:
            with self.assertRaises(ValueError):
                worker.preflight(self.request)
            live.assert_not_called()

    def test_native_reader_rejects_stale_malformed_symlink_and_wrong_permissions(self):
        folder = self.root / 'native'; folder.mkdir(mode=0o755)
        path = folder / 'state.json'
        self.save(path, self.start_state); path.chmod(0o644)
        self.assertEqual(worker.native_state(path, self.uid, lambda: 1000), self.start_state)
        self.assertIsNone(worker.native_state(path, self.uid, lambda: 6001))
        self.save(path, dict(self.start_state, online=1))
        self.assertIsNone(worker.native_state(path, self.uid, lambda: 1000))
        path.unlink(); path.symlink_to(self.state / worker.REQUEST_NAME)
        self.assertIsNone(worker.native_state(path, self.uid, lambda: 1000))

    def test_command_event_opens_only_fixed_validated_pty_and_never_a_serial_device(self):
        folder = self.root / 'command'; folder.mkdir(mode=0o750)
        path = folder / 'svxlink-command'
        path.symlink_to('/dev/ttyACM0')
        with patch.object(worker.os, 'open') as opener:
            with self.assertRaises((OSError, ValueError)):
                worker.send_event(self.request, path, self.uid)
            opener.assert_not_called()

    def test_boot_and_path_units_are_bounded_and_expose_only_private_runtime(self):
        unit = (ROOT / 'systemd/repeater-message-broadcast.service').read_text()
        path = (ROOT / 'systemd/repeater-message-broadcast.path').read_text()
        for expected in ('Type=oneshot', 'User=root', 'Group=repeater-control',
                         'Before=repeater-live.service', 'RuntimeDirectory=repeater-message-playback',
                         'RuntimeDirectoryMode=0750', 'StateDirectory=repeater-message-dispatcher',
                         'StateDirectoryMode=0700', 'TimeoutStartSec=65', 'WantedBy=multi-user.target'):
            self.assertIn(expected, unit)
        self.assertIn('PathChanged=/var/lib/repeater-dashboard/message-broadcast-request.json', path)
        self.assertIn('WantedBy=multi-user.target', path)
        self.assertNotIn('Restart=', unit)


if __name__ == '__main__':
    unittest.main()
