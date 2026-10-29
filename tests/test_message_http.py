"""Authenticated one-shot message HTTP flow and lightweight public activity."""
import copy
import http.client
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import quote
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'dashboard'))
import controls
import server as dashboard
from radio_messages import RadioMessages


class MessageHTTPTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password_record = controls.password_record('local-admin-test')

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.state = self.root / 'state'; self.state.mkdir(mode=0o700)
        self.library = self.root / 'messages'; self.library.mkdir(mode=0o750)
        recordings = self.root / 'recordings'; recordings.mkdir()
        admin = self.root / 'admin.json'; controls.write_json(admin, self.password_record)
        with patch.object(dashboard.AudioHub, 'start'), patch.object(dashboard.DtmfControl, 'start', return_value=False):
            self.server = dashboard.make_server('127.0.0.1', 0, recordings, self.state, admin)
        self.native = dict(schema=1, invocation_id='b'*32, heartbeat_ms=1000, online=True,
            rx_open=False, tx_on=False, control_busy=False, message_job='', message_state='idle', message_sequence=0)
        self.radio = dict(pending=False, verification_required=False, catalog_digest='c'*64,
            roles={'rx': {'verification_required': False}, 'tx': {'verification_required': False}},
            pair={'pair_id': 'pair2', 'stored_verified': True, 'catalog_changed': False,
                  'activation_required': False, 'verification_required': False,
                  'roles': {'rx': {'verified': True}, 'tx': {'verified': True}},
                  'plan': {'rx_hz': 145330000, 'tx_hz': 433525000, 'tone_hz': 88.5,
                           'name': 'BOX-RX2 / BOX-TX2', 'rx_name': 'BOX-RX2', 'tx_name': 'BOX-TX2', 'power': 'High'}},
            pair_switching={'presets': [{'id': 'pair2', 'label': 'Pair 2', 'rx_name': 'BOX-RX2', 'tx_name': 'BOX-TX2'}]})
        self.server.native_state.read = lambda: copy.deepcopy(self.native)
        self.server.programming.status = lambda: copy.deepcopy(self.radio)
        self.server.updates.status = lambda: {'pending': False}
        self.server.wifi.status = lambda: {'pending': False}
        self.server.dtmf.code_status = lambda **kwargs: {'pending': False}
        self.server.dtmf.status = lambda: {'available': False}
        self.server.monitor.status = lambda: {'version': dashboard.VERSION,
            'setup': {'rf_enabled': True, 'notes': [], 'phase': 'Live repeater'}, 'svxlink': {'mode': 'repeater'}}
        self.server.messages = RadioMessages(self.state, self.library, native_state_reader=lambda: copy.deepcopy(self.native),
            busy_check=lambda: dashboard.message_busy_reason(self.server), converter=self.convert,
            result_owner_uid=os.geteuid(), mutation_lock=self.server.mutation_lock)
        token, auth = self.server.auth.login('local-admin-test', '127.0.0.1')
        self.cookie = 'repeater_admin=' + token; self.csrf = auth['csrf_token']
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(3)
        self.temporary.cleanup()

    @staticmethod
    def convert(source, destination, extension):
        with wave.open(str(destination), 'wb') as audio:
            audio.setnchannels(1); audio.setsampwidth(2); audio.setframerate(16000)
            audio.writeframes(b'\x01\x00' * 16000)
        destination.chmod(0o600)

    def request(self, path, method='GET', data=None, *, auth=False, headers=None, raw=None):
        supplied = dict(headers or {})
        if auth:
            supplied.setdefault('Cookie', self.cookie); supplied.setdefault('X-CSRF-Token', self.csrf)
        if data is not None:
            raw = json.dumps(data).encode(); supplied.setdefault('Content-Type', 'application/json')
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        connection.request(method, path, body=raw, headers=supplied)
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        connection.close(); return result

    def upload(self, name='Field message.m4a'):
        code, _headers, body = self.request('/api/messages/upload', 'POST', auth=True, raw=b'encoded audio',
            headers={'Content-Type': 'application/octet-stream', 'X-File-Name': quote(name, safe='')})
        self.assertEqual(code, 201, body)
        return json.loads(body)['message']

    def broadcast(self, entry, **changes):
        value = dict(id=entry['id'], sha256=entry['sha256'], pair_id='pair2', catalog_digest='c'*64, invocation_id='b'*32)
        value.update(changes)
        return self.request('/api/messages/broadcast', 'POST', value, auth=True)

    def test_public_activity_is_cheap_fresh_and_has_no_admin_or_hardware_dependency(self):
        self.native.update(rx_open=True, tx_on=False)
        with patch.object(self.server.monitor, 'status', side_effect=AssertionError('No inventory')), \
             patch.object(self.server.programming, 'status', side_effect=AssertionError('No programming probe')), \
             patch.object(self.server.messages, 'status', side_effect=AssertionError('No library scan')):
            code, headers, body = self.request('/api/activity')
        self.assertEqual(code, 200); self.assertIn('no-store', headers['Cache-Control'])
        self.assertEqual(json.loads(body), {'available': True, 'receiving': True, 'transmitting': False})
        self.native = None
        self.assertEqual(json.loads(self.request('/api/activity')[2]), {'available': False, 'receiving': None, 'transmitting': None})

    def test_signed_out_status_hides_library_but_includes_verified_selected_pair_and_activity(self):
        self.upload('Private message.wav')
        code, _, body = self.request('/api/status'); value = json.loads(body)
        self.assertEqual(code, 200); self.assertEqual(value['radio_messages'], {'admin_required': True})
        self.assertNotIn('Private message', body.decode())
        self.assertEqual(value['setup']['radio_plan'], {'rx_mhz': '145.330000', 'tx_mhz': '433.525000',
            'tone_hz': 88.5, 'power': 'High', 'verified': True, 'label': 'Pair 2', 'pair_id': 'pair2', 'rx_name': 'BOX-RX2', 'tx_name': 'BOX-TX2'})
        self.assertEqual(value['radio_activity']['transmitting'], False)
        for field in ('pending', 'verification_required'):
            self.radio[field] = True
            self.assertFalse(json.loads(self.request('/api/status')[2])['setup']['radio_plan']['verified'])
            self.radio[field] = False
        self.radio['pair']['catalog_changed'] = True
        self.assertFalse(json.loads(self.request('/api/status')[2])['setup']['radio_plan']['verified'])

    def test_library_and_preview_require_admin_including_head_and_ranges(self):
        entry = self.upload()
        for path in ('/api/messages', entry['preview_url']):
            for method in ('GET', 'HEAD'):
                self.assertEqual(self.request(path, method, headers={'Range': 'bytes=0-3'})[0], 403)
        self.assertEqual(self.request('/api/messages', auth=True)[0], 200)
        self.assertEqual(self.request(entry['preview_url'], auth=True)[0], 200)

    def test_preview_bytes_ranges_head_and_invalid_paths_do_not_queue_transmission(self):
        entry = self.upload('Voice #1.mp3'); raw = (self.library / (entry['id'] + '.wav')).read_bytes()
        code, headers, body = self.request(entry['preview_url'], auth=True, headers={'Range': 'bytes=0-3'})
        self.assertEqual((code, body), (206, b'RIFF')); self.assertEqual(headers['Content-Type'], 'audio/wav')
        self.assertEqual(headers['Content-Range'], f'bytes 0-3/{len(raw)}'); self.assertEqual(headers['Accept-Ranges'], 'bytes')
        code, headers, body = self.request(entry['preview_url'], 'HEAD', auth=True, headers={'Range': 'bytes=-4'})
        self.assertEqual(code, 206); self.assertEqual(body, b''); self.assertEqual(headers['Content-Length'], '4')
        self.assertEqual(self.request(entry['preview_url'], auth=True, headers={'Range': 'bytes=999999999-'})[0], 416)
        for path in ('/api/messages/../audio', '/api/messages/' + 'f'*32 + '/audio', '/api/messages/%2e%2e/audio'):
            self.assertEqual(self.request(path, auth=True)[0], 404)
        self.assertFalse((self.state / RadioMessages.REQUEST).exists())

    def test_upload_authentication_and_origin_are_checked_before_large_body_read(self):
        headers = {'Content-Type': 'application/octet-stream', 'Content-Length': '8388608', 'X-File-Name': 'voice.wav'}
        # No payload is sent: a body read would wait until the client times out.
        self.assertEqual(self.request('/api/messages/upload', 'POST', headers=headers)[0], 403)
        self.assertEqual(self.request('/api/messages/upload', 'POST', headers={**headers, 'Cookie': self.cookie})[0], 403)
        self.assertEqual(self.request('/api/messages/upload', 'POST', auth=True,
            headers={**headers, 'Origin': 'http://unrelated.invalid'})[0], 403)
        self.assertEqual(list(self.library.iterdir()), [])

    def test_upload_rejects_invalid_framing_type_filename_and_empty_body(self):
        defaults = {'Content-Type': 'application/octet-stream', 'X-File-Name': 'voice.wav'}
        for changed in ({'Content-Length': '8388609'}, {'Content-Length': '-1'}, {'Content-Length': '0'},
                        {'Transfer-Encoding': 'chunked'}, {'Content-Type': 'application/json'},
                        {'X-File-Name': 'bad%ZZ.wav'}, {'X-File-Name': '..%2fescape.wav'}, {'X-File-Name': 'file.exe'}):
            with self.subTest(changed=changed):
                code, _, _ = self.request('/api/messages/upload', 'POST', auth=True, raw=b'x', headers={**defaults, **changed})
                self.assertEqual(code, 400)
        self.assertEqual(list(self.library.iterdir()), [])

    def test_upload_uri_name_and_canonical_library_result_are_not_a_broadcast(self):
        entry = self.upload('Voice message #2.M4A')
        self.assertEqual(entry['name'], 'Voice message #2.M4A'); self.assertEqual(entry['duration_seconds'], 1)
        self.assertEqual(entry['preview_url'], '/api/messages/' + entry['id'] + '/audio')
        status = json.loads(self.request('/api/messages', auth=True)[2])['radio_messages']
        self.assertEqual(status['messages'], [entry]); self.assertEqual(status['pair_id'], 'pair2')
        self.assertTrue(status['can_broadcast']); self.assertFalse((self.state / RadioMessages.REQUEST).exists())

    def test_upload_requires_one_content_length_and_current_session_after_body(self):
        for length_headers in ([], [('Content-Length', '1'), ('Content-Length', '1')]):
            connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
            try:
                connection.putrequest('POST', '/api/messages/upload')
                for key, value in [('Cookie', self.cookie), ('X-CSRF-Token', self.csrf),
                        ('Content-Type', 'application/octet-stream'), ('X-File-Name', 'voice.wav'), *length_headers]:
                    connection.putheader(key, value)
                connection.endheaders()
                response = connection.getresponse(); self.assertEqual(response.status, 400); response.read()
            finally:
                connection.close()
        with patch.object(self.server.auth, 'authorize', side_effect=[True, False]), \
             patch.object(self.server.messages, 'store_upload') as store:
            code, _, _ = self.request('/api/messages/upload', 'POST', auth=True, raw=b'audio',
                headers={'Content-Type': 'application/octet-stream', 'X-File-Name': 'voice.wav'})
        self.assertEqual(code, 403); store.assert_not_called()

    def test_broadcast_requires_admin_csrf_origin_and_exact_review_shape(self):
        entry = self.upload(); value = dict(id=entry['id'], sha256=entry['sha256'], pair_id='pair2', catalog_digest='c'*64, invocation_id='b'*32)
        route = '/api/messages/broadcast'
        self.assertEqual(self.request(route, 'POST', value)[0], 403)
        self.assertEqual(self.request(route, 'POST', value, headers={'Cookie': self.cookie})[0], 403)
        self.assertEqual(self.request(route, 'POST', value, auth=True, headers={'Origin': 'http://outside.invalid'})[0], 403)
        for invalid in ({**value, 'password': 'unnecessary'}, {key: val for key, val in value.items() if key != 'invocation_id'}, {**value, 'sha256': True}):
            self.assertEqual(self.request(route, 'POST', invalid, auth=True)[0], 400)
        self.assertFalse((self.state / RadioMessages.REQUEST).exists())

    def test_one_explicit_request_is_queued_once_and_interlocks_conflicting_mutations(self):
        entry = self.upload(); code, _, body = self.broadcast(entry)
        self.assertEqual(code, 202, body); request = (self.state / RadioMessages.REQUEST).read_bytes()
        value = json.loads(request); self.assertEqual(value['message_id'], entry['id']); self.assertEqual(value['pair_id'], 'pair2')
        self.assertNotIn('password', value); self.assertTrue(json.loads(body)['radio_messages']['job']['pending'])
        self.assertEqual(self.broadcast(entry)[0], 409); self.assertEqual((self.state / RadioMessages.REQUEST).read_bytes(), request)
        for route, payload in (('/api/settings', {'station_name': 'Changed'}), ('/api/recording', {'enabled': True}),
            ('/api/dtmf-control', {'enabled': True}), ('/api/station-identification', {'enabled': False}),
            ('/api/radio-editor/read', {'role': 'rx'}), ('/api/wifi-visibility', {'hidden': True}),
            ('/api/software-updates/check', {}), ('/api/messages/delete', {'id': entry['id'], 'sha256': entry['sha256']})):
            with self.subTest(route=route): self.assertEqual(self.request(route, 'POST', payload, auth=True)[0], 409)
        self.assertEqual(self.request('/api/admin/logout', 'POST', {}, auth=True)[0], 200)

    def test_stale_message_pair_catalog_invocation_and_busy_channel_never_queue(self):
        entry = self.upload()
        for changed in ({'sha256': 'e'*64}, {'pair_id': 'other'}, {'catalog_digest': 'e'*64}, {'invocation_id': 'e'*32}):
            with self.subTest(changed=changed): self.assertEqual(self.broadcast(entry, **changed)[0], 409)
        for field in ('rx_open', 'tx_on', 'control_busy'):
            self.native[field] = True; self.assertEqual(self.broadcast(entry)[0], 409); self.native[field] = False
        self.radio['pair']['stored_verified'] = False; self.assertEqual(self.broadcast(entry)[0], 409)
        self.assertFalse((self.state / RadioMessages.REQUEST).exists())

    def test_delete_needs_exact_digest_and_works_without_live_mode(self):
        entry = self.upload(); self.native = None
        payload = {'id': entry['id'], 'sha256': 'e'*64}
        self.assertEqual(self.request('/api/messages/delete', 'POST', payload, auth=True)[0], 409)
        payload['sha256'] = entry['sha256']; self.assertEqual(self.request('/api/messages/delete', 'POST', payload, auth=True)[0], 200)
        self.assertEqual(json.loads(self.request('/api/messages', auth=True)[2])['radio_messages']['messages'], [])

    def test_upload_reservation_releases_global_lock_and_rejects_mutations_during_conversion(self):
        entered = threading.Event(); release = threading.Event(); result = []
        def delayed(source, destination, extension):
            entered.set()
            if not release.wait(4): raise RuntimeError('Test conversion was never released')
            self.convert(source, destination, extension)
        self.server.messages.converter = delayed
        worker = threading.Thread(target=lambda: result.append(self.request('/api/messages/upload', 'POST', auth=True,
            raw=b'audio', headers={'Content-Type': 'application/octet-stream', 'X-File-Name': 'test.wav'})))
        worker.start()
        try:
            self.assertTrue(entered.wait(3)); self.assertTrue(self.server.messages.active)
            self.assertTrue(self.server.mutation_lock.acquire(blocking=False)); self.server.mutation_lock.release()
            self.assertEqual(self.request('/api/settings', 'POST', {'station_name': 'Changed'}, auth=True)[0], 409)
            self.assertEqual(self.request('/api/activity')[0], 200)
            self.assertTrue(json.loads(self.request('/api/messages', auth=True)[2])['radio_messages']['upload_pending'])
        finally:
            release.set(); worker.join(5)
        self.assertEqual(result[0][0], 201); self.assertFalse(self.server.messages.active)

    def test_outgoing_monitor_setting_defaults_true_validates_bool_and_survives_partial_updates(self):
        self.assertTrue(self.server.settings.get()['monitor_transmit_audio'])
        self.assertEqual(self.request('/api/settings', 'POST', {'monitor_transmit_audio': 'false'}, auth=True)[0], 400)
        self.assertEqual(self.request('/api/settings', 'POST', {'monitor_transmit_audio': False}, auth=True)[0], 200)
        self.assertEqual(self.request('/api/settings', 'POST', {'station_name': 'Local box'}, auth=True)[0], 200)
        self.assertFalse(controls.SettingsStore(self.state / 'settings.json').get()['monitor_transmit_audio'])

    def test_malformed_saved_pair_never_breaks_status_or_authorizes_broadcast(self):
        entry = self.upload()
        for plan in ({}, {'rx_hz': None}, {'rx_hz': '145330000', 'tx_hz': 433525000, 'tone_hz': 88.5},
                     {'rx_hz': 145330000, 'tx_hz': 433525000, 'tone_hz': float('inf')}):
            with self.subTest(plan=plan):
                self.radio['pair']['plan'] = plan
                code, _, body = self.request('/api/status')
                self.assertEqual(code, 200); self.assertIsNone(json.loads(body)['setup']['radio_plan'])
                self.assertEqual(self.broadcast(entry)[0], 409)

    def test_upload_io_error_does_not_expose_private_paths(self):
        with patch.object(self.server.messages, 'store_upload', side_effect=OSError('private /secret/file/path')):
            code, _, body = self.request('/api/messages/upload', 'POST', auth=True, raw=b'audio',
                headers={'Content-Type': 'application/octet-stream', 'X-File-Name': 'voice.wav'})
        self.assertEqual(code, 400); self.assertNotIn(b'/secret', body); self.assertNotIn(b'private', body)


if __name__ == '__main__':
    unittest.main()
